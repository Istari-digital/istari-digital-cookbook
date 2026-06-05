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
import sys
from datetime import datetime

from uat.common import Status, TestContext, build_context, setup_logging, write_results

# ---------------------------------------------------------------------------
# Suite registry — ordered by dependency
# ---------------------------------------------------------------------------

SUITES: list[str] = [
    # v2 — read-only / independent first
    "v2.v2_users",
    "v2.v2_files",
    "v2.v2_models",        # → stores "model"
    "v2.v2_artifacts",     # needs "model"
    "v2.v2_revisions",     # needs "model"
    "v2.v2_jobs",          # needs "model"
    "v2.v2_systems",       # needs "model" → stores "system", "configuration"
    "v2.v2_documents",     # needs "configuration"
    "v2.v2_snapshots",     # needs "system", "configuration"
    "v2.v2_access",        # needs "model"
    "v2.v2_control_tags",  # needs "model"
    "v2.v2_agents",
    "v2.v2_tools",
    # v3 — documented at docs.istaridigital.com/developers/SDK/v3/quick-start
    "v3.v3_resources",     # → stores "v3_resource"
    "v3.v3_revisions",     # needs "v3_resource"
    "v3.v3_relationships", # needs "v3_resource" (list xfail — CPD-598)
]

# Short names users can pass on CLI (strip the package prefix)
_SHORT_TO_FULL = {s.split(".")[-1]: s for s in SUITES}
_SHORT_TO_FULL.update({s: s for s in SUITES})  # also accept full names


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


def _run_suite(suite_module_path: str, ctx: TestContext) -> None:
    short = suite_module_path.split(".")[-1]
    ctx._current_suite = short
    ctx._log.info(f"\n{'─' * 60}")
    ctx._log.info(f"  Suite: {short}")
    ctx._log.info(f"{'─' * 60}")
    try:
        mod = importlib.import_module(f"uat.{suite_module_path}")
        mod.run(ctx)
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
    parser.add_argument(
        "--env",
        choices=["dev", "stage", "demo", "perf", "prod"],
        default="demo",
        help="Target environment (default: demo)",
    )
    parser.add_argument(
        "--suite",
        metavar="SUITE[,SUITE...]",
        help="Comma-separated suites to run; default runs all",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Skip archiving resources created during the run",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print available suite names and exit",
    )
    args = parser.parse_args()

    if args.list:
        print("Available suites (pass short name to --suite):")
        for s in SUITES:
            print(f"  {s.split('.')[-1]:<28}  ({s})")
        return

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log = setup_logging(run_id)
    log.info(f"UAT run {run_id}  env={args.env}  no-cleanup={args.no_cleanup}")

    try:
        ctx = build_context(args.env, args.no_cleanup, run_id, log)
    except (FileNotFoundError, ValueError) as exc:
        log.error(str(exc))
        sys.exit(1)

    suites = _resolve_suites(args.suite)
    log.info(f"Running {len(suites)} suite(s): {', '.join(s.split('.')[-1] for s in suites)}")

    for suite in suites:
        _run_suite(suite, ctx)

    ctx.cleanup()

    results_path = write_results(ctx, [s.split(".")[-1] for s in suites])

    summary = ctx.summary()
    log.info(f"\n{'═' * 60}")
    log.info(f"  PASS={summary[Status.PASS]}  FAIL={summary[Status.FAIL]}  SKIP={summary[Status.SKIP]}")
    log.info(f"  Results: {results_path}")
    log.info(f"{'═' * 60}")

    sys.exit(1 if ctx.has_failures() else 0)


if __name__ == "__main__":
    main()
