"""Visualize UAT/perf results — writes one self-contained HTML report.

    python -m uat.visualize                                # latest run, env perf
    python -m uat.visualize --run 20260610_104751 --env dev --open

Charts: UAT step latency (red x = FAIL), baseline entity counts (-1 = uncountable,
dips below the axis), perf per-call latency time series with errors overlaid as
red x, entity footprint over runs. Failures are data, not noise — always shown.
Needs the `experiment` extras (plotly, pandas).
"""

from __future__ import annotations

import argparse
import json
import webbrowser
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

_RESULTS = Path(__file__).parent / "results"
_FAIL_RED = "#d62728"
_STATUS_COLORS = {"PASS": "#2a9d8f", "FAIL": _FAIL_RED, "SKIP": "#999999"}


def _read_jsonl(path: Path) -> pd.DataFrame:
    return pd.DataFrame(json.loads(l) for l in path.read_text().splitlines())


def step_latency(run: dict, name: str) -> go.Figure:
    """One point per step in execution order; FAIL = red x, SKIP = hollow."""
    steps = pd.DataFrame(run["steps"]).reset_index(names="order")
    steps["call"] = steps["description"].str.split(" — ").str[0]
    if "error" not in steps:
        steps["error"] = ""
    steps["error"] = steps["error"].fillna("")
    fig = px.scatter(
        steps, x="order", y="duration_s", color="status", symbol="status",
        color_discrete_map=_STATUS_COLORS,
        symbol_map={"PASS": "circle", "FAIL": "x", "SKIP": "circle-open"},
        hover_data={"suite": True, "call": True, "error": True, "order": False},
        title=f"{name} · {run['env']} — step latency (execution order) · {run['summary']}",
    )
    fig.update_traces(marker_size=9)
    fig.update_layout(xaxis_title="step #", yaxis_title="seconds", height=420)
    return fig


def baseline_counts(run: dict, name: str) -> go.Figure | None:
    """Baseline vs final entity counts; -1 (uncountable) plots below the axis."""
    if not run.get("baseline"):
        return None

    def rows(d: dict, when: str) -> pd.DataFrame:
        items = [(k, v) for k, v in d.items() if k not in ("taken_at", "env")]
        return pd.DataFrame(items, columns=["entity", "count"]).assign(when=when)

    df = rows(run["baseline"], "baseline")
    if run.get("final_counts"):
        df = pd.concat([df, rows(run["final_counts"], "final")])
    n_bad = int((df["count"] == -1).sum())
    fig = px.bar(
        df, x="entity", y="count", color="when", barmode="group", text="count",
        title=f"{name} · {run['env']} — entity counts ({n_bad}/{len(df)} uncountable, shown as -1)",
    )
    fig.update_layout(yaxis_title="count", height=380)
    return fig


def perf_latency(env: str) -> go.Figure | None:
    """Per-call latency over time from {env}.samples.jsonl; errors as red x."""
    path = _RESULTS / "perf" / f"{env}.samples.jsonl"
    if not path.exists():
        return None
    s = _read_jsonl(path)
    s["started_at"] = pd.to_datetime(s["started_at"])
    ok, err = s[s["status"] == "ok"], s[s["status"] != "ok"]
    fig = px.line(
        ok, x="started_at", y="duration_s", color="operation", markers=True,
        hover_data=["run_id", "iteration"],
        title=f"{env} — per-call latency ({len(s)} samples, {len(err)} errors)",
    )
    fig.add_scatter(
        x=err["started_at"], y=err["duration_s"], mode="markers", name="error",
        marker=dict(color=_FAIL_RED, symbol="x", size=11),
        text=err["operation"] + ": " + err["error"].str.slice(0, 120),
    )
    fig.update_layout(yaxis_title="seconds", height=420)
    return fig


def perf_footprint(env: str) -> go.Figure | None:
    """Entity counts over runs from {env}.baseline.jsonl; -1 as red x at zero."""
    path = _RESULTS / "perf" / f"{env}.baseline.jsonl"
    if not path.exists():
        return None
    b = _read_jsonl(path)
    b["taken_at"] = pd.to_datetime(b["taken_at"])
    long = b.melt(id_vars=["run_id", "env", "taken_at"], var_name="entity", value_name="count")
    ok, bad = long[long["count"] >= 0], long[long["count"] < 0]
    fig = px.line(
        ok, x="taken_at", y="count", color="entity", markers=True,
        title=f"{env} — entity footprint over runs ({len(bad)} uncountable)",
    )
    fig.add_scatter(
        x=bad["taken_at"], y=[0] * len(bad), mode="markers", name="uncountable (-1)",
        marker=dict(color=_FAIL_RED, symbol="x", size=11), text=bad["entity"],
    )
    fig.update_layout(yaxis_title="count", height=380)
    return fig


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render UAT/perf results to HTML",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--run", help="run id (default: latest run_*.json)")
    parser.add_argument("--env", default="perf", help="env for the perf time series (default: perf)")
    parser.add_argument("--out", help="output HTML path (default: results/viz_{run}.html)")
    parser.add_argument("--open", action="store_true", help="open the report in a browser")
    args = parser.parse_args()

    run_path = (_RESULTS / f"run_{args.run}.json") if args.run \
        else sorted(_RESULTS.glob("run_*.json"))[-1]
    run = json.loads(run_path.read_text())
    name = run_path.stem

    figs = [f for f in (
        step_latency(run, name),
        baseline_counts(run, name),
        perf_latency(args.env),
        perf_footprint(args.env),
    ) if f is not None]

    out = Path(args.out) if args.out else _RESULTS / f"viz_{name.removeprefix('run_')}.html"
    body = "".join(
        f.to_html(full_html=False, include_plotlyjs="cdn" if i == 0 else False)
        for i, f in enumerate(figs)
    )
    out.write_text(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{name}</title></head>"
        f"<body><h2>{name} · env={run['env']} · perf series: {args.env}</h2>{body}</body></html>",
        encoding="utf-8",
    )
    print(f"{len(figs)} chart(s) → {out}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
