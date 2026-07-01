"""
Run @istari:extract or @istari:twc_extract on a Cameo model via Istari Digital.

Two mutually exclusive modes:

  LOCAL MODE  — upload a local .mdzip file, then run @istari:extract.
  TWC MODE    — upload a .istari_teamwork_cloud_metadata_mdzip metadata file and
                an auth_secret.json, then run @istari:twc_extract. No local
                download of the model required.

Configuration via environment variables or CLI flags:
  ISTARI_REGISTRY_URL        - Platform URL (e.g. https://your-instance.istari.digital)
  ISTARI_REGISTRY_AUTH_TOKEN - Personal access token

TWC auth_secret.json format:
  { "username": "your_username", "password": "your_password" }

TWC metadata file format (.istari_teamwork_cloud_metadata_mdzip):
  {
    "project_id": "twcloud:/ac084d05.../6fc8e2dd...",
    "branch_name": "trunk",
    "version": "12",
    "server_name": "192.0.2.10"
  }

Usage — local file:
  python istari_cameo_upload_extract.py --local /path/to/model.mdzip

Usage — Teamwork Cloud:
  python istari_cameo_upload_extract.py --twc \\
      --twc-metadata my_project.istari_teamwork_cloud_metadata_mdzip \\
      --twc-auth auth_secret.json

Optional — scope extraction to a sub-tree:
  python istari_cameo_upload_extract.py --local /path/to/model.mdzip \\
      --root-element-id "_2021x_2_38b206bc_1744829738919_924302_3837"
"""

import argparse
import os
import sys
import time
from pathlib import Path

from istari_digital_client import Client, Configuration, FunctionAuthType, NewSource


TERMINAL_STATUSES = {"Completed", "Failed", "Canceled"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run @istari:extract or @istari:twc_extract on a Cameo model",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # --- Source mode ---
    source = parser.add_argument_group("Model source (choose one)")
    mode = source.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--local",
        metavar="FILE",
        dest="local_path",
        help="Path to a local .mdzip file to upload",
    )
    mode.add_argument(
        "--twc",
        action="store_true",
        help="Model lives in Teamwork Cloud",
    )

    # --- TWC options ---
    twc = parser.add_argument_group("Teamwork Cloud options (--twc mode)")
    twc.add_argument(
        "--model-id",
        metavar="ID",
        help="Existing Istari model ID — skips upload, uses this model directly",
    )
    twc.add_argument(
        "--twc-metadata",
        metavar="FILE",
        help="Path to .istari_teamwork_cloud_metadata_mdzip metadata file (used when --model-id is not set)",
    )
    twc.add_argument(
        "--twc-auth",
        metavar="FILE",
        default="auth_secret.json",
        help="Path to auth_secret.json with TWC username/password (default: auth_secret.json)",
    )

    # --- Istari platform auth ---
    auth = parser.add_argument_group("Istari platform auth")
    auth.add_argument("--url", default=None, help="Istari registry URL (overrides ISTARI_REGISTRY_URL)")
    auth.add_argument("--token", default=None, help="Istari auth token (overrides ISTARI_REGISTRY_AUTH_TOKEN)")

    # --- Job execution ---
    job = parser.add_argument_group("Job execution")
    job.add_argument("--tool-version", default="2022x Refresh2", help="Cameo version (default: '2022x Refresh2')")
    job.add_argument("--os", default="Windows 11", help="Target agent OS (default: 'Windows 11')")
    job.add_argument("--poll-interval", type=int, default=10, help="Seconds between status polls (default: 10)")
    job.add_argument("--timeout", type=int, default=3600, help="Max wait seconds (default: 3600)")
    job.add_argument(
        "--root-element-id",
        default=None,
        help="Optional: scope extraction to a sub-tree rooted at this element ID",
    )

    # --- Model metadata ---
    meta = parser.add_argument_group("Model metadata")
    meta.add_argument("--display-name", default=None, help="Human-readable name for the uploaded model")
    meta.add_argument("--description", default=None, help="Description for the uploaded model")

    return parser.parse_args()


def validate_args(args):
    if args.twc:
        if not args.model_id and not args.twc_metadata:
            sys.exit("Error: --twc requires either --model-id (existing model) or --twc-metadata (metadata file to upload).")
        if args.twc_metadata and not os.path.isfile(args.twc_metadata):
            sys.exit(f"Error: TWC metadata file not found: {args.twc_metadata}")
        if not os.path.isfile(args.twc_auth):
            sys.exit(
                f"Error: TWC auth file not found: {args.twc_auth}\n"
                "Create auth_secret.json with: {\"username\": \"...\", \"password\": \"...\"}"
            )
    else:
        if not os.path.isfile(args.local_path):
            sys.exit(f"Error: file not found: {args.local_path}")
        if not args.local_path.lower().endswith(".mdzip"):
            print(f"Warning: expected a .mdzip file, got: {args.local_path}")


def build_client(args) -> Client:
    registry_url = args.url or os.environ.get("ISTARI_REGISTRY_URL")
    registry_auth_token = args.token or os.environ.get("ISTARI_REGISTRY_AUTH_TOKEN")
    if not registry_url:
        sys.exit("Error: registry URL not set. Use --url or set ISTARI_REGISTRY_URL.")
    if not registry_auth_token:
        sys.exit("Error: auth token not set. Use --token or set ISTARI_REGISTRY_AUTH_TOKEN.")
    return Client(Configuration(registry_url=registry_url, registry_auth_token=registry_auth_token))


def add_twc_auth_source(client: Client, auth_path: str) -> NewSource:
    """Upload the auth_secret.json as an encrypted function auth secret."""
    print(f"  Registering TWC auth secret from: {auth_path}")
    secret = client.add_function_auth_secret(
        path=auth_path,
        function_auth_type=FunctionAuthType.BASIC,
    )
    return NewSource(
        revision_id=secret.revision.id,
        relationship_identifier="twc_auth_login",
    )


def resolve_model_local(client: Client, args) -> tuple[str, list]:
    path = args.local_path
    print(f"Uploading local model: {path}")
    model = client.add_model(
        path=path,
        display_name=args.display_name or os.path.basename(path),
        description=args.description,
    )
    print(f"  Model uploaded  id={model.id}  name={model.display_name}")
    return model.id, []


def resolve_model_twc(client: Client, args) -> tuple[str, list]:
    if args.model_id:
        print(f"Using existing Istari model  id={args.model_id}")
        model_id = args.model_id
    else:
        print(f"Uploading TWC metadata file: {args.twc_metadata}")
        model = client.add_model(
            path=args.twc_metadata,
            display_name=args.display_name or Path(args.twc_metadata).stem,
            description=args.description,
        )
        print(f"  TWC model record created  id={model.id}")
        model_id = model.id
    auth_source = add_twc_auth_source(client, args.twc_auth)
    return model_id, [auth_source]


def submit_extract_job(client: Client, model_id: str, sources: list,
                       tool_version: str, operating_system: str,
                       twc_mode: bool, root_element_id: str | None):
    function = "@istari:twc_extract" if twc_mode else "@istari:extract"
    print(f"Submitting {function}  tool_version={tool_version!r}  os={operating_system!r}")

    parameters = {}
    if root_element_id:
        parameters["root_element_id"] = root_element_id
        print(f"  Scoped to root element: {root_element_id}")

    job = client.add_job(
        model_id=model_id,
        function=function,
        tool_name="dassault_cameo",
        tool_version=tool_version,
        operating_system=operating_system,
        parameters=parameters if parameters else None,
        sources=sources if sources else None,
    )
    print(f"  Job created  id={job.id}")
    return job


def get_status_name(job) -> str:
    """Extract the status string from a Job object, handling SDK attribute variations."""
    if not job.status:
        return "Unknown"
    s = job.status
    for attr in ("status_name", "name", "value", "status"):
        val = getattr(s, attr, None)
        if val is not None:
            return str(val)
    return str(s)


def wait_for_job(client: Client, job_id: str, poll_interval: int, timeout: int):
    print(f"Waiting for job {job_id} to complete (poll every {poll_interval}s, timeout {timeout}s)...")
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        job = client.get_job(job_id=job_id)
        current_status = get_status_name(job)
        if current_status != last_status:
            print(f"  Status: {current_status}")
            last_status = current_status
        if current_status in TERMINAL_STATUSES:
            return job
        time.sleep(poll_interval)
    sys.exit(f"Error: job {job_id} did not complete within {timeout} seconds.")


def print_job_result(job):
    status = get_status_name(job)
    print(f"\nJob finished with status: {status}")
    if status == "Completed":
        print("Extraction completed successfully.")
        if hasattr(job, "outputs") and job.outputs:
            print("Outputs:")
            for output in job.outputs:
                print(f"  {output}")
    elif status == "Failed":
        message = getattr(job.status, "message", "") or "" if job.status else ""
        print(f"Job failed. Message: {message}")
        sys.exit(1)
    else:
        print(f"Job ended with status '{status}'.")
        sys.exit(1)


def main():
    args = parse_args()
    validate_args(args)

    client = build_client(args)

    compat = client.check_compatibility()
    if compat:
        print(f"Connected to Istari platform  version={compat.server_version}")

    if args.twc:
        model_id, sources = resolve_model_twc(client, args)
    else:
        model_id, sources = resolve_model_local(client, args)

    job = submit_extract_job(client, model_id, sources, args.tool_version, args.os, args.twc, args.root_element_id)
    completed_job = wait_for_job(client, job.id, args.poll_interval, args.timeout)
    print_job_result(completed_job)


if __name__ == "__main__":
    main()
