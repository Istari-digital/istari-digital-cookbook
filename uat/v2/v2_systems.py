"""v2 Systems — create, configure, add tracked files, baseline, archive/restore."""

from uat.common import TestContext


def run(ctx: TestContext) -> None:
    model = ctx.shared.get("model")

    @ctx.step("create_system — create a new engineering system")
    def system():
        from istari_digital_client.v2.models.new_system import NewSystem
        return ctx.client.create_system(
            NewSystem(
                name=f"UAT v2 System {ctx.run_id}",
                description="Created by UAT runner",
            )
        )

    if system:
        ctx.register("system", system)

    @ctx.step("get_system — fetch system by id")
    def fetched():
        assert system, "depends on create_system"
        return ctx.client.get_system(system.id)

    @ctx.step("list_systems — paginate system results")
    def page():
        return ctx.client.list_systems(size=10)

    if not model:
        ctx.skip("create_configuration", "no model in ctx — skipping tracked-file steps")
        ctx.skip("list_configurations", "no model in ctx")
        ctx.skip("get_system_baseline", "no model in ctx")
    else:
        @ctx.step("create_configuration — add a configuration with a tracked model")
        def config():
            assert system, "depends on create_system"
            from istari_digital_client.v2.models.new_system_configuration import NewSystemConfiguration
            from istari_digital_client.v2.models.new_tracked_file import NewTrackedFile
            from istari_digital_client.v2.models.tracked_file_specifier_type import TrackedFileSpecifierType
            return ctx.client.create_configuration(
                system_id=system.id,
                new_system_configuration=NewSystemConfiguration(
                    name="uat-config-v1",
                    tracked_files=[
                        NewTrackedFile(
                            specifier_type=TrackedFileSpecifierType.LATEST,
                            file_id=model.file_id,
                        )
                    ],
                ),
            )

        if config:
            ctx.register("configuration", config)

        @ctx.step("list_configurations — list configurations on a system")
        def configs_page():
            assert system, "depends on create_system"
            return ctx.client.list_system_configurations(system.id, size=10)

        @ctx.step("get_system_baseline — fetch the baseline snapshot tag")
        def baseline():
            assert system, "depends on create_system"
            return ctx.client.get_system_baseline(system.id)

    @ctx.step("update_system — rename a system")
    def updated():
        assert system, "depends on create_system"
        from istari_digital_client.v2.models.update_system import UpdateSystem
        return ctx.client.update_system(
            system.id,
            UpdateSystem(name=f"UAT v2 System {ctx.run_id}", description="Updated by UAT runner"),
        )

    @ctx.step("archive_system — soft-delete a system")
    def _():
        assert system, "depends on create_system"
        ctx.client.archive_system(system.id)

    @ctx.step("restore_system — un-archive a system")
    def _r():
        assert system, "depends on create_system"
        ctx.client.restore_system(system.id)
