"""Chained-Jobs Demo Runner (no fluent helpers)
================================================

Reproduces ``samples/chaining_jobs_no_helper.ipynb`` as a single script so it
can be run non-interactively and its output piped into ``build_lineage.py``.

What it does
------------
1.  Uploads ``samples/Group3-UAS-Requirements.xlsx`` as a Model (v2 ``add_model``).
2.  Submits ``@istari:extract`` via ``open_spreadsheet`` -- this produces a
    ``workbook.xlsx`` artifact and a ``named_cells.json`` artifact.
3.  Promotes ``workbook.xlsx`` to a new Model (mimicking the helper's
    auto-promotion path -- ``add_model`` with a ``NewSource(promoted_from)``).
4.  Submits ``@istari:extract`` again on the promoted model. The result is the
    final ``named_cells.json`` with a full 6-deep provenance chain.

The whole flow uses the flat ``istari_digital_client.Client`` -- no
``istari_labs_helpers`` -- so it doubles as a worked example of the v2 SDK.

Quick start
-----------
1.  ``export ISTARI_REGISTRY_URL=https://fileservice-v2.demo.istari.app``
    ``export ISTARI_PERSONAL_ACCESS_TOKEN=<your token>``
2.  Make sure an agent that hosts ``open_spreadsheet`` is online -- the script
    polls until each job completes.
3.  ``uv run --project ~/git/istari-digital-cookbook/istari-labs-helpers \\``
    ``python run_chain.py``

Final output ends with a ``CHAIN_RESULT_JSON`` block containing every UUID
(model, jobs, intermediate workbook, promoted model, final named_cells).
Feed ``final_named_cells_resource_id`` (or ``..._revision_id``) into
``build_lineage.py``::

    python build_lineage.py --resource-id <final_named_cells_resource_id> --upload

Prerequisites: ``Group3-UAS-Requirements.xlsx`` next to the cookbook samples
folder (it ships with the repo).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from istari_digital_client import Client, Configuration, JobStatusName
from istari_digital_client.v2.models.new_source import NewSource

XLSX_PATH = Path(__file__).resolve().parent.parent / "Group3-UAS-Requirements.xlsx"
EXTERNAL_ID = "sdk-tutorial-uas-requirements"
DISPLAY_NAME = "Group3-UAS-Requirements (tutorial sdk)"
FUNCTION = "@istari:extract"
TOOL = "open_spreadsheet"


def wait_job(client: Client, job_id: str, *, timeout: int = 900, poll_interval: int = 5):
    start = time.time()
    while True:
        job = client.get_job(job_id)
        st = job.status.name
        msg = getattr(job.status, "message", None)
        elapsed = int(time.time() - start)
        print(f"  [{elapsed:>4}s] {st.value if hasattr(st, 'value') else st}  id={job.id}  msg={msg!r}", flush=True)
        if st == JobStatusName.PENDING and msg and "None agent" in msg:
            raise RuntimeError(f"No agent available: {msg}")
        if st in (JobStatusName.COMPLETED, JobStatusName.FAILED):
            return job
        if time.time() - start > timeout:
            return job
        time.sleep(poll_interval)


def require_completed(job):
    if job.status.name != JobStatusName.COMPLETED:
        raise RuntimeError(f"Job {job.id} ended with {job.status.name}")
    return job


def find_product_by_filename(job, filename: str):
    """Find a product on the job's latest revision matching ``filename``.

    Caller passes a completed job (its file.revision is already populated);
    no re-fetch needed.
    """
    if not job.file or not job.file.revisions:
        return None, None
    for p in job.file.revision.products or []:
        if p.resource_type and p.resource_id and p.revision_id:
            rrev = p.revision
            if rrev is not None and rrev.name == filename:
                return p, rrev
    return None, None


def promote_revision_to_model(client: Client, revision):
    content = client.read_contents(token=revision.content_token)
    upload_name = revision.name or f"promote-{revision.id}.xlsx"
    with tempfile.TemporaryDirectory(prefix="istari_promote_") as tmp_dir:
        tmp_path = Path(tmp_dir) / upload_name
        tmp_path.write_bytes(content)
        return client.add_model(
            path=str(tmp_path),
            display_name=Path(upload_name).stem,
            sources=[NewSource(revision_id=revision.id, relationship_identifier="promoted_from")],
        )


def main() -> int:
    registry_url = os.environ.get("ISTARI_REGISTRY_URL")
    token = os.environ.get("ISTARI_PERSONAL_ACCESS_TOKEN") or os.environ.get("ISTARI_REGISTRY_AUTH_TOKEN")
    if not registry_url or not token:
        print("ERROR: set ISTARI_REGISTRY_URL and ISTARI_PERSONAL_ACCESS_TOKEN", file=sys.stderr)
        return 2
    if not XLSX_PATH.exists():
        print(f"ERROR: {XLSX_PATH} not found", file=sys.stderr)
        return 2

    client = Client(Configuration(registry_url=registry_url, registry_auth_token=token))
    health = client.readiness_check()
    assert health.healthy, f"Platform unhealthy: {health}"
    print(f"Connected: {registry_url}")

    print("\n[1] Uploading model...")
    model = client.add_model(
        XLSX_PATH,
        external_identifier=EXTERNAL_ID,
        display_name=DISPLAY_NAME,
    )
    print(f"    model.id={model.id}  file.id={model.file.id if model.file else None}")

    print(f"\n[2] Submitting job 1 ({FUNCTION} via {TOOL})...")
    job1_raw = client.add_job(model.id, FUNCTION, tool_name=TOOL)
    print(f"    job1.id={job1_raw.id}")
    job1 = require_completed(wait_job(client, job1_raw.id))
    print(f"    job1 COMPLETED")

    wb_p, wb_rev = find_product_by_filename(job1, "workbook.xlsx")
    if wb_rev is None:
        print("ERROR: job1 did not produce workbook.xlsx", file=sys.stderr)
        return 3
    print(f"    workbook.xlsx revision={wb_rev.id}  file={wb_rev.file_id}")

    print("\n[3] Promoting workbook.xlsx to a Model for job 2...")
    promoted = promote_revision_to_model(client, wb_rev)
    print(f"    promoted.id={promoted.id}  file.id={promoted.file.id if promoted.file else None}")

    print(f"\n[4] Submitting job 2 ({FUNCTION} via {TOOL})...")
    job2_raw = client.add_job(promoted.id, FUNCTION, tool_name=TOOL)
    print(f"    job2.id={job2_raw.id}")
    job2 = require_completed(wait_job(client, job2_raw.id))
    print(f"    job2 COMPLETED")

    final_p, final_rev = find_product_by_filename(job2, "named_cells.json")
    if final_rev is None:
        print("ERROR: job2 did not produce named_cells.json", file=sys.stderr)
        return 3

    summary = {
        "model_id": model.id,
        "job1_id": job1.id,
        "workbook_xlsx_resource_id": wb_p.resource_id,
        "workbook_xlsx_revision_id": wb_rev.id,
        "promoted_model_id": promoted.id,
        "job2_id": job2.id,
        "final_named_cells_resource_id": final_p.resource_id,
        "final_named_cells_revision_id": final_rev.id,
        "final_named_cells_file_id": final_rev.file_id,
    }
    print("\n=== CHAIN_RESULT_JSON ===")
    print(json.dumps(summary, indent=2))
    print("=== END_CHAIN_RESULT_JSON ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
