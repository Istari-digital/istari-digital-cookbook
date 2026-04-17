# Digital Thread Pattern Language — v1

## Foundations

Every interaction begins with a configured client. Best practice is a shared factory function so credentials stay in one place.

```python
from istari_digital_client import Client, Configuration

def get_client() -> Client:
    return Client(Configuration(
        registry_url=os.getenv("ISTARI_DIGITAL_REGISTRY_URL"),
        registry_auth_token=os.getenv("ISTARI_DIGITAL_REGISTRY_AUTH_TOKEN"),
    ))
```

---

## 1. Extract & Retrieve

The bread-and-butter pattern. Upload a source file, run an extraction job, wait for it to complete, then find and download a specific artifact by name. This is the shortest path from "I have a CAD file" to "I have structured data."

```
add_model() ──→ add_job(extract) ──→ poll until done ──→ find artifact by name ──→ read_bytes()
```

```python
# 1. Upload the source file
model = client.add_model(
    path="assembly.CATProduct",
    display_name="Wing Assembly",
)

# 2. Submit the extraction job
job = client.add_job(
    model_id=model.id,
    function="@istari:extract",
    tool_name="dassault_catia_v5",
    operating_system="Windows 10",
)

# 3. Wait for completion
status = job.poll_job()
if status.name in ["FAILED", "CANCELED"]:
    raise RuntimeError(f"Extraction failed: {status.name}")

# 4. Refresh the model and find a specific artifact by name
model = client.get_model(job.model_id)
bom = next(a for a in model.artifacts if a.name == "bill_of_materials.json")
bom_data = bom.read_json()
```

### Extraction functions

| Function | Tool | What it extracts |
|----------|------|-----------------|
| `@istari:extract` | `dassault_3dexperience`, `microsoft_office_excel`, `microsoft_office_word`, `pdf`, `ptc_creo_parametric`, `dassault_catia_v5`, `dassault_cameo`, `microsoft_office_powerpoint` | Views, parameters, BOMs, text |
| `@istari:extract_sysmlv2` | `sysgit` | Requirements JSON, parts JSON, diagrams |
| `@istari:extract_input` | `nastran` | FEA mesh/input data |

### Artifact retrieval options

```python
artifact.read_bytes()   # raw bytes — images, STEP files, etc.
artifact.read_json()    # parsed dict — structured data
artifact.read_text()    # decoded string — SysML, CSV, etc.
```

---

## 2. PLM-Connected Extraction

Extends Extract & Retrieve for models stored in an external PLM system (Windchill, Teamwork Cloud). The key addition is registering credentials as a `FunctionAuthSecret` and passing them to the job as a source.

```
register credentials ──→ upload metadata ──→ add_job(extract, auth_source) ──→ poll ──→ retrieve artifacts
```

```python
from istari_digital_client.models.function_auth_type import FunctionAuthType
from istari_digital_client.models.new_source import NewSource

# 1. Register PLM credentials (once per session)
secret = client.add_function_auth_secret(
    path="windchill_secret.json",
    function_auth_type=FunctionAuthType.BASIC,
)
auth_source = NewSource(
    revision_id=secret.revision.id,
    relationship_identifier="windchill_auth",
)

# 2. Upload the PLM metadata pointer
model = client.add_model(
    path="metadata.istari_windchill_metadata",
    display_name="Windchill CAD Model",
)

# 3. Extract with credentials attached
job = client.add_job(
    model_id=model.id,
    function="@istari:windchill_extract",
    tool_name="ptc_creo_parametric",
    operating_system="Windows 10",
    sources=[auth_source],
)

# 4. Wait and retrieve
status = job.poll_job()
model = client.get_model(job.model_id)
for artifact in model.artifacts:
    Path(artifact.name).write_bytes(artifact.read_bytes())
```

| PLM | Function | Auth relationship |
|-----|----------|-------------------|
| Windchill | `@istari:windchill_extract` | `windchill_auth` |
| Teamwork Cloud | `@istari:twc_extract` | `twc_auth` |

---

## 3. Multi-Source Simulation

Sets up a computational analysis that consumes multiple inputs (geometry, boundary conditions, load faces), runs the simulation, and retrieves structured results. The `NewSource` mechanism attaches additional model revisions with semantic labels the solver knows how to interpret.

```
upload geometry ──→ upload boundary files ──→ add_job(simulate, sources=[...]) ──→ poll ──→ read results
```

```python
from istari_digital_client.v2.models.new_source import NewSource

# 1. Upload the primary model and supporting geometry
beam = client.add_model(path="cantilever.step", display_name="Beam Geometry")
load_face = client.add_model(path="end-face.stl", display_name="Load Application Face")
restraint_face = client.add_model(path="fixed-face.stl", display_name="Fixed Restraint Face")

# 2. Wire up sources with semantic relationships
sources = [
    NewSource(
        revision_id=load_face.file.revisions[0].id,
        relationship_identifier="load_geometry_file",
    ),
    NewSource(
        revision_id=restraint_face.file.revisions[0].id,
        relationship_identifier="restraint_geometry_file",
    ),
]

# 3. Submit the simulation with parameters
sim_config = {"material": "steel", "load_n": 500, "mesh_size": 0.01}
job = client.add_job(
    model_id=beam.id,
    function="@istari:run_pyintact_simulation",
    parameters={"simulation_config": json.dumps(sim_config)},
    operating_system=OS("macOS 15"),
    assigned_agent_id=agent_id,
    sources=sources,
)

# 4. Wait and retrieve results
status = job.poll_job()
if status.name in ["FAILED", "CANCELED"]:
    raise RuntimeError(f"Simulation failed: {status.name}")

model = client.get_model(job.model_id)
results = next(a for a in model.artifacts if a.name == "results.json")
print(results.read_json())
```

### Simulation parameter conventions

| Solver | Function | Parameter wrapping |
|--------|----------|--------------------|
| Luminary CFD | `@luminary:run_cfd` | `{"cfd_config": {"value": config_dict}}` |
| PyIntact FEA | `@istari:run_pyintact_simulation` | `{"simulation_config": json.dumps(config)}` |

---

## 4. Edit, Re-extract, and Verify

The inner loop for iterative design. Download a model's content, make a change locally, re-upload as a new revision, extract again, and verify the change is reflected in the output artifacts. This keeps every design decision inside the digital thread.

```
get_model() ──→ read_text() ──→ edit ──→ update_model() ──→ add_job(extract) ──→ poll ──→ verify artifacts
```

```python
# 1. Pull the current content
model = client.get_model(model_id)
content = model.read_text()

# 2. Make a targeted edit
content = content.replace("1000", "1500")  # extend range requirement

# 3. Upload as a new revision
tmp_path = Path("requirements_v2.sysml")
tmp_path.write_text(content)
client.update_model(
    model_id=model_id,
    path=str(tmp_path),
    version_name="v2",
    description="Range: 1000 → 1500 nm",
)

# 4. Re-extract to validate
job = client.add_job(
    model_id=model_id,
    function="@istari:extract_sysmlv2",
    tool_name="sysgit",
    operating_system="Linux",
)
status = job.poll_job()

# 5. Verify the change landed
model = client.get_model(model_id)
reqs = next(a for a in model.artifacts if a.name == "requirements.json")
for req in reqs.read_json():
    if "range" in req.get("name", "").lower():
        assert req["value"] == "1500", f"Expected 1500, got {req['value']}"
```

---

## 5. Design Evolution with Configuration Management

The most sophisticated orchestration. Creates a system that groups related models, organizes them into named configurations, then walks through design versions — snapshotting and tagging at every milestone. This gives the full history of how a design evolved, navigable by human-readable tags.

```
create_system()
  ├─ create_configuration("Docs",   tracked=[readme, notebook])
  └─ create_configuration("Design", tracked=[readme, notebook, sysml])

for each version:
  update_model(sysml)    ──→ snapshot + tag("vN-uploaded")
  add_job(extract) + poll ──→ snapshot + tag("vN-extracted")
  update_model(readme)    ──→ snapshot + tag("vN-documented")
```

```python
from istari_digital_client.v2.models import (
    NewSystem, NewSystemConfiguration, NewTrackedFile,
    TrackedFileSpecifierType, NewSnapshot, NewSnapshotTag,
)

# 1. Create the system
system = client.create_system(
    new_system=NewSystem(
        name="Wing Aerodynamics Study",
        description="Cross-tool validation: nTop design vs Luminary CFD",
    )
)

# 2. Define configurations with tracked files
def track(file_id):
    return NewTrackedFile(
        specifier_type=TrackedFileSpecifierType.LATEST,
        file_id=file_id,
    )

design_config = client.create_configuration(
    system_id=system.id,
    new_system_configuration=NewSystemConfiguration(
        name="Design",
        tracked_files=[track(sysml_file_id), track(readme_file_id)],
    ),
)

# 3. Iterate through design versions
for version, sysml_path in [("v1", "baseline.sysml"), ("v2", "extended_range.sysml")]:
    client.update_model(model_id=sysml_model_id, path=sysml_path)

    snap = client.create_snapshot(
        design_config.id,
        NewSnapshot(description=f"{version} uploaded"),
    )
    client.create_tag(snap.actual_instance.id, NewSnapshotTag(tag=f"{version}-uploaded"))

    job = client.add_job(model_id=sysml_model_id, function="@istari:extract_sysmlv2",
                         tool_name="sysgit", operating_system="Linux")
    job.poll_job()

    snap = client.create_snapshot(
        design_config.id,
        NewSnapshot(description=f"{version} extracted"),
    )
    client.create_tag(snap.actual_instance.id, NewSnapshotTag(tag=f"{version}-extracted"))
```

---

## 6. Governed Batch Onboarding

Processes a batch of source files with full governance: generate metadata from a CSV, upload each file as a model, classify it with control taggings, grant access to the right users, then extract — all in a single automated sweep.

```
CSV ──→ for each row:
          add_model()
          ├─ add_model_control_taggings()
          ├─ create_access() × N users
          └─ add_job(extract) ──→ poll ──→ verify
```

```python
import csv
from istari_digital_client import AccessSubjectType, AccessRelation

with open("manifest.csv") as f:
    for row in csv.DictReader(f):
        # 1. Upload
        model = client.add_model(
            path=row["file_path"],
            display_name=row["display_name"],
            external_identifier=row["part_number"],
        )

        # 2. Classify
        client.add_model_control_taggings(
            model_id=model.id,
            taggings={"classification": row["classification"]},
        )

        # 3. Grant access
        for email in row["viewers"].split(";"):
            model.create_access(
                subject_type=AccessSubjectType.USER,
                email=email.strip(),
                relation=AccessRelation.VIEWER,
            )

        # 4. Extract and wait
        job = client.add_job(
            model_id=model.id,
            function="@istari:extract",
            tool_name=row["tool_name"],
            operating_system="Windows 10",
        )
        status = job.poll_job()
        print(f"  {row['display_name']}: {status.name}")
```

---

## 7. Artifact Promotion & Lineage

Takes an artifact produced by a job and promotes it to a first-class model in the digital thread, preserving the provenance link back to its origin. The promoted model can then be tracked in configurations, versioned, and fed into downstream jobs — creating a chain of derived data with full traceability.

```
Source Model ──→ Job ──→ Artifact ──→ promote() ──→ New Model (with provenance) ──→ track in configuration
```

```python
from istari_digital_client.v2.models.new_source import NewSource

# 1. Run an extraction (assume job completed, model refreshed)
model = client.get_model(job.model_id)
bom_artifact = next(a for a in model.artifacts if a.name == "bill_of_materials.json")

# 2. Promote to a standalone model
bom_model = bom_artifact.promote(
    display_name="Extracted BOM — Wing Assembly",
    sources=[NewSource(revision_id=bom_artifact.revision_id)],
)

# 3. Track the promoted model in a system configuration
config = client.create_configuration(
    system_id=system.id,
    new_system_configuration=NewSystemConfiguration(
        name="Derived Data",
        tracked_files=[track(bom_model.file.id)],
    ),
)
```

---

## 8. Revision History Audit

Walks the full revision history of a model and downloads every version alongside its linked artifacts, creating a timestamped directory tree. Used for compliance, auditing, or reconstructing how a design evolved outside the platform.

```
get_model() ──→ for each revision: download source + download all linked artifacts
```

```python
model = client.get_model(model_id)
output = Path("audit") / model.display_name

for revision in model.revisions:
    rev_dir = output / f"{revision.created:%Y%m%d_%H%M%S}_{revision.id[:8]}"
    rev_dir.mkdir(parents=True, exist_ok=True)

    # Download the source file at this revision
    rev_data = client.get_revision(revision.id)
    (rev_dir / revision.name).write_bytes(rev_data.read_bytes())

    # Download all artifacts linked to this revision
    for product in revision.products:
        if product.resource_type == "Artifact":
            art_rev = client.get_revision(product.revision_id)
            art_dir = rev_dir / "artifacts"
            art_dir.mkdir(exist_ok=True)
            (art_dir / art_rev.name).write_bytes(art_rev.read_bytes())
```

---

## 9. Cross-Tenant Sharing

Grants access to models and systems across organizational boundaries. Two mechanisms exist depending on whether you're sharing within a tenant (by user ID) or across tenants (by email).

```python
from istari_digital_client import AccessSubjectType, AccessRelation, AccessResourceType

# Cross-tenant: share a system with an external collaborator
client.create_access_by_email_for_other_tenants(
    subject_type=AccessSubjectType.USER,
    email="collaborator@partner.com",
    resource_type=AccessResourceType.SYSTEM,
    resource_id=system.id,
    access_relationship=AccessRelation.EDITOR,
)

# Within-tenant: share a model with a team member
model.create_access(
    subject_type=AccessSubjectType.USER,
    subject_id=user_id,
    relation=AccessRelation.VIEWER,
)
```

---

## Job Monitoring Reference

Three idioms for waiting on job completion, from simplest to production-grade.

**SDK built-in** — use this by default:

```python
status = job.poll_job()
if status.name in ["FAILED", "CANCELED"]:
    raise RuntimeError(f"Job failed: {status.name}")
```

**With timeout** — prevents CI hangs in automation:

```python
def poll_job(client, job_id, timeout_seconds=300, pending_timeout=120):
    start = time.time()
    ever_started = False
    while True:
        if time.time() - start > timeout_seconds:
            return None
        if not ever_started and time.time() - start > pending_timeout:
            return None
        time.sleep(5)
        job = client.get_job(job_id)
        if job.status.name in [JobStatusName.COMPLETED, JobStatusName.FAILED]:
            return job
        if job.status.name.value not in ("Pending", "Queued"):
            ever_started = True
```

---

## Quick Reference

| # | Pattern | What it does |
|---|---------|-------------|
| 1 | **Extract & Retrieve** | Upload → extract → poll → find artifact by name → download |
| 2 | **PLM-Connected Extraction** | Register PLM credentials → extract from Windchill/TWC → retrieve |
| 3 | **Multi-Source Simulation** | Wire up geometry + boundary files → run solver → get results |
| 4 | **Edit, Re-extract, Verify** | Download → edit → new revision → re-extract → assert change |
| 5 | **Design Evolution** | System + configurations + versioned snapshots with tags |
| 6 | **Governed Batch Onboarding** | CSV-driven: upload + classify + share + extract per model |
| 7 | **Artifact Promotion & Lineage** | Promote job output to a tracked, provenance-linked model |
| 8 | **Revision History Audit** | Walk all revisions + artifacts into a timestamped directory tree |
| 9 | **Cross-Tenant Sharing** | Grant access by email (cross-tenant) or user ID (within tenant) |
