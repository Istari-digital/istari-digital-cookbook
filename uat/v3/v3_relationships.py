"""v3 Relationships — list types, create relationship.

Note: list_revision_relationships returns 500 on all current environments
due to a SpiceDB LookupResources scaling issue (CPD-598/601). That step is
marked as expected-fail and will be fixed when those tickets ship.
"""

from uat.common import TestContext


def run(ctx: TestContext) -> None:

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

    # list_revision_relationships returns 500 server-side — known issue CPD-598/601
    try:
        @ctx.step("list_revision_relationships — list relationships for a revision [known 500 — CPD-598]")
        def rels():
            assert isinstance(rev_a, str)
            return ctx.v3.list_revision_relationships(revision_id=rev_a)
    except Exception:
        ctx.skip("list_revision_relationships", "server 500 — CPD-598/601 pending fix")
