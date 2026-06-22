"""Visualize UAT/perf results — writes one self-contained HTML report.

    python -m uat.visualize                                # latest run, env perf
    python -m uat.visualize --run 20260610_104751 --env dev --open

Charts: per-run summary table (payload, latency stats, effective throughput, uplink,
footprint), perf upload latency vs. models-extant (x = footprint not wall-clock; color =
run; log-y; hover shows resource/revision id), plus UAT step latency + baseline counts
when a run JSON exists. Needs `experiment` extras.
"""

from __future__ import annotations

import argparse
import json
import webbrowser
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

_RESULTS = Path(__file__).parent / "results"
_FAIL_RED = "#d62728"
_STATUS_COLORS = {"PASS": "#2a9d8f", "FAIL": _FAIL_RED, "SKIP": "#999999"}


def _read_jsonl(path: Path) -> pd.DataFrame:
    return pd.DataFrame(json.loads(l) for l in path.read_text().splitlines())


def _run_label(run_id: str) -> str:
    """'20260622_112711' → '2026-06-22 11:27' for display; pass through if unparseable."""
    try:
        return datetime.strptime(str(run_id), "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(run_id)


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


def _run_start_models(env: str) -> dict[str, int]:
    """Per-run starting model count = the earliest (before-run) baseline row's `models`.
    Lets us put each upload on a footprint x-axis. -1 (uncountable) → omitted (run falls
    back to a 0 offset, so it's plotted by upload # and labeled approximate)."""
    bpath = _RESULTS / "perf" / f"{env}.baseline.jsonl"
    if not bpath.exists():
        return {}
    b = _read_jsonl(bpath).sort_values("taken_at")
    start = {}
    for rid, g in b.groupby("run_id"):
        m = int(g.iloc[0]["models"])
        if m >= 0:
            start[rid] = m
    return start


def perf_latency(env: str) -> go.Figure | None:
    """Each upload's latency vs. how many models existed when it ran (run start + upload #).

    x = footprint (not wall-clock), so runs at different baselines occupy different x and
    form one continuous curve; color = run. log-y so a 5s call and a 187s spike both read.
    Tuned for upload runs (each call adds one model); meaningless for read-only ops.
    """
    path = _RESULTS / "perf" / f"{env}.samples.jsonl"
    if not path.exists():
        return None
    s = _read_jsonl(path)
    for c in ("resource_id", "revision_id"):  # absent in older/recovered rows
        if c not in s:
            s[c] = None
    start = _run_start_models(env)
    # Only runs with a countable starting baseline can be placed on the footprint axis.
    # Runs whose baseline timed out are DROPPED here (never plotted at a fake 0) — they
    # still appear in the summary table, marked unknown.
    excluded = sorted(set(s["run_id"]) - set(start))
    s = s[s["run_id"].isin(start)].copy()
    if s.empty:
        return None
    s["models_extant"] = s.apply(lambda r: start[r["run_id"]] + int(r["iteration"]), axis=1)
    s["run"] = s["run_id"].map(_run_label)
    note = f" — {len(excluded)} run(s) hidden: baseline uncountable" if excluded else ""
    fig = px.scatter(
        s, x="models_extant", y="duration_s", color="run", symbol="status",
        symbol_map={"ok": "circle", "error": "x", "timeout": "x"},
        hover_data=["iteration", "upload_mb", "status", "resource_id", "revision_id", "started_at"],
        log_y=True,
        title=f"{env} — upload latency vs. models extant ({len(s)} placed uploads){note}",
    )
    fig.update_traces(marker_size=7)
    fig.update_layout(xaxis_title="models extant (run start + upload #)",
                      yaxis_title="seconds (log scale)", height=460)
    return fig


def perf_unknown_runs(env: str) -> go.Figure | None:
    """Latency scatter for runs whose baseline was uncountable, so they can't go on the
    footprint axis: x = run, y = each upload's duration (jittered, log-y). Keeps their data
    visible. Returns None when every run has a countable baseline (nothing to show)."""
    path = _RESULTS / "perf" / f"{env}.samples.jsonl"
    if not path.exists():
        return None
    s = _read_jsonl(path)
    for c in ("resource_id", "revision_id"):
        if c not in s:
            s[c] = None
    known = _run_start_models(env)
    s = s[~s["run_id"].isin(known)].copy()
    if s.empty:
        return None
    s["run"] = s["run_id"].map(_run_label)
    fig = px.strip(
        s, x="run", y="duration_s", color="run", log_y=True,
        hover_data=["iteration", "upload_mb", "status", "resource_id", "revision_id", "started_at"],
        title=f"{env} — uncountable-baseline runs ({s['run'].nunique()} run(s), {len(s)} uploads) — no footprint x",
    )
    fig.update_traces(marker_size=5, jitter=0.4)
    fig.update_layout(xaxis_title="run", yaxis_title="seconds (log scale)",
                      showlegend=False, height=440)
    return fig


def perf_run_summary_html(env: str) -> str | None:
    """One HTML table row per run: payload, latency stats, effective throughput, uplink,
    footprint. Rendered as a native <table> (plotly's go.Table embeds blank in multi-figure
    HTML) — also keeps the numbers selectable/copyable.

    Effective throughput (payload ÷ median latency) is the *achieved* upload speed end-to-end;
    compare it to the measured uplink (`--measure-network`): if effective << uplink, the
    platform dominates, not the network.
    """
    spath = _RESULTS / "perf" / f"{env}.samples.jsonl"
    if not spath.exists():
        return None
    s = _read_jsonl(spath)
    start = _run_start_models(env)
    # uplink (ul_mbps) per run from the baseline row, if --measure-network captured it
    bpath = _RESULTS / "perf" / f"{env}.baseline.jsonl"
    uplink: dict[str, float] = {}
    if bpath.exists():
        b = _read_jsonl(bpath)
        if "ul_mbps" in b:
            for rid, g in b.groupby("run_id"):
                vals = g["ul_mbps"].dropna()
                if len(vals):
                    uplink[str(rid)] = float(vals.iloc[0])

    cols = ["run", "uploads", "ok", "err/timeout", "payload MB", "median s", "p95 s", "max s",
            "eff MB/s", "eff Mbps", "uplink Mbps", "models"]
    rows: list[list] = []
    for rid, g in s.groupby("run_id"):
        rid = str(rid)
        ok = g[g["status"] == "ok"]["duration_s"]
        bad = int((g["status"] != "ok").sum())
        med = float(ok.median()) if len(ok) else None
        p95 = float(ok.quantile(0.95)) if len(ok) else None
        mb = g["upload_mb"].dropna()
        payload = float(mb.iloc[0]) if len(mb) else None
        eff = (payload / med) if (payload and med) else None
        st = start.get(rid)
        rows.append([
            _run_label(rid), len(g), len(ok), bad,
            payload if payload is not None else "—",
            f"{med:.2f}" if med is not None else "—",
            f"{p95:.2f}" if p95 is not None else "—",
            f"{ok.max():.2f}" if len(ok) else "—",
            f"{eff:.2f}" if eff else "—",
            f"{eff * 8:.1f}" if eff else "—",
            f"{uplink[rid]:.0f}" if rid in uplink else "—",
            f"{st}→{st + len(g)}" if st is not None else "unknown (baseline failed)",
        ])
    th = "".join(f"<th style='text-align:left;padding:4px 12px;border-bottom:2px solid #444'>{c}</th>" for c in cols)
    trs = "".join("<tr>" + "".join(f"<td style='padding:4px 12px;border-bottom:1px solid #ddd'>{v}</td>"
                                   for v in r) + "</tr>" for r in rows)
    return (f"<h3 style='font-family:sans-serif'>{env} — per-run summary ({len(rows)} run(s))</h3>"
            f"<table style='border-collapse:collapse;font-family:ui-monospace,monospace;font-size:13px'>"
            f"<thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>")


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

    # A UAT run JSON drives the step-latency + baseline charts; it's optional —
    # with only perf JSONL (e.g. after clearing results), render the perf charts alone.
    run, name = None, f"perf-{args.env}"
    if args.run:
        run = json.loads((_RESULTS / f"run_{args.run}.json").read_text())
        name = f"run_{args.run}"
    elif (found := sorted(_RESULTS.glob("run_*.json"))):
        run = json.loads(found[-1].read_text())
        name = found[-1].stem

    summary_html = perf_run_summary_html(args.env)  # native HTML table, not a plotly fig
    figs = [f for f in (
        perf_latency(args.env),
        perf_unknown_runs(args.env),
        step_latency(run, name) if run else None,
        baseline_counts(run, name) if run else None,
    ) if f is not None]
    if not figs and not summary_html:
        print(f"nothing to plot: no run_*.json and no perf samples for env {args.env!r}")
        return

    env_label = run["env"] if run else args.env
    out = Path(args.out) if args.out else _RESULTS / f"viz_{name.removeprefix('run_')}.html"
    body = (summary_html or "") + "".join(
        f.to_html(full_html=False, include_plotlyjs="cdn" if i == 0 else False)
        for i, f in enumerate(figs)
    )
    out.write_text(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{name}</title></head>"
        f"<body><h2>{name} · env={env_label} · perf series: {args.env}</h2>{body}</body></html>",
        encoding="utf-8",
    )
    print(f"{len(figs)} chart(s) + {'summary table' if summary_html else 'no table'} → {out}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
