"""Perf measurement CLI — run a subset of endpoints N times per env, record latency.

    python -m uat.perf --env perf --repeat 10
    python -m uat.perf --env dev,stage --ops upload_model,list_models --repeat 20
    python -m uat.perf --list

One invocation may target several envs (comma-separated) sharing a run_id, so a
single run can compare envs. Results append to uat/results/perf/{env}.samples.jsonl
(+ {env}.baseline.jsonl) — a per-env time series for dashboards. The env name is
the .env.{env} suffix and is stamped on every sample and baseline row.
"""

from __future__ import annotations

import sys
from pathlib import Path

# When run as a script the repo root (which holds the `uat` package) isn't on
# sys.path; add it before importing uat.*.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import argparse
from datetime import datetime

from uat.common import build_context, setup_logging, take_baseline
from uat.perf import store
from uat.perf.measure import measure
from uat.perf.operations import OPERATIONS, Operation, make_junk_file, measure_network
from uat.perf.report import summarize


def _resolve_ops(spec: str | None) -> list[Operation]:
    if not spec:
        return list(OPERATIONS.values())
    resolved = []
    for name in (s.strip() for s in spec.split(",") if s.strip()):
        if name not in OPERATIONS:
            print(f"Unknown operation: {name!r}. Run --list to see them.", file=sys.stderr)
            sys.exit(1)
        resolved.append(OPERATIONS[name])
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Istari SDK perf measurement runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--env", default="perf", help="comma-separated env(s), e.g. 'perf' or 'dev,stage'")
    parser.add_argument("--ops", metavar="OP[,OP...]", help="operations to measure; default all")
    parser.add_argument("--repeat", type=int, default=10, help="calls per operation (default: 10)")
    parser.add_argument("--no-baseline", action="store_true", help="skip the entity-count footprint baseline")
    parser.add_argument("--no-cleanup", action="store_true", help="skip archiving resources created during the run")
    parser.add_argument("--make-junk", type=int, metavar="MB",
                        help="generate a random MB-sized junk file (uat/data/junk_{MB}mb.bin) and exit")
    parser.add_argument("--upload-mb", type=int, default=10, metavar="MB",
                        help="upload ops send an auto-generated junk file of this size "
                             "(default 10; 0 = an empty 0-byte file)")
    parser.add_argument("--call-timeout", type=float, default=300.0, metavar="S",
                        help="abandon any single call after S seconds → 'timeout' sample (default: 300)")
    parser.add_argument("--no-network", action="store_true",
                        help="skip the macOS networkQuality uplink measurement (on by default, ~20s at run start)")
    parser.add_argument("--baseline-timeout", type=float, default=180.0, metavar="S",
                        help="baseline deadline — all list counts run in parallel against it (default 180)")
    parser.add_argument("--list", action="store_true", help="print available operations and exit")
    args = parser.parse_args()

    if args.list:
        print("Available operations (pass to --ops):")
        for name in OPERATIONS:
            print(f"  {name}")
        return

    if args.make_junk:  # util mode — just build the payload and stop
        p = make_junk_file(args.make_junk)
        print(f"Wrote {args.make_junk} MB ({p.stat().st_size:,} bytes) → {p}")
        return

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log = setup_logging(run_id)
    envs = [e.strip() for e in args.env.split(",") if e.strip()]
    ops = _resolve_ops(args.ops)
    log.info(f"Perf run {run_id}  envs={envs}  ops={[o.name for o in ops]}  repeat={args.repeat}")

    # upload ops always send a sized junk file (default 10 MB; 0 = an empty file)
    upload_path = make_junk_file(args.upload_mb)
    log.info(f"Upload ops will send a {args.upload_mb} MB file: {upload_path}")

    # measure the uplink ONCE up front (before any uploads — never during, they'd contend);
    # on by default so every run is interpretable against the network, --no-network to skip
    net = {} if args.no_network else measure_network()
    if net:
        log.info(f"Network: up={net.get('ul_mbps')}Mbps down={net.get('dl_mbps')}Mbps rpm={net.get('rpm')}")

    all_samples = []
    for env in envs:
        try:
            ctx = build_context(env, args.no_cleanup, run_id, log)
        except (FileNotFoundError, ValueError) as exc:
            log.error(f"[{env}] {exc}; skipping env")
            continue
        ctx.shared["upload_path"] = upload_path
        ctx.shared["upload_mb"] = args.upload_mb  # stamped on each sample for size analysis
        if not args.no_baseline:
            store.write_baseline(run_id, take_baseline(ctx, args.baseline_timeout), extra=net)  # footprint + uplink, before
        # flush each sample as it lands (a hang/crash then loses only the in-flight call)
        samples = measure(ctx, ops, args.repeat,
                          call_timeout_s=args.call_timeout,
                          on_sample=lambda s: store.write_samples([s]))
        if not args.no_baseline:
            store.write_baseline(run_id, take_baseline(ctx, args.baseline_timeout))  # footprint after → growth signal
        ctx.cleanup()
        all_samples.extend(samples)

    summarize(all_samples, log)
    log.info(f"Results: {store.PERF_DIR}")


if __name__ == "__main__":
    main()
