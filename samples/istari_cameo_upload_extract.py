"""
Upload a Cameo (.mdzip) file to the Istari Digital Platform and run the @istari:extract function.

Configuration via environment variables or command-line arguments:
  ISTARI_REGISTRY_URL        - Platform URL (e.g. https://your-instance.istari.digital)
  ISTARI_REGISTRY_AUTH_TOKEN - Personal access token

Usage:
  python istari_cameo_upload_extract.py <path_to_model.mdzip> [options]

Options:
  --url URL           Override ISTARI_REGISTRY_URL
  --token TOKEN       Override ISTARI_REGISTRY_AUTH_TOKEN
  --tool-version VER  Cameo version string (default: 2024x-refresh2)
  --os OS             Agent OS (default: Windows 11)
  --poll-interval N   Seconds between job status checks (default: 10)
  --timeout N         Maximum seconds to wait for job completion (default: 3600)
  --display-name NAME Human-readable name for the uploaded model
  --description TEXT  Description for the uploaded model
"""

import argparse
import os
import sys
import time

from istari_digital_client import Client, Configuration


TERMINAL_STATUSES = {"Completed", "Failed", "Canceled"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Upload a Cameo .mdzip model and run @istari:extract on Istari Digital Platform"
    )
    parser.add_argument("model_path", help="Path to the .mdzip file to upload")
    parser.add_argument("--url", default=None, help="Istari registry URL")
    parser.add_argument("--token", default=None, help="Istari registry auth token")
    parser.add_argument(
        "--tool-version",
        default="2024x-refresh2",
        help="Cameo tool version (default: 2024x-refresh2)",
    )
    parser.add_argument(
        "--os",
        default="Windows 11",
        help="Target agent operating system (default: 'Windows 11')",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=10,
        help="Seconds between job status polls (default: 10)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="Max seconds to wait for job completion (default: 3600)",
    )
    parser.add_argument("--display-name", default=None, help="Human-readable model name")
    parser.add_argument("--description", default=None, help="Model description")
    return parser.parse_args()


def build_client(args) -> Client:
    registry_url = args.url or os.environ.get("ISTARI_REGISTRY_URL")
    registry_auth_token = args.token or os.environ.get("ISTARI_REGISTRY_AUTH_TOKEN")

    if not registry_url:
        sys.exit(
            "Error: registry URL not set. Use --url or set ISTARI_REGISTRY_URL."
        )
    if not registry_auth_token:
        sys.exit(
            "Error: auth token not set. Use --token or set ISTARI_REGISTRY_AUTH_TOKEN."
        )

    config = Configuration(
        registry_url=registry_url,
        registry_auth_token=registry_auth_token,
    )
    return Client(config)


def upload_model(client: Client, model_path: str, display_name: str | None, description: str | None):
    print(f"Uploading model: {model_path}")
    model = client.add_model(
        path=model_path,
        display_name=display_name or os.path.basename(model_path),
        description=description,
    )
    print(f"  Model uploaded  id={model.id}  name={model.display_name}")
    return model


def submit_extract_job(client: Client, model_id: str, tool_version: str, operating_system: str):
    print(f"Submitting @istari:extract job  tool_version={tool_version}  os={operating_system}")
    job = client.add_job(
        model_id=model_id,
        function="@istari:extract",
        tool_name="dassault_cameo",
        tool_version=tool_version,
        operating_system=operating_system,
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
        message = ""
        if job.status and hasattr(job.status, "message"):
            message = job.status.message or ""
        print(f"Job failed. Message: {message}")
        sys.exit(1)
    else:
        print(f"Job ended with status '{status}'.")
        sys.exit(1)


def main():
    args = parse_args()

    model_path = args.model_path
    if not os.path.isfile(model_path):
        sys.exit(f"Error: file not found: {model_path}")
    if not model_path.lower().endswith(".mdzip"):
        print(f"Warning: expected a .mdzip file, got: {model_path}")

    client = build_client(args)

    # Verify connectivity
    compat = client.check_compatibility()
    if compat:
        print(f"Connected to Istari platform  version={compat.server_version}")

    model = upload_model(client, model_path, args.display_name, args.description)
    job = submit_extract_job(client, model.id, args.tool_version, args.os)
    completed_job = wait_for_job(client, job.id, args.poll_interval, args.timeout)
    print_job_result(completed_job)


if __name__ == "__main__":
    main()
