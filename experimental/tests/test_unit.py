"""Unit tests for istari_experimental views -- no live API required."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from istari_digital_client import JobStatusName
from istari_experimental.istari_utils import (
    IstariPlatform,
    JobDefinition,
    JobView,
    ModelView,
    ProductView,
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
