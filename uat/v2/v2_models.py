"""v2 Models — upload, fetch, revision, list, archive/restore."""

from uat.common import TestContext


def run(ctx: TestContext) -> None:

    @ctx.step("upload_model — create a new model from a local file")
    def model():
        return ctx.platform.upload_model(
            ctx.data("dummy.txt"),
            external_id=f"{ctx.run_id}-v2-model",
            display_name="UAT v2 Model",
        )

    if model:
        ctx.register("model", model)

    @ctx.step("get_model — fetch a model by id")
    def fetched():
        assert model, "depends on upload_model"
        got = ctx.platform.get_model(model.id)
        assert got.id == model.id, "get_model returned wrong id"
        return got

    @ctx.step("update_model — add a new revision to an existing model")
    def updated():
        assert model, "depends on upload_model"
        return ctx.client.update_model(
            model_id=model.id,
            path=str(ctx.data("dummy.txt")),
            display_name="UAT v2 Model (rev 2)",
        )

    @ctx.step("list_models — paginate model results")
    def page():
        return ctx.client.list_models(size=10)

    @ctx.step("archive_model — soft-delete a model")
    def _archived():
        assert model, "depends on upload_model"
        ctx.client.archive_model(model.id)

    @ctx.step("restore_model — un-archive a model")
    def _restored():
        assert model, "depends on upload_model"
        ctx.client.restore_model(model.id)
