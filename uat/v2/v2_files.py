"""v2 Files — standalone file upload, revision, list, archive/restore."""

from uat.common import TestContext


def run(ctx: TestContext) -> None:

    @ctx.step("add_file — upload a raw file (not a model resource)")
    def f():
        return ctx.client.add_file(
            path=str(ctx.data("dummy.txt")),
            display_name="UAT v2 File",
        )

    if f:
        ctx.register("file", f)

    @ctx.step("get_file — fetch a file by id")
    def fetched():
        assert f, "depends on add_file"
        return ctx.client.get_file(f.id)

    @ctx.step("update_file — add a new revision to an existing file")
    def updated():
        assert f, "depends on add_file"
        return ctx.client.update_file(
            file_id=f.id,
            path=str(ctx.data("dummy.txt")),
        )

    @ctx.step("list_files — paginate file results")
    def page():
        return ctx.client.list_files(size=10)

    @ctx.step("archive_file — soft-delete a file")
    def _():
        assert f, "depends on add_file"
        ctx.client.archive_file(f.id)

    @ctx.step("restore_file — un-archive a file")
    def _r():
        assert f, "depends on add_file"
        ctx.client.restore_file(f.id)
