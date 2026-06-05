"""v2 Users — current user, list users, personal access tokens."""

from uat.common import TestContext


def run(ctx: TestContext) -> None:

    @ctx.step("get_current_user — fetch the authenticated user's profile")
    def me():
        return ctx.client.get_current_user()

    if me:
        ctx.shared["current_user"] = me

    @ctx.step("list_users — list users in the organisation")
    def users_page():
        return ctx.client.list_users()

    @ctx.step("list_personal_access_tokens — list PATs for the current user")
    def tokens_page():
        return ctx.client.list_personal_access_tokens(size=10)

    @ctx.step("create_personal_access_token — create a short-lived PAT")
    def new_token():
        return ctx.client.create_personal_access_token(name=f"uat-pat-{ctx.run_id}")

    if new_token:
        @ctx.step("delete_personal_access_token — delete the PAT created above")
        def _():
            ctx.client.delete_personal_access_token(new_token.id)
