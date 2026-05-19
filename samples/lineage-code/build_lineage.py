"""Digital Thread Lineage Builder
================================

Walk an Istari System (or trace a single Resource/Revision), build a complete
backward-lineage tree with every UUID + creator + job/function metadata, and
render the result as JSON + a self-contained Markdown report (ASCII tree per
root + a clickable provenance table). Optionally uploads the report back into
Istari.

Why this exists
---------------
The platform exposes provenance through ``FileRevision.sources``, but the raw
graph is hard to read (anonymous ``parameters.json`` blobs, diamond shapes,
etc.). This script collapses the graph into a readable tree -- Job nodes carry
function/tool metadata, promotion edges are labelled, and users are resolved
to display names / emails. The output is meant to be a digital-thread artifact
that engineers can open and click through.

The walk uses the flat ``istari_digital_client.Client`` (v2) directly -- no
fluent wrappers -- and the upload uses ``V3Client``.

Quick start
-----------
1.  Set credentials::

        export ISTARI_REGISTRY_URL=https://fileservice-v2.demo.istari.app
        export ISTARI_PERSONAL_ACCESS_TOKEN=<your token>

    (``ISTARI_REGISTRY_AUTH_TOKEN`` also works; ``--env <path>`` loads from a
    .env file.)

2.  Pick one starting point:

    -   ``python build_lineage.py <system_id>``  -- lineage for every file on
        the system's baseline configuration.
    -   ``python build_lineage.py --resource-id <uuid>`` -- trace one Model or
        Artifact (resolves its current revision via v3).
    -   ``python build_lineage.py --revision-id <uuid>`` -- trace a specific
        ``FileRevision``.

3.  Add ``--upload`` to push the report back to Istari:

    -   **v3** (always): registers ``lineage.md`` as a v3 ``Resource`` with
        ``resource_type=MODEL``, ``lineage.json`` as ``resource_type=ARTIFACT``,
        and links them with a ``produces`` revision-relationship.
    -   **v2** (system mode only): also creates a new
        ``SystemConfiguration`` on the system that tracks the same two files
        alongside everything already tracked on the baseline (uses the v3
        file_ids -- no duplicate upload). Baseline tag is not moved.

Outputs (written next to the script):

-   ``<--out>.json`` -- full digital-thread payload (default ``lineage.json``).
-   ``<--out>.md``   -- human-readable Markdown report with ASCII trees and
    deep links to ``demo.istari.app/systems``, ``/models``, ``/jobs``.

Useful flags
------------
``--depth N``        Cap the lineage recursion depth (default 12).
``--no-tree``        Skip the stdout pretty-print.
``--relationship-type NAME``
                     Override the v3 relationship type linking json -> md
                     (default ``produces``).

Run via the cookbook's uv environment so ``istari_digital_client`` is on the
path::

    uv run --project ~/git/istari-digital-cookbook/istari-labs-helpers \\
        python build_lineage.py <args>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from istari_digital_client import Client, Configuration, V3Client
from istari_digital_client.v2.models import (
    FileRevision,
    NewSystemConfiguration,
    NewTrackedFile,
    TrackedFile,
    TrackedFileSpecifierType,
)
from istari_digital_client.v3.models import (
    NewRevisionRelationshipDto,
    ResourceTypeDto,
)


# ---------------------------------------------------------------------------
# User resolution (cached)
# ---------------------------------------------------------------------------

class UserCache:
    """Resolves user UUIDs to ``{id, display_name, email, first_name, last_name}``.

    Returns a stub record on lookup failure so the digital thread keeps the id
    even when the user is no longer reachable.
    """

    def __init__(self, client: Client):
        self._client = client
        self._cache: dict[str, dict[str, Any]] = {}

    def resolve(self, user_id: str | None) -> dict[str, Any] | None:
        if not user_id:
            return None
        if user_id in self._cache:
            return self._cache[user_id]
        try:
            u = self._client.get_user_by_id(user_id)
            record = {
                "id": u.id,
                "display_name": getattr(u, "display_name", None),
                "first_name": getattr(u, "first_name", None),
                "last_name": getattr(u, "last_name", None),
                "email": getattr(u, "email", None),
            }
        except Exception as exc:
            record = {"id": user_id, "display_name": None, "email": None, "_error": repr(exc)}
        self._cache[user_id] = record
        return record


# ---------------------------------------------------------------------------
# Lineage tree
# ---------------------------------------------------------------------------

@dataclass
class LineageNode:
    """One node in a backward lineage chain.

    Represents a single ``FileRevision`` plus the resource that owns it. The
    classification (``step``) tells you how the revision came to exist; the
    ``edge_relationship`` is how this node's *child* linked back to it.
    """

    step: str  # upload | job_run | promotion | derived
    edge_relationship: str | None  # source.relationship_identifier (from child)
    revision_id: str
    file_id: str | None
    resource_type: str | None  # "Model" | "Artifact" | "Job" | ...
    resource_id: str | None
    name: str | None
    display_name: str | None
    filename: str | None
    mime: str | None
    size: int | None
    external_identifier: str | None
    created: str | None  # ISO 8601
    created_by: dict[str, Any] | None
    # Job-only enrichment (populated when resource_type == "Job")
    job: dict[str, Any] | None = None
    parents: list["LineageNode"] = field(default_factory=list)
    truncated: bool = False

    @property
    def label(self) -> str:
        if self.resource_type == "Job" and self.job:
            fn = self.job.get("function_name") or "job"
            return f"{fn} ({self.resource_id})"
        return self.display_name or self.name or self.revision_id


def _classify_step(rev: FileRevision, resource_type: str | None) -> str:
    if resource_type == "Job":
        return "job_run"
    sources = rev.sources or []
    if not sources:
        return "upload"
    if any(getattr(s, "relationship_identifier", None) == "promoted_from" for s in sources):
        return "promotion"
    if any(getattr(s, "resource_type", None) == "Job" for s in sources):
        return "job_run"
    return "derived"


def _job_metadata(client: Client, job_id: str, users: UserCache) -> dict[str, Any]:
    """Fetch the Job and pull function/tool/status metadata for the digital thread."""
    try:
        job = client.get_job(job_id)
    except Exception as exc:
        return {"_error": f"get_job failed: {exc!r}"}

    fn = getattr(job, "function", None)
    function_block: dict[str, Any] | None = None
    if fn is not None:
        function_block = {
            "id": getattr(fn, "id", None),
            "name": getattr(fn, "name", None),
            "version": getattr(fn, "version", None),
            "module_name": getattr(fn, "module_name", None),
            "module_version": getattr(fn, "module_version", None),
            "tool_name": getattr(fn, "tool_name", None),
            "tool_display_name": getattr(fn, "tool_display_name", None),
        }

    status_history: list[dict[str, Any]] = []
    for s in (job.status_history or []):
        status_history.append({
            "id": getattr(s, "id", None),
            "name": getattr(getattr(s, "name", None), "value", None) or str(getattr(s, "name", None)),
            "message": getattr(s, "message", None),
            "created": _iso(getattr(s, "created", None)),
            "created_by": users.resolve(getattr(s, "created_by_id", None)),
        })

    return {
        "job_id": job.id,
        "function": function_block,
        "function_name": function_block["name"] if function_block else None,
        "created": _iso(getattr(job, "created", None)),
        "created_by": users.resolve(getattr(job, "created_by_id", None)),
        "assigned_agent_id": getattr(job, "assigned_agent_id", None),
        "assigned_agent_pool_id": getattr(job, "assigned_agent_pool_id", None),
        "agent_id": getattr(job, "agent_id", None),
        "status_history": status_history,
        "current_status": status_history[-1]["name"] if status_history else None,
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def build_lineage(
    client: Client,
    rev: FileRevision,
    *,
    edge_relationship: str | None,
    max_depth: int,
    depth: int,
    cache: dict[str, LineageNode],
    users: UserCache,
    source_hint: Any = None,
) -> LineageNode:
    """Recursively build a LineageNode from a FileRevision.

    Same restructuring rules as ``istari_utils._build_lineage_node``: when a
    revision has Job sources, we keep only the Job + ``promoted_from`` sources
    at this level so the tree reads ``Artifact <- Job <- Model`` rather than
    ``Artifact <- [Model, params.json <- Model]``.
    """
    if rev.id in cache:
        return cache[rev.id]

    resource_type = getattr(source_hint, "resource_type", None) if source_hint else None
    resource_id = getattr(source_hint, "resource_id", None) if source_hint else None
    if not resource_type and rev.file_id:
        try:
            f = client.get_file(rev.file_id)
            resource_type = getattr(f, "resource_type", None) or resource_type
            resource_id = getattr(f, "resource_id", None) or resource_id
        except Exception:
            pass

    job_block: dict[str, Any] | None = None
    if resource_type == "Job" and resource_id:
        job_block = _job_metadata(client, resource_id, users)

    node = LineageNode(
        step=_classify_step(rev, resource_type),
        edge_relationship=edge_relationship,
        revision_id=rev.id,
        file_id=rev.file_id,
        resource_type=resource_type,
        resource_id=resource_id,
        name=rev.name,
        display_name=rev.display_name,
        filename=rev.name,
        mime=rev.mime,
        size=rev.size,
        external_identifier=rev.external_identifier,
        created=_iso(rev.created),
        created_by=users.resolve(getattr(rev, "created_by_id", None)),
        job=job_block,
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
            or getattr(s, "relationship_identifier", None) == "promoted_from"
        ]

    for src in sources:
        try:
            parent_rev = client.get_revision(src.revision_id)
        except Exception:
            continue
        parent = build_lineage(
            client, parent_rev,
            edge_relationship=src.relationship_identifier,
            max_depth=max_depth,
            depth=depth + 1,
            cache=cache,
            users=users,
            source_hint=src,
        )
        node.parents.append(parent)

    return node


# ---------------------------------------------------------------------------
# Pretty printer
# ---------------------------------------------------------------------------

def print_tree(node: LineageNode, indent: int = 0, is_root: bool = True) -> None:
    prefix = "  " * indent
    res = node.resource_type or "Revision"
    edge = "" if is_root else f"  [via {node.edge_relationship or '-'}]"
    print(f"{prefix}- {res} {node.label!r}{edge}")
    print(f"{prefix}    step={node.step}  rev={node.revision_id}")
    if node.resource_id:
        print(f"{prefix}    {res.lower()}_id={node.resource_id}")
    if node.file_id:
        print(f"{prefix}    file_id={node.file_id}")
    if node.created:
        who = (node.created_by or {}).get("display_name") or (node.created_by or {}).get("email") or (node.created_by or {}).get("id") or "?"
        print(f"{prefix}    created={node.created}  by={who}")
    if node.job:
        fn = node.job.get("function") or {}
        print(
            f"{prefix}    function={fn.get('name')} v{fn.get('version')} "
            f"module={fn.get('module_name')} tool={fn.get('tool_name')}"
        )
        print(f"{prefix}    status={node.job.get('current_status')}  agent={node.job.get('agent_id')}")
    if node.truncated:
        print(f"{prefix}    ... (truncated: max_depth reached)")
    for p in node.parents:
        print_tree(p, indent + 1, is_root=False)


def node_to_dict(node: LineageNode) -> dict[str, Any]:
    out = asdict(node)
    return out


# ---------------------------------------------------------------------------
# Markdown + mermaid rendering
# ---------------------------------------------------------------------------

def _ui_base_from_registry(registry_url: str) -> str:
    """Derive the Istari UI base URL from the registry URL.

    ``https://fileservice-v2.demo.istari.app`` -> ``https://demo.istari.app``.
    Falls back to the registry URL when the pattern doesn't match.
    """
    import re
    m = re.match(r"^(https?://)(?:fileservice-v2\.)?(.+?)/?$", registry_url)
    if not m:
        return registry_url.rstrip("/")
    return f"{m.group(1)}{m.group(2)}"


def _short(uuid: str | None, n: int = 8) -> str:
    return (uuid or "")[:n]


def _user_label(user: dict[str, Any] | None) -> str:
    if not user:
        return "_unknown_"
    name = user.get("display_name") or (
        f"{user.get('first_name') or ''} {user.get('last_name') or ''}".strip()
    )
    email = user.get("email")
    if name and email:
        return f"{name} <{email}>"
    if name:
        return name
    if email:
        return email
    return f"_service_ `{_short(user.get('id'))}`"


def _ui_link(ui_base: str, kind: str, uuid: str | None) -> str:
    """Build a deep link to the Istari UI for a given resource."""
    if not uuid:
        return ""
    return f"{ui_base}/{kind}/{uuid}"


def render_ascii_tree(tree: dict[str, Any]) -> str:
    """Render a lineage tree as an indented ASCII string.

    Matches the layout of ``print_tree`` so the same shape lands in the
    markdown report and the terminal::

        - Model 'updated_SATestAssemby.zip'
            step=job_run  rev=5a3e697a-...
            model_id=1e4f97ad-...
            ...
          - Job '@istari:update_parameters (26ef0772-...)'  [via -]
              step=job_run
              ...
    """
    lines: list[str] = []
    seen: set[str] = set()

    def visit(node: dict[str, Any], indent: int, is_root: bool) -> None:
        rev_id = node.get("revision_id") or ""
        already_seen = rev_id in seen
        seen.add(rev_id)

        prefix = "  " * indent
        rtype = node.get("resource_type") or "Revision"
        if rtype == "Job":
            fn = (node.get("job") or {}).get("function_name") or "job"
            rid = node.get("resource_id") or "?"
            label = f"{fn} ({rid})"
        else:
            label = (
                node.get("display_name")
                or node.get("name")
                or node.get("revision_id")
                or "?"
            )

        edge = ""
        if not is_root:
            rel = node.get("edge_relationship")
            if rel and rel != "-":
                edge = f"  [via {rel}]"

        lines.append(f"{prefix}- {rtype} '{label}'{edge}")
        lines.append(f"{prefix}    step={node.get('step')}  rev={rev_id}")
        if node.get("resource_id"):
            lines.append(f"{prefix}    {rtype.lower()}_id={node.get('resource_id')}")
        if node.get("file_id"):
            lines.append(f"{prefix}    file_id={node.get('file_id')}")
        created = node.get("created")
        if created:
            by_label = _user_label(node.get("created_by"))
            # The markdown variant of _user_label includes markdown bold markers;
            # strip those for the ASCII view so the tree stays plain text.
            by_label = by_label.replace("`", "").replace("_", "")
            lines.append(f"{prefix}    created={created}  by={by_label}")
        if node.get("job"):
            job = node["job"]
            fn = job.get("function") or {}
            lines.append(
                f"{prefix}    function={fn.get('name')} v{fn.get('version')} "
                f"module={fn.get('module_name')} tool={fn.get('tool_name')}"
            )
            status = job.get("current_status") or "?"
            agent = job.get("agent_id") or job.get("assigned_agent_id") or "-"
            lines.append(f"{prefix}    status={status}  agent={agent}")
        if node.get("truncated"):
            lines.append(f"{prefix}    ... (truncated: max_depth reached)")
        if already_seen and (node.get("parents") or []):
            lines.append(f"{prefix}    ... (subtree already shown above)")
            return
        for child in node.get("parents") or []:
            visit(child, indent + 1, False)

    visit(tree, 0, True)
    return "\n".join(lines)


def _flatten_lineage(tree: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """Return (depth, node) pairs in DFS order, deduplicating shared parents."""
    out: list[tuple[int, dict[str, Any]]] = []
    seen: set[str] = set()

    def visit(node: dict[str, Any], depth: int) -> None:
        rev_id = node.get("revision_id") or ""
        if rev_id in seen:
            return
        seen.add(rev_id)
        out.append((depth, node))
        for child in node.get("parents") or []:
            visit(child, depth + 1)

    visit(tree, 0)
    return out


def _render_detail_row(node: dict[str, Any], depth: int, ui_base: str) -> str:
    rtype = node.get("resource_type") or "Revision"
    rev_id = node.get("revision_id") or ""
    short_rev = f"`{_short(rev_id)}`"

    if rtype == "Job":
        job = node.get("job") or {}
        fn = job.get("function") or {}
        fn_name = fn.get("name") or "job"
        fn_ver = fn.get("version")
        name_cell = f"{fn_name}" + (f" v{fn_ver}" if fn_ver else "")
        tool = fn.get("tool_display_name") or fn.get("tool_name")
        if tool:
            name_cell += f" _(via {tool})_"
        resource_id = node.get("resource_id")
        link = _ui_link(ui_base, "jobs", resource_id)
        id_cell = f"[`{_short(resource_id)}…`]({link})" if link else f"`{_short(resource_id)}…`"
        id_cell += f"<br/>rev {short_rev}"
    else:
        name_cell = node.get("display_name") or node.get("name") or "(unnamed)"
        resource_id = node.get("resource_id")
        kind = "models" if rtype == "Model" else "artifacts"
        link = _ui_link(ui_base, kind, resource_id)
        id_cell = f"[`{_short(resource_id)}…`]({link})" if link else f"`{_short(resource_id)}…`"
        id_cell += f"<br/>rev {short_rev}"

    edge = node.get("edge_relationship")
    edge_cell = f" _(via {edge})_" if edge and edge != "-" else ""
    indent = "&nbsp;" * (depth * 4)

    created = node.get("created") or ""
    by = _user_label(node.get("created_by"))
    return (
        f"| {indent}{node.get('step') or '?'}{edge_cell} "
        f"| {rtype} "
        f"| {name_cell} "
        f"| {id_cell} "
        f"| {created} "
        f"| {by} |"
    )


def _render_model_section(m: dict[str, Any], ui_base: str) -> str:
    tree = m.get("lineage") or {}
    model_id = m.get("model_id")
    model_name = m.get("model_name") or tree.get("display_name") or "(unnamed)"
    tf = m.get("tracked_file") or {}
    root_type = tree.get("resource_type") or "Resource"
    kind = "models" if root_type == "Model" else "resources" if root_type == "Artifact" else root_type.lower() + "s"
    type_label = "Model" if root_type == "Model" else root_type

    head = [
        f"### {model_name}",
        "",
        f"- **{type_label}:** [`{model_id}`]({_ui_link(ui_base, kind, model_id)})",
        f"- **Latest revision:** `{tree.get('revision_id')}`",
        f"- **Root step:** `{tree.get('step')}`",
    ]
    if tf:
        head.append(f"- **Tracked-file specifier:** {tf.get('specifier_type') or '?'}")
    head.append("")

    if "_error" in m:
        head.append(f"> Lineage error: `{m['_error']}`")
        head.append("")
        return "\n".join(head)

    diagram = "```text\n" + render_ascii_tree(tree) + "\n```"

    table_header = [
        "",
        "<details>",
        "<summary>Provenance detail</summary>",
        "",
        "| Step | Type | Name | ID | Created | By |",
        "|---|---|---|---|---|---|",
    ]
    rows = [_render_detail_row(node, depth, ui_base) for depth, node in _flatten_lineage(tree)]
    table_footer = ["", "</details>", ""]

    return "\n".join(head + [diagram] + table_header + rows + table_footer)


def render_markdown(result: dict[str, Any], *, ui_base: str) -> str:
    """Render the full lineage payload as a self-contained markdown document.

    Works for both the system-walk mode (multiple tracked models) and the
    single-resource mode (one root, no system metadata).
    """
    sys_meta = result.get("system") or None
    cfg_meta = result.get("configuration") or None
    snap_meta = result.get("baseline_snapshot") or {}
    root_meta = result.get("root") or None
    models = result.get("models") or []
    total_nodes = sum(_count_nodes(m.get("lineage")) for m in models)
    generated = datetime.now(timezone.utc).isoformat()

    if sys_meta:
        title = f"Digital Thread — {sys_meta.get('name')}"
    elif root_meta:
        title = f"Digital Thread — {root_meta.get('name') or root_meta.get('resource_id') or 'resource'}"
    else:
        title = "Digital Thread"

    header = [f"# {title}", ""]
    if sys_meta:
        header.append(f"- **System:** [`{sys_meta['id']}`]({_ui_link(ui_base, 'systems', sys_meta['id'])})")
        if cfg_meta:
            header.append(f"- **Baseline configuration:** {cfg_meta['name']} (`{cfg_meta['id']}`)")
        header.append(f"- **Baseline snapshot:** `{snap_meta.get('id')}`")
    elif root_meta:
        rtype = root_meta.get("resource_type") or "Resource"
        kind = "models" if rtype == "Model" else "resources" if rtype == "Artifact" else rtype.lower() + "s"
        rid = root_meta.get("resource_id")
        if rid:
            header.append(f"- **{rtype}:** [`{rid}`]({_ui_link(ui_base, kind, rid)})")
        header.append(f"- **Starting revision:** `{root_meta.get('revision_id')}`")
    header.append(f"- **Generated:** {generated}")
    header.append(f"- **Lineage:** {len(models)} root(s), {total_nodes} nodes total")
    header.append("")
    header.append("## Lineage")
    header.append("")

    sections = [_render_model_section(m, ui_base) for m in models]
    return "\n".join(header + sections) + "\n"


# ---------------------------------------------------------------------------
# System walk
# ---------------------------------------------------------------------------

def walk_single_revision(
    client: Client,
    revision_id: str,
    users: UserCache,
    max_depth: int,
) -> dict[str, Any]:
    """Build a result payload with one lineage tree rooted at ``revision_id``.

    Used when the user gave ``--resource-id`` or ``--revision-id`` instead of a
    system. The shape mirrors ``walk_system`` so the markdown renderer and the
    v3 uploader work unchanged.
    """
    rev = client.get_revision(revision_id)
    tree = build_lineage(
        client, rev,
        edge_relationship=None,
        max_depth=max_depth,
        depth=0,
        cache={},
        users=users,
    )

    # Force-resolve the root's owning resource so the markdown header has good
    # identifiers (without this the root looks like a bare Revision).
    if (not tree.resource_type or not tree.resource_id) and rev.file_id:
        try:
            f = client.get_file(rev.file_id)
            tree.resource_type = getattr(f, "resource_type", None) or tree.resource_type
            tree.resource_id = getattr(f, "resource_id", None) or tree.resource_id
        except Exception:
            pass

    name = tree.display_name or tree.name or f"resource {tree.resource_id or revision_id}"

    return {
        "system": None,
        "baseline_snapshot": None,
        "configuration": None,
        "root": {
            "revision_id": tree.revision_id,
            "resource_id": tree.resource_id,
            "resource_type": tree.resource_type,
            "name": name,
        },
        "models": [{
            "model_id": tree.resource_id,
            "model_name": name,
            "tracked_file": None,
            "lineage": node_to_dict(tree),
            "_tree_obj": tree,
        }],
    }


def walk_system(client: Client, system_id: str, users: UserCache, max_depth: int) -> dict[str, Any]:
    """Walk one system's baseline configuration -> models -> lineage trees."""
    system = client.get_system(system_id)
    if not system.baseline_tagged_snapshot_id:
        raise RuntimeError(f"System {system_id} has no baseline snapshot")

    snapshot = client.get_snapshot(system.baseline_tagged_snapshot_id)
    cfg = next((c for c in (system.configurations or []) if c.id == snapshot.configuration_id), None)
    if cfg is None:
        raise RuntimeError(f"Snapshot {snapshot.id} points at configuration {snapshot.configuration_id}, not on system")

    tracked_page = client.list_tracked_files(configuration_id=cfg.id, size=100)
    tracked = list(tracked_page.iter_items())

    models: list[dict[str, Any]] = []
    for tf in tracked:
        if not tf.resource_id:
            continue
        try:
            model = client.get_model(tf.resource_id)
        except Exception as exc:
            models.append({"model_id": tf.resource_id, "_error": repr(exc)})
            continue

        latest_rev = None
        if model.file and model.file.revisions:
            current_rev_id = tf.current_file_revision_id
            latest_rev = next(
                (r for r in model.file.revisions if r.id == current_rev_id),
                model.file.revisions[-1],
            )
        if latest_rev is None:
            models.append({
                "model_id": model.id,
                "name": getattr(model, "name", None),
                "_error": "no revisions",
            })
            continue

        tree = build_lineage(
            client, latest_rev,
            edge_relationship=None,
            max_depth=max_depth,
            depth=0,
            cache={},
            users=users,
        )
        # Decorate the root with the model identity (the revision's own resource
        # type comes from `file.resource_type`; force it here so the root is
        # unambiguously the Model, not just a revision).
        tree.resource_type = "Model"
        tree.resource_id = model.id
        tree.display_name = tree.display_name or getattr(model, "name", None)
        models.append({
            "model_id": model.id,
            "model_name": getattr(model, "name", None),
            "tracked_file": {
                "id": tf.id,
                "specifier_type": getattr(getattr(tf, "specifier_type", None), "value", None) or str(getattr(tf, "specifier_type", None)),
                "current_file_revision_id": tf.current_file_revision_id,
                "pinned_file_revision_id": getattr(tf, "pinned_file_revision_id", None),
            },
            "lineage": node_to_dict(tree),
            "_tree_obj": tree,
        })

    return {
        "system": {
            "id": system.id,
            "name": system.name,
            "description": getattr(system, "description", None),
            "created": _iso(getattr(system, "created", None)),
            "created_by": users.resolve(getattr(system, "created_by_id", None)),
        },
        "baseline_snapshot": {
            "id": snapshot.id,
            "configuration_id": snapshot.configuration_id,
            "created": _iso(getattr(snapshot, "created", None)),
            "created_by": users.resolve(getattr(snapshot, "created_by_id", None)),
        },
        "configuration": {
            "id": cfg.id,
            "name": cfg.name,
        },
        "models": models,
        # Private handles for the v2 system uploader; popped before JSON write.
        "_cfg": cfg,
        "_tracked_files": tracked,
        "_existing_config_names": [c.name for c in (system.configurations or [])],
    }


# ---------------------------------------------------------------------------
# Upload (v3)
# ---------------------------------------------------------------------------

def _pick_relationship_type(v3: V3Client, preferred: str | None) -> Any:
    """Find a RevisionRelationshipTypeDto matching ``preferred`` by name.

    Falls back to the first available type when no match is found, and raises
    when the registry exposes no relationship types at all.
    """
    page = v3.list_revision_relationship_types(size=100)
    items = list(getattr(page, "items", []) or [])
    if not items:
        raise RuntimeError("No revision relationship types are defined on this registry")

    target = (preferred or "produces").lower()
    for t in items:
        if t.name.lower() == target:
            return t
    return items[0]


def upload_lineage_v3(
    v3: V3Client,
    *,
    md_path: Path,
    json_path: Path,
    system_id: str,
    system_name: str,
    relationship_type_name: str | None = None,
) -> dict[str, Any]:
    """Upload ``md_path`` as a Model and ``json_path`` as an Artifact child of it.

    Uses the v3 APIs end-to-end: ``create_resource`` for both files, then
    ``create_revision_relationship`` to link the artifact revision to the
    model revision. The model is the *left* (source / "produces") side and the
    artifact is the *right* (derived / "produced_by") side.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    md_display = f"Digital Thread Lineage [md] — {system_name} @ {ts}"
    md_ext_id = f"digital-thread-lineage-md-{system_id}-{ts}"
    md_resource = v3.create_resource(
        path=str(md_path),
        resource_type=ResourceTypeDto.MODEL,
        display_name=md_display,
        external_identifier=md_ext_id,
        description=f"Human-readable digital thread for system {system_id} ({system_name})",
    )

    json_display = f"Digital Thread Lineage [json] — {system_name} @ {ts}"
    json_ext_id = f"digital-thread-lineage-json-{system_id}-{ts}"
    json_resource = v3.create_resource(
        path=str(json_path),
        resource_type=ResourceTypeDto.ARTIFACT,
        display_name=json_display,
        external_identifier=json_ext_id,
        description=f"Raw digital thread lineage payload for system {system_id} ({system_name})",
    )

    rel_type = _pick_relationship_type(v3, relationship_type_name)
    relationship = v3.create_revision_relationship(
        new_revision_relationship_dto=NewRevisionRelationshipDto(
            relationship_type_id=rel_type.id,
            left_revision_id=md_resource.file_revision_id,
            right_revision_id=json_resource.file_revision_id,
        )
    )

    return {
        "md_resource": {
            "resource_id": md_resource.resource_id,
            "file_id": md_resource.file_id,
            "file_revision_id": md_resource.file_revision_id,
            "display_name": md_display,
            "external_identifier": md_ext_id,
            "resource_type": ResourceTypeDto.MODEL.value,
        },
        "json_resource": {
            "resource_id": json_resource.resource_id,
            "file_id": json_resource.file_id,
            "file_revision_id": json_resource.file_revision_id,
            "display_name": json_display,
            "external_identifier": json_ext_id,
            "resource_type": ResourceTypeDto.ARTIFACT.value,
        },
        "relationship": {
            "id": relationship.id,
            "type_id": rel_type.id,
            "type_name": rel_type.name,
            "type_name_inverse": rel_type.name_inverse,
        },
    }


# ---------------------------------------------------------------------------
# Upload (v2 system configuration)
# ---------------------------------------------------------------------------

def _next_config_name(
    base_cfg_name: str,
    description: str,
    *,
    existing_names: list[str] | None = None,
) -> str:
    """Pick a fresh ``Config N`` name that doesn't collide with existing configs."""
    import re

    names = list(existing_names or [base_cfg_name])
    max_n = 0
    for nm in names:
        m = re.match(r"^Config\s+(\d+)\b", nm or "")
        if m:
            max_n = max(max_n, int(m.group(1)))
    if max_n > 0:
        return f"Config {max_n + 1} — {description}"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    return f"{base_cfg_name}__{description}_{ts}"


def add_lineage_to_system_v2(
    client: Client,
    *,
    system_id: str,
    base_cfg: Any,
    tracked_files: list[TrackedFile],
    existing_config_names: list[str],
    new_file_ids: list[str],
) -> dict[str, Any]:
    """Create a new system configuration that tracks ``new_file_ids`` alongside
    everything already tracked on the baseline configuration.

    Existing tracked files keep their specifier types (LATEST/LOCKED) and pinned
    revisions. The new files are tracked LATEST. Baseline is not moved.

    ``new_file_ids`` are typically the ``file_id`` values from the v3 upload
    step — v2 and v3 share the underlying ``File`` storage, so referencing the
    v3-created file ids here avoids re-uploading the same bytes.
    """
    entries: list[NewTrackedFile] = []
    for tf in tracked_files:
        if tf.specifier_type == TrackedFileSpecifierType.LOCKED:
            entries.append(NewTrackedFile(
                specifier_type=TrackedFileSpecifierType.LOCKED,
                file_id=tf.file_id,
                pinned_file_revision_id=tf.pinned_file_revision_id or tf.current_file_revision_id,
            ))
        else:
            entries.append(NewTrackedFile(
                specifier_type=TrackedFileSpecifierType.LATEST,
                file_id=tf.file_id,
            ))
    for fid in new_file_ids:
        entries.append(NewTrackedFile(
            specifier_type=TrackedFileSpecifierType.LATEST,
            file_id=fid,
        ))

    config_name = _next_config_name(
        base_cfg.name,
        "digital-thread-lineage",
        existing_names=existing_config_names,
    )
    new_cfg = client.create_configuration(
        system_id=system_id,
        new_system_configuration=NewSystemConfiguration(
            name=config_name,
            tracked_files=entries,
        ),
    )

    return {
        "configuration_id": new_cfg.id,
        "configuration_name": new_cfg.name,
        "tracked_count": len(entries),
        "added_file_ids": list(new_file_ids),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("system_id", nargs="?", default=None,
                    help="System UUID — lineage for every tracked file on its baseline configuration.")
    ap.add_argument("--resource-id", default=None,
                    help="Trace lineage from a single resource (Model or Artifact). Uses its current revision.")
    ap.add_argument("--revision-id", default=None,
                    help="Trace lineage from a specific FileRevision id (most precise).")
    ap.add_argument("--env", default=None, help="Path to .env file (uses process env if omitted)")
    ap.add_argument("--depth", type=int, default=12, help="Max lineage depth (default: 12)")
    ap.add_argument("--out", default="lineage.json", help="JSON output path")
    ap.add_argument("--no-tree", action="store_true", help="Skip pretty tree printing")
    ap.add_argument("--upload", action="store_true",
                    help="Upload lineage.md as a v3 Model and lineage.json as a v3 child Artifact")
    ap.add_argument("--relationship-type", default=None,
                    help="Override the relationship type name linking the artifact back to the model (default: 'produces', else the first available type)")
    args = ap.parse_args()

    if args.env:
        load_dotenv(args.env, override=True)
    else:
        load_dotenv(override=False)

    registry_url = os.getenv("ISTARI_REGISTRY_URL")
    token = os.getenv("ISTARI_PERSONAL_ACCESS_TOKEN") or os.getenv("ISTARI_REGISTRY_AUTH_TOKEN")
    if not registry_url or not token:
        print(
            "ERROR: set ISTARI_REGISTRY_URL and one of "
            "ISTARI_PERSONAL_ACCESS_TOKEN / ISTARI_REGISTRY_AUTH_TOKEN",
            file=sys.stderr,
        )
        return 2

    chosen = [bool(args.system_id), bool(args.resource_id), bool(args.revision_id)]
    if sum(chosen) != 1:
        print(
            "ERROR: provide exactly one of: system_id (positional), --resource-id, --revision-id",
            file=sys.stderr,
        )
        return 2

    config = Configuration(registry_url=registry_url, registry_auth_token=token)
    client = Client(config)
    users = UserCache(client)

    if args.system_id:
        result = walk_system(client, args.system_id, users, max_depth=args.depth)
    else:
        rev_id = args.revision_id
        if rev_id is None:
            v3_lookup = V3Client(config)
            resource = v3_lookup.get_resource(resource_id=args.resource_id)
            rev_id = resource.file_revision_id
            print(f"Resolved resource {args.resource_id} -> revision {rev_id}")
        result = walk_single_revision(client, rev_id, users, max_depth=args.depth)

    if not args.no_tree:
        sys_meta = result.get("system")
        cfg_meta = result.get("configuration")
        root_meta = result.get("root")
        if sys_meta:
            print(f"System: {sys_meta['name']}  ({sys_meta['id']})")
            if cfg_meta:
                print(f"  baseline config: {cfg_meta['name']}  ({cfg_meta['id']})")
            print(f"  models tracked:  {len(result['models'])}")
        elif root_meta:
            print(f"Root resource: {root_meta.get('name')}  ({root_meta.get('resource_type')} {root_meta.get('resource_id')})")
            print(f"  starting revision: {root_meta.get('revision_id')}")
        print()
        for m in result["models"]:
            print(f"\n=== {m.get('model_name')}  ({m.get('model_id')}) ===")
            tree = m.pop("_tree_obj", None)
            if tree is not None:
                print_tree(tree)

    # Strip the in-memory tree objects before serialising.
    for m in result["models"]:
        m.pop("_tree_obj", None)
    base_cfg = result.pop("_cfg", None)
    tracked_files = result.pop("_tracked_files", None)
    existing_config_names = result.pop("_existing_config_names", [])

    out_path = Path(args.out)
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"\nWrote {out_path}  ({len(result['models'])} models, {sum(_count_nodes(m.get('lineage')) for m in result['models'])} lineage nodes)")

    ui_base = _ui_base_from_registry(registry_url)
    md_path = out_path.with_suffix(".md")
    md_path.write_text(render_markdown(result, ui_base=ui_base))
    print(f"Wrote {md_path}  (ASCII trees + per-model tables, links rooted at {ui_base})")
    print(f"Resolved {len(users._cache)} unique users")

    if args.upload:
        print("\nUploading via v3: lineage.md as Model, lineage.json as child Artifact...")
        v3 = V3Client(config)
        sys_meta = result.get("system") or {}
        root_meta = result.get("root") or {}
        scope_id = sys_meta.get("id") or root_meta.get("resource_id") or root_meta.get("revision_id") or "unscoped"
        scope_name = sys_meta.get("name") or root_meta.get("name") or "(no system)"
        upload_result = upload_lineage_v3(
            v3,
            md_path=md_path,
            json_path=out_path,
            system_id=scope_id,
            system_name=scope_name,
            relationship_type_name=args.relationship_type,
        )
        md_r = upload_result["md_resource"]
        js_r = upload_result["json_resource"]
        rel = upload_result["relationship"]
        print(f"  [Model]    {md_r['display_name']}")
        print(f"             resource_id={md_r['resource_id']}")
        print(f"             revision_id={md_r['file_revision_id']}")
        print(f"             {ui_base}/resources/{md_r['resource_id']}")
        print(f"  [Artifact] {js_r['display_name']}")
        print(f"             resource_id={js_r['resource_id']}")
        print(f"             revision_id={js_r['file_revision_id']}")
        print(f"             {ui_base}/resources/{js_r['resource_id']}")
        print(f"  Relationship: {rel['type_name']} (inverse: {rel['type_name_inverse']})")
        print(f"             id={rel['id']}  type_id={rel['type_id']}")

        if args.system_id and base_cfg is not None and tracked_files is not None:
            print("\nAlso adding both files to a new v2 system configuration...")
            v2_result = add_lineage_to_system_v2(
                client,
                system_id=result["system"]["id"],
                base_cfg=base_cfg,
                tracked_files=tracked_files,
                existing_config_names=existing_config_names,
                new_file_ids=[md_r["file_id"], js_r["file_id"]],
            )
            print(f"  Configuration: {v2_result['configuration_name']}  ({v2_result['configuration_id']})")
            print(f"  Tracked files: {v2_result['tracked_count']} (added {len(v2_result['added_file_ids'])} new)")
            print(f"  System view:   {ui_base}/systems/{result['system']['id']}")
    return 0


def _count_nodes(node: dict[str, Any] | None) -> int:
    if not node:
        return 0
    return 1 + sum(_count_nodes(p) for p in (node.get("parents") or []))


if __name__ == "__main__":
    sys.exit(main())
