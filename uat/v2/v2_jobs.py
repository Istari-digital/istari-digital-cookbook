"""v2 Jobs — submit, get, list, archive/restore.

Uses sample.xlsx with the @istari:extract / open_spreadsheet function, which
is a basic Istari module available on all environments.
"""

from uat.common import TestContext


def run(ctx: TestContext) -> None:

    @ctx.step("list_functions — discover available job functions")
    def functions_page():
        return ctx.client.list_functions(size=10)

    @ctx.step("list_jobs — paginate job results")
    def jobs_page():
        return ctx.client.list_jobs(size=10)

    # Search specifically for @istari:extract — basic Istari module that should
    # exist on all environments. Use name filter to avoid paginating through all.
    has_extract = False
    extract_page = ctx.client.list_functions(name="@istari:extract", size=1)
    if extract_page and getattr(extract_page, "items", None):
        has_extract = len(extract_page.items) > 0

    if not has_extract:
        ctx._log.warning(
            "⚠️  WARNING: @istari:extract / open_spreadsheet not found on this environment. "
            "Job endpoint tests are SKIPPED. This must be resolved before jobs can be validated."
        )
        for step in ("upload_model (xlsx)", "add_job", "get_job",
                     "list_model_jobs", "archive_job", "restore_job"):
            ctx.skip(step, "@istari:extract not available — job tests cannot run")
        return

    # Upload the xlsx as a model specifically for job submission
    @ctx.step("upload_model (xlsx) — upload a spreadsheet to run extraction against")
    def xlsx_model():
        return ctx.platform.upload_model(
            ctx.data("sample.xlsx"),
            external_id=f"{ctx.run_id}-v2-xlsx",
            display_name="UAT v2 Job Model (xlsx)",
        )

    if xlsx_model:
        ctx.register("model", xlsx_model, share_key="job_model")

    @ctx.step("add_job — submit @istari:extract / open_spreadsheet against the xlsx model")
    def job():
        assert xlsx_model, "depends on xlsx model upload"
        return ctx.client.add_job(
            model_id=xlsx_model.id,
            function="@istari:extract",
            tool_name="open_spreadsheet",
        )

    if job:
        ctx.register("job", job)

    @ctx.step("get_job — fetch job by id and verify it entered a valid state")
    def fetched():
        assert job, "depends on add_job"
        j = ctx.client.get_job(job.id)
        status = j.status.name.value if j.status and j.status.name else str(j.status)
        assert status in {"Created", "Pending", "Claimed", "Validating", "Running",
                          "Uploading", "Completed", "Failed", "Canceled", "Unknown"}, \
            f"unexpected job status: {status}"
        return j

    @ctx.step("list_model_jobs — list jobs scoped to the xlsx model")
    def model_jobs():
        assert xlsx_model, "depends on xlsx model upload"
        return ctx.client.list_model_jobs(xlsx_model.id, size=10)

    @ctx.step("archive_job — soft-delete a job")
    def _():
        assert job, "depends on add_job"
        ctx.client.archive_job(job.id)

    @ctx.step("restore_job — un-archive a job")
    def _r():
        assert job, "depends on add_job"
        ctx.client.restore_job(job.id)
