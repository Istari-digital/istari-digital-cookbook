# Python Client Usage — Iterating Paginated Resources (v2)

This guide covers the iteration capabilities exposed by the v2 API
(`istari_digital_client.Client`, which extends `V2Api`). It applies to every
`Page*` response returned by a `list_*` or `search_*` method.

> v3 (`cursor_page_*`) uses cursor-based pagination and is **not** covered
> here.

## TL;DR

Every v2 `list_*` / `search_*` method returns a `Page*` object that supports:

| Method                | Returns                              | Use it when…                               |
|-----------------------|--------------------------------------|---------------------------------------------|
| `page.items`          | `list[T]` of items in **this page**  | You only need the current page              |
| `page.iter_items()`   | iterator of `T` across **all pages** | You want to walk the whole result set       |
| `page.iter_pages()`   | iterator of `Page*` (this page + 1 per subsequent fetch) | You need page-level metadata between fetches |

Pagination metadata is on the page itself: `page.total`, `page.page`,
`page.size`, `page.pages`.

```python
from istari_digital_client import Client, Configuration

client = Client(Configuration())

# Walk every model on every page.
for model in client.list_models().iter_items():
    print(model.id, model.name)
```

That single loop transparently fetches page 2, 3, … as you consume it.

---

## How it works

The mixin `istari_digital_client.mixin.Pageable` provides `iter_items()` and
`iter_pages()` to every `Page*` class. When a `list_*` method on `V2Api`
deserializes its response, it attaches the originating method and its
arguments to the returned page:

```python
resp_obj._list_method = self.list_models
resp_obj._list_method_args = {"page": page, "size": size, "filter_by": ..., ...}
```

`iter_items()` then:

1. Yields the items already in `self.items` (no extra HTTP call).
2. If `self.pages > self.page`, calls `_list_method(**base_args, page=current+1, size=size)` for each subsequent page, yielding their items.
3. Stops once `current_page > page.pages` (or when an empty page is returned).

`iter_pages()` does the same thing but yields `Page*` objects instead of
items.

### Defaults and limits

- `size` defaults to **10** when omitted, server-capped at **100** items per page.
- `page` is 1-indexed.
- `iter_items()` / `iter_pages()` respect any filter / sort args you passed to
  the initial `list_*` call — only `page` and `size` are mutated between
  fetches.

---

## Common entities

All examples assume:

```python
from istari_digital_client import Client, Configuration

client = Client(Configuration())
```

### Models

```python
for model in client.list_models().iter_items():
    print(model.id, model.name)
```

Smaller pages still iterate the entire result set:

```python
for model in client.list_models(size=25).iter_items():
    ...
```

### Resources (search)

```python
for resource in client.search_resources().iter_items():
    print(resource.id, resource.resource_type)
```

`search_resources` accepts the usual filter args (resource type, owner, etc.);
`iter_items()` keeps them stable across pages.

### Systems

```python
for system in client.list_systems().iter_items():
    print(system.id, system.name)
```

System-scoped collections work the same way:

```python
for cfg in client.list_system_configurations(system_id=system.id).iter_items():
    ...

for snapshot in client.list_snapshots().iter_items():
    ...
```

### Jobs

```python
for job in client.list_jobs().iter_items():
    print(job.id, job.status)
```

Per-model jobs:

```python
for job in client.list_model_jobs(model_id=model.id).iter_items():
    ...
```

### Files

```python
for file in client.list_files().iter_items():
    print(file.id, file.name)
```

### Artifacts and comments

```python
for artifact in client.list_artifacts().iter_items():
    ...

for artifact in client.list_model_artifacts(model_id=model.id).iter_items():
    ...

for comment in client.list_artifact_comments(artifact_id=artifact.id).iter_items():
    ...

for comment in client.list_model_comments(model_id=model.id).iter_items():
    ...
```

### Agents, modules, tools

```python
for agent in client.list_agents().iter_items():
    ...

for module in client.list_modules().iter_items():
    ...

for tool in client.list_tools().iter_items():
    ...
```

---

## Iterating pages instead of items

When you need page-level metadata (e.g. progress reporting, batch processing),
use `iter_pages()`:

```python
result = client.list_models(size=50)
print(f"{result.total} models across {result.pages} pages")

for page in result.iter_pages():
    print(f"Processing page {page.page}/{page.pages} ({len(page.items)} items)")
    bulk_process(page.items)
```

---

## `for x in page:` — legacy shortcut

For backward compatibility, a subset of `Page*` classes overrides `__iter__`
so that `for x in page:` is equivalent to `for x in page.iter_items():`:

- `PageArtifact`
- `PageComment`
- `PageFile`
- `PageJob`
- `PageModelListItem`
- `PageSnapshotItem`
- `PageSnapshotRevisionSearchItem`
- `PageTrackedFile`

```python
for model in client.list_models():        # works (legacy class)
    ...

for system in client.list_systems():      # does NOT walk pages — Pydantic default
    ...
```

For everything else (`PageSystem`, `PageAgent`, `PageModule`, `PageTool`,
`PageSnapshot`, `PageDocumentListItem`, …) **always call `.iter_items()`
explicitly**. To keep code uniform across all entities, prefer
`page.iter_items()` everywhere.

---

## All v2 paginated endpoints

Every entry below returns a `Page*` and supports `iter_items()` /
`iter_pages()`.

| Method                              | Page type                                  |
|-------------------------------------|---------------------------------------------|
| `list_access`                       | `PageAccess` *(via wrapped result)*         |
| `list_agent_pool_agents`            | `PageAgentPoolAgentMembershipWithAgent`     |
| `list_agent_pool_users`             | `PageAgentPoolUserMembershipWithUser`       |
| `list_agent_pools`                  | `PageAgentPool`                             |
| `list_agent_status_history`         | `PageAgentStatus`                           |
| `list_agents`                       | `PageAgent`                                 |
| `list_app_integrations`             | `PageAppIntegration`                        |
| `list_artifact_access`              | `PageAccess`                                |
| `list_artifact_comments`            | `PageComment`                               |
| `list_artifacts`                    | `PageArtifact`                              |
| `list_auth_integrations`            | `PageAuthIntegration`                       |
| `list_authors`                      | `PageModuleAuthor`                          |
| `list_configuration_documents`      | `PageDocumentListItem`                      |
| `list_configuration_revisions`      | `PageConfigurationRevisionSearchItem`       |
| `list_configuration_subsystems`     | `PageSubsystem`                             |
| `list_control_tags`                 | `PageControlTag`                            |
| `list_documents`                    | `PageDocumentListItem`                      |
| `list_files`                        | `PageFile`                                  |
| `list_functions`                    | `PageFunctionVersion`                       |
| `list_infosec_levels`               | `PageInfosecLevel`                          |
| `list_job_access`                   | `PageAccess`                                |
| `list_jobs`                         | `PageJob`                                   |
| `list_model_access`                 | `PageAccess`                                |
| `list_model_artifacts`              | `PageArtifact`                              |
| `list_model_comments`               | `PageComment`                               |
| `list_model_jobs`                   | `PageJob`                                   |
| `list_models`                       | `PageModelListItem`                         |
| `list_module_versions`              | `PageModuleVersion`                         |
| `list_modules`                      | `PageModule`                                |
| `list_operating_systems`            | `PageOperatingSystem`                       |
| `list_personal_access_tokens`       | `PagePersonalAccessToken`                   |
| `list_resource_type_permissions`    | `PageResourceTypePermission`                |
| `list_resources`                    | `PageResourceSearchItem`                    |
| `list_snapshot_items`               | `PageSnapshotItem`                          |
| `list_snapshot_revisions`           | `PageSnapshotRevisionSearchItem`            |
| `list_snapshot_subsystems`          | `PageSnapshotSubsystemItem`                 |
| `list_snapshots`                    | `PageSnapshot`                              |
| `list_system_configurations`        | `PageSystemConfiguration`                   |
| `list_systems`                      | `PageSystem`                                |
| `list_tags`                         | `PageSnapshotTag`                           |
| `list_tool_versions`                | `PageToolVersion`                           |
| `list_tools`                        | `PageTool`                                  |
| `list_tracked_files`                | `PageTrackedFile`                           |
| `list_upstream_remotes`             | `PageUpstreamRemote`                        |
| `list_users`                        | `PageUser`                                  |
| `search_resources`                  | `PageTypeVarCustomizedResourceSearchItem`   |

> Programmatic check: every class above is a subclass of
> `istari_digital_client.mixin.Pageable` and exposes `iter_items()` /
> `iter_pages()`. The integration test
> `tests/test_pageable_mixins.py::TestPageableIntrospection`
> enforces this for all `list_*` / `search_*` methods on `Client`.

---

## Performance and ergonomics tips

- **First page is free.** `iter_items()` yields the items already in
  `self.items` before issuing any extra HTTP call, so even single-page
  results don't pay for an extra round trip.
- **Pick a sensible `size`.** Larger pages reduce HTTP overhead but increase
  per-request latency and memory. The server caps `size` at 100.
- **Filters carry through.** Any keyword argument you passed to the initial
  `list_*` call (e.g. `archive_status="active"`, `sort="-created_at"`) is
  reused for every subsequent page fetch.
- **Don't mutate while iterating.** `iter_items()` is lazy. If you create or
  delete entities while iterating, results may shift between page fetches —
  collect into a list first if you need a stable snapshot:
  ```python
  models = list(client.list_models().iter_items())
  ```
- **Hand-built `Page*` instances need wiring.** If you construct a `Page*`
  yourself (e.g. in a unit test) without `_list_method` set, `iter_items()`
  will raise `ValueError("No list method defined for pagination")` as soon as
  it tries to fetch page 2. Use `client.list_*` to get a properly wired page.

---

## Verifying pagination at runtime

If you suspect a query is only returning one page, check the metadata first:

```python
page = client.list_models(size=2)
print("total =", page.total, "pages =", page.pages, "size =", page.size)

count = sum(1 for _ in page.iter_items())
print("iter_items yielded:", count)
assert count == page.total
```

To watch the actual HTTP traffic, enable debug logging:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("urllib3").setLevel(logging.DEBUG)
```

You should see one `GET …?page=1&size=2`, then `…page=2&size=2`, etc. as
`iter_items()` advances.
