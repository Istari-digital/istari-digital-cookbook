"""v2 suites — one section per topic of the v2 client reference page, in the
order documented at docs.istaridigital.com/developers/SDK/api_reference/client/.
"""

from uat.common import TestContext


# ── Files, models & artifacts ────────────────────────────────────────────────

def files(ctx: TestContext) -> None:
    """Standalone file upload, revision, list, archive/restore."""

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


def models(ctx: TestContext) -> None:
    """Upload, fetch, revision, list, archive/restore."""

    @ctx.step("upload_model — create a new model from a local file")
    def model():
        return ctx.platform.upload_model(
            ctx.data("dummy.txt"),
            external_id=f"{ctx.run_id}-v2-model",
            display_name="UAT v2 Model",
        )

    if model:
        ctx.register("model", model)

    @ctx.step("get_model — fetch a model by id")
    def fetched():
        assert model, "depends on upload_model"
        got = ctx.platform.get_model(model.id)
        assert got.id == model.id, "get_model returned wrong id"
        return got

    @ctx.step("update_model — add a new revision to an existing model")
    def updated():
        assert model, "depends on upload_model"
        return ctx.client.update_model(
            model_id=model.id,
            path=str(ctx.data("dummy.txt")),
            display_name="UAT v2 Model (rev 2)",
        )

    @ctx.step("list_models — paginate model results")
    def page():
        return ctx.client.list_models(size=10)

    @ctx.step("archive_model — soft-delete a model")
    def _archived():
        assert model, "depends on upload_model"
        ctx.client.archive_model(model.id)

    @ctx.step("restore_model — un-archive a model")
    def _restored():
        assert model, "depends on upload_model"
        ctx.client.restore_model(model.id)


def artifacts(ctx: TestContext) -> None:
    """Create from a model, fetch, list, archive/restore."""
    model = ctx.shared.get("model")
    if not model:
        ctx.skip("add_artifact", "no model in ctx — run v2_models first")
        return

    @ctx.step("add_artifact — create an artifact from a model file")
    def artifact():
        return ctx.client.add_artifact(
            model_id=model.id,
            path=str(ctx.data("dummy.txt")),
            display_name="UAT v2 Artifact",
        )

    if artifact:
        ctx.register("artifact", artifact)

    @ctx.step("get_artifact — fetch artifact by id")
    def fetched():
        assert artifact, "depends on add_artifact"
        return ctx.client.get_artifact(artifact.id)

    @ctx.step("list_artifacts — paginate artifact results")
    def page():
        return ctx.client.list_artifacts(size=10)

    @ctx.step("archive_artifact — soft-delete an artifact")
    def _():
        assert artifact, "depends on add_artifact"
        ctx.client.archive_artifact(artifact.id)

    @ctx.step("restore_artifact — un-archive an artifact")
    def _r():
        assert artifact, "depends on add_artifact"
        ctx.client.restore_artifact(artifact.id)


def revisions(ctx: TestContext) -> None:
    """Fetch revisions, copy and transfer between resources."""
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


# ── Jobs ─────────────────────────────────────────────────────────────────────

def jobs(ctx: TestContext) -> None:
    """Submit, get, list, archive/restore.

    Uses sample.xlsx with the @istari:extract / open_spreadsheet function, which
    is a basic Istari module available on all environments.
    """

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


# ── Systems & snapshots ──────────────────────────────────────────────────────

def systems(ctx: TestContext) -> None:
    """Create, configure, add tracked files, baseline, archive/restore."""
    model = ctx.shared.get("model")

    @ctx.step("create_system — create a new engineering system")
    def system():
        from istari_digital_client.v2.models.new_system import NewSystem
        return ctx.client.create_system(
            NewSystem(
                name=f"UAT v2 System {ctx.run_id}",
                description="Created by UAT runner",
            )
        )

    if system:
        ctx.register("system", system)

    @ctx.step("get_system — fetch system by id")
    def fetched():
        assert system, "depends on create_system"
        return ctx.client.get_system(system.id)

    @ctx.step("list_systems — paginate system results")
    def page():
        return ctx.client.list_systems(size=10)

    if not model:
        ctx.skip("create_configuration", "no model in ctx — skipping tracked-file steps")
        ctx.skip("list_configurations", "no model in ctx")
        ctx.skip("get_system_baseline", "no model in ctx")
    else:
        @ctx.step("create_configuration — add a configuration with a tracked model")
        def config():
            assert system, "depends on create_system"
            from istari_digital_client.v2.models.new_system_configuration import NewSystemConfiguration
            from istari_digital_client.v2.models.new_tracked_file import NewTrackedFile
            from istari_digital_client.v2.models.tracked_file_specifier_type import TrackedFileSpecifierType
            return ctx.client.create_configuration(
                system_id=system.id,
                new_system_configuration=NewSystemConfiguration(
                    name="uat-config-v1",
                    tracked_files=[
                        NewTrackedFile(
                            specifier_type=TrackedFileSpecifierType.LATEST,
                            file_id=model.file_id,
                        )
                    ],
                ),
            )

        if config:
            ctx.register("configuration", config)

        @ctx.step("list_configurations — list configurations on a system")
        def configs_page():
            assert system, "depends on create_system"
            return ctx.client.list_system_configurations(system.id, size=10)

        @ctx.step("get_system_baseline — fetch the baseline snapshot tag")
        def baseline():
            assert system, "depends on create_system"
            return ctx.client.get_system_baseline(system.id)

    @ctx.step("update_system — rename a system")
    def updated():
        assert system, "depends on create_system"
        from istari_digital_client.v2.models.update_system import UpdateSystem
        return ctx.client.update_system(
            system.id,
            UpdateSystem(name=f"UAT v2 System {ctx.run_id}", description="Updated by UAT runner"),
        )

    @ctx.step("archive_system — soft-delete a system")
    def _():
        assert system, "depends on create_system"
        ctx.client.archive_system(system.id)

    @ctx.step("restore_system — un-archive a system")
    def _r():
        assert system, "depends on create_system"
        ctx.client.restore_system(system.id)


def snapshots(ctx: TestContext) -> None:
    """Create, fetch, compare, list items, snapshot tags."""
    system = ctx.shared.get("system")
    config = ctx.shared.get("configuration")

    if not system or not config:
        ctx.skip("create_snapshot", "no system/configuration in ctx — run v2_systems first")
        ctx.skip("get_snapshot", "no system in ctx")
        ctx.skip("list_snapshots", "no system in ctx")
        ctx.skip("list_snapshot_items", "no system in ctx")
        return

    @ctx.step("create_snapshot — capture a point-in-time snapshot of a configuration")
    def snapshot_response():
        from istari_digital_client.v2.models.new_snapshot import NewSnapshot
        return ctx.client.create_snapshot(
            configuration_id=config.id,
            new_snapshot=NewSnapshot(),
        )

    # ResponseCreateSnapshot wraps a union (Snapshot | DryRunSnapshot | NoOpResponse)
    snapshot_id = getattr(getattr(snapshot_response, "actual_instance", None), "id", None)

    @ctx.step("get_snapshot — fetch snapshot by id")
    def fetched():
        assert snapshot_id, "depends on create_snapshot returning a real Snapshot"
        return ctx.client.get_snapshot(snapshot_id)

    @ctx.step("list_snapshots — list snapshots for a system")
    def page():
        return ctx.client.list_snapshots(system_id=system.id, size=10)

    @ctx.step("list_snapshot_items — list resources captured in a snapshot")
    def items():
        assert snapshot_id, "depends on create_snapshot"
        return ctx.client.list_snapshot_items(snapshot_id, size=10)

    @ctx.step("list_snapshot_subsystems — list subsystems in a snapshot")
    def subsystems():
        assert snapshot_id, "depends on create_snapshot"
        return ctx.client.list_snapshot_subsystems(snapshot_id, size=10)


# ── Access control ───────────────────────────────────────────────────────────

def access(ctx: TestContext) -> None:
    """List and inspect permissions on a resource."""
    model = ctx.shared.get("model")
    if not model:
        ctx.skip("list_model_access", "no model in ctx — run v2_models first")
        return

    @ctx.step("list_model_access — list who has access to a model")
    def access_page():
        return ctx.client.list_model_access(model.id)

    @ctx.step("list_model_access — verify access list returns successfully")
    def verified():
        assert access_page is not None, "depends on list_model_access"
        # list_model_access returns List[AccessRelationship] directly
        assert isinstance(access_page, list)
        return access_page


def control_tags(ctx: TestContext) -> None:
    """Create tag, apply to model, inspect tagging history."""
    model = ctx.shared.get("model")

    @ctx.step("create_control_tag — create a new control tag")
    def tag():
        from istari_digital_client.v2.models.new_control_tag import NewControlTag
        return ctx.client.create_control_tag(
            NewControlTag(name=f"uat-tag-{ctx.run_id}", description="UAT control tag")
        )

    if tag:
        ctx.shared["control_tag"] = tag

    @ctx.step("list_control_tags — list all control tags")
    def page():
        return ctx.client.list_control_tags()

    @ctx.step("get_control_tag — fetch a control tag by id")
    def fetched():
        assert tag, "depends on create_control_tag"
        return ctx.client.get_control_tag(tag.id)

    if not model:
        ctx.skip("add_model_control_taggings", "no model in ctx — run v2_models first")
        ctx.skip("get_model_control_tags", "no model in ctx")
        ctx.skip("get_model_control_tagging_history", "no model in ctx")
        ctx.skip("remove_model_control_taggings", "no model in ctx")
        return

    @ctx.step("add_model_control_taggings — apply a control tag to a model")
    def tagging():
        assert tag, "depends on create_control_tag"
        return ctx.client.add_model_control_taggings(model.id, [tag.id])

    @ctx.step("get_model_control_tags — inspect control tags on a model")
    def model_tags():
        return ctx.client.get_model_control_tags(model.id)

    @ctx.step("get_model_control_tagging_history — audit history for model tags")
    def history():
        return ctx.client.get_model_control_tagging_history(model.id)

    @ctx.step("remove_model_control_taggings — remove a control tag from a model")
    def _():
        assert tag, "depends on create_control_tag"
        ctx.client.remove_model_control_taggings(model.id, [tag.id])


# ── Documents ────────────────────────────────────────────────────────────────

def documents(ctx: TestContext) -> None:
    """Create, fetch, update, list, archive."""
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


# ── Agents, modules & tools ──────────────────────────────────────────────────

def agents(ctx: TestContext) -> None:
    """List agents, pools, and status history (read-only)."""

    @ctx.step("list_agents — paginate registered agents")
    def agents_page():
        return ctx.client.list_agents(size=10)

    @ctx.step("list_agent_pools — paginate agent pools")
    def pools_page():
        return ctx.client.list_agent_pools(size=10)

    # If at least one agent exists, fetch its status history
    agent = None
    if agents_page and agents_page.items:
        agent = agents_page.items[0]

    if not agent:
        ctx.skip("get_agent", "no agents registered on platform")
        ctx.skip("list_agent_status_history", "no agents registered on platform")
        return

    @ctx.step("get_agent — fetch an agent by id")
    def fetched():
        return ctx.client.get_agent(agent.id)

    @ctx.step("list_agent_status_history — fetch status history for an agent")
    def status_history():
        return ctx.client.list_agent_status_history(agent.id, size=10)


def tools(ctx: TestContext) -> None:
    """List tools, functions, modules (read-only)."""

    @ctx.step("list_functions — list available job functions")
    def functions_page():
        return ctx.client.list_functions(size=20)

    @ctx.step("list_tools — list registered tools")
    def tools_page():
        return ctx.client.list_tools(size=20)

    @ctx.step("list_tool_versions — list all tool versions")
    def tool_versions():
        return ctx.client.list_tool_versions(size=20)

    @ctx.step("list_modules — list available modules")
    def modules_page():
        return ctx.client.list_modules(size=20)

    # Fetch function detail using its UUID id (not name)
    fn = None
    if functions_page and getattr(functions_page, "items", None):
        fn = functions_page.items[0]

    if fn:
        fn_id = getattr(fn, "id", None)
        if fn_id:
            @ctx.step("get_function — fetch a function by id")
            def function_detail():
                return ctx.client.get_function(fn_id)


# ── Users, tokens & admin ────────────────────────────────────────────────────

def users(ctx: TestContext) -> None:
    """Current user, list users, personal access tokens."""

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
