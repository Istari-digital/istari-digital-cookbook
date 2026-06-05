"""v2 Access — list and inspect permissions on a resource."""

from uat.common import TestContext


def run(ctx: TestContext) -> None:
    model = ctx.shared.get("model")
    if not model:
        ctx.skip("list_model_access", "no model in ctx — run v2_models first")
        return

    @ctx.step("list_model_access — list who has access to a model")
    def access_page():
        return ctx.client.list_model_access(model.id)

    me = ctx.shared.get("current_user")

    @ctx.step("list_model_access — verify access list returns successfully")
    def verified():
        assert access_page is not None, "depends on list_model_access"
        # list_model_access returns List[AccessRelationship] directly
        assert isinstance(access_page, list)
        return access_page
