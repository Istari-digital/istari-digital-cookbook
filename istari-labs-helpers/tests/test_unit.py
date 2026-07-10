"""Unit tests for istari_labs_helpers views -- no live API required."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from istari_digital_client import JobStatusName
from istari_labs_helpers.istari_utils import (
    IstariPlatform,
    JobDefinition,
    JobView,
    ModelView,
    ResourceView,
    _build_lineage_node,
    configure_ssl_certificates,
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

    @patch("istari_labs_helpers.istari_utils.time.sleep")
    def test_wait_returns_self_immediately_when_already_complete(self, _sleep):
        mock_client = MagicMock()
        mock_client.get_job.return_value = _make_job(JobStatusName.COMPLETED)
        jv = JobView(_job=_make_job(JobStatusName.PENDING), _client=mock_client)
        result = jv.wait(timeout=10, poll_interval=1)
        assert result is jv
        _sleep.assert_not_called()

    @patch("istari_labs_helpers.istari_utils.time.sleep")
    def test_wait_calls_on_poll_with_current_jobview(self, _sleep):
        """on_poll fires every poll with the (refreshed) JobView."""
        mock_client = MagicMock()
        mock_client.get_job.side_effect = [
            _make_job(JobStatusName.PENDING),
            _make_job(JobStatusName.COMPLETED),
        ]
        jv = JobView(_job=_make_job(JobStatusName.PENDING), _client=mock_client)

        seen: list[str] = []
        jv.wait(timeout=10, poll_interval=0, on_poll=lambda j: seen.append(j.status))
        assert seen == [JobStatusName.PENDING.value, JobStatusName.COMPLETED.value]

    @patch("istari_labs_helpers.istari_utils.time.sleep")
    def test_wait_callback_exceptions_do_not_abort_loop(self, _sleep, capsys):
        """A buggy callback logs to stderr but wait still completes."""
        mock_client = MagicMock()
        mock_client.get_job.return_value = _make_job(JobStatusName.COMPLETED)
        jv = JobView(_job=_make_job(JobStatusName.PENDING), _client=mock_client)

        def boom(_):
            raise RuntimeError("boom")

        result = jv.wait(timeout=10, poll_interval=0, on_poll=boom)
        assert result is jv
        err = capsys.readouterr().err
        assert "on_poll callback raised" in err
        assert "RuntimeError" in err

    @patch("istari_labs_helpers.istari_utils.time.sleep")
    def test_wait_on_poll_accepts_builtin_print(self, _sleep, capsys):
        """Passing ``print`` directly uses JobView.__repr__ and writes to stdout."""
        mock_client = MagicMock()
        mock_client.get_job.return_value = _make_job(JobStatusName.COMPLETED)
        jv = JobView(_job=_make_job(JobStatusName.PENDING), _client=mock_client)

        jv.wait(timeout=10, poll_interval=0, on_poll=print)
        out = capsys.readouterr().out
        assert "Job(" in out and JobStatusName.COMPLETED.value in out

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

    def test_find_product_short_circuits_without_extra_calls(self):
        """find_product stops at the first match.

        Pays 1 revision call per scanned candidate; the owning resource is
        never fetched (lazy -- only loaded if the caller touches ``.file``).
        """
        hit_p, _hit_r, hit_rev = _make_product_record(
            revision_id="rev-hit", resource_id="art-hit", name="target.json",
        )
        miss_p, _miss_r, miss_rev = _make_product_record(
            revision_id="rev-miss", resource_id="art-miss", name="other.json",
        )
        mock_job = _make_job_with_products("job-99", [hit_p, miss_p])

        client = MagicMock()
        client.get_revision.side_effect = lambda rid: {
            "rev-hit": hit_rev, "rev-miss": miss_rev,
        }[rid]

        jv = JobView(_job=mock_job, _client=client)
        match = jv.find_product(name="target.json")

        assert match is not None and match.name == "target.json"
        client.get_job.assert_not_called()
        assert client.get_revision.call_count == 1
        assert client.get_revision.call_args.args == ("rev-hit",)
        client.get_resource.assert_not_called()

    def test_get_products_caches_when_job_is_terminal(self):
        """A completed job's product list is immutable -- second call is free."""
        product, _r, _rev = _make_product_record()
        mock_job = _make_job_with_products("job-99", [product], status=JobStatusName.COMPLETED)
        client = MagicMock()

        jv = JobView(_job=mock_job, _client=client)
        first = jv.get_products()
        second = jv.get_products()

        assert first == second
        assert first[0] is second[0]
        client.get_job.assert_not_called()
        client.get_resource.assert_not_called()
        client.get_revision.assert_not_called()

    def test_get_products_cache_memoises_revision_across_calls(self):
        """Touching .name on a cached view once loads the revision; second call is free."""
        product, _r, rev = _make_product_record(name="out.json")
        mock_job = _make_job_with_products("job-99", [product], status=JobStatusName.COMPLETED)
        client = MagicMock()
        client.get_revision.return_value = rev

        jv = JobView(_job=mock_job, _client=client)
        v1 = jv.get_products()[0]
        assert v1.name == "out.json"
        assert client.get_revision.call_count == 1

        v2 = jv.get_products()[0]
        assert v1 is v2
        assert v2.name == "out.json"
        assert client.get_revision.call_count == 1

    def test_find_product_is_free_on_repeat_call(self):
        """Cache + per-view memoisation: second find_product does zero round-trips."""
        product, _r, rev = _make_product_record(name="out.json")
        mock_job = _make_job_with_products("job-99", [product], status=JobStatusName.COMPLETED)
        client = MagicMock()
        client.get_revision.return_value = rev

        jv = JobView(_job=mock_job, _client=client)
        a = jv.find_product(name="out.json")
        baseline_revisions = client.get_revision.call_count

        b = jv.find_product(name="out.json")
        assert a is b
        assert client.get_revision.call_count == baseline_revisions
        client.get_job.assert_not_called()

    def test_get_products_refresh_true_invalidates_cache(self):
        """refresh=True drops the cache and forces a get_job round-trip."""
        product, _r, _rev = _make_product_record()
        mock_job = _make_job_with_products("job-99", [product], status=JobStatusName.COMPLETED)
        client = MagicMock()
        client.get_job.return_value = mock_job

        jv = JobView(_job=mock_job, _client=client)
        jv.get_products()
        client.get_job.assert_not_called()

        jv.get_products(refresh=True)
        client.get_job.assert_called_once_with("job-99")

    def test_get_products_does_not_cache_non_terminal_job(self):
        """Running / pending jobs keep re-reading the server (products may grow)."""
        product, _r, _rev = _make_product_record()
        mock_job = _make_job_with_products("job-99", [product], status=JobStatusName.RUNNING)
        client = MagicMock()
        client.get_job.return_value = mock_job

        jv = JobView(_job=mock_job, _client=client)
        assert jv._is_terminal() is False
        jv.get_products()
        jv.get_products()
        assert jv._products_cache is None

    def test_get_products_lazy_does_not_fetch_resources_or_revisions(self):
        product, _resource, _rev = _make_product_record()
        mock_job = _make_job_with_products("job-99", [product])
        client = MagicMock()

        jv = JobView(_job=mock_job, _client=client)
        views = jv.get_products()  # lazy by default

        assert len(views) == 1
        client.get_job.assert_not_called()
        client.get_resource.assert_not_called()
        client.get_revision.assert_not_called()
        assert views[0].is_pinned is True
        assert views[0].id == "art-1"
        assert views[0].type == "Artifact"

    def test_get_products_lazy_loads_revision_on_first_access(self):
        product, _resource, rev = _make_product_record(name="out.json")
        mock_job = _make_job_with_products("job-99", [product])
        client = MagicMock()
        client.get_revision.return_value = rev

        jv = JobView(_job=mock_job, _client=client)
        (view,) = jv.get_products()

        client.get_revision.assert_not_called()
        _ = view.name
        _ = view.name
        assert client.get_revision.call_count == 1
        client.get_resource.assert_not_called()

    def test_get_products_refresh_forces_get_job(self):
        product, resource, rev = _make_product_record()
        mock_job = _make_job_with_products("job-99", [product])
        client = MagicMock()
        client.get_job.return_value = mock_job
        client.get_resource.return_value = resource
        client.get_revision.return_value = rev

        jv = JobView(_job=mock_job, _client=client)
        jv.get_products(refresh=True)
        client.get_job.assert_called_once_with("job-99")

    def test_get_products_eager_matches_legacy_behavior(self):
        product, resource, rev = _make_product_record()
        mock_job = _make_job_with_products("job-99", [product])
        client = MagicMock()
        client.get_job.return_value = mock_job
        client.get_resource.return_value = resource
        client.get_revision.return_value = rev

        jv = JobView(_job=mock_job, _client=client)
        views = jv.get_products(lazy=False)
        assert len(views) == 1
        client.get_resource.assert_called_once()
        client.get_revision.assert_called_once()
        assert views[0]._pinned_revision is rev


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

    @patch("istari_labs_helpers.istari_utils.time.sleep")
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

    def test_systems_factory_returns_item_query_bound_to_list_systems(self):
        from istari_labs_helpers import ItemQuery

        mock_client = MagicMock()
        q = IstariPlatform(mock_client).systems()
        assert isinstance(q, ItemQuery)
        assert q._list_fn is mock_client.list_systems

    def test_jobs_factory_with_model_id_uses_dedicated_endpoint(self):
        from istari_labs_helpers import ItemQuery

        mock_client = MagicMock()
        q_all = IstariPlatform(mock_client).jobs()
        q_one = IstariPlatform(mock_client).jobs(model_id="m-42")
        assert isinstance(q_all, ItemQuery) and isinstance(q_one, ItemQuery)
        assert q_all._list_fn is mock_client.list_jobs
        assert q_one._list_fn is mock_client.list_model_jobs
        assert q_one._filters == {"model_id": "m-42"}

    def test_resources_factory_returns_resource_query(self):
        from istari_labs_helpers import ResourceQuery

        mock_client = MagicMock()
        q = IstariPlatform(mock_client).resources()
        assert isinstance(q, ResourceQuery)
        assert q._list_fn is mock_client.list_resources

    def test_get_resource_wraps_model_as_model_view(self):
        mock_client = MagicMock()
        mock_client.get_resource.return_value = _make_model("mid-1")
        v = IstariPlatform(mock_client).get_resource("model", "mid-1")
        assert isinstance(v, ModelView)
        mock_client.get_resource.assert_called_once_with("Model", "mid-1")

    def test_get_resource_rejects_job_slug_and_enum(self):
        from istari_digital_client.v2.models.resource_type import ResourceType

        mock_client = MagicMock()
        with pytest.raises(TypeError, match="not resources"):
            IstariPlatform(mock_client).get_resource("Job", "x")
        with pytest.raises(TypeError, match="not resources"):
            IstariPlatform(mock_client).get_resource(ResourceType.JOB, "x")
        mock_client.get_resource.assert_not_called()

    def test_get_resource_maps_list_slug_resource_to_artifact(self):
        from istari_digital_client.v2.models.resource_type import ResourceType

        art = MagicMock()
        mock_client = MagicMock()
        mock_client.get_resource.return_value = art
        IstariPlatform(mock_client).get_resource(ResourceType.RESOURCE, "rid-1")
        mock_client.get_resource.assert_called_once_with("Artifact", "rid-1")

    def test_get_revision_delegates_to_client(self):
        mock_client = MagicMock()
        rev = MagicMock()
        mock_client.get_revision.return_value = rev
        out = IstariPlatform(mock_client).get_revision("rev-9")
        assert out is rev
        mock_client.get_revision.assert_called_once_with("rev-9")

    def test_put_text_file_add_and_update(self):
        mock_client = MagicMock()
        created = MagicMock()
        created.id = "mid-1"
        mock_client.add_model.return_value = created
        mock_client.update_model = MagicMock(return_value=created)
        platform = IstariPlatform(mock_client)
        platform.put_text_file(
            "hello",
            filename="demo.txt",
            external_identifier="ext-1",
            version_name="v1",
        )
        mock_client.add_model.assert_called_once()
        assert mock_client.add_model.call_args.kwargs["version_name"] == "v1"
        assert mock_client.add_model.call_args.kwargs["external_identifier"] == "ext-1"
        platform.put_text_file(
            "next rev",
            filename="demo.txt",
            model_id="mid-1",
            version_name="v2",
        )
        mock_client.update_model.assert_called_once()
        assert mock_client.update_model.call_args[0][0] == "mid-1"
        assert mock_client.update_model.call_args.kwargs["version_name"] == "v2"

    def test_agents_factory_returns_item_query_bound_to_list_agents(self):
        from istari_labs_helpers import ItemQuery

        mock_client = MagicMock()
        q = IstariPlatform(mock_client).agents()
        assert isinstance(q, ItemQuery)
        assert q._list_fn is mock_client.list_agents

    def test_whoami_returns_user_view(self):
        mock_client = MagicMock()
        user = MagicMock()
        user.id = "user-abc"
        user.email = "alice@example.com"
        user.display_name = "Alice"
        user.user_name = None
        mock_client.get_current_user.return_value = user

        me = IstariPlatform(mock_client).whoami()
        assert me.id == "user-abc"
        assert me.email == "alice@example.com"
        assert str(me) == "Alice (alice@example.com)"
        mock_client.get_current_user.assert_called_once()

    def test_tools_query_yields_tool_views(self):
        from istari_labs_helpers import ToolView

        tool = MagicMock()
        tool.id = "tool-1"
        tool.name = "ansys"
        tool.functions = [MagicMock(), MagicMock()]
        page = MagicMock()
        page.iter_items.return_value = iter([tool])
        mock_client = MagicMock()
        mock_client.list_tools.return_value = page

        views = list(IstariPlatform(mock_client).tools())
        assert len(views) == 1
        assert isinstance(views[0], ToolView)
        assert views[0].id == "tool-1"
        assert views[0].name == "ansys"
        assert views[0].function_count == 2

    def test_find_user_by_email(self):
        u1 = MagicMock()
        u1.email = "bob@example.com"
        u1.display_name = "Bob"
        u1.id = "u1"
        page = MagicMock()
        page.iter_items.return_value = iter([u1])
        mock_client = MagicMock()
        mock_client.list_users.return_value = page

        found = IstariPlatform(mock_client).find_user("Bob@Example.com")
        assert found is not None
        assert found.id == "u1"
        assert IstariPlatform(mock_client).find_user("missing@example.com") is None

    def test_user_tools_returns_execute_grants(self):
        from istari_labs_helpers import ToolView, UserToolAccessQuery

        perm = MagicMock()
        perm.resource_id = "tool-a"
        perm_page = MagicMock()
        perm_page.iter_items.side_effect = lambda: iter([perm])

        tool_a = MagicMock()
        tool_a.id = "tool-a"
        tool_a.name = "ansys"
        tool_a.functions = [MagicMock()]
        tool_b = MagicMock()
        tool_b.id = "tool-b"
        tool_b.name = "other"
        tool_b.functions = []
        tools_page = MagicMock()
        tools_page.iter_items.side_effect = lambda: iter([tool_a, tool_b])

        mock_client = MagicMock()
        mock_client.list_resource_type_permissions.return_value = perm_page
        mock_client.list_tools.return_value = tools_page

        user = MagicMock()
        user.id = "user-1"
        user.email = "bob@example.com"
        user.display_name = "Bob"
        user.user_name = None
        users_page = MagicMock()
        users_page.iter_items.return_value = iter([user])
        mock_client.list_users.return_value = users_page

        platform = IstariPlatform(mock_client)
        bob = platform.get_user("bob@example.com")
        q = bob.tools()
        assert isinstance(q, UserToolAccessQuery)
        assert q.tool_ids == {"tool-a"}

        views = list(q)
        assert len(views) == 1
        assert isinstance(views[0], ToolView)
        assert views[0].name == "ansys"
        assert len(bob.granted_tools()) == 1

    @patch("istari_labs_helpers._sdk.V3Client")
    @patch("istari_labs_helpers._sdk.Client")
    @patch("istari_digital_client.configuration.Configuration")
    @patch("dotenv.load_dotenv")
    def test_from_env_uses_istari_ca_bundle(self, _ld, _cfg, _client, _v3, monkeypatch, tmp_path):
        bundle = tmp_path / "ca.pem"
        bundle.write_text("dummy")
        monkeypatch.setenv("ISTARI_CA_BUNDLE", str(bundle))
        monkeypatch.setenv("ISTARI_REGISTRY_URL", "https://reg.example")
        monkeypatch.setenv("ISTARI_PERSONAL_ACCESS_TOKEN", "tok")
        with patch("istari_labs_helpers.istari_utils.ssl.create_default_context"):
            IstariPlatform.from_env(".env")
        assert os.environ["REQUESTS_CA_BUNDLE"] == str(bundle.resolve())
        _client.assert_called_once()
        _v3.assert_called_once()


class TestBranchDownload:
    def _make_system(self, mock_client):
        system = MagicMock()
        system.id = "sys-1"
        system.name = "Demo System"
        system.configurations = []
        system.client = mock_client
        return system

    def test_branches_lists_baseline_and_user_branches(self):
        from istari_labs_helpers import BranchView

        baseline = MagicMock()
        baseline.tag = "baseline"
        baseline.is_baseline = True
        baseline.snapshot_id = "snap-b"
        baseline.id = "tag-b"

        main = MagicMock()
        main.tag = "main"
        main.is_baseline = False
        main.snapshot_id = "snap-m"
        main.id = "tag-m"

        mock_client = MagicMock()
        system = self._make_system(mock_client)
        system.get_branch = MagicMock(side_effect=lambda name: baseline if name == "baseline" else main)
        system.list_branches = MagicMock(return_value=[main])

        from istari_labs_helpers.istari_utils import SystemView

        views = SystemView(_system=system, _client=mock_client).branches()
        assert len(views) == 2
        assert all(isinstance(v, BranchView) for v in views)
        assert views[0].name == "baseline"
        assert views[1].name == "main"

    def test_download_single_revision_writes_file(self, tmp_path):
        from istari_labs_helpers import BranchDownloadResult

        branch_tag = MagicMock(tag="baseline", is_baseline=True, snapshot_id="snap-1", id="tag-1")
        item = MagicMock()
        item.name = "wing.mdzip"
        item.display_name = "Wing"
        item.extension = None
        item.revision_id = "rev-1"
        item.read_bytes.return_value = b"MODEL"

        mock_client = MagicMock()
        system = self._make_system(mock_client)
        system.get_branch = MagicMock(return_value=branch_tag)
        system._iter_snapshot_revisions = MagicMock(return_value=[item])
        system._iter_snapshot_subsystems = MagicMock(return_value=[])

        from istari_labs_helpers.istari_utils import SystemView

        result = SystemView(_system=system, _client=mock_client).download_resources(
            "baseline", dest=tmp_path
        )

        assert isinstance(result, BranchDownloadResult)
        assert result.file_count == 1
        assert result.is_zip is False
        assert result.members == ("wing.mdzip",)
        assert result.path.read_bytes() == b"MODEL"

    def test_download_multiple_revisions_writes_zip(self, tmp_path):
        import zipfile

        from istari_labs_helpers import BranchDownloadResult

        branch_tag = MagicMock(tag="main", is_baseline=False, snapshot_id="snap-2", id="tag-2")

        def make_item(name, content):
            item = MagicMock()
            item.name = name
            item.display_name = name
            item.extension = None
            item.revision_id = f"rev-{name}"
            item.read_bytes.return_value = content
            return item

        mock_client = MagicMock()
        system = self._make_system(mock_client)
        system.get_branch = MagicMock(return_value=branch_tag)
        system._iter_snapshot_revisions = MagicMock(
            return_value=[make_item("a.json", b"A"), make_item("b.json", b"B")]
        )
        system._iter_snapshot_subsystems = MagicMock(return_value=[])

        from istari_labs_helpers.istari_utils import SystemView

        result = SystemView(_system=system, _client=mock_client).download_resources(
            "main", dest=tmp_path / "bundle.zip"
        )

        assert result.is_zip is True
        assert result.file_count == 2
        with zipfile.ZipFile(result.path) as zf:
            assert sorted(zf.namelist()) == ["a.json", "b.json"]

    def test_download_system_resources_by_id(self, tmp_path):
        from istari_labs_helpers import IstariPlatform

        branch_tag = MagicMock(tag="main", is_baseline=False, snapshot_id="snap-3", id="tag-3")
        item = MagicMock()
        item.name = "only.catpart"
        item.display_name = "Only"
        item.extension = None
        item.revision_id = "rev-1"
        item.read_bytes.return_value = b"X"

        mock_client = MagicMock()
        system = self._make_system(mock_client)
        system.get_branch = MagicMock(return_value=branch_tag)
        system._iter_snapshot_revisions = MagicMock(return_value=[item])
        system._iter_snapshot_subsystems = MagicMock(return_value=[])
        mock_client.get_system.return_value = system

        result = IstariPlatform(mock_client).download_system_resources(
            "sys-1", "main", dest=tmp_path
        )
        assert result.file_count == 1
        assert result.path.name == "only.catpart"

    def test_branch_subsystems(self):
        from istari_labs_helpers import SubsystemView

        branch_tag = MagicMock(tag="baseline", is_baseline=True, snapshot_id="snap-1", id="tag-1")
        sub_item = MagicMock()
        sub_item.system_id = "sub-1"
        sub_item.system_name = "Wing"
        sub_item.system_description = "Wing assembly"
        sub_item.tag_id = "tag-sub"
        sub_item.tagged_configuration_id = "cfg-sub"
        sub_item.tagged_configuration_name = "v1"
        sub_item.tagged_snapshot_id = "snap-sub"
        sub_item.is_archived = False

        mock_client = MagicMock()
        system = self._make_system(mock_client)
        system.get_branch = MagicMock(return_value=branch_tag)
        system.list_branch_subsystems = MagicMock(return_value=[sub_item])

        from istari_labs_helpers.istari_utils import SystemView

        views = SystemView(_system=system, _client=mock_client).get_branch("baseline").subsystems()
        assert len(views) == 1
        assert isinstance(views[0], SubsystemView)
        assert views[0].system_name == "Wing"
        assert views[0].snapshot_id == "snap-sub"
        system.list_branch_subsystems.assert_called_once_with(branch_tag)

    def test_download_with_subsystem_depth(self, tmp_path):
        import zipfile

        from istari_labs_helpers import BranchDownloadResult

        branch_tag = MagicMock(tag="baseline", is_baseline=True, snapshot_id="snap-root", id="tag-1")

        root_item = MagicMock()
        root_item.name = "root.json"
        root_item.display_name = "root"
        root_item.extension = None
        root_item.revision_id = "rev-root"
        root_item.read_bytes.return_value = b"ROOT"

        sub_item = MagicMock()
        sub_item.system_id = "sub-1"
        sub_item.system_name = "Wing"
        sub_item.tagged_snapshot_id = "snap-sub"

        sub_file = MagicMock()
        sub_file.name = "wing.json"
        sub_file.display_name = "wing"
        sub_file.extension = None
        sub_file.revision_id = "rev-sub"
        sub_file.read_bytes.return_value = b"WING"

        sub_system = MagicMock()
        sub_system.id = "sub-1"
        sub_system.name = "Wing"
        sub_system.configurations = []
        sub_system.client = None

        mock_client = MagicMock()
        system = self._make_system(mock_client)
        system.get_branch = MagicMock(return_value=branch_tag)
        system._iter_snapshot_revisions = MagicMock(return_value=[root_item])
        system._iter_snapshot_subsystems = MagicMock(return_value=[sub_item])
        sub_system._iter_snapshot_revisions = MagicMock(return_value=[sub_file])
        sub_system._iter_snapshot_subsystems = MagicMock(return_value=[])
        mock_client.get_system.return_value = sub_system

        from istari_labs_helpers.istari_utils import SystemView

        result = SystemView(_system=system, _client=mock_client).download_resources(
            "baseline", dest=tmp_path, depth=2
        )

        assert isinstance(result, BranchDownloadResult)
        assert result.is_zip is True
        assert result.file_count == 2
        with zipfile.ZipFile(result.path) as zf:
            assert sorted(zf.namelist()) == ["Wing/wing.json", "root.json"]
            assert zf.read("root.json") == b"ROOT"
            assert zf.read("Wing/wing.json") == b"WING"

    def test_download_depth_one_skips_subsystems(self, tmp_path):
        branch_tag = MagicMock(tag="baseline", is_baseline=True, snapshot_id="snap-root", id="tag-1")

        root_item = MagicMock()
        root_item.name = "only.json"
        root_item.display_name = "only"
        root_item.extension = None
        root_item.revision_id = "rev-root"
        root_item.read_bytes.return_value = b"ONLY"

        mock_client = MagicMock()
        system = self._make_system(mock_client)
        system.get_branch = MagicMock(return_value=branch_tag)
        system._iter_snapshot_revisions = MagicMock(return_value=[root_item])
        system._iter_snapshot_subsystems = MagicMock()

        from istari_labs_helpers.istari_utils import SystemView

        SystemView(_system=system, _client=mock_client).download_resources(
            "baseline", dest=tmp_path, depth=1
        )
        system._iter_snapshot_subsystems.assert_not_called()


class TestItemQuery:
    """Behaviour of the generic ItemQuery wrapper (queries.py)."""

    @staticmethod
    def _fake_list_fn(items, *, total=None):
        """Build a list_fn whose response page yields ``items`` via iter_items."""
        page = MagicMock()
        page.iter_items.return_value = iter(items)
        page.total = total if total is not None else len(items)
        fn = MagicMock(return_value=page)
        fn.__name__ = "list_fake"
        return fn, page

    def test_iter_yields_items_via_iter_items(self):
        from istari_labs_helpers import ItemQuery

        fn, _ = self._fake_list_fn([{"id": 1}, {"id": 2}])
        q = ItemQuery(fn)
        assert list(iter(q)) == [{"id": 1}, {"id": 2}]
        fn.assert_called_once()

    def test_iter_defaults_to_max_page_size(self):
        from istari_labs_helpers import ItemQuery
        from istari_labs_helpers.queries import DEFAULT_PAGE_SIZE

        fn, _ = self._fake_list_fn([])
        list(ItemQuery(fn))
        assert fn.call_args.kwargs["size"] == DEFAULT_PAGE_SIZE

    def test_filter_returns_new_immutable_query(self):
        from istari_labs_helpers import ItemQuery

        fn, _ = self._fake_list_fn([])
        base = ItemQuery(fn, archive_status="active")
        narrowed = base.filter(status_name="completed")
        assert base is not narrowed
        assert base._filters == {"archive_status": "active"}
        assert narrowed._filters == {"archive_status": "active", "status_name": "completed"}

    def test_filter_overrides_existing_key(self):
        from istari_labs_helpers import ItemQuery

        fn, _ = self._fake_list_fn([])
        q = ItemQuery(fn, size=10).filter(size=50)
        assert q._filters == {"size": 50}

    def test_sort_sets_sort_field(self):
        from istari_labs_helpers import ItemQuery

        fn, _ = self._fake_list_fn([])
        q = ItemQuery(fn).sort("-created")
        assert q._filters == {"sort": "-created"}

    def test_first_returns_first_item_or_none(self):
        from istari_labs_helpers import ItemQuery

        fn_full, _ = self._fake_list_fn([{"id": 1}, {"id": 2}])
        fn_empty, _ = self._fake_list_fn([])
        assert ItemQuery(fn_full).first() == {"id": 1}
        assert ItemQuery(fn_empty).first() is None

    def test_take_returns_at_most_n_items(self):
        from istari_labs_helpers import ItemQuery

        fn, _ = self._fake_list_fn([{"id": i} for i in range(5)])
        assert ItemQuery(fn).take(3) == [{"id": 0}, {"id": 1}, {"id": 2}]

    def test_all_materialises_full_iterator(self):
        from istari_labs_helpers import ItemQuery

        items = [{"id": i} for i in range(4)]
        fn, _ = self._fake_list_fn(items)
        assert ItemQuery(fn).all() == items

    def test_count_uses_page_total_with_size_one(self):
        from istari_labs_helpers import ItemQuery

        fn, _ = self._fake_list_fn([{"id": 1}], total=4242)
        q = ItemQuery(fn).filter(archive_status="active")
        assert q.count() == 4242
        assert len(q) == 4242
        for call in fn.call_args_list:
            assert call.kwargs["page"] == 1
            assert call.kwargs["size"] == 1
            assert call.kwargs["archive_status"] == "active"

    def test_repr_describes_query(self):
        from istari_labs_helpers import ItemQuery

        fn, _ = self._fake_list_fn([])
        r = repr(ItemQuery(fn).filter(archive_status="active").sort("-created"))
        assert "list_fake" in r
        assert "archive_status='active'" in r
        assert "sort='-created'" in r


class TestResourceQuery:
    """ResourceQuery adds ``.type(...)`` sugar on top of ItemQuery."""

    def test_type_filters_by_resource_type_enum(self):
        from istari_digital_client.v2.models.resource_type import ResourceType
        from istari_labs_helpers import ResourceQuery

        fn = MagicMock()
        fn.__name__ = "list_resources"
        q = ResourceQuery(fn).type("model")
        assert q._filters == {"type_name": [ResourceType.MODEL]}

    def test_type_accepts_enum_directly(self):
        from istari_digital_client.v2.models.resource_type import ResourceType
        from istari_labs_helpers import ResourceQuery

        fn = MagicMock()
        fn.__name__ = "list_resources"
        q = ResourceQuery(fn).type(ResourceType.ARTIFACT)
        assert q._filters == {"type_name": [ResourceType.ARTIFACT]}

    def test_type_returns_resource_query_so_chain_keeps_subtype(self):
        from istari_labs_helpers import ResourceQuery

        fn = MagicMock()
        fn.__name__ = "list_resources"
        q = ResourceQuery(fn).type("model").filter(file_name="x.pdf")
        assert isinstance(q, ResourceQuery)


class TestResourceViewReadJson:
    def test_read_json(self):
        view, _ = _make_pinned_product_view(content=b'{"k": [1]}')
        assert view.read_json() == {"k": [1]}


class TestConfigureSsl:
    def test_missing_bundle_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            configure_ssl_certificates(tmp_path / "nope.pem")

    def test_sets_env_when_bundle_exists(self, tmp_path):
        bundle = tmp_path / "ca.pem"
        bundle.write_text("dummy-pem")
        sentinel = object()
        with patch("istari_labs_helpers.istari_utils.ssl.create_default_context", return_value=sentinel) as m_ctx:
            ctx = configure_ssl_certificates(bundle)
        assert ctx is sentinel
        m_ctx.assert_called_once()
        assert os.environ["REQUESTS_CA_BUNDLE"] == str(bundle.resolve())


class TestModelViewDownloadArtifacts:
    def test_in_memory_json_and_bytes(self):
        art_j = MagicMock()
        art_j.name = "a.json"
        art_j.read_bytes.return_value = b'{"x": 1}'
        art_bin = MagicMock()
        art_bin.name = "b.bin"
        art_bin.read_bytes.return_value = b"\x01\x02"
        model = _make_model()
        object.__setattr__(model, "artifacts", [art_j, art_bin])
        mv = ModelView(_resource=model, _client=MagicMock())
        out = mv.download_artifacts()
        assert out["a.json"] == {"x": 1}
        assert out["b.bin"] == b"\x01\x02"

    def test_filtered_miss_raises(self):
        model = _make_model()
        object.__setattr__(model, "artifacts", [])
        mv = ModelView(_resource=model, _client=MagicMock())
        with pytest.raises(FileNotFoundError):
            mv.download_artifacts(names={"missing.json"})

    def test_writes_directory_returns_empty_dict(self, tmp_path):
        art = MagicMock()
        art.name = "out.json"
        art.read_bytes.return_value = b'{"z": true}'
        model = _make_model()
        object.__setattr__(model, "artifacts", [art])
        mv = ModelView(_resource=model, _client=MagicMock())
        assert mv.download_artifacts(dest=tmp_path) == {}
        written = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
        assert written == {"z": True}


class TestModelViewArchive:
    def test_archive_calls_client(self):
        mc = MagicMock()
        mv = ModelView(_resource=_make_model("mid-1"), _client=mc)
        mv.archive()
        mc.archive_model.assert_called_once_with("mid-1")

    def test_raises_when_no_model_id(self):
        from istari_digital_client.v2.models import Model

        bare = Model.__new__(Model)
        object.__setattr__(bare, "id", "")
        object.__setattr__(bare, "file", None)
        object.__setattr__(bare, "artifacts", [])
        mv = ModelView(_resource=bare, _client=MagicMock())
        with pytest.raises(ValueError, match="no id"):
            mv.archive()


# ---------------------------------------------------------------------------
# Lineage -- Job node restructuring
# ---------------------------------------------------------------------------

class TestLineageJobRestructuring:
    """Raw platform graph: Artifact <- [Model, parameters.json <- Model].

    Fluent tree: Artifact <- Job <- Model.  We surface the parameters
    revision as a ``Job`` node and drop the redundant sibling Model.
    """

    def _make_rev(self, rev_id, name, sources=None, file_id=None):
        r = MagicMock()
        r.id = rev_id
        r.name = name
        r.display_name = name
        r.file_id = file_id or f"file-{rev_id}"
        r.created = None
        r.sources = sources or []
        return r

    def _make_source(self, rev_id, resource_type, resource_id=None, relationship=None):
        s = MagicMock()
        s.revision_id = rev_id
        s.resource_type = resource_type
        s.resource_id = resource_id or f"res-{rev_id}"
        s.relationship_identifier = relationship
        return s

    def _build(self, artifact_rev, revisions, *, job_function_name=None):
        client = MagicMock()
        client.get_revision.side_effect = lambda rid: revisions[rid]
        client.get_file.side_effect = Exception
        job = MagicMock()
        job.function.name = job_function_name
        client.get_job.return_value = job
        return _build_lineage_node(
            client, artifact_rev,
            relationship_to_child=None, max_depth=5, depth=0, cache={},
        )

    def test_tree_reads_artifact_then_job_then_model(self):
        """The Model sibling at the Artifact level is dropped; Model lives under Job."""
        model_rev = self._make_rev("m-1", "input.xlsx")
        params_rev = self._make_rev(
            "p-1", "parameters_abc.json",
            sources=[self._make_source("m-1", "Model", "mid-1")],
        )
        artifact_rev = self._make_rev(
            "a-1", "workbook.xlsx",
            sources=[
                self._make_source("m-1", "Model", "mid-1"),
                self._make_source("p-1", "Job", "job-abc"),
            ],
        )
        revisions = {"m-1": model_rev, "p-1": params_rev, "a-1": artifact_rev}

        root = self._build(artifact_rev, revisions, job_function_name="@istari:extract")

        assert [p.resource_type for p in root.parents] == ["Job"]
        job_node = root.parents[0]
        assert job_node.step == "job_run"
        assert job_node.resource_id == "job-abc"
        assert job_node.function_name == "@istari:extract"
        assert "@istari:extract" in job_node.label and "job-abc" in job_node.label

        assert [p.resource_type for p in job_node.parents] == ["Model"]
        model_node = job_node.parents[0]
        assert model_node.step == "upload"
        assert model_node.label == "input.xlsx"

    def test_promoted_from_sibling_is_kept_alongside_job(self):
        """A structural ``promoted_from`` source is preserved; plain Model sibling is dropped."""
        model_rev = self._make_rev("m-1", "input.xlsx")
        params_rev = self._make_rev(
            "p-1", "parameters_abc.json",
            sources=[self._make_source("m-1", "Model", "mid-1")],
        )
        promoted_src_rev = self._make_rev("pf-1", "source.xlsx")
        artifact_rev = self._make_rev(
            "a-1", "workbook.xlsx",
            sources=[
                self._make_source("m-1", "Model", "mid-1"),
                self._make_source("p-1", "Job", "job-abc"),
                self._make_source("pf-1", "Artifact", "art-1", relationship="promoted_from"),
            ],
        )
        revisions = {
            "m-1": model_rev, "p-1": params_rev,
            "pf-1": promoted_src_rev, "a-1": artifact_rev,
        }

        root = self._build(artifact_rev, revisions)
        kept = {(p.resource_type, p.relationship_to_child) for p in root.parents}
        assert kept == {("Job", None), ("Artifact", "promoted_from")}

    def test_job_node_falls_back_to_id_when_function_unavailable(self):
        model_rev = self._make_rev("m-1", "input.xlsx")
        params_rev = self._make_rev(
            "p-1", "parameters_abc.json",
            sources=[self._make_source("m-1", "Model", "mid-1")],
        )
        artifact_rev = self._make_rev(
            "a-1", "workbook.xlsx",
            sources=[
                self._make_source("m-1", "Model", "mid-1"),
                self._make_source("p-1", "Job", "job-abc"),
            ],
        )
        revisions = {"m-1": model_rev, "p-1": params_rev, "a-1": artifact_rev}

        client = MagicMock()
        client.get_revision.side_effect = lambda rid: revisions[rid]
        client.get_file.side_effect = Exception
        client.get_job.side_effect = Exception

        root = _build_lineage_node(
            client, artifact_rev,
            relationship_to_child=None, max_depth=5, depth=0, cache={},
        )

        (job_node,) = root.parents
        assert job_node.resource_type == "Job"
        assert job_node.function_name is None
        assert job_node.label == "job (job-abc)"
