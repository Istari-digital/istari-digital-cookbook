"""
Istari SDK utilities -- object-oriented wrappers over the flat client API.

Entity hierarchy
----------------
    IstariPlatform           (entry point, wraps Client)
      +-- SystemView         (wraps System)
      |     +-- .baseline              -> SnapshotView
      |     +-- .configurations        -> list[ConfigurationView]
      |     +-- .add_file / .add_revision
      +-- SnapshotView        (wraps Snapshot)
      |     +-- .configuration         -> ConfigurationView
      +-- ConfigurationView   (wraps SystemConfiguration)
      |     +-- .get_models()          -> list[ModelView]
      |     +-- .get_tracked_files()   -> list[TrackedFile]
      |     +-- .add_file(fid)         -> TrackedFileSet  (builder)
      |     +-- .set_baseline()        -> self  (moves system baseline here)
      +-- TrackedFileSet        (builder for new configurations)
      |     +-- .add_file(fid)         -> self  (chainable)
      |     +-- .save(name=None)       -> ConfigurationView
      +-- ModelView           (wraps Model + optional TrackedFile)
      |     +-- .name / .id
      |     +-- .current_revision_id / .pinned_revision_id
      |     +-- .get_jobs()            -> list[JobView]
      |     +-- .submit_job()          -> JobView
      |     +-- .run_job()             -> JobView
      +-- JobView             (wraps Job)
      |     +-- .status / .created / .function_name
      |     +-- .model_revision_id
      |     +-- .revision              -> FileRevision (latest job-output revision)
      |     +-- .get_products()        -> list[ResourceView]  (each pinned to the product's revision)
      |     +-- .find_product()        -> ResourceView | None  (pinned)
      |     +-- .wait()                -> self (chainable)
      |     +-- .on_success()          -> self or raise
      |     +-- .completed / .failed   bool properties
      +-- ResourceView        (unified wrapper for Artifact / Model / Job / ...)
      |     +-- .id / .type / .raw / .file / .latest_revision
      |     +-- .revision              -> FileRevision  (pinned if set, else latest)
      |     +-- .pin(rev) / .unpinned  (toggle the revision pin)
      |     +-- .name / .filename / .mime / .file_id / .revision_id
      |     +-- .read_bytes() / .read_text() / .download(dest)
      |     +-- .as_source()           -> NewSource  (chain into next job, no API call)
      |     +-- .promote()             -> ModelView  (revision-to-model, tagged 'promoted_from')
      |     +-- .get_lineage()         -> LineageNode  (backward provenance)
      |     +-- .submit_job(defn)      -> JobView  (auto-promotes Artifact resources)
      |     +-- .run_job(defn)         -> JobView  (submit + wait + on_success)
      +-- ModelView           (ResourceView specialised for Model; adds tracked-file context)
      |     +-- .current_revision_id / .pinned_revision_id
      |     +-- .get_jobs() / .get_configurations()
      +-- LineageNode         (one revision in a backward lineage tree)
            +-- .step          'upload' | 'job_run' | 'promotion' | 'derived'
            +-- .parents       list[LineageNode]  (recursive)
            +-- .walk() / .print_tree()

Quick start
-----------
    from istari_fluent import IstariPlatform

    platform = IstariPlatform.from_env()

    # System -> baseline -> configuration -> models -> jobs
    system = platform.get_system("Berserker")
    for model in system.baseline.configuration.get_models():
        for job in model.get_jobs():
            print(job.function_name, job.status, job.model_revision_id)

    # Browse all configurations
    for cfg in system.configurations:
        print(cfg.name, len(cfg.get_models()))

    # Find a model globally, submit a job, inspect outputs
    model = platform.find_model(name="My Model")
    job = model.submit_job(JobDefinition(...)).wait().on_success()
    for p in job.get_products():
        rev = p.revision                  # exact revision the agent wrote
        print(rev.name, rev.file_id, rev.id)

    # Chain jobs: feed a product from job 1 into job 2 as a source
    src = job.find_product(filename="named_cells.json").as_source()
    job2 = model.submit_job(JobDefinition(..., sources=[src]))

    # Trace how a model was created (upload / job_run / promotion / derived)
    model.get_lineage().print_tree()

    # Upload a model and add it to the baseline configuration
    model = platform.upload_model("model.mdzip", external_id="ext-123")
    cfg = system.baseline.configuration
    new_cfg = cfg.add_file(model.file_id).save()         # auto-name: "v3" -> "v4"
    new_cfg = cfg.add_file(f1).add_file(f2).save("v5")   # explicit name
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from pydantic import BaseModel, Field
from istari_digital_client.client import Client as IstariClient
from istari_digital_client.v2.models import (
    Model, File, FileRevision, System, Job,
    Snapshot, SystemConfiguration, TrackedFile,
    NewTrackedFile, NewSystemConfiguration, TrackedFileSpecifierType,
    UpdateTag,
)
from istari_digital_client import JobStatusName


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _paginate_manually(
    list_func: Callable[..., Any],
    page_size: int = 100,
    **kwargs: Any,
) -> list[Any]:
    """Iterate through Page objects that lack ``iter_items()``."""
    page = 1
    items: list[Any] = []
    while True:
        current = list_func(page=page, size=page_size, **kwargs)
        if not current.items:
            break
        items.extend(current.items)
        if current.pages and page >= current.pages:
            break
        page += 1
    return items


def _latest_revision(model: Model):
    """Return the most recent FileRevision of a model, or None."""
    if model.file and model.file.revisions:
        return model.file.revisions[-1]
    return None


def _model_display_name(model: Model) -> str:
    rev = _latest_revision(model)
    if rev:
        return rev.display_name or rev.name or model.id
    return model.id


def _next_config_name(current: str) -> str:
    """Increment trailing digits in a config name, or append a timestamp.

    ``'v12'`` -> ``'v13'``, ``'config_3'`` -> ``'config_4'``,
    ``'baseline'`` -> ``'baseline_20260306_143022'``
    """
    from datetime import datetime

    m = re.match(r"^(.*?)(\d+)$", current)
    if m:
        return f"{m.group(1)}{int(m.group(2)) + 1}"
    return f"{current}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


# ---------------------------------------------------------------------------
# Data objects
# ---------------------------------------------------------------------------

class JobDefinition(BaseModel):
    """Parameters needed to submit a job.

    Use ``parameters`` for simple key-value function parameters::

        JobDefinition(
            function="@sysml:cameo_to_sysml",
            tool_name="sysml-tool",
            parameters={"output_filename": "model.sysml"},
        )

    The agent wraps each value into ``{"type": "parameter", "value": ...}``
    before writing the module's input file -- do NOT pre-wrap here.

    Use ``input_json_data`` for raw/structured payloads (e.g. nTop inputs).
    Both are merged if provided together.
    """
    function: str = Field(description="Function to execute, e.g. '@istari:extract'")
    tool_name: str = Field(description="Tool name, e.g. 'dassault_cameo'")
    tool_version: str | None = Field(default=None, description="Tool version, e.g. '2024x Refresh2'")
    operating_system: str | None = Field(default=None, description="OS name, e.g. 'RHEL 8'")
    input_json_data: dict | None = Field(default=None, description="Raw/structured input payload")
    parameters: dict | None = Field(default=None, description="Simple key-value parameters (plain values)")

    def build_parameters(self) -> dict | None:
        """Merge input_json_data and parameters into the final payload.

        Values are passed as-is. The agent handles wrapping them into
        ``{"type": "parameter", "value": ...}`` for the module.
        """
        result = dict(self.input_json_data or {})
        result.update(self.parameters or {})
        return result or None


# ---------------------------------------------------------------------------
# LineageNode  --  one revision in a backward lineage chain
# ---------------------------------------------------------------------------

@dataclass
class LineageNode:
    """One revision in a backward-pointing lineage tree.

    The root represents the resource's current revision; ``parents`` are the
    revisions that fed into it (its ``sources``), recursively, until either
    an upload (no sources) or ``max_depth`` is reached.

    ``step`` classifies how this revision came to exist:
        - ``upload``    : no sources -- a fresh user upload (root of a chain)
        - ``job_run``   : at least one source has resource_type == "Job"
        - ``promotion`` : at least one source has relationship_identifier ==
                          "promoted_from" (the marker that ``ResourceView.promote``
                          writes)
        - ``derived``   : has sources but doesn't match the above

    ``relationship_to_child`` is the ``Source.relationship_identifier`` from
    the perspective of the *child* node (i.e. how the child labels its link
    back to this parent).  ``None`` on the root.

    Inspect with ``print_tree()`` or iterate with ``walk()``.
    """
    revision_id: str
    file_id: str | None
    name: str | None
    display_name: str | None
    created: datetime | None
    resource_type: str | None
    resource_id: str | None
    step: str
    relationship_to_child: str | None
    parents: list[LineageNode] = field(default_factory=list)
    truncated: bool = False

    @property
    def label(self) -> str:
        return self.display_name or self.name or self.revision_id

    def __repr__(self) -> str:
        res = f"{self.resource_type}" if self.resource_type else "?"
        return (
            f"LineageNode(step={self.step!r}, {res}, "
            f"name={self.label!r}, rev={self.revision_id})"
        )

    def walk(self) -> Iterator[LineageNode]:
        """Depth-first iteration over this node and all ancestors."""
        yield self
        for p in self.parents:
            yield from p.walk()

    def print_tree(self, indent: int = 0, _is_root: bool = True) -> None:
        """Pretty-print the lineage tree to stdout (root first, parents below)."""
        prefix = "  " * indent
        res = self.resource_type or "Revision"
        when = self.created.strftime("%Y-%m-%d %H:%M") if self.created else "?"
        edge = "" if _is_root else f"  [via {self.relationship_to_child or '-'}]"
        print(f"{prefix}- {res} {self.label!r} (rev={self.revision_id}){edge}")
        print(f"{prefix}    step={self.step}  created={when}")
        if self.truncated:
            print(f"{prefix}    ... (truncated: max_depth reached)")
        for p in self.parents:
            p.print_tree(indent + 1, _is_root=False)


def _classify_step(rev: FileRevision) -> str:
    """Classify how a FileRevision came into existence from its sources."""
    sources = rev.sources or []
    if not sources:
        return "upload"
    for s in sources:
        if s.relationship_identifier == "promoted_from":
            return "promotion"
    for s in sources:
        if s.resource_type == "Job":
            return "job_run"
    return "derived"


def _build_lineage_node(
    client: IstariClient,
    rev: FileRevision,
    *,
    relationship_to_child: str | None,
    max_depth: int,
    depth: int,
    cache: dict[str, LineageNode],
) -> LineageNode:
    """Recursively build a LineageNode tree from a FileRevision.

    ``cache`` memoizes by revision id so a diamond-shaped DAG is built once.
    Stops descending at ``max_depth``; deeper nodes are returned with
    ``truncated=True`` and no parents.
    """
    if rev.id in cache:
        cached = cache[rev.id]
        return cached

    resource_type = None
    resource_id = None
    if rev.file_id:
        try:
            f = client.get_file(rev.file_id)
            resource_type = getattr(f, "resource_type", None)
            resource_id = getattr(f, "resource_id", None)
        except Exception:
            pass

    node = LineageNode(
        revision_id=rev.id,
        file_id=rev.file_id,
        name=rev.name,
        display_name=rev.display_name,
        created=rev.created,
        resource_type=resource_type,
        resource_id=resource_id,
        step=_classify_step(rev),
        relationship_to_child=relationship_to_child,
        parents=[],
        truncated=False,
    )
    cache[rev.id] = node

    if depth >= max_depth:
        node.truncated = bool(rev.sources)
        return node

    for src in rev.sources or []:
        try:
            parent_rev = client.get_revision(src.revision_id)
        except Exception:
            continue
        parent = _build_lineage_node(
            client,
            parent_rev,
            relationship_to_child=src.relationship_identifier,
            max_depth=max_depth,
            depth=depth + 1,
            cache=cache,
        )
        node.parents.append(parent)

    return node


# ---------------------------------------------------------------------------
# Shared promotion helper
# ---------------------------------------------------------------------------

def _promote_revision_to_model(
    client: IstariClient,
    rev: FileRevision,
    *,
    display_name: str | None = None,
    filename: str | None = None,
    external_identifier: str | None = None,
    relationship_identifier: str | None = "promoted_from",
) -> Model:
    """Download a FileRevision and re-upload it as a standalone Model.

    The new Model records ``rev`` as a source, preserving provenance.  Used
    both by explicit ``ResourceView.promote()`` and by the auto-promotion
    performed when running a job on an Artifact resource.
    """
    from istari_digital_client.v2.models.new_source import NewSource

    content = client.read_contents(token=rev.content_token)
    upload_name = filename or rev.name or rev.id
    upload_path = Path(upload_name)
    suffix = upload_path.suffix or rev.suffix or ""

    if display_name:
        name = display_name
    elif filename:
        name = upload_path.stem
    else:
        name = rev.display_name or rev.name or rev.id
    if suffix and name.endswith(suffix):
        name = name[: -len(suffix)]

    tmp_dir = tempfile.mkdtemp(prefix="istari_promote_")
    tmp_path = os.path.join(tmp_dir, upload_name)
    try:
        with open(tmp_path, "wb") as f:
            f.write(content)
        model = client.add_model(
            path=tmp_path,
            display_name=name,
            external_identifier=external_identifier,
            sources=[NewSource(
                revision_id=rev.id,
                relationship_identifier=relationship_identifier,
            )],
        )
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        if os.path.isdir(tmp_dir):
            os.rmdir(tmp_dir)
    return model


# ---------------------------------------------------------------------------
# ResourceView  --  unified wrapper over any Resource (Artifact/Model/Job/...)
# ---------------------------------------------------------------------------

@dataclass
class ResourceView:
    """Wraps a platform Resource (Artifact, Model, Job, ...) with an optional
    revision pin.

    A ``ResourceView`` plays two roles depending on whether ``_pinned_revision``
    is set:

    - **Unpinned** (default): represents the resource in its current state.
      ``revision`` returns the latest revision of the resource's file.
    - **Pinned**: represents the resource *as of* a specific revision.  This
      is how products behave -- every ``ResourceView`` returned by
      ``JobView.get_products()`` carries the exact revision the agent wrote,
      even if newer revisions land later (race-safe).

        # Unpinned -- current state
        artifact = platform.get_resource("Artifact", "art-1")
        artifact.name, artifact.read_bytes()      # from latest revision

        # Pinned -- the output of a specific job
        report = job.find_product(filename="report.json")   # pinned ResourceView
        report.read_text()                        # the bytes that job wrote
        report.as_source()                        # NewSource for chaining

    Running a job:

        # On a Model -- direct
        model.run_job(definition)

        # On an Artifact -- auto-promoted to a Model first
        artifact.run_job(definition)
    """
    _resource: Any = field(repr=False)
    _client: IstariClient = field(repr=False)
    _pinned_revision: FileRevision | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        mime = self.mime or "?"
        pin = f", pinned_rev={self._pinned_revision.id}" if self._pinned_revision else ""
        return f"{self.type}({self.name!r}, id={self.id}, mime={mime}{pin})"

    # -- identity -----------------------------------------------------------

    @property
    def id(self) -> str | None:
        return getattr(self._resource, "id", None)

    @property
    def type(self) -> str:
        """Concrete resource type name (e.g. ``'Model'``, ``'Artifact'``, ``'Job'``)."""
        return type(self._resource).__name__

    @property
    def raw(self) -> Any:
        return self._resource

    @property
    def file(self) -> File | None:
        """The ``File`` backing this resource (if any)."""
        return getattr(self._resource, "file", None)

    @property
    def file_id(self) -> str | None:
        f = self.file
        return f.id if f else None

    # -- revision handling --------------------------------------------------

    @property
    def latest_revision(self) -> FileRevision | None:
        """Most recent ``FileRevision`` of the resource's file."""
        f = self.file
        if f and f.revisions:
            return f.revisions[-1]
        return None

    @property
    def revision(self) -> FileRevision | None:
        """The effective revision: pinned if set, else latest."""
        return self._pinned_revision or self.latest_revision

    @property
    def revision_id(self) -> str | None:
        rev = self.revision
        return rev.id if rev else None

    @property
    def is_pinned(self) -> bool:
        return self._pinned_revision is not None

    def pin(self, revision: FileRevision | str) -> ResourceView:
        """Return a new view pinned to a specific revision (fetches if given an id)."""
        if isinstance(revision, str):
            revision = self._client.get_revision(revision)
        return type(self)(
            _resource=self._resource,
            _client=self._client,
            _pinned_revision=revision,
        )

    @property
    def unpinned(self) -> ResourceView:
        """Return a copy of this view without the revision pin."""
        return type(self)(
            _resource=self._resource,
            _client=self._client,
            _pinned_revision=None,
        )

    # -- content (operates on self.revision) --------------------------------

    @property
    def name(self) -> str:
        rev = self.revision
        if rev:
            return rev.display_name or rev.name or rev.id
        return self.id or ""

    @property
    def filename(self) -> str | None:
        """Filename of the effective revision, including extension."""
        rev = self.revision
        return rev.name if rev else None

    @property
    def mime(self) -> str | None:
        rev = self.revision
        return rev.mime if rev else None

    def read_bytes(self) -> bytes:
        """Return the effective revision's content as raw bytes."""
        rev = self.revision
        if rev is None:
            raise ValueError(f"{self.type} {self.id} has no revision to read")
        return self._client.read_contents(token=rev.content_token)

    def read_text(self, encoding: str = "utf-8") -> str:
        return self.read_bytes().decode(encoding)

    def download(self, dest: str | Path) -> Path:
        """Download the effective revision's content to a local path.

        *dest* can be a file path or a directory.  When a directory is given
        the revision's original filename is used.
        """
        dest = Path(dest)
        if dest.is_dir():
            dest = dest / (self.filename or self.id or "download")
        dest.write_bytes(self.read_bytes())
        return dest

    # -- provenance / chaining ---------------------------------------------

    def as_source(self, relationship_identifier: str | None = None) -> Any:
        """Return a ``NewSource`` pointing to this view's effective revision.

        Use to chain operations: feed a product from job N into job N+1 (or
        into a new model/artifact) as a source.  Uses ``self.revision.id`` so
        pinned views produce race-safe sources.

            sources = [job1.find_product(filename="named_cells.json").as_source()]
            job2 = model.submit_job(JobDefinition(..., sources=sources))
        """
        from istari_digital_client.v2.models.new_source import NewSource
        rev = self.revision
        if rev is None:
            raise ValueError(f"{self.type} {self.id} has no revision to reference")
        return NewSource(
            revision_id=rev.id,
            relationship_identifier=relationship_identifier,
        )

    def get_lineage(self, max_depth: int = 10) -> LineageNode | None:
        """Return the backward lineage tree rooted at this view's effective revision.

        Walks ``revision.sources`` recursively, classifying each step as
        ``upload``, ``job_run``, ``promotion``, or ``derived``.  Returns
        ``None`` if the resource has no revisions.

        Pinned views trace the lineage of the exact revision they point to;
        unpinned views trace the latest revision.
        """
        rev = self.revision
        if rev is None:
            return None
        return _build_lineage_node(
            self._client, rev,
            relationship_to_child=None,
            max_depth=max_depth,
            depth=0,
            cache={},
        )

    def promote(
        self,
        display_name: str | None = None,
        filename: str | None = None,
        external_identifier: str | None = None,
        relationship_identifier: str | None = "promoted_from",
    ) -> ModelView:
        """Promote this view's effective revision to a standalone Model.

        The new Model records the source revision so ``get_lineage()`` can
        classify the step as a promotion.

        *display_name* sets the human-readable model name (defaults to the
        revision's display name).  *filename* controls the stored file name
        (defaults to the revision's original filename).

        ``relationship_identifier`` defaults to ``"promoted_from"``; pass
        ``None`` to omit the label.
        """
        rev = self.revision
        if rev is None:
            raise ValueError(f"{self.type} {self.id} has no revision to promote")
        model = _promote_revision_to_model(
            self._client, rev,
            display_name=display_name,
            filename=filename,
            external_identifier=external_identifier,
            relationship_identifier=relationship_identifier,
        )
        return ModelView(_resource=model, _client=self._client)

    # -- execution ----------------------------------------------------------

    def submit_job(
        self,
        definition: JobDefinition,
        save_input: bool = False,
        save_input_as_revision: bool = False,
        sources: list[Any] | None = None,
        promotion_relationship: str | None = "promoted_from",
    ) -> JobView:
        """Submit a job against this resource and return a ``JobView`` (async).

        Dispatch rules:
            - Model    -> submit directly (the platform accepts model_id)
            - Artifact -> auto-promote the effective revision to a Model,
                          then submit.  The promotion creates a new Model
                          with ``relationship_identifier=promotion_relationship``
                          so ``get_lineage()`` recognises it.
            - Other    -> raises ``TypeError``.

        ``sources`` -- optional list of ``NewSource`` objects (use
        ``ResourceView.as_source()`` to build them) attached to the job so the
        platform records the provenance link.

        ``save_input_as_revision`` is incompatible with the Artifact path
        (each auto-promoted model is single-use) and raises ``ValueError``.
        """
        rtype = self.type
        if rtype == "Model":
            job = _submit_job_impl(
                self._client, self.id, definition,
                save_input=save_input,
                save_input_as_revision=save_input_as_revision,
                extra_sources=sources,
            )
            return JobView(_job=job, _client=self._client)

        if rtype == "Artifact":
            if save_input_as_revision:
                raise ValueError(
                    "save_input_as_revision is not supported when running a job "
                    "on an Artifact (the auto-promoted model is single-use). "
                    "Promote explicitly first if you need this behaviour."
                )
            rev = self.revision
            if rev is None:
                raise ValueError(f"Artifact {self.id} has no revision to promote")
            promoted = _promote_revision_to_model(
                self._client, rev,
                relationship_identifier=promotion_relationship,
            )
            job = _submit_job_impl(
                self._client, promoted.id, definition,
                save_input=save_input,
                save_input_as_revision=False,
                extra_sources=sources,
            )
            return JobView(_job=job, _client=self._client)

        raise TypeError(
            f"Cannot run a job on resource of type {rtype!r}; "
            "only 'Model' and 'Artifact' are supported."
        )

    def run_job(
        self,
        definition: JobDefinition,
        timeout: int = 3600,
        save_input: bool = False,
        save_input_as_revision: bool = False,
        sources: list[Any] | None = None,
        poll_interval: int = 5,
        promotion_relationship: str | None = "promoted_from",
    ) -> JobView:
        """Submit, wait, and return the completed ``JobView``.

        Same dispatch rules as ``submit_job``.  Raises ``RuntimeError`` if the
        job fails or times out.
        """
        jv = self.submit_job(
            definition,
            save_input=save_input,
            save_input_as_revision=save_input_as_revision,
            sources=sources,
            promotion_relationship=promotion_relationship,
        )
        return jv.wait(timeout=timeout, poll_interval=poll_interval).on_success()


def _make_resource_view(
    resource: Any,
    client: IstariClient,
    *,
    pinned_revision: FileRevision | None = None,
) -> ResourceView:
    """Factory: return a ``ModelView`` for Model resources, else ``ResourceView``.

    Used wherever we turn an SDK resource object into a view; keeps call sites
    from having to type-check the resource themselves.
    """
    if isinstance(resource, Model):
        return ModelView(_resource=resource, _client=client, _pinned_revision=pinned_revision)
    return ResourceView(_resource=resource, _client=client, _pinned_revision=pinned_revision)


# ---------------------------------------------------------------------------
# JobView
# ---------------------------------------------------------------------------

@dataclass
class JobView:
    """
    Wraps a Job with convenience properties.

        job = model.get_jobs()[0]
        print(job.function_name, job.status, job.created)
        print(job.model_revision_id)          # revision the job ran on
        products = job.wait().on_success().get_products()
        report = job.find_product(name="report.json")
    """
    _job: Job = field(repr=False)
    _client: IstariClient = field(repr=False)

    def __repr__(self) -> str:
        return f"Job({self.function_name!r}, status={self.status!r}, created={self.created!r}, id={self.id})"

    @property
    def id(self) -> str:
        return self._job.id

    @property
    def raw(self) -> Job:
        return self._job

    @property
    def function_name(self) -> str:
        return self._job.function.name if self._job.function else "?"

    @property
    def status(self) -> str:
        latest = self._job.status_history[-1] if self._job.status_history else None
        return latest.name.value if latest else "unknown"

    @property
    def completed(self) -> bool:
        return self._job.status.name == JobStatusName.COMPLETED

    @property
    def failed(self) -> bool:
        return self._job.status.name == JobStatusName.FAILED

    @property
    def created(self) -> str:
        return self._job.created.strftime("%Y-%m-%d %H:%M") if self._job.created else ""

    @property
    def model_revision_id(self) -> str | None:
        """The model file-revision that was used as input."""
        if self._job.file and self._job.file.revisions:
            for rev in self._job.file.revisions:
                for src in rev.sources or []:
                    if src.resource_type == "Model":
                        return src.revision_id
        return None

    # -- actions ------------------------------------------------------------

    def wait(self, timeout: int = 3600, poll_interval: int = 5) -> JobView:
        """Block until the job reaches a terminal state or *timeout* seconds elapse.

        Returns ``self`` so calls can be chained::

            products = job.wait().on_success().get_products()
        """
        start = time.time()
        while True:
            job = self._client.get_job(self.id)
            self._job = job
            elapsed = time.time() - start

            if job.status.name == JobStatusName.PENDING:
                msg = job.status.message
                if msg and "None agent" in msg:
                    raise RuntimeError(f"No agent available: {msg}")

            if job.status.name in (JobStatusName.COMPLETED, JobStatusName.FAILED):
                return self

            if elapsed > timeout:
                return self

            time.sleep(poll_interval)

    def get_input(self) -> dict:
        """Return the input parameters submitted with this job.

        The job's file revision stores the parameter JSON that was uploaded
        by ``add_job``.  Returns the parsed dict, e.g.::

            {"output_filename": "model.sysml", "dry_run": "true"}
        """
        return self._job.read_json() or {}

    def get_input_text(self) -> str:
        """Return the raw input parameters as a JSON string."""
        return self._job.read_text()

    def on_success(self) -> JobView:
        """Return ``self`` if the job completed, raise otherwise.

        Designed for chaining after ``wait()``::

            job.wait().on_success().get_products()
        """
        if self.completed:
            return self
        raise RuntimeError(f"Job {self.id} did not complete (status={self.status})")

    @property
    def revision(self) -> FileRevision | None:
        """Latest ``FileRevision`` of the job's output file (if any).

        The agent records every output it writes as a ``Product`` on this
        revision, so ``self.revision.products`` is the source of truth for
        what the job produced.
        """
        if self._job.file and self._job.file.revisions:
            return self._job.file.revisions[-1]
        return None

    def get_products(self, *, resource_type: str | None = None) -> list[ResourceView]:
        """Return products generated by this job as pinned ``ResourceView``s.

        Reads ``job.revision.products`` -- each ``Product`` points to the exact
        ``FileRevision`` the agent wrote.  Each returned view is **pinned** to
        that revision so the result stays race-safe even after newer revisions
        land on the same artifact file.

        ``resource_type`` (e.g. ``"Artifact"``) filters by the owning resource.
        """
        job = self._client.get_job(self.id)
        self._job = job
        rev = self.revision
        if rev is None or not rev.products:
            return []
        products = rev.products
        if resource_type:
            products = [p for p in products if p.resource_type == resource_type]

        views: list[ResourceView] = []
        for p in products:
            if not p.resource_type or not p.resource_id:
                continue
            try:
                r = self._client.get_resource(p.resource_type, p.resource_id)
            except Exception:
                continue
            if r is None:
                continue
            try:
                pinned_rev = self._client.get_revision(p.revision_id)
            except Exception:
                pinned_rev = None
            views.append(_make_resource_view(r, self._client, pinned_revision=pinned_rev))
        return views

    def find_product(
        self,
        *,
        name: str | None = None,
        filename: str | None = None,
        resource_type: str | None = None,
    ) -> ResourceView | None:
        """Find a product by display name, filename, and/or resource type.

        Returns a pinned ``ResourceView`` (see ``get_products``), or ``None``.

        ``name`` matches against the revision's display name or file name.
        ``filename`` matches the revision's actual filename (``rev.name``).
        ``resource_type`` restricts the search to e.g. ``"Artifact"``.
        """
        if not name and not filename:
            raise ValueError("Provide name or filename")
        for p in self.get_products(resource_type=resource_type):
            if name and p.name == name:
                return p
            if filename and p.filename == filename:
                return p
        return None

    def attach_file(
        self,
        file_path: str | Path,
        display_name: str,
        as_model: bool = False,
        external_id: str | None = None,
    ) -> JobView:
        """Upload a file and attach it as a source to this job."""
        from istari_digital_client.v2.models.new_source import NewSource

        file_path = Path(file_path)
        if as_model:
            if not external_id:
                raise ValueError("external_id required when as_model=True")
            model = self._client.add_model(
                path=file_path,
                external_identifier=external_id,
                display_name=display_name,
            )
            uploaded_file = model.file
        else:
            uploaded_file = self._client.add_file(path=file_path, display_name=display_name)

        rev_id = uploaded_file.revision.id
        source = NewSource(revision_id=rev_id)

        suffix = file_path.suffix or ".json"
        is_binary = suffix.lower() in [".xls", ".xlsx", ".bin", ".ntop"]
        mode = "rb" if is_binary else "r"
        wmode = "wb" if is_binary else "w"

        with tempfile.NamedTemporaryFile(mode=wmode, suffix=suffix, delete=False) as tmp:
            with open(file_path, mode) as src:
                tmp.write(src.read())
            tmp_path = tmp.name

        try:
            updated = self._client.update_job(job_id=self.id, path=tmp_path, sources=[source])
            self._job = updated
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        return self


# ---------------------------------------------------------------------------
# ModelView  --  ResourceView specialised for Model resources
# ---------------------------------------------------------------------------

@dataclass
class ModelView(ResourceView):
    """
    ``ResourceView`` specialisation for Model resources.

    Adds Model-only affordances -- tracked-file context, job listing, and
    configuration membership -- on top of the generic resource API (content
    access, lineage, promotion, job submission).

        model = platform.find_model(name="My Model")
        model.name                         # display name from latest revision
        model.current_revision_id          # from TrackedFile if available
        jobs = model.get_jobs()
        job  = model.submit_job(definition)
    """
    _tracked_file: TrackedFile | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        parts = [f"Model({self.name!r}"]
        if self.filename and self.filename != self.name:
            parts.append(f"filename={self.filename!r}")
        parts.append(f"id={self.id}")
        if self.file_id:
            parts.append(f"file={self.file_id}")
        if self.current_revision_id:
            parts.append(f"rev={self.current_revision_id}")
        return ", ".join(parts) + ")"

    __str__ = __repr__

    @property
    def current_revision_id(self) -> str | None:
        """Revision this Model is currently pinned to via a TrackedFile, if any.

        Falls back to the latest revision when no TrackedFile context is set.
        """
        if self._tracked_file:
            return self._tracked_file.current_file_revision_id
        rev = self.latest_revision
        return rev.id if rev else None

    @property
    def pinned_revision_id(self) -> str | None:
        """Tracked-file pinned revision id, if this view carries TrackedFile context."""
        if self._tracked_file:
            return self._tracked_file.pinned_file_revision_id
        return None

    # -- queries ------------------------------------------------------------

    def get_jobs(self, size: int = 100) -> list[JobView]:
        page = self._client.list_model_jobs(self.id, size=size)
        return [JobView(_job=j, _client=self._client) for j in page.iter_items()]

    def get_configurations(self) -> list[tuple[System, SystemConfiguration]]:
        """Find every (system, configuration) that tracks this model."""
        if not self._resource.file:
            return []
        file_id = self._resource.file.id
        results: list[tuple[System, SystemConfiguration]] = []
        for system in _paginate_manually(self._client.list_systems):
            for cfg in system.configurations or []:
                try:
                    page = self._client.list_tracked_files(configuration_id=cfg.id)
                    for tf in page.iter_items():
                        if tf.file_id == file_id:
                            results.append((system, cfg))
                            break
                except Exception:
                    continue
        return results


# ---------------------------------------------------------------------------
# TrackedFileSet  --  builder for new configurations
# ---------------------------------------------------------------------------

class TrackedFileSet:
    """Mutable builder that collects tracked files and creates a configuration.

        cfg.add_file(file_id_a).add_file(file_id_b).save("v4")

        # auto-name: "v3" -> "v4"
        cfg.add_file(file_id_a).save()

        # upload a local file as a model and track it in one step
        cfg.add_file(path="model.mdzip", display_name="My Model").save()
    """

    def __init__(
        self,
        system_id: str,
        base_name: str,
        client: IstariClient,
        tracked: list[TrackedFile] | None = None,
    ):
        self._system_id = system_id
        self._base_name = base_name
        self._client = client
        self._entries: list[NewTrackedFile] = []
        for tf in tracked or []:
            if tf.specifier_type == TrackedFileSpecifierType.LOCKED:
                self._entries.append(NewTrackedFile(
                    specifier_type=TrackedFileSpecifierType.LOCKED,
                    file_id=tf.file_id,
                    pinned_file_revision_id=tf.pinned_file_revision_id or tf.current_file_revision_id,
                ))
            else:
                self._entries.append(NewTrackedFile(
                    specifier_type=TrackedFileSpecifierType.LATEST,
                    file_id=tf.file_id,
                ))

    def add_file(
        self,
        file_id: str | None = None,
        *,
        path: str | Path | None = None,
        display_name: str | None = None,
        external_identifier: str | None = None,
        version_name: str | None = None,
    ) -> TrackedFileSet:
        """Track a file at its latest revision. Returns ``self`` for chaining.

        Either provide *file_id* for an already-uploaded file, or *path* to
        upload a local file as a model first.
        """
        if path is not None:
            model = self._client.add_model(
                path=path,
                display_name=display_name,
                external_identifier=external_identifier,
                version_name=version_name,
            )
            file_id = model.file.id
        if file_id is None:
            raise ValueError("file_id or path required")
        self._entries.append(NewTrackedFile(
            specifier_type=TrackedFileSpecifierType.LATEST,
            file_id=file_id,
        ))
        return self

    def add_revision(self, file_id: str, revision_id: str) -> TrackedFileSet:
        """Track a file pinned to a specific revision. Returns ``self`` for chaining."""
        self._entries.append(NewTrackedFile(
            specifier_type=TrackedFileSpecifierType.LOCKED,
            file_id=file_id,
            pinned_file_revision_id=revision_id,
        ))
        return self

    def add_product_as_model(
        self,
        product: ResourceView,
        display_name: str | None = None,
        filename: str | None = None,
        external_identifier: str | None = None,
    ) -> TrackedFileSet:
        """Promote a product (pinned ``ResourceView``) to a model and track it.

        Returns ``self`` for chaining.
        """
        mv = product.promote(display_name=display_name, filename=filename, external_identifier=external_identifier)
        self._entries.append(NewTrackedFile(
            specifier_type=TrackedFileSpecifierType.LATEST,
            file_id=mv.file_id,
        ))
        return self

    def save(self, name: str | None = None) -> ConfigurationView:
        """Create the new configuration on the system.

        If *name* is omitted, one is derived from the source configuration:
        trailing digits are incremented (``v3`` -> ``v4``), otherwise a
        timestamp is appended.
        """
        config_name = name or _next_config_name(self._base_name)
        new_cfg = self._client.create_configuration(
            system_id=self._system_id,
            new_system_configuration=NewSystemConfiguration(
                name=config_name,
                tracked_files=self._entries,
            ),
        )
        return ConfigurationView(_config=new_cfg, _client=self._client)

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"TrackedFileSet({len(self._entries)} files, base={self._base_name!r})"


# ---------------------------------------------------------------------------
# ConfigurationView
# ---------------------------------------------------------------------------

@dataclass
class ConfigurationView:
    """
    Wraps a SystemConfiguration with access to its tracked models and files.

        config = system.baseline.configuration
        config.name                                # e.g. "v2"
        models = config.get_models()               # models tracked by this config
        files  = config.get_tracked_files()        # all tracked files

        # build a new configuration from this one
        config.add_file(fid1).add_file(fid2).save("v3")
        config.add_file(fid1).save()               # auto-name: "v2" -> "v3"
    """
    _config: SystemConfiguration = field(repr=False)
    _client: IstariClient = field(repr=False)
    _models: list[ModelView] | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return f"Configuration({self.name!r}, id={self.id})"

    @property
    def id(self) -> str:
        return self._config.id

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def raw(self) -> SystemConfiguration:
        return self._config

    def get_models(self) -> list[ModelView]:
        """Models tracked by this configuration."""
        if self._models is None:
            page = self._client.list_tracked_files(self._config.id, size=100)
            self._models = []
            for tf in page.iter_items():
                if tf.resource_id:
                    model = self._client.get_model(tf.resource_id)
                    self._models.append(ModelView(_resource=model, _client=self._client, _tracked_file=tf))
        return self._models

    def find_model(
        self,
        *,
        name: str | None = None,
        filename: str | None = None,
        external_id: str | None = None,
    ) -> ModelView | None:
        """Find a tracked model by display name, filename, or external identifier.

        ``name`` matches against ``rev.display_name`` or ``rev.name`` (usually
        the basename without extension).  ``filename`` matches against the
        file's actual name including extension -- use this when two models share
        the same basename but differ by extension (e.g. ``model.ntop`` vs
        ``model.json``).

            model = cfg.find_model(name="My SysML Model")
            model = cfg.find_model(filename="Group3-UAS-Wing-v9.ntop")
            model = cfg.find_model(external_id="dod-safe-berserker")
        """
        if not name and not filename and not external_id:
            raise ValueError("Provide name, filename, or external_id")
        page = self._client.list_tracked_files(self._config.id, size=100)
        for tf in page.iter_items():
            if not tf.resource_id:
                continue
            f = self._client.get_file(tf.file_id)
            rev = f.revision
            matched = False
            if name and (rev.display_name == name or rev.name == name):
                matched = True
            if filename and rev.name == filename:
                matched = True
            if external_id and rev.external_identifier == external_id:
                matched = True
            if matched:
                model = self._client.get_model(tf.resource_id)
                return ModelView(_resource=model, _client=self._client, _tracked_file=tf)
        return None

    def get_tracked_files(self) -> list[TrackedFile]:
        """All tracked files in this configuration."""
        page = self._client.list_tracked_files(configuration_id=self._config.id)
        return list(page.iter_items())

    def add_file(
        self,
        file_id: str | None = None,
        *,
        path: str | Path | None = None,
        display_name: str | None = None,
        external_identifier: str | None = None,
        version_name: str | None = None,
    ) -> TrackedFileSet:
        """Start building a new configuration with an additional file (LATEST).

        Either provide *file_id* for an already-uploaded file, or *path* to
        upload a local file as a model first.  Returns a ``TrackedFileSet``
        pre-populated with this configuration's tracked files plus the new one.
        Chain more ``.add_file()`` calls and finish with ``.save(name)``.
        """
        return TrackedFileSet(
            system_id=self._config.system_id,
            base_name=self._config.name,
            client=self._client,
            tracked=self.get_tracked_files(),
        ).add_file(
            file_id,
            path=path,
            display_name=display_name,
            external_identifier=external_identifier,
            version_name=version_name,
        )

    def add_revision(self, file_id: str, revision_id: str) -> TrackedFileSet:
        """Start building a new configuration with a pinned revision (LOCKED)."""
        return TrackedFileSet(
            system_id=self._config.system_id,
            base_name=self._config.name,
            client=self._client,
            tracked=self.get_tracked_files(),
        ).add_revision(file_id, revision_id)

    def add_product_as_model(
        self,
        product: ResourceView,
        display_name: str | None = None,
        filename: str | None = None,
        external_identifier: str | None = None,
    ) -> TrackedFileSet:
        """Promote a product (pinned ``ResourceView``) to a model and track it in a new configuration."""
        return TrackedFileSet(
            system_id=self._config.system_id,
            base_name=self._config.name,
            client=self._client,
            tracked=self.get_tracked_files(),
        ).add_product_as_model(product, display_name=display_name, filename=filename, external_identifier=external_identifier)

    def set_baseline(self) -> ConfigurationView:
        """Move the system's baseline tag to this configuration's snapshot.

        Returns ``self`` so the call can be chained::

            cfg.add_file(fid).save("v5").set_baseline()
        """
        system_id = self._config.system_id
        snapshots = _paginate_manually(
            self._client.list_snapshots,
            configuration_id=self._config.id,
        )
        if not snapshots:
            raise ValueError(f"No snapshot found for configuration {self._config.id}")
        snapshot = snapshots[0]
        baseline = self._client.get_system_baseline(system_id)
        self._client.update_tag(baseline.tag_id, UpdateTag(snapshot_id=snapshot.id))
        return self


# ---------------------------------------------------------------------------
# SnapshotView
# ---------------------------------------------------------------------------

@dataclass
class SnapshotView:
    """
    Wraps a Snapshot with access to its parent system's configuration.

        system.baseline.id                          # snapshot id
        system.baseline.configuration               # ConfigurationView
        system.baseline.configuration.get_models()  # models in that config
    """
    _snapshot: Snapshot = field(repr=False)
    _system: System = field(repr=False)
    _client: IstariClient = field(repr=False)

    def __repr__(self) -> str:
        return f"Snapshot(config={self.configuration_id}, id={self.id})"

    @property
    def id(self) -> str:
        return self._snapshot.id

    @property
    def raw(self) -> Snapshot:
        return self._snapshot

    @property
    def configuration_id(self) -> str:
        return self._snapshot.configuration_id

    @property
    def configuration(self) -> ConfigurationView:
        """The configuration that produced this snapshot."""
        cfg_id = self._snapshot.configuration_id
        match = next((c for c in self._system.configurations or [] if c.id == cfg_id), None)
        if match is None:
            raise ValueError(f"Configuration {cfg_id} not in system")
        return ConfigurationView(_config=match, _client=self._client)


# ---------------------------------------------------------------------------
# SystemView
# ---------------------------------------------------------------------------

@dataclass
class SystemView:
    """
    Fluent wrapper: System -> baseline -> configuration -> models.

        system = platform.get_system("Berserker")
        system.baseline.configuration.name         # config behind the baseline
        system.baseline.configuration.get_models()  # models from that config
        system.configurations                      # all configs on the system
    """
    _system: System = field(repr=False)
    _client: IstariClient = field(repr=False)

    def __repr__(self) -> str:
        return f"System({self.name!r}, id={self.id})"

    @property
    def id(self) -> str:
        return self._system.id

    @property
    def name(self) -> str:
        return self._system.name

    @property
    def raw(self) -> System:
        return self._system

    @property
    def baseline(self) -> SnapshotView:
        self._system = self._client.get_system(self._system.id)
        snap = self._client.get_snapshot(self._system.baseline_tagged_snapshot_id)
        return SnapshotView(_snapshot=snap, _system=self._system, _client=self._client)

    @property
    def configurations(self) -> list[ConfigurationView]:
        """All configurations on this system."""
        return [ConfigurationView(_config=c, _client=self._client) for c in self._system.configurations or []]

    # -- mutations ----------------------------------------------------------

    def add_file(self, file_id: str, configuration_name: str | None = None) -> SystemConfiguration:
        """Track a file (latest revision) by creating a new configuration."""
        return _add_tracked_file(self._client, self._system.id, file_id=file_id, config_name=configuration_name)

    def add_revision(self, revision_id: str, configuration_name: str | None = None) -> SystemConfiguration:
        """Track a pinned file revision by creating a new configuration."""
        return _add_tracked_file(self._client, self._system.id, revision_id=revision_id, config_name=configuration_name)


# ---------------------------------------------------------------------------
# IstariPlatform  --  top-level entry point
# ---------------------------------------------------------------------------

class IstariPlatform:
    """
    Entry point that hides the flat ``Client`` API behind entity-oriented methods.

        platform = IstariPlatform.from_env()

        system = platform.get_system("Berserker")
        model  = platform.find_model(name="MQ-99 SFR")
        model  = platform.get_model("uuid-here")
    """

    def __init__(self, client: IstariClient):
        self._client = client

    def __repr__(self) -> str:
        url = getattr(self._client, '_registry_url', None) or '?'
        return f"IstariPlatform(url={url!r})"

    @classmethod
    def from_env(cls, dotenv_path: str = ".env") -> IstariPlatform:
        """Create from ``ISTARI_REGISTRY_URL`` and ``ISTARI_PERSONAL_ACCESS_TOKEN``.

        These are the same variable names used by the official Istari Digital
        Python client documentation.  Set them in a ``.env`` file next to your
        script/notebook, or export them in your shell.
        """
        from dotenv import load_dotenv
        from istari_digital_client.configuration import Configuration

        load_dotenv(dotenv_path)
        registry_url = os.getenv("ISTARI_REGISTRY_URL")
        token = os.getenv("ISTARI_PERSONAL_ACCESS_TOKEN")
        if not registry_url:
            raise RuntimeError(
                "ISTARI_REGISTRY_URL is not set. In the platform UI open "
                "Settings > Developer Settings, copy the Registry URL, and "
                "export it (or write it into a .env file)."
            )
        if not token:
            raise RuntimeError(
                "ISTARI_PERSONAL_ACCESS_TOKEN is not set. Create one in "
                "Settings > Developer Settings > Personal Access Tokens, "
                "and export it (or write it into a .env file)."
            )
        config = Configuration(
            registry_url=registry_url,
            registry_auth_token=token,
        )
        return cls(IstariClient(config))

    @property
    def client(self) -> IstariClient:
        return self._client

    # -- system -------------------------------------------------------------

    def get_system(self, name: str) -> SystemView:
        """
        Find a system by name and return a ``SystemView``.

            system = platform.get_system("Berserker")
            models = system.get_models()
        """
        for s in _paginate_manually(self._client.list_systems):
            if s.name == name:
                return SystemView(_system=s, _client=self._client)
        raise ValueError(f"System '{name}' not found")

    def find_system(self, name: str) -> SystemView | None:
        """Find a system by name or return ``None`` (non-raising variant of ``get_system``)."""
        for s in _paginate_manually(self._client.list_systems):
            if s.name == name:
                return SystemView(_system=s, _client=self._client)
        return None

    def get_or_create_system(
        self,
        name: str,
        description: str = "",
    ) -> SystemView:
        """Find a system by name, creating it if missing.

        Returns a ``SystemView`` either way.  Useful at the top of a notebook
        or tutorial where you don't want to fail if the system isn't there
        yet::

            system = platform.get_or_create_system(
                "UAS-Demo",
                description="Group 3 UAS demo workspace",
            )
        """
        from istari_digital_client.v2.models.new_system import NewSystem

        existing = self.find_system(name)
        if existing is not None:
            return existing
        created = self._client.create_system(
            NewSystem(name=name, description=description or f"Created by istari_fluent for '{name}'"),
        )
        return SystemView(_system=created, _client=self._client)

    # -- model --------------------------------------------------------------

    def get_job(self, job_id: str) -> JobView:
        job = self._client.get_job(job_id)
        return JobView(_job=job, _client=self._client)

    def get_model(self, model_id: str) -> ModelView:
        model = self._client.get_model(model_id)
        return ModelView(_resource=model, _client=self._client)

    def find_model(
        self,
        *,
        name: str | None = None,
        filename: str | None = None,
        external_id: str | None = None,
    ) -> ModelView | None:
        """Search for a model by name, filename, or external_id.

        ``name`` matches against the model's name/display_name (typically the
        basename without extension).  ``filename`` matches against the file's
        actual name including extension -- use this to disambiguate models that
        share a basename but have different extensions.

            model = platform.find_model(name="MQ-99 Berserker SFR SYSML Model")
            model = platform.find_model(filename="Group3-UAS-Wing-v9.ntop")
            model = platform.find_model(external_id="ext-123")
        """
        if not name and not filename and not external_id:
            raise ValueError("Provide name, filename, or external_id")
        page = self._client.list_models()
        for m in page.iter_items():
            if name and (m.name == name or getattr(m, "display_name", None) == name):
                full = self._client.get_model(m.id)
                return ModelView(_resource=full, _client=self._client)
            if filename and m.file:
                try:
                    f = self._client.get_file(m.file.id)
                    rev = f.revision
                    if rev and rev.name == filename:
                        full = self._client.get_model(m.id)
                        return ModelView(_resource=full, _client=self._client)
                except Exception:
                    continue
            if external_id and m.file:
                try:
                    f = self._client.get_file(m.file.id)
                    if getattr(f, "external_identifier", None) == external_id:
                        full = self._client.get_model(m.id)
                        return ModelView(_resource=full, _client=self._client)
                except Exception:
                    continue
        return None

    def upload_model(
        self,
        file_path: str | Path,
        external_id: str,
        display_name: str | None = None,
        sources: list[Any] | None = None,
    ) -> ModelView:
        """
        Upload a file and create a model.

            model = platform.upload_model("part.mdzip", external_id="ext-1")
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(file_path)
        model = self._client.add_model(
            path=file_path,
            external_identifier=external_id,
            display_name=display_name or file_path.stem,
            sources=sources,
        )
        return ModelView(_resource=model, _client=self._client)

    # -- file ---------------------------------------------------------------

    def find_file(self, name: str) -> File | None:
        """
        Find a file by name (raises if ambiguous).

            f = platform.find_file("input_params.json")
        """
        page = self._client.list_files()
        matches = [
            f for f in page.iter_items()
            if getattr(f.revision, "name", None) == name
            or getattr(f.revision, "display_name", None) == name
        ]
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError(f"Multiple files named '{name}': {[f.id for f in matches]}")
        return matches[0]

    # -- bulk queries -------------------------------------------------------

    def list_modules(self) -> list[str]:
        return [m.name for m in _paginate_manually(self._client.list_modules) if m.name]


# ---------------------------------------------------------------------------
# Internal implementation helpers (used by classes above)
# ---------------------------------------------------------------------------

def _submit_job_impl(
    client: IstariClient,
    model_id: str,
    defn: JobDefinition,
    save_input: bool = False,
    save_input_as_revision: bool = False,
    extra_sources: list[Any] | None = None,
) -> Job:
    """Core job-submission logic shared by ModelView and free functions."""
    from istari_digital_client.v2.models.new_source import NewSource

    sources: list[Any] = list(extra_sources or [])

    if save_input or save_input_as_revision:
        model = client.get_model(model_id)
        model_name = model.name or _model_display_name(model)
        base = Path(model_name).stem if "." in model_name else model_name
        filename = f"{base}_inputs.json"
        content = json.dumps(defn.input_json_data, indent=2) if isinstance(defn.input_json_data, dict) else defn.input_json_data

        tmp_path = os.path.join(tempfile.gettempdir(), filename)
        try:
            with open(tmp_path, "w") as f:
                f.write(content)

            rev_id = None
            if save_input_as_revision:
                existing = _find_model_by_name(client, base)
                if existing and existing.file:
                    updated = client.update_file(file_id=existing.file.id, path=tmp_path)
                    rev_id = updated.revision.id
            if rev_id is None:
                m = client.add_model(path=tmp_path, external_identifier=base, display_name=base)
                rev_id = m.file.revision.id if m.file else None

            if rev_id:
                sources.append(NewSource(revision_id=rev_id, relationship_identifier="input"))
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    return client.add_job(
        model_id=model_id,
        function=defn.function,
        tool_name=defn.tool_name,
        tool_version=defn.tool_version,
        operating_system=defn.operating_system,
        parameters=defn.build_parameters(),
        sources=sources or None,
    )


def _find_model_by_name(client: IstariClient, name: str) -> Model | None:
    page = client.list_models()
    for m in page.iter_items():
        if m.name == name or getattr(m, "display_name", None) == name:
            return m
    return None


def _add_tracked_file(
    client: IstariClient,
    system_id: str,
    file_id: str | None = None,
    revision_id: str | None = None,
    config_name: str | None = None,
) -> SystemConfiguration:
    from datetime import datetime
    from istari_digital_client.v2.models.new_tracked_file import NewTrackedFile
    from istari_digital_client.v2.models.tracked_file_specifier_type import TrackedFileSpecifierType
    from istari_digital_client.v2.models.new_system_configuration import NewSystemConfiguration

    if revision_id:
        rev = client.get_revision(revision_id)
        tf = NewTrackedFile(
            specifier_type=TrackedFileSpecifierType.LOCKED,
            file_id=rev.file_id,
            pinned_file_revision_id=revision_id,
        )
    elif file_id:
        tf = NewTrackedFile(specifier_type=TrackedFileSpecifierType.LATEST, file_id=file_id)
    else:
        raise ValueError("file_id or revision_id required")

    cfg = NewSystemConfiguration(
        name=config_name or datetime.now().strftime("config_%Y%m%d_%H%M%S"),
        tracked_files=[tf],
    )
    return client.create_configuration(system_id=system_id, new_system_configuration=cfg)
