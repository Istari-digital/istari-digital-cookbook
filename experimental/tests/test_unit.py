"""Unit tests for istari_experimental views -- no live API required."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from istari_digital_client import JobStatusName
from istari_experimental.istari_utils import (
    ArtifactView,
    IstariPlatform,
    JobDefinition,
    JobView,
    ModelView,
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


def _make_artifact(
    artifact_id: str = "art-1",
    name: str = "output.json",
    content: bytes = b"{}",
) -> tuple[MagicMock, MagicMock]:
    artifact = MagicMock()
    artifact.id = artifact_id
    rev = MagicMock()
    rev.display_name = name
    rev.name = name
    rev.mime = "application/json"
    rev.content_token = f"token-{artifact_id}"
    artifact.file.revision = rev
    artifact.file.revisions = [rev]
    client = MagicMock()
    client.read_contents.return_value = content
    return artifact, client


def _make_artifact_with_job_source(
    job_id: str,
    artifact_id: str = "art-1",
    name: str = "report.json",
) -> MagicMock:
    src = MagicMock()
    src.resource_type = "Job"
    src.resource_id = job_id
    rev = MagicMock()
    rev.display_name = name
    rev.name = name
    rev.mime = "application/json"
    rev.content_token = "tok"
    rev.sources = [src]
    artifact = MagicMock()
    artifact.id = artifact_id
    artifact.file.revisions = [rev]
    artifact.file.revision = rev
    return artifact


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

    def test_get_artifacts_returns_artifacts_linked_to_this_job(self):
        job_id = "job-99"
        mock_artifact = _make_artifact_with_job_source(job_id, name="report.json")

        mock_job = _make_job(JobStatusName.COMPLETED)
        mock_job.id = job_id
        mock_job.model_id = "model-1"

        mock_model = _make_model("model-1")
        mock_model.artifacts = [mock_artifact]

        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_client.get_model.return_value = mock_model

        jv = JobView(_job=mock_job, _client=mock_client)
        artifacts = jv.get_artifacts()
        assert len(artifacts) == 1
        assert artifacts[0].name == "report.json"

    def test_get_artifacts_excludes_artifacts_from_other_jobs(self):
        src = MagicMock()
        src.resource_type = "Job"
        src.resource_id = "some-other-job"
        rev = MagicMock()
        rev.sources = [src]
        artifact = MagicMock()
        artifact.file.revisions = [rev]

        mock_job = _make_job(JobStatusName.COMPLETED)
        mock_job.id = "job-99"
        mock_job.model_id = "model-1"
        mock_model = _make_model("model-1")
        mock_model.artifacts = [artifact]

        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_client.get_model.return_value = mock_model

        jv = JobView(_job=mock_job, _client=mock_client)
        assert jv.get_artifacts() == []

    def test_find_artifact_by_name(self):
        job_id = "job-99"
        mock_artifact = _make_artifact_with_job_source(job_id, name="report.json")
        mock_job = _make_job(JobStatusName.COMPLETED)
        mock_job.id = job_id
        mock_job.model_id = "model-1"
        mock_model = _make_model("model-1")
        mock_model.artifacts = [mock_artifact]
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_client.get_model.return_value = mock_model

        jv = JobView(_job=mock_job, _client=mock_client)
        assert jv.find_artifact(name="report.json") is not None
        assert jv.find_artifact(name="missing.json") is None


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
# ArtifactView
# ---------------------------------------------------------------------------

class TestArtifactView:
    def test_name_from_revision_display_name(self):
        artifact, client = _make_artifact(name="results.json")
        av = ArtifactView(_artifact=artifact, _client=client)
        assert av.name == "results.json"

    def test_read_bytes(self):
        artifact, client = _make_artifact(content=b"\x00\x01\x02")
        av = ArtifactView(_artifact=artifact, _client=client)
        assert av.read_bytes() == b"\x00\x01\x02"

    def test_read_text(self):
        artifact, client = _make_artifact(content=b'{"ok": true}')
        av = ArtifactView(_artifact=artifact, _client=client)
        assert av.read_text() == '{"ok": true}'

    def test_download_writes_to_explicit_path(self, tmp_path):
        artifact, client = _make_artifact(content=b"file content")
        av = ArtifactView(_artifact=artifact, _client=client)
        dest = tmp_path / "result.json"
        av.download(dest)
        assert dest.read_bytes() == b"file content"

    def test_download_to_directory_uses_artifact_filename(self, tmp_path):
        artifact, client = _make_artifact(name="output.json", content=b"data")
        av = ArtifactView(_artifact=artifact, _client=client)
        result = av.download(tmp_path)
        assert result.name == "output.json"
        assert result.read_bytes() == b"data"


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
