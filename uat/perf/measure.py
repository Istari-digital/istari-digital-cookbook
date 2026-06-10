"""Measurement loop — *how* the perf harness times.

Run each operation `repeat` times against one already-built context, timing
every call. A failing call is recorded as an "error" sample (with the message)
rather than aborting — a degraded endpoint is itself a measurement.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from logging import Logger

from uat.common import TestContext
from uat.perf.operations import Operation
from uat.perf.store import Sample


def measure(ctx: TestContext, ops: list[Operation], repeat: int, log: Logger) -> list[Sample]:
    samples: list[Sample] = []
    for op in ops:
        if op.setup:
            try:
                op.setup(ctx)
            except Exception as exc:  # noqa: BLE001 — fixture failed; skip this op
                log.error(f"  [{ctx.env}] {op.name}: setup failed ({exc}); skipping")
                continue
        for i in range(1, repeat + 1):
            started_at = datetime.now(timezone.utc).isoformat()
            t0 = time.perf_counter()
            status, error = "ok", ""
            try:
                op.call(ctx)
            except Exception as exc:  # noqa: BLE001 — recorded as an error sample
                status, error = "error", f"{type(exc).__name__}: {exc}"
            duration = time.perf_counter() - t0
            samples.append(Sample(ctx.run_id, ctx.env, op.name, i, started_at, duration, status, error))
            log.info(f"  [{ctx.env}] {op.name} {i}/{repeat}  {duration:7.3f}s  {status}")
    return samples
