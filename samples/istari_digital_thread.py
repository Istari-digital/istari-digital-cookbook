"""
Trace the digital thread for a model in the Istari platform.

Walks the model's artifacts and jobs, following source/product relationships
on file revisions to build a connected graph, then prints it as a tree.

Configuration via environment variables or CLI flags:
  ISTARI_REGISTRY_URL        - Platform URL
  ISTARI_REGISTRY_AUTH_TOKEN - Personal access token

Usage:
  python istari_digital_thread.py --model-id <id>
  python istari_digital_thread.py --model-id <id> --output thread.json
  python istari_digital_thread.py --model-id <id> --format dot > thread.dot
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trace the Istari digital thread for a model",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--model-id", required=True, metavar="ID", help="Istari model ID to trace")
    parser.add_argument("--url", default=None, help="Istari registry URL (overrides ISTARI_REGISTRY_URL)")
    parser.add_argument("--token", default=None, help="Istari auth token (overrides ISTARI_REGISTRY_AUTH_TOKEN)")
    parser.add_argument(
        "--output", default=None, metavar="FILE",
        help="Write JSON thread output to this file (default: print to stdout)",
    )
    parser.add_argument(
        "--format", default="tree", choices=["tree", "json", "dot"],
        help="Output format: tree (default), json, or dot (Graphviz)",
    )
    parser.add_argument(
        "--depth", type=int, default=5,
        help="Max relationship depth to follow (default: 5)",
    )
    return parser.parse_args()


def build_client(args):
    from istari_digital_client import Client, Configuration
    registry_url = args.url or os.environ.get("ISTARI_REGISTRY_URL")
    registry_auth_token = args.token or os.environ.get("ISTARI_REGISTRY_AUTH_TOKEN")
    if not registry_url:
        sys.exit("Error: registry URL not set. Use --url or set ISTARI_REGISTRY_URL.")
    if not registry_auth_token:
        sys.exit("Error: auth token not set. Use --token or set ISTARI_REGISTRY_AUTH_TOKEN.")
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


def revision_label(rev):
    name = safe_get(rev, "display_name") or safe_get(rev, "name") or safe_get(rev, "stem") or ""
    ext = safe_get(rev, "extension") or safe_get(rev, "suffix") or ""
    if ext and not name.endswith(ext):
        name = f"{name}.{ext.lstrip('.')}" if name else ext
    return name or rev.id[:8]


def collect_thread(client, model_id, max_depth):
    """
    Walk the model's artifacts and jobs, following source/product links
    on file revisions. Returns a graph dict:
      nodes: {id: {type, label, meta}}
      edges: [{from, to, relationship}]
    """
    nodes = {}
    edges = []
    visited_revisions = set()

    def add_node(node_id, node_type, label, meta=None):
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "type": node_type, "label": label, "meta": meta or {}}

    def add_edge(from_id, to_id, relationship):
        edge = {"from": from_id, "to": to_id, "relationship": relationship}
        if edge not in edges:
            edges.append(edge)

    def walk_revision(rev, parent_id, parent_type, depth):
        if depth > max_depth or rev.id in visited_revisions:
            return
        visited_revisions.add(rev.id)

        rev_label = revision_label(rev)
        rev_node_id = f"rev:{rev.id}"
        add_node(rev_node_id, "revision", rev_label, {
            "revision_id": rev.id,
            "file_id": safe_get(rev, "file_id"),
            "extension": safe_get(rev, "extension"),
            "size": safe_get(rev, "size"),
            "created": fmt_date(safe_get(rev, "created")),
        })
        add_edge(parent_id, rev_node_id, "has_revision")

        # Follow sources (inputs to this revision)
        for source in (safe_get(rev, "sources") or []):
            src_rev_id = safe_get(source, "revision_id")
            rel = safe_get(source, "relationship_identifier") or "source"
            if src_rev_id:
                src_node_id = f"rev:{src_rev_id}"
                add_node(src_node_id, "revision", src_rev_id[:8], {
                    "revision_id": src_rev_id,
                    "resource_type": safe_get(source, "resource_type"),
                    "resource_id": safe_get(source, "resource_id"),
                })
                add_edge(src_node_id, rev_node_id, rel)
                # Try to resolve and walk the source revision
                try:
                    src_rev = client.get_revision(revision_id=src_rev_id)
                    if src_rev and src_rev.id not in visited_revisions:
                        nodes[src_node_id]["label"] = revision_label(src_rev)
                        walk_revision(src_rev, src_node_id, "revision", depth + 1)
                except Exception:
                    pass

        # Follow products (outputs derived from this revision)
        for product in (safe_get(rev, "products") or []):
            prod_rev_id = safe_get(product, "revision_id")
            rel = safe_get(product, "relationship_identifier") or "product"
            if prod_rev_id:
                prod_node_id = f"rev:{prod_rev_id}"
                add_node(prod_node_id, "revision", prod_rev_id[:8], {
                    "revision_id": prod_rev_id,
                    "resource_type": safe_get(product, "resource_type"),
                    "resource_id": safe_get(product, "resource_id"),
                })
                add_edge(rev_node_id, prod_node_id, rel)
                try:
                    prod_rev = client.get_revision(revision_id=prod_rev_id)
                    if prod_rev and prod_rev.id not in visited_revisions:
                        nodes[prod_node_id]["label"] = revision_label(prod_rev)
                        walk_revision(prod_rev, prod_node_id, "revision", depth + 1)
                except Exception:
                    pass

    # --- Root model ---
    print(f"Fetching model {model_id}...")
    try:
        model = client.get_model(model_id=model_id)
    except Exception as e:
        sys.exit(f"Error fetching model: {e}")

    model_label = safe_get(model, "file", "revisions", default=[])
    # Get display name from the file's latest revision
    file_revs = safe_get(model, "file", "revisions") or []
    model_name = None
    if file_revs:
        latest = file_revs[-1]
        model_name = safe_get(latest, "display_name") or safe_get(latest, "name") or safe_get(latest, "stem")
    model_name = model_name or model_id[:8]

    model_node_id = f"model:{model_id}"
    add_node(model_node_id, "model", model_name, {"model_id": model_id})

    # Walk model file revisions
    for rev in file_revs:
        walk_revision(rev, model_node_id, "model", depth=1)

    # --- Artifacts ---
    artifacts = safe_get(model, "artifacts") or []
    print(f"  Found {len(artifacts)} artifact(s)...")
    for artifact in artifacts:
        art_id = artifact.id
        art_revs = safe_get(artifact, "file", "revisions") or []
        art_name = None
        if art_revs:
            latest = art_revs[-1]
            art_name = safe_get(latest, "display_name") or safe_get(latest, "name") or safe_get(latest, "stem")
        art_name = art_name or art_id[:8]

        art_node_id = f"artifact:{art_id}"
        add_node(art_node_id, "artifact", art_name, {"artifact_id": art_id, "model_id": model_id})
        add_edge(model_node_id, art_node_id, "has_artifact")

        for rev in art_revs:
            walk_revision(rev, art_node_id, "artifact", depth=1)

    # --- Jobs ---
    jobs = safe_get(model, "jobs") or []
    if not jobs:
        # Try fetching separately
        try:
            result = client.list_model_jobs(model_id=model_id, size=100)
            jobs = result.items if hasattr(result, "items") and result.items else \
                   result.content if hasattr(result, "content") and result.content else []
        except Exception:
            pass

    print(f"  Found {len(jobs)} job(s)...")
    for job in jobs:
        job_id = job.id
        fn = safe_get(job, "function", "name") or safe_get(job, "function") or "unknown"
        if hasattr(fn, "name"):
            fn = fn.name
        status = None
        if job.status:
            s = job.status
            for attr in ("status_name", "name", "value", "status"):
                v = getattr(s, attr, None)
                if v:
                    status = str(v)
                    break

        job_label = f"{fn}"
        job_node_id = f"job:{job_id}"
        add_node(job_node_id, "job", job_label, {
            "job_id": job_id,
            "function": str(fn),
            "status": status,
            "model_id": model_id,
        })
        add_edge(model_node_id, job_node_id, "has_job")

        # Walk job file revisions
        job_revs = safe_get(job, "file", "revisions") or []
        for rev in job_revs:
            walk_revision(rev, job_node_id, "job", depth=1)

    return {"model_id": model_id, "nodes": nodes, "edges": edges}


def render_tree(thread):
    nodes = thread["nodes"]
    edges = thread["edges"]

    # Build adjacency list
    children = defaultdict(list)
    has_parent = set()
    for edge in edges:
        children[edge["from"]].append((edge["to"], edge["relationship"]))
        has_parent.add(edge["to"])

    # Find roots (nodes with no parent)
    roots = [nid for nid in nodes if nid not in has_parent]

    def print_node(node_id, prefix="", is_last=True, depth=0):
        node = nodes.get(node_id)
        if not node:
            return
        connector = "└── " if is_last else "├── "
        type_tag = f"[{node['type']}]"
        label = node["label"]
        meta = node["meta"]

        # Show key metadata inline
        extras = []
        if node["type"] == "job":
            if meta.get("status"):
                extras.append(meta["status"])
        elif node["type"] == "revision":
            if meta.get("extension"):
                extras.append(meta["extension"])
            if meta.get("size"):
                size = meta["size"]
                extras.append(f"{size:,} bytes" if size > 1024 else f"{size} B")
        extra_str = f"  ({', '.join(extras)})" if extras else ""

        print(f"{prefix}{connector}{type_tag} {label}{extra_str}")

        child_list = children.get(node_id, [])
        child_prefix = prefix + ("    " if is_last else "│   ")
        for i, (child_id, rel) in enumerate(child_list):
            is_child_last = i == len(child_list) - 1
            print(f"{child_prefix}{'└── ' if is_child_last else '├── '}─({rel})─▶")
            print_node(child_id, child_prefix + ("    " if is_child_last else "│   "), True, depth + 1)

    for i, root in enumerate(roots):
        print_node(root, "", i == len(roots) - 1)


def render_dot(thread):
    nodes = thread["nodes"]
    edges = thread["edges"]

    type_colors = {
        "model": "lightblue",
        "artifact": "lightgreen",
        "job": "lightyellow",
        "revision": "lightsalmon",
    }

    lines = ["digraph digital_thread {", '  rankdir=LR;', '  node [shape=box fontname="Helvetica"];']
    for node_id, node in nodes.items():
        safe_id = node_id.replace(":", "_").replace("-", "_")
        color = type_colors.get(node["type"], "white")
        label = node["label"].replace('"', '\\"')
        lines.append(f'  {safe_id} [label="{label}\\n({node["type"]})" fillcolor="{color}" style=filled];')

    for edge in edges:
        from_id = edge["from"].replace(":", "_").replace("-", "_")
        to_id = edge["to"].replace(":", "_").replace("-", "_")
        rel = edge["relationship"]
        lines.append(f'  {from_id} -> {to_id} [label="{rel}"];')

    lines.append("}")
    return "\n".join(lines)


def main():
    args = parse_args()
    client = build_client(args)

    compat = client.check_compatibility()
    if compat:
        print(f"Connected to Istari platform  version={compat.server_version}")

    thread = collect_thread(client, args.model_id, args.depth)

    node_count = len(thread["nodes"])
    edge_count = len(thread["edges"])
    print(f"\nDigital thread: {node_count} node(s), {edge_count} relationship(s)\n")

    if args.format == "json":
        output = json.dumps(thread, indent=2, default=str)
        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
            print(f"Written to {args.output}")
        else:
            print(output)

    elif args.format == "dot":
        output = render_dot(thread)
        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
            print(f"Written to {args.output}")
        else:
            print(output)

    else:  # tree
        render_tree(thread)
        if args.output:
            # Also write JSON when --output is specified with tree format
            with open(args.output, "w") as f:
                json.dump(thread, f, indent=2, default=str)
            print(f"\nJSON thread data written to {args.output}")


if __name__ == "__main__":
    main()
