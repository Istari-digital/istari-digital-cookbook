"""
Run @istari:update_tags or @istari:twc_update_tags on a Cameo model via Istari Digital.

Two mutually exclusive modes:

  LOCAL MODE  — upload a local .mdzip file, then run @istari:update_tags.
  TWC MODE    — upload a .istari_teamwork_cloud_metadata_mdzip metadata file and
                an auth_secret.json, then run @istari:twc_update_tags. The modified
                project is committed back to Teamwork Cloud as a new version.

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
  python istari_cameo_update_tags.py --local /path/to/model.mdzip \\
      --tags-file tags.json

  python istari_cameo_update_tags.py --local /path/to/model.mdzip \\
      --element-id "_abc123" --tag "Risk=High" --tag "Status=Reviewed"

Usage — Teamwork Cloud:
  python istari_cameo_update_tags.py --twc \\
      --twc-metadata my_project.istari_teamwork_cloud_metadata_mdzip \\
      --twc-auth auth_secret.json \\
      --tags-file tags.json

Tag file format (JSON array):
  [
    {
      "element_id": "_2021x_abc123",
      "replace_existing": true,
      "tags": { "Risk": "High", "Status": "Reviewed", "Logs": null }
    },
    {
      "element_name": "Requirement Brake Deployment",
      "tags": { "Requirement Verification Pass or Fail": "PASSED" }
    }
  ]

Set a tag value to null (JSON file) or KEY= (CLI) to clear a tag.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from istari_digital_client import Client, Configuration, FunctionAuthType, NewSource


TERMINAL_STATUSES = {"Completed", "Failed", "Canceled"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run @istari:update_tags or @istari:twc_update_tags on a Cameo model",
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

    # --- Tag specification ---
    tags = parser.add_argument_group("Tag specification (choose one)")
    tag_src = tags.add_mutually_exclusive_group(required=True)
    tag_src.add_argument("--tags-file", metavar="FILE", help="JSON file containing the updates array")
    tag_src.add_argument("--element-id", metavar="ID", help="Single element ID to tag (use with --tag)")
    tag_src.add_argument("--element-name", metavar="NAME", help="Single element name to tag (use with --tag)")
    tags.add_argument(
        "--tag",
        metavar="KEY=VALUE",
        action="append",
        dest="tags",
        help="Tag to set as KEY=VALUE. Repeat for multiple. Use KEY= to clear.",
    )
    tags.add_argument(
        "--replace-existing",
        action="store_true",
        default=False,
        help="Replace all existing tags on the element (default: merge/update only)",
    )

    # --- Job execution ---
    job = parser.add_argument_group("Job execution")
    job.add_argument("--tool-version", default="2022x Refresh2", help="Cameo version (default: '2022x Refresh2')")
    job.add_argument("--os", default="Windows 11", help="Target agent OS (default: 'Windows 11')")
    job.add_argument("--poll-interval", type=int, default=10, help="Seconds between status polls (default: 10)")
    job.add_argument("--timeout", type=int, default=3600, help="Max wait seconds (default: 3600)")

    # --- Local-upload metadata ---
    meta = parser.add_argument_group("Model metadata (--local mode)")
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


def build_updates(args) -> list[dict]:
    if args.tags_file:
        with open(args.tags_file) as f:
            updates = json.load(f)
        if not isinstance(updates, list):
            sys.exit("Error: --tags-file must contain a JSON array at the top level.")
        return updates

    if not args.tags:
        sys.exit("Error: --element-id / --element-name requires at least one --tag KEY=VALUE.")

    tags: dict = {}
    for kv in args.tags:
        if "=" not in kv:
            sys.exit(f"Error: --tag must be KEY=VALUE, got: {kv!r}")
        key, _, value = kv.partition("=")
        tags[key] = value if value else None
    update: dict = {"tags": tags, "replace_existing": args.replace_existing}
    if args.element_id:
        update["element_id"] = args.element_id
    else:
        update["element_name"] = args.element_name
    return [update]


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


def submit_job(client: Client, model_id: str, updates: list[dict], sources: list,
               tool_version: str, operating_system: str, twc_mode: bool):
    function = "@istari:twc_update_tags" if twc_mode else "@istari:update_tags"
    print(f"Submitting {function}  tool_version={tool_version!r}  os={operating_system!r}")
    print(f"  Targeting {len(updates)} element update(s)")
    job = client.add_job(
        model_id=model_id,
        function=function,
        tool_name="dassault_cameo",
        tool_version=tool_version,
        operating_system=operating_system,
        parameters={"updates": updates},
        sources=sources if sources else None,
    )
    print(f"  Job created  id={job.id}")
    return job


def wait_for_job(client: Client, job_id: str, poll_interval: int, timeout: int):
    print(f"Waiting for job {job_id} to complete (poll every {poll_interval}s, timeout {timeout}s)...")
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        job = client.get_job(job_id=job_id)
        current_status = job.status.status_name if job.status else "Unknown"
        if current_status != last_status:
            print(f"  Status: {current_status}")
            last_status = current_status
        if current_status in TERMINAL_STATUSES:
            return job
        time.sleep(poll_interval)
    sys.exit(f"Error: job {job_id} did not complete within {timeout} seconds.")


def print_job_result(job, twc_mode: bool):
    status = job.status.status_name if job.status else "Unknown"
    print(f"\nJob finished with status: {status}")
    if status == "Completed":
        if twc_mode:
            print("Tags committed back to Teamwork Cloud as a new version.")
            print("Refresh the model with client.get_model(model_id) to get the latest revision.")
        else:
            print("Tags updated successfully. The modified model is available as a new artifact.")
        if hasattr(job, "outputs") and job.outputs:
            print("Outputs:")
            for output in job.outputs:
                print(f"  {output}")
    elif status == "Failed":
        message = (job.status.message or "") if job.status and hasattr(job.status, "message") else ""
        print(f"Job failed. Message: {message}")
        sys.exit(1)
    else:
        print(f"Job ended with status '{status}'.")
        sys.exit(1)


def main():
    args = parse_args()
    validate_args(args)

    updates = build_updates(args)
    client = build_client(args)

    compat = client.check_compatibility()
    if compat:
        print(f"Connected to Istari platform  version={compat.server_version}")

    if args.twc:
        model_id, sources = resolve_model_twc(client, args)
    else:
        model_id, sources = resolve_model_local(client, args)

    job = submit_job(client, model_id, updates, sources, args.tool_version, args.os, args.twc)
    completed_job = wait_for_job(client, job.id, args.poll_interval, args.timeout)
    print_job_result(completed_job, args.twc)


if __name__ == "__main__":
    main()
