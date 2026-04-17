# Digital Thread Pattern Language for Istari Digital Client

After analyzing all Python code using `istari_digital_client` across `code-share-boeing` (2 scripts, 2 notebooks), `code-share-blue` (5 Python modules), `istari-digital-examples` (9 scripts, 16 notebooks, 1 CI runner), and `hackathons/istari_experimental` (1 facade module), we identified **35 patterns** organized into **8 top-level categories**.

---

## 1. Foundation

Patterns for establishing and managing connections to the Istari Digital platform.

### 1.1 Client Bootstrap

**What it does:** Creates a `Configuration` + `Client` from environment variables (registry URL and auth token). This is the entry point for every digital thread interaction.

**Variants observed:**
- **Inline** (code-share-boeing): `dotenv` load + direct construction, one-off per script
- **Factory** (code-share-blue): Centralized `make_client()` reused across all demos
- **Shared module** (istari-digital-examples): Dedicated `istari_client.py` module with `get_client()`, includes smoke-test via `get_current_user()`

**Implementation references:**

Inline bootstrap (Boeing):

```9:12:customer-bda-transfer/python_prototypes/3dx_extract.py
config = Configuration(
    registry_url=registry_url, registry_auth_token=registry_auth_token
)
client = Client(config=config)
```

Factory pattern (Blue):

```80:91:/Users/raphael/GitHub/istari-digital/code-share-blue/istari_commons/utils.py
def make_client(token: str | None = None) -> Client:
    """Create and return an Istari client with proper configuration"""
    if token is None:
        token = os.getenv("REG_AUTH_TOKEN") 
    REGISTRY_URL = os.getenv("REG_URL")
    return Client(
        config=Configuration(
            registry_url=REGISTRY_URL,
            registry_auth_token=token,
        )
    )
```

Shared module with connection verification (Examples):

```1:18:/Users/raphael/GitHub/istari-digital/istari-digital-examples/istari_client.py
"""Istari Digital SDK client helper - reusable connection setup."""
import os
from dotenv import load_dotenv
from istari_digital_client import Client, Configuration

load_dotenv()

def get_client() -> Client:
    config = Configuration(
        registry_url=os.getenv("ISTARI_DIGITAL_REGISTRY_URL"),
        registry_auth_token=os.getenv("ISTARI_DIGITAL_REGISTRY_AUTH_TOKEN"),
    )
    return Client(config)

if __name__ == "__main__":
    client = get_client()
    user = client.get_current_user()
    print(f"Connected as: {user.display_name} ({user.email})")
```

### 1.2 Authenticated External Source

**What it does:** Registers credentials for an external PLM system (Windchill, Teamwork Cloud) as a `FunctionAuthSecret`, then wraps it in a `NewSource` object that can be passed to extraction jobs. This bridges the digital thread to external enterprise tools.

**Implementation references:**

```38:51:/Users/raphael/GitHub/istari-digital/code-share-blue/demos/windchill_demo.py
def add_windchill_auth_source() -> NewSource:
    """Add Windchill authentication from windchill_secret.json."""
    # ... file check ...
    secret = client.add_function_auth_secret(
        path="windchill_secret.json",
        function_auth_type=FunctionAuthType.BASIC,
    )
    return NewSource(
        revision_id=secret.revision.id,
        relationship_identifier="windchill_auth",
    )
```

```39:52:/Users/raphael/GitHub/istari-digital/code-share-blue/demos/twc_demo.py
def add_twc_auth_source() -> NewSource:
    """Add TWC authentication from auth_secret.json."""
    # ... file check ...
    secret = client.add_function_auth_secret(
        path="auth_secret.json",
        function_auth_type=FunctionAuthType.BASIC,
    )
    return NewSource(
        revision_id=secret.revision.id,
        relationship_identifier="twc_auth_login",
    )
```

---

## 2. Ingestion

Patterns for getting data into the digital thread.

### 2.1 Single Model Upload

**What it does:** Uploads a single file (metadata or native format) as an Istari model with display name, description, and optional external identifier. This is the atomic "put data in" operation.

**Implementation references:**

```23:28:customer-bda-transfer/python_prototypes/3dx_extract.py
model = client.add_model(
    path=path,
    external_identifier="1.0.0",
    display_name="UAVOne",
    description="CATIA Demo"
)
```

```63:67:/Users/raphael/GitHub/istari-digital/code-share-blue/demos/windchill_demo.py
model = client.add_model(
    path="windchill_metadata.istari_windchill_metadata",
    display_name="Windchill CAD Model",
    description="Creo model extracted from Windchill PLM",
)
```

### 2.2 Metadata-Driven Batch Ingest

**What it does:** Reads a CSV containing product identifiers, generates one metadata JSON file per row (`.istari_dassault_3dexperience_metadata`), then uploads each as a model. This turns tabular data into a set of digital thread models.

**Implementation reference:**

```27:46:CSV-3DX-Batch-Process/csv-3dx-batch.py
def write_json(json_output_path, csv_data):
    json_file_list = []
    for row in csv_data:
        json_filename = json_output_path
        for data in row:
            if data:
                json_filename = json_filename+data+'_'
        json_filename = json_filename[:-1]+".istari_dassault_3dexperience_metadata"
        metadata_3dx = {
            "product_id": row[0],
            "major_revision": row[1],
            "minor_revision": row[2]
        }
        with open(json_filename, 'w') as file:
            json.dump(metadata_3dx, file, indent=4)
        json_file_list.append(json_filename)
    return json_file_list
```

### 2.3 Model Update (New Revision)

**What it does:** Updates an existing model with new content, creating a new revision. Used for iterative editing workflows where a model evolves over time (e.g., editing a SysML requirement and re-uploading). The `LATEST` specifier in tracked files automatically picks up new revisions.

**Seen in:** `update_and_extract_sysml.py`, `explore_sysml_clean.py`, `check_design_clean.py`

**Implementation reference:**

```92:104:/Users/raphael/GitHub/istari-digital/istari-digital-examples/use-cases/explore-sysml-model/update_and_extract_sysml.py
            # Re-upload as new revision
            with tempfile.NamedTemporaryFile(
                suffix=".sysml", mode="w", delete=False
            ) as tmp:
                tmp.write(content)
                tmp_path = Path(tmp.name)

            updated = client.update_model(
                model_id=args.model_id,
                path=tmp_path,
                description=f"Updated: {args.find} → {args.replace}",
            )
            tmp_path.unlink()
```

### 2.4 File Upload (Derived Assets)

**What it does:** Uploads standalone files using `add_file` (as opposed to `add_model`). Used for publishing derived assets like comparison data, README documents, or images that don't need the full model lifecycle. These files are tracked in system configurations alongside models.

**Seen in:** `create_system.ipynb` (validate-wing-aerodynamics)

**Implementation reference (from subagent analysis):**

```python
uploaded = {}
uploads = [
    ("aerodeck", "Aerodeck Metrics", "nTop aerodeck — L/D max 23.64, MTOW 479 lb"),
    ("iso", "Wing Geometry (Isometric)", "Group 3 UAS tailless flying wing"),
]
for key, display, desc in uploads:
    f = client.add_file(path=files[key], display_name=display, description=desc)
    uploaded[key] = f.id
```

---

## 3. Transformation

Patterns for running extraction/transformation jobs on models.

### 3.1 Extraction Job Submission

**What it does:** Submits an extraction job targeting a specific tool. This is what turns raw metadata/CAD into structured artifacts.

**Variants by function and tool:**

| Function | Tool | Source |
|----------|------|--------|
| `@istari:extract` | `dassault_3dexperience` | `3dx_extract.py`, `csv-3dx-batch.py` |
| `@istari:extract` | `microsoft_office_excel` | `istari_excel_module_extraction_demo.ipynb`, `extract_excel.ipynb` |
| `@istari:extract` | `microsoft_office_word` | `extract_word.ipynb` |
| `@istari:extract` | `microsoft_office_powerpoint` | `extract_powerpoint.ipynb` |
| `@istari:extract` | `pdf` | `extract_pdf.ipynb` |
| `@istari:extract` | `ptc_creo_parametric` | `extract_creo.ipynb` |
| `@istari:extract` | `dassault_catia_v5` | `extract_catia_v5.ipynb` |
| `@istari:extract` | `dassault_cameo` | `extract_cameo.ipynb` |
| `@istari:extract_input` | `nastran` | `extract_nastran_model.ipynb` |
| `@istari:extract_sysmlv2` | `sysgit` | `update_and_extract_sysml.py`, `explore_sysml_clean.py` |
| `@istari:windchill_extract` | `ptc_creo_parametric` | `windchill_demo.py` |
| `@istari:twc_extract` | `dassault_cameo` | `twc_demo.py` |

**Implementation references:**

Basic extraction (no auth):

```45:50:customer-bda-transfer/python_prototypes/3dx_extract.py
extraction_job = client.add_job(
    model_id  = model.id,
    function  = function,
    tool_name = tool_name,
    operating_system= operating_system,
)
```

Authenticated extraction (with PLM source):

```72:79:/Users/raphael/GitHub/istari-digital/code-share-blue/demos/windchill_demo.py
job = client.add_job(
    model_id=model.id,
    function="@istari:windchill_extract",
    tool_name="ptc_creo_parametric",
    tool_version="10.0.0.0",
    operating_system="Windows 11",
    sources=[add_windchill_auth_source()],
)
```

SysGit extraction with agent assignment (Examples):

```110:117:/Users/raphael/GitHub/istari-digital/istari-digital-examples/use-cases/explore-sysml-model/update_and_extract_sysml.py
    job = client.add_job(
        model_id=args.model_id,
        function=EXTRACT_FUNCTION,
        tool_name=TOOL_NAME,
        tool_version=TOOL_VERSION,
        operating_system=OPERATING_SYSTEM,
        parameters={},
    )
```

### 3.2 Job Polling (Manual Loop)

**What it does:** Polls `get_job` every 5 seconds until the status reaches `Completed` or `Failed`. This is the lower-level, manual approach used in the Boeing scripts. Gives control over timing display but is more verbose.

**Implementation reference:**

```60:64:customer-bda-transfer/python_prototypes/3dx_extract.py
while job.status.name.value not in ("Completed", "Failed"):
    elapsed_time = datetime.now().replace(microsecond=0)-start_time
    print(f"\rExtraction job {job_id} status: {job.status.name.value} ({elapsed_time})", end="")
    sleep(5)
    job = client.get_job(job_id)
```

### 3.3 Job Watch (SDK poll_job)

**What it does:** Uses the SDK's built-in `job.poll_job()` method instead of a manual loop, then checks the returned status. Cleaner, with error propagation via exceptions. This is the evolved pattern from code-share-blue.

**Implementation references:**

```71:99:/Users/raphael/GitHub/istari-digital/code-share-blue/istari_commons/jobs.py
def watch_job(client: Client, job_id: str) -> Model:
    job = client.get_job(job_id)
    logger.info(f"Watching job {job_id} ({job.function})")
    status = job.poll_job()
    if status.name in ["FAILED", "CANCELED"]:
        error_msg = f"Job {job_id} failed with status: {status.name}"
        logger.error(error_msg)
        raise RuntimeError(error_msg)
    model = client.get_model(job.model_id)
    return model
```

```142:164:/Users/raphael/GitHub/istari-digital/code-share-blue/istari_commons/jobs.py
def poll_job_with_error_handling(job: Any) -> None:
    job_description = f"Job {job.id} ({job.function})"
    status = job.poll_job()
    if status.name in ["FAILED", "CANCELED"]:
        error_msg = f"{job_description} failed with status: {status.name}"
        raise RuntimeError(error_msg)
```

### 3.4 Simulation Job (Parameterized)

**What it does:** Submits a simulation/analysis job (not just extraction) with a `parameters` dict containing the simulation configuration. This extends the digital thread from data extraction into computational analysis. Different tools require different parameter wrapping conventions.

**Variants by function:**

| Function | Parameter Style | Source |
|----------|----------------|--------|
| `@luminary:run_cfd` | `{"cfd_config": {"value": <dict>}}` | `run_luminary_cfd.py` |
| `@istari:run_pyintact_simulation` | `{"simulation_config": json.dumps(<dict>)}` (stringified) | `run_intact_simulation.py` |
| `@ntop:run_model` | direct parameters | `run_ntop_model.py` |

**Implementation references:**

Luminary CFD (wrapped dict):

```164:170:/Users/raphael/GitHub/istari-digital/istari-digital-examples/use-cases/run-luminary-cfd/run_luminary_cfd.py
    job: Job = client.add_job(
        model_id=model_id,
        function=FUNCTION,
        parameters={"cfd_config": {"value": cfd_config}},
        operating_system=OS(OPERATING_SYSTEM),
        assigned_agent_id=args.agent_id,
    )
```

PyIntact simulation (stringified JSON + multi-source):

```222:233:/Users/raphael/GitHub/istari-digital/istari-digital-examples/use-cases/run-intact-simulation/run_intact_simulation.py
    job_kwargs = dict(
        model_id=model_id,
        function=FUNCTION,
        parameters={"simulation_config": json.dumps(sim_config)},
        operating_system=OS(OPERATING_SYSTEM),
        assigned_agent_id=args.agent_id,
    )
    if sources:
        job_kwargs["sources"] = sources

    job: Job = client.add_job(**job_kwargs)
```

### 3.5 Multi-Source Job

**What it does:** Attaches additional model revisions to a job via `NewSource` objects with semantic `relationship_identifier` labels. This enables jobs that consume multiple inputs — e.g., a structural simulation that needs the primary geometry plus separate load and restraint face geometries.

**Seen in:** `run_intact_simulation.py`, `nightly/runner.py`, `windchill_demo.py`, `twc_demo.py`

**Implementation references:**

Building sources from uploaded models (PyIntact):

```188:201:/Users/raphael/GitHub/istari-digital/istari-digital-examples/use-cases/run-intact-simulation/run_intact_simulation.py
        if args.load_geometry:
            load_path = Path(args.load_geometry)
            print(f"Uploading load face: {load_path.name}")
            load_model = client.add_model(path=str(load_path), display_name="load-face")
            load_rev_id = load_model.file.revisions[0].id
            sources.append(NewSource(revision_id=load_rev_id, relationship_identifier="load_geometry_file"))

        if args.restraint_geometry:
            restr_path = Path(args.restraint_geometry)
            print(f"Uploading restraint face: {restr_path.name}")
            restr_model = client.add_model(path=str(restr_path), display_name="restraint-face")
            restr_rev_id = restr_model.file.revisions[0].id
            sources.append(NewSource(revision_id=restr_rev_id, relationship_identifier="restraint_geometry_file"))
```

Building sources from existing models (nightly runner):

```251:260:/Users/raphael/GitHub/istari-digital/istari-digital-examples/nightly/runner.py
    if test.get("sources_from_models") and NewSource:
        sources = []
        for src in test["sources_from_models"]:
            src_model = client.get_model(src["model_id"])
            rev_id = src_model.file.revisions[0].id
            sources.append(NewSource(
                revision_id=rev_id,
                relationship_identifier=src["relationship_identifier"],
            ))
        job_kwargs["sources"] = sources
```

### 3.6 Job Polling with Timeout

**What it does:** Extends the basic polling pattern with a safety timeout and a separate "pending timeout" that detects when no agent picks up the job. This prevents CI pipelines from hanging when agents are offline.

**Seen in:** `nightly/runner.py`

**Implementation reference:**

```64:101:/Users/raphael/GitHub/istari-digital/istari-digital-examples/nightly/runner.py
def poll_job(client, job_id, timeout_seconds=300, pending_timeout=120, interval=5):
    """Poll a job until COMPLETED, FAILED, or timeout.

    Two timeout thresholds:
      - pending_timeout: max seconds to wait if no agent picks up the job
      - timeout_seconds: max total seconds including execution time.
    """
    start = time.time()
    last_status_value = "Unknown"
    ever_started = False
    while True:
        elapsed = time.time() - start
        if elapsed > timeout_seconds:
            return None, elapsed, last_status_value
        if not ever_started and elapsed > pending_timeout:
            return None, elapsed, last_status_value
        time.sleep(interval)
        job = client.get_job(job_id)
        status = job.status.name
        last_status_value = status.value
        if status == JobStatusName.COMPLETED:
            return job, elapsed, last_status_value
        if status == JobStatusName.FAILED:
            return job, elapsed, last_status_value
        if last_status_value not in ("Pending", "Queued"):
            ever_started = True
```

---

## 4. Retrieval

Patterns for getting data out of the digital thread.

### 4.1 Artifact Download (Type-Dispatched)

**What it does:** After a job completes, refreshes the model via `get_model`, iterates `model.artifacts`, and writes each to disk. JSON artifacts are deserialized/re-serialized with formatting; binary artifacts are written as raw bytes. This is the most common retrieval pattern, appearing in almost every script.

**Implementation references:**

```83:92:customer-bda-transfer/python_prototypes/3dx_extract.py
for artifact in model.artifacts:
    print("Processing "+artifact.name+" ...")
    output_file_path = f"{output_base_path}//{artifact.name}"
    if artifact.extension in ["json"]:
        with open(output_file_path, "w") as f:
            f.write(artifact.read_text())
    else:
        with open(output_file_path, "wb") as f:
            f.write(artifact.read_bytes())
```

```103:117:/Users/raphael/GitHub/istari-digital/code-share-blue/demos/windchill_demo.py
for artifact in result_model.artifacts:
    artifact_path = output_path / artifact.name
    raw_bytes = artifact.read_bytes()
    if artifact.name.endswith(".json"):
        content = json.loads(raw_bytes.decode("utf-8"))
        with open(artifact_path, "w", encoding="utf-8") as f:
            json.dump(content, f, indent=2, ensure_ascii=False)
    else:
        with open(artifact_path, "wb") as f:
            f.write(raw_bytes)
```

### 4.2 Filtered Artifact Download

**What it does:** Downloads artifacts but only those matching a name filter set. Supports both in-memory return (as a dict) and disk writes. Raises `FileNotFoundError` if no match. This is the library-quality version of 4.1.

**Implementation reference:**

```97:159:/Users/raphael/GitHub/istari-digital/code-share-blue/istari_commons/utils.py
def download_artifacts(
    model_id: str,
    path: str | None = None,
    filter_name: set[str] | None = None,
) -> dict[str, bytes | dict[str, Any]]:
    client = make_client()
    model = client.get_model(model_id)
    artifacts = {}
    # ... mkdir if path ...
    for artifact in model.artifacts:
        if filter_name is not None and artifact.name not in filter_name:
            continue
        raw_bytes = artifact.read_bytes()
        if artifact.name.endswith(".json"):
            content = json.loads(raw_bytes.decode("utf-8"))
        else:
            content = raw_bytes
        # ... save or store in dict ...
    return artifacts
```

### 4.3 Revision History Export

**What it does:** Walks `model.revisions`, creates timestamped directories, and for each revision downloads both the model revision file and all linked artifact revisions (via `revision.products` filtered by `resource_type == 'Artifact'`). This is a traceability pattern for auditing the full history of a model's evolution through the digital thread.

**Implementation reference:**

```49:91:/Users/raphael/GitHub/istari-digital/code-share-blue/demos/model_artifact_history_by_model_revision_id.py
def download_model_revision(revision, rev_dir: Path) -> None:
    (rev_dir / revision.name).write_bytes(client.get_revision(revision.id).read_bytes())

def download_artifact_revisions(revision, rev_dir: Path) -> None:
    artifact_products = [p for p in getattr(revision, 'products', []) 
                       if p.resource_type == 'Artifact']
    # ...
    for art_idx, product in enumerate(artifact_products, 1):
        artifact_revision = client.get_revision(product.revision_id)
        (artifacts_dir / artifact_revision.name).write_bytes(artifact_revision.read_bytes())

# Main loop
for idx, revision in enumerate(revisions, 1):
    timestamp = revision.created.strftime("%Y%m%d_%H%M%S")
    dir_name = f"{timestamp} -- {revision.id}"
    rev_dir = output_path / dir_name
    download_model_revision(revision, rev_dir)
    download_artifact_revisions(revision, rev_dir)
```

### 4.4 Artifact JSON Read (Convenience)

**What it does:** Uses `artifact.read_json()` to directly parse a JSON artifact into a Python dict, avoiding the manual `read_bytes()` → `json.loads()` dance. Also `artifact.read_text()` and `json.loads(artifact.read_text())` are used. This is the cleanest retrieval for structured data.

**Seen in:** `run_luminary_cfd.py`, `run_intact_simulation.py`, `explore_sysml_clean.py`

**Implementation references:**

```191:200:/Users/raphael/GitHub/istari-digital/istari-digital-examples/use-cases/run-luminary-cfd/run_luminary_cfd.py
        for artifact in model.artifacts:
            rev = artifact.file.revisions[0] if artifact.file.revisions else None
            if rev and rev.name == "results.json":
                results = artifact.read_json()
                lift = results.get("lift_converged", 0)
                drag = results.get("drag_converged", 0)
                ld = lift / drag if drag else 0
                print(f"\nAerodynamic Results:")
                print(f"  Lift:  {lift:,.1f} N ({lift * 0.2248:,.1f} lbf)")
                print(f"  L/D:   {ld:.2f}")
```

```186:198:/Users/raphael/GitHub/istari-digital/istari-digital-examples/use-cases/explore-sysml-model/explore_sysml_clean.py
def count_extraction_results(model_id):
    model = client.get_model(model_id)
    reqs_data = parts_data = None
    for a in model.artifacts:
        rev = a.file.revisions[0] if a.file.revisions else None
        if rev and "requirements" in rev.name and rev.name.endswith(".json"):
            reqs_data = json.loads(a.read_text())
        elif rev and "parts" in rev.name and rev.name.endswith(".json"):
            parts_data = json.loads(a.read_text())
    n_reqs = len(reqs_data) if reqs_data else 0
    n_parts = len(parts_data) if parts_data else 0
    return n_reqs, n_parts, reqs_data, parts_data
```

### 4.5 Direct Revision Content Read

**What it does:** Fetches a specific file revision by ID and reads its raw content. Used for one-off retrieval of known artifacts (e.g., exporting credentials or specific files). This is the most granular retrieval pattern.

**Implementation reference:**

```113:123:/Users/raphael/GitHub/istari-digital/code-share-blue/demos/twc_demo.py
from istari_digital_client import FileRevision

revision: FileRevision = client.get_revision(revision_id)
contents = revision.read_contents()

with open("job_auth_secret.txt", "wb") as file:
    file.write(contents)
```

---

## 5. Governance

Patterns for controlling access, classification, and discovery within the digital thread.

### 5.1 Control Tag Assignment

**What it does:** Looks up a control tag by name from the platform's tag list, then applies it to a model. Tags are the primary classification mechanism for organizing models in the digital thread.

**Implementation reference:**

```86:93:CSV-3DX-Batch-Process/csv-3dx-batch.py
control_tags = client.list_control_tags()
for tag in control_tags:
    if tag.name == control_tag_name:
        control_tag_id = tag.id

# ... later, per model:
output = client.add_model_control_taggings(model.id, [control_tag_id])
```

### 5.2 Access Sharing

**What it does:** Grants access to resources for specific users. Two approaches exist:
- **By user ID** (code-share-boeing): Resolve user IDs via `list_users`, then call `model.create_access` per user.
- **By email / cross-tenant** (istari-digital-examples): Use `create_access_by_email_for_other_tenants` to share with users by email, supporting cross-organization sharing on both SYSTEM and MODEL resource types.

**Implementation references:**

By user ID (Boeing):

```106:144:CSV-3DX-Batch-Process/csv-3dx-batch.py
full_user_list = client.list_users()
for user_share in user_share_list:
    for user in full_user_list:
        if user_share in user.user_name:
            user_share_id_list.append(user.id)

# ... per model:
relation = AccessRelation.VIEWER
subject_type = AccessSubjectType.USER
for user in user_share_id_list:
    new_access_relationship = model.create_access(
        subject_type=subject_type,
        subject_id=user,
        relation=relation,
    )
```

By email / cross-tenant (Examples — shares system + all its files):

```18:26:/Users/raphael/GitHub/istari-digital/istari-digital-examples/getting-started/03_share_resources.py
def share_resource(client, resource_type, resource_id, email, relation):
    """Share a resource with a user by email."""
    client.create_access_by_email_for_other_tenants(
        subject_type=AccessSubjectType.USER,
        email=email,
        resource_type=resource_type,
        resource_id=resource_id,
        access_relationship=relation,
    )
```

Sharing system + all tracked files (Examples):

```64:82:/Users/raphael/GitHub/istari-digital/istari-digital-examples/getting-started/03_share_resources.py
    # Share the system
    share_resource(client, AccessResourceType.SYSTEM, args.system_id, args.email, relation)

    # Collect all file IDs from the system's configurations
    configs = client.list_system_configurations(args.system_id, page=1, size=50)
    file_ids = set()
    for config in configs.items:
        tracked = client.list_tracked_files(config.id, page=1, size=50)
        for tf in tracked.items:
            file_ids.add(tf.file_id)

    # Share each file
    for file_id in file_ids:
        try:
            share_resource(client, AccessResourceType.FILE, file_id, args.email, relation)
        except Exception as e:
            print(f"  Warning: could not share file {file_id}: {e}")
```

### 5.3 Paginated Catalog Listing

**What it does:** Pages through all models in the platform using `list_models` with `page`/`size` parameters. Useful for catalog exploration, bulk operations, and auditing the full scope of the digital thread.

**Implementation reference:**

```49:59:/Users/raphael/GitHub/istari-digital/code-share-blue/istari_commons/jobs.py
def list_all_models(client: Client) -> list[Model]:
    models = client.list_models(size=100)
    all_models = []
    assert models.pages
    for _ in range(models.pages):
        assert models.page
        logger.info(f"Page {models.page} of {models.pages}")
        all_models.extend(models.items)
        models = client.list_models(page=models.page + 1, size=100)
    return all_models
```

### 5.4 Bulk Model Archival

**What it does:** Archives a list of models with error tolerance (try/except per model). Used for cleanup and lifecycle management of the digital thread.

**Implementation reference:**

```62:68:/Users/raphael/GitHub/istari-digital/code-share-blue/istari_commons/jobs.py
def archive_models(client: Client, models: list[Model]) -> None:
    for model in models:
        try:
            client.archive_model(model.id)
        except Exception as e:
            logger.error(e)
```

### 5.5 Snapshot & Tagging

**What it does:** Creates immutable snapshots of a system configuration at key milestones, then tags them with human-readable labels. Snapshots capture the state of all tracked files at a point in time. Tags like `v1-baseline`, `v2-extracted` let you navigate the evolution of the digital thread.

**Seen in:** `explore_sysml_clean.py`, `check_design_clean.py`, all extract notebooks in `istari-digital-examples`

**Implementation reference:**

```132:148:/Users/raphael/GitHub/istari-digital/istari-digital-examples/use-cases/explore-sysml-model/explore_sysml_clean.py
def snapshot_and_tag(config_id, tag_name, description=""):
    snap = client.create_snapshot(config_id, NewSnapshot(description=description))
    actual = snap.actual_instance
    if hasattr(actual, "id"):
        snap_id = actual.id
        client.create_tag(snap_id, NewSnapshotTag(tag=tag_name))
        print(f"    Snapshot {snap_id[:8]}... tagged: {tag_name}")
        return snap_id
    else:
        snaps = client.list_snapshots(configuration_id=config_id, page=1, size=1)
        if snaps.items:
            snap_id = snaps.items[0].id
            client.create_tag(snap_id, NewSnapshotTag(tag=tag_name))
            print(f"    NoOp — tagged existing snapshot as: {tag_name}")
            return snap_id
        return None
```

---

## 6. Structure

Patterns for organizing models into higher-level digital thread structures.

### 6.1 System Creation

**What it does:** Creates a named system that groups related models, configurations, and snapshots into a cohesive digital thread. Systems are the top-level organizational unit.

**Seen in:** `explore_sysml_clean.py`, `check_design_clean.py`, `run_luminary_cfd.py`, `run_intact_simulation.py`

**Implementation reference:**

```733:744:/Users/raphael/GitHub/istari-digital/istari-digital-examples/use-cases/explore-sysml-model/explore_sysml_clean.py
system = client.create_system(
    new_system=NewSystem(
        name="Example: Explore SysML Model",
        description=(
            "SysML v2 design evolution through 4 iterations — baseline through "
            "enhanced payload. SysGit extraction at each step reveals requirements "
            "and architecture without needing a SysML editor."
        ),
    )
)
SID = system.id
```

### 6.2 System Configuration with Tracked Files

**What it does:** Creates configurations within a system, each tracking a set of files using `LATEST` specifiers. As tracked files get new revisions (via `update_model`), the next snapshot automatically picks up the latest versions. Multiple configurations can track overlapping file sets for different views of the same digital thread.

**Seen in:** `explore_sysml_clean.py`, `check_design_clean.py`

**Implementation reference:**

```129:131:/Users/raphael/GitHub/istari-digital/istari-digital-examples/use-cases/explore-sysml-model/explore_sysml_clean.py
def track(file_id):
    return NewTrackedFile(specifier_type=TrackedFileSpecifierType.LATEST, file_id=file_id)
```

```786:810:/Users/raphael/GitHub/istari-digital/istari-digital-examples/use-cases/explore-sysml-model/explore_sysml_clean.py
# Config 1: System Documentation (tracks README + notebook)
config1 = client.create_configuration(
    system_id=SID,
    new_system_configuration=NewSystemConfiguration(
        name="System Documentation",
        tracked_files=[track(README_FILE_ID), track(NB_FILE_ID)],
    ),
)

# Config 2: Design Evolution (tracks README + notebook + SysML model)
config2 = client.create_configuration(
    system_id=SID,
    new_system_configuration=NewSystemConfiguration(
        name="Design Evolution",
        tracked_files=[
            track(README_FILE_ID),
            track(NB_FILE_ID),
            track(SYSML_FILE_ID),
        ],
    ),
)
```

---

## 7. Orchestration

These are composite patterns that compose the above primitives into end-to-end workflows. These represent the highest-value patterns in the digital thread.

### 7.1 Linear Extract Pipeline

**What it does:** The canonical digital thread recipe: **Upload model -> Submit extraction job -> Poll until done -> Download artifacts**. Every script in both repos implements some variant of this. It's the fundamental "import and extract" workflow.

**Seen in:** `3dx_extract.py`, `windchill_demo.py`, `twc_demo.py`, `istari_excel_module_extraction_demo.ipynb`

**Schematic:**

```
add_model() --> add_job() --> poll_job()/get_job() loop --> get_model() --> iterate artifacts --> read_bytes()
```

**Implementation reference (cleanest version):**

```54:119:/Users/raphael/GitHub/istari-digital/code-share-blue/demos/windchill_demo.py
def extract_windchill_model() -> None:
    # 1. Upload
    model = client.add_model(
        path="windchill_metadata.istari_windchill_metadata",
        display_name="Windchill CAD Model",
        description="Creo model extracted from Windchill PLM",
    )
    # 2. Submit job (with auth source)
    job = client.add_job(
        model_id=model.id,
        function="@istari:windchill_extract",
        tool_name="ptc_creo_parametric",
        tool_version="10.0.0.0",
        operating_system="Windows 11",
        sources=[add_windchill_auth_source()],
    )
    # 3. Poll
    status = job.poll_job()
    # ... error check ...
    # 4. Retrieve
    result_model = client.get_model(job.model_id)
    # 5. Download artifacts
    for artifact in result_model.artifacts:
        raw_bytes = artifact.read_bytes()
        # ... write to disk ...
```

### 7.2 Governed Batch Pipeline

**What it does:** The most complex orchestration pattern: reads a CSV of models, generates metadata files, then for **each** model executes: upload -> tag -> share with N users -> extract -> poll. This combines Ingestion (2.2), Governance (5.1, 5.2), and Transformation (3.1, 3.2) into a complete governed batch workflow.

**Seen in:** `csv-3dx-batch.py` (only instance)

**Schematic:**

```
read_csv() --> write_json() per row
                    |
              for each file:
                add_model()
                  --> add_model_control_taggings()
                  --> create_access() x N users
                  --> add_job()
                  --> monitor_job() [poll loop]
```

**Implementation reference:**

```118:157:CSV-3DX-Batch-Process/csv-3dx-batch.py
for currfile in json_file_list:
    # ... read metadata for display name ...
    model = client.add_model(path=currfile, display_name=extracted_name, ...)
    output = client.add_model_control_taggings(model.id, [control_tag_id])
    for user in user_share_id_list:
        model.create_access(subject_type=subject_type, subject_id=user, relation=relation)
    extraction_job = client.add_job(model_id=model.id, function="@istari:extract", tool_name="dassault_3dexperience")
    monitor_job(extraction_job.id, start_time, False)
```

### 7.3 Edit-Extract-Verify Loop

**What it does:** Downloads a model's content, edits it locally (e.g., changing a requirement value), re-uploads as a new revision via `update_model`, then runs extraction to verify the change. This is the "inner loop" for iterative design — enabling engineers to modify and re-validate without leaving the digital thread.

**Seen in:** `update_and_extract_sysml.py`, `check_design_clean.py`

**Schematic:**

```
get_model() --> read_text() --> local edit --> update_model() --> add_job(@istari:extract_sysmlv2) --> poll --> verify artifacts
```

**Implementation reference:**

```74:132:/Users/raphael/GitHub/istari-digital/istari-digital-examples/use-cases/explore-sysml-model/update_and_extract_sysml.py
    # Step 1: Download current model
    model = client.get_model(args.model_id)
    content = model.read_text()

    # Step 2: Edit
    content = content.replace(args.find, args.replace)

    # Step 3: Re-upload as new revision
    updated = client.update_model(
        model_id=args.model_id,
        path=tmp_path,
        description=f"Updated: {args.find} → {args.replace}",
    )

    # Step 4: Run extraction
    job = client.add_job(
        model_id=args.model_id,
        function=EXTRACT_FUNCTION,
        tool_name=TOOL_NAME,
        tool_version=TOOL_VERSION,
        operating_system=OPERATING_SYSTEM,
        parameters={},
    )
    final_job = monitor_job(client, job.id, "Extraction")

    # Step 5: Verify results
    refreshed = client.get_model(args.model_id)
    for artifact in refreshed.artifacts:
        # ... inspect extracted requirements/parts ...
```

### 7.4 Multi-Configuration Design Evolution

**What it does:** The most sophisticated orchestration pattern. Creates a system with multiple configurations, then iterates through design versions. At each version: upload new SysML revision → snapshot → run extraction → snapshot → update living documents (README, notebook) → snapshot. Tags mark every milestone. This produces a complete, navigable digital thread.

**Seen in:** `explore_sysml_clean.py`, `check_design_clean.py`

**Schematic:**

```
create_system()
  --> create_configuration("Docs", tracked=[readme, notebook])
  --> create_configuration("Design", tracked=[readme, notebook, sysml])

for each design version (v1..v4):
  update_model(sysml, new_version)
    --> snapshot_and_tag("vN-uploaded")
  add_job(@istari:extract_sysmlv2) --> poll
    --> count_extraction_results()
    --> update_model(readme, updated_content)
    --> update_model(notebook, new_cells)
    --> snapshot_and_tag("vN-extracted")

share with collaborators
verify via list_tags, list_system_configurations
```

**Implementation reference:**

```820:923:/Users/raphael/GitHub/istari-digital/istari-digital-examples/use-cases/explore-sysml-model/explore_sysml_clean.py
for i, v in enumerate(VERSIONS):
    # Upload new SysML revision
    if v["name"] != "v1":
        sysml_model = client.update_model(
            model_id=SYSML_ID,
            path=SYSML_SOURCES[v["name"]],
            version_name=v["label"],
            description=v["changes"],
        )
        snapshot_and_tag(C2_ID, v["upload_tag"], f"SysML {v['label']} uploaded")

    # Run SysGit extraction
    job, elapsed = run_extraction(SYSML_ID, v["label"])
    n_reqs, n_parts, reqs_data, parts_data = count_extraction_results(SYSML_ID)

    # Update living documents at milestones
    # ... update README and notebook ...

    # Snapshot: extraction complete
    snapshot_and_tag(C2_ID, v["extract_tag"],
                     f"SysGit extraction of {v['label']}: {n_reqs} reqs, {n_parts} parts")
```

### 7.5 Declarative Test Runner (CI)

**What it does:** A JSON-driven integration test harness that runs all use cases as automated tests. Supports three modes: `upload_and_run` (upload input file, run job, verify artifacts), `run_on_existing` (run on pre-existing model), and `read_only` (verify artifacts are accessible). Generates markdown reports. This pattern enables nightly CI validation of the entire digital thread.

**Seen in:** `nightly/runner.py` + `nightly/test_config.json`

**Implementation reference:**

```351:366:/Users/raphael/GitHub/istari-digital/istari-digital-examples/nightly/runner.py
def run_single_test(client, test, use_case_base):
    """Dispatch to the correct test mode."""
    use_case_path = use_case_base / test.get("use_case_dir", "")
    mode = test.get("mode", "upload_and_run")

    if mode == "upload_and_run":
        return run_upload_and_run(client, test, use_case_path)
    elif mode == "run_on_existing":
        return run_on_existing(client, test, use_case_path)
    elif mode == "read_only":
        return run_read_only(client, test, use_case_path)
    else:
        result = TestResult(name=test["name"], status="PASS")
        result.status = "ERROR"
        result.error_message = f"Unknown test mode: {mode}"
        return result
```

---

## 8. Facade & Fluent API

Patterns that wrap the flat `Client` API into higher-level, object-oriented abstractions. These emerge when teams need reusable, composable building blocks rather than one-off scripts.

**Seen in:** `hackathons/istari_experimental/istari_utils.py` (~1400 lines)

### 8.1 Platform Facade (Entity-Oriented Client)

**What it does:** Wraps the flat `Client` into an `IstariPlatform` entry point that returns typed *View* objects (`SystemView`, `ModelView`, `JobView`, `ArtifactView`, `ConfigurationView`, `SnapshotView`). Each view encapsulates navigation logic (e.g., `system.baseline.configuration.get_models()`) so callers never need to manually chain `get_*` and `list_*` calls.

**Implementation reference:**

```1020:1049:/Users/raphael/GitHub/istari-digital/hackathons/istari_experimental/istari_utils.py
class IstariPlatform:
    """Entry point that hides the flat Client API behind entity-oriented methods."""

    def __init__(self, client: IstariClient):
        self._client = client

    @classmethod
    def from_env(cls, dotenv_path: str = ".env") -> IstariPlatform:
        """Create from ISTARI_ENVIRONMENT_URL and ISTARI_PAT env vars."""
        from dotenv import load_dotenv
        from istari_digital_client.configuration import Configuration

        load_dotenv(dotenv_path)
        config = Configuration(
            registry_url=os.getenv("ISTARI_ENVIRONMENT_URL", "..."),
            registry_auth_token=os.getenv("ISTARI_PAT"),
        )
        return cls(IstariClient(config))
```

Usage — fluent navigation from system to models:

```python
platform = IstariPlatform.from_env()
system = platform.get_system("Berserker")
for model in system.baseline.configuration.get_models():
    for job in model.get_jobs():
        print(job.function_name, job.status)
```

### 8.2 Artifact Promotion (Artifact-to-Model with Provenance)

**What it does:** Downloads an artifact's bytes via its content token, then re-uploads as a new model with `sources=[NewSource(revision_id=...)]` to preserve the provenance chain: `Original Model → Job → Artifact → New Model`. This turns derived outputs into first-class digital thread models that can be tracked in configurations.

**Implementation reference:**

```254:306:/Users/raphael/GitHub/istari-digital/hackathons/istari_experimental/istari_utils.py
    def promote(
        self,
        display_name: str | None = None,
        filename: str | None = None,
        external_identifier: str | None = None,
    ) -> ModelView:
        """Promote this artifact to a standalone model.

        The new model records the artifact revision as a *source*, preserving
        the provenance chain: Original Model -> Job -> Artifact -> Model.
        """
        from istari_digital_client.v2.models.new_source import NewSource

        rev = self._artifact.file.revision
        content = self._client.read_contents(token=rev.content_token)
        # ... write to temp file ...
        model = self._client.add_model(
            path=tmp_path,
            display_name=name,
            external_identifier=external_identifier,
            sources=[NewSource(revision_id=rev.id)],
        )
        return ModelView(_model=model, _client=self._client)
```

### 8.3 Fluent Configuration Builder (TrackedFileSet)

**What it does:** A chainable builder that collects tracked files and creates a new system configuration in one fluent expression. Supports uploading local files inline, pinning specific revisions, and promoting artifacts to models — all as chainable `.add_*()` calls terminated by `.save()`. Auto-increments configuration names (e.g., `v3` → `v4`).

**Implementation reference:**

```641:746:/Users/raphael/GitHub/istari-digital/hackathons/istari_experimental/istari_utils.py
class TrackedFileSet:
    """Mutable builder that collects tracked files and creates a configuration.

        cfg.add_file(file_id_a).add_file(file_id_b).save("v4")
        cfg.add_file(file_id_a).save()  # auto-name: "v3" -> "v4"
    """

    def add_file(self, file_id=None, *, path=None, display_name=None, ...) -> TrackedFileSet:
        """Track a file at its latest revision. Returns self for chaining."""
        if path is not None:
            model = self._client.add_model(path=path, display_name=display_name, ...)
            file_id = model.file.id
        self._entries.append(NewTrackedFile(
            specifier_type=TrackedFileSpecifierType.LATEST, file_id=file_id,
        ))
        return self

    def add_artifact_as_model(self, artifact, ...) -> TrackedFileSet:
        """Promote an artifact to a model and track it. Returns self for chaining."""
        mv = artifact.promote(display_name=display_name, ...)
        self._entries.append(NewTrackedFile(
            specifier_type=TrackedFileSpecifierType.LATEST, file_id=mv.file_id,
        ))
        return self

    def save(self, name=None) -> ConfigurationView:
        """Create the new configuration on the system."""
        config_name = name or _next_config_name(self._base_name)
        new_cfg = self._client.create_configuration(
            system_id=self._system_id,
            new_system_configuration=NewSystemConfiguration(
                name=config_name, tracked_files=self._entries,
            ),
        )
        return ConfigurationView(_config=new_cfg, _client=self._client)
```

### 8.4 Post-Submit Job Attachment (update_job)

**What it does:** Uploads a file and attaches it as a source to an *already-submitted* job via `update_job`. This enables workflows where additional inputs are provided after job creation — unlike `NewSource` at `add_job` time, this works post-hoc.

**Implementation reference:**

```465:508:/Users/raphael/GitHub/istari-digital/hackathons/istari_experimental/istari_utils.py
    def attach_file(
        self,
        file_path: str | Path,
        display_name: str,
        as_model: bool = False,
        external_id: str | None = None,
    ) -> JobView:
        """Upload a file and attach it as a source to this job."""
        from istari_digital_client.v2.models.new_source import NewSource

        if as_model:
            model = self._client.add_model(path=file_path, ...)
            uploaded_file = model.file
        else:
            uploaded_file = self._client.add_file(path=file_path, display_name=display_name)

        rev_id = uploaded_file.revision.id
        source = NewSource(revision_id=rev_id)
        updated = self._client.update_job(job_id=self.id, path=tmp_path, sources=[source])
        self._job = updated
        return self
```

### 8.5 Job-Scoped Artifact Filtering

**What it does:** When retrieving artifacts from a model after a job completes, filters to only those artifacts whose source chain links back to the specific job ID. This solves a problem other repos don't address: when a model has artifacts from *multiple* jobs, this returns only the ones produced by a given job.

**Implementation reference:**

```422:436:/Users/raphael/GitHub/istari-digital/hackathons/istari_experimental/istari_utils.py
    def get_artifacts(self) -> list[ArtifactView]:
        """Return artifacts produced by this job."""
        job = self._client.get_job(self.id)
        model = self._client.get_model(job.model_id)
        result: list[ArtifactView] = []
        for artifact in model.artifacts:
            if artifact.file and artifact.file.revisions:
                latest = artifact.file.revisions[-1]
                for src in latest.sources or []:
                    if src.resource_type == "Job" and src.resource_id == self.id:
                        result.append(ArtifactView(_artifact=artifact, _client=self._client))
                        break
        return result
```

### 8.6 Reverse Configuration Lookup

**What it does:** Given a model, discovers all (system, configuration) pairs that track it by scanning all systems and their tracked files. This is a "where is this model used?" reverse index — a relationship discovery pattern not available as a direct API call.

**Implementation reference:**

```585:601:/Users/raphael/GitHub/istari-digital/hackathons/istari_experimental/istari_utils.py
    def get_configurations(self) -> list[tuple[System, SystemConfiguration]]:
        """Find every (system, configuration) that tracks this model."""
        file_id = self._model.file.id
        results: list[tuple[System, SystemConfiguration]] = []
        for system in _paginate_manually(self._client.list_systems):
            for cfg in system.configurations or []:
                try:
                    page = self._client.list_tracked_files(configuration_id=cfg.id)
                    for tf in page.iter_items():
                        if tf.file_id == file_id:
                            results.append((system, cfg))
                            break
                except Exception:
                    continue
        return results
```

---

## Pattern Map (Summary)

| Category | Pattern | Where Used |
|----------|---------|------------|
| **Foundation** | 1.1 Client Bootstrap | All files (all repos) |
| | 1.2 Authenticated External Source | `windchill_demo.py`, `twc_demo.py` |
| **Ingestion** | 2.1 Single Model Upload | `3dx_extract.py`, `windchill_demo.py`, notebooks, examples |
| | 2.2 Metadata-Driven Batch Ingest | `csv-3dx-batch.py` |
| | 2.3 Model Update (New Revision) | `update_and_extract_sysml.py`, `explore_sysml_clean.py` |
| | 2.4 File Upload (Derived Assets) | `create_system.ipynb` |
| **Transformation** | 3.1 Extraction Job Submission | All pipeline scripts (12 tool variants) |
| | 3.2 Job Polling (Manual Loop) | `3dx_extract.py`, `csv-3dx-batch.py`, examples |
| | 3.3 Job Watch (SDK poll_job) | `windchill_demo.py`, `twc_demo.py`, `jobs.py` |
| | 3.4 Simulation Job (Parameterized) | `run_luminary_cfd.py`, `run_intact_simulation.py` |
| | 3.5 Multi-Source Job | `run_intact_simulation.py`, `nightly/runner.py` |
| | 3.6 Job Polling with Timeout | `nightly/runner.py` |
| **Retrieval** | 4.1 Artifact Download (Type-Dispatched) | All pipeline scripts |
| | 4.2 Filtered Artifact Download | `utils.py` |
| | 4.3 Revision History Export | `model_artifact_history_by_model_revision_id.py` |
| | 4.4 Artifact JSON Read | `run_luminary_cfd.py`, `run_intact_simulation.py`, `explore_sysml_clean.py` |
| | 4.5 Direct Revision Content Read | `twc_demo.py` |
| **Governance** | 5.1 Control Tag Assignment | `csv-3dx-batch.py` |
| | 5.2 Access Sharing | `csv-3dx-batch.py`, `03_share_resources.py`, `explore_sysml_clean.py` |
| | 5.3 Paginated Catalog Listing | `jobs.py` |
| | 5.4 Bulk Model Archival | `jobs.py` |
| | 5.5 Snapshot & Tagging | `explore_sysml_clean.py`, `check_design_clean.py`, extract notebooks |
| **Structure** | 6.1 System Creation | `explore_sysml_clean.py`, `run_luminary_cfd.py`, `run_intact_simulation.py` |
| | 6.2 System Configuration with Tracked Files | `explore_sysml_clean.py`, `check_design_clean.py` |
| **Orchestration** | 7.1 Linear Extract Pipeline | `3dx_extract.py`, `windchill_demo.py`, `twc_demo.py`, extract notebooks |
| | 7.2 Governed Batch Pipeline | `csv-3dx-batch.py` |
| | 7.3 Edit-Extract-Verify Loop | `update_and_extract_sysml.py`, `check_design_clean.py` |
| | 7.4 Multi-Configuration Design Evolution | `explore_sysml_clean.py`, `check_design_clean.py` |
| | 7.5 Declarative Test Runner (CI) | `nightly/runner.py` |
| **Facade & Fluent API** | 8.1 Platform Facade (Entity-Oriented Client) | `istari_utils.py` |
| | 8.2 Artifact Promotion (with Provenance) | `istari_utils.py` |
| | 8.3 Fluent Configuration Builder | `istari_utils.py` |
| | 8.4 Post-Submit Job Attachment | `istari_utils.py` |
| | 8.5 Job-Scoped Artifact Filtering | `istari_utils.py` |
| | 8.6 Reverse Configuration Lookup | `istari_utils.py` |

---

## Sources

| Repository | Files Analyzed |
|------------|---------------|
| `code-share-boeing` | `3dx_extract.py`, `csv-3dx-batch.py`, 2 notebooks |
| `code-share-blue` | `utils.py`, `jobs.py`, `windchill_demo.py`, `twc_demo.py`, `model_artifact_history_by_model_revision_id.py` |
| `istari-digital-examples` | `istari_client.py`, `03_share_resources.py`, `run_ntop_model.py`, `run_luminary_cfd.py`, `run_intact_simulation.py`, `update_and_extract_sysml.py`, `explore_sysml_clean.py`, `check_design_clean.py`, `nightly/runner.py`, 16 notebooks |
| `hackathons/istari_experimental` | `istari_utils.py` (~1400 lines, OO facade over Client API) |

---

## Observations

1. **Everything is synchronous and sequential.** No async, no threading, no parallel job submission anywhere. A natural next pattern would be *Parallel Batch Pipeline* (submit N jobs, poll all concurrently).

2. **Three polling idioms coexist.** Boeing uses manual `sleep(5)` + `get_job()` loops; Blue uses the SDK's `poll_job()`; Examples and Hackathons use manual loops with `JobStatusName` enum checks and timeout guards. The SDK `poll_job()` is cleanest for simple cases; the timeout-aware variant from the nightly runner is best for CI/automation.

3. **The Linear Extract Pipeline (7.1) is the universal backbone.** Every meaningful script is a variant of upload → job → poll → download. The differences are: which tool, whether auth sources are needed, and what post-processing happens to artifacts.

4. **The Examples repo introduces structural patterns (Systems, Configurations, Snapshots).** These move beyond flat model collections into true digital thread graphs. The `explore_sysml_clean.py` script is the most complete example of a managed digital thread, with 9 tagged snapshots across 2 configurations tracking 3 evolving files.

5. **Simulation jobs extend the digital thread from extraction to analysis.** The Luminary CFD and PyIntact patterns show how the same upload → job → poll → read pattern applies to computational analysis, not just data extraction — with added complexity in parameter wrapping and multi-source inputs.

6. **Cross-tenant sharing is only used in istari-digital-examples.** `create_access_by_email_for_other_tenants` enables collaboration across organizations, supporting both SYSTEM and FILE resource types.

7. **Model update (`update_model`) enables the edit-extract-verify inner loop.** This is a fundamentally different workflow from upload-once-and-extract — it enables iterative design within the digital thread.

8. **The hackathons facade (`istari_experimental`) represents the most mature API layering.** It wraps the flat `Client` into entity-oriented `*View` objects with fluent navigation (`system.baseline.configuration.get_models()`), chainable builders (`cfg.add_file(f1).add_file(f2).save("v4")`), and provenance-preserving operations (`artifact.promote()`). This suggests the raw SDK could benefit from a higher-level convenience layer.

9. **Provenance tracking via `NewSource` is emerging as a key pattern.** The hackathons code uses `sources=[NewSource(revision_id=...)]` not just for auth secrets or multi-input jobs, but also for artifact promotion — creating a traceable chain from `Original Model → Job → Artifact → Promoted Model`. This lineage capability is unique to the hackathons codebase.

10. **No search/query patterns exist.** All four repos use client-side filtering after listing. The SDK's `list_resources()` with its rich server-side filter parameters (like-match on `display_name`, `file_name`, etc.) remains unused in all code.
