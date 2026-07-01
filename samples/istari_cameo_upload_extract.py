"""
Run @istari:extract on a Cameo model via the Istari Digital Platform.

Two mutually exclusive modes:

  LOCAL MODE  — upload a local .mdzip file, then run the extraction job.
  TWC MODE    — the agent fetches the model directly from Teamwork Cloud;
                no local file is needed. Requires an existing Istari model
                record to attach the job to (--model-id).

Configuration via environment variables or CLI flags:
  ISTARI_REGISTRY_URL        - Platform URL (e.g. https://your-instance.istari.digital)
  ISTARI_REGISTRY_AUTH_TOKEN - Personal access token

  TWC_USERNAME               - Teamwork Cloud username        (TWC mode)
  TWC_PASSWORD               - Teamwork Cloud password        (TWC mode)
  TWC_PROJECT_URL            - URL to the TWC project/branch  (TWC mode)

Usage — local file:
  python istari_cameo_upload_extract.py --local /path/to/model.mdzip

Usage — Teamwork Cloud:
  python istari_cameo_upload_extract.py --twc \\
      --model-id <istari-model-id> \\
      --twc-project-url "https://twc24.uat.mbx.us.lmco.com/your/project" \\
      --twc-username myuser --twc-password mypass
"""

import argparse
import os
import sys
import time

from istari_digital_client import Client, Configuration


TERMINAL_STATUSES = {"Completed", "Failed", "Canceled"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run @istari:extract on a Cameo model (local upload or Teamwork Cloud)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # --- Source mode (mutually exclusive) ---
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
        help="Model lives in Teamwork Cloud — no local file upload",
    )

    # --- TWC options ---
    twc = parser.add_argument_group("Teamwork Cloud options (--twc mode)")
    twc.add_argument(
        "--model-id",
        metavar="ID",
        help="Existing Istari model ID to attach the job to (required for --twc)",
    )
    twc.add_argument(
        "--twc-project-url",
        metavar="URL",
        default=None,
        help="URL of the TWC project/branch (overrides TWC_PROJECT_URL)",
    )
    twc.add_argument("--twc-username", metavar="USER", default=None, help="TWC username (overrides TWC_USERNAME)")
    twc.add_argument("--twc-password", metavar="PASS", default=None, help="TWC password (overrides TWC_PASSWORD)")

    # --- Istari platform auth ---
    auth = parser.add_argument_group("Istari platform auth")
    auth.add_argument("--url", default=None, help="Istari registry URL (overrides ISTARI_REGISTRY_URL)")
    auth.add_argument("--token", default=None, help="Istari auth token (overrides ISTARI_REGISTRY_AUTH_TOKEN)")

    # --- Job execution ---
    job = parser.add_argument_group("Job execution")
    job.add_argument("--tool-version", default="2024x-refresh2", help="Cameo version (default: 2024x-refresh2)")
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
        if not args.model_id:
            sys.exit("Error: --twc requires --model-id (the Istari model ID to attach the job to).")
        twc_url = args.twc_project_url or os.environ.get("TWC_PROJECT_URL")
        if not twc_url:
            sys.exit("Error: --twc requires --twc-project-url or TWC_PROJECT_URL.")
        twc_user = args.twc_username or os.environ.get("TWC_USERNAME")
        twc_pass = args.twc_password or os.environ.get("TWC_PASSWORD")
        if not twc_user or not twc_pass:
            sys.exit("Error: --twc requires TWC credentials (--twc-username/--twc-password or TWC_USERNAME/TWC_PASSWORD).")
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


def resolve_model_local(client: Client, args) -> str:
    """Upload a local .mdzip and return the new model ID."""
    path = args.local_path
    print(f"Uploading model: {path}")
    model = client.add_model(
        path=path,
        display_name=args.display_name or os.path.basename(path),
        description=args.description,
    )
    print(f"  Model uploaded  id={model.id}  name={model.display_name}")
    return model.id


def resolve_model_twc(args) -> str:
    """Return the existing Istari model ID for a TWC-hosted model."""
    print(f"Using existing Istari model  id={args.model_id}")
    return args.model_id


def build_parameters(args) -> dict:
    """Build job parameters. Empty for local mode; TWC link + credentials for TWC mode."""
    if not args.twc:
        return {}

    twc_url = args.twc_project_url or os.environ.get("TWC_PROJECT_URL")
    twc_user = args.twc_username or os.environ.get("TWC_USERNAME")
    twc_pass = args.twc_password or os.environ.get("TWC_PASSWORD")

    return {
        "twc_link": twc_url,
        "auth_info": {"type": "basic", "username": twc_user, "password": twc_pass},
    }


def submit_extract_job(client: Client, model_id: str, parameters: dict, tool_version: str, operating_system: str, twc_mode: bool = False):
    function = "@istari:extract"
    tool_name = "teamwork_cloud" if twc_mode else "dassault_cameo"
    print(f"Submitting {function} job  tool_name={tool_name}  tool_version={tool_version}  os={operating_system}")
    job = client.add_job(
        model_id=model_id,
        function=function,
        tool_name=tool_name,
        tool_version=tool_version,
        operating_system=operating_system,
        parameters=parameters or None,
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


def print_job_result(job):
    status = job.status.status_name if job.status else "Unknown"
    print(f"\nJob finished with status: {status}")

    if status == "Completed":
        print("Extraction completed successfully.")
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

    client = build_client(args)

    compat = client.check_compatibility()
    if compat:
        print(f"Connected to Istari platform  version={compat.server_version}")

    if args.twc:
        model_id = resolve_model_twc(args)
    else:
        model_id = resolve_model_local(client, args)

    parameters = build_parameters(args)
    job = submit_extract_job(client, model_id, parameters, args.tool_version, args.os, twc_mode=args.twc)
    completed_job = wait_for_job(client, job.id, args.poll_interval, args.timeout)
    print_job_result(completed_job)


if __name__ == "__main__":
    main()
