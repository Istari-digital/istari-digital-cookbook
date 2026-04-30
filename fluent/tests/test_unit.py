"""Unit tests for istari_fluent views -- no live API required."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from istari_digital_client import JobStatusName
from istari_fluent.istari_utils import (
    IstariPlatform,
    JobDefinition,
    JobView,
    ModelView,
    ResourceView,
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


def _make_model(model_id: str = "model-1", display_name: str = "My Model"):
    """Build a Model-typed mock whose ``type(obj).__name__`` is 'Model'.

    We also make isinstance(obj, Model) work so ``_make_resource_view``
    returns a ``ModelView`` rather than a bare ``ResourceView``.
    """
    from istari_digital_client.v2.models import Model

    # A real subclass of Model avoids pydantic validation but keeps isinstance.
    model = Model.__new__(Model)
    object.__setattr__(model, "id", model_id)

    file_mock = MagicMock()
    file_mock.id = f"file-{model_id}"

    rev = MagicMock()
    rev.display_name = display_name
    rev.name = f"{display_name}.mdzip"
    rev.id = f"rev-{model_id}"

    file_mock.revisions = [rev]
    object.__setattr__(model, "file", file_mock)
    object.__setattr__(model, "artifacts", [])
    return model


def _make_pinned_product_view(
    *,
    revision_id: str = "rev-1",
    file_id: str = "file-1",
    resource_type: str = "Artifact",
    resource_id: str = "art-1",
    name: str = "output.json",
    mime: str = "application/json",
    content: bytes = b"{}",
) -> tuple[ResourceView, MagicMock]:
    """Build a ResourceView pinned to a product revision (mimics JobView.find_product).

    Returns (view, client) so tests can assert on the client too.
    """
    # The owning resource (e.g. an Artifact); ``type(resource).__name__`` must
    # equal the requested resource_type, so we build a throwaway class.
    cls = type(resource_type, (), {})
    resource = cls()
    resource.id = resource_id
    resource.file = MagicMock()
    resource.file.id = file_id
    resource.file.revisions = []

    # The pinned revision
    rev = MagicMock()
    rev.id = revision_id
    rev.file_id = file_id
    rev.display_name = name
    rev.name = name
    rev.mime = mime
    rev.suffix = ""
    rev.content_token = f"token-{revision_id}"

    client = MagicMock()
    client.read_contents.return_value = content

    view = ResourceView(_resource=resource, _client=client, _pinned_revision=rev)
    return view, client


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


def _make_product_record(
    *,
    revision_id: str = "rev-1",
    file_id: str = "file-1",
    resource_type: str = "Artifact",
    resource_id: str = "art-1",
    name: str = "output.json",
    mime: str = "application/json",
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build (Product, owning resource, revision) mocks for use with a JobView.

    ``JobView.get_products`` fetches the resource and the revision via the
    client, so tests must register those on the client mock.
    """
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

    resource = MagicMock()
    resource.id = resource_id
    resource.__class__.__name__ = resource_type
    resource.file = MagicMock()
    resource.file.id = file_id
    resource.file.revisions = [rev]

    return product, resource, rev


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

    @patch("istari_fluent.istari_utils.time.sleep")
    def test_wait_returns_self_immediately_when_already_complete(self, _sleep):
        mock_client = MagicMock()
        mock_client.get_job.return_value = _make_job(JobStatusName.COMPLETED)
        jv = JobView(_job=_make_job(JobStatusName.PENDING), _client=mock_client)
        result = jv.wait(timeout=10, poll_interval=1)
        assert result is jv
        _sleep.assert_not_called()

    def test_get_products_returns_pinned_resource_views(self):
        product, resource, rev = _make_product_record(name="report.json")
        mock_job = _make_job_with_products("job-99", [product])
        client = MagicMock()
        client.get_job.return_value = mock_job
        client.get_resource.return_value = resource
        client.get_revision.return_value = rev

        jv = JobView(_job=mock_job, _client=client)
        products = jv.get_products()
        assert len(products) == 1
        assert isinstance(products[0], ResourceView)
        assert products[0].is_pinned is True
        assert products[0].name == "report.json"
        assert products[0].revision_id == "rev-1"

    def test_get_products_returns_empty_when_revision_has_no_products(self):
        mock_job = _make_job_with_products("job-99", [])
        client = MagicMock()
        client.get_job.return_value = mock_job

        jv = JobView(_job=mock_job, _client=client)
        assert jv.get_products() == []

    def test_get_products_filters_by_resource_type(self):
        art_p, art_r, art_rev = _make_product_record(name="a.json", resource_type="Artifact", resource_id="art-1")
        mod_p, mod_r, mod_rev = _make_product_record(name="m.json", resource_type="Model", resource_id="mod-1")
        mock_job = _make_job_with_products("job-99", [art_p, mod_p])
        client = MagicMock()
        client.get_job.return_value = mock_job
        client.get_resource.side_effect = lambda rtype, rid: {"art-1": art_r, "mod-1": mod_r}[rid]
        client.get_revision.side_effect = lambda rid: {"rev-1": art_rev}.get(rid, mod_rev)

        jv = JobView(_job=mock_job, _client=client)
        artifacts = jv.get_products(resource_type="Artifact")
        assert len(artifacts) == 1
        assert artifacts[0].type == "Artifact"

    def test_find_product_by_name(self):
        product, resource, rev = _make_product_record(name="report.json")
        mock_job = _make_job_with_products("job-99", [product])
        client = MagicMock()
        client.get_job.return_value = mock_job
        client.get_resource.return_value = resource
        client.get_revision.return_value = rev

        jv = JobView(_job=mock_job, _client=client)
        assert jv.find_product(name="report.json") is not None
        assert jv.find_product(name="missing.json") is None


# ---------------------------------------------------------------------------
# ModelView
# ---------------------------------------------------------------------------

class TestModelView:
    def test_name_from_latest_revision(self):
        mv = ModelView(_resource=_make_model(display_name="My SysML Model"), _client=MagicMock())
        assert mv.name == "My SysML Model"

    def test_submit_job_returns_job_view(self):
        mock_client = MagicMock()
        mock_client.add_job.return_value = _make_job()
        mv = ModelView(_resource=_make_model(), _client=mock_client)
        jv = mv.submit_job(JobDefinition(function="@test:fn", tool_name="tool"))
        assert isinstance(jv, JobView)
        mock_client.add_job.assert_called_once()

    def test_submit_job_passes_correct_parameters(self):
        mock_client = MagicMock()
        mock_client.add_job.return_value = _make_job()
        mv = ModelView(_resource=_make_model("model-42"), _client=mock_client)
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

    @patch("istari_fluent.istari_utils.time.sleep")
    def test_run_job_submits_waits_and_returns_completed_job(self, _sleep):
        mock_client = MagicMock()
        mock_client.add_job.return_value = _make_job(JobStatusName.PENDING)
        mock_client.get_job.return_value = _make_job(JobStatusName.COMPLETED)
        mv = ModelView(_resource=_make_model(), _client=mock_client)
        jv = mv.run_job(JobDefinition(function="@test:fn", tool_name="tool"), timeout=30)
        assert jv.completed is True


# ---------------------------------------------------------------------------
# ResourceView (pinned = former ProductView behaviour)
# ---------------------------------------------------------------------------

class TestResourceViewPinned:
    def test_name_from_pinned_revision(self):
        view, _ = _make_pinned_product_view(name="results.json")
        assert view.name == "results.json"

    def test_pinned_revision_does_not_fetch(self):
        view, client = _make_pinned_product_view()
        _ = view.name
        _ = view.mime
        _ = view.filename
        # Pinned revision is already attached; no lazy fetch needed.
        client.get_revision.assert_not_called()

    def test_read_bytes(self):
        view, _ = _make_pinned_product_view(content=b"\x00\x01\x02")
        assert view.read_bytes() == b"\x00\x01\x02"

    def test_read_text(self):
        view, _ = _make_pinned_product_view(content=b'{"ok": true}')
        assert view.read_text() == '{"ok": true}'

    def test_download_writes_to_explicit_path(self, tmp_path):
        view, _ = _make_pinned_product_view(content=b"file content")
        dest = tmp_path / "result.json"
        view.download(dest)
        assert dest.read_bytes() == b"file content"

    def test_download_to_directory_uses_filename(self, tmp_path):
        view, _ = _make_pinned_product_view(name="output.json", content=b"data")
        result = view.download(tmp_path)
        assert result.name == "output.json"
        assert result.read_bytes() == b"data"

    def test_as_source_uses_pinned_revision_id_without_api_call(self):
        view, client = _make_pinned_product_view(revision_id="rev-XYZ")
        src = view.as_source(relationship_identifier="input")
        assert src.revision_id == "rev-XYZ"
        assert src.relationship_identifier == "input"
        client.get_revision.assert_not_called()


# ---------------------------------------------------------------------------
# ResourceView dispatch -- run_job on Artifact auto-promotes
# ---------------------------------------------------------------------------

class TestResourceViewDispatch:
    def test_submit_job_on_non_supported_type_raises(self):
        # Build a JobView-like resource and wrap it
        cls = type("Job", (), {})
        resource = cls()
        resource.id = "job-xyz"
        view = ResourceView(_resource=resource, _client=MagicMock())
        with pytest.raises(TypeError, match="Cannot run a job"):
            view.submit_job(JobDefinition(function="@x:y", tool_name="t"))

    def test_submit_job_on_artifact_auto_promotes_then_submits(self):
        view, client = _make_pinned_product_view(resource_type="Artifact", name="out.json")

        # Promotion: add_model returns a Model
        promoted_model = _make_model("promoted-1", "out")
        client.add_model.return_value = promoted_model

        # Job submission on the promoted model
        client.add_job.return_value = _make_job()

        jv = view.submit_job(JobDefinition(function="@x:y", tool_name="t"))

        assert isinstance(jv, JobView)
        # Promotion happened
        client.add_model.assert_called_once()
        promote_kwargs = client.add_model.call_args.kwargs
        assert len(promote_kwargs["sources"]) == 1
        assert promote_kwargs["sources"][0].revision_id == "rev-1"
        assert promote_kwargs["sources"][0].relationship_identifier == "promoted_from"
        # Job used the promoted model id, not the artifact
        add_job_kwargs = client.add_job.call_args.kwargs
        assert add_job_kwargs["model_id"] == "promoted-1"

    def test_submit_job_on_artifact_rejects_save_input_as_revision(self):
        view, _ = _make_pinned_product_view(resource_type="Artifact")
        with pytest.raises(ValueError, match="save_input_as_revision"):
            view.submit_job(
                JobDefinition(function="@x:y", tool_name="t"),
                save_input_as_revision=True,
            )


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
