"""
Upload a Cameo (.mdzip) file to the Istari Digital Platform and run @istari:update_tags
to write tags onto model elements in a Teamwork Cloud project.

Elements can be targeted by element_id, element_name, or both.
Tags are arbitrary key/value pairs; set a value to null to clear a tag.

Configuration via environment variables or CLI flags:
  ISTARI_REGISTRY_URL        - Platform URL (e.g. https://your-instance.istari.digital)
  ISTARI_REGISTRY_AUTH_TOKEN - Personal access token

  TWC_USERNAME               - Teamwork Cloud username
  TWC_PASSWORD               - Teamwork Cloud password
  TWC_URL                    - Teamwork Cloud server URL (e.g. https://twc.example.com)

Usage:
  python istari_cameo_update_tags.py <path_to_model.mdzip> --tags-file tags.json [options]
  python istari_cameo_update_tags.py <path_to_model.mdzip> --element-id ID --tag KEY=VALUE [options]
  python istari_cameo_update_tags.py <path_to_model.mdzip> --element-name NAME --tag KEY=VALUE [options]

Tag file format (JSON array):
  [
    {
      "element_id": "_2021x_abc123",
      "replace_existing": true,
      "tags": {
        "Risk": "High",
        "Status": "Reviewed",
        "Logs": null
      }
    },
    {
      "element_name": "Requirement Brake Deployment",
      "tags": {
        "Requirement Verification Pass or Fail": "PASSED",
        "Version History": "v1.3.4"
      }
    }
  ]
"""

import argparse
import json
import os
import sys
import time

from istari_digital_client import Client, Configuration


TERMINAL_STATUSES = {"Completed", "Failed", "Canceled"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Upload a Cameo .mdzip and run @istari:update_tags on the Istari Digital Platform"
    )
    parser.add_argument("model_path", help="Path to the .mdzip file to upload")

    # Istari platform auth
    parser.add_argument("--url", default=None, help="Istari registry URL (overrides ISTARI_REGISTRY_URL)")
    parser.add_argument("--token", default=None, help="Istari auth token (overrides ISTARI_REGISTRY_AUTH_TOKEN)")

    # TWC credentials
    parser.add_argument("--twc-url", default=None, help="Teamwork Cloud server URL (overrides TWC_URL)")
    parser.add_argument("--twc-username", default=None, help="TWC username (overrides TWC_USERNAME)")
    parser.add_argument("--twc-password", default=None, help="TWC password (overrides TWC_PASSWORD)")

    # Tag specification — file or inline
    tag_group = parser.add_mutually_exclusive_group(required=True)
    tag_group.add_argument(
        "--tags-file",
        metavar="FILE",
        help="JSON file containing the updates array (see module docstring for format)",
    )
    tag_group.add_argument(
        "--element-id",
        metavar="ID",
        help="Single element ID to tag (use with --tag)",
    )
    tag_group.add_argument(
        "--element-name",
        metavar="NAME",
        help="Single element name to tag (use with --tag)",
    )

    parser.add_argument(
        "--tag",
        metavar="KEY=VALUE",
        action="append",
        dest="tags",
        help="Tag to set as KEY=VALUE. Repeat for multiple tags. Use KEY= to clear a tag.",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        default=False,
        help="Replace all existing tags on the element (default: merge/update only)",
    )

    # Job execution
    parser.add_argument("--tool-version", default="2024x-refresh2", help="Cameo tool version (default: 2024x-refresh2)")
    parser.add_argument("--os", default="Windows 11", help="Target agent OS (default: 'Windows 11')")
    parser.add_argument("--poll-interval", type=int, default=10, help="Seconds between job status polls (default: 10)")
    parser.add_argument("--timeout", type=int, default=3600, help="Max seconds to wait for job (default: 3600)")

    # Model metadata
    parser.add_argument("--display-name", default=None, help="Human-readable name for the uploaded model")
    parser.add_argument("--description", default=None, help="Description for the uploaded model")

    return parser.parse_args()


def build_client(args) -> Client:
    registry_url = args.url or os.environ.get("ISTARI_REGISTRY_URL")
    registry_auth_token = args.token or os.environ.get("ISTARI_REGISTRY_AUTH_TOKEN")

    if not registry_url:
        sys.exit("Error: registry URL not set. Use --url or set ISTARI_REGISTRY_URL.")
    if not registry_auth_token:
        sys.exit("Error: auth token not set. Use --token or set ISTARI_REGISTRY_AUTH_TOKEN.")

    return Client(Configuration(
        registry_url=registry_url,
        registry_auth_token=registry_auth_token,
    ))


def build_updates(args) -> list[dict]:
    """Build the updates array from either a JSON file or inline CLI flags."""
    if args.tags_file:
        with open(args.tags_file) as f:
            updates = json.load(f)
        if not isinstance(updates, list):
            sys.exit("Error: --tags-file must contain a JSON array at the top level.")
        return updates

    # Inline single-element update
    if not args.tags:
        sys.exit("Error: --element-id / --element-name requires at least one --tag KEY=VALUE.")

    tags: dict = {}
    for kv in args.tags:
        if "=" not in kv:
            sys.exit(f"Error: --tag must be KEY=VALUE, got: {kv!r}")
        key, _, value = kv.partition("=")
        tags[key] = value if value else None  # empty value → clear the tag

    update: dict = {"tags": tags, "replace_existing": args.replace_existing}
    if args.element_id:
        update["element_id"] = args.element_id
    else:
        update["element_name"] = args.element_name

    return [update]


def build_parameters(args, updates: list[dict]) -> dict:
    """Assemble the job parameters dict for @istari:update_tags."""
    params: dict = {"updates": updates}

    # TWC credentials — required for Teamwork Cloud models
    twc_url = args.twc_url or os.environ.get("TWC_URL")
    twc_username = args.twc_username or os.environ.get("TWC_USERNAME")
    twc_password = args.twc_password or os.environ.get("TWC_PASSWORD")

    if twc_url:
        params["twc_link"] = twc_url
    if twc_username and twc_password:
        params["auth_info"] = {
            "type": "basic",
            "username": twc_username,
            "password": twc_password,
        }
    elif twc_username or twc_password:
        sys.exit("Error: both --twc-username and --twc-password are required together.")

    return params


def upload_model(client: Client, model_path: str, display_name: str | None, description: str | None):
    print(f"Uploading model: {model_path}")
    model = client.add_model(
        path=model_path,
        display_name=display_name or os.path.basename(model_path),
        description=description,
    )
    print(f"  Model uploaded  id={model.id}  name={model.display_name}")
    return model


def submit_update_tags_job(
    client: Client,
    model_id: str,
    parameters: dict,
    tool_version: str,
    operating_system: str,
):
    print(f"Submitting @istari:update_tags job  tool_version={tool_version}  os={operating_system}")
    print(f"  Targeting {len(parameters['updates'])} element update(s)")
    job = client.add_job(
        model_id=model_id,
        function="@istari:update_tags",
        tool_name="dassault_cameo",
        tool_version=tool_version,
        operating_system=operating_system,
        parameters=parameters,
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
        print("Tags updated successfully.")
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

    model_path = args.model_path
    if not os.path.isfile(model_path):
        sys.exit(f"Error: file not found: {model_path}")
    if not model_path.lower().endswith(".mdzip"):
        print(f"Warning: expected a .mdzip file, got: {model_path}")

    updates = build_updates(args)
    parameters = build_parameters(args, updates)

    client = build_client(args)

    compat = client.check_compatibility()
    if compat:
        print(f"Connected to Istari platform  version={compat.server_version}")

    model = upload_model(client, model_path, args.display_name, args.description)
    job = submit_update_tags_job(client, model.id, parameters, args.tool_version, args.os)
    completed_job = wait_for_job(client, job.id, args.poll_interval, args.timeout)
    print_job_result(completed_job)


if __name__ == "__main__":
    main()
