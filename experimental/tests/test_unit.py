"""Unit tests for istari_experimental views -- no live API required."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from istari_digital_client import JobStatusName
from istari_experimental.istari_utils import (
    BranchView,
    IstariPlatform,
    JobDefinition,
    JobView,
    ModelView,
    ProductView,
    ResourceView,
    SystemView,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_job(status: JobStatusName = JobStatusName.COMPLETED) -> MagicMock:
    job = MagicMock()
    job.id = "job-1"
    job.status.name = status
    job.status.message = None
    history_entry = MagicMock()
    history_entry.name.value = status.value
    job.status_history = [history_entry]
    job.function.name = "@test:fn"
    job.created = None
    job.file = None
    return job


def _make_model(model_id: str = "model-1", display_name: str = "My Model") -> MagicMock:
    model = MagicMock()
    model.id = model_id
    model.file.id = f"file-{model_id}"
    rev = MagicMock()
    rev.display_name = display_name
    rev.name = f"{display_name}.mdzip"
    rev.id = f"rev-{model_id}"
    model.file.revisions = [rev]
    model.artifacts = []
    return model


def _make_product(
    *,
    revision_id: str = "rev-1",
    file_id: str = "file-1",
    resource_type: str = "Artifact",
    resource_id: str = "art-1",
    name: str = "output.json",
    mime: str = "application/json",
    content: bytes = b"{}",
) -> tuple[MagicMock, MagicMock]:
    """Build a (Product, Client) pair where the client resolves the revision."""
    product = MagicMock()
    product.revision_id = revision_id
    product.file_id = file_id
    product.resource_type = resource_type
    product.resource_id = resource_id
    product.relationship_identifier = None

    rev = MagicMock()
    rev.id = revision_id
    rev.file_id = file_id
    rev.display_name = name
    rev.name = name
    rev.mime = mime
    rev.suffix = ""
    rev.content_token = f"token-{revision_id}"

    client = MagicMock()
    client.get_revision.return_value = rev
    client.read_contents.return_value = content
    return product, client


def _make_job_with_products(
    job_id: str,
    products: list[MagicMock],
    status: JobStatusName = JobStatusName.COMPLETED,
) -> MagicMock:
    """Build a Job mock whose latest revision lists the given products."""
    job = _make_job(status)
    job.id = job_id
    rev = MagicMock()
    rev.products = products
    job.file = MagicMock()
    job.file.revisions = [rev]
    return job


# ---------------------------------------------------------------------------
# JobView
# ---------------------------------------------------------------------------

class TestJobView:
    def test_status_reads_latest_history(self):
        jv = JobView(_job=_make_job(JobStatusName.COMPLETED), _client=MagicMock())
        assert jv.status == JobStatusName.COMPLETED.value

    def test_completed_true_when_done(self):
        jv = JobView(_job=_make_job(JobStatusName.COMPLETED), _client=MagicMock())
        assert jv.completed is True

    def test_failed_true_when_failed(self):
        jv = JobView(_job=_make_job(JobStatusName.FAILED), _client=MagicMock())
        assert jv.failed is True

    def test_on_success_returns_self_when_completed(self):
        jv = JobView(_job=_make_job(JobStatusName.COMPLETED), _client=MagicMock())
        assert jv.on_success() is jv

    def test_on_success_raises_when_failed(self):
        jv = JobView(_job=_make_job(JobStatusName.FAILED), _client=MagicMock())
        with pytest.raises(RuntimeError, match="did not complete"):
            jv.on_success()

    @patch("istari_experimental.istari_utils.time.sleep")
    def test_wait_returns_self_immediately_when_already_complete(self, _sleep):
        mock_client = MagicMock()
        mock_client.get_job.return_value = _make_job(JobStatusName.COMPLETED)
        jv = JobView(_job=_make_job(JobStatusName.PENDING), _client=mock_client)
        result = jv.wait(timeout=10, poll_interval=1)
        assert result is jv
        _sleep.assert_not_called()

    def test_get_products_reads_jobs_revision_products(self):
        product, client = _make_product(name="report.json")
        mock_job = _make_job_with_products("job-99", [product])
        client.get_job.return_value = mock_job

        jv = JobView(_job=mock_job, _client=client)
        products = jv.get_products()
        assert len(products) == 1
        assert products[0].name == "report.json"
        assert isinstance(products[0], ProductView)

    def test_get_products_returns_empty_when_revision_has_no_products(self):
        mock_job = _make_job_with_products("job-99", [])
        client = MagicMock()
        client.get_job.return_value = mock_job

        jv = JobView(_job=mock_job, _client=client)
        assert jv.get_products() == []

    def test_get_products_filters_by_resource_type(self):
        artifact_p, client = _make_product(name="a.json", resource_type="Artifact")
        model_p, _ = _make_product(name="m.json", resource_type="Model")
        mock_job = _make_job_with_products("job-99", [artifact_p, model_p])
        client.get_job.return_value = mock_job

        jv = JobView(_job=mock_job, _client=client)
        artifacts = jv.get_products(resource_type="Artifact")
        assert len(artifacts) == 1
        assert artifacts[0].resource_type == "Artifact"

    def test_find_product_by_name(self):
        product, client = _make_product(name="report.json")
        mock_job = _make_job_with_products("job-99", [product])
        client.get_job.return_value = mock_job

        jv = JobView(_job=mock_job, _client=client)
        assert jv.find_product(name="report.json") is not None
        assert jv.find_product(name="missing.json") is None


# ---------------------------------------------------------------------------
# ModelView
# ---------------------------------------------------------------------------

class TestModelView:
    def test_name_from_latest_revision(self):
        mv = ModelView(_model=_make_model(display_name="My SysML Model"), _client=MagicMock())
        assert mv.name == "My SysML Model"

    def test_submit_job_returns_job_view(self):
        mock_client = MagicMock()
        mock_client.add_job.return_value = _make_job()
        mv = ModelView(_model=_make_model(), _client=mock_client)
        jv = mv.submit_job(JobDefinition(function="@test:fn", tool_name="tool"))
        assert isinstance(jv, JobView)
        mock_client.add_job.assert_called_once()

    def test_submit_job_passes_correct_parameters(self):
        mock_client = MagicMock()
        mock_client.add_job.return_value = _make_job()
        mv = ModelView(_model=_make_model("model-42"), _client=mock_client)
        mv.submit_job(JobDefinition(
            function="@sysml:extract",
            tool_name="cameo",
            operating_system="RHEL 8",
        ))
        kwargs = mock_client.add_job.call_args.kwargs
        assert kwargs["model_id"] == "model-42"
        assert kwargs["function"] == "@sysml:extract"
        assert kwargs["tool_name"] == "cameo"
        assert kwargs["operating_system"] == "RHEL 8"

    @patch("istari_experimental.istari_utils.time.sleep")
    def test_run_job_submits_waits_and_returns_completed_job(self, _sleep):
        mock_client = MagicMock()
        mock_client.add_job.return_value = _make_job(JobStatusName.PENDING)
        mock_client.get_job.return_value = _make_job(JobStatusName.COMPLETED)
        mv = ModelView(_model=_make_model(), _client=mock_client)
        jv = mv.run_job(JobDefinition(function="@test:fn", tool_name="tool"), timeout=30)
        assert jv.completed is True


# ---------------------------------------------------------------------------
# ProductView
# ---------------------------------------------------------------------------

class TestProductView:
    def test_name_from_revision_display_name(self):
        product, client = _make_product(name="results.json")
        pv = ProductView(_product=product, _client=client)
        assert pv.name == "results.json"

    def test_revision_is_lazy_loaded_and_cached(self):
        product, client = _make_product()
        pv = ProductView(_product=product, _client=client)
        _ = pv.name
        _ = pv.mime
        _ = pv.filename
        assert client.get_revision.call_count == 1

    def test_read_bytes(self):
        product, client = _make_product(content=b"\x00\x01\x02")
        pv = ProductView(_product=product, _client=client)
        assert pv.read_bytes() == b"\x00\x01\x02"

    def test_read_text(self):
        product, client = _make_product(content=b'{"ok": true}')
        pv = ProductView(_product=product, _client=client)
        assert pv.read_text() == '{"ok": true}'

    def test_download_writes_to_explicit_path(self, tmp_path):
        product, client = _make_product(content=b"file content")
        pv = ProductView(_product=product, _client=client)
        dest = tmp_path / "result.json"
        pv.download(dest)
        assert dest.read_bytes() == b"file content"

    def test_download_to_directory_uses_product_filename(self, tmp_path):
        product, client = _make_product(name="output.json", content=b"data")
        pv = ProductView(_product=product, _client=client)
        result = pv.download(tmp_path)
        assert result.name == "output.json"
        assert result.read_bytes() == b"data"

    def test_resource_returns_resource_view(self):
        product, client = _make_product(resource_type="Artifact", resource_id="art-1")
        underlying = MagicMock()
        underlying.id = "art-1"
        client.get_resource.return_value = underlying

        pv = ProductView(_product=product, _client=client)
        rv = pv.resource
        assert isinstance(rv, ResourceView)
        assert rv.id == "art-1"
        client.get_resource.assert_called_once_with("Artifact", "art-1")


# ---------------------------------------------------------------------------
# IstariPlatform
# ---------------------------------------------------------------------------

class TestIstariPlatform:
    def test_upload_model_returns_model_view(self, tmp_path):
        f = tmp_path / "model.json"
        f.write_text("{}")
        mock_client = MagicMock()
        mock_client.add_model.return_value = _make_model("new-model")
        platform = IstariPlatform(mock_client)
        mv = platform.upload_model(f, external_id="ext-1")
        assert isinstance(mv, ModelView)
        mock_client.add_model.assert_called_once()

    def test_upload_model_raises_for_missing_file(self):
        platform = IstariPlatform(MagicMock())
        with pytest.raises(FileNotFoundError):
            platform.upload_model("/nonexistent/model.mdzip", external_id="ext-1")

    def test_find_model_by_name_returns_model_view(self):
        mock_candidate = MagicMock()
        mock_candidate.id = "m-1"
        mock_candidate.name = "Target Model"
        mock_candidate.display_name = None
        mock_candidate.file = None
        mock_page = MagicMock()
        mock_page.iter_items.return_value = [mock_candidate]
        mock_client = MagicMock()
        mock_client.list_models.return_value = mock_page
        mock_client.get_model.return_value = _make_model("m-1", "Target Model")

        platform = IstariPlatform(mock_client)
        result = platform.find_model(name="Target Model")
        assert result is not None
        assert result.id == "m-1"

    def test_find_model_returns_none_when_not_found(self):
        mock_page = MagicMock()
        mock_page.iter_items.return_value = []
        mock_client = MagicMock()
        mock_client.list_models.return_value = mock_page
        assert IstariPlatform(mock_client).find_model(name="Ghost") is None


# ---------------------------------------------------------------------------
# Branch helpers / fixtures
# ---------------------------------------------------------------------------

def _make_tag(tag_id: str, name: str, snapshot_id: str = "snap-1") -> MagicMock:
    t = MagicMock()
    t.id = tag_id
    t.tag = name
    t.snapshot_id = snapshot_id
    t.is_baseline = False
    return t


def _make_tracked_file(
    tf_id: str = "tf-1",
    file_id: str = "file-1",
    resource_id: str = "model-1",
    pinned_revision_id: str | None = "rev-1",
    specifier_type=None,
) -> MagicMock:
    from istari_digital_client.v2.models import TrackedFileSpecifierType
    tf = MagicMock()
    tf.id = tf_id
    tf.file_id = file_id
    tf.resource_id = resource_id
    tf.pinned_file_revision_id = pinned_revision_id
    tf.current_file_revision_id = pinned_revision_id
    tf.specifier_type = specifier_type or TrackedFileSpecifierType.LATEST
    return tf


def _make_page(items: list) -> MagicMock:
    p = MagicMock()
    p.items = items
    p.pages = 1
    p.iter_items.return_value = items
    return p


def _make_system_for_branch(system_id: str = "sys-1", name: str = "MySys") -> MagicMock:
    sys = MagicMock()
    sys.id = system_id
    sys.name = name
    sys.configurations = []
    sys.baseline_tagged_snapshot_id = "snap-baseline"
    return sys


# ---------------------------------------------------------------------------
# SystemView -- branches
# ---------------------------------------------------------------------------

class TestSystemViewBranches:
    def test_list_branches_filters_by_system_via_sdk(self):
        client = MagicMock()
        client.list_tags.return_value = _make_page([_make_tag("t1", "main"), _make_tag("t2", "feat")])
        sv = SystemView(_system=_make_system_for_branch(), _client=client)

        branches = sv.list_branches()
        assert [b.name for b in branches] == ["main", "feat"]
        client.list_tags.assert_called_with(page=1, size=100, system_id="sys-1")

    def test_get_branch_returns_matching_branch(self):
        client = MagicMock()
        client.list_tags.return_value = _make_page([_make_tag("t1", "main"), _make_tag("t2", "feat")])
        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        assert sv.get_branch("feat").id == "t2"

    def test_get_branch_raises_when_missing(self):
        client = MagicMock()
        client.list_tags.return_value = _make_page([])
        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        with pytest.raises(ValueError, match="Branch 'main' not found"):
            sv.get_branch("main")

    def test_create_branch_copies_tracked_items_and_advances_tag(self):
        client = MagicMock()
        # Source = an existing branch named "main"
        client.list_tags.return_value = _make_page([_make_tag("t-main", "main", snapshot_id="snap-base")])
        # Source snapshot -> source config
        src_snap = MagicMock(); src_snap.id = "snap-base"; src_snap.configuration_id = "cfg-base"
        client.get_snapshot.return_value = src_snap
        client.get_configuration.return_value = MagicMock(id="cfg-base", name="branch:main")
        # Source has 1 tracked file, 0 subsystems
        client.list_tracked_files.return_value = _make_page([_make_tracked_file()])
        client.list_configuration_subsystems.return_value = _make_page([])
        # New config + snapshot + tag
        new_cfg = MagicMock(id="cfg-new", name="branch:feature")
        client.create_configuration.return_value = new_cfg
        snap_resp = MagicMock(); snap_resp.actual_instance = MagicMock(id="snap-new")
        client.create_snapshot.return_value = snap_resp
        new_tag = _make_tag("tag-new", "feature", snapshot_id="snap-new")
        client.create_tag.return_value = new_tag

        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        branch = sv.create_branch("feature", from_branch="main")

        assert isinstance(branch, BranchView)
        assert branch.name == "feature"
        assert branch.snapshot_id == "snap-new"
        # New config carried over the tracked file
        cfg_kwargs = client.create_configuration.call_args.kwargs
        assert cfg_kwargs["system_id"] == "sys-1"
        assert cfg_kwargs["new_system_configuration"].name == "branch:feature"
        assert len(cfg_kwargs["new_system_configuration"].tracked_files) == 1
        # Snapshot and tag were created against the new config
        client.create_snapshot.assert_called_once()
        client.create_tag.assert_called_once()
        # We do NOT touch baseline anymore -- forks are explicit via from_branch.
        client.get_system_baseline.assert_not_called()

    def test_create_branch_rejects_duplicate_name(self):
        client = MagicMock()
        client.list_tags.return_value = _make_page([_make_tag("t1", "main")])
        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        with pytest.raises(ValueError, match="already exists"):
            sv.create_branch("main")

    def test_create_branch_skips_archived_tracked_files(self):
        """Platform refuses 'Archived files cannot be added to a configuration.'"""
        client = MagicMock()
        client.list_tags.return_value = _make_page([_make_tag("t-main", "main", snapshot_id="snap-base")])
        src_snap = MagicMock(); src_snap.id = "snap-base"; src_snap.configuration_id = "cfg-base"
        client.get_snapshot.return_value = src_snap
        client.get_configuration.return_value = MagicMock(id="cfg-base")

        active_tf = _make_tracked_file(tf_id="tf-active", file_id="f-active")
        active_tf.archive_status = "Active"
        archived_tf = _make_tracked_file(tf_id="tf-archived", file_id="f-archived")
        archived_tf.archive_status = "Archived"
        client.list_tracked_files.return_value = _make_page([active_tf, archived_tf])
        client.list_configuration_subsystems.return_value = _make_page([])

        # Underlying File lookup -- both reported active so verify-files passes
        active_file = MagicMock(archive_status="Active", is_archived=False)
        client.get_file.return_value = active_file

        client.create_configuration.return_value = MagicMock(id="cfg-new")
        snap_resp = MagicMock(); snap_resp.actual_instance = MagicMock(id="snap-new")
        client.create_snapshot.return_value = snap_resp
        client.create_tag.return_value = _make_tag("tag-new", "feature", snapshot_id="snap-new")

        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        sv.create_branch("feature", from_branch="main")

        new_cfg = client.create_configuration.call_args.kwargs["new_system_configuration"]
        assert [tf.file_id for tf in new_cfg.tracked_files] == ["f-active"]

    def test_active_tracked_files_can_verify_underlying_file_when_asked(self):
        """Opt-in deeper verification: drop tracked files whose File is archived."""
        from istari_experimental.istari_utils import _active_tracked_files

        tf_a = _make_tracked_file(tf_id="tf-a", file_id="f-good")
        tf_a.archive_status = "Active"
        tf_b = _make_tracked_file(tf_id="tf-b", file_id="f-bad")
        tf_b.archive_status = "Active"

        good_file = MagicMock(archive_status="Active", is_archived=False)
        bad_file = MagicMock(archive_status="Archived", is_archived=True)

        client = MagicMock()
        client.get_file.side_effect = lambda fid: good_file if fid == "f-good" else bad_file

        kept = _active_tracked_files(client, [tf_a, tf_b], verify_files=True)
        assert [tf.file_id for tf in kept] == ["f-good"]

    def test_create_branch_raises_clear_error_when_source_has_only_archived_items(self):
        """Better error than 'Configuration must have at least one tracked file or tracked system.'"""
        client = MagicMock()
        client.list_tags.return_value = _make_page([_make_tag("t-main", "main", snapshot_id="snap-base")])
        src_snap = MagicMock(); src_snap.id = "snap-base"; src_snap.configuration_id = "cfg-base"
        client.get_snapshot.return_value = src_snap
        client.get_configuration.return_value = MagicMock(id="cfg-base")

        archived_tf = _make_tracked_file(tf_id="tf-x", file_id="f-x")
        archived_tf.archive_status = "Archived"
        client.list_tracked_files.return_value = _make_page([archived_tf])
        client.list_configuration_subsystems.return_value = _make_page([])

        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        with pytest.raises(ValueError, match=r"no active tracked files or subsystems.*1 were skipped as archived"):
            sv.create_branch("feature", from_branch="main")
        client.create_configuration.assert_not_called()

    def test_create_snapshot_falls_back_to_existing_on_noop(self):
        """If platform returns NoOp ('no change'), use the existing snapshot."""
        from istari_experimental.istari_utils import _create_snapshot

        class FakeNoOp:
            status = "no-op"
            message = "No change from the last snapshot."

        existing = MagicMock(id="snap-existing")
        client = MagicMock()
        client.create_snapshot.return_value = MagicMock(actual_instance=FakeNoOp())
        client.list_snapshots.return_value = _make_page([existing])

        snap = _create_snapshot(client, "cfg-1")
        assert snap is existing
        client.list_snapshots.assert_called_once()
        kwargs = client.list_snapshots.call_args.kwargs
        assert kwargs["configuration_id"] == "cfg-1"

    def test_create_branch_resolves_string_as_model_id_first(self):
        """A string seed should be resolved as a Model id first; falls back to file_id."""
        client = MagicMock()
        client.list_tags.return_value = _make_page([])
        # Pretend the id is a known Model on the platform
        model = MagicMock(id="m-123")
        model.file = MagicMock(id="file-from-model-123")
        client.get_model.return_value = model
        client.create_configuration.return_value = MagicMock(id="cfg-new")
        snap_resp = MagicMock(); snap_resp.actual_instance = MagicMock(id="snap-new")
        client.create_snapshot.return_value = snap_resp
        client.create_tag.return_value = _make_tag("tag-new", "feature", snapshot_id="snap-new")

        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        sv.create_branch("feature", resources=["m-123"])

        client.get_system_baseline.assert_not_called()
        client.list_tracked_files.assert_not_called()
        client.get_model.assert_called_once_with("m-123")
        new_cfg = client.create_configuration.call_args.kwargs["new_system_configuration"]
        # tracked file should reference the Model's underlying file_id, so the
        # platform binds it back to the Model and it shows up as a Resource.
        assert [tf.file_id for tf in new_cfg.tracked_files] == ["file-from-model-123"]

    def test_create_branch_string_falls_back_to_file_id_on_model_miss(self):
        """If get_model raises NotFoundException, treat the string as a raw file_id."""
        from istari_digital_client.exceptions import NotFoundException
        client = MagicMock()
        client.list_tags.return_value = _make_page([])
        client.get_model.side_effect = NotFoundException(status=404, reason="Not Found")
        client.create_configuration.return_value = MagicMock(id="cfg-new")
        snap_resp = MagicMock(); snap_resp.actual_instance = MagicMock(id="snap-new")
        client.create_snapshot.return_value = snap_resp
        client.create_tag.return_value = _make_tag("tag-new", "feature", snapshot_id="snap-new")

        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        sv.create_branch("feature", resources=["raw-file-id"])

        client.get_model.assert_called_once_with("raw-file-id")
        new_cfg = client.create_configuration.call_args.kwargs["new_system_configuration"]
        assert [tf.file_id for tf in new_cfg.tracked_files] == ["raw-file-id"]

    def test_create_branch_auto_seeds_readme_when_no_args(self):
        """create_branch(name) with nothing else should auto-create a README.md."""
        client = MagicMock()
        client.list_tags.return_value = _make_page([])
        # Auto-README path: client.add_model is called with the temp file
        readme_model = MagicMock(id="m-readme")
        readme_model.file = MagicMock(id="file-readme")
        client.add_model.return_value = readme_model
        client.create_configuration.return_value = MagicMock(id="cfg-new")
        snap_resp = MagicMock(); snap_resp.actual_instance = MagicMock(id="snap-new")
        client.create_snapshot.return_value = snap_resp
        client.create_tag.return_value = _make_tag("tag-new", "spike", snapshot_id="snap-new")

        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        sv.create_branch("spike")

        # No baseline-fork attempted
        client.get_system_baseline.assert_not_called()
        client.list_tracked_files.assert_not_called()
        # README uploaded as a Model (not a bare File)
        client.add_model.assert_called_once()
        kwargs = client.add_model.call_args.kwargs
        assert kwargs["display_name"] == "README - spike"
        # The path that was uploaded should be a real file containing default text
        readme_path = kwargs["path"]
        body = Path(readme_path).read_text() if Path(readme_path).exists() else ""
        # If the helper unlinks before we can read, just check the call shape.
        if body:
            assert "spike" in body
        # Tracked file points at the uploaded model's file_id
        new_cfg = client.create_configuration.call_args.kwargs["new_system_configuration"]
        assert [tf.file_id for tf in new_cfg.tracked_files] == ["file-readme"]

    def test_create_branch_auto_readme_uses_description_text(self):
        client = MagicMock()
        client.list_tags.return_value = _make_page([])
        readme_model = MagicMock(id="m-readme")
        readme_model.file = MagicMock(id="file-readme")
        client.add_model.return_value = readme_model
        client.create_configuration.return_value = MagicMock(id="cfg-new")
        snap_resp = MagicMock(); snap_resp.actual_instance = MagicMock(id="snap-new")
        client.create_snapshot.return_value = snap_resp
        client.create_tag.return_value = _make_tag("tag-new", "feat", snapshot_id="snap-new")

        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        sv.create_branch("feat", description="Antenna redesign")

        kwargs = client.add_model.call_args.kwargs
        assert kwargs["description"] == "Antenna redesign"
        readme_path = kwargs["path"]
        if Path(readme_path).exists():
            body = Path(readme_path).read_text()
            assert "Antenna redesign" in body

    def test_create_branch_auto_readme_disabled_when_explicit_seeds(self):
        """If user supplies resources, no README should be generated."""
        client = MagicMock()
        client.list_tags.return_value = _make_page([])
        # No add_model expected for README (only for the seed itself, but the
        # seed is a string so it should go through get_model).
        from istari_digital_client.exceptions import NotFoundException
        client.get_model.side_effect = NotFoundException(status=404, reason="Not Found")
        client.create_configuration.return_value = MagicMock(id="cfg-new")
        snap_resp = MagicMock(); snap_resp.actual_instance = MagicMock(id="snap-new")
        client.create_snapshot.return_value = snap_resp
        client.create_tag.return_value = _make_tag("tag-new", "feat", snapshot_id="snap-new")

        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        sv.create_branch("feat", resources=["existing-file-id"])

        client.add_model.assert_not_called()  # no README upload
        new_cfg = client.create_configuration.call_args.kwargs["new_system_configuration"]
        assert [tf.file_id for tf in new_cfg.tracked_files] == ["existing-file-id"]

    def test_create_branch_uploads_paths_as_model(self, tmp_path):
        """Path-on-disk seed resources should be uploaded via client.add_model
        (not add_file) so the new tracked entry binds to a Resource."""
        client = MagicMock()
        client.list_tags.return_value = _make_page([])
        uploaded_model = MagicMock(id="m-uploaded-1")
        uploaded_model.file = MagicMock(id="file-from-uploaded-model")
        client.add_model.return_value = uploaded_model
        client.create_configuration.return_value = MagicMock(id="cfg-new")
        snap_resp = MagicMock(); snap_resp.actual_instance = MagicMock(id="snap-new")
        client.create_snapshot.return_value = snap_resp
        client.create_tag.return_value = _make_tag("tag-new", "feature", snapshot_id="snap-new")

        f = tmp_path / "seed.txt"
        f.write_text("hello")

        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        sv.create_branch("feature", resources=[f])

        client.add_model.assert_called_once_with(path=f)
        client.add_file.assert_not_called()
        new_cfg = client.create_configuration.call_args.kwargs["new_system_configuration"]
        assert [tf.file_id for tf in new_cfg.tracked_files] == ["file-from-uploaded-model"]

    def test_create_branch_combines_fork_with_seeds(self):
        """from_branch + resources => copy fork rows AND append seeds."""
        client = MagicMock()
        client.list_tags.return_value = _make_page([_make_tag("t-main", "main", snapshot_id="snap-main")])
        src_snap = MagicMock(id="snap-main", configuration_id="cfg-main")
        client.get_snapshot.return_value = src_snap
        client.get_configuration.return_value = MagicMock(id="cfg-main")
        active_tf = _make_tracked_file(tf_id="tf-1", file_id="f-from-main")
        active_tf.archive_status = "Active"
        client.list_tracked_files.return_value = _make_page([active_tf])
        client.list_configuration_subsystems.return_value = _make_page([])
        # The seed string is not a valid Model id, so the resolver falls back
        # to treating it as a raw file_id.
        from istari_digital_client.exceptions import NotFoundException
        client.get_model.side_effect = NotFoundException(status=404, reason="Not Found")
        client.create_configuration.return_value = MagicMock(id="cfg-new")
        snap_resp = MagicMock(); snap_resp.actual_instance = MagicMock(id="snap-new")
        client.create_snapshot.return_value = snap_resp
        client.create_tag.return_value = _make_tag("tag-new", "feature", snapshot_id="snap-new")

        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        sv.create_branch("feature", from_branch="main", resources=["seed-file-id"])

        new_cfg = client.create_configuration.call_args.kwargs["new_system_configuration"]
        assert [tf.file_id for tf in new_cfg.tracked_files] == ["f-from-main", "seed-file-id"]

    def test_merge_rejects_self_merge(self):
        client = MagicMock()
        client.list_tags.return_value = _make_page([_make_tag("t1", "main")])
        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        with pytest.raises(ValueError, match="must differ"):
            sv.merge(from_branch="main", to_branch="main")

    def test_merge_replaces_target_with_source_and_advances_pointer(self):
        client = MagicMock()
        # Two existing branches
        client.list_tags.return_value = _make_page([
            _make_tag("t-feat", "feat", snapshot_id="snap-feat"),
            _make_tag("t-main", "main", snapshot_id="snap-main"),
        ])

        # Each branch resolves its config; both list snapshots/configs distinctly
        def _get_snapshot(snap_id):
            s = MagicMock()
            s.id = snap_id
            s.configuration_id = f"cfg-of-{snap_id}"
            return s

        def _get_configuration(cfg_id):
            c = MagicMock(); c.id = cfg_id; c.name = cfg_id
            return c

        client.get_snapshot.side_effect = _get_snapshot
        client.get_configuration.side_effect = _get_configuration
        client.list_tracked_files.return_value = _make_page([_make_tracked_file(file_id="src-file")])
        client.list_configuration_subsystems.return_value = _make_page([])

        new_cfg = MagicMock(id="cfg-merge")
        client.create_configuration.return_value = new_cfg
        snap_resp = MagicMock(); snap_resp.actual_instance = MagicMock(id="snap-merged")
        client.create_snapshot.return_value = snap_resp
        client.update_tag.return_value = _make_tag("t-main", "main", snapshot_id="snap-merged")

        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        result = sv.merge(from_branch="feat", to_branch="main", message="merge")

        assert result.name == "main"
        assert result.snapshot_id == "snap-merged"
        # Tag pointer advanced to the new snapshot
        client.update_tag.assert_called_once()
        update_kwargs = client.update_tag.call_args.kwargs
        assert update_kwargs["tag_id"] == "t-main"
        assert update_kwargs["update_tag"].snapshot_id == "snap-merged"


# ---------------------------------------------------------------------------
# BranchView staging + commit
# ---------------------------------------------------------------------------

class TestBranchViewStaging:
    def _build(self, *, tracked: list | None = None, subsystems: list | None = None):
        client = MagicMock()
        client.list_tags.return_value = _make_page([])
        snap = MagicMock(id="snap-1"); snap.configuration_id = "cfg-1"
        client.get_snapshot.return_value = snap
        cfg = MagicMock(id="cfg-1", name="branch:main")
        client.get_configuration.return_value = cfg
        client.list_tracked_files.return_value = _make_page(tracked or [])
        client.list_configuration_subsystems.return_value = _make_page(subsystems or [])

        new_cfg = MagicMock(id="cfg-2")
        client.create_configuration.return_value = new_cfg
        snap_resp = MagicMock(); snap_resp.actual_instance = MagicMock(id="snap-2")
        client.create_snapshot.return_value = snap_resp
        client.update_tag.return_value = _make_tag("t-main", "main", snapshot_id="snap-2")

        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        branch = BranchView(_system=sv, _tag=_make_tag("t-main", "main"), _client=client)
        return branch, client

    def test_add_resources_is_chainable(self):
        branch, _ = self._build()
        result = branch.add_resources("m1").add_resources("m2")
        assert result is branch
        assert branch.has_pending_changes is True

    def test_remove_resources_cancels_pending_add(self):
        branch, _ = self._build()
        branch.add_resources("m1").remove_resources("m1")
        assert "m1" not in branch._pending_add_resources
        assert "m1" in branch._pending_remove_resources

    def test_commit_no_op_when_nothing_staged(self):
        branch, client = self._build()
        branch.commit()
        client.create_configuration.assert_not_called()
        client.create_snapshot.assert_not_called()

    def test_commit_creates_config_snapshot_and_advances_tag(self):
        existing = _make_tracked_file(tf_id="old", resource_id="m-old", file_id="f-old")
        branch, client = self._build(tracked=[existing])
        # Stage adding a new model and removing an old one
        new_model = MagicMock(); new_model.id = "m-new"; new_model.file = MagicMock(id="f-new")
        client.get_model.return_value = new_model

        branch.add_resources("m-new").remove_resources("m-old").commit("first commit")

        client.create_configuration.assert_called_once()
        cfg_kwargs = client.create_configuration.call_args.kwargs
        new_cfg = cfg_kwargs["new_system_configuration"]
        # Removed old, added new -> exactly one tracked file
        file_ids = [tf.file_id for tf in new_cfg.tracked_files]
        assert file_ids == ["f-new"]
        # Snapshot + tag pointer advanced
        client.create_snapshot.assert_called_once_with(
            configuration_id="cfg-2",
            new_snapshot=ANY,
        )
        client.update_tag.assert_called_once()
        # Pending cleared
        assert branch.has_pending_changes is False
        assert branch.snapshot_id == "snap-2"

    def test_add_revisions_accepts_strings_and_FileRevision_objects(self):
        from istari_digital_client.v2.models import FileRevision, TrackedFileSpecifierType
        branch, _ = self._build()
        rev_obj = MagicMock(spec=FileRevision)
        rev_obj.id = "rev-from-obj"
        branch.add_revisions("rev-from-str", rev_obj)
        assert branch._pending_add_revisions == ["rev-from-str", "rev-from-obj"]
        assert branch.has_pending_changes is True

    def test_commit_pins_added_revisions_LOCKED(self):
        from istari_digital_client.v2.models import TrackedFileSpecifierType
        branch, client = self._build()
        # pretend revision exists with file_id=f-from-rev
        rev = MagicMock(id="rev-1", file_id="f-from-rev")
        client.get_revision.return_value = rev

        branch.add_revisions("rev-1").commit()

        cfg_kwargs = client.create_configuration.call_args.kwargs
        new_cfg = cfg_kwargs["new_system_configuration"]
        assert len(new_cfg.tracked_files) == 1
        tf = new_cfg.tracked_files[0]
        assert tf.specifier_type == TrackedFileSpecifierType.LOCKED
        assert tf.file_id == "f-from-rev"
        assert tf.pinned_file_revision_id == "rev-1"
        assert branch.has_pending_changes is False

    def test_commit_raises_clear_error_when_revision_has_no_file(self):
        branch, client = self._build()
        rev = MagicMock(id="rev-orphan"); rev.file_id = None
        client.get_revision.return_value = rev
        branch.add_revisions("rev-orphan")
        with pytest.raises(ValueError, match="not attached to a file"):
            branch.commit()


class TestBranchHistory:
    @staticmethod
    def _make_revision(rev_id, snapshot_id, created, archive_status="Active"):
        from istari_digital_client.v2.models import SnapshotTagRevision
        rev = MagicMock(spec=SnapshotTagRevision)
        rev.id = rev_id
        rev.snapshot_id = snapshot_id
        rev.created = created
        rev.archive_status = archive_status
        rev.tag_id = "t-main"
        rev.created_by_id = "user-1"
        return rev

    def _branch(self, history):
        client = MagicMock()
        client.get_tag_history.return_value = history
        sv = SystemView(_system=_make_system_for_branch(), _client=client)
        branch = BranchView(_system=sv, _tag=_make_tag("t-main", "main"), _client=client)
        return branch, client

    def test_get_history_defaults_to_newest_first(self):
        from datetime import datetime, timezone
        early = self._make_revision("r-1", "snap-1", datetime(2026, 1, 1, tzinfo=timezone.utc))
        mid = self._make_revision("r-2", "snap-2", datetime(2026, 2, 1, tzinfo=timezone.utc))
        late = self._make_revision("r-3", "snap-3", datetime(2026, 3, 1, tzinfo=timezone.utc))
        # Platform may return in any order -- our wrapper sorts.
        branch, client = self._branch([mid, early, late])

        history = branch.get_history()

        client.get_tag_history.assert_called_once_with("t-main")
        assert [r.id for r in history] == ["r-3", "r-2", "r-1"]

    def test_get_history_chronological_when_newest_first_false(self):
        from datetime import datetime, timezone
        early = self._make_revision("r-1", "snap-1", datetime(2026, 1, 1, tzinfo=timezone.utc))
        late = self._make_revision("r-2", "snap-2", datetime(2026, 2, 1, tzinfo=timezone.utc))
        branch, _ = self._branch([late, early])

        history = branch.get_history(newest_first=False)
        assert [r.id for r in history] == ["r-1", "r-2"]

    def test_get_history_filters_archived_by_default(self):
        from datetime import datetime, timezone
        active = self._make_revision("r-active", "snap-A", datetime(2026, 2, 1, tzinfo=timezone.utc))
        archived = self._make_revision("r-archived", "snap-B", datetime(2026, 1, 1, tzinfo=timezone.utc), archive_status="Archived")
        branch, _ = self._branch([active, archived])

        history = branch.get_history()
        assert [r.id for r in history] == ["r-active"]

        history_all = branch.get_history(include_archived=True)
        assert sorted(r.id for r in history_all) == ["r-active", "r-archived"]

    def test_get_snapshot_at_resolves_by_revision_or_id(self):
        from datetime import datetime, timezone
        rev = self._make_revision("r-1", "snap-target", datetime(2026, 1, 1, tzinfo=timezone.utc))
        branch, client = self._branch([rev])
        client.get_snapshot.return_value = MagicMock(id="snap-target")

        out = branch.get_snapshot_at(rev)
        client.get_snapshot.assert_called_with("snap-target")
        assert out.id == "snap-target"

        client.get_snapshot.reset_mock()
        branch.get_snapshot_at("snap-other")
        client.get_snapshot.assert_called_with("snap-other")


class TestModelViewUploadRevision:
    def test_upload_revision_calls_update_model_and_returns_latest_rev(self, tmp_path):
        from istari_experimental import ModelView
        # Build a model fixture with one existing revision after update
        path = tmp_path / "spec.txt"
        path.write_text("hello")

        client = MagicMock()
        new_rev = MagicMock(id="rev-2", file_id="f-1", name="spec.txt")
        updated_model = MagicMock(id="m-1")
        updated_model.file = MagicMock(id="f-1", revisions=[MagicMock(id="rev-1"), new_rev])
        client.update_model.return_value = updated_model

        original_model = MagicMock(id="m-1")
        original_model.file = MagicMock(id="f-1", revisions=[MagicMock(id="rev-1")])
        mv = ModelView(_model=original_model, _client=client)

        out = mv.upload_revision(path, display_name="v2", version_name="v2")

        client.update_model.assert_called_once()
        kwargs = client.update_model.call_args.kwargs
        assert kwargs["display_name"] == "v2"
        assert kwargs["version_name"] == "v2"
        # Latest revision returned and view refreshed
        assert out is new_rev
        assert mv._model is updated_model
