"""UAT entry point.

    uv run uat --list
    uv run uat --env demo
    uv run uat --env demo --suite v2_models,v2_jobs
    uv run uat --env stage --no-cleanup

Credentials are read from istari-labs-helpers/.env.{env}.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Put the repo root on sys.path so `uat` imports when run as a console script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import argparse
from datetime import datetime

from uat import v2, v3
from uat.common import (
    Status,
    TestContext,
    build_context,
    recheck_baseline,
    setup_logging,
    take_baseline,
    write_results,
)

# The suites to run, in order. The order satisfies cross-suite deps (e.g. models before
# artifacts); each suite also skip-guards its own inputs, so it's forgiving. Add a function
# to v2.py / v3.py and list it here to run it.
SUITES = (
    v2.files, v2.models, v2.artifacts, v2.revisions, v2.jobs, v2.systems, v2.snapshots,
    v2.access, v2.control_tags, v2.documents, v2.agents, v2.tools, v2.users,
    v3.resources, v3.revisions, v3.comments, v3.relationships, v3.remotes,
)


def _name(fn) -> str:
    """v2.files → 'v2_files'."""
    return f"{fn.__module__.rsplit('.', 1)[-1]}_{fn.__name__}"


def _select(spec: str | None) -> tuple:
    if not spec:
        return SUITES
    wanted = {s.strip() for s in spec.split(",") if s.strip()}
    unknown = wanted - {_name(fn) for fn in SUITES}
    if unknown:
        print(f"Unknown suite(s): {', '.join(sorted(unknown))}. Run --list.", file=sys.stderr)
        sys.exit(1)
    return tuple(fn for fn in SUITES if _name(fn) in wanted)


def _run(fn, ctx: TestContext) -> None:
    ctx._current_suite = _name(fn)
    ctx.log.info(f"\n{'─' * 60}\n  Suite: {_name(fn)}\n{'─' * 60}")
    try:
        fn(ctx)
    except Exception as exc:
        ctx.log.error(f"Suite {_name(fn)} crashed: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Istari Digital SDK UAT runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--env", choices=["dev", "stage", "demo", "perf", "prod"], default="demo",
                        help="target environment (default: demo)")
    parser.add_argument("--suite", metavar="SUITE[,SUITE...]",
                        help="comma-separated suites to run; default all")
    parser.add_argument("--no-cleanup", action="store_true",
                        help="skip archiving resources created during the run")
    parser.add_argument("--list", action="store_true", help="print suite names and exit")
    args = parser.parse_args()

    if args.list:
        for fn in SUITES:
            print(_name(fn))
        return

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log = setup_logging(run_id)
    log.info(f"UAT run {run_id}  env={args.env}  no-cleanup={args.no_cleanup}")

    try:
        ctx = build_context(args.env, args.no_cleanup, run_id, log)
    except (FileNotFoundError, ValueError) as exc:
        log.error(str(exc))
        sys.exit(1)

    take_baseline(ctx)  # footprint before the run

    suites = _select(args.suite)
    log.info(f"Running {len(suites)} suite(s): {', '.join(_name(fn) for fn in suites)}")
    for fn in suites:
        _run(fn, ctx)

    ctx.cleanup()
    recheck_baseline(ctx)  # footprint after → ctx.drift + footprint-growth warning

    results_path = write_results(ctx, [_name(fn) for fn in suites])

    summary = ctx.summary()
    log.info(f"\n{'═' * 60}")
    log.info(f"  PASS={summary[Status.PASS]}  FAIL={summary[Status.FAIL]}  SKIP={summary[Status.SKIP]}")
    failures = [r for r in ctx._results if r.status == Status.FAIL]
    if failures:
        log.info(f"  Failures ({len(failures)}):")
        for r in failures:
            log.info(f"    [{r.suite}] {r.description} — {r.error.splitlines()[0] if r.error else ''}")
    if ctx.drift:
        log.info(f"  Baseline drift ({len(ctx.drift)}):")
        for fieldname, expected, actual in ctx.drift:
            log.info(f"    {fieldname}: expected {expected}, found {actual} (Δ{actual - expected:+d})")
    log.info(f"  Results: {results_path}")
    log.info(f"{'═' * 60}")

    sys.exit(1 if ctx.has_failures() else 0)


if __name__ == "__main__":
    main()
