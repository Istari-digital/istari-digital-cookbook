"""v2 Artifacts — create from a model, fetch, list, archive/restore."""

from uat.common import TestContext


def run(ctx: TestContext) -> None:
    model = ctx.shared.get("model")
    if not model:
        ctx.skip("add_artifact", "no model in ctx — run v2_models first")
        return

    @ctx.step("add_artifact — create an artifact from a model file")
    def artifact():
        return ctx.client.add_artifact(
            model_id=model.id,
            path=str(ctx.data("dummy.txt")),
            display_name="UAT v2 Artifact",
        )

    if artifact:
        ctx.register("artifact", artifact)

    @ctx.step("get_artifact — fetch artifact by id")
    def fetched():
        assert artifact, "depends on add_artifact"
        return ctx.client.get_artifact(artifact.id)

    @ctx.step("list_artifacts — paginate artifact results")
    def page():
        return ctx.client.list_artifacts(size=10)

    @ctx.step("archive_artifact — soft-delete an artifact")
    def _():
        assert artifact, "depends on add_artifact"
        ctx.client.archive_artifact(artifact.id)

    @ctx.step("restore_artifact — un-archive an artifact")
    def _r():
        assert artifact, "depends on add_artifact"
        ctx.client.restore_artifact(artifact.id)
