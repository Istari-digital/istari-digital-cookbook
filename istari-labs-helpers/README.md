# istari-labs-helpers

Entity-oriented wrapper over the [Istari Digital](https://www.istaridigital.com/) Python client. Exposes systems, configurations, models, jobs, and artifacts as chainable objects instead of flat SDK calls.

> **Status**: an opinionated, higher-level layer maintained alongside the official [`istari-digital-client`](https://docs.istaridigital.com/developers/SDK/api_reference/). It is intended to make common workflows shorter and safer, but it is not the officially supported SDK — for production integrations, the core client remains the source of truth.

## Install

This package is developed inside the [client cookbook](https://github.com/Istari-digital/istari-digital-client-cookbook). It is **not** published to PyPI as of this repository; install from a checkout:

```bash
pip install -e ./istari-labs-helpers
# or: uv pip install -e ./istari-labs-helpers
```

Or clone this cookbook, `cd` into `istari-labs-helpers/`, then sync dev dependencies with [uv](https://docs.astral.sh/uv/):

```bash
cd istari-labs-helpers
uv sync
```

Optional extras:

- `dev`: pytest, pytest-cov, black, ruff
- `experiment`: plotly, pandas, numpy, jupyter, ipykernel

```bash
uv sync --extra dev
uv sync --extra experiment
```

## Environment

Copy `.env.example` to `.env` and fill in your credentials (do not commit it):

```
ISTARI_REGISTRY_URL=https://fileservice-v2.demo.istari.app
ISTARI_PERSONAL_ACCESS_TOKEN=your_personal_access_token
```

## Entity hierarchy

```
IstariPlatform                (entry point)
  +-- .whoami()                      -> UserView
  +-- .find_user() / .get_user()     -> UserView
  +-- .client / .v3                  -> v2 Client / v3 V3Client (SDK escape hatches)
  +-- .resources()                   -> ResourceQuery  (lazy; .type("model") etc.)
  +-- .systems() / .jobs() / .tools() / .agents() / .files() / .artifacts() / ...
  |                                  -> ItemQuery or ToolQuery (lazy, chainable)
  +-- UserView                (wraps User)
  |     +-- .id / .email / .display_name
  |     +-- .tools()                 -> UserToolAccessQuery (execute grants for this user)
  |     +-- .granted_tools()         -> list[ToolView]
  +-- ToolView                (wraps Tool)
  |     +-- .id / .name / .functions / .function_count
  +-- SystemView              (wraps System)
  |     +-- .baseline                -> SnapshotView
  |     +-- .configurations          -> list[ConfigurationView]
  |     +-- .branches() / .get_branch()  -> BranchView (snapshot tags)
  |     +-- .download_resources()    -> BranchDownloadResult
  +-- BranchView              (wraps SnapshotTag — a branch)
  |     +-- .list_revisions()        -> list[SnapshotRevisionSearchItem]
  |     +-- .configuration           -> ConfigurationView
  |     +-- .advance_to(cfg)         -> self
  |     +-- .download_resources()    -> BranchDownloadResult
  |     +-- .subsystems()            -> list[SubsystemView]
  +-- SnapshotView            (wraps Snapshot)
  |     +-- .configuration           -> ConfigurationView
  +-- ConfigurationView       (wraps SystemConfiguration)
  |     +-- .get_models()            -> list[ModelView]
  |     +-- .get_tracked_files()     -> list[TrackedFile]
  |     +-- .add_file()              -> TrackedFileSet (builder)
  |     +-- .add_product_as_model()  -> TrackedFileSet (builder)
  |     +-- .set_baseline()          -> self
  +-- TrackedFileSet           (builder for new configurations)
  |     +-- .add_file() / .add_product_as_model() / .add_revision()  -> self
  |     +-- .save(name=None)         -> ConfigurationView
  +-- ResourceView            (unified wrapper over any Resource: Artifact, Model, ...)
  |     +-- .name / .filename / .mime / .file_id / .revision_id
  |     +-- .revision / .latest_revision / .pin(rev) / .unpinned
  |     +-- .read_bytes() / .read_text() / .download(dest)
  |     +-- .as_source()             -> NewSource (chain into next job)
  |     +-- .promote()               -> ModelView (tag: "promoted_from")
  |     +-- .get_lineage()           -> LineageNode
  |     +-- .submit_job() / .run_job()  (auto-promotes Artifact resources)
  +-- ModelView               (ResourceView specialised for Models)
  |     +-- .current_revision_id / .pinned_revision_id
  |     +-- .get_jobs() / .get_configurations()
  +-- JobView                 (wraps Job)
  |     +-- .revision                -> FileRevision (job's output revision)
  |     +-- .get_products()          -> list[ResourceView]  (each pinned to a product's revision)
  |     +-- .find_product()          -> ResourceView | None  (pinned)
  |     +-- .wait() / .on_success()
  +-- LineageNode             (one revision in a backward lineage tree)
        +-- .step 'upload' | 'job_run' | 'promotion' | 'derived'
        +-- .parents  / .walk() / .print_tree()
```

## Usage

All examples assume:

```python
from istari_labs_helpers import IstariPlatform, JobDefinition

platform = IstariPlatform.from_env()  # reads .env
```

### Who am I? List a user's tools

```python
me = platform.whoami()
print(me.id)                    # user uuid
print(me)                       # "Alice (alice@example.com)"

for tool in me.tools():         # tools you may execute
    print(tool.name, tool.function_count)

# Org-admin: another user's execute grants (Manage Tool Access)
user = platform.get_user("bob@example.com")
print(user.id)
for tool in user.tools():
    print(tool.id, tool.name)
print(f"{len(user.tools())} tool(s) with execute access")
```

To browse the **full tool catalog** visible to your admin token (not scoped to
one user), use ``platform.tools()`` instead.

### Browse a system's baseline models and their jobs

```python
system = platform.get_system("Berserker")
cfg = system.baseline.configuration

for model in cfg.get_models():
    print(model.name, model.current_revision_id)
    for job in model.get_jobs():
        print(f"  [{job.status}] {job.function_name}  {job.created}")
```

### Upload a model and add it to a configuration

Upload a local file as a model and track it in a new configuration derived from the baseline. The config name is auto-incremented (`v3` becomes `v4`).

```python
system = platform.get_system("System Under Design")
cfg = system.baseline.configuration

new_cfg = cfg.add_file(
    path="resources/model.mdzip",
    display_name="My SysML Model",
    external_identifier="my-model-ext-id",
).save()
```

### Add a file on a branch and advance the branch HEAD

```python
branch = system.get_branch("baseline")  # or any snapshot tag name
new_cfg = branch.configuration.add_file(
    path="report.html",
    display_name="report.html",
).save()
branch.advance_to(new_cfg)  # snapshot + move this branch tag
```

For the baseline tag only, `save().set_baseline()` is equivalent.

### Find a model with the lazy resource query

The platform exposes a chainable, immutable `ResourceQuery` that walks pages on
demand. `.type("model")` narrows to model resources; any other v2
`list_resources` filter (`display_name`, `file_name`, `external_identifier`,
`mime_type`, `archive_status`, ...) goes through `.filter(**kwargs)`.

```python
# First match (only fetches the first matching page)
item = platform.resources().type("model").filter(display_name="MQ-99").first()

# All matches under a sort order, capped at 5
recent = platform.resources().type("model").sort("-created").take(5)

# Total count without iterating
n = platform.resources().type("model").filter(archive_status="active").count()

# Lift an item to a full ModelView when you need the heavier shape
model = platform.get_model(item.id)
```

The same pattern works for `platform.systems()`, `platform.agents()`, `platform.jobs(model_id=...)`,
`platform.files()`, `platform.artifacts()`, `platform.snapshots()`,
`platform.functions()`, `platform.modules()`, and `platform.tools()`.

### Submit a job and wait for results

```python
item  = platform.resources().type("model").filter(display_name="MQ-99 SFR").first()
model = platform.get_model(item.id)

job = model.submit_job(JobDefinition(
    input_json_data={"key": "value"},
    function="@istari:extract",
    tool_name="cameo",
    operating_system="RHEL 8",
))

job.wait(timeout=600)

# Each product is a ResourceView pinned to the exact FileRevision the agent wrote
# (race-safe: unaffected by any later jobs that touch the same files).
for p in job.get_products():
    print(p.name, p.mime, "rev=", p.revision_id, "file=", p.file_id)
```

### Download a product

```python
job = platform.get_job("job-uuid")
report = job.find_product(name="report.json")
report.download("local_report.json")

# or read content directly
data = report.read_text()
```

### Run a job on an artifact (auto-promotion)

`run_job` / `submit_job` dispatch on resource type. Models go directly to the
platform; Artifacts are first auto-promoted to a Model (with the lineage edge
labelled `"promoted_from"`), then the job runs on the promoted Model.

```python
# Model -- direct
item  = platform.resources().type("model").filter(display_name="MQ-99 SFR").first()
model = platform.get_model(item.id)
job = model.run_job(JobDefinition(function="@istari:extract", tool_name="cameo"))

# Artifact -- auto-promoted under the hood
artifact = job.find_product(filename="extraction_output.json")   # pinned ResourceView
next_job = artifact.run_job(JobDefinition(function="@sysml:transform", tool_name="..."))
```

### Chain jobs via `as_source` (no promotion)

When you only need to feed a product as a source into another job (rather than
running the job *on* it), use `as_source()` -- no extra Model is created:

```python
src = job.find_product(filename="named_cells.json").as_source()
next_job = model.submit_job(JobDefinition(..., sources=[src]))
```

### Explicit promotion

Use `promote()` when you want a standalone, reusable Model instead of a
one-shot auto-promotion:

```python
output = job.find_product(name="extraction_output.json")
new_cfg = cfg.add_product_as_model(output, display_name="Extracted Data").save()

# Or promote, then do anything a Model can do
model = output.promote(display_name="Extracted Data")
next_job = model.submit_job(JobDefinition(...))
```

### Trace how something was created

```python
model.get_lineage().print_tree()
# - Model 'extracted.json' (rev=...)   step=promotion
#   - Artifact 'extracted.json' (rev=...)   step=job_run [via input]
#     - Model 'source.mdzip' (rev=...)   step=upload
```

### List all configurations of a system

```python
system = platform.get_system("Berserker")
for cfg in system.configurations:
    print(cfg.name, cfg.id)
```

### List branches on a system

```python
system = platform.get_system("Berserker")
for branch in system.branches():
    print(branch.name, len(branch.list_revisions()))
```

### Download all resources on a branch

```python
# Single file when one revision at branch HEAD; .zip when several
result = platform.download_system_resources(
    system.id,
    "baseline",
    dest="./exports",
)
print(result.path, result.is_zip, result.members)

# Or via SystemView
result = system.download_resources("baseline", dest="./exports")
result = system.get_branch("baseline").download_resources("./exports")
```

### Find a model in a configuration

```python
cfg = system.baseline.configuration

model = cfg.find_model(name="My SysML Model")
model = cfg.find_model(filename="Group3-UAS-Wing-v9.ntop")
model = cfg.find_model(external_id="dod-safe-berserker")
```

## Tests

### Unit tests (no platform required)

```bash
uv sync --extra dev
uv run pytest tests/ -m "not integration" -v
```

### Integration tests (live platform)

Set credentials in `.env`, then:

```bash
uv run pytest tests/integration/ -m integration -v
```

The integration suite uploads `Group3-UAS-Requirements.xlsx`, runs `open_spreadsheet @istari:extract`, and verifies that `named_cells.json`, `worksheet_data.json`, `workbook.pdf`, and `workbook.html` are produced. It also downloads `named_cells.json` and asserts it is valid JSON.

## Project layout

```
istari-labs-helpers/
  istari_labs_helpers/   package source (istari_utils.py, __init__.py)
  tests/                 pytest suite
  pyproject.toml         project and build config
  .env.example           environment variable template
```

See the [Istari SDK API reference](https://docs.istaridigital.com/developers/SDK/api_reference/) for the underlying client.
