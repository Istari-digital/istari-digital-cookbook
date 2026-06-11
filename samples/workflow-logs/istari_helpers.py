"""Display helpers for the workflow log demo.

Snapshot a configuration and point the system's baseline tag at the result,
so the system shows its latest tracked files when opened in the UI. The
workflow-log feature itself does not depend on any of this.
"""

from __future__ import annotations

from istari_digital_client.v2.models.new_snapshot import NewSnapshot
from istari_digital_client.v2.models.new_snapshot_tag import NewSnapshotTag
from istari_digital_client.v2.models.update_tag import UpdateTag


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


def _point_baseline_at(client, system_id: str, snapshot_id: str) -> None:
    """Move the system's baseline tag to the snapshot (create it if missing)."""
    try:
        baseline = client.get_system_baseline(system_id)
        client.update_tag(baseline.tag_id, update_tag=UpdateTag(snapshot_id=snapshot_id))
    except Exception:
        # no baseline yet — create one on this snapshot
        client.create_tag(snapshot_id, new_snapshot_tag=NewSnapshotTag(tag="baseline"))


def show_config_on_system(client, system_id: str, configuration_id: str) -> None:
    """Snapshot the configuration and baseline it so the UI shows its files.

    If the current state is already captured (snapshot no-op), the baseline
    still gets pointed at the newest snapshot — a fresh system's baseline tag
    starts on an empty pre-configuration snapshot, which would otherwise show
    no files.
    """
    snap = _snapshot(client, configuration_id)
    snapshot_id = snap.id if snap is not None else _latest_snapshot_id(
        client, system_id, configuration_id)
    if snapshot_id is not None:
        _point_baseline_at(client, system_id, snapshot_id)


def capture_and_show(client, system_id: str, configuration_id: str) -> None:
    """Re-snapshot after a tracked file changed and advance the baseline."""
    show_config_on_system(client, system_id, configuration_id)
