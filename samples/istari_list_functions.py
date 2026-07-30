"""
List all functions registered on the Istari platform, optionally filtered by tool name.
Use this to discover the correct function names available on your instance.

Usage:
  python istari_list_functions.py
  python istari_list_functions.py --tool dassault_cameo
  python istari_list_functions.py --model-id <model-id>
"""

import argparse
import os
import sys

from istari_digital_client import Client, Configuration


def parse_args():
    parser = argparse.ArgumentParser(description="List available Istari platform functions")
    parser.add_argument("--url", default=None, help="Istari registry URL")
    parser.add_argument("--token", default=None, help="Istari auth token")
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG, metavar="FILE",
        help=f"JSON credentials file {{\"url\":...,\"token\":...}} (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument("--tool", default=None, help="Filter by tool name (e.g. dassault_cameo)")
    parser.add_argument("--model-id", default=None, help="Show jobs/functions tied to a specific model ID")
    parser.add_argument("--search", default=None, help="Filter function names containing this string")
    return parser.parse_args()


DEFAULT_CONFIG = os.path.expanduser("~/.istari/config.json")


def load_config_file(path):
    """Load url/token from a JSON config file. Returns (url, token), either may be None."""
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        if path != DEFAULT_CONFIG:
            sys.exit(f"Error: config file not found: {expanded}")
        return None, None
    try:
        with open(expanded) as f:
            cfg = json.load(f)
    except Exception as e:
        sys.exit(f"Error reading config file {expanded}: {e}")
    return cfg.get("url"), cfg.get("token")


def build_client(args) -> Client:
    cfg_url, cfg_token = load_config_file(args.config)
    registry_url = args.url or os.environ.get("ISTARI_REGISTRY_URL") or cfg_url
    registry_auth_token = args.token or os.environ.get("ISTARI_REGISTRY_AUTH_TOKEN") or cfg_token
    if not registry_url:
        sys.exit(
            f"Error: registry URL not set.\n"
            f"  Use --url, set ISTARI_REGISTRY_URL, or add \"url\" to {args.config}"
        )
    if not registry_auth_token:
        sys.exit(
            f"Error: auth token not set.\n"
            f"  Use --token, set ISTARI_REGISTRY_AUTH_TOKEN, or add \"token\" to {args.config}"
        )
    return Client(Configuration(registry_url=registry_url, registry_auth_token=registry_auth_token))


def main():
    args = parse_args()
    client = build_client(args)

    compat = client.check_compatibility()
    if compat:
        print(f"Connected to Istari platform  version={compat.server_version}\n")

    print("Fetching available functions...")
    page, size = 1, 100
    all_functions = []

    while True:
        result = client.list_functions(
            tool=args.tool or None,
            page=page,
            size=size,
            status="all",
        )
        # PageFunctionVersion may use .items or .content
        items = (
            result.items if hasattr(result, "items") and result.items is not None
            else result.content if hasattr(result, "content") and result.content is not None
            else []
        )
        all_functions.extend(items)
        if not items or len(items) < size:
            break
        page += 1

    if not all_functions:
        # Debug: show raw result attributes
        print(f"Raw result type: {type(result)}")
        print(f"Raw result attrs: {[a for a in dir(result) if not a.startswith('_')]}")
        try:
            print(f"Raw result: {result}")
        except Exception:
            pass

    # Apply search filter
    if args.search:
        all_functions = [f for f in all_functions if args.search.lower() in (getattr(f, "name", "") or "").lower()]

    if not all_functions:
        print("No functions found matching your filters.")
        return

    print(f"Found {len(all_functions)} function(s):\n")
    print(f"  {'NAME':<40} {'TOOL':<25} {'TOOL VERSION':<20} {'OS'}")
    print(f"  {'-'*40} {'-'*25} {'-'*20} {'-'*20}")
    for f in sorted(all_functions, key=lambda x: getattr(x, "name", "")):
        name = getattr(f, "name", "?")
        tool = getattr(f, "tool", None) or getattr(f, "tool_name", "?") or "—"
        version = getattr(f, "tool_version", "?") or "any"
        os_name = getattr(f, "operating_system", "?") or "any"
        print(f"  {name:<40} {tool:<25} {version:<20} {os_name}")

    # If a model ID was given, also show its existing jobs
    if args.model_id:
        print(f"\nRecent jobs for model {args.model_id}:")
        jobs = client.list_model_jobs(model_id=args.model_id, size=20)
        items = jobs.content if hasattr(jobs, "content") else []
        if not items:
            print("  (no jobs found)")
        else:
            print(f"  {'FUNCTION':<40} {'STATUS':<15} {'JOB ID'}")
            print(f"  {'-'*40} {'-'*15} {'-'*36}")
            for j in items:
                fn = getattr(j, "function", "?")
                status = j.status.status_name if j.status else "?"
                print(f"  {fn:<40} {status:<15} {j.id}")


if __name__ == "__main__":
    main()
