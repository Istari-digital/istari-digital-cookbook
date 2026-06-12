"""Helpers for the external workflow log tutorials.

Registry version checks for Connect, system lookup, and branching-style
commits (snapshot + move the baseline tag).
"""

from __future__ import annotations

import re

from istari_digital_client.v2.models.new_snapshot import NewSnapshot
from istari_digital_client.v2.models.new_snapshot_tag import NewSnapshotTag
from istari_digital_client.v2.models.update_tag import UpdateTag

# External workflow log SDK endpoints require Registry Service > 10.17.3 (2026-05+).
MINIMUM_REGISTRY_VERSION = "10.17.3"
_VERSION_PREFIX = re.compile(r"^[vV]?(\d+(?:\.\d+)*)")


def parse_registry_version(version: str) -> tuple[int, ...]:
    """Parse ``X-Istari-Registry-Version`` to a comparable numeric tuple."""
    match = _VERSION_PREFIX.match(version.strip())
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def registry_meets_minimum(version: str, minimum: str = MINIMUM_REGISTRY_VERSION) -> bool:
    """Return True when *version* exceeds *minimum*, or is a dev build (contains ``commit``)."""
    if "commit" in version.casefold():
        return True
    parsed = parse_registry_version(version)
    return bool(parsed) and parsed > parse_registry_version(minimum)


def find_system_by_name(client, name: str):
    """Return the first active system with an exact *name*, or ``None``."""
    from istari_digital_client.v2.models.archive_status import ArchiveStatus

    for system in client.list_systems(size=100, archive_status=ArchiveStatus.ACTIVE).iter_items():
        if system.name == name:
            return system
    return None


def find_configuration(client, system_id: str, config_name: str):
    """Return a configuration by name on *system_id*, or ``None``."""
    for cfg in client.list_system_configurations(system_id).iter_items():
        if cfg.name == config_name:
            return cfg
    return None


def model_id_for_file(client, file_id: str) -> str | None:
    """Return the model id that owns *file_id*, if any."""
    for model in client.list_models(size=100).iter_items():
        if model.file.id == file_id:
            return model.id
    return None


def _snapshot(client, configuration_id: str):
    """Capture a snapshot of the configuration.

    Returns the new Snapshot, or None when the platform reports a no-op
    (``NoOpResponse``: the current state is already captured by an
    existing snapshot).
    """
    resp = client.create_snapshot(configuration_id, new_snapshot=NewSnapshot())
    snap = getattr(resp, "actual_instance", resp)
    return snap if hasattr(snap, "id") else None


def _latest_snapshot_id(client, system_id: str, configuration_id: str) -> str | None:
    """Newest snapshot on the system, preferring ones for this configuration."""
    page = client.list_snapshots(system_id=system_id, size=50)
    snaps = sorted(page.items, key=lambda s: s.created, reverse=True)
    for s in snaps:
        if getattr(s, "configuration_id", None) == configuration_id:
            return s.id
    return snaps[0].id if snaps else None


def _move_baseline_tag(client, system_id: str, snapshot_id: str) -> None:
    """Point the system's baseline tag at *snapshot_id* (create the tag if missing)."""
    try:
        baseline = client.get_system_baseline(system_id)
        client.update_tag(baseline.tag_id, update_tag=UpdateTag(snapshot_id=snapshot_id))
    except Exception:
        client.create_tag(snapshot_id, new_snapshot_tag=NewSnapshotTag(tag="baseline"))


def commit_changes(client, system_id: str, configuration_id: str) -> None:
    """Commit the configuration state to the baseline branch.

    Captures a snapshot of *configuration_id* and moves the system's baseline
    tag to that snapshot — the same branching idea as ``save().set_baseline()``
    in ``istari_labs_helpers``: record the change, then advance baseline HEAD.

    If the platform returns a snapshot no-op (state already captured), the
    baseline tag still moves to the newest snapshot for this configuration.
    """
    snap = _snapshot(client, configuration_id)
    snapshot_id = snap.id if snap is not None else _latest_snapshot_id(
        client, system_id, configuration_id)
    if snapshot_id is not None:
        _move_baseline_tag(client, system_id, snapshot_id)
