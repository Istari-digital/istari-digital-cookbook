"""v2 Control Tags — create tag, apply to model, inspect tagging history."""

from uat.common import TestContext


def run(ctx: TestContext) -> None:
    model = ctx.shared.get("model")

    @ctx.step("create_control_tag — create a new control tag")
    def tag():
        from istari_digital_client.v2.models.new_control_tag import NewControlTag
        return ctx.client.create_control_tag(
            NewControlTag(name=f"uat-tag-{ctx.run_id}", description="UAT control tag")
        )

    if tag:
        ctx.shared["control_tag"] = tag

    @ctx.step("list_control_tags — list all control tags")
    def page():
        return ctx.client.list_control_tags()

    @ctx.step("get_control_tag — fetch a control tag by id")
    def fetched():
        assert tag, "depends on create_control_tag"
        return ctx.client.get_control_tag(tag.id)

    if not model:
        ctx.skip("add_model_control_taggings", "no model in ctx — run v2_models first")
        ctx.skip("get_model_control_tags", "no model in ctx")
        ctx.skip("get_model_control_tagging_history", "no model in ctx")
        ctx.skip("remove_model_control_taggings", "no model in ctx")
        return

    @ctx.step("add_model_control_taggings — apply a control tag to a model")
    def tagging():
        assert tag, "depends on create_control_tag"
        return ctx.client.add_model_control_taggings(model.id, [tag.id])

    @ctx.step("get_model_control_tags — inspect control tags on a model")
    def model_tags():
        return ctx.client.get_model_control_tags(model.id)

    @ctx.step("get_model_control_tagging_history — audit history for model tags")
    def history():
        return ctx.client.get_model_control_tagging_history(model.id)

    @ctx.step("remove_model_control_taggings — remove a control tag from a model")
    def _():
        assert tag, "depends on create_control_tag"
        ctx.client.remove_model_control_taggings(model.id, [tag.id])
