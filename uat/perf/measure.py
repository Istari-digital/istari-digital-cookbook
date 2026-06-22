"""Measurement loop — *how* the perf harness times.

Run each operation `repeat` times against one already-built context, timing every
call. A failing call is recorded as an "error" sample (with the message) rather than
aborting — a degraded endpoint is itself a measurement.

Two robustness guards, learned the hard way (a run once hung 4 days on a dead socket
and lost everything because samples were only written at the end):
  • each call is bounded by `call_timeout_s` (a hung socket → a "timeout" sample, not
    an infinite stall); the abandoned worker thread is a daemon and dies with the process
  • each sample is flushed via `on_sample` the instant it completes, so a crash/hang
    loses at most the in-flight call, never the whole run
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from logging import Logger
from typing import Callable

from uat.common import TestContext
from uat.perf.operations import Operation
from uat.perf.store import Sample


def _call_bounded(fn: Callable[[], object], timeout_s: float) -> tuple[str, str, object]:
    """Run fn in a daemon thread; return (status, error, result). 'timeout' if it overruns."""
    box: dict[str, object] = {"exc": None, "result": None}

    def work() -> None:
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001 — surfaced via box
            box["exc"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        return "timeout", f"exceeded {timeout_s:.0f}s (call abandoned, still running)", None
    if box["exc"] is not None:
        return "error", f"{type(box['exc']).__name__}: {box['exc']}", None
    return "ok", "", box["result"]


def _ids(obj: object) -> tuple[str | None, str | None]:
    """Best-effort (resource_id, revision_id) from a created object, for log correlation.
    Covers v3 (resource_id/file_revision_id) and v2 (id/latest_revision.id)."""
    if obj is None:
        return None, None
    rid = getattr(obj, "resource_id", None) or getattr(obj, "id", None)
    rev = getattr(obj, "file_revision_id", None) or getattr(getattr(obj, "latest_revision", None), "id", None)
    return (str(rid) if rid is not None else None), (str(rev) if rev is not None else None)


def measure(
    ctx: TestContext,
    ops: list[Operation],
    repeat: int,
    log: Logger,
    call_timeout_s: float = 300.0,
    on_sample: Callable[[Sample], None] | None = None,
) -> list[Sample]:
    samples: list[Sample] = []
    for op in ops:
        if op.setup:
            try:
                op.setup(ctx)
            except Exception as exc:  # noqa: BLE001 — fixture failed; skip this op
                log.error(f"  [{ctx.env}] {op.name}: setup failed ({exc}); skipping")
                continue
        # most ops run `repeat` times; pool-derived ops (the relationship chain)
        # run as many times as their inputs allow — 0 means inputs aren't there yet.
        count = op.iterations(ctx, repeat) if op.iterations else repeat
        if count == 0:
            log.warning(f"  [{ctx.env}] {op.name}: no work (needs the upload pool — "
                        "run create_resource first, with repeat ≥ 2); skipping")
            continue
        for i in range(1, count + 1):
            started_at = datetime.now(timezone.utc).isoformat()
            t0 = time.perf_counter()
            status, error, result = _call_bounded(lambda: op.call(ctx), call_timeout_s)
            duration = time.perf_counter() - t0
            resource_id, revision_id = _ids(result)
            s = Sample(ctx.run_id, ctx.env, op.name, i, started_at, duration, status, error,
                       upload_mb=ctx.shared.get("upload_mb"),
                       resource_id=resource_id, revision_id=revision_id)
            samples.append(s)
            if on_sample:
                on_sample(s)  # flush immediately — survive a later hang/crash
            log.info(f"  [{ctx.env}] {op.name} {i}/{count}  {duration:7.3f}s  {status}")
    return samples
