# istari-digital-fluent-client

Entity-oriented wrapper over the [Istari Digital](https://www.istaridigital.com/) Python client. Exposes systems, configurations, models, jobs, and artifacts as chainable objects instead of flat SDK calls.

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
ISTARI_PAT=your_personal_access_token
ISTARI_ENVIRONMENT_URL=https://fileservice-v2.demo.istari.app
```

## Entity hierarchy

```
IstariPlatform                (entry point)
  +-- SystemView              (wraps System)
  |     +-- .baseline                -> SnapshotView
  |     +-- .configurations          -> list[ConfigurationView]
  |     +-- .list_branches()         -> list[BranchView]
  |     +-- .get_branch(name)        -> BranchView
  |     +-- .create_branch(name, from_branch=...)  -> BranchView
  |     +-- .merge(from_branch=..., to_branch=..., message=...) -> BranchView
  +-- BranchView              (wraps SnapshotTag = Git-style branch)
  |     +-- .name / .snapshot_id / .is_baseline
  |     +-- .configuration           -> ConfigurationView (current working area)
  |     +-- .get_resources()         -> list[TrackedFile]
  |     +-- .get_subsystems()        -> list[Subsystem]
  |     +-- .get_history()           -> list[SnapshotTagRevision] (git log)
  |     +-- .get_snapshot_at(rev)    -> Snapshot
  |     +-- .add_resources(*ids)     -> self  (staged)
  |     +-- .remove_resources(*ids)  -> self  (staged)
  |     +-- .add_revisions(*revs)    -> self  (staged, LOCKED)
  |     +-- .add_subsystems(*ids)    -> self  (staged)
  |     +-- .remove_subsystems(*ids) -> self  (staged)
  |     +-- .commit(message)         -> self  (snapshot + advance pointer)
  |     +-- .archive() / .restore()
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
  +-- ModelView               (wraps Model)
  |     +-- .get_jobs()              -> list[JobView]
  |     +-- .submit_job()            -> JobView
  |     +-- .run_job()               -> JobView (submit + wait)
  +-- JobView                 (wraps Job)
  |     +-- .revision                -> FileRevision (job's output revision)
  |     +-- .get_products()          -> list[ProductView]   (race-safe)
  |     +-- .find_product()          -> ProductView | None
  |     +-- .wait()                  -> self (chainable)
  |     +-- .on_success()            -> self or raise
  +-- ProductView             (wraps Product = (revision, resource) pair)
  |     +-- .revision                -> FileRevision (exact rev the job wrote)
  |     +-- .resource                -> ResourceView | None
  |     +-- .download() / .read_bytes() / .read_text()
  |     +-- .promote()               -> ModelView
  +-- ResourceView            (wraps Resource: Artifact, Model, Job, ...)
        +-- .id / .type / .raw
```

## Usage

All examples assume:

```python
from istari_experimental import IstariPlatform, JobDefinition

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

### Submit a job and wait for results

```python
model = platform.find_model(name="MQ-99 Berserker SFR SYSML Model")

job = model.submit_job(JobDefinition(
    input_json_data={"key": "value"},
    function="@istari:extract",
    tool_name="cameo",
    operating_system="RHEL 8",
))

job.wait(timeout=600)

# Each ProductView points to the exact FileRevision the agent wrote
# (race-safe: unaffected by any later jobs that touch the same files).
for p in job.get_products():
    rev = p.revision
    print(p.name, p.mime, "rev=", rev.id, "file=", rev.file_id)
```

### Download a product

```python
job = platform.get_job("job-uuid")
report = job.find_product(name="report.json")
report.download("local_report.json")

# or read content directly
data = report.read_text()
```

### Promote a product to a model (job chaining)

Take a product written by a job, promote it to a standalone model with source
traceability, and add it to the configuration so a subsequent job can run on it.

```python
job = platform.get_job("job-uuid")
output = job.find_product(name="extraction_output.json")

# Promote and add to config in one chain
new_cfg = cfg.add_product_as_model(
    output,
    display_name="Extracted Data",
).save()

# Or promote separately and submit next job
model = output.promote(display_name="Extracted Data")
next_job = model.submit_job(JobDefinition(...))
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

### Branching

A *branch* is a `SnapshotTag` -- a named, mutable pointer to a `Snapshot`.
A *commit* is a new `Snapshot` of a `SystemConfiguration` (the working area).
Configurations are immutable once created, so `commit()` materialises any
staged add/remove of resources or subsystems by creating a fresh configuration,
snapshotting it, and advancing the tag to that new snapshot.

Working configurations created by branch operations are named
`branch:<branch_name>` (initial) and `branch:<branch_name>:<timestamp>`
on subsequent commits.

```python
system = platform.get_system("Berserker")

# List branches
for branch in system.list_branches():
    print(branch.name, branch.snapshot_id, "(baseline)" if branch.is_baseline else "")

# Look one up
main = system.get_branch("main")

# Create a branch (auto-seeds a README.md if no source/seeds given),
# stage resources/subsystems, commit -- all chainable.
feature = (
    system.create_branch("feature/antenna", from_branch="main")
          .add_resources("model-uuid-1", "model-uuid-2")
          .add_subsystems("system-uuid-A")
          .commit("Add antenna model and RF subsystem")
)

# Read live state from the branch's current configuration
resources  = feature.get_resources()    # list[TrackedFile]
subsystems = feature.get_subsystems()   # list[Subsystem]

# Branch history -- like `git log <branch>` -- newest first.
for rev in feature.get_history():
    print(rev.created, rev.created_by_id, rev.snapshot_id)
prev_snap = feature.get_snapshot_at(feature.get_history()[1])  # one commit back

# Further chained mutations
(feature
    .remove_resources("model-uuid-old")
    .add_resources("model-uuid-new")
    .commit("Swap legacy resource"))

# Merge a feature branch back into main (ours-overwrite -- no 3-way merge)
updated_main = system.merge(
    from_branch="feature/antenna",
    to_branch="main",
    message="Merge antenna feature",
)

# Soft-delete / restore a branch
feature.archive()
feature.restore()
```

Internals worth knowing:

- `add_resources(*ids)` / `remove_resources(*ids)` take **resource ids** (typically `Model.id`). Adds resolve to the model's current `file_id` and track LATEST.
- `add_subsystems(*ids)` takes other **`System.id`** values; the subsystem is pinned to that system's current baseline tag.
- Mutations are **staged locally** and applied atomically by `commit()`. Until you commit, `branch.has_pending_changes` is `True` and the live `get_resources()` / `get_subsystems()` reflect the *committed* state, not the pending edits.

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
experimental/
  istari_experimental/   package source (istari_utils.py, __init__.py)
  tests/                 pytest suite
  pyproject.toml         project and build config
  .env.example           environment variable template
```

See the [Istari SDK API reference](https://docs.istaridigital.com/developers/SDK/api_reference/) for the underlying client.
