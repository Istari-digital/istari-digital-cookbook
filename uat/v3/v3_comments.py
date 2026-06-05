"""v3 Comments — create, reply, list, get, update, archive, restore."""

import tempfile
from pathlib import Path

from uat.common import TestContext


def _comment_file(text: str) -> Path:
    fd, path = tempfile.mkstemp(suffix=".txt")
    import os
    os.write(fd, text.encode())
    os.close(fd)
    return Path(path)


def run(ctx: TestContext) -> None:
    resource = ctx.shared.get("v3_resource")
    if not resource:
        ctx.skip("create_comment", "no v3_resource in ctx — run v3_resources first")
        return

    rid = resource.resource_id
    path = _comment_file("UAT v3 comment — created by UAT runner")

    @ctx.step("create_comment — post a comment on a resource")
    def comment():
        return ctx.v3.create_comment(resource_id=rid, path=path)

    path.unlink(missing_ok=True)

    if comment:
        ctx.track("v3_comment", comment.id, extra=rid)
        ctx.shared["v3_comment"] = comment

    @ctx.step("list_comments — list comments on a resource")
    def comments_page():
        return ctx.v3.list_comments(resource_id=rid)

    @ctx.step("get_comment — fetch a specific comment by id")
    def fetched():
        assert comment, "depends on create_comment"
        assert isinstance(rid, str) and isinstance(comment.id, str)
        return ctx.v3.get_comment(resource_id=rid, comment_id=comment.id)

    reply_path = _comment_file("UAT v3 reply comment")

    @ctx.step("create_comment (reply) — post a reply to an existing comment")
    def reply():
        assert comment, "depends on create_comment"
        assert isinstance(rid, str) and isinstance(comment.id, str)
        return ctx.v3.create_comment(
            resource_id=rid,
            path=reply_path,
            parent_comment_id=comment.id,
        )

    reply_path.unlink(missing_ok=True)

    if reply:
        ctx.track("v3_comment", reply.id, extra=rid)

    update_path = _comment_file("UAT v3 comment — updated text")

    @ctx.step("update_comment — edit a comment's content")
    def updated():
        assert comment, "depends on create_comment"
        assert isinstance(rid, str) and isinstance(comment.id, str)
        return ctx.v3.update_comment(
            resource_id=rid,
            comment_id=comment.id,
            path=update_path,
        )

    update_path.unlink(missing_ok=True)

    if reply:
        @ctx.step("archive_comment — soft-delete a comment")
        def _():
            assert isinstance(rid, str) and isinstance(reply.id, str)
            return ctx.v3.archive_comment(resource_id=rid, comment_id=reply.id)

        @ctx.step("restore_comment — un-archive a comment")
        def _r():
            assert isinstance(rid, str) and isinstance(reply.id, str)
            return ctx.v3.restore_comment(resource_id=rid, comment_id=reply.id)
