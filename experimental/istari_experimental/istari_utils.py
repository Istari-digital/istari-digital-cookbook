"""
Istari SDK utilities -- object-oriented wrappers over the flat client API.

Entity hierarchy
----------------
    IstariPlatform           (entry point, wraps Client)
      +-- SystemView         (wraps System)
      |     +-- .baseline              -> SnapshotView
      |     +-- .configurations        -> list[ConfigurationView]
      |     +-- .add_file / .add_revision
      |     +-- .list_branches()       -> list[BranchView]
      |     +-- .get_branch(name)      -> BranchView
      |     +-- .create_branch(name, from_branch=...)  -> BranchView
      |     +-- .merge(from_branch=..., to_branch=..., message=...) -> BranchView
      +-- BranchView          (wraps SnapshotTag = Git-style branch)
      |     +-- .name / .snapshot_id / .is_baseline
      |     +-- .configuration         -> ConfigurationView (current working area)
      |     +-- .get_resources()       -> list[TrackedFile]
      |     +-- .get_subsystems()      -> list[Subsystem]
      |     +-- .add_resources(*ids)   -> self (staged)
      |     +-- .remove_resources(*ids)-> self (staged)
      |     +-- .add_subsystems(*ids)  -> self (staged)
      |     +-- .remove_subsystems(*ids) -> self (staged)
      |     +-- .commit(message)       -> self  (snapshot + advance pointer)
      |     +-- .archive() / .restore()
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
      |     +-- .get_products()        -> list[ProductView]
      |     +-- .find_product()        -> ProductView | None
      |     +-- .wait()                -> self (chainable)
      |     +-- .on_success()          -> self or raise
      |     +-- .completed / .failed   bool properties
      +-- ProductView         (wraps Product = race-safe (revision, resource) pair)
      |     +-- .revision              -> FileRevision  (exact rev the job wrote)
      |     +-- .resource              -> ResourceView | None  (owning entity)
      |     +-- .name / .filename / .mime / .file_id / .revision_id
      |     +-- .read_bytes() / .read_text() / .download(dest)
      |     +-- .promote()             -> ModelView  (revision-to-model)
      +-- ResourceView        (wraps a Resource: Artifact, Model, Job, ...)
            +-- .id / .type / .raw

Branching model
---------------
A *branch* is a ``SnapshotTag`` -- a named, mutable pointer to a ``Snapshot``.
A *commit* is a new ``Snapshot`` of a ``SystemConfiguration`` (the working area).
Configurations are immutable once created, so ``commit()`` materialises any
staged add/remove of resources or subsystems by creating a fresh configuration,
snapshotting it, and advancing the tag pointer to the new snapshot.

Naming convention: each branch's working configurations are named
``"branch:<branch_name>"`` (initial) and ``"branch:<branch_name>:<timestamp>"``
on subsequent commits to keep names unique.

Quick start
-----------
    from istari_experimental import IstariPlatform

    platform = IstariPlatform.from_env()

    # System -> baseline -> configuration -> models -> jobs
    system = platform.get_system("Berserker")
    for model in system.baseline.configuration.get_models():
        for job in model.get_jobs():
            print(job.function_name, job.status, job.model_revision_id)

    # Branches: list, create, stage, commit, merge -- all chainable
    main = system.get_branch("main")
    feature = (
        system.create_branch("feature/antenna", from_branch="main")
              .add_resources("model-uuid-1", "model-uuid-2")
              .add_subsystems("system-uuid-A")
              .commit("Add antenna model and RF subsystem")
    )
    updated_main = system.merge(
        from_branch="feature/antenna", to_branch="main", message="Merge antenna"
    )

    # Find a model globally, submit a job, inspect outputs
    model = platform.find_model(name="My Model")
    job = model.submit_job(JobDefinition(...)).wait().on_success()
    for p in job.get_products():
        rev = p.revision                  # exact revision the agent wrote
        print(rev.name, rev.file_id, rev.id)
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence, Union

from pydantic import BaseModel, Field
from istari_digital_client.client import Client as IstariClient
from istari_digital_client.v2.models import (
    Model, File, FileRevision, Product, System, Job,
    Snapshot, SnapshotTag, SnapshotTagRevision, SystemConfiguration, TrackedFile,
    NewSnapshot, NewSnapshotTag, NewTrackedFile, NewTrackedSystem,
    NewSystemConfiguration, TrackedFileSpecifierType,
    UpdateTag, Subsystem,
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


def _timestamp_suffix() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _tracked_file_to_new(tf: TrackedFile) -> NewTrackedFile:
    """Convert an existing TrackedFile into a NewTrackedFile preserving pinning."""
    if tf.specifier_type == TrackedFileSpecifierType.LOCKED:
        return NewTrackedFile(
            specifier_type=TrackedFileSpecifierType.LOCKED,
            file_id=tf.file_id,
            pinned_file_revision_id=tf.pinned_file_revision_id or tf.current_file_revision_id,
        )
    return NewTrackedFile(
        specifier_type=TrackedFileSpecifierType.LATEST,
        file_id=tf.file_id,
    )


def _is_active(entity: Any) -> bool:
    """``True`` if an SDK entity (TrackedFile, Subsystem, File, ...) is not archived.

    Handles both string archive_status (``"Active"``/``"Archived"``) and the
    ``is_archived`` boolean used by ``Subsystem``.  Only an explicit ``True``
    value of ``is_archived`` and an explicit ``"archived"`` archive_status
    (case-insensitive) flag the entity as archived; everything else is treated
    as active so this stays robust against partially-populated mocks/stubs.
    """
    if getattr(entity, "is_archived", False) is True:
        return False
    status = getattr(entity, "archive_status", None)
    if status is None:
        return True
    name = getattr(status, "name", status)
    name = getattr(name, "value", name)
    if not isinstance(name, str):
        return True
    return name.lower() != "archived"


def _active_tracked_files(
    client: IstariClient,
    tracked_files: list[TrackedFile],
    *,
    verify_files: bool = False,
) -> list[TrackedFile]:
    """Return only tracked files whose entry is active.

    The platform refuses ``create_configuration`` if any referenced file is
    archived (``"Archived files cannot be added to a configuration."``), so
    callers that copy tracked files between configurations must filter first.

    By default we only check ``TrackedFile.archive_status``; set
    ``verify_files=True`` to also fetch each underlying ``File`` and drop
    tracked files whose File is archived even though the TrackedFile reports
    Active (this costs an extra API call per file).
    """
    active_tfs = [tf for tf in tracked_files if _is_active(tf)]
    if not verify_files:
        return active_tfs
    out: list[TrackedFile] = []
    for tf in active_tfs:
        if not tf.file_id:
            continue
        try:
            f = client.get_file(tf.file_id)
        except Exception:
            out.append(tf)
            continue
        if _is_active(f):
            out.append(tf)
    return out


def _format_dropped_rows(dropped_files: list[TrackedFile], dropped_subs: list[Any], limit: int = 10) -> str:
    """Render a short bullet list explaining why each row was filtered out."""
    lines: list[str] = []
    for tf in dropped_files[:limit]:
        lines.append(
            f"    - tracked_file id={tf.id} file_id={tf.file_id} "
            f"archive_status={tf.archive_status!r}"
        )
    for s in dropped_subs[:limit]:
        is_arch = getattr(s, "is_archived", None)
        status = getattr(s, "archive_status", None)
        sid = getattr(s, "id", None) or getattr(s, "tag_id", None)
        lines.append(f"    - subsystem id={sid} is_archived={is_arch} archive_status={status!r}")
    overflow = max(0, (len(dropped_files) + len(dropped_subs)) - limit)
    if overflow:
        lines.append(f"    ... and {overflow} more")
    return "\n".join(lines)


def _create_branch_configuration(
    client: IstariClient,
    *,
    system_id: str,
    name: str,
    tracked_files: list[NewTrackedFile],
    tracked_systems: list[NewTrackedSystem],
    source_label: str,
    source_files_total: int = 0,
    source_subs_total: int = 0,
    dropped_archived_files: list[TrackedFile] | None = None,
    dropped_archived_subs: list[Any] | None = None,
) -> SystemConfiguration:
    """Wrap ``create_configuration`` with branch-aware error messages.

    Raises ``ValueError`` (instead of bubbling a raw 400) when the platform
    would reject the configuration because every tracked item was archived.
    """
    dropped_files = dropped_archived_files or []
    dropped_subs = dropped_archived_subs or []
    dropped_total = len(dropped_files) + len(dropped_subs)
    if not tracked_files and not tracked_systems:
        msg = (
            f"Cannot create configuration {name!r}: no active tracked files or "
            f"subsystems to copy from {source_label}. "
            f"Source had {source_files_total} tracked file(s) and "
            f"{source_subs_total} subsystem(s); {dropped_total} were "
            f"skipped as archived."
        )
        if source_files_total == 0 and source_subs_total == 0:
            msg += (
                " The source branch's configuration is empty -- add at least one"
                " file/revision to it (e.g. via .add_file/.add_revision on the"
                " source branch) before forking."
            )
        elif dropped_total:
            details = _format_dropped_rows(dropped_files, dropped_subs)
            msg += (
                "\n  Dropped rows:\n"
                f"{details}\n"
                "  Note: these tracking *rows* are archived even if the underlying"
                " files are still active. Restore the tracking rows via the"
                " platform UI/API, or stage fresh adds with"
                " .add_resources/.add_subsystems before retrying."
            )
        raise ValueError(msg)
    return client.create_configuration(
        system_id=system_id,
        new_system_configuration=NewSystemConfiguration(
            name=name,
            tracked_files=tracked_files,
            tracked_systems=tracked_systems,
        ),
    )


def _create_snapshot(client: IstariClient, configuration_id: str) -> Snapshot:
    """Create a snapshot of a configuration's current tracked-file state.

    The platform returns a ``NoOpResponse`` if the configuration's content is
    identical to its existing snapshot (e.g. when re-snapshotting a config that
    was just created from the same seed file as a previous snapshot). In that
    case we look up the existing snapshot and return it, so callers can treat
    "snapshot already exists" the same as "snapshot just created".
    """
    response = client.create_snapshot(
        configuration_id=configuration_id,
        new_snapshot=NewSnapshot(dry_run=False),
    )
    snap = getattr(response, "actual_instance", response)
    if getattr(snap, "id", None):
        return snap

    # NoOp (or otherwise no id): fall back to the most recent existing snapshot
    # for this configuration.
    status = getattr(snap, "status", None)
    if status == "no-op" or "NoOp" in type(snap).__name__:
        page = client.list_snapshots(configuration_id=configuration_id, size=1, sort="-created")
        existing = next(iter(page.iter_items()), None)
        if existing is not None and getattr(existing, "id", None):
            return existing
    raise RuntimeError(f"create_snapshot returned no snapshot: {response!r}")


# Accepted "resource" inputs for seeding a branch:
# - Path / str path on disk: uploaded via client.add_model(), tracked at LATEST.
#   We use add_model (not add_file) so the new tracked entry is bound to a Model
#   and shows up as a Resource in the UI. To upload only a bare File (rare),
#   pass a pre-built `NewTrackedFile` or upload separately and pass the `File`.
# - Model: tracked at LATEST by the model's file_id.
# - File: tracked at LATEST by file_id (no Model binding -- invisible in UI's
#   Resources tab unless something else binds it later).
# - FileRevision: tracked LOCKED at this revision.
# - TrackedFile: rebuilt as NewTrackedFile preserving specifier.
# - NewTrackedFile: passed through.
# - str (not a real path on disk): tried first as a Model id (preferred so the
#   tracked entry surfaces as a Resource), then as a raw file_id on lookup miss.
ResourceLike = Union[str, Path, File, Model, FileRevision, TrackedFile, NewTrackedFile]


def _resolve_seed_resources(
    client: IstariClient,
    resources: Sequence[ResourceLike] | None,
) -> list[NewTrackedFile]:
    """Convert a heterogeneous list of seed resources into ``NewTrackedFile`` rows.

    Path-like items are uploaded as **Models** (not bare Files) so the resulting
    tracked entry is a Resource in the UI. Bare ``File`` objects bypass that and
    track only the file_id.
    """
    from istari_digital_client.exceptions import NotFoundException

    out: list[NewTrackedFile] = []
    for r in resources or []:
        if isinstance(r, NewTrackedFile):
            out.append(r)
        elif isinstance(r, TrackedFile):
            out.append(_tracked_file_to_new(r))
        elif isinstance(r, FileRevision):
            out.append(NewTrackedFile(
                specifier_type=TrackedFileSpecifierType.LOCKED,
                file_id=r.file_id,
                pinned_file_revision_id=r.id,
            ))
        elif isinstance(r, Model):
            if not r.file:
                raise ValueError(f"Model {r.id!r} has no file to track")
            out.append(NewTrackedFile(
                specifier_type=TrackedFileSpecifierType.LATEST,
                file_id=r.file.id,
            ))
        elif isinstance(r, File):
            out.append(NewTrackedFile(
                specifier_type=TrackedFileSpecifierType.LATEST,
                file_id=r.id,
            ))
        elif isinstance(r, (str, Path)):
            p = Path(r)
            if p.exists():
                uploaded_model = client.add_model(path=p)
                if not uploaded_model.file:
                    raise RuntimeError(
                        f"add_model({p}) returned a model with no file: {uploaded_model.id}"
                    )
                out.append(NewTrackedFile(
                    specifier_type=TrackedFileSpecifierType.LATEST,
                    file_id=uploaded_model.file.id,
                ))
            elif isinstance(r, Path):
                raise FileNotFoundError(f"Path does not exist: {r}")
            else:
                # str: try as Model id first (preferred so tracking surfaces a
                # Resource in the UI), fall back to a raw file_id on miss.
                try:
                    model = client.get_model(r)
                except NotFoundException:
                    model = None
                if model is not None:
                    if not model.file:
                        raise ValueError(f"Model {r!r} has no file")
                    out.append(NewTrackedFile(
                        specifier_type=TrackedFileSpecifierType.LATEST,
                        file_id=model.file.id,
                    ))
                else:
                    out.append(NewTrackedFile(
                        specifier_type=TrackedFileSpecifierType.LATEST,
                        file_id=r,
                    ))
        else:
            raise TypeError(
                f"Unsupported resource type for create_branch: {type(r).__name__} "
                f"(expected str, Path, File, Model, FileRevision, TrackedFile or NewTrackedFile)"
            )
    return out


def _generate_readme_seed(
    client: IstariClient,
    branch_name: str,
    description: str | None,
) -> NewTrackedFile:
    """Upload a README.md as a Model and return a ``NewTrackedFile`` row for it.

    Used by :meth:`SystemView.create_branch` when no explicit seed source is
    given (no ``from_branch``, ``resources``, or ``subsystems``). The platform
    refuses to create empty configurations, so this provides a reasonable
    default seed.
    """
    from datetime import datetime as _dt

    timestamp = _dt.now().isoformat(timespec="seconds")
    if description:
        body = f"# {branch_name}\n\n{description}\n"
    else:
        body = (
            f"# {branch_name}\n\n"
            f"Branch {branch_name!r} created on {timestamp}.\n"
        )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="README-", delete=False, encoding="utf-8"
    ) as f:
        f.write(body)
        tmp_path = Path(f.name)
    try:
        uploaded_model = client.add_model(
            path=tmp_path,
            display_name=f"README - {branch_name}",
            description=description,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if not uploaded_model.file:
        raise RuntimeError(
            f"add_model returned a model with no file for README of {branch_name!r}: "
            f"{uploaded_model.id}"
        )
    return NewTrackedFile(
        specifier_type=TrackedFileSpecifierType.LATEST,
        file_id=uploaded_model.file.id,
    )


def _resolve_seed_subsystems(
    subsystems: Sequence[Any] | None,
) -> list[NewTrackedSystem]:
    """Convert seed subsystems (BranchView | (sys_id, tag_id) | NewTrackedSystem) into rows."""
    out: list[NewTrackedSystem] = []
    for s in subsystems or []:
        if isinstance(s, NewTrackedSystem):
            out.append(s)
            continue
        # Avoid importing BranchView at module top (forward ref); duck-type instead.
        sys_id = None
        tag_id = None
        if isinstance(s, tuple) and len(s) == 2:
            sys_id, tag_id = s
        else:
            tag = getattr(s, "_tag", None)
            sub_system = getattr(s, "_system", None)
            if tag is not None and sub_system is not None:
                tag_id = getattr(tag, "id", None)
                inner = getattr(sub_system, "_system", None)
                sys_id = getattr(inner, "id", None) if inner is not None else None
        if not sys_id or not tag_id:
            raise TypeError(
                f"Unsupported subsystem entry: {s!r} (expected BranchView, "
                f"NewTrackedSystem, or (system_id, tag_id) tuple)"
            )
        out.append(NewTrackedSystem(system_id=sys_id, tag_id=tag_id))
    return out


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
# ResourceView  --  generic wrapper around a Resource (Artifact/Model/Job/...)
# ---------------------------------------------------------------------------

@dataclass
class ResourceView:
    """Lightweight wrapper around an SDK ``Resource`` (Artifact, Model, Job, ...).

    Returned by ``ProductView.resource`` to expose the entity that owns a
    product's revision without committing to a specific concrete type.

        product = job.get_products()[0]
        res = product.resource           # ResourceView | None
        res.type                         # 'Artifact', 'Model', ...
        res.raw                          # the underlying SDK model
    """
    _resource: Any = field(repr=False)
    _client: IstariClient = field(repr=False)

    def __repr__(self) -> str:
        return f"Resource(type={self.type!r}, id={self.id})"

    @property
    def id(self) -> str | None:
        return getattr(self._resource, "id", None)

    @property
    def type(self) -> str:
        return type(self._resource).__name__

    @property
    def raw(self) -> Any:
        return self._resource


# ---------------------------------------------------------------------------
# ProductView  --  wraps a Product (race-safe revision + resource pair)
# ---------------------------------------------------------------------------

@dataclass
class ProductView:
    """Wraps a ``Product`` -- a derived output produced by a job (or any op).

    A Product captures the exact ``FileRevision`` that was written and the
    ``Resource`` (Artifact, Model, ...) that owns it.  This is *race-safe*:
    even if subsequent jobs add new revisions to the same artifact file,
    a Product still points to the revision created by the original job.

        for p in job.get_products():
            print(p.name, p.revision.id)
        report = job.find_product(name="report.json")
        report.download("local_report.json")
    """
    _product: Product = field(repr=False)
    _client: IstariClient = field(repr=False)
    _revision: FileRevision | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        mime = self.mime or "?"
        return (
            f"Product({self.name!r}, mime={mime!r}, "
            f"resource={self.resource_type}/{self.resource_id}, rev={self.revision_id})"
        )

    # -- product fields -----------------------------------------------------

    @property
    def raw(self) -> Product:
        return self._product

    @property
    def revision_id(self) -> str:
        return self._product.revision_id

    @property
    def file_id(self) -> str | None:
        return self._product.file_id

    @property
    def resource_type(self) -> str | None:
        return self._product.resource_type

    @property
    def resource_id(self) -> str | None:
        return self._product.resource_id

    @property
    def relationship_identifier(self) -> str | None:
        return self._product.relationship_identifier

    # -- lazy lookups -------------------------------------------------------

    @property
    def revision(self) -> FileRevision:
        """The exact ``FileRevision`` this product points to (race-safe).

        Fetched lazily on first access and cached on this view.
        """
        if self._revision is None:
            self._revision = self._client.get_revision(self._product.revision_id)
        return self._revision

    @property
    def resource(self) -> ResourceView | None:
        """The owning ``Resource`` (Artifact, Model, ...) wrapped in a view."""
        if not self._product.resource_type or not self._product.resource_id:
            return None
        try:
            r = self._client.get_resource(self._product.resource_type, self._product.resource_id)
        except Exception:
            return None
        return ResourceView(_resource=r, _client=self._client) if r else None

    # -- revision-derived convenience ---------------------------------------

    @property
    def id(self) -> str | None:
        """The owning resource id (e.g. Artifact id), falling back to revision id."""
        return self._product.resource_id or self._product.revision_id

    @property
    def name(self) -> str:
        rev = self.revision
        return rev.display_name or rev.name or self._product.revision_id

    @property
    def filename(self) -> str:
        """Original filename of the produced revision (with extension)."""
        rev = self.revision
        return rev.name or self._product.revision_id

    @property
    def mime(self) -> str | None:
        return self.revision.mime

    # -- content access -----------------------------------------------------

    def read_bytes(self) -> bytes:
        """Return the product's content as raw bytes."""
        return self._client.read_contents(token=self.revision.content_token)

    def read_text(self, encoding: str = "utf-8") -> str:
        """Return the product's content as decoded text."""
        return self.read_bytes().decode(encoding)

    def download(self, dest: str | Path) -> Path:
        """Download the product's content to a local path.

        *dest* can be a file path or a directory.  When a directory is given
        the product's original filename is used.
        """
        dest = Path(dest)
        if dest.is_dir():
            dest = dest / self.filename
        dest.write_bytes(self.read_bytes())
        return dest

    # -- mutations ----------------------------------------------------------

    def promote(
        self,
        display_name: str | None = None,
        filename: str | None = None,
        external_identifier: str | None = None,
    ) -> ModelView:
        """Promote this product's revision to a standalone model.

        *display_name* sets the human-readable model name (defaults to the
        product's display name).  *filename* sets the file name stored on
        the platform (defaults to the product's original filename, e.g.
        ``blocks.json``).  The two are independent -- ``filename`` controls
        what ``find_model(filename=...)`` matches against.

        The new model records the product's revision as a *source*, preserving
        the provenance chain: ``Original Model -> Job -> Product -> Model``.
        """
        from istari_digital_client.v2.models.new_source import NewSource

        rev = self.revision
        content = self._client.read_contents(token=rev.content_token)
        upload_name = filename or self.filename
        upload_path = Path(upload_name)
        suffix = upload_path.suffix or rev.suffix or ""

        if display_name:
            name = display_name
        elif filename:
            name = upload_path.stem
        else:
            name = self.name
        if suffix and name.endswith(suffix):
            name = name[: -len(suffix)]

        tmp_dir = tempfile.mkdtemp(prefix="istari_promote_")
        tmp_path = os.path.join(tmp_dir, upload_name)
        try:
            with open(tmp_path, "wb") as f:
                f.write(content)
            model = self._client.add_model(
                path=tmp_path,
                display_name=name,
                external_identifier=external_identifier,
                sources=[NewSource(revision_id=rev.id)],
            )
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            if os.path.isdir(tmp_dir):
                os.rmdir(tmp_dir)
        return ModelView(_model=model, _client=self._client)


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

    def get_products(self, *, resource_type: str | None = None) -> list[ProductView]:
        """Return products generated by this job (race-safe).

        Reads ``job.revision.products`` -- each ``Product`` points to the
        exact ``FileRevision`` the agent wrote, so concurrent jobs that add
        new revisions to the same artifact files cannot affect what this
        method returns.

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
        return [ProductView(_product=p, _client=self._client) for p in products]

    def find_product(
        self,
        *,
        name: str | None = None,
        filename: str | None = None,
        resource_type: str | None = None,
    ) -> ProductView | None:
        """Find a product by display name, filename, and/or resource type.

        ``name`` matches against display_name or rev.name (like ``ProductView.name``).
        ``filename`` matches against the revision's actual filename (``rev.name``).
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
# ModelView
# ---------------------------------------------------------------------------

@dataclass
class ModelView:
    """
    Wraps a Model with optional tracked-file context.

        model = platform.find_model(name="My Model")
        model.name                         # display name from latest revision
        model.current_revision_id          # from TrackedFile if available
        jobs = model.get_jobs()
        job  = model.submit_job(definition)
        id, status, arts = model.run_job(definition, timeout=600)
    """
    _model: Model = field(repr=False)
    _client: IstariClient = field(repr=False)
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
    def id(self) -> str:
        return self._model.id

    @property
    def raw(self) -> Model:
        return self._model

    @property
    def name(self) -> str:
        return _model_display_name(self._model)

    @property
    def filename(self) -> str | None:
        """Actual file name including extension (e.g. ``'model.mdzip'``)."""
        rev = _latest_revision(self._model)
        return rev.name if rev else None

    @property
    def file_id(self) -> str | None:
        return self._model.file.id if self._model.file else None

    @property
    def current_revision_id(self) -> str | None:
        if self._tracked_file:
            return self._tracked_file.current_file_revision_id
        rev = _latest_revision(self._model)
        return rev.id if rev else None

    @property
    def pinned_revision_id(self) -> str | None:
        if self._tracked_file:
            return self._tracked_file.pinned_file_revision_id
        return None

    # -- queries ------------------------------------------------------------

    def get_jobs(self, size: int = 100) -> list[JobView]:
        page = self._client.list_model_jobs(self._model.id, size=size)
        return [JobView(_job=j, _client=self._client) for j in page.iter_items()]

    def get_configurations(self) -> list[tuple[System, SystemConfiguration]]:
        """Find every (system, configuration) that tracks this model."""
        if not self._model.file:
            return []
        file_id = self._model.file.id
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

    # -- mutations ----------------------------------------------------------

    def upload_revision(
        self,
        path: str | Path,
        *,
        display_name: str | None = None,
        description: str | None = None,
        version_name: str | None = None,
        external_identifier: str | None = None,
        sources: list[Any] | None = None,
    ) -> FileRevision:
        """Upload a new revision to this model and return the new ``FileRevision``.

        Wraps ``client.update_model(model_id, path, ...)`` and refreshes this
        view's underlying ``Model`` so subsequent properties (``current_revision_id``,
        etc.) reflect the new revision.

            model = platform.find_model(name="Group3 UAS Requirements")
            new_rev = model.upload_revision("Group3-UAS-Requirements.xlsx")
            feature.add_revisions(new_rev).commit("Bump requirements")
        """
        updated_model = self._client.update_model(
            self._model.id,
            Path(path),
            sources=sources,
            description=description,
            version_name=version_name,
            external_identifier=external_identifier,
            display_name=display_name,
        )
        self._model = updated_model
        rev = _latest_revision(updated_model)
        if rev is None:
            raise RuntimeError(
                f"update_model returned model {updated_model.id} with no revisions"
            )
        return rev

    def submit_job(
        self,
        definition: JobDefinition,
        save_input: bool = False,
        save_input_as_revision: bool = False,
    ) -> JobView:
        """Submit a job on this model and return a ``JobView``."""
        job = _submit_job_impl(
            self._client, self._model.id, definition,
            save_input=save_input, save_input_as_revision=save_input_as_revision,
        )
        return JobView(_job=job, _client=self._client)

    def run_job(
        self,
        definition: JobDefinition,
        timeout: int = 3600,
        save_input: bool = False,
        save_input_as_revision: bool = False,
        poll_interval: int = 5,
    ) -> JobView:
        """Submit, wait, and return the completed JobView.

        Raises ``RuntimeError`` if the job fails or times out.

            job = model.run_job(definition)
            products = job.get_products()
        """
        jv = self.submit_job(definition, save_input=save_input, save_input_as_revision=save_input_as_revision)
        return jv.wait(timeout=timeout, poll_interval=poll_interval).on_success()


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
        product: ProductView,
        display_name: str | None = None,
        filename: str | None = None,
        external_identifier: str | None = None,
    ) -> TrackedFileSet:
        """Promote a product to a model and track it. Returns ``self`` for chaining."""
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
                    self._models.append(ModelView(_model=model, _client=self._client, _tracked_file=tf))
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
                return ModelView(_model=model, _client=self._client, _tracked_file=tf)
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
        product: ProductView,
        display_name: str | None = None,
        filename: str | None = None,
        external_identifier: str | None = None,
    ) -> TrackedFileSet:
        """Promote a product to a model and track it in a new configuration."""
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
# BranchView  --  Git-style branch over SnapshotTag + SystemConfiguration
# ---------------------------------------------------------------------------

def _branch_config_name(branch_name: str, suffix: str | None = None) -> str:
    base = f"branch:{branch_name}"
    return f"{base}:{suffix}" if suffix else base


@dataclass
class BranchView:
    """
    Wraps a ``SnapshotTag`` (the branch pointer) and its current
    ``SystemConfiguration`` (the working area).  Mutations are *staged*
    locally and materialised on ``commit()``.

        main = system.get_branch("main")
        feat = (
            system.create_branch("feature/antenna", from_branch="main")
                  .add_resources("model-uuid-1")
                  .add_subsystems("subsystem-uuid-A")
                  .commit("Add antenna and RF subsystem")
        )
        feat.get_resources()       # list[TrackedFile] live from API
        feat.get_subsystems()      # list[Subsystem]   live from API
    """
    _system: SystemView = field(repr=False)
    _tag: SnapshotTag = field(repr=False)
    _client: IstariClient = field(repr=False)
    _config: SystemConfiguration | None = field(default=None, repr=False)
    _pending_add_resources: list[str] = field(default_factory=list, repr=False)
    _pending_remove_resources: set[str] = field(default_factory=set, repr=False)
    _pending_add_revisions: list[str] = field(default_factory=list, repr=False)
    _pending_add_subsystems: list[str] = field(default_factory=list, repr=False)
    _pending_remove_subsystems: set[str] = field(default_factory=set, repr=False)

    def __repr__(self) -> str:
        flags = " *" if self.has_pending_changes else ""
        return f"Branch({self.name!r}, snapshot={self.snapshot_id}{flags})"

    # -- properties ---------------------------------------------------------

    @property
    def id(self) -> str:
        return self._tag.id

    @property
    def name(self) -> str:
        return self._tag.tag

    @property
    def snapshot_id(self) -> str | None:
        return self._tag.snapshot_id

    @property
    def is_baseline(self) -> bool:
        return bool(self._tag.is_baseline)

    @property
    def raw(self) -> SnapshotTag:
        return self._tag

    @property
    def has_pending_changes(self) -> bool:
        return bool(
            self._pending_add_resources
            or self._pending_remove_resources
            or self._pending_add_revisions
            or self._pending_add_subsystems
            or self._pending_remove_subsystems
        )

    @property
    def configuration(self) -> ConfigurationView:
        """Current working configuration (the snapshot the branch points at)."""
        cfg = self._resolve_config()
        return ConfigurationView(_config=cfg, _client=self._client)

    # -- reads --------------------------------------------------------------

    def get_resources(self) -> list[TrackedFile]:
        """Tracked files in this branch's current configuration (live)."""
        cfg = self._resolve_config()
        page = self._client.list_tracked_files(configuration_id=cfg.id, size=100)
        return list(page.iter_items())

    def get_subsystems(self) -> list[Subsystem]:
        """Subsystems tracked by this branch's current configuration (live)."""
        cfg = self._resolve_config()
        page = self._client.list_configuration_subsystems(configuration_id=cfg.id, size=100)
        return list(page.iter_items())

    def get_history(
        self,
        *,
        newest_first: bool = True,
        include_archived: bool = False,
    ) -> list[SnapshotTagRevision]:
        """Audit trail of every snapshot this branch's tag has pointed at.

        Equivalent to ``git log <branch>``: each :class:`SnapshotTagRevision`
        records when the tag was moved (``created``), who moved it
        (``created_by_id``), and which snapshot it pointed at
        (``snapshot_id``). The current ``snapshot_id`` of the branch is the
        ``snapshot_id`` of the most recent (non-archived) revision.

        Pair each entry with :meth:`get_snapshot_at` (or
        ``client.get_snapshot(rev.snapshot_id)``) to drill into the
        configuration as it was at that point in time.

        Args:
            newest_first: when ``True`` (default), most recent move first --
                matches ``git log``'s default. Set ``False`` for chronological
                order (oldest commit first).
            include_archived: when ``False`` (default), filter out revisions
                whose ``archive_status`` is anything other than ``Active``
                (e.g. rewritten history).
        """
        revisions = list(self._client.get_tag_history(self._tag.id))
        if not include_archived:
            revisions = [
                r for r in revisions
                if (getattr(r, "archive_status", None) or "active").lower() == "active"
            ]
        revisions.sort(key=lambda r: r.created, reverse=newest_first)
        return revisions

    def get_snapshot_at(self, revision: SnapshotTagRevision | str) -> Snapshot:
        """Resolve a history entry (or revision id) to its underlying ``Snapshot``."""
        snap_id = revision.snapshot_id if isinstance(revision, SnapshotTagRevision) else str(revision)
        return self._client.get_snapshot(snap_id)

    # -- staged mutations ---------------------------------------------------

    def add_resources(self, *resource_ids: str) -> BranchView:
        """Stage resources (model ids) to be tracked at next ``commit()``."""
        for rid in resource_ids:
            self._pending_remove_resources.discard(rid)
            if rid not in self._pending_add_resources:
                self._pending_add_resources.append(rid)
        return self

    def remove_resources(self, *resource_ids: str) -> BranchView:
        """Stage resources to be untracked at next ``commit()``."""
        for rid in resource_ids:
            self._pending_remove_resources.add(rid)
            if rid in self._pending_add_resources:
                self._pending_add_resources.remove(rid)
        return self

    def add_revisions(self, *revisions: FileRevision | str) -> BranchView:
        """Stage pinned file revisions to be tracked at next ``commit()``.

        Unlike :meth:`add_resources` (which tracks the resource at LATEST so the
        branch follows future revisions), this tracks each revision LOCKED so
        the branch is pinned to that exact revision.

        Each item may be a ``FileRevision`` instance or a revision id string.

            new_rev = model.upload_revision("Group3-UAS-Requirements.xlsx")
            feature.add_revisions(new_rev).commit("Pin updated requirements")
        """
        for r in revisions:
            rid = r.id if isinstance(r, FileRevision) else str(r)
            if rid not in self._pending_add_revisions:
                self._pending_add_revisions.append(rid)
        return self

    def add_subsystems(self, *system_ids: str) -> BranchView:
        """Stage subsystems (other Systems referenced by id) to track at next commit."""
        for sid in system_ids:
            self._pending_remove_subsystems.discard(sid)
            if sid not in self._pending_add_subsystems:
                self._pending_add_subsystems.append(sid)
        return self

    def remove_subsystems(self, *system_ids: str) -> BranchView:
        """Stage subsystems to be untracked at next ``commit()``."""
        for sid in system_ids:
            self._pending_remove_subsystems.add(sid)
            if sid in self._pending_add_subsystems:
                self._pending_add_subsystems.remove(sid)
        return self

    # -- commit -------------------------------------------------------------

    def commit(self, message: str = "") -> BranchView:
        """Materialise staged changes: new config + snapshot + advance pointer.

        ``message`` is reserved for future commit-message support; the SDK's
        ``NewSnapshot`` does not yet accept a message, so it is not persisted.
        """
        del message
        if not self.has_pending_changes:
            return self

        all_files = self.get_resources()
        all_subs = self.get_subsystems()
        current_files = _active_tracked_files(self._client, all_files)
        current_subs = [s for s in all_subs if _is_active(s)]
        dropped_files = [tf for tf in all_files if not _is_active(tf)]
        dropped_subs = [s for s in all_subs if not _is_active(s)]

        kept_files = [tf for tf in current_files if tf.resource_id not in self._pending_remove_resources]
        added_files = [self._tracked_file_for_resource(rid) for rid in self._pending_add_resources]
        pinned_files = [self._tracked_file_for_revision(rid) for rid in self._pending_add_revisions]

        kept_subs = [s for s in current_subs if s.tag_id and self._subsystem_system_id(s) not in self._pending_remove_subsystems]
        added_subs = [self._tracked_system_for(sid) for sid in self._pending_add_subsystems]

        new_cfg = _create_branch_configuration(
            self._client,
            system_id=self._system.id,
            name=_branch_config_name(self.name, _timestamp_suffix()),
            tracked_files=[*[_tracked_file_to_new(tf) for tf in kept_files], *added_files, *pinned_files],
            tracked_systems=[
                *[NewTrackedSystem(system_id=self._subsystem_system_id(s), tag_id=s.tag_id) for s in kept_subs],
                *added_subs,
            ],
            source_label=f"branch {self.name!r}",
            source_files_total=len(all_files),
            source_subs_total=len(all_subs),
            dropped_archived_files=dropped_files,
            dropped_archived_subs=dropped_subs,
        )

        snap = _create_snapshot(self._client, new_cfg.id)
        self._tag = self._client.update_tag(
            tag_id=self._tag.id,
            update_tag=UpdateTag(snapshot_id=snap.id),
        )
        self._config = new_cfg

        self._pending_add_resources.clear()
        self._pending_remove_resources.clear()
        self._pending_add_revisions.clear()
        self._pending_add_subsystems.clear()
        self._pending_remove_subsystems.clear()
        return self

    # -- branch lifecycle ---------------------------------------------------

    def archive(self) -> BranchView:
        """Soft-delete this branch (the underlying SnapshotTag)."""
        self._tag = self._client.archive_tag(self._tag.id)
        return self

    def restore(self) -> BranchView:
        """Restore an archived branch."""
        self._tag = self._client.restore_tag(self._tag.id)
        return self

    # -- internals ----------------------------------------------------------

    def _resolve_config(self) -> SystemConfiguration:
        if self._config is not None:
            return self._config
        if not self._tag.snapshot_id:
            raise ValueError(f"Branch {self.name!r} has no snapshot")
        snap = self._client.get_snapshot(self._tag.snapshot_id)
        self._config = self._client.get_configuration(snap.configuration_id)
        return self._config

    def _tracked_file_for_resource(self, resource_id: str) -> NewTrackedFile:
        """Resolve a resource id (Model id) to a LATEST-pinned NewTrackedFile."""
        model = self._client.get_model(resource_id)
        if not model.file:
            raise ValueError(f"Resource {resource_id!r} has no file")
        return NewTrackedFile(
            specifier_type=TrackedFileSpecifierType.LATEST,
            file_id=model.file.id,
        )

    def _tracked_file_for_revision(self, revision_id: str) -> NewTrackedFile:
        """Resolve a revision id to a LOCKED NewTrackedFile pinned at that revision."""
        rev = self._client.get_revision(revision_id)
        file_id = getattr(rev, "file_id", None)
        if not file_id:
            raise ValueError(
                f"Revision {revision_id!r} is not attached to a file (file_id is empty); "
                f"upload it via Model.upload_revision() or client.update_model() first"
            )
        return NewTrackedFile(
            specifier_type=TrackedFileSpecifierType.LOCKED,
            file_id=file_id,
            pinned_file_revision_id=revision_id,
        )

    def _tracked_system_for(self, system_id: str) -> NewTrackedSystem:
        """Resolve a system id to its current baseline tag for tracking as subsystem."""
        baseline = self._client.get_system_baseline(system_id)
        if not baseline.tag_id:
            raise ValueError(f"System {system_id!r} has no baseline tag")
        return NewTrackedSystem(system_id=system_id, tag_id=baseline.tag_id)

    @staticmethod
    def _subsystem_system_id(s: Subsystem) -> str | None:
        """Subsystems are referenced by tag; resolve back to the originating system id."""
        cfg = getattr(s, "tagged_configuration", None)
        return getattr(cfg, "system_id", None) if cfg else None


# ---------------------------------------------------------------------------
# SystemView
# ---------------------------------------------------------------------------

@dataclass
class SystemView:
    """
    Fluent wrapper: System -> baseline -> configuration -> models, plus branches.

        system = platform.get_system("Berserker")
        system.baseline.configuration.get_models()
        for branch in system.list_branches():
            print(branch.name, branch.snapshot_id)
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

    # -- branches -----------------------------------------------------------

    def list_branches(self) -> list[BranchView]:
        """All branches (SnapshotTags) on this system."""
        tags = _paginate_manually(self._client.list_tags, system_id=self._system.id)
        return [BranchView(_system=self, _tag=t, _client=self._client) for t in tags]

    def get_branch(self, name: str) -> BranchView:
        """Lookup a branch by name. Raises ``ValueError`` if not found."""
        for b in self.list_branches():
            if b.name == name:
                return b
        raise ValueError(f"Branch {name!r} not found on system {self.name!r}")

    def create_branch(
        self,
        name: str,
        *,
        description: str | None = None,
        from_branch: str | None = None,
        resources: Sequence[ResourceLike] | None = None,
        subsystems: Sequence[Any] | None = None,
    ) -> BranchView:
        """Create a new branch.

        The new branch is materialised as a fresh ``SystemConfiguration`` named
        ``"branch:<name>"``, a ``Snapshot`` of it, and a ``SnapshotTag(tag=name)``
        pointing at that snapshot.

        The platform refuses to create empty configurations, so the new branch
        must contain at least one tracked file or subsystem. Seeding rules:

        1. Explicit ``resources`` / ``subsystems`` are always honored.
        2. ``from_branch="..."`` forks that branch's active tracked items
           (combined with explicit seeds if both are given).
        3. If neither (1) nor (2) is provided, a small ``README.md`` is
           generated and uploaded as a Model — so ``create_branch(name)``
           "just works".

        There is **no** implicit baseline fork: forking another branch is
        always opt-in via ``from_branch=``.

        Args:
            name: branch name (must be unique on this system).
            description: optional text; becomes the README body when the
                auto-README fallback fires, otherwise stored as Model metadata.
            from_branch: name of an existing branch to fork from.
            resources: optional seed list. Items may be paths on disk
                (uploaded via ``client.add_model``), Model id strings (looked
                up first; falls back to ``file_id`` on miss), or
                ``Model`` / ``File`` / ``FileRevision`` / ``TrackedFile``
                / ``NewTrackedFile`` objects.
            subsystems: optional seed list. Items may be ``BranchView``,
                ``(system_id, tag_id)`` tuples, or ``NewTrackedSystem``.
        """
        if any(b.name == name for b in self.list_branches()):
            raise ValueError(f"Branch {name!r} already exists on system {self.name!r}")

        seed_files = _resolve_seed_resources(self._client, resources)
        seed_subs = _resolve_seed_subsystems(subsystems)
        has_explicit_seeds = bool(seed_files or seed_subs)

        # Optional fork from an existing branch.
        fork_files: list[NewTrackedFile] = []
        fork_subs: list[NewTrackedSystem] = []
        source_label = "explicit seed list"
        source_files_total = 0
        source_subs_total = 0
        dropped_files: list[TrackedFile] = []
        dropped_subs: list[Any] = []

        if from_branch is not None:
            source_snapshot_id = self.get_branch(from_branch).snapshot_id
            source_label = f"branch {from_branch!r}"
            if not source_snapshot_id:
                if not has_explicit_seeds:
                    raise ValueError(f"{source_label} has no snapshot to fork from")
            else:
                source_snap = self._client.get_snapshot(source_snapshot_id)
                source_cfg = self._client.get_configuration(source_snap.configuration_id)
                source_files = list(self._client.list_tracked_files(configuration_id=source_cfg.id, size=100).iter_items())
                source_subs = list(self._client.list_configuration_subsystems(configuration_id=source_cfg.id, size=100).iter_items())
                active_files = _active_tracked_files(self._client, source_files)
                active_subs = [s for s in source_subs if _is_active(s)]
                dropped_files = [tf for tf in source_files if not _is_active(tf)]
                dropped_subs = [s for s in source_subs if not _is_active(s)]
                source_files_total = len(source_files)
                source_subs_total = len(source_subs)
                fork_files = [_tracked_file_to_new(tf) for tf in active_files]
                fork_subs = [
                    NewTrackedSystem(
                        system_id=BranchView._subsystem_system_id(s),
                        tag_id=s.tag_id,
                    )
                    for s in active_subs
                    if s.tag_id and BranchView._subsystem_system_id(s)
                ]
            if has_explicit_seeds:
                source_label = f"{source_label} + seeds"

        # Auto-README: nothing else to seed with -> generate a README.md so the
        # platform's "configuration must have at least one tracked file" rule
        # is met without forcing the caller to pre-stage anything.
        elif not has_explicit_seeds:
            seed_files = [_generate_readme_seed(self._client, name, description)]
            has_explicit_seeds = True
            source_label = "auto README seed"

        new_cfg = _create_branch_configuration(
            self._client,
            system_id=self._system.id,
            name=_branch_config_name(name),
            tracked_files=[*fork_files, *seed_files],
            tracked_systems=[*fork_subs, *seed_subs],
            source_label=source_label,
            source_files_total=source_files_total + len(seed_files),
            source_subs_total=source_subs_total + len(seed_subs),
            dropped_archived_files=dropped_files,
            dropped_archived_subs=dropped_subs,
        )
        snap = _create_snapshot(self._client, new_cfg.id)
        new_tag = self._client.create_tag(
            snapshot_id=snap.id,
            new_snapshot_tag=NewSnapshotTag(tag=name),
        )
        return BranchView(_system=self, _tag=new_tag, _client=self._client, _config=new_cfg)

    def merge(self, *, from_branch: str, to_branch: str, message: str = "") -> BranchView:
        """Merge ``from_branch`` into ``to_branch`` by replacing the latter's tracked
        items with the former's, then committing on ``to_branch``.

        This is a "ours-overwrite" merge -- there is no three-way reconciliation.
        """
        if from_branch == to_branch:
            raise ValueError("from_branch and to_branch must differ")

        source = self.get_branch(from_branch)
        target = self.get_branch(to_branch)

        all_files = source.get_resources()
        all_subs = source.get_subsystems()
        source_files = _active_tracked_files(self._client, all_files)
        source_subs = [s for s in all_subs if _is_active(s)]
        dropped_files = [tf for tf in all_files if not _is_active(tf)]
        dropped_subs = [s for s in all_subs if not _is_active(s)]

        new_cfg = _create_branch_configuration(
            self._client,
            system_id=self._system.id,
            name=_branch_config_name(target.name, _timestamp_suffix()),
            tracked_files=[_tracked_file_to_new(tf) for tf in source_files],
            tracked_systems=[
                NewTrackedSystem(
                    system_id=BranchView._subsystem_system_id(s),
                    tag_id=s.tag_id,
                )
                for s in source_subs
                if s.tag_id and BranchView._subsystem_system_id(s)
            ],
            source_label=f"branch {from_branch!r}",
            source_files_total=len(all_files),
            source_subs_total=len(all_subs),
            dropped_archived_files=dropped_files,
            dropped_archived_subs=dropped_subs,
        )
        snap = _create_snapshot(self._client, new_cfg.id)
        target._tag = self._client.update_tag(
            tag_id=target.id,
            update_tag=UpdateTag(snapshot_id=snap.id),
        )
        target._config = new_cfg
        return target


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
        """Create from ``ISTARI_ENVIRONMENT_URL`` and ``ISTARI_PAT`` env vars."""
        from dotenv import load_dotenv
        from istari_digital_client.configuration import Configuration

        load_dotenv(dotenv_path)
        config = Configuration(
            registry_url=os.getenv("ISTARI_ENVIRONMENT_URL", "https://fileservice-v2.stage.istari.app"),
            registry_auth_token=os.getenv("ISTARI_PAT"),
        )
        return cls(IstariClient(config))

    @property
    def client(self) -> IstariClient:
        return self._client

    # -- system -------------------------------------------------------------

    def get_system(
        self,
        name_or_id: str,
        *,
        by_id: bool | None = None,
        include_archived: bool = False,
        page_size: int = 100,
        request_timeout_secs: int | None = 60,
        verbose: bool = False,
    ) -> SystemView:
        """Find a system by name (or id) and return a ``SystemView``.

            system = platform.get_system("Berserker")
            system = platform.get_system("01HXYZ...", by_id=True)

        Strategy:

        * If ``by_id`` is True (or the argument looks like a UUID/ULID), do a
          single ``client.get_system(id)`` call — much faster than scanning.
        * Otherwise stream through ``list_systems`` page by page and return as
          soon as a name match is found, instead of fetching every system first.
        * ``archive_status`` defaults to ``"active"`` to keep the listing
          small; pass ``include_archived=True`` to scan archived systems too.
        * ``request_timeout_secs`` bounds each individual API request so a
          slow tenant won't hang the notebook indefinitely.
        * ``verbose=True`` prints per-page progress.
        """
        if by_id is None:
            # heuristic: ULIDs/UUIDs are 26+ chars and contain no spaces
            by_id = len(name_or_id) >= 26 and " " not in name_or_id and "-" in name_or_id

        if by_id:
            if verbose:
                print(f"[get_system] direct fetch by id: {name_or_id}")
            try:
                s = self._client.get_system(name_or_id)
            except Exception as e:
                raise ValueError(f"System with id {name_or_id!r} not found: {e}") from e
            return SystemView(_system=s, _client=self._client)

        from istari_digital_client.v2.models import ArchiveStatus

        list_kwargs: dict[str, Any] = {
            "size": page_size,
            "archive_status": (ArchiveStatus.ALL if include_archived else ArchiveStatus.ACTIVE),
        }
        if request_timeout_secs is not None:
            list_kwargs["http_request_timeout_secs"] = request_timeout_secs

        page = 1
        scanned = 0
        while True:
            if verbose:
                print(f"[get_system] fetching page {page} (size={page_size}) ...", flush=True)
            current = self._client.list_systems(page=page, **list_kwargs)
            items = current.items or []
            if not items:
                break
            for s in items:
                scanned += 1
                if s.name == name_or_id:
                    if verbose:
                        print(f"[get_system] matched on page {page} after {scanned} systems")
                    return SystemView(_system=s, _client=self._client)
            if verbose:
                total = getattr(current, "pages", None)
                print(f"[get_system] page {page}/{total or '?'} done  (scanned {scanned} so far)")
            if current.pages and page >= current.pages:
                break
            page += 1
        raise ValueError(
            f"System {name_or_id!r} not found after scanning {scanned} system(s) "
            f"across {page} page(s) (include_archived={include_archived})"
        )

    # -- model --------------------------------------------------------------

    def get_job(self, job_id: str) -> JobView:
        job = self._client.get_job(job_id)
        return JobView(_job=job, _client=self._client)

    def get_model(self, model_id: str) -> ModelView:
        model = self._client.get_model(model_id)
        return ModelView(_model=model, _client=self._client)

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
                return ModelView(_model=full, _client=self._client)
            if filename and m.file:
                try:
                    f = self._client.get_file(m.file.id)
                    rev = f.revision
                    if rev and rev.name == filename:
                        full = self._client.get_model(m.id)
                        return ModelView(_model=full, _client=self._client)
                except Exception:
                    continue
            if external_id and m.file:
                try:
                    f = self._client.get_file(m.file.id)
                    if getattr(f, "external_identifier", None) == external_id:
                        full = self._client.get_model(m.id)
                        return ModelView(_model=full, _client=self._client)
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
        return ModelView(_model=model, _client=self._client)

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
) -> Job:
    """Core job-submission logic shared by ModelView and free functions."""
    from istari_digital_client.v2.models.new_source import NewSource

    sources: list[Any] = []

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
