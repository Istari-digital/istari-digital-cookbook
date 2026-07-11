"""
Istari SDK utilities -- object-oriented wrappers over the flat client API.

Entity hierarchy
----------------
    IstariPlatform           (entry point, wraps v2 Client + v3 V3Client)
      +-- .whoami()                   -> UserView
      +-- .find_user() / .get_user()  -> UserView
      +-- .client / .v3               -> SDK escape hatches
      +-- .resources()              -> ResourceQuery (lazy, chainable; .type("model") etc.)
      +-- .systems() / .jobs() / .agents() / .files() / .artifacts() / ...  -> ItemQuery (lazy)
      +-- SystemView         (wraps System)
      |     +-- .baseline              -> SnapshotView
      |     +-- .configurations        -> list[ConfigurationView]
      |     +-- .branches()            -> list[BranchView]  (snapshot tags)
      |     +-- .get_branch()          -> BranchView
      |     +-- .download_resources()  -> BranchDownloadResult (file or zip; optional depth)
      |     +-- .add_file / .add_revision
      +-- BranchView          (wraps SnapshotTag — a branch)
      |     +-- .list_revisions() / .subsystems() / .download_resources()
      |     +-- .configuration         -> ConfigurationView (at branch HEAD)
      |     +-- .advance_to(cfg)       -> self  (snapshot + move this branch tag)
      +-- SnapshotView        (wraps Snapshot)
      |     +-- .configuration         -> ConfigurationView
      +-- ConfigurationView   (wraps SystemConfiguration)
      |     +-- .get_models()          -> list[ModelView]
      |     +-- .get_tracked_files()   -> list[TrackedFile]
      |     +-- .add_file(fid)         -> TrackedFileSet  (builder)
      |     +-- .set_baseline()        -> self  (snapshot + move baseline tag)
      +-- TrackedFileSet        (builder for new configurations)
      |     +-- .add_file(fid)         -> self  (chainable)
      |     +-- .save(name=None)       -> ConfigurationView
      +-- ModelView           (wraps Model + optional TrackedFile; extends ResourceView)
      |     +-- .name / .id
      |     +-- .current_revision_id / .pinned_revision_id
      |     +-- .get_jobs() / .get_configurations()
      |     +-- .download_artifacts() / .archive()
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
      +-- ResourceView        (unified wrapper for Artifact / Model / …)
      |     +-- .id / .type / .raw / .file / .latest_revision
      |     +-- .revision              -> FileRevision  (pinned if set, else latest)
      |     +-- .pin(rev) / .unpinned  (toggle the revision pin)
      |     +-- .name / .filename / .mime / .file_id / .revision_id
      |     +-- .read_bytes() / .read_text() / .read_json() / .download(dest)
      |     +-- .as_source()           -> NewSource  (chain into next job, no API call)
      |     +-- .promote()             -> ModelView  (revision-to-model, tagged 'promoted_from')
      |     +-- .get_lineage()         -> LineageNode  (backward provenance)
      |     +-- .submit_job(defn)      -> JobView  (auto-promotes Artifact resources)
      |     +-- .run_job(defn)         -> JobView  (submit + wait + on_success)
      +-- LineageNode         (one revision in a backward lineage tree)
            +-- .step          'upload' | 'job_run' | 'promotion' | 'derived'
            +-- .parents       list[LineageNode]  (recursive)
            +-- .walk() / .print_tree()
      +-- UserView            (wraps User)
      |     +-- .id / .email / .display_name
      |     +-- .tools()             -> UserToolAccessQuery (execute grants)
      |     +-- .granted_tools()     -> list[ToolView]
      +-- ToolView            (wraps Tool)
            +-- .id / .name / .function_count

Quick start
-----------
    from istari_labs_helpers import IstariPlatform, configure_ssl_certificates

    configure_ssl_certificates("/path/to/ca.pem")   # optional — corporate TLS only
    platform = IstariPlatform.from_env()             # or: from_env(ca_bundle="...")
    me = platform.whoami()
    print(me.id, me.email)
    for tool in me.tools():
        print(tool.name, tool.function_count)

    user = platform.get_user("bob@example.com")
    print(len(user.tools()), "tools with execute access")

    # System -> baseline -> configuration -> models -> jobs
    system = platform.get_system("Berserker")
    for model in system.baseline.configuration.get_models():
        for job in model.get_jobs():
            print(job.function_name, job.status, job.model_revision_id)

    # Browse all configurations
    for cfg in system.configurations:
        print(cfg.name, len(cfg.get_models()))

    # Find a model globally via the lazy resource query, submit a job, inspect outputs
    item = platform.resources().type("model").filter(display_name="My Model").first()
    model = platform.get_model(item.id)
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
import ssl
import tempfile
import sys
import time
import zipfile
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
    NewSnapshot, UpdateTag,
)
from istari_digital_client import JobStatusName

from istari_labs_helpers._sdk import SdkClients
from istari_labs_helpers.queries import ItemQuery, ResourceQuery, ToolQuery, UserToolAccessQuery


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _snapshot_configuration(
    client: IstariClient,
    configuration_id: str,
    *,
    system_id: str | None = None,
) -> str:
    """Create a snapshot of *configuration_id* and return its id.

    When the platform returns a no-op (state already captured), fall back to
    the newest snapshot for that configuration (optionally scoped by *system_id*).
    """
    resp = client.create_snapshot(configuration_id, new_snapshot=NewSnapshot())
    for candidate in (getattr(resp, "actual_instance", None), resp):
        if candidate is None:
            continue
        snapshot_id = getattr(candidate, "id", None)
        if isinstance(snapshot_id, str) and snapshot_id:
            return snapshot_id

    kwargs: dict[str, Any] = {"configuration_id": configuration_id}
    if system_id is not None:
        kwargs["system_id"] = system_id
    snapshots = _paginate_manually(client.list_snapshots, **kwargs)
    if not snapshots and system_id is not None:
        snapshots = _paginate_manually(client.list_snapshots, system_id=system_id)
    if not snapshots:
        raise ValueError(f"No snapshot found for configuration {configuration_id}")
    return snapshots[0].id


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


@dataclass(frozen=True)
class BranchDownloadResult:
    """Result of downloading file revisions tracked on a system branch (snapshot tag)."""

    path: Path
    file_count: int
    is_zip: bool
    members: tuple[str, ...]


def _member_name_for_branch_revision(item: Any) -> str:
    """Choose a zip/archive member name for a branch revision row."""
    if item.name:
        return item.name
    if item.display_name:
        base = item.display_name
        ext = getattr(item, "extension", None)
        if ext:
            suffix = ext if str(ext).startswith(".") else f".{ext}"
            if not base.endswith(suffix):
                return base + suffix
        return base
    return item.revision_id


def _wire_client(client: IstariClient, obj: Any) -> Any:
    """Attach *client* to SDK models that use ``ClientHaving``."""
    if getattr(obj, "client", None) is None:
        obj.client = client
    return obj


def _entries_for_snapshot(
    system: System,
    snapshot_id: str,
    client: IstariClient,
    *,
    prefix: str = "",
) -> list[tuple[str, bytes]]:
    """Collect ``(filename, bytes)`` for every revision pinned on a snapshot."""
    _wire_client(client, system)
    entries: list[tuple[str, bytes]] = []
    for item in system._iter_snapshot_revisions(snapshot_id):
        _wire_client(client, item)
        name = prefix + _member_name_for_branch_revision(item)
        content = item.read_bytes()
        entries.append((name, content))
    return entries


def _entries_at_snapshot_recursive(
    system: System,
    snapshot_id: str,
    client: IstariClient,
    *,
    depth: int,
    prefix: str = "",
) -> list[tuple[str, bytes]]:
    """Collect revisions at *snapshot_id* and, when *depth* > 1, nested subsystems."""
    entries = _entries_for_snapshot(system, snapshot_id, client, prefix=prefix)
    if depth <= 1:
        return entries
    _wire_client(client, system)
    for sub in system._iter_snapshot_subsystems(snapshot_id):
        sub_system = _wire_client(client, client.get_system(sub.system_id))
        sub_prefix = f"{prefix}{sub.system_name}/"
        entries.extend(
            _entries_at_snapshot_recursive(
                sub_system,
                sub.tagged_snapshot_id,
                client,
                depth=depth - 1,
                prefix=sub_prefix,
            )
        )
    return entries


def _entries_for_branch(
    system: System,
    branch_name: str,
    client: IstariClient,
    *,
    depth: int = 1,
) -> list[tuple[str, bytes]]:
    """Collect ``(filename, bytes)`` for every revision on a branch snapshot tag."""
    if depth < 1:
        raise ValueError("depth must be >= 1")
    _wire_client(client, system)
    branch = system.get_branch(branch_name)
    return _entries_at_snapshot_recursive(
        system,
        branch.snapshot_id,
        client,
        depth=depth,
        prefix="",
    )


def _disambiguate_member_names(names: list[str]) -> list[str]:
    """Ensure archive member names are unique (append ``_2``, ``_3``, … before suffix)."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        if name not in seen:
            seen[name] = 1
            out.append(name)
            continue
        seen[name] += 1
        stem = Path(name)
        candidate = f"{stem.stem}_{seen[name]}{stem.suffix}"
        while candidate in seen:
            seen[name] += 1
            candidate = f"{stem.stem}_{seen[name]}{stem.suffix}"
        seen[candidate] = 1
        out.append(candidate)
    return out


def _write_branch_download(
    entries: list[tuple[str, bytes]],
    *,
    dest: Path | None,
    default_stem: str,
) -> BranchDownloadResult:
    """Write one file or a zip depending on *entries* length."""
    if not entries:
        raise ValueError("No tracked resources to download")

    names = _disambiguate_member_names([n for n, _ in entries])
    entries = list(zip(names, [b for _, b in entries], strict=True))

    if len(entries) == 1:
        name, content = entries[0]
        out = dest or Path.cwd() / name
        if out.is_dir():
            out = out / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(content)
        return BranchDownloadResult(path=out, file_count=1, is_zip=False, members=(name,))

    archive = dest or Path.cwd() / f"{default_stem}.zip"
    if archive.is_dir():
        archive = archive / f"{default_stem}.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return BranchDownloadResult(
        path=archive,
        file_count=len(entries),
        is_zip=True,
        members=tuple(n for n, _ in entries),
    )


def _v2_resource_class_name_for_get(resource_type: Any) -> str:
    """Map fluent / ``list_resources`` types to PascalCase names for ``Client.get_resource``.

    The v2 client compares string literals (``\"Model\"``, ``\"Artifact\"``, …).
    Standalone uploaded **files** are **Artifact** resources; the catch-all
    ``ResourceType.RESOURCE`` (slug ``\"resource\"``) is treated as **Artifact**
    so list/search rows still load.

    **Jobs** are not supported here — they are not “resources” in this API
    surface. Use :meth:`IstariPlatform.get_job` instead.
    """
    from istari_digital_client.v2.models.resource_type import ResourceType as RT

    def _reject_job() -> None:
        raise TypeError(
            "Jobs are not resources: use platform.get_job(job_id), not get_resource()."
        )

    if isinstance(resource_type, RT):
        slug = resource_type.value
        if slug == RT.JOB.value:
            _reject_job()
        if slug == RT.RESOURCE.value:
            return "Artifact"
        table_rt = {
            RT.MODEL.value: "Model",
            RT.ARTIFACT.value: "Artifact",
            RT.COMMENT.value: "Comment",
            RT.DOCUMENT.value: "Document",
        }
        if slug in table_rt:
            return table_rt[slug]
        return slug[:1].upper() + slug[1:] if slug else "Model"

    text = str(resource_type).strip()
    if text == "Job" or text.lower() == "job":
        _reject_job()
    if text in (
        "Model",
        "Artifact",
        "Comment",
        "Document",
        "FunctionAuthSecretEntity",
    ):
        return text
    key = text.lower()
    if key == RT.RESOURCE.value:
        return "Artifact"
    if key == RT.JOB.value:
        _reject_job()
    table = {
        "model": "Model",
        "artifact": "Artifact",
        "comment": "Comment",
        "document": "Document",
    }
    if key in table:
        return table[key]
    raise ValueError(f"Unsupported resource_type for get_resource: {resource_type!r}")


def configure_ssl_certificates(bundle_path: str | Path) -> ssl.SSLContext:
    """Point Python ``requests`` / ``urllib3`` at a custom CA bundle (corporate TLS interception).

    Sets ``REQUESTS_CA_BUNDLE`` and ``SSL_CERT_FILE`` to *bundle_path* and returns an
    ``ssl.SSLContext`` using that file.  Call **before** creating
    ``IstariPlatform.from_env()`` (or pass ``ca_bundle=`` into ``from_env``).

    *bundle_path* must exist (typically a ``*.pem`` from your IT team).

    Alternatively set ``ISTARI_CA_BUNDLE`` in the environment and use
    ``IstariPlatform.from_env()`` without arguments.
    """
    bundle_path_obj = Path(bundle_path)
    if not bundle_path_obj.is_file():
        raise FileNotFoundError(f"Certificate bundle not found: {bundle_path_obj}")

    resolved = str(bundle_path_obj.resolve())
    os.environ["REQUESTS_CA_BUNDLE"] = resolved
    os.environ["SSL_CERT_FILE"] = resolved

    return ssl.create_default_context(cafile=resolved)


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
    function_name: str | None = None

    @property
    def label(self) -> str:
        if self.resource_type == "Job":
            fn = self.function_name or "job"
            if self.resource_id:
                return f"{fn} ({self.resource_id})"
            return fn
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


def _classify_step(rev: FileRevision, resource_type: str | None = None) -> str:
    """Classify how a FileRevision came into existence.

    A revision whose owning resource is a ``Job`` is always a ``job_run``
    (it *represents* a job invocation).  Otherwise we look at sources:
    none -> upload, any ``promoted_from`` marker -> promotion, any Job
    source -> job_run, else derived.
    """
    if resource_type == "Job":
        return "job_run"
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
    source_info: Any = None,
) -> LineageNode:
    """Recursively build a LineageNode tree from a FileRevision.

    Two restructuring rules turn the raw platform graph into a readable
    provenance tree:

    1. **Job nodes.**  The SDK uploads a ``parameters<hash>.json`` blob onto
       the Job's own file when submitting.  Every output artifact lists that
       parameters revision as a source with ``resource_type=="Job"``.  We
       surface it as a ``Job`` node carrying the job id and (when fetchable)
       its ``function.name``, rather than as an anonymous "Revision".  The
       ``resource_type``/``resource_id`` are read directly from the
       ``Source`` record (no extra ``get_file`` round-trip).

    2. **Drop redundant siblings.**  When an Artifact has sources like
       ``[Model input, Job parameters]``, the Model is *also* the Job's own
       input -- it shows up under the Job node a level deeper.  Printing it
       both places bloats the tree and makes the provenance confusing, so
       when at least one Job source exists we keep only Job and
       ``promoted_from`` sources at the current level.

    The end result is ``Artifact <- Job <- Model`` instead of the raw
    ``Artifact <- [Model, parameters.json <- Model]``.

    ``cache`` memoizes by revision id so a diamond-shaped DAG is built once.
    Stops descending at ``max_depth``; deeper nodes are returned with
    ``truncated=True`` and no parents.
    """
    if rev.id in cache:
        return cache[rev.id]

    resource_type = getattr(source_info, "resource_type", None) if source_info is not None else None
    resource_id = getattr(source_info, "resource_id", None) if source_info is not None else None
    if not resource_type and rev.file_id:
        try:
            f = client.get_file(rev.file_id)
            resource_type = getattr(f, "resource_type", None) or resource_type
            resource_id = getattr(f, "resource_id", None) or resource_id
        except Exception:
            pass

    function_name: str | None = None
    if resource_type == "Job" and resource_id:
        try:
            job = client.get_job(resource_id)
            function_name = getattr(getattr(job, "function", None), "name", None)
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
        step=_classify_step(rev, resource_type=resource_type),
        relationship_to_child=relationship_to_child,
        parents=[],
        truncated=False,
        function_name=function_name,
    )
    cache[rev.id] = node

    if depth >= max_depth:
        node.truncated = bool(rev.sources)
        return node

    sources = list(rev.sources or [])
    has_job_source = any(getattr(s, "resource_type", None) == "Job" for s in sources)
    if has_job_source:
        sources = [
            s for s in sources
            if getattr(s, "resource_type", None) == "Job"
            or s.relationship_identifier == "promoted_from"
        ]

    for src in sources:
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
            source_info=src,
        )
        node.parents.append(parent)

    return node


# ---------------------------------------------------------------------------
# Shared promotion helper
# ---------------------------------------------------------------------------

class _LazyResource:
    """Proxy for an SDK Resource -- fetches on first real attribute access.

    Used by ``JobView.get_products(lazy=True)`` so enumerating products does
    not pay a ``get_resource`` round-trip per product.  Exposes ``id`` and the
    resource-type hint without loading; any other attribute forwards to the
    loaded Resource.
    """

    __slots__ = ("_client", "_resource_type", "_resource_id", "_loaded")

    def __init__(self, client: IstariClient, resource_type: str, resource_id: str) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_resource_type", resource_type)
        object.__setattr__(self, "_resource_id", resource_id)
        object.__setattr__(self, "_loaded", None)

    @property
    def id(self) -> str:
        return self._resource_id

    def _load(self) -> Any:
        if self._loaded is None:
            object.__setattr__(
                self, "_loaded",
                self._client.get_resource(self._resource_type, self._resource_id),
            )
        return self._loaded

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


def _make_revision_loader(
    client: IstariClient,
    revision_id: str,
) -> Callable[[], FileRevision | None]:
    """Return a zero-arg callable that fetches and returns ``revision_id`` once.

    Intended for ``ResourceView._revision_loader``.  Swallows SDK errors and
    returns ``None`` so a broken id on a single product does not prevent the
    caller from inspecting the rest of the list.
    """
    def _load() -> FileRevision | None:
        try:
            return client.get_revision(revision_id)
        except Exception:
            return None
    return _load


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
# ResourceView  --  unified wrapper over any Resource (Artifact, Model, …)
# ---------------------------------------------------------------------------

@dataclass
class ResourceView:
    """Wraps a platform Resource (Artifact, Model, …) with an optional
    revision pin.

    A ``ResourceView`` plays two roles depending on whether ``_pinned_revision``
    is set:

    - **Unpinned** (default): represents the resource in its current state.
      ``revision`` returns the latest revision of the resource's file.
    - **Pinned**: represents the resource *as of* a specific revision.  This
      is how products behave -- every ``ResourceView`` returned by
      ``JobView.get_products()`` carries the exact revision the agent wrote,
      even if newer revisions land later (race-safe).

        # Unpinned -- current state (``Artifact`` = file-backed resource in v2)
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
    _revision_loader: Callable[[], FileRevision | None] | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        pin = f", pinned_rev={self._pinned_revision.id}" if self._pinned_revision else ""
        return f"{self.type}({self.name!r}, id={self.id}{pin})"

    # -- identity -----------------------------------------------------------

    @property
    def id(self) -> str | None:
        return getattr(self._resource, "id", None)

    @property
    def type(self) -> str:
        """Concrete resource type name (e.g. ``'Model'``, ``'Artifact'``, ``'Job'``)."""
        res = self._resource
        if isinstance(res, _LazyResource):
            return res._resource_type
        return type(res).__name__

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
        """The effective revision: pinned if set, else latest.

        Honours a lazy ``_revision_loader`` when the pin was deferred (see
        ``JobView.get_products(lazy=True)``); loads once and memoises.
        """
        if self._pinned_revision is None and self._revision_loader is not None:
            self._pinned_revision = self._revision_loader()
            self._revision_loader = None
        return self._pinned_revision or self.latest_revision

    @property
    def revision_id(self) -> str | None:
        rev = self.revision
        return rev.id if rev else None

    @property
    def is_pinned(self) -> bool:
        """True when the view targets a specific revision (already loaded or still deferred)."""
        return self._pinned_revision is not None or self._revision_loader is not None

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

    def read_json(self) -> Any:
        """Parse the effective revision's content as JSON (UTF-8)."""
        return json.loads(self.read_text())

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

        Job invocations appear as ``Job`` nodes (with the job id and
        ``function.name`` when available).  The tree is restructured to
        read ``Artifact <- Job <- Model`` instead of the raw
        ``Artifact <- [Model, parameters.json]`` the platform stores -- see
        ``_build_lineage_node`` for details.

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
        on_poll: Callable[["JobView"], None] | None = None,
    ) -> JobView:
        """Submit, wait, and return the completed ``JobView``.

        Same dispatch rules as ``submit_job``.  ``on_poll`` is forwarded to
        ``JobView.wait`` for live status feedback (see ``wait``).  Raises
        ``RuntimeError`` if the job fails or times out.
        """
        jv = self.submit_job(
            definition,
            save_input=save_input,
            save_input_as_revision=save_input_as_revision,
            sources=sources,
            promotion_relationship=promotion_relationship,
        )
        return jv.wait(
            timeout=timeout,
            poll_interval=poll_interval,
            on_poll=on_poll,
        ).on_success()


def _make_resource_view(
    resource: Any,
    client: IstariClient,
    *,
    pinned_revision: FileRevision | None = None,
    revision_loader: Callable[[], FileRevision | None] | None = None,
) -> ResourceView:
    """Factory: return a ``ModelView`` for Model resources, else ``ResourceView``.

    Dispatches on the real Python class (or on ``_LazyResource._resource_type``
    when the resource is a lazy proxy).  ``revision_loader`` defers the
    ``get_revision`` fetch until the caller actually touches the revision --
    used by ``JobView.get_products(lazy=True)``.
    """
    if isinstance(resource, _LazyResource):
        type_name = resource._resource_type
    elif isinstance(resource, Model):
        type_name = "Model"
    else:
        type_name = type(resource).__name__

    if type_name == "Model":
        return ModelView(
            _resource=resource,
            _client=client,
            _pinned_revision=pinned_revision,
            _revision_loader=revision_loader,
        )
    return ResourceView(
        _resource=resource,
        _client=client,
        _pinned_revision=pinned_revision,
        _revision_loader=revision_loader,
    )


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
    _products_cache: list[ResourceView] | None = field(default=None, repr=False)
    _cache_terminal: bool = field(default=False, repr=False)

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

    def wait(
        self,
        timeout: int = 3600,
        poll_interval: int = 5,
        on_poll: Callable[["JobView"], None] | None = None,
    ) -> JobView:
        """Block until the job reaches a terminal state or *timeout* seconds elapse.

        ``on_poll`` -- optional callable invoked on every poll with the freshly
        refreshed ``JobView``.  Use it to watch the status evolve::

            # Quick one-liner (uses JobView.__repr__):
            job.wait(on_poll=print)

            # Custom format:
            job.wait(on_poll=lambda j: print(f"[{j.status}] {j.id}"))

        Anything callable works -- a lambda, a logger method, a progress-bar
        updater, etc.  Exceptions raised by the callback are logged to stderr
        and do not interrupt the wait loop.

        Returns ``self`` so calls can be chained::

            products = job.wait().on_success().get_products()
        """
        start = time.time()
        while True:
            job = self._client.get_job(self.id)
            self._job = job
            elapsed = time.time() - start

            if on_poll is not None:
                try:
                    on_poll(self)
                except Exception as exc:
                    print(f"[wait] on_poll callback raised: {exc!r}", file=sys.stderr)

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

    def _is_terminal(self) -> bool:
        """True when the job status is COMPLETED or FAILED.

        A terminal job's product list is immutable, so it's safe to cache.
        """
        status = getattr(self._job, "status", None)
        name = getattr(status, "name", None) if status is not None else None
        return name in (JobStatusName.COMPLETED, JobStatusName.FAILED)

    def _build_product_views(self, lazy: bool) -> list[ResourceView]:
        """Construct views for every product in ``self._job.revision.products``."""
        rev = self.revision
        if rev is None or not rev.products:
            return []
        views: list[ResourceView] = []
        for p in rev.products:
            if not p.resource_type or not p.resource_id:
                continue
            if lazy:
                views.append(_make_resource_view(
                    _LazyResource(self._client, p.resource_type, p.resource_id),
                    self._client,
                    revision_loader=_make_revision_loader(self._client, p.revision_id),
                ))
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

    def get_products(
        self,
        *,
        resource_type: str | None = None,
        refresh: bool = False,
        lazy: bool = True,
    ) -> list[ResourceView]:
        """Return products generated by this job as pinned ``ResourceView``s.

        Reads ``job.revision.products`` -- each ``Product`` points to the exact
        ``FileRevision`` the agent wrote.  Each returned view is pinned to
        that revision so the result stays race-safe even after newer revisions
        land on the same artifact file.

        ``resource_type`` (e.g. ``"Artifact"``) filters by the owning resource.

        ``refresh`` forces a ``get_job`` round-trip and invalidates the cache.
        Default ``False`` because ``wait()`` / ``platform.get_job()`` already
        returned fresh state and, once the job is in a terminal state, its
        product list is immutable.

        ``lazy`` (default) returns views that fetch the owning resource and
        the pinned revision on first access.  Cheap listing + per-view
        expansion ends up paying fewer round-trips than the eager path when
        the caller only inspects a subset of products.  Set ``lazy=False``
        to pre-fetch everything (the historical behaviour).

        Caching: once the job is terminal (COMPLETED / FAILED), the full
        product list is cached on this ``JobView`` and subsequent calls cost
        **zero API round-trips**.  Re-running a notebook cell like
        ``products = job.get_products()`` is instant after the first hit.
        The cached views memoise their lazy revision/resource loads, so
        repeated attribute access is also free.
        """
        if refresh:
            self._products_cache = None
            self._cache_terminal = False

        if self._products_cache is not None and self._cache_terminal:
            views = self._products_cache
        else:
            if refresh or self.revision is None or not self._job.file:
                self._job = self._client.get_job(self.id)
            views = self._build_product_views(lazy=lazy)
            if self._is_terminal():
                self._products_cache = views
                self._cache_terminal = True

        if resource_type:
            views = [v for v in views if v.type == resource_type]
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

        Walks the cached product views built by ``get_products`` (lazy by
        default) and short-circuits on the first match.  Because each view
        memoises its pinned revision after the first access, repeated
        ``find_product`` calls on the same job pay at most one
        ``get_revision`` round-trip per *previously unseen* product and
        **zero** round-trips for a product already inspected.  The owning
        resource is never fetched unless the caller actually touches it.
        """
        if not name and not filename:
            raise ValueError("Provide name or filename")

        for view in self.get_products(resource_type=resource_type):
            view_name = view.name
            if name and view_name == name:
                return view
            if filename and view.filename == filename:
                return view
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

    def download_artifacts(
        self,
        dest: str | Path | None = None,
        names: set[str] | None = None,
        *,
        auto_parse_json: bool = True,
    ) -> dict[str, Any]:
        """Download every artifact attached to this model (optionally filtered by *names*).

        When *dest* is ``None``, returns a mapping ``artifact_name -> content``.  For
        ``.json`` artifacts and *auto_parse_json* true, values are parsed ``dict`` /
        ``list``; otherwise values are raw ``bytes``.  When *dest* is a directory,
        artifacts are written there and an **empty** dict is returned (same behaviour as
        the legacy ``istari_commons.download_artifacts`` helper).

        Raises:
            FileNotFoundError: *names* was given but no artifact on the model matched.
        """
        model = self.raw
        artifacts = getattr(model, "artifacts", None) or []
        output_dir = Path(dest) if dest is not None else None
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)

        out: dict[str, Any] = {}
        matched = False
        for artifact in artifacts:
            aname = getattr(artifact, "name", None) or ""
            if not aname:
                continue
            if names is not None and aname not in names:
                continue
            matched = True
            raw_bytes = artifact.read_bytes()
            if auto_parse_json and aname.endswith(".json"):
                content: Any = json.loads(raw_bytes.decode("utf-8"))
            else:
                content = raw_bytes
            if output_dir is not None:
                out_path = output_dir / aname
                if isinstance(content, (dict, list)):
                    with open(out_path, "w", encoding="utf-8") as f:
                        json.dump(content, f, indent=2, ensure_ascii=False)
                else:
                    out_path.write_bytes(content)
            else:
                out[aname] = content

        if names is not None and not matched:
            raise FileNotFoundError(f"No artifacts matched names={names!r}")

        return out

    def archive(self) -> None:
        """Archive this model on the platform."""

        mid = self.id
        if not mid:
            raise ValueError("Model has no id")
        self._client.archive_model(mid)


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

    def add_resource(self, resource: "ResourceView | ModelView") -> "TrackedFileSet":
        """Track an already-uploaded resource by its file id. Returns ``self``."""
        file_id = resource.file_id
        if not file_id:
            raise ValueError(f"{resource.type} {resource.id} has no backing file to track")
        return self.add_file(file_id)

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

    def add_resource(self, resource: "ResourceView | ModelView") -> TrackedFileSet:
        """Track an already-uploaded resource (Model / Artifact) by its file id.

            report = platform.upload_model("report.html", external_id="…")
            cfg.add_resource(report).save()
        """
        file_id = resource.file_id
        if not file_id:
            raise ValueError(f"{resource.type} {resource.id} has no backing file to track")
        return self.add_file(file_id)

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
        """Snapshot this configuration (if needed) and move the system's baseline tag here.

        Returns ``self`` so the call can be chained::

            cfg.add_file(fid).save("v5").set_baseline()
        """
        system_id = self._config.system_id
        snapshot_id = _snapshot_configuration(
            self._client, self._config.id, system_id=system_id
        )
        baseline = self._client.get_system_baseline(system_id)
        self._client.update_tag(baseline.tag_id, UpdateTag(snapshot_id=snapshot_id))
        return self

    # -- mutations ----------------------------------------------------------

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
# SubsystemView  —  child system linked at a branch snapshot
# ---------------------------------------------------------------------------

@dataclass
class SubsystemView:
    """Fluent wrapper around a :class:`~istari_digital_client.v2.models.snapshot_subsystem_item.SnapshotSubsystemItem`.

    Subsystems are other systems pinned to a parent branch's snapshot.  Use
    :meth:`as_system` to load the child system for further inspection.
    """

    _item: Any = field(repr=False)
    _parent_system: System = field(repr=False)
    _client: IstariClient = field(repr=False)

    def __repr__(self) -> str:
        archived = "archived" if self.is_archived else "active"
        return f"Subsystem({self.system_name!r}, {archived}, snapshot={self.snapshot_id})"

    @property
    def system_id(self) -> str:
        return self._item.system_id

    @property
    def system_name(self) -> str:
        return self._item.system_name

    @property
    def system_description(self) -> str | None:
        return self._item.system_description

    @property
    def tag_id(self) -> str:
        return self._item.tag_id

    @property
    def configuration_id(self) -> str:
        return self._item.tagged_configuration_id

    @property
    def configuration_name(self) -> str:
        return self._item.tagged_configuration_name

    @property
    def snapshot_id(self) -> str:
        return self._item.tagged_snapshot_id

    @property
    def is_archived(self) -> bool:
        return self._item.is_archived

    @property
    def raw(self) -> Any:
        return self._item

    def as_system(self) -> "SystemView":
        """Load the linked child system."""
        system = self._client.get_system(self.system_id)
        return SystemView(_system=system, _client=self._client)


# ---------------------------------------------------------------------------
# BranchView  —  snapshot tag (branch) on a system
# ---------------------------------------------------------------------------

@dataclass
class BranchView:
    """Fluent wrapper around a :class:`~istari_digital_client.v2.models.snapshot_tag.SnapshotTag`.

    Branches are snapshot tags on a system — ``baseline``, ``main``, feature
    branches, and so on.  Use :meth:`list_revisions` to see file revisions at
    the branch HEAD, and :meth:`download_resources` to export them locally.
    """

    _tag: Any = field(repr=False)
    _system: System = field(repr=False)
    _client: IstariClient = field(repr=False)

    def __repr__(self) -> str:
        kind = "baseline" if self.is_baseline else "branch"
        return f"Branch({self.name!r}, {kind}, snapshot={self.snapshot_id})"

    @property
    def id(self) -> str:
        return self._tag.id

    @property
    def name(self) -> str:
        return self._tag.tag

    @property
    def is_baseline(self) -> bool:
        return self._tag.is_baseline

    @property
    def snapshot_id(self) -> str:
        return self._tag.snapshot_id

    @property
    def raw(self) -> Any:
        return self._tag

    @property
    def configuration(self) -> ConfigurationView:
        """Configuration behind this branch's current snapshot HEAD."""
        snap = self._client.get_snapshot(self.snapshot_id)
        system = self._client.get_system(self._system.id)
        match = next(
            (c for c in system.configurations or [] if c.id == snap.configuration_id),
            None,
        )
        if match is None:
            raise ValueError(
                f"Configuration {snap.configuration_id} for branch {self.name!r} "
                f"not found on system {system.name!r}"
            )
        return ConfigurationView(_config=match, _client=self._client)

    def add_resource(self, resource: "ResourceView | ModelView") -> TrackedFileSet:
        """Track an uploaded resource on this branch's configuration.

            report = platform.upload_model("report.html", external_id="…")
            new_cfg = branch.add_resource(report).save()
            branch.advance_to(new_cfg)
        """
        return self.configuration.add_resource(resource)

    def add_file(
        self,
        file_id: str | None = None,
        *,
        path: str | Path | None = None,
        display_name: str | None = None,
        external_identifier: str | None = None,
        version_name: str | None = None,
    ) -> TrackedFileSet:
        """Start a new configuration from this branch HEAD with an added file."""
        return self.configuration.add_file(
            file_id,
            path=path,
            display_name=display_name,
            external_identifier=external_identifier,
            version_name=version_name,
        )

    def list_revisions(self) -> list[Any]:
        """File revisions tracked at this branch's current snapshot."""
        _wire_client(self._client, self._system)
        items = self._system.list_branch_revisions(self._tag)
        return [_wire_client(self._client, item) for item in items]

    def subsystems(self) -> list[SubsystemView]:
        """Child systems linked at this branch's current snapshot.

            for sub in branch.subsystems():
                print(sub.system_name, sub.configuration_name)
        """
        s = _wire_client(self._client, self._system)
        items = s.list_branch_subsystems(self._tag)
        return [
            SubsystemView(_item=item, _parent_system=s, _client=self._client)
            for item in items
        ]

    def advance_to(self, configuration: ConfigurationView) -> "BranchView":
        """Snapshot *configuration* and point this branch's tag at that snapshot.

            branch = system.get_branch("baseline")
            new_cfg = branch.configuration.add_file(path="report.html").save()
            branch.advance_to(new_cfg)
        """
        snapshot_id = _snapshot_configuration(
            self._client,
            configuration.id,
            system_id=self._system.id,
        )
        self._client.update_tag(self.id, UpdateTag(snapshot_id=snapshot_id))
        # Refresh the wrapped tag so snapshot_id reflects the new HEAD.
        s = _wire_client(self._client, self._system)
        self._tag = s.get_branch(self.name)
        self._system = s
        return self

    def download_resources(
        self,
        dest: str | Path | None = None,
        *,
        depth: int = 1,
    ) -> BranchDownloadResult:
        """Download revisions on this branch (single file or zip).

        *depth* controls how many subsystem levels to include:

        - ``1`` (default) — this branch only
        - ``2`` — branch plus direct subsystems
        - ``3+`` — deeper nesting (subsystem paths use ``<name>/`` prefixes)
        """
        entries = _entries_for_branch(self._system, self.name, self._client, depth=depth)
        stem = f"{self._system.name}_{self.name}".replace(" ", "_")
        return _write_branch_download(entries, dest=Path(dest) if dest else None, default_stem=stem)


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
        """All configurations on this system (version history — not branches)."""
        return [ConfigurationView(_config=c, _client=self._client) for c in self._system.configurations or []]

    def _system_with_client(self) -> System:
        """Return the wrapped ``System`` with the SDK client attached."""
        return _wire_client(self._client, self._system)

    def branches(self) -> list[BranchView]:
        """All branches on this system (snapshot tags), including ``baseline``.

            for branch in system.branches():
                print(branch.name, len(branch.list_revisions()))
        """
        s = self._system_with_client()
        tags = [s.get_branch("baseline"), *s.list_branches()]
        return [BranchView(_tag=t, _system=s, _client=self._client) for t in tags]

    def get_branch(self, name: str) -> BranchView:
        """Look up a branch (snapshot tag) by name."""
        s = self._system_with_client()
        return BranchView(_tag=s.get_branch(name), _system=s, _client=self._client)

    def find_branch(self, name: str) -> BranchView | None:
        """Look up a branch by name or return ``None``."""
        try:
            return self.get_branch(name)
        except ValueError:
            return None

    def download_resources(
        self,
        branch: str,
        dest: str | Path | None = None,
        *,
        depth: int = 1,
    ) -> BranchDownloadResult:
        """Download all file revisions on *branch* (snapshot tag name).

            result = system.download_resources("baseline", dest="./exports")
            result = system.download_resources("baseline", dest="./exports", depth=2)
        """
        return self.get_branch(branch).download_resources(dest, depth=depth)

    def find_configuration(self, name: str) -> ConfigurationView | None:
        """Find a configuration by name, or ``None``."""
        needle = name.casefold()
        for cfg in self.configurations:
            if cfg.name.casefold() == needle:
                return cfg
        return None

    def get_configuration(self, name: str) -> ConfigurationView:
        """Find a configuration by name or raise ``LookupError``."""
        match = self.find_configuration(name)
        if match is None:
            names = [c.name for c in self.configurations]
            raise LookupError(
                f"Configuration {name!r} not found on system {self.name!r}. "
                f"Available: {names or '(none)'}"
            )
        return match

    # -- mutations ----------------------------------------------------------

    def add_file(self, file_id: str, configuration_name: str | None = None) -> SystemConfiguration:
        """Track a file (latest revision) by creating a new configuration."""
        return _add_tracked_file(self._client, self._system.id, file_id=file_id, config_name=configuration_name)

    def add_revision(self, revision_id: str, configuration_name: str | None = None) -> SystemConfiguration:
        """Track a pinned file revision by creating a new configuration."""
        return _add_tracked_file(self._client, self._system.id, revision_id=revision_id, config_name=configuration_name)


# ---------------------------------------------------------------------------
# UserView / ToolView
# ---------------------------------------------------------------------------

@dataclass
class ToolView:
    """Fluent wrapper around a v2 :class:`~istari_digital_client.v2.models.tool.Tool`."""

    _tool: Any = field(repr=False)
    _client: IstariClient = field(repr=False)

    def __repr__(self) -> str:
        return f"Tool({self.name!r}, id={self.id})"

    @property
    def id(self) -> str:
        return self._tool.id

    @property
    def name(self) -> str:
        return self._tool.name

    @property
    def raw(self) -> Any:
        return self._tool

    @property
    def functions(self) -> list[Any]:
        return list(self._tool.functions or [])

    @property
    def function_count(self) -> int:
        return len(self.functions)


@dataclass
class UserView:
    """Fluent wrapper around a v2 :class:`~istari_digital_client.v2.models.user.User`."""

    _user: Any = field(repr=False)
    _platform: "IstariPlatform" = field(repr=False)

    def __repr__(self) -> str:
        label = self.display_name or self.email or self.id
        return f"User({label!r}, id={self.id})"

    def __str__(self) -> str:
        if self.display_name and self.email:
            return f"{self.display_name} ({self.email})"
        if self.email:
            return self.email
        return self.id

    @property
    def id(self) -> str:
        return self._user.id

    @property
    def email(self) -> str | None:
        return self._user.email

    @property
    def display_name(self) -> str | None:
        return self._user.display_name

    @property
    def user_name(self) -> str | None:
        return self._user.user_name

    @property
    def raw(self) -> Any:
        return self._user

    def tools(self, *, include_functions: bool = True) -> UserToolAccessQuery:
        """Tools this user may execute (Manage Tool Access / executor grants).

        Works for any :class:`UserView` — including :meth:`IstariPlatform.whoami`
        and users resolved via :meth:`IstariPlatform.get_user`::

            user = platform.get_user("bob@example.com")
            for tool in user.tools():
                print(tool.name, tool.function_count)

        Uses the permissions API (``execute`` on ``tool`` resources).  Requires
        sufficient privileges on your token when inspecting another user.
        """
        return UserToolAccessQuery(
            self._platform.client,
            self.id,
            include_functions=include_functions,
        )

    def granted_tools(self, *, include_functions: bool = True) -> list[ToolView]:
        """Materialised list of :meth:`tools` (kept for explicit naming)."""
        return self.tools(include_functions=include_functions).all()


# ---------------------------------------------------------------------------
# IstariPlatform  --  top-level entry point
# ---------------------------------------------------------------------------

class IstariPlatform:
    """
    Entry point that hides the flat ``Client`` API behind entity-oriented methods.

        platform = IstariPlatform.from_env()

        # Direct lookups by id
        system = platform.get_system("Berserker")
        model  = platform.get_model("uuid-here")
        view   = platform.get_resource("artifact", "uuid-here")

        # Lazy, chainable queries (see ItemQuery / ResourceQuery)
        for s in platform.systems():
            print(s.name)

        item = (
            platform.resources()
            .type("model")
            .filter(display_name="MQ-99 SFR")
            .first()
        )
    """

    def __init__(self, client: IstariClient | SdkClients):
        if isinstance(client, SdkClients):
            self._sdk = client
        else:
            self._sdk = SdkClients.from_v2(client)

    @property
    def _client(self) -> IstariClient:
        return self._sdk.v2

    @property
    def url(self) -> str | None:
        """Registry URL the wrapped client is talking to, or ``None`` if unknown."""
        cfg = self._sdk.config
        return getattr(cfg, "registry_url", None) if cfg else None

    def __repr__(self) -> str:
        url = self.url or "<unknown>"
        return f"IstariPlatform connected to {url}"

    @classmethod
    def from_env(
        cls,
        dotenv_path: str = ".env",
        *,
        ca_bundle: str | Path | None = None,
    ) -> IstariPlatform:
        """Create from ``ISTARI_REGISTRY_URL`` and ``ISTARI_PERSONAL_ACCESS_TOKEN``.

        These are the same variable names used by the official Istari Digital
        Python client documentation.  Set them in a ``.env`` file next to your
        script/notebook, or export them in your shell.

        *ca_bundle* (or env ``ISTARI_CA_BUNDLE``) configures a custom CA file before
        the client is constructed — required on some corporate networks.  See
        ``configure_ssl_certificates``.
        """
        from dotenv import load_dotenv
        from istari_digital_client.configuration import Configuration

        bundle = ca_bundle
        if bundle is None:
            env_bundle = (os.getenv("ISTARI_CA_BUNDLE") or "").strip()
            if env_bundle:
                bundle = env_bundle
        if bundle:
            configure_ssl_certificates(bundle)

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
        return cls(SdkClients.from_config(config))

    @property
    def client(self) -> IstariClient:
        """Underlying v2 SDK client (systems, jobs, models, users, tools, …)."""
        return self._sdk.v2

    @property
    def v3(self):
        """Underlying v3 SDK client (unified resources, comments, remotes, …)."""
        return self._sdk.v3

    # -- identity -----------------------------------------------------------

    def whoami(self) -> UserView:
        """Return a view of the user authenticated by the current token.

            me = platform.whoami()
            print(me.id)
            for tool in me.tools():
                print(tool.name)
        """
        return UserView(_user=self._client.get_current_user(), _platform=self)

    def find_user(self, email: str) -> UserView | None:
        """Find an organization user by email (case-insensitive), or ``None``."""
        needle = email.casefold().strip()
        for user in ItemQuery(self._client.list_users):
            if (user.email or "").casefold().strip() == needle:
                return UserView(_user=user, _platform=self)
        return None

    def get_user(self, email: str) -> UserView:
        """Find an organization user by email or raise ``LookupError``."""
        match = self.find_user(email)
        if match is None:
            raise LookupError(f"No user with email {email!r}")
        return match

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

    def get_system_by_id(self, system_id: str) -> SystemView:
        """Load a system by id."""
        system = self._client.get_system(system_id)
        return SystemView(_system=system, _client=self._client)

    def download_system_resources(
        self,
        system_id: str,
        branch: str,
        dest: str | Path | None = None,
        *,
        depth: int = 1,
    ) -> BranchDownloadResult:
        """Download all file revisions on a system branch (snapshot tag).

        Writes a **single file** when the branch has one revision,
        otherwise a **zip** archive.  Set *depth* > 1 to include nested
        subsystem resources (see :meth:`BranchView.download_resources`).

            result = platform.download_system_resources(
                "92f95bf5-6c46-4e8d-a1e9-1cda4dd7eb3a",
                "UAV Sizing Study",
                dest="./exports",
            )
            print(result.path)
        """
        return self.get_system_by_id(system_id).download_resources(branch, dest=dest, depth=depth)

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
            NewSystem(name=name, description=description or f"Created by istari_labs_helpers for '{name}'"),
        )
        return SystemView(_system=created, _client=self._client)

    # -- model --------------------------------------------------------------

    def get_job(self, job_id: str) -> JobView:
        job = self._client.get_job(job_id)
        return JobView(_job=job, _client=self._client)

    def get_model(self, model_id: str) -> ModelView:
        model = self._client.get_model(model_id)
        return ModelView(_resource=model, _client=self._client)

    def get_resource(self, resource_type: Any, resource_id: str) -> ResourceView | ModelView:
        """Load a resource by type and id — same contract as v2 ``Client.get_resource``.

        *resource_type* can be a :class:`~istari_digital_client.v2.models.resource_type.ResourceType`
        enum value, a lowercase slug (``\"model\"``, ``\"artifact\"``), or the
        exact PascalCase string the API expects (``\"Model\"``, ``\"Artifact\"``).

        **Models** return :class:`ModelView`. Everything else (e.g. **artifacts**)
        returns :class:`ResourceView`.

        **Jobs** are not supported — use :meth:`get_job`.

        The ``\"resource\"`` slug from legacy listings is mapped to **Artifact**.
        """
        key = _v2_resource_class_name_for_get(resource_type)
        r = self._client.get_resource(key, resource_id)
        if isinstance(r, Model):
            return ModelView(_resource=r, _client=self._client)
        return ResourceView(_resource=r, _client=self._client)

    def get_revision(self, revision_id: str) -> FileRevision:
        """Return a single file revision by id (for downloads / lineage checks)."""
        return self._client.get_revision(revision_id)

    def get_resource_at_revision(self, revision_id: str) -> ResourceView | ModelView:
        """Load the resource that owns *revision_id*, pinned to that revision.

        ``FileRevision`` exposes the revision UUID as ``.id`` and the parent
        resource via ``.resource`` / ``.file.resource_id`` (there is no
        ``.parent``).  This helper returns a :class:`ResourceView` (or
        :class:`ModelView`) so callers can use:

            doc = platform.get_resource_at_revision(revision_id)
            doc.id            # resource (Model / Artifact) UUID
            doc.revision_id   # file-revision UUID
            doc.read_bytes()  # content of that exact revision
        """
        rev = self.get_revision(revision_id)
        file_obj = rev.file or self._client.get_file(rev.file_id)
        resource_id = getattr(file_obj, "resource_id", None)
        resource_type = getattr(file_obj, "resource_type", None)
        if not resource_id or not resource_type:
            raise ValueError(
                f"Revision {revision_id!r} has no parent resource "
                f"(file_id={getattr(rev, 'file_id', None)!r})"
            )
        return self.get_resource(resource_type, resource_id).pin(rev)

    def put_text_file(
        self,
        text: str,
        *,
        filename: str,
        model_id: str | None = None,
        display_name: str | None = None,
        external_identifier: str | None = None,
        version_name: str | None = None,
        description: str | None = None,
    ) -> Model:
        """Write UTF-8 *text* to a temp file and ``add_model`` or ``update_model``.

        Registers a **Model** resource (the platform ties the backing ``File``
        to that model). When *model_id* is set, the bytes become a **new
        revision** on that model’s file.

        *filename* sets the on-disk upload basename (add a suffix if needed;
        bare names get ``.txt``). Optional kwargs mirror the client.
        """
        path = Path(filename)
        if path.suffix == "":
            path = path.with_suffix(".txt")
        with tempfile.TemporaryDirectory() as td:
            upload = Path(td) / path.name
            upload.write_text(text, encoding="utf-8")
            eff_display = display_name or path.stem
            if model_id is None:
                return self._client.add_model(
                    upload,
                    display_name=eff_display,
                    description=description,
                    version_name=version_name,
                    external_identifier=external_identifier,
                )
            return self._client.update_model(
                model_id,
                upload,
                display_name=eff_display,
                description=description,
                version_name=version_name,
                external_identifier=external_identifier,
            )

    # -- lazy, chainable queries -------------------------------------------
    #
    # Each of these returns an ``ItemQuery`` (or its resource-typed subclass
    # ``ResourceQuery``) bound to the matching v2 ``list_*`` endpoint.
    # Nothing hits the network until you iterate, slice, or count.  All v2
    # filter parameters are forwarded through ``.filter(**kwargs)`` and
    # ``.sort(field)``.
    #
    # ``resources()`` is the forward-compatible primary surface: V3 will
    # collapse the per-type list endpoints into one resource endpoint, so
    # call sites written today as
    #     platform.resources().type("model").filter(...)
    # will keep working with minimal changes.  The typed factories below are
    # kept for first-class entities that don't live behind the resource
    # endpoint (Systems, Jobs, Functions, ...).

    def resources(self) -> ResourceQuery:
        """Lazy query against ``client.list_resources`` (any resource type).

        Use ``.type("model")`` / ``.type("artifact")`` / ``.type("document")`` /
        ``.type("comment")`` to narrow.  Standalone **file** uploads are **artifacts**
        in v2.  Do not use ``get_resource`` for jobs — use :meth:`get_job`.
        See :class:`ResourceQuery` for the full filter set forwarded to the
        underlying v2 endpoint (``file_name``, ``external_identifier``,
        ``mime_type``, ``archive_status``, ``access_type``, ...).
        """
        return ResourceQuery(self._client.list_resources)

    def systems(self) -> ItemQuery[System]:
        """Lazy query against ``client.list_systems``."""
        return ItemQuery(self._client.list_systems)

    def jobs(self, *, model_id: str | None = None) -> ItemQuery[Job]:
        """Lazy query against ``client.list_jobs``.

        Pass ``model_id=...`` to scope to one model (uses the dedicated
        ``list_model_jobs`` endpoint, which is faster than client-side
        filtering on the generic listing).
        """
        if model_id is not None:
            return ItemQuery(self._client.list_model_jobs, model_id=model_id)
        return ItemQuery(self._client.list_jobs)

    def files(self) -> ItemQuery[File]:
        """Lazy query against ``client.list_files``."""
        return ItemQuery(self._client.list_files)

    def artifacts(self) -> ItemQuery[Any]:
        """Lazy query against ``client.list_artifacts``."""
        return ItemQuery(self._client.list_artifacts)

    def snapshots(self) -> ItemQuery[Snapshot]:
        """Lazy query against ``client.list_snapshots``."""
        return ItemQuery(self._client.list_snapshots)

    def functions(self) -> ItemQuery[Any]:
        """Lazy query against ``client.list_functions`` (FunctionVersion items)."""
        return ItemQuery(self._client.list_functions)

    def modules(self) -> ItemQuery[Any]:
        """Lazy query against ``client.list_modules``."""
        return ItemQuery(self._client.list_modules)

    def tools(self) -> ToolQuery:
        """Lazy query against ``client.list_tools``; iteration yields :class:`ToolView`."""
        return ToolQuery(self._client.list_tools)

    def agents(self) -> ItemQuery[Any]:
        """Lazy query against ``client.list_agents``."""
        return ItemQuery(self._client.list_agents)

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
