"""Perf result storage — the time-series contract dashboards read.

Two append-only JSONL streams per env, under uat/results/perf/:

    {env}.samples.jsonl   one line per measured call  (the latency series)
    {env}.baseline.jsonl  one line per run            (the entity-count footprint)

Both carry run_id + an ISO timestamp, so a dashboard can plot latency and
footprint over time for one env, and compare envs by filtering the same schema.
Appending (never rewriting) keeps every run's history; one file per env keeps a
single env's dashboard a single file read.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PERF_DIR = Path(__file__).resolve().parent.parent / "results" / "perf"


@dataclass
class Sample:
    """One timed endpoint call."""
    run_id: str
    env: str
    operation: str
    iteration: int
    started_at: str        # ISO-8601 UTC — the dashboard x-axis
    duration_s: float
    status: str            # "ok" | "error"
    error: str = ""
    upload_mb: float | None = None    # payload size for this run's uploads (None = dummy.txt)
    resource_id: str | None = None    # created model/file/resource id — for server-log correlation
    revision_id: str | None = None    # created file-revision id


def samples_path(env: str) -> Path:
    return PERF_DIR / f"{env}.samples.jsonl"


def baseline_path(env: str) -> Path:
    return PERF_DIR / f"{env}.baseline.jsonl"


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    PERF_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, default=str) + "\n")


def write_samples(samples: list[Sample]) -> None:
    """Append samples to their env's stream (grouped so each file opens once)."""
    by_env: dict[str, list[dict[str, Any]]] = {}
    for s in samples:
        by_env.setdefault(s.env, []).append(asdict(s))
    for env, rows in by_env.items():
        _append_jsonl(samples_path(env), rows)


def write_baseline(run_id: str, baseline: Any, extra: dict | None = None) -> None:
    """Append one footprint row (a PlatformCounts: .env, .taken_at, + counts).
    `extra` merges extra per-run context (e.g. network ul_mbps/dl_mbps/rpm)."""
    _append_jsonl(baseline_path(baseline.env), [{"run_id": run_id, **asdict(baseline), **(extra or {})}])
