"""v3 suites — one section per topic of the V3Client reference page, in the
order documented at docs.istaridigital.com/developers/SDK/v3/v3-client/.
"""

import os
import tempfile
from pathlib import Path

from istari_digital_client.v3.models.resource_type_dto import ResourceTypeDto

from uat.common import TestContext


# ── Resources ────────────────────────────────────────────────────────────────

def resources(ctx: TestContext) -> None:
    """Create, get, list resources (Model and Artifact types)."""

    @ctx.step("create_resource (MODEL) — upload a file as a v3 Model resource")
    def resource():
        return ctx.v3.create_resource(
            path=ctx.data("dummy.txt"),
            resource_type=ResourceTypeDto.MODEL,
            display_name="UAT v3 Resource",
            description="Created by UAT runner",
        )

    if resource:
        ctx.register("v3_resource", resource)

    @ctx.step("get_resource — fetch a v3 resource by id")
    def fetched():
        assert resource, "depends on create_resource"
        return ctx.v3.get_resource(resource.resource_id)

    @ctx.step("create_resource (ARTIFACT) — upload a file as a v3 Artifact resource")
    def artifact():
        return ctx.v3.create_resource(
            path=ctx.data("dummy.txt"),
            resource_type=ResourceTypeDto.ARTIFACT,
            display_name="UAT v3 Artifact",
        )

    if artifact:
        ctx.track("v3_resource", artifact.resource_id)


# ── Revisions & content ──────────────────────────────────────────────────────

def revisions(ctx: TestContext) -> None:
    """Create, list, get, download content."""
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


# ── Comments ─────────────────────────────────────────────────────────────────

def _comment_file(text: str) -> Path:
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.write(fd, text.encode())
    os.close(fd)
    return Path(path)


def comments(ctx: TestContext) -> None:
    """Create, reply, list, get, update, archive, restore."""
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


# ── Revision relationships ───────────────────────────────────────────────────

def relationships(ctx: TestContext) -> None:
    """List types, create relationship.

    Note: list_revision_relationships returns 500 on all current environments
    due to a SpiceDB LookupResources scaling issue (CPD-598/601). That step is
    marked as expected-fail and will be fixed when those tickets ship.
    """

    @ctx.step("list_revision_relationship_types — enumerate available relationship types")
    def rel_types():
        return ctx.v3.list_revision_relationship_types()

    rel_type_id = None
    if rel_types and rel_types.items:
        rel_type_id = rel_types.items[0].id

    rid = ctx.shared.get("v3_resource")
    rev_id = ctx.shared.get("v3_revision_id")

    if not rid or not rev_id or not rel_type_id:
        ctx.skip("create_revision_relationship", "need v3_resource, v3_revision_id, and a rel type")
        ctx.skip("list_revision_relationships", "depends on create_revision_relationship")
        return

    resource_id = rid.resource_id

    @ctx.step("list_resource_revisions — get two revision ids to link")
    def revs():
        assert isinstance(resource_id, str)
        return ctx.v3.list_resource_revisions(resource_id=resource_id)

    rev_a = rev_b = None
    if revs and revs.items and len(revs.items) >= 2:
        rev_a = revs.items[0].file_revision_id
        rev_b = revs.items[1].file_revision_id

    if not rev_a or not rev_b:
        ctx.skip("create_revision_relationship", "need at least 2 revisions")
        ctx.skip("list_revision_relationships", "depends on create_revision_relationship")
        return

    @ctx.step("create_revision_relationship — link two revisions with a typed relationship")
    def rel():
        from istari_digital_client.v3.models.new_revision_relationship_dto import NewRevisionRelationshipDto
        assert isinstance(rev_a, str) and isinstance(rev_b, str) and isinstance(rel_type_id, str)
        return ctx.v3.create_revision_relationship(
            new_revision_relationship_dto=NewRevisionRelationshipDto(
                left_revision_id=rev_a,
                right_revision_id=rev_b,
                relationship_type_id=rel_type_id,
            )
        )

    # Returns 500 server-side (CPD-598/601); ctx.step records the FAIL.
    @ctx.step("list_revision_relationships — list relationships for a revision [known 500 — CPD-598]")
    def rels():
        assert isinstance(rev_a, str)
        return ctx.v3.list_revision_relationships(revision_id=rev_a)


# ── Remote connections ───────────────────────────────────────────────────────

def remotes(ctx: TestContext) -> None:
    """List sending and receiving remote connections."""

    @ctx.step("list_sending_remotes — enumerate outbound remote connections")
    def sending():
        return ctx.v3.list_sending_remotes()

    if sending and sending.items:
        @ctx.step("get_sending_remote — fetch a sending remote by id")
        def _():
            remote_id = sending.items[0].id
            assert isinstance(remote_id, str)
            return ctx.v3.get_sending_remote(remote_id=remote_id)
    else:
        ctx.skip("get_sending_remote", "list_sending_remotes failed or returned no remotes")

    @ctx.step("list_receiving_remotes — enumerate inbound remote connections")
    def receiving():
        return ctx.v3.list_receiving_remotes()

    if receiving and receiving.items:
        @ctx.step("get_receiving_remote — fetch a receiving remote by id")
        def _r():
            remote_id = receiving.items[0].id
            assert isinstance(remote_id, str)
            return ctx.v3.get_receiving_remote(remote_id=remote_id)
    else:
        ctx.skip("get_receiving_remote", "list_receiving_remotes failed or returned no remotes")
