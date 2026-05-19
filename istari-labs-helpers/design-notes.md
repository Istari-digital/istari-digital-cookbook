# Design notes

Notes on a few subsystems of `istari_labs_helpers` that are not obvious from
the public API. Every chapter documents **what the platform actually does**,
**what the fluent client pretends is happening** (so the provenance / perf /
ergonomics story is clean), and **where in the code the mapping lives**.

## 1. Lineage

`ResourceView.get_lineage()` returns a backward-pointing `LineageNode` tree
rooted at the view's effective revision. Each node carries:

- `revision_id`, `file_id`, `name`, `display_name`, `created`
- `resource_type` / `resource_id` (`"Model"`, `"Artifact"`, `"Job"`, ...)
- `step`: `upload` | `job_run` | `promotion` | `derived`
- `relationship_to_child`: the `Source.relationship_identifier` as seen
  from the child (e.g. `"promoted_from"`)
- `function_name`: for Job nodes, the job's `function.name`
- `parents`: the recursively-built parents; empty at `upload`

### Raw platform graph vs fluent tree

The platform stores the raw source graph it needs; it is not always the graph
we wants to read. Two restructurings happen in `_build_lineage_node`.

**1. Jobs are first-class nodes, not anonymous revisions.**

When you call `add_job`, the SDK serialises the job's parameters into a
`parameters<hash>.json` blob and uploads it as a revision on the Job's own
file. Every output artifact then lists two sources:

- the input Model revision (content provenance), and
- the parameters revision on the Job's file (invocation metadata).

The fluent client resolves that parameters source by reading `Source.resource_type`
and `Source.resource_id` directly -- no `get_file` round-trip needed. It then
fetches the Job once (memoised inside the tree cache) to populate
`function_name` on the node. The label becomes `'@istari:extract (<job_id>)'`
instead of `Revision 'parameters1mob8mlm.json'`.

**2. Drop redundant siblings next to Job sources.**

Because the Model source and the Job source both appear at the artifact level,
printing both produces a bloated tree where the Model is duplicated (once as a
direct sibling of the Job, once again under the Job). The fluent rule: when a
revision has at least one Job source, keep only Job sources and
`promoted_from` sources at the current level. Non-Job, non-structural
siblings are pruned because they are already covered under the Job's own
source chain.

### The expected shape

For the tutorial notebook (Job 1 produces `workbook.xlsx`, auto-promoted to
Model, Job 2 extracts again):

```
- Artifact 'named_cells.json'                step=job_run
  - Job '@istari:extract (<job2-id>)'        step=job_run
    - Model 'workbook'                       step=promotion
      - Artifact 'workbook.xlsx'  [via promoted_from]   step=job_run
        - Job '@istari:extract (<job1-id>)'             step=job_run
          - Model 'Group3-UAS-Requirements (tutorial)'  step=upload
```

Read top-down: Job 2 produced `named_cells.json`; Job 2 ran on the promoted
`workbook` Model; that Model was promoted from the `workbook.xlsx` artifact;
`workbook.xlsx` was produced by Job 1, which ran on the original upload.

### Truncation and the DAG-as-tree cache

- `max_depth` (default `10`) caps recursion. Deeper nodes are returned with
  `truncated=True` and no parents.
- `_build_lineage_node(cache=...)` memoises by revision id so a diamond-shaped
  DAG (the same revision reached through multiple paths) is built once and
  referenced from multiple parents of the tree.

Code: `LineageNode`, `_classify_step`, `_build_lineage_node` in
`istari_labs_helpers/istari_utils.py`.

## 2. Auto-promote to Model when running a job on an Artifact

The platform's job API accepts only `model_id`, not an `artifact_id`: agents
consume Model revisions. The fluent client hides that constraint on
`ResourceView.run_job(...)` / `submit_job(...)`:

- **Model** -> submit directly.
- **Artifact** -> promote the effective revision to a new Model first, then
  submit on the promoted Model.
- **Anything else** -> `TypeError`.

The promotion is a `client.add_model(...)` call that links the new Model's
revision back to the artifact's revision via a source with
`relationship_identifier="promoted_from"`. That edge is what `get_lineage()`
later classifies as `step=promotion`, and it's how the UI's **Promote to
model** action records the same operation.

Two restrictions codified in `submit_job`:

- `save_input_as_revision=True` is rejected on the Artifact path. The
  auto-promoted Model is single-use; writing a new input revision onto it
  would mutate the lineage edge we just created. Users who need this must
  promote explicitly with `artifact.promote(...)`, then submit on the
  resulting Model.
- `promotion_relationship` is a `str | None` kwarg (default
  `"promoted_from"`) so the same dispatch code can produce promotions with
  a custom relationship tag if a workflow needs it.

Effect on the lineage tree: the `Artifact -> Job -> Model` chain described
above naturally materialises, because the input Model that Job 2 ran on was
the auto-promoted Model linked to `workbook.xlsx` via `promoted_from`.

Code: `ResourceView.submit_job` / `run_job` dispatch in
`istari_labs_helpers/istari_utils.py`, helper `_promote_revision_to_model`, helper
`_submit_job_impl`.

## 3. Lazy approach

Listing a job's products, or building a view from a revision id, is a pure
metadata operation from the user's point of view -- but the underlying SDK
requires two round-trips per product (`get_resource` + `get_revision`). If
the caller only inspects a subset, or only touches one view's `.name`, the
eager path wastes calls.

The fluent client defers both fetches via two mechanisms.

### `_LazyResource` (proxy for `Resource`)

A tiny proxy class that holds `(client, resource_type, resource_id)` and
exposes `id` + the resource-type hint synchronously:

```python
class _LazyResource:
    __slots__ = ("_client", "_resource_type", "_resource_id", "_loaded")
    @property
    def id(self): return self._resource_id
    def __getattr__(self, name):
        # load the real Resource on first non-id access, memoise, then forward
        return getattr(self._load(), name)
```

Key properties:

- `ResourceView.type` consults `_resource_type` directly, so dispatch
  (`ModelView` vs `ResourceView`) still works without loading the resource.
- `_make_resource_view(_LazyResource(...), ...)` picks the right class based
  on the hint.
- Calling `.file`, `.get_jobs()`, etc. triggers a single
  `client.get_resource(resource_type, resource_id)` and memoises the result
  inside the proxy.

### `_revision_loader` (callable on `ResourceView`)

A `Callable[[], FileRevision | None]` stored on the view. The `revision`
property calls it once on first access and swaps the result into
`_pinned_revision`:

```python
@property
def revision(self) -> FileRevision | None:
    if self._pinned_revision is None and self._revision_loader is not None:
        self._pinned_revision = self._revision_loader()
        self._revision_loader = None
    return self._pinned_revision or self.latest_revision
```

`is_pinned` returns `True` when either a pin is already fetched or a loader
is still deferred, so downstream code doesn't need to care which state the
view is in.

### Where it pays off

- `JobView.get_products(lazy=True)` (default) builds N views with zero
  `get_resource` and zero `get_revision` calls. `view.name` triggers exactly
  one `get_revision`; `view.file` triggers exactly one `get_resource`.
- `JobView.find_product` iterates those lazy views, short-circuits on match,
  and never loads the Artifact resource unless the caller touches it.

`lazy=False` is kept for callers who know they'll fully hydrate everything
and prefer to pay the cost up front.

Code: `_LazyResource`, `_make_revision_loader`, `_make_resource_view`,
`ResourceView.revision`/`ResourceView.is_pinned`, and
`JobView._build_product_views` in `istari_labs_helpers/istari_utils.py`.

## 4. Cache

The fluent client adds two caches over the SDK: one on `JobView` (products)
and one per `LineageNode` tree build. Both are safe because the underlying
data is immutable at the point we cache it.

### `JobView` product cache

Fields: `_products_cache: list[ResourceView] | None`, `_cache_terminal: bool`.

Safety invariant: a job's product list is immutable **once the job is
COMPLETED or FAILED**. Before that, agents can still write new products.

Flow in `get_products(refresh=False, lazy=True)`:

1. `refresh=True` drops the cache and forces a `get_job` round-trip.
2. If the cache exists and was populated while the job was terminal, return
   it directly (in-memory filter for `resource_type`). Zero API calls.
3. Otherwise: only call `get_job` when `self._job` is stale (`self.revision
   is None or not self._job.file`). Build views with `_build_product_views`.
   Cache the list only if the job is now terminal.

Running / pending jobs therefore never cache -- the caller always gets the
latest product list. Re-running a notebook cell like
`products = job.get_products()` on a completed job costs **0** API calls
after the first hit.

`find_product` builds on top of this: it walks `get_products()` views,
matches on `view.name` (memoised revision), and short-circuits on the first
hit. Repeat calls re-use the same views, so the second `find_product` for a
previously-matched name costs zero network calls.

### Lineage tree cache (per call)

`_build_lineage_node(cache={})` passes a per-call dict of
`revision_id -> LineageNode`. A diamond-shaped DAG (multiple paths to the
same revision) is built once and referenced from every parent that points
at it. The same cache also indirectly de-duplicates `get_job` calls for
Job-labelled nodes: when two artifacts reference the same Job's parameters
revision, we resolve that revision once, which means we look up the Job's
`function.name` only once.

The cache is local to a single `get_lineage()` call (never shared across
views) because revisions can gain new sources between calls and we want a
fresh, consistent tree each time the user asks.

### Not cached on purpose

- `platform.get_job(id)`, `platform.get_model(id)`, `platform.get_resource(...)` --
  these return fresh state because the caller just asked for it.
- `ResourceView.latest_revision` -- reads from `self._resource.file.revisions`,
  which is bound to the Resource object the view was built from. If you want
  a fresh latest revision, re-fetch via `platform.get_resource(...)`.

Code: `JobView._is_terminal`, `JobView.get_products`, `JobView.find_product`,
`_build_lineage_node(cache=...)` in `istari_labs_helpers/istari_utils.py`.

## 5. Chainable list queries (`ItemQuery` / `ResourceQuery`)

Fluent exposes v2 paginated `list_*` endpoints the same way many libraries expose
"queries you build first and run later": **immutable builders**, **lazy
evaluation**, and **terminals** that trigger the minimum network work. Think
Django `QuerySet` (chain `.filter()` / `.order_by()`, evaluate with `list()` or
`.first()`), SQLAlchemy 2.x `select().where()`, or LINQ's deferred execution —
not an ORM, but the same *ergonomic contract*.

### Mapping to the v2 client

Each factory on `IstariPlatform` binds a `Client` method that returns a
`Page*` model (subclass of `Pageable`):

- One initial `list_*` call is made with the accumulated kwargs (filters, sort,
  optional `size`; default iteration uses `size=100`, the server maximum, to cut
  round-trips — see `DEFAULT_PAGE_SIZE` in `queries.py`).
- **Iteration** delegates to the SDK's `page.iter_items()` on that response, so
  subsequent pages use the same `_list_method` / `_list_method_args` wiring the
  client attaches after deserialization. That matches exactly how v2 pagination is
  meant to be consumed; see `python-client-usage.md` (prefer `.iter_items()`
  over relying on `for x in page` where the page type doesn't implement it).
- **`count()` / `__len__`** issue a single `page=1`, `size=1` request and read
  `page.total`, mirroring the lightweight "metadata only" pattern in that guide.

`filter(**kwargs)` forwards keyword arguments verbatim to the underlying
`list_*` signature (same names the OpenAPI/SDK use: `archive_status`, `sort`,
`type_name` lists on resources, etc.). Passing `page` via `filter()` is unusual:
you start mid-result-set on purpose; the iterator will not revisit earlier
pages. `count()` always forces `page=1` and `size=1` so totals stay correct.

### `list_resources` vs `search_resources`

`ResourceQuery` is wired to **`client.list_resources`**: structured filters
(types, names, archive state, ...). The v2 **`search_resources`** method is a
different entry point — it takes a `FullTextSearch` payload for full-text
search and returns another page type. Cookbook prose that says "search
resources" in the informal sense means "list with filters"; fluent uses
`list_resources` for that. Use `search_resources` from the raw `Client` when you
need that dedicated FTS API.

### Subclass: `ResourceQuery`

`ResourceQuery` only adds `.type("model" | ...)` sugar that turns into
`filter(type_name=[...])` with a single `ResourceType` for `list_resources`. Everything else
inherits `ItemQuery` behaviour and stays immutable (`filter` / `sort` return new
instances via `type(self)(...)`).

Code: `ItemQuery`, `ResourceQuery` in `istari_labs_helpers/queries.py`; factories in
`IstariPlatform.resources`, `.systems`, `.jobs`, ... in
`istari_labs_helpers/istari_utils.py`.
