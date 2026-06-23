"""Console summary of a measurement run — per-(env, operation) latency + trend.

`trend` is last sample minus first across the run's repeats: positive means the
endpoint got slower as the run progressed (the within-run signal). Longer-term
trends come from the persisted JSONL — see store.py.
"""

from __future__ import annotations

import statistics
from logging import Logger

from uat.perf.store import Sample

_HEADER = f"  {'operation':<18} {'env':<6} {'n':>3} {'ok':>3} {'min':>8} {'med':>8} {'max':>8} {'trend':>8}"


def summarize(samples: list[Sample], log: Logger) -> None:
    if not samples:
        return
    by_key: dict[tuple[str, str], list[Sample]] = {}
    for s in samples:
        by_key.setdefault((s.env, s.operation), []).append(s)

    log.info(f"\n{'─' * 68}\n{_HEADER}\n{'─' * 68}")
    for (env, op), rows in sorted(by_key.items()):
        ok = [r.duration_s for r in rows if r.status == "ok"]
        if not ok:
            log.info(f"  {op:<18} {env:<6} {len(rows):>3} {0:>3}   (all errored)")
            continue
        trend = ok[-1] - ok[0]
        log.info(
            f"  {op:<18} {env:<6} {len(rows):>3} {len(ok):>3} "
            f"{min(ok):>8.3f} {statistics.median(ok):>8.3f} {max(ok):>8.3f} {trend:>+8.3f}"
        )
    log.info("─" * 68)
