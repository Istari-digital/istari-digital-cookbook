# istari-digital-fluent-client

Entity-oriented wrapper over the [Istari Digital](https://www.istaridigital.com/) Python client. Exposes systems, configurations, models, jobs, and artifacts as chainable objects instead of flat SDK calls.

> **Status**: an opinionated, higher-level layer maintained alongside the official [`istari-digital-client`](https://docs.istaridigital.com/developers/SDK/api_reference/). It is intended to make common workflows shorter and safer, but it is not the officially supported SDK -- for production integrations, the core client remains the source of truth.

## Install

```bash
pip install istari-digital-fluent-client
```

Or with [uv](https://github.com/astral-sh/uv) from source:

```bash
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
  +-- .resources()                   -> ResourceQuery  (lazy; .type("model") etc.)
  +-- .systems() / .jobs() / .agents() / .files() / .artifacts() / .snapshots() / ...
  |                                  -> ItemQuery      (lazy, chainable, immutable)
  +-- SystemView              (wraps System)
  |     +-- .baseline                -> SnapshotView
  |     +-- .configurations          -> list[ConfigurationView]
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
from istari_fluent import IstariPlatform, JobDefinition

platform = IstariPlatform.from_env()  # reads .env
```

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

### Add multiple files and set as baseline

```python
new_cfg = (
    cfg
    .add_file(path="resources/file_a.mdzip", display_name="File A")
    .add_file(path="resources/file_b.mdzip", display_name="File B")
    .save("v5")
    .set_baseline()
)
```

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
fluent/
  istari_fluent/         package source (istari_utils.py, __init__.py)
  tests/                 pytest suite
  pyproject.toml         project and build config
  .env.example           environment variable template
```

See the [Istari SDK API reference](https://docs.istaridigital.com/developers/SDK/api_reference/) for the underlying client.
