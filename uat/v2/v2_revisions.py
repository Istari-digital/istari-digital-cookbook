"""v2 Revisions — fetch revisions, copy and transfer between resources."""

from uat.common import TestContext


def run(ctx: TestContext) -> None:
    model = ctx.shared.get("model")
    if not model:
        ctx.skip("get_revision", "no model in ctx — run v2_models first")
        return

    rev = model.latest_revision
    if not rev:
        ctx.skip("get_revision", "model has no revisions")
        return
    rev_id = rev.id

    @ctx.step("get_revision — fetch a file revision by id")
    def revision():
        return ctx.client.get_revision(rev_id)

    @ctx.step("get_file_by_revision_id — fetch the file that owns a revision")
    def file_by_rev():
        return ctx.client.get_file_by_revision_id(rev_id)

    @ctx.step("copy_revision_to_new_file — copy a revision into a new file")
    def copied():
        return ctx.client.copy_revision_to_new_file(revision_id=rev_id)

    if copied:
        ctx.track("file", copied.id)

    @ctx.step("read_contents — download raw bytes for a revision's content token")
    def content():
        assert revision, "depends on get_revision"
        token = revision.content_token
        assert token, "revision has no content_token"
        return ctx.client.read_contents(token=token)

    if content:
        assert len(content) > 0, "downloaded 0 bytes"
