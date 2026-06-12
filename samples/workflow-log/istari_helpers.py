"""Small UI-convenience helpers for the walkthrough.

These capture a snapshot of a configuration so the tracked design surfaces when
the system is opened in the UI. They're best-effort: the workflow-log story does
not depend on them, so if the snapshot call isn't available on a given instance
they degrade to a printed note rather than breaking the notebook.
"""
from __future__ import annotations

from istari_digital_client.v2.models.new_snapshot import NewSnapshot


def _snapshot(client, configuration_id: str):
    return client.create_snapshot(
        configuration_id=configuration_id,
        new_snapshot=NewSnapshot(dry_run=False),
    )


def show_config_on_system(client, system_id: str, configuration_id: str) -> None:
    """Capture a snapshot so the configuration's files show on the system."""
    try:
        _snapshot(client, configuration_id)
        print("  (snapshot captured — design will show on the system)")
    except Exception as e:  # noqa: BLE001 - best-effort UI nicety
        print(f"  (skipped snapshot: {type(e).__name__}: {e})")


def capture_and_show(client, system_id: str, configuration_id: str) -> None:
    """Capture a fresh snapshot after a revision so the system shows the latest."""
    try:
        _snapshot(client, configuration_id)
        print("  (new snapshot captured — system now shows the latest revision)")
    except Exception as e:  # noqa: BLE001 - best-effort UI nicety
        print(f"  (skipped snapshot: {type(e).__name__}: {e})")
