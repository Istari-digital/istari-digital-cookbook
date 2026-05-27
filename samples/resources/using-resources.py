#!/usr/bin/env python3
"""
Sample code: working with Files (resources) on the Istari Digital Platform.

Uses istari-digital-client 10.10.0 — V3Client (V3 resources API) where available,
otherwise the v2 Client.

Reads ISTARI_REGISTRY_URL and ISTARI_PERSONAL_ACCESS_TOKEN from samples/.env
(same convention as the other cookbook recipes).

How to run (from the cookbook repository root):

  uv sync --group dev
  uv run python samples/resources/using-resources.py

Or run the same recipe interactively: open samples/resources/using-resources.ipynb
(select the istari-client-cookbook kernel — see notebook intro).

After inspecting the platform UI, archive demo resources and allow a rerun:

  uv run python samples/resources/using-resources-clean.py

Actions demonstrated:
  - Upload a resource from your device (with and without display_name)
  - Upload with external identifier and version label
  - Search, filter, and get resources by id, revision, and external identifiers; resolve current user for owner filter; browse with cursor pagination
  - Add a comment on a resource
  - View resource details, versions, and comments
  - Upload a new version, compare revisions, download content
  - Archive a throwaway resource (main demo resources stay active for UI review)
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from importlib.metadata import version as pkg_version
from pathlib import Path

import dotenv
from istari_digital_client import Configuration
from istari_digital_client.client import Client
from istari_digital_client.v3_client import V3Client
from istari_digital_client.v3.models.archive_status import ArchiveStatus
from istari_digital_client.v3.models.resource_type_dto import ResourceTypeDto

EXPECTED_CLIENT_VERSION = "10.10.0"
SCRIPT_DIR = Path(__file__).resolve().parent
SAMPLES_DIR = SCRIPT_DIR.parent
STATE_PATH = SCRIPT_DIR / ".using-resources-state.json"
RECIPE_TAG = "using-resources-recipe"

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


def ui_base_from_registry(registry_url: str) -> str:
    match = re.match(r"^(https?://)(?:fileservice-v2\.)?(.+?)/?$", registry_url)
    if not match:
        return registry_url.rstrip("/")
    return f"{match.group(1)}{match.group(2)}"


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))
    print(f"Saved state → {STATE_PATH}")


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def verify_resource_metadata(
    v3: V3Client,
    resource_id: str,
    *,
    name: str,
    display_name: str | None,
    external_identifier: str | None,
    version_name: str | None,
) -> None:
    """Re-read the resource from the platform and assert stored metadata."""
    fetched = v3.get_resource(resource_id=resource_id)
    assert fetched.name == name, f"name: got {fetched.name!r}, want {name!r}"
    assert fetched.display_name == display_name, (
        f"display_name: got {fetched.display_name!r}, want {display_name!r}"
    )
    assert fetched.external_identifier == external_identifier, (
        f"external_identifier: got {fetched.external_identifier!r}, "
        f"want {external_identifier!r}"
    )
    assert fetched.version_name == version_name, (
        f"version_name: got {fetched.version_name!r}, want {version_name!r}"
    )


def report_upload(
    ui_base: str,
    resource_id: str,
    *,
    name: str,
    display_name: str | None,
    external_identifier: str | None,
    version_name: str | None,
) -> None:
    """Print upload outcome after create_resource + verify_resource_metadata."""
    print(f"Uploaded model resource_id={resource_id}")
    print(f"  name={name!r}")
    print(f"  display_name={display_name!r}")
    print(f"  external_identifier={external_identifier!r}")
    print(f"  version_name={version_name!r}")
    print("  assert: get_resource() metadata matches")
    print(f"UI: {ui_base}/files/{resource_id}")


def main() -> int:
    assert_client_version()
    registry_url, token = load_env()
    config = Configuration(registry_url=registry_url, registry_auth_token=token)
    v3 = V3Client(config)
    client = Client(config)
    ui_base = ui_base_from_registry(registry_url)

    xlsx_v1 = SAMPLES_DIR / "Group3-UAS-Requirements.xlsx"
    xlsx_v2 = SAMPLES_DIR / "Group3-UAS-Requirements-v2.xlsx"
    if not xlsx_v1.is_file():
        raise FileNotFoundError(f"Sample spreadsheet not found: {xlsx_v1}")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    state: dict = {"run_id": run_id, "resource_ids": [], "comment_ids": []}

    print(f"istari-digital-client {EXPECTED_CLIENT_VERSION}")
    print(f"Registry: {registry_url}")

    # -------------------------------------------------------------------------
    # 1. Upload a resource from your device (no display_name)
    # UI: Files list — row shows the file name when display_name is omitted.
    # -------------------------------------------------------------------------
    section("1 · Upload a resource from your device (no display_name)")
    minimal = v3.create_resource(
        path=xlsx_v1,
        resource_type=ResourceTypeDto.MODEL,
    )
    state["resource_ids"].append(minimal.resource_id)

    assert minimal.resource_id
    assert minimal.file_revision_id
    assert not minimal.archived
    verify_resource_metadata(
        v3,
        minimal.resource_id,
        name=xlsx_v1.name,
        display_name=None,
        external_identifier=None,
        version_name=None,
    )
    report_upload(
        ui_base,
        minimal.resource_id,
        name=xlsx_v1.name,
        display_name=None,
        external_identifier=None,
        version_name=None,
    )

    # -------------------------------------------------------------------------
    # 2. Upload a resource with display_name, description, and version
    # UI: Files list — row shows display_name; open detail page.
    # -------------------------------------------------------------------------
    section("2 · Upload a resource with display_name")
    display_name = f"{RECIPE_TAG} UAS Requirements ({run_id})"
    uploaded = v3.create_resource(
        path=xlsx_v1,
        resource_type=ResourceTypeDto.MODEL,
        display_name=display_name,
        description=f"Cookbook upload demo {run_id}",
        version_name="v1",
    )
    resource_id = uploaded.resource_id
    state["primary_resource_id"] = resource_id
    state["resource_ids"].append(resource_id)

    assert uploaded.resource_id
    assert uploaded.file_revision_id
    assert not uploaded.archived
    verify_resource_metadata(
        v3,
        resource_id,
        name=xlsx_v1.name,
        display_name=display_name,
        external_identifier=None,
        version_name="v1",
    )
    report_upload(
        ui_base,
        resource_id,
        name=xlsx_v1.name,
        display_name=display_name,
        external_identifier=None,
        version_name="v1",
    )

    # -------------------------------------------------------------------------
    # 3. Upload with external identifier and version label
    # UI: File detail → External ID and Version fields on the revision.
    # -------------------------------------------------------------------------
    section("3 · Upload with external id and version label")
    external_id = f"{RECIPE_TAG}-ext-{run_id}"
    external_version = f"{RECIPE_TAG}-ver-{run_id}"
    external_display_name = f"{RECIPE_TAG} external ids ({run_id})"
    with_external = v3.create_resource(
        path=xlsx_v1,
        resource_type=ResourceTypeDto.MODEL,
        display_name=external_display_name,
        external_identifier=external_id,
        version_name=external_version,
    )
    state["external_resource_id"] = with_external.resource_id
    state["resource_ids"].append(with_external.resource_id)

    assert with_external.resource_id
    assert with_external.file_revision_id
    assert not with_external.archived
    verify_resource_metadata(
        v3,
        with_external.resource_id,
        name=xlsx_v1.name,
        display_name=external_display_name,
        external_identifier=external_id,
        version_name=external_version,
    )
    report_upload(
        ui_base,
        with_external.resource_id,
        name=xlsx_v1.name,
        display_name=external_display_name,
        external_identifier=external_id,
        version_name=external_version,
    )

    # -------------------------------------------------------------------------
    # 4. Search and filter resources; get by id, revision, and external identifiers
    # UI: Files list filters / search bar; file detail via direct lookup.
    # -------------------------------------------------------------------------
    section("4 · Search and filter resources")

    # Search by file name — may return many resources (same filename uploaded repeatedly).
    by_name = v3.list_resources(name=[xlsx_v1.name], size=50, include_total=True)
    assert by_name.total is not None
    assert by_name.total >= 1
    name_match_ids = {r.resource_id for r in by_name.items}
    assert resource_id in name_match_ids
    print(f"name={xlsx_v1.name!r} matched {by_name.total} resource(s)")

    # Get one resource by id — use get_resource (not list_resources).
    detail = v3.get_resource(resource_id=resource_id)
    assert detail.resource_id == resource_id
    assert detail.display_name == display_name
    assert detail.version_name == "v1"
    print(f"get_resource({resource_id}) → display_name={detail.display_name!r}")

    # Get a specific revision — use get_resource_revision (resource_id + revision_id).
    revision = v3.get_resource_revision(
        resource_id=resource_id,
        revision_id=detail.file_revision_id,
    )
    assert revision.file_revision_id == detail.file_revision_id
    assert revision.owning_entity_id == resource_id
    assert revision.version_name == "v1"
    print(
        f"get_resource_revision({resource_id}, {detail.file_revision_id}) "
        f"→ version_name={revision.version_name!r}"
    )

    # Find a resource by external identifier (uploaded in section 3).
    by_external = v3.list_resources(external_identifier=[external_id])
    assert any(r.resource_id == with_external.resource_id for r in by_external.items)
    print(
        f"external_identifier={external_id!r} → resource_id={with_external.resource_id}"
    )

    # Narrow with external identifier and version label together.
    by_external_version = v3.list_resources(
        external_identifier=[external_id],
        version_name=[external_version],
    )
    assert len(by_external_version.items) == 1
    match = by_external_version.items[0]
    assert match.resource_id == with_external.resource_id
    assert match.external_identifier == external_id
    assert match.version_name == external_version
    print(
        f"external_identifier={external_id!r} version_name={external_version!r} "
        f"→ resource_id={match.resource_id}"
    )

    current_user = client.get_current_user()
    assert current_user.id
    assert detail.created_by_id == current_user.id
    print(
        f"get_current_user() → id={current_user.id!r} email={current_user.email!r}"
    )

    # Other list_resources filters (each returns a page; may match multiple rows).
    by_description = v3.list_resources(description=[f"Cookbook upload demo {run_id}"])
    assert any(r.resource_id == resource_id for r in by_description.items)

    by_version = v3.list_resources(version_name=["v1"])
    assert any(r.resource_id == resource_id for r in by_version.items)

    by_owner = v3.list_resources(created_by_id=[current_user.id])
    assert any(r.resource_id == resource_id for r in by_owner.items)

    by_status = v3.list_resources(archive_status=ArchiveStatus.ACTIVE)
    assert any(r.resource_id == resource_id for r in by_status.items)
    assert not detail.archived
    print(f"Filter examples include resource_id={resource_id} (owner={current_user.id})")

    # -------------------------------------------------------------------------
    # 5. Browse resources you can access
    # UI: Files list page — all rows you have permission to view.
    #
    # Paginate through all non-archived models (v3 cursor pagination).
    # Keep include_total=True on every page — cursor + include_total=False 500s on demo.
    #
    #   cursor = None
    #   while True:
    #       page = v3.list_resources(
    #           type_name=["model"], size=50, cursor=cursor, include_total=True,
    #       )
    #       for r in page.items:
    #           ...
    #       cursor = page.next_page   # response field; pass as cursor= next call
    #       if not cursor:
    #           break
    # -------------------------------------------------------------------------
    section("5 · Browse resources you can access")
    cursor = None
    seen_ids: set[str] = set()
    preview = 0
    model_total: int | None = None
    while True:
        page = v3.list_resources(
            type_name=["model"],
            archive_status=ArchiveStatus.ACTIVE,
            size=50,
            cursor=cursor,
            include_total=True,
        )
        if model_total is None:
            assert page.total is not None
            model_total = page.total
            print(f"Active models total: {model_total}")

        for r in page.items:
            seen_ids.add(r.resource_id)
            if preview < 5:
                print(f"  {r.display_name or r.name}  id={r.resource_id}")
                preview += 1

        cursor = page.next_page
        if not cursor:
            break

    assert len(seen_ids) == model_total
    assert resource_id in seen_ids
    print(f"Iterated all {len(seen_ids)} active model(s) across cursor pages")

    # -------------------------------------------------------------------------
    # 6. Add a comment on a resource
    # UI: File detail → Comments tab.
    # -------------------------------------------------------------------------
    section("6 · Add a comment")
    comment_body = Path(tempfile.gettempdir()) / f"{RECIPE_TAG}-comment-{run_id}.txt"
    comment_body.write_text(f"Review note from {RECIPE_TAG} at {run_id}\n")
    try:
        comment = v3.create_comment(resource_id=resource_id, path=comment_body)
        state["comment_ids"].append(comment.id)
        text = v3.get_content(comment).decode("utf-8")
        assert RECIPE_TAG in text
        print(f"Added comment id={comment.id}")
        print(f"Comments tab in UI: {ui_base}/files/{resource_id}")
    finally:
        comment_body.unlink(missing_ok=True)

    # -------------------------------------------------------------------------
    # 7. View resource details including IDs, versions, and comments
    # UI: File detail header, Versions tab, Comments tab.
    # -------------------------------------------------------------------------
    section("7 · View resource details")
    revisions = v3.list_resource_revisions(resource_id=resource_id, include_total=True)
    comments_page = v3.list_comments(resource_id=resource_id, include_total=True)

    assert detail.file_id
    assert detail.file_revision_id
    assert revisions.total is not None and revisions.total >= 1
    assert comments_page.total is not None and comments_page.total >= 1
    assert any(c.id == comment.id for c in comments_page.items)
    print(f"resource_id={detail.resource_id}")
    print(f"file_id={detail.file_id}  revision_id={detail.file_revision_id}")
    print(f"Revisions: {revisions.total}  Comments: {comments_page.total}")

    # -------------------------------------------------------------------------
    # 8. Upload a new version of an existing resource
    # UI: Versions tab → new revision row.
    # -------------------------------------------------------------------------
    section("8 · Upload a new version")
    version_path = xlsx_v2 if xlsx_v2.is_file() else xlsx_v1
    revision_v2 = v3.create_resource_revision(
        resource_id=resource_id,
        path=version_path,
        description=f"Second revision {run_id}",
        version_name="v2",
        display_name=f"{display_name} (v2)",
    )
    state["revision_v2_id"] = revision_v2.file_revision_id

    assert revision_v2.owning_entity_id == resource_id
    assert revision_v2.file_revision_id != detail.file_revision_id
    rev_list = v3.list_resource_revisions(resource_id=resource_id, include_total=True)
    assert rev_list.total is not None and rev_list.total >= 2
    print(f"New revision_id={revision_v2.file_revision_id}")

    # -------------------------------------------------------------------------
    # 9. Compare resource versions side by side
    # UI: Versions tab → select two revisions → Compare.
    # SDK: compare content hashes (no dedicated compare API in 10.10.0).
    # -------------------------------------------------------------------------
    section("9 · Compare resource versions")
    rev_v1 = v3.get_resource_revision(
        resource_id=resource_id,
        revision_id=detail.file_revision_id,
    )
    rev_v2 = v3.get_resource_revision(
        resource_id=resource_id,
        revision_id=revision_v2.file_revision_id,
    )
    content_v1 = v3.get_content(rev_v1)
    content_v2 = v3.get_content(rev_v2)
    same_bytes = content_v1 == content_v2
    print(f"v1 bytes={len(content_v1)}  v2 bytes={len(content_v2)}  identical={same_bytes}")
    if not same_bytes:
        assert rev_v1.content_token is not None and rev_v2.content_token is not None
        assert rev_v1.content_token.sha != rev_v2.content_token.sha
    print(f"Compare in UI: {ui_base}/files/{resource_id} (Versions → Compare)")

    # -------------------------------------------------------------------------
    # 10. Download resource content
    # UI: Download button on file detail / revision.
    # -------------------------------------------------------------------------
    section("10 · Download resource content")
    download_dir = SCRIPT_DIR / "downloads"
    download_dir.mkdir(exist_ok=True)
    dest = download_dir / f"{RECIPE_TAG}-{run_id}.xlsx"
    dest.write_bytes(v3.get_content(detail))
    assert dest.stat().st_size == xlsx_v1.stat().st_size
    state["download_path"] = str(dest)
    print(f"Wrote {dest} ({dest.stat().st_size} bytes)")

    # -------------------------------------------------------------------------
    # 11. Archive a resource (throwaway — primary demo resource stays active)
    # UI: Archived filter shows this resource; primary resource remains active.
    # -------------------------------------------------------------------------
    section("11 · Archive a resource (throwaway)")
    archive_display_name = f"{RECIPE_TAG} archive demo ({run_id})"
    archive_target = v3.create_resource(
        path=xlsx_v1,
        resource_type=ResourceTypeDto.FILE,
        display_name=archive_display_name,
    )
    state["archive_demo_resource_id"] = archive_target.resource_id
    state["resource_ids"].append(archive_target.resource_id)

    verify_resource_metadata(
        v3,
        archive_target.resource_id,
        name=xlsx_v1.name,
        display_name=archive_display_name,
        external_identifier=None,
        version_name=None,
    )

    v3.archive_resource(resource_id=archive_target.resource_id)
    archived = v3.get_resource(resource_id=archive_target.resource_id)
    assert archived.archived
    archived_list = v3.list_resources(
        resource_id=[archive_target.resource_id],
        archive_status=ArchiveStatus.ARCHIVED,
    )
    assert len(archived_list.items) == 1
    print(f"Archived throwaway resource_id={archive_target.resource_id}")

    save_state(state)
    print("\nDone. Inspect the UI, then run using-resources-clean.py to archive demo resources.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
