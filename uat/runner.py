"""
runner.py — UAT entry point.

    cd istari-labs-helpers
    uv run uat --list
    uv run uat --env demo
    uv run uat --env demo --suite v2_models,v2_jobs
    uv run uat --env stage --no-cleanup

Credentials are read from istari-labs-helpers/.env.{env}.
"""

from __future__ import annotations

import sys
from pathlib import Path

# When invoked as a script entry point the repo root isn't automatically on
# sys.path; add it so the `uat` package (which lives there) is importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import argparse
import importlib
import inspect
from datetime import datetime

from uat.common import (
    Status,
    TestContext,
    build_context,
    recheck_baseline,
    setup_logging,
    take_baseline,
    write_results,
)

# ---------------------------------------------------------------------------
# Suites are auto-discovered: every public `(ctx)` function in uat/v2.py and uat/v3.py
# is a suite, run in definition order (which mirrors the docs pages and satisfies the
# cross-suite deps — each suite also skip-guards its own inputs, so order isn't fragile).
# No registry to keep in sync: define a function, it runs.
# ---------------------------------------------------------------------------

def _discover(modname: str) -> list[str]:
    """'module.function' for each public function defined in uat.<modname>, in def order.
    (module __dict__ preserves definition order; imports/helpers/`_`-prefixed are skipped.)"""
    mod = importlib.import_module(f"uat.{modname}")
    return [f"{modname}.{name}" for name, obj in vars(mod).items()
            if inspect.isfunction(obj) and not name.startswith("_") and obj.__module__ == mod.__name__]


def _all_suites() -> list[str]:
    return _discover("v2") + _discover("v3")


def _resolve_suites(spec: str | None) -> list[str]:
    suites = _all_suites()
    if not spec:
        return suites
    by_name = {s.replace(".", "_"): s for s in suites}
    by_name.update({s: s for s in suites})  # also accept dotted names
    resolved: list[str] = []
    for name in (s.strip() for s in spec.split(",") if s.strip()):
        if name not in by_name:
            print(f"Unknown suite: {name!r}. Run --list to see available suites.", file=sys.stderr)
            sys.exit(1)
        if by_name[name] not in resolved:
            resolved.append(by_name[name])
    return resolved


def _run_suite(spec: str, ctx: TestContext) -> None:
    modname, fnname = spec.split(".")
    short = f"{modname}_{fnname}"
    ctx._current_suite = short
    ctx._log.info(f"\n{'─' * 60}")
    ctx._log.info(f"  Suite: {short}")
    ctx._log.info(f"{'─' * 60}")
    try:
        fn = getattr(importlib.import_module(f"uat.{modname}"), fnname)
        fn(ctx)
    except Exception as exc:
        ctx._log.error(f"Suite {short} crashed: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
    parser.add_argument("--list", action="store_true", help="print available suite names and exit")
    args = parser.parse_args()

    if args.list:
        print("Available suites (pass short name to --suite):")
        for s in _all_suites():
            print(f"  {s.replace('.', '_'):<28}  (uat/{s.split('.')[0]}.py)")
        return

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log = setup_logging(run_id)
    log.info(f"UAT run {run_id}  env={args.env}  no-cleanup={args.no_cleanup}")

    try:
        ctx = build_context(args.env, args.no_cleanup, run_id, log)
    except (FileNotFoundError, ValueError) as exc:
        log.error(str(exc))
        sys.exit(1)

    take_baseline(ctx)  # always — every run records the footprint (before + recheck after)

    suites = _resolve_suites(args.suite)
    log.info(f"Running {len(suites)} suite(s): {', '.join(s.replace('.', '_') for s in suites)}")

    for suite in suites:
        _run_suite(suite, ctx)

    ctx.cleanup()

    recheck_baseline(ctx)  # re-measure footprint; records ctx.drift + logs footprint growth

    results_path = write_results(ctx, [s.replace(".", "_") for s in suites])

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
