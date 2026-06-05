"""v2 Documents — create, fetch, update, list, archive."""

from uat.common import TestContext


def run(ctx: TestContext) -> None:
    config = ctx.shared.get("configuration")
    if not config:
        ctx.skip("create_document", "no configuration in ctx — run v2_systems first")
        return

    @ctx.step("create_document — create a rich-text document in a configuration")
    def doc():
        from istari_digital_client.v2.models.create_document_request import CreateDocumentRequest
        return ctx.client.create_document(
            CreateDocumentRequest(
                configuration_id=config.id,
                name="UAT v2 Document",
                content={"type": "doc", "content": []},
            )
        )

    if doc:
        ctx.register("document", doc)

    @ctx.step("get_document — fetch document by id")
    def fetched():
        assert doc, "depends on create_document"
        return ctx.client.get_document(doc.id)

    @ctx.step("update_document — update document content")
    def updated():
        assert doc, "depends on create_document"
        from istari_digital_client.v2.models.update_document_request import UpdateDocumentRequest
        return ctx.client.update_document(
            doc.id,
            UpdateDocumentRequest(
                name="UAT v2 Document (updated)",
                content={"type": "doc", "content": []},
            ),
        )

    @ctx.step("list_documents — paginate document results")
    def page():
        return ctx.client.list_documents(size=10)

    @ctx.step("archive_document — soft-delete a document")
    def _():
        assert doc, "depends on create_document"
        ctx.client.archive_document(doc.id)
