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
  +-- SnapshotView            (wraps Snapshot)
  |     +-- .configuration           -> ConfigurationView
  +-- ConfigurationView       (wraps SystemConfiguration)
  |     +-- .get_models()            -> list[ModelView]
  |     +-- .get_tracked_files()     -> list[TrackedFile]
  |     +-- .add_file()              -> TrackedFileSet (builder)
  |     +-- .add_artifact_as_model() -> TrackedFileSet (builder)
  |     +-- .set_baseline()          -> self
  +-- TrackedFileSet           (builder for new configurations)
  |     +-- .add_file() / .add_artifact_as_model() / .add_revision()  -> self
  |     +-- .save(name=None)         -> ConfigurationView
  +-- ModelView               (wraps Model)
  |     +-- .get_jobs()              -> list[JobView]
  |     +-- .submit_job()            -> JobView
  |     +-- .run_job()               -> JobView (submit + wait)
  +-- JobView                 (wraps Job)
  |     +-- .get_artifacts()         -> list[ArtifactView]
  |     +-- .find_artifact()         -> ArtifactView | None
  |     +-- .wait()                  -> self (chainable)
  |     +-- .on_success()            -> self or raise
  +-- ArtifactView            (wraps Artifact)
        +-- .download() / .read_bytes() / .read_text()
        +-- .promote()               -> ModelView
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

for a in job.get_artifacts():
    print(a.name, a.mime)
```

### Download an artifact

```python
job = platform.get_job("job-uuid")
report = job.find_artifact(name="report.json")
report.download("local_report.json")

# or read content directly
data = report.read_text()
```

### Promote an artifact to a model (job chaining)

Take an artifact produced by a job, promote it to a standalone model with source traceability, and add it to the configuration so a subsequent job can run on it.

```python
job = platform.get_job("job-uuid")
output = job.find_artifact(name="extraction_output.json")

# Promote and add to config in one chain
new_cfg = cfg.add_artifact_as_model(
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

## Tests

```bash
uv sync --extra dev
uv run pytest tests/ -v
```

## Project layout

```
experimental/
  istari_experimental/   package source (istari_utils.py, __init__.py)
  tests/                 pytest suite
  pyproject.toml         project and build config
  .env.example           environment variable template
```

See the [Istari SDK API reference](https://docs.istaridigital.com/developers/SDK/api_reference/) for the underlying client.
