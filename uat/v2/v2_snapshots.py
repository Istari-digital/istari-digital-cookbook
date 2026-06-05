"""v2 Snapshots — create, fetch, compare, list items, snapshot tags."""

from uat.common import TestContext


def run(ctx: TestContext) -> None:
    system = ctx.shared.get("system")
    config = ctx.shared.get("configuration")

    if not system or not config:
        ctx.skip("create_snapshot", "no system/configuration in ctx — run v2_systems first")
        ctx.skip("get_snapshot", "no system in ctx")
        ctx.skip("list_snapshots", "no system in ctx")
        ctx.skip("list_snapshot_items", "no system in ctx")
        return

    @ctx.step("create_snapshot — capture a point-in-time snapshot of a configuration")
    def snapshot_response():
        from istari_digital_client.v2.models.new_snapshot import NewSnapshot
        return ctx.client.create_snapshot(
            configuration_id=config.id,
            new_snapshot=NewSnapshot(),
        )

    # ResponseCreateSnapshot wraps a union (Snapshot | DryRunSnapshot | NoOpResponse)
    snapshot_id = getattr(getattr(snapshot_response, "actual_instance", None), "id", None)

    @ctx.step("get_snapshot — fetch snapshot by id")
    def fetched():
        assert snapshot_id, "depends on create_snapshot returning a real Snapshot"
        return ctx.client.get_snapshot(snapshot_id)

    @ctx.step("list_snapshots — list snapshots for a system")
    def page():
        return ctx.client.list_snapshots(system_id=system.id, size=10)

    @ctx.step("list_snapshot_items — list resources captured in a snapshot")
    def items():
        assert snapshot_id, "depends on create_snapshot"
        return ctx.client.list_snapshot_items(snapshot_id, size=10)

    @ctx.step("list_snapshot_subsystems — list subsystems in a snapshot")
    def subsystems():
        assert snapshot_id, "depends on create_snapshot"
        return ctx.client.list_snapshot_subsystems(snapshot_id, size=10)
