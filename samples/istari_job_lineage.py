"""
Trace resource lineage by following job executions on the Istari platform.

Unlike istari_resource_lineage.py (which starts from a model or system and
finds files), this script starts from jobs and traces what each job consumed
and produced — building a cross-system provenance graph purely from job
execution history.

  [source revision] ──▶ [job: @istari:extract] ──▶ [product revision]
                                                            │
                                                            ▼
                                              [job: @istari:update_tags] ──▶ ...

Because jobs reference revisions by ID regardless of which model or system
those files live in, this naturally spans system boundaries.

Configuration (in priority order — first match wins):
  1. --url / --token CLI flags
  2. ISTARI_REGISTRY_URL / ISTARI_REGISTRY_AUTH_TOKEN environment variables
  3. --config FILE  (default: ~/.istari/config.json)
     JSON format: {"url": "https://...", "token": "your-token"}

Usage:
  # All completed jobs you own
  python istari_job_lineage.py

  # Filter to a specific model
  python istari_job_lineage.py --model-id <id>

  # Filter to a specific function
  python istari_job_lineage.py --function @istari:extract

  # Limit to last N jobs
  python istari_job_lineage.py --max-jobs 50

  # Include jobs from all users (admin only)
  python istari_job_lineage.py --all-users

  # Generate interactive HTML chart
  python istari_job_lineage.py --format html --output lineage.html

  # Graphviz SVG
  python istari_job_lineage.py --format dot | dot -Tsvg > lineage.svg
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime


DEFAULT_CONFIG = os.path.expanduser("~/.istari/config.json")


def load_config_file(path):
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
        description="Trace resource lineage via job execution history",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    scope = parser.add_argument_group("Scope (all optional — omit for all your jobs)")
    scope.add_argument("--model-id", metavar="ID", default=None, help="Limit to jobs on this model ID")
    scope.add_argument("--function", metavar="NAME", default=None, help="Limit to jobs with this function name (substring match, e.g. @istari:extract)")
    scope.add_argument("--status", metavar="STATUS", default="Completed",
                       help="Job status filter: Completed (default), Failed, all")
    scope.add_argument("--all-users", action="store_true", help="Include jobs from all users (requires admin)")
    scope.add_argument("--max-jobs", type=int, default=100, metavar="N", help="Max jobs to fetch (default: 100)")

    auth = parser.add_argument_group("Auth (first match wins: flag > env var > config file)")
    auth.add_argument("--url", default=None, help="Istari registry URL")
    auth.add_argument("--token", default=None, help="Istari auth token")
    auth.add_argument(
        "--config", default=DEFAULT_CONFIG, metavar="FILE",
        help=f"JSON credentials file (default: {DEFAULT_CONFIG})",
    )

    parser.add_argument(
        "--format", default="flow", choices=["flow", "json", "dot", "html"],
        help="Output format: flow (default), json, dot (Graphviz), html (interactive chart)",
    )
    parser.add_argument("--output", default=None, metavar="FILE", help="Write output to file")
    parser.add_argument(
        "--collapse-revisions", action="store_true",
        help="Show one node per resource rather than one per revision",
    )
    return parser.parse_args()


def build_client(args):
    from istari_digital_client import Client, Configuration
    cfg_url, cfg_token = load_config_file(args.config)
    registry_url = args.url or os.environ.get("ISTARI_REGISTRY_URL") or cfg_url
    registry_auth_token = args.token or os.environ.get("ISTARI_REGISTRY_AUTH_TOKEN") or cfg_token
    if not registry_url:
        sys.exit(f"Error: registry URL not set.\n  Use --url, set ISTARI_REGISTRY_URL, or add \"url\" to {args.config}")
    if not registry_auth_token:
        sys.exit(f"Error: auth token not set.\n  Use --token, set ISTARI_REGISTRY_AUTH_TOKEN, or add \"token\" to {args.config}")
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


def job_fn_name(job):
    fn = safe_get(job, "function", "name") or safe_get(job, "function") or "unknown"
    if hasattr(fn, "name"):
        fn = fn.name
    return str(fn)


def job_status_str(job):
    if not job.status:
        return None
    s = job.status
    for attr in ("status_name", "name", "value", "status"):
        v = getattr(s, attr, None)
        if v:
            return str(v)
    return None


def collect_lineage(client, args):
    """
    Fetch jobs and build a provenance graph:
      revision node → job node → revision node

    Each job's file revisions carry sources (inputs) and products (outputs).
    We stitch these into:
      source_revision ──▶ job ──▶ product_revision
    """
    nodes = {}
    edges = []
    seen_edges = set()

    def add_node(node_id, node_type, label, meta=None):
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "type": node_type, "label": label, "meta": meta or {}}

    def add_edge(from_id, to_id, relationship):
        key = (from_id, to_id, relationship)
        if key not in seen_edges:
            seen_edges.add(key)
            edges.append({"from": from_id, "to": to_id, "relationship": relationship})

    # --- Fetch jobs ---
    from istari_digital_client.v2.models.job_status_name import JobStatusName

    status_filter = None
    if args.status and args.status.lower() != "all":
        try:
            status_filter = JobStatusName(args.status.capitalize())
        except ValueError:
            sys.exit(f"Error: unknown status '{args.status}'. Valid: Completed, Failed, Running, Pending, all")

    print(f"Fetching jobs (status={args.status}, max={args.max_jobs})...")
    all_jobs = []
    page = 1
    while len(all_jobs) < args.max_jobs:
        try:
            result = client.list_jobs(
                model_id=args.model_id or None,
                status_name=status_filter,
                all_users=args.all_users or None,
                page=page,
                size=min(100, args.max_jobs - len(all_jobs)),
            )
        except Exception as e:
            sys.exit(f"Error listing jobs: {e}")
        items = (
            result.items if hasattr(result, "items") and result.items is not None else
            result.content if hasattr(result, "content") and result.content is not None else []
        )
        if not items:
            break
        all_jobs.extend(items)
        if len(items) < 100:
            break
        page += 1

    # Apply function filter
    if args.function:
        fn_filter = args.function.lower()
        all_jobs = [j for j in all_jobs if fn_filter in job_fn_name(j).lower()]

    print(f"  Found {len(all_jobs)} job(s)")

    # Index: revision_id → node_id (populated as we encounter revisions)
    rev_to_node = {}

    def ensure_rev_node(rev_id, resource_id="", resource_type="", display_name="", extension="", size=None, created=""):
        """Get or create a revision/resource node."""
        if args.collapse_revisions and resource_id:
            node_id = f"resource:{resource_id}"
        else:
            node_id = f"rev:{rev_id}"

        if node_id not in nodes:
            name = display_name or ""
            ext = extension.lstrip(".") if extension else ""
            if ext and name and not name.endswith(f".{ext}"):
                name = f"{name}.{ext}"
            label = name or (resource_id[:16] + "…" if resource_id and len(resource_id) > 16 else resource_id or rev_id)
            add_node(node_id, "resource", label, {
                "revision_id": rev_id,
                "resource_id": resource_id,
                "resource_type": resource_type,
                "extension": ext,
                "size": size,
                "created": created,
            })
        rev_to_node[rev_id] = node_id
        if resource_id:
            rev_to_node[resource_id] = node_id  # also index by resource_id
        return node_id

    # --- Build graph from job file revisions ---
    for job in all_jobs:
        job_id = job.id
        fn = job_fn_name(job)
        status = job_status_str(job)
        model_id = safe_get(job, "model_id") or ""
        created = fmt_date(safe_get(job, "created"))

        job_node_id = f"job:{job_id}"
        add_node(job_node_id, "job", fn, {
            "job_id": job_id,
            "function": fn,
            "status": status,
            "model_id": model_id,
            "created": created,
        })

        job_revs = safe_get(job, "file", "revisions") or []
        for rev in job_revs:
            rev_id = rev.id

            # Sources → inputs to the job
            for source in (safe_get(rev, "sources") or []):
                src_rev_id = safe_get(source, "revision_id")
                src_res_id = safe_get(source, "resource_id") or ""
                src_res_type = safe_get(source, "resource_type") or ""
                rel = safe_get(source, "relationship_identifier") or "input"
                if src_rev_id:
                    # Check if we already know this node
                    src_node = rev_to_node.get(src_rev_id) or rev_to_node.get(src_res_id)
                    if not src_node:
                        src_node = ensure_rev_node(
                            src_rev_id,
                            resource_id=src_res_id,
                            resource_type=src_res_type,
                        )
                    add_edge(src_node, job_node_id, rel)

            # Products → outputs from the job
            for product in (safe_get(rev, "products") or []):
                prod_rev_id = safe_get(product, "revision_id")
                prod_res_id = safe_get(product, "resource_id") or ""
                prod_res_type = safe_get(product, "resource_type") or ""
                rel = safe_get(product, "relationship_identifier") or "output"
                if prod_rev_id:
                    prod_node = rev_to_node.get(prod_rev_id) or rev_to_node.get(prod_res_id)
                    if not prod_node:
                        prod_node = ensure_rev_node(
                            prod_rev_id,
                            resource_id=prod_res_id,
                            resource_type=prod_res_type,
                        )
                    add_edge(job_node_id, prod_node, rel)

    # --- Enrich resource nodes with names from job revision metadata ---
    # Job file revisions themselves describe their own resource; check each
    for job in all_jobs:
        job_revs = safe_get(job, "file", "revisions") or []
        for rev in job_revs:
            rev_id = rev.id
            res_id = safe_get(rev, "resource_id") or ""
            res_type = safe_get(rev, "resource_type") or ""
            display = safe_get(rev, "display_name") or safe_get(rev, "name") or safe_get(rev, "stem") or ""
            ext = (safe_get(rev, "extension") or safe_get(rev, "suffix") or "").lstrip(".")
            size = safe_get(rev, "size")
            created = fmt_date(safe_get(rev, "created"))
            # Update any existing stub node for this revision
            node_id = rev_to_node.get(rev_id) or rev_to_node.get(res_id)
            if node_id and node_id in nodes:
                n = nodes[node_id]
                if not n["label"] or n["label"] == rev_id or n["label"].endswith("…"):
                    name = display
                    if ext and name and not name.endswith(f".{ext}"):
                        name = f"{name}.{ext}"
                    if name:
                        n["label"] = name
                m = n["meta"]
                if not m.get("resource_id") and res_id:
                    m["resource_id"] = res_id
                if not m.get("resource_type") and res_type:
                    m["resource_type"] = res_type
                if not m.get("extension") and ext:
                    m["extension"] = ext
                if not m.get("size") and size:
                    m["size"] = size
                if not m.get("created") and created:
                    m["created"] = created

    return {
        "nodes": nodes,
        "edges": edges,
        "job_count": len(all_jobs),
    }


def render_flow(lineage):
    nodes = lineage["nodes"]
    edges = lineage["edges"]

    children = defaultdict(list)
    has_parent = set()
    for edge in edges:
        children[edge["from"]].append((edge["to"], edge["relationship"]))
        has_parent.add(edge["to"])

    roots = [nid for nid in nodes if nid not in has_parent]

    TYPE_ICONS = {"resource": "📦", "revision": "📄", "job": "⚙️ ", "external": "🔗"}

    def node_summary(node):
        meta = node["meta"]
        t = node["type"]
        parts = []
        if t == "job":
            if meta.get("status"):
                parts.append(meta["status"])
            if meta.get("model_id"):
                parts.append(f"model:{meta['model_id'][:8]}…")
            if meta.get("created"):
                parts.append(meta["created"])
        elif t in ("resource", "revision"):
            if meta.get("size"):
                parts.append(fmt_size(meta["size"]))
            if meta.get("created"):
                parts.append(meta["created"])
            if meta.get("resource_type"):
                parts.append(meta["resource_type"])
        return f"  ({', '.join(parts)})" if parts else ""

    def id_line(node):
        meta = node["meta"]
        if node["type"] == "job":
            return f"job_id:   {meta.get('job_id', '')}"
        return f"rev_id:   {meta.get('revision_id', '')}"

    def print_node(node_id, prefix="", is_last=True, depth=0):
        node = nodes.get(node_id)
        if not node:
            return
        connector = "└── " if is_last else "├── "
        icon = TYPE_ICONS.get(node["type"], "   ")
        print(f"{prefix}{connector}{icon} {node['label']}{node_summary(node)}")
        detail_prefix = prefix + ("    " if is_last else "│   ")
        line = id_line(node)
        if line:
            print(f"{detail_prefix}    {line}")
        child_list = children.get(node_id, [])
        child_prefix = prefix + ("    " if is_last else "│   ")
        for i, (child_id, rel) in enumerate(child_list):
            is_child_last = i == len(child_list) - 1
            print(f"{child_prefix}{'└── ' if is_child_last else '├── '}─({rel})─▶")
            print_node(child_id, child_prefix + ("    " if is_child_last else "│   "), True, depth + 1)

    job_count = sum(1 for n in nodes.values() if n["type"] == "job")
    res_count = len(nodes) - job_count
    print(f"Job execution lineage: {len(nodes)} nodes ({job_count} job(s), {res_count} resource(s)), {len(edges)} edge(s)\n")
    for i, root in enumerate(roots):
        print_node(root, "", i == len(roots) - 1)


def render_dot(lineage):
    nodes = lineage["nodes"]
    edges = lineage["edges"]

    type_style = {
        "resource": ('box',       'lightgreen',  'black'),
        "revision": ('ellipse',   'lightyellow', 'black'),
        "job":      ('component', 'lightsalmon', 'black'),
        "external": ('box',       'lightgrey',   'grey'),
    }

    def safe_id(nid):
        return nid.replace(":", "_").replace("-", "_")

    def escape(s):
        return str(s).replace('"', '\\"').replace('\n', '\\n')

    lines = [
        "digraph job_lineage {",
        '  label="Job execution lineage";',
        '  labelloc=t;',
        '  rankdir=LR;',
        '  node [fontname="Helvetica" fontsize=11];',
        '  edge [fontsize=9];',
        "",
        "  subgraph cluster_jobs {",
        '    label="Jobs"; style=dashed; color=grey;',
    ]
    for nid, node in nodes.items():
        if node["type"] == "job":
            shape, fill, font = type_style["job"]
            meta = node["meta"]
            status = meta.get("status") or ""
            date = meta.get("created") or ""
            lbl = escape(node["label"])
            if status:
                lbl += f"\\n{status}"
            if date:
                lbl += f"\\n{date}"
            lines.append(f'    {safe_id(nid)} [label="{lbl}" shape={shape} fillcolor="{fill}" style=filled];')
    lines.append("  }")
    lines.append("")

    for nid, node in nodes.items():
        if node["type"] == "job":
            continue
        shape, fill, font = type_style.get(node["type"], ('box', 'white', 'black'))
        meta = node["meta"]
        lbl = escape(node["label"])
        details = []
        if meta.get("size"):
            details.append(fmt_size(meta["size"]))
        if meta.get("resource_type"):
            details.append(escape(meta["resource_type"]))
        if details:
            lbl += "\\n" + ", ".join(details)
        lines.append(f'  {safe_id(nid)} [label="{lbl}" shape={shape} fillcolor="{fill}" style=filled fontcolor="{font}"];')

    lines.append("")
    for edge in edges:
        rel = edge["relationship"]
        lines.append(
            f'  {safe_id(edge["from"])} -> {safe_id(edge["to"])} [label="{escape(rel)}" color=steelblue fontsize=9];'
        )

    lines.append("}")
    return "\n".join(lines)


def render_html(lineage):
    nodes = lineage["nodes"]
    edges = lineage["edges"]
    title = "Job Execution Lineage"

    type_colors = {
        "resource": {"background": "#d4edda", "border": "#5a9e6f"},
        "revision": {"background": "#fff3cd", "border": "#c0962a"},
        "job":      {"background": "#c8f0e8", "border": "#2a9d8f"},
        "external": {"background": "#e2e3e5", "border": "#999"},
    }

    vis_nodes = []
    vis_edges = []

    for nid, node in nodes.items():
        colors = type_colors.get(node["type"], {"background": "#fff", "border": "#999"})
        meta = node["meta"]
        label_lines = [node["label"]]
        if node["type"] == "job":
            if meta.get("status"):
                label_lines.append(meta["status"])
            if meta.get("created"):
                label_lines.append(meta["created"])
        elif node["type"] in ("resource", "revision"):
            if meta.get("resource_type"):
                label_lines.append(meta["resource_type"])
            if meta.get("size"):
                label_lines.append(fmt_size(meta["size"]))

        tooltip_lines = [f"<b>{node['type'].upper()}</b>"]
        for k, v in meta.items():
            if v:
                tooltip_lines.append(f"{k}: {v}")

        vis_nodes.append({
            "id": nid,
            "label": "\n".join(label_lines),
            "title": "<br/>".join(tooltip_lines),
            "shape": "box" if node["type"] != "revision" else "ellipse",
            "color": {
                "background": colors["background"],
                "border": colors["border"],
                "highlight": {"background": colors["background"], "border": "#333"},
            },
            "font": {"color": "#000", "size": 12, "face": "helvetica"},
            "margin": 10,
            "widthConstraint": {"maximum": 200},
        })

    for i, edge in enumerate(edges):
        vis_edges.append({
            "id": i,
            "from": edge["from"],
            "to": edge["to"],
            "label": edge["relationship"],
            "arrows": "to",
            "color": {"color": "#5a9e6f", "highlight": "#333"},
            "font": {"size": 10, "color": "#666", "align": "middle"},
            "smooth": {"type": "cubicBezier", "forceDirection": "horizontal"},
        })

    nodes_json = json.dumps(vis_nodes, indent=2)
    edges_json = json.dumps(vis_edges, indent=2)
    job_count = sum(1 for n in nodes.values() if n["type"] == "job")
    res_count = len(nodes) - job_count

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <script src="https://unpkg.com/vis-network@9.1.9/dist/vis-network.min.js"></script>
  <link href="https://unpkg.com/vis-network@9.1.9/dist/dist/vis-network.min.css" rel="stylesheet"/>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: Helvetica, Arial, sans-serif; background: #f8f9fa; }}
    #header {{
      padding: 12px 20px; background: #fff; border-bottom: 1px solid #ddd;
      display: flex; align-items: center; gap: 16px;
    }}
    #header h1 {{ font-size: 16px; font-weight: 600; color: #222; }}
    #header .meta {{ font-size: 12px; color: #888; }}
    #legend {{ display: flex; gap: 12px; align-items: center; margin-left: auto; }}
    .legend-item {{ display: flex; align-items: center; gap: 5px; font-size: 11px; color: #555; }}
    .legend-dot {{ width: 12px; height: 12px; border-radius: 2px; border: 1px solid #999; }}
    #network {{ width: 100%; height: calc(100vh - 52px); }}
    #tooltip {{
      position: fixed; background: #fff; border: 1px solid #ddd; border-radius: 6px;
      padding: 8px 12px; font-size: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.15);
      pointer-events: none; display: none; max-width: 300px; z-index: 999;
    }}
  </style>
</head>
<body>
  <div id="header">
    <h1>{title}</h1>
    <div class="meta">{len(nodes)} nodes &nbsp;·&nbsp; {job_count} job(s) &nbsp;·&nbsp; {res_count} resource(s) &nbsp;·&nbsp; {len(edges)} edge(s)</div>
    <div id="legend">
      <div class="legend-item"><div class="legend-dot" style="background:#d4edda;border-color:#5a9e6f"></div>Resource</div>
      <div class="legend-item"><div class="legend-dot" style="background:#c8f0e8;border-color:#2a9d8f"></div>Job</div>
      <div class="legend-item"><div class="legend-dot" style="background:#e2e3e5;border-color:#999"></div>External</div>
    </div>
  </div>
  <div id="network"></div>
  <div id="tooltip"></div>
  <script>
    const nodes = new vis.DataSet({nodes_json});
    const edges = new vis.DataSet({edges_json});
    const container = document.getElementById("network");
    const options = {{
      layout: {{
        hierarchical: {{
          enabled: true, direction: "LR", sortMethod: "directed",
          levelSeparation: 240, nodeSpacing: 110, treeSpacing: 160,
          blockShifting: true, edgeMinimization: true, parentCentralization: true,
        }}
      }},
      physics: {{ enabled: false }},
      interaction: {{ hover: true, tooltipDelay: 100, navigationButtons: true, keyboard: true }},
      edges: {{
        smooth: {{ type: "cubicBezier", forceDirection: "horizontal", roundness: 0.5 }},
        selectionWidth: 2,
      }},
      nodes: {{
        borderWidth: 1.5,
        shadow: {{ enabled: true, size: 4, x: 2, y: 2, color: "rgba(0,0,0,0.08)" }},
      }},
    }};
    const network = new vis.Network(container, {{ nodes, edges }}, options);
    const tooltip = document.getElementById("tooltip");
    network.on("hoverNode", params => {{
      const node = nodes.get(params.node);
      if (node && node.title) {{
        tooltip.innerHTML = node.title;
        tooltip.style.display = "block";
      }}
    }});
    network.on("blurNode", () => {{ tooltip.style.display = "none"; }});
    document.addEventListener("mousemove", e => {{
      tooltip.style.left = (e.clientX + 16) + "px";
      tooltip.style.top  = (e.clientY + 8)  + "px";
    }});
    network.once("stabilized", () => network.fit({{ animation: false }}));
  </script>
</body>
</html>"""


def main():
    args = parse_args()
    client = build_client(args)

    compat = client.check_compatibility()
    if compat:
        print(f"Connected to Istari platform  version={compat.server_version}")

    lineage = collect_lineage(client, args)

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

    elif args.format == "html":
        output = render_html(lineage)
        out_path = args.output or "job_lineage.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Interactive chart written to {out_path}")

    else:  # flow
        render_flow(lineage)
        if args.output:
            with open(args.output, "w") as f:
                json.dump(lineage, f, indent=2, default=str)
            print(f"\nJSON lineage data written to {args.output}")


if __name__ == "__main__":
    main()
