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
# Suite registry — "module.function" in uat/v2.py and uat/v3.py; section order
# mirrors the docs pages (api_reference/client and v3/v3-client respectively)
# and still satisfies cross-suite dependencies.
# ---------------------------------------------------------------------------

SUITES: list[str] = [
    "v2.files",
    "v2.models",         # → stores "model"
    "v2.artifacts",      # needs "model"
    "v2.revisions",      # needs "model"
    "v2.jobs",
    "v2.systems",        # needs "model" → stores "system", "configuration"
    "v2.snapshots",      # needs "system", "configuration"
    "v2.access",         # needs "model"
    "v2.control_tags",   # needs "model"
    "v2.documents",      # needs "configuration"
    "v2.agents",
    "v2.tools",
    "v2.users",
    "v3.resources",      # → stores "v3_resource"
    "v3.revisions",      # needs "v3_resource"
    "v3.comments",       # needs "v3_resource"
    "v3.relationships",  # needs "v3_resource" (list xfail — CPD-598)
    "v3.remotes",
]

# CLI names: v2.files → v2_files (unchanged from the one-file-per-suite layout)
_SHORT_TO_FULL = {s.replace(".", "_"): s for s in SUITES}
_SHORT_TO_FULL.update({s: s for s in SUITES})  # also accept dotted names


def _resolve_suites(spec: str | None) -> list[str]:
    if not spec:
        return SUITES
    names = [s.strip() for s in spec.split(",") if s.strip()]
    resolved = []
    for name in names:
        if name not in _SHORT_TO_FULL:
            print(f"Unknown suite: {name!r}. Run --list to see available suites.", file=sys.stderr)
            sys.exit(1)
        full = _SHORT_TO_FULL[name]
        if full not in resolved:
            resolved.append(full)
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
    parser.add_argument("--baseline", action="store_true",
                        help="take an entity-count baseline before the run "
                             "(adds per-step platform_state; use on a dedicated perf env)")
    parser.add_argument("--list", action="store_true", help="print available suite names and exit")
    args = parser.parse_args()

    if args.list:
        print("Available suites (pass short name to --suite):")
        for s in SUITES:
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

    if args.baseline:
        take_baseline(ctx)

    suites = _resolve_suites(args.suite)
    log.info(f"Running {len(suites)} suite(s): {', '.join(s.replace('.', '_') for s in suites)}")

    for suite in suites:
        _run_suite(suite, ctx)

    ctx.cleanup()

    if args.baseline:  # re-measure footprint; records ctx.drift + logs footprint growth
        recheck_baseline(ctx)

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
