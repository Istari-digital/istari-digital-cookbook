#!/usr/bin/env python3
"""
Archive and clean up resources created by using-resources.py.

Uses istari-digital-client 10.10.0 (V3Client).
Reads ISTARI_REGISTRY_URL and ISTARI_PERSONAL_ACCESS_TOKEN from samples/.env.

How to run (from the cookbook repository root, after using-resources.py):

  uv sync --group dev
  uv run python samples/resources/using-resources-clean.py
"""

from __future__ import annotations

import json
import os
import sys
from importlib.metadata import version as pkg_version
from pathlib import Path

import dotenv
from istari_digital_client import Configuration
from istari_digital_client.v3_client import V3Client
from istari_digital_client.v3.models.archive_status import ArchiveStatus

EXPECTED_CLIENT_VERSION = "10.10.0"
SCRIPT_DIR = Path(__file__).resolve().parent
SAMPLES_DIR = SCRIPT_DIR.parent
STATE_PATH = SCRIPT_DIR / ".using-resources-state.json"


def assert_client_version() -> None:
    installed = pkg_version("istari-digital-client")
    assert installed == EXPECTED_CLIENT_VERSION, (
        f"Expected istari-digital-client=={EXPECTED_CLIENT_VERSION}, got {installed}"
    )


def load_env() -> tuple[str, str]:
    dotenv.load_dotenv(SAMPLES_DIR / ".env")
    registry_url = os.environ.get("ISTARI_REGISTRY_URL")
    token = os.environ.get("ISTARI_PERSONAL_ACCESS_TOKEN")
    if not registry_url or not token:
        raise RuntimeError(
            "Set ISTARI_REGISTRY_URL and ISTARI_PERSONAL_ACCESS_TOKEN in samples/.env"
        )
    return registry_url, token


def main() -> int:
    assert_client_version()
    if not STATE_PATH.is_file():
        print(f"No state file at {STATE_PATH}; nothing to clean.")
        return 0

    state = json.loads(STATE_PATH.read_text())
    registry_url, token = load_env()
    config = Configuration(registry_url=registry_url, registry_auth_token=token)
    v3 = V3Client(config)

    resource_ids: list[str] = list(dict.fromkeys(state.get("resource_ids", [])))
    download_path = state.get("download_path")

    print(f"Cleaning run_id={state.get('run_id', '?')} ({len(resource_ids)} resource(s))")

    for resource_id in resource_ids:
        try:
            current = v3.get_resource(resource_id=resource_id)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            print(f"  skip resource_id={resource_id}: {exc}")
            continue

        if current.archived:
            print(f"  already archived: {resource_id}")
            continue

        v3.archive_resource(resource_id=resource_id)
        archived = v3.get_resource(resource_id=resource_id)
        assert archived.archived
        active = v3.list_resources(
            resource_id=[resource_id],
            archive_status=ArchiveStatus.ACTIVE,
        )
        assert len(active.items) == 0
        print(f"  archived: {resource_id}")

    if download_path:
        path = Path(download_path)
        if path.is_file():
            path.unlink()
            print(f"  removed download: {path}")

    STATE_PATH.unlink(missing_ok=True)
    print(f"Removed {STATE_PATH}")
    print("Cleanup complete — you can rerun using-resources.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
