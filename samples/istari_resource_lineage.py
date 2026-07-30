"""
Chart the resource lineage within a model on the Istari platform.

Unlike istari_digital_thread.py (which shows the containment hierarchy),
this script builds a provenance flow graph:

  [input artifact/revision] ──▶ [job] ──▶ [output artifact/revision]

Jobs appear as transformation nodes; artifacts and their revisions appear as
data nodes. Source/product relationships on file revisions are used to stitch
inputs to jobs and jobs to outputs.

Configuration (in priority order — first match wins):
  1. --url / --token CLI flags
  2. ISTARI_REGISTRY_URL / ISTARI_REGISTRY_AUTH_TOKEN environment variables
  3. --config FILE  (default: ~/.istari/config.json)
     JSON format: {"url": "https://...", "token": "your-token"}

Usage:
  python istari_resource_lineage.py --model-id <id>
  python istari_resource_lineage.py --model-id <id> --config ~/my_creds.json
  python istari_resource_lineage.py --model-id <id> --format dot | dot -Tsvg > out.svg
  python istari_resource_lineage.py --model-id <id> --format json --output lineage.json
  python istari_resource_lineage.py --model-id <id> --collapse-revisions
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime


DEFAULT_CONFIG = os.path.expanduser("~/.istari/config.json")


def load_config_file(path):
    """Load url/token from a JSON config file. Returns (url, token), either may be None."""
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        if path != DEFAULT_CONFIG:
            sys.exit(f"Error: config file not found: {expanded}")
        return None, None
    try:
        with open(expanded) as f:
            cfg = json.load(f)
    except Exception as e:
        sys.exit(f"Error reading config file {expanded}: {e}")
    return cfg.get("url"), cfg.get("token")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Chart resource lineage (provenance flow) for an Istari model",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--model-id", metavar="ID", required=True, help="Istari model ID to trace")

    auth = parser.add_argument_group("Auth (first match wins: flag > env var > config file)")
    auth.add_argument("--url", default=None, help="Istari registry URL")
    auth.add_argument("--token", default=None, help="Istari auth token")
    auth.add_argument(
        "--config", default=DEFAULT_CONFIG, metavar="FILE",
        help=f"JSON credentials file {{\"url\":...,\"token\":...}} (default: {DEFAULT_CONFIG})",
    )

    parser.add_argument(
        "--format", default="flow", choices=["flow", "json", "dot"],
        help="Output format: flow (default), json, or dot (Graphviz)",
    )
    parser.add_argument("--output", default=None, metavar="FILE", help="Write output to file instead of stdout")
    parser.add_argument(
        "--collapse-revisions",
        action="store_true",
        help="Collapse multiple revisions of the same artifact into one node",
    )
    parser.add_argument(
        "--jobs-only",
        action="store_true",
        help="Only show artifacts that are inputs or outputs of a job (hide unconnected artifacts)",
    )
    return parser.parse_args()


def build_client(args):
    from istari_digital_client import Client, Configuration
    cfg_url, cfg_token = load_config_file(args.config)
    registry_url = args.url or os.environ.get("ISTARI_REGISTRY_URL") or cfg_url
    registry_auth_token = args.token or os.environ.get("ISTARI_REGISTRY_AUTH_TOKEN") or cfg_token
    if not registry_url:
        sys.exit(
            f"Error: registry URL not set.\n"
            f"  Use --url, set ISTARI_REGISTRY_URL, or add \"url\" to {args.config}"
        )
    if not registry_auth_token:
        sys.exit(
            f"Error: auth token not set.\n"
            f"  Use --token, set ISTARI_REGISTRY_AUTH_TOKEN, or add \"token\" to {args.config}"
        )
    return Client(Configuration(registry_url=registry_url, registry_auth_token=registry_auth_token))


def safe_get(obj, *attrs, default=None):
    for attr in attrs:
        if obj is None:
            return default
        obj = getattr(obj, attr, None)
    return obj if obj is not None else default


def fmt_date(dt):
    if dt is None:
        return ""
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(dt)


def fmt_size(sz):
    if not sz:
        return ""
    if sz >= 1_048_576:
        return f"{sz / 1_048_576:.1f} MB"
    if sz >= 1024:
        return f"{sz / 1024:.1f} KB"
    return f"{sz} B"


def artifact_display_name(artifact):
    art_id = artifact.id
    art_revs = safe_get(artifact, "file", "revisions") or []
    name = (
        safe_get(artifact, "display_name")
        or safe_get(artifact, "name")
        or safe_get(artifact, "file", "display_name")
        or safe_get(artifact, "file", "name")
    )
    if not name and art_revs:
        latest = art_revs[-1]
        name = safe_get(latest, "display_name") or safe_get(latest, "name") or safe_get(latest, "stem")
    if not name:
        return art_id

    # Add extension from revisions if not already present
    ext = ""
    for rev in art_revs:
        ext = safe_get(rev, "extension") or safe_get(rev, "suffix") or ""
        if ext:
            ext = ext.lstrip(".")
            break
    if ext and not name.endswith(f".{ext}"):
        name = f"{name}.{ext}"
    return name


def job_function_name(job):
    fn = safe_get(job, "function", "name") or safe_get(job, "function") or "unknown"
    if hasattr(fn, "name"):
        fn = fn.name
    return str(fn)


def job_status(job):
    if not job.status:
        return None
    s = job.status
    for attr in ("status_name", "name", "value", "status"):
        v = getattr(s, attr, None)
        if v:
            return str(v)
    return None


def collect_lineage(client, model_id, collapse_revisions):
    """
    Build a provenance-flow graph:
      - artifact nodes (or revision nodes if not collapsing)
      - job nodes as transformation steps
      - edges: artifact/rev → job (input), job → artifact/rev (output)

    Returns:
      {
        "model_id": ...,
        "model_name": ...,
        "nodes": {id: {type, label, meta}},
        "edges": [{from, to, relationship}],
      }
    """
    nodes = {}
    edges = []

    def add_node(node_id, node_type, label, meta=None):
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "type": node_type, "label": label, "meta": meta or {}}

    def add_edge(from_id, to_id, relationship):
        edge = {"from": from_id, "to": to_id, "relationship": relationship}
        if edge not in edges:
            edges.append(edge)

    print(f"Fetching model {model_id}...")
    try:
        model = client.get_model(model_id=model_id)
    except Exception as e:
        sys.exit(f"Error fetching model: {e}")

    # Resolve model display name
    file_revs = safe_get(model, "file", "revisions") or []
    model_name = None
    if file_revs:
        latest = file_revs[-1]
        model_name = safe_get(latest, "display_name") or safe_get(latest, "name") or safe_get(latest, "stem")
    model_name = model_name or model_id

    # --- Index: revision_id → artifact node id ---
    # Used to stitch job source/product revision IDs back to their artifact.
    rev_to_artifact = {}   # revision_id → artifact_node_id
    rev_to_artifact_meta = {}  # revision_id → artifact metadata for hover details

    # --- Index: resource_id → artifact node id (for source/product resource_id lookups) ---
    resource_to_artifact = {}

    # --- Process artifacts ---
    artifacts = safe_get(model, "artifacts") or []
    print(f"  Found {len(artifacts)} artifact(s)...")

    for artifact in artifacts:
        art_id = artifact.id
        art_revs = safe_get(artifact, "file", "revisions") or []
        display = artifact_display_name(artifact)

        # Collect aggregate metadata
        sizes = [safe_get(r, "size") for r in art_revs if safe_get(r, "size")]
        dates = [safe_get(r, "created") for r in art_revs if safe_get(r, "created")]
        earliest = fmt_date(min(dates)) if dates else ""
        latest_date = fmt_date(max(dates)) if dates else ""
        ext = ""
        for rev in art_revs:
            ext = (safe_get(rev, "extension") or safe_get(rev, "suffix") or "").lstrip(".")
            if ext:
                break

        art_meta = {
            "artifact_id": art_id,
            "model_id": model_id,
            "revision_count": len(art_revs),
            "extension": ext,
            "created": earliest,
            "last_updated": latest_date if latest_date != earliest else "",
            "latest_size": max(sizes) if sizes else None,
        }

        if collapse_revisions:
            # One node per artifact
            art_node_id = f"artifact:{art_id}"
            add_node(art_node_id, "artifact", display, art_meta)
            resource_to_artifact[art_id] = art_node_id
            for rev in art_revs:
                rev_to_artifact[rev.id] = art_node_id
                rev_to_artifact_meta[rev.id] = art_meta
        else:
            # One node per artifact revision, grouped under the artifact
            art_node_id = f"artifact:{art_id}"
            add_node(art_node_id, "artifact", display, art_meta)
            resource_to_artifact[art_id] = art_node_id
            for rev in art_revs:
                rev_name = (
                    safe_get(rev, "display_name") or safe_get(rev, "name") or safe_get(rev, "stem") or ""
                )
                rev_ext = (safe_get(rev, "extension") or safe_get(rev, "suffix") or "").lstrip(".")
                if rev_ext and not rev_name.endswith(f".{rev_ext}"):
                    rev_name = f"{rev_name}.{rev_ext}" if rev_name else f".{rev_ext}"
                rev_label = rev_name or rev.id
                rev_meta = {
                    "revision_id": rev.id,
                    "artifact_id": art_id,
                    "extension": rev_ext,
                    "size": safe_get(rev, "size"),
                    "created": fmt_date(safe_get(rev, "created")),
                    "version_name": safe_get(rev, "version_name"),
                }
                rev_node_id = f"rev:{rev.id}"
                add_node(rev_node_id, "revision", rev_label, rev_meta)
                add_edge(art_node_id, rev_node_id, "has_revision")
                rev_to_artifact[rev.id] = art_node_id
                rev_to_artifact_meta[rev.id] = rev_meta

    # --- Process jobs ---
    jobs = safe_get(model, "jobs") or []
    if not jobs:
        try:
            result = client.list_model_jobs(model_id=model_id, size=100)
            jobs = (
                result.items if hasattr(result, "items") and result.items else
                result.content if hasattr(result, "content") and result.content else []
            )
        except Exception:
            pass
    print(f"  Found {len(jobs)} job(s)...")

    for job in jobs:
        job_id = job.id
        fn = job_function_name(job)
        status = job_status(job)

        job_node_id = f"job:{job_id}"
        add_node(job_node_id, "job", fn, {
            "job_id": job_id,
            "function": fn,
            "status": status,
            "model_id": model_id,
        })

        # Walk job's file revisions to find source → job → product links
        job_revs = safe_get(job, "file", "revisions") or []
        for rev in job_revs:
            rev_id = rev.id

            # Sources of this job revision = inputs to the job
            for source in (safe_get(rev, "sources") or []):
                src_rev_id = safe_get(source, "revision_id")
                src_resource_id = safe_get(source, "resource_id") or ""
                rel = safe_get(source, "relationship_identifier") or "input"
                if src_rev_id:
                    # Prefer the artifact node if we know this revision
                    if collapse_revisions:
                        src_node_id = rev_to_artifact.get(src_rev_id) or resource_to_artifact.get(src_resource_id)
                    else:
                        src_node_id = f"rev:{src_rev_id}" if src_rev_id in rev_to_artifact else \
                                      rev_to_artifact.get(src_rev_id) or resource_to_artifact.get(src_resource_id)
                    if not src_node_id:
                        # External revision not in this model — create a stub
                        resource_type = safe_get(source, "resource_type") or "resource"
                        label = f"{resource_type}:{src_resource_id}" if src_resource_id else src_rev_id
                        src_node_id = f"rev:{src_rev_id}"
                        add_node(src_node_id, "external", label, {
                            "revision_id": src_rev_id,
                            "resource_id": src_resource_id,
                            "resource_type": resource_type,
                        })
                    add_edge(src_node_id, job_node_id, rel)

            # Products of this job revision = outputs from the job
            for product in (safe_get(rev, "products") or []):
                prod_rev_id = safe_get(product, "revision_id")
                prod_resource_id = safe_get(product, "resource_id") or ""
                rel = safe_get(product, "relationship_identifier") or "output"
                if prod_rev_id:
                    if collapse_revisions:
                        prod_node_id = rev_to_artifact.get(prod_rev_id) or resource_to_artifact.get(prod_resource_id)
                    else:
                        prod_node_id = f"rev:{prod_rev_id}" if prod_rev_id in rev_to_artifact else \
                                       rev_to_artifact.get(prod_rev_id) or resource_to_artifact.get(prod_resource_id)
                    if not prod_node_id:
                        resource_type = safe_get(product, "resource_type") or "resource"
                        label = f"{resource_type}:{prod_resource_id}" if prod_resource_id else prod_rev_id
                        prod_node_id = f"rev:{prod_rev_id}"
                        add_node(prod_node_id, "external", label, {
                            "revision_id": prod_rev_id,
                            "resource_id": prod_resource_id,
                            "resource_type": resource_type,
                        })
                    add_edge(job_node_id, prod_node_id, rel)

    return {
        "model_id": model_id,
        "model_name": model_name,
        "nodes": nodes,
        "edges": edges,
    }


def filter_jobs_only(lineage):
    """Remove artifact/revision nodes that have no connection to any job."""
    nodes = lineage["nodes"]
    edges = lineage["edges"]

    # Collect all nodes connected to a job
    job_ids = {nid for nid, n in nodes.items() if n["type"] == "job"}
    connected = set(job_ids)
    for edge in edges:
        if edge["from"] in job_ids or edge["to"] in job_ids:
            connected.add(edge["from"])
            connected.add(edge["to"])
    # Also keep artifact parents of connected revisions
    for edge in edges:
        if edge["to"] in connected and edge["relationship"] == "has_revision":
            connected.add(edge["from"])

    lineage["nodes"] = {nid: n for nid, n in nodes.items() if nid in connected}
    lineage["edges"] = [e for e in edges if e["from"] in connected and e["to"] in connected]
    return lineage


def render_flow(lineage):
    """
    Print a left-to-right provenance flow.
    Groups: inputs (no incoming job edge) → jobs → outputs (no outgoing job edge).
    """
    nodes = lineage["nodes"]
    edges = lineage["edges"]

    # Classify each node's role
    has_incoming_job_edge = set()
    has_outgoing_job_edge = set()
    for edge in edges:
        src, dst = edge["from"], edge["to"]
        if nodes.get(src, {}).get("type") == "job":
            has_incoming_job_edge.add(dst)
        if nodes.get(dst, {}).get("type") == "job":
            has_outgoing_job_edge.add(src)

    # Build child map (excluding has_revision so revisions nest under artifacts)
    children = defaultdict(list)
    has_parent = set()
    for edge in edges:
        if edge["relationship"] == "has_revision":
            children[edge["from"]].append((edge["to"], edge["relationship"]))
            has_parent.add(edge["to"])
        else:
            children[edge["from"]].append((edge["to"], edge["relationship"]))
            has_parent.add(edge["to"])

    roots = [nid for nid in nodes if nid not in has_parent]

    TYPE_ICONS = {
        "artifact": "📦",
        "revision": "📄",
        "job": "⚙️ ",
        "model": "🗂️ ",
        "external": "🔗",
    }

    def node_summary(node):
        meta = node["meta"]
        t = node["type"]
        parts = []
        if t == "job":
            if meta.get("status"):
                parts.append(meta["status"])
        elif t == "artifact":
            if meta.get("revision_count"):
                parts.append(f"{meta['revision_count']} rev")
            if meta.get("latest_size"):
                parts.append(fmt_size(meta["latest_size"]))
            if meta.get("created"):
                parts.append(meta["created"])
        elif t == "revision":
            if meta.get("size"):
                parts.append(fmt_size(meta["size"]))
            if meta.get("created"):
                parts.append(meta["created"])
            if meta.get("version_name"):
                parts.append(f"v:{meta['version_name']}")
        return f"  ({', '.join(parts)})" if parts else ""

    def node_id_line(node):
        meta = node["meta"]
        t = node["type"]
        if t == "job":
            return f"job_id:  {meta.get('job_id', '')}"
        elif t == "artifact":
            return f"art_id:  {meta.get('artifact_id', '')}"
        elif t == "revision":
            return f"rev_id:  {meta.get('revision_id', '')}"
        elif t == "external":
            return f"rev_id:  {meta.get('revision_id', '')}"
        elif t == "model":
            return f"mod_id:  {meta.get('model_id', '')}"
        return ""

    def print_node(node_id, prefix="", is_last=True, depth=0):
        node = nodes.get(node_id)
        if not node:
            return
        connector = "└── " if is_last else "├── "
        icon = TYPE_ICONS.get(node["type"], "   ")
        summary = node_summary(node)
        id_line = node_id_line(node)

        print(f"{prefix}{connector}{icon} {node['label']}{summary}")
        detail_prefix = prefix + ("    " if is_last else "│   ")
        if id_line:
            print(f"{detail_prefix}    {id_line}")

        child_list = children.get(node_id, [])
        child_prefix = prefix + ("    " if is_last else "│   ")
        for i, (child_id, rel) in enumerate(child_list):
            is_child_last = i == len(child_list) - 1
            print(f"{child_prefix}{'└── ' if is_child_last else '├── '}─({rel})─▶")
            print_node(child_id, child_prefix + ("    " if is_child_last else "│   "), True, depth + 1)

    model_name = lineage.get("model_name", lineage["model_id"])
    print(f"Resource lineage: {model_name}")
    print(f"{'─' * (len(model_name) + 20)}")

    job_count = sum(1 for n in nodes.values() if n["type"] == "job")
    artifact_count = sum(1 for n in nodes.values() if n["type"] in ("artifact", "revision"))
    print(f"{len(nodes)} nodes  ({artifact_count} resource(s), {job_count} job(s))  {len(edges)} edge(s)\n")

    for i, root in enumerate(roots):
        print_node(root, "", i == len(roots) - 1)


def render_dot(lineage):
    nodes = lineage["nodes"]
    edges = lineage["edges"]
    model_name = lineage.get("model_name", lineage["model_id"])

    type_style = {
        "model":    ('box',       'lightblue',   'black'),
        "artifact": ('box',       'lightgreen',  'black'),
        "revision": ('ellipse',   'lightyellow', 'black'),
        "job":      ('component', 'lightsalmon', 'black'),
        "external": ('box',       'lightgrey',   'grey'),
    }

    def safe_id(nid):
        return nid.replace(":", "_").replace("-", "_")

    def escape(s):
        return s.replace('"', '\\"').replace('\n', '\\n')

    lines = [
        "digraph resource_lineage {",
        f'  label="{escape(model_name)} — resource lineage";',
        '  labelloc=t;',
        '  rankdir=LR;',
        '  node [fontname="Helvetica" fontsize=11];',
        '  edge [fontsize=9];',
        "",
        "  // Cluster jobs for visual grouping",
    ]

    # Jobs subgraph
    job_ids = [nid for nid, n in nodes.items() if n["type"] == "job"]
    if job_ids:
        lines.append("  subgraph cluster_jobs {")
        lines.append('    label="Transformations"; style=dashed; color=grey;')
        for nid in job_ids:
            node = nodes[nid]
            shape, fill, font = type_style["job"]
            status = node["meta"].get("status") or ""
            lbl = f"{escape(node['label'])}\\n{status}" if status else escape(node["label"])
            lines.append(f'    {safe_id(nid)} [label="{lbl}" shape={shape} fillcolor="{fill}" style=filled fontcolor="{font}"];')
        lines.append("  }")
        lines.append("")

    for nid, node in nodes.items():
        if node["type"] == "job":
            continue
        shape, fill, font = type_style.get(node["type"], ('box', 'white', 'black'))
        meta = node["meta"]
        details = []
        if node["type"] == "artifact":
            if meta.get("revision_count"):
                details.append(f"{meta['revision_count']} rev")
            if meta.get("latest_size"):
                details.append(fmt_size(meta["latest_size"]))
        elif node["type"] == "revision":
            if meta.get("size"):
                details.append(fmt_size(meta["size"]))
            if meta.get("created"):
                details.append(meta["created"])
        detail_str = "\\n" + ", ".join(details) if details else ""
        lbl = f"{escape(node['label'])}{detail_str}"
        lines.append(f'  {safe_id(nid)} [label="{lbl}" shape={shape} fillcolor="{fill}" style=filled fontcolor="{font}"];')

    lines.append("")
    for edge in edges:
        if edge["relationship"] == "has_revision":
            style = 'style=dashed color=grey'
        else:
            style = 'color=steelblue'
        lines.append(
            f'  {safe_id(edge["from"])} -> {safe_id(edge["to"])} '
            f'[label="{escape(edge["relationship"])}" {style}];'
        )

    lines.append("}")
    return "\n".join(lines)


def main():
    args = parse_args()
    client = build_client(args)

    compat = client.check_compatibility()
    if compat:
        print(f"Connected to Istari platform  version={compat.server_version}")

    lineage = collect_lineage(client, args.model_id, args.collapse_revisions)

    if args.jobs_only:
        lineage = filter_jobs_only(lineage)

    if args.format == "json":
        output = json.dumps(lineage, indent=2, default=str)
        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
            print(f"Written to {args.output}")
        else:
            print(output)

    elif args.format == "dot":
        output = render_dot(lineage)
        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
            print(f"Written to {args.output}")
        else:
            print(output)

    else:  # flow
        render_flow(lineage)
        if args.output:
            with open(args.output, "w") as f:
                json.dump(lineage, f, indent=2, default=str)
            print(f"\nJSON lineage data written to {args.output}")


if __name__ == "__main__":
    main()
