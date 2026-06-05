"""v3 Revisions — create, list, get, download content."""

from uat.common import TestContext


def run(ctx: TestContext) -> None:
    resource = ctx.shared.get("v3_resource")
    if not resource:
        ctx.skip("create_resource_revision", "no v3_resource in ctx — run v3_resources first")
        ctx.skip("list_resource_revisions", "no v3_resource in ctx")
        ctx.skip("get_resource_revision", "no v3_resource in ctx")
        ctx.skip("get_content", "no v3_resource in ctx")
        return

    rid = resource.resource_id

    @ctx.step("create_resource_revision — add a new revision to an existing resource")
    def revision():
        return ctx.v3.create_resource_revision(
            resource_id=rid,
            path=ctx.data("dummy.txt"),
            display_name="UAT v3 Revision 2",
            version_name="v2",
        )

    if revision:
        ctx.shared["v3_revision_id"] = revision.file_revision_id

    @ctx.step("list_resource_revisions — list all revisions of a resource")
    def revs_page():
        return ctx.v3.list_resource_revisions(resource_id=rid)

    rev_id = ctx.shared.get("v3_revision_id")
    if not rev_id and revs_page and revs_page.items:
        rev_id = revs_page.items[0].file_revision_id
        ctx.shared["v3_revision_id"] = rev_id

    if not rev_id:
        ctx.skip("get_resource_revision", "no revision id available")
        ctx.skip("get_content", "no revision id available")
        return

    @ctx.step("get_resource_revision — fetch a specific revision by id")
    def rev():
        assert isinstance(rid, str) and isinstance(rev_id, str)
        return ctx.v3.get_resource_revision(resource_id=rid, revision_id=rev_id)

    @ctx.step("get_content — download raw bytes for a revision")
    def content():
        assert rev, "depends on get_resource_revision"
        data = ctx.v3.get_content(rev)
        assert len(data) > 0, "downloaded 0 bytes"
        return data
