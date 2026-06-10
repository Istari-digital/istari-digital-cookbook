"""Shared UAT infrastructure: TestContext, @ctx.step, baseline, cleanup, results.

Suite usage:

    def run(ctx: TestContext) -> None:

        @ctx.step("upload_model — create a model from a local file")
        def model():
            return ctx.platform.upload_model(ctx.data("dummy.txt"), ...)
        # 'model' is now the return value, or None if the step failed

        if model:
            ctx.register("model", model)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

_UAT_ROOT = Path(__file__).parent
_RESULTS_DIR = _UAT_ROOT / "results"
_LOG_MAX_BYTES = 1 * 1024 * 1024
_LOG_BACKUP_COUNT = 9  # uat.log + 9 backups = 10 total


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass
class PlatformCounts:
    """Platform-wide entity counts at a point in time.

    Used as ctx.baseline (measured) and StepResult.platform_state (derived as
    baseline + tracked delta). -1 means that type could not be counted; it
    stays -1 in derived snapshots.
    """
    taken_at: str
    env: str
    files: int = 0
    models: int = 0
    artifacts: int = 0
    systems: int = 0
    documents: int = 0
    jobs: int = 0
    v3_resources: int = 0


# PlatformCounts field -> the ctx._tracked key (singular) for that entity type.
_COUNT_FIELD_TO_TRACKED = {
    "files": "file", "models": "model", "artifacts": "artifact", "systems": "system",
    "documents": "document", "jobs": "job", "v3_resources": "v3_resource",
}


@dataclass
class StepResult:
    suite: str
    description: str
    status: Status
    error: str = ""
    duration_s: float = 0.0
    # counts when the step *began* (latency is a function of pre-step footprint);
    # None when no baseline was taken.
    platform_state: PlatformCounts | None = None


def setup_logging(run_id: str) -> logging.Logger:
    """Terminal (INFO) + rotating file (DEBUG, ~10 MB history) logger."""
    _RESULTS_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
    logger = logging.getLogger(f"uat.{run_id}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    stream = logging.StreamHandler()
    stream.setLevel(logging.INFO)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    fh = RotatingFileHandler(
        _RESULTS_DIR / "uat.log", maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT, encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


@dataclass
class TestContext:
    """Shared state for a full UAT run, passed into every suite's run()."""
    run_id: str
    env: str
    no_cleanup: bool
    _log: logging.Logger = field(repr=False)

    platform: Any = field(default=None, repr=False)   # IstariPlatform
    client: Any = field(default=None, repr=False)     # raw v2 Client
    v3: Any = field(default=None, repr=False)         # V3Client

    # cross-suite resource sharing — suites read/write freely
    shared: dict[str, Any] = field(default_factory=dict, repr=False)

    baseline: PlatformCounts | None = field(default=None, repr=False)
    final_counts: PlatformCounts | None = field(default=None, repr=False)
    drift: list[tuple[str, int, int]] = field(default_factory=list, repr=False)

    _current_suite: str = field(default="", repr=False)
    _results: list[StepResult] = field(default_factory=list, repr=False)
    # tracked[entity_type] = [(id, extra)] — extra used for v3 comments (resource_id)
    _tracked: dict[str, list[tuple[str, Any]]] = field(default_factory=dict, repr=False)

    # ── resource tracking ────────────────────────────────────────────────

    def track(self, entity_type: str, resource_id: str, extra: Any = None) -> None:
        """Register a resource for cleanup."""
        self._tracked.setdefault(entity_type, []).append((resource_id, extra))

    def register(self, entity_type: str, resource: Any, share_key: str | None = None) -> None:
        """Track a resource for cleanup and share it (under share_key or entity_type)."""
        if resource is None:
            return
        rid = getattr(resource, "resource_id", None) or getattr(resource, "id", None)
        self.track(entity_type, rid)
        self.shared[share_key or entity_type] = resource

    def _snapshot_state(self) -> PlatformCounts | None:
        """Derived counts now: baseline + tracked-so-far per type (-1 stays -1)."""
        base = self.baseline
        if base is None:
            return None

        def derived(field: str, tracked_key: str) -> int:
            count = getattr(base, field)
            return count if count < 0 else count + len(self._tracked.get(tracked_key, []))

        return PlatformCounts(
            taken_at=datetime.now(timezone.utc).isoformat(),
            env=base.env,
            **{field: derived(field, key) for field, key in _COUNT_FIELD_TO_TRACKED.items()},
        )

    # ── steps ────────────────────────────────────────────────────────────

    def step(self, description: str) -> Callable:
        """Immediate-execution decorator: calls the function once, records
        PASS/FAIL, and binds the decorated name to the return value (None on
        failure). See module docstring."""
        def decorator(fn: Callable) -> Any:
            self._log.info(f"  [{self._current_suite}] {description}")
            pre_state = self._snapshot_state()
            t0 = time.perf_counter()
            try:
                result = fn()
                status, msg = Status.PASS, ""
            except Exception as exc:
                result = None
                status = Status.FAIL
                msg = (str(exc) or "assertion failed") if isinstance(exc, AssertionError) \
                    else f"{type(exc).__name__}: {exc}"
            duration = time.perf_counter() - t0
            self._record(description, status, msg, duration_s=duration, platform_state=pre_state)
            if status is Status.PASS:
                self._log.debug(f"    PASS  ({duration:.3f}s)")
            else:
                self._log.error(f"    FAIL  {msg}")
            return result
        return decorator

    def skip(self, description: str, reason: str = "") -> None:
        self._record(description, Status.SKIP, reason)
        self._log.warning(f"  [{self._current_suite}] SKIP  {description}" + (f" — {reason}" if reason else ""))

    def data(self, filename: str) -> Path:
        return _UAT_ROOT / "data" / filename

    # ── cleanup ──────────────────────────────────────────────────────────

    def cleanup(self) -> None:
        if self.no_cleanup:
            self._log.info("--no-cleanup: skipping resource teardown")
            return
        self._log.info("Cleaning up tracked resources …")
        order = [
            "job", "v3_comment", "artifact", "model", "file",
            "document", "configuration", "system", "v3_resource",
        ]
        remaining = [k for k in self._tracked if k not in set(order)]
        for entity_type in order + remaining:
            for (rid, extra) in reversed(self._tracked.get(entity_type, [])):
                self._archive_one(entity_type, rid, extra)

    def _archive_one(self, entity_type: str, rid: str, extra: Any) -> None:
        if entity_type == "v3_comment":
            archive = lambda: self.v3.archive_comment(resource_id=extra, comment_id=rid)
        elif entity_type == "v3_resource":
            archive = lambda: self.v3.archive_resource(rid)
        else:
            method = getattr(self.client, f"archive_{entity_type}", None)
            if method is None:
                self._log.warning(f"  no archiver for tracked type {entity_type!r} ({rid}) — leaked")
                return
            archive = lambda: method(rid)
        try:
            archive()
            self._log.debug(f"  archived {entity_type} {rid}")
        except Exception as exc:
            self._log.warning(f"  cleanup failed for {entity_type} {rid}: {exc}")

    # ── results ──────────────────────────────────────────────────────────

    def _record(self, description: str, status: Status, error: str = "",
                duration_s: float = 0.0, platform_state: PlatformCounts | None = None) -> None:
        self._results.append(StepResult(
            suite=self._current_suite, description=description, status=status,
            error=error, duration_s=duration_s, platform_state=platform_state,
        ))

    def summary(self) -> dict[Status, int]:
        counts = {s: 0 for s in Status}
        for r in self._results:
            counts[r.status] += 1
        return counts

    def has_failures(self) -> bool:
        return any(r.status == Status.FAIL for r in self._results)


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def _measure_counts(ctx: TestContext, per_call_timeout_s: float) -> PlatformCounts:
    """Total entity footprint: one size=1 list call per type, read page.total.

    Counts include archived resources (archive_status="all"): archive is a
    soft-delete and archived rows still drive the SpiceDB permission scan on
    every list call (CPD-598/601), so they are what determines latency — see
    uat/README.md. Each count runs in a daemon thread bounded by
    per_call_timeout_s; a slow or failing one is recorded as -1.
    """
    from istari_digital_client.v2.models.archive_status import ArchiveStatus as V2ArchiveStatus
    from istari_digital_client.v3.models.archive_status import ArchiveStatus as V3ArchiveStatus

    def _count(list_fn: Callable, **kwargs: Any) -> int:
        # Outcome lands in `box`; all logging happens on this thread after the
        # join, so an abandoned thread that finishes late never logs.
        box: dict[str, Any] = {"value": None, "exc": None}

        def _work() -> None:
            try:
                total = getattr(list_fn(size=1, **kwargs), "total", None)
                box["value"] = -1 if total is None else total
            except Exception as exc:
                box["exc"] = exc

        t = threading.Thread(target=_work, daemon=True)
        t.start()
        t.join(per_call_timeout_s)
        if t.is_alive():
            ctx._log.warning(f"baseline: {list_fn.__name__} exceeded {per_call_timeout_s}s; recording -1")
            return -1
        if box["exc"] is not None:
            ctx._log.warning(f"baseline: could not count via {list_fn.__name__}: {box['exc']}")
            return -1
        return box["value"]

    v2_all, v3_all = V2ArchiveStatus.ALL, V3ArchiveStatus.ALL
    return PlatformCounts(
        taken_at=datetime.now(timezone.utc).isoformat(),
        env=ctx.env,
        files=_count(ctx.client.list_files, archive_status=v2_all),
        models=_count(ctx.client.list_models, archive_status=v2_all),
        artifacts=_count(ctx.client.list_artifacts, archive_status=v2_all),
        systems=_count(ctx.client.list_systems, archive_status=v2_all),
        documents=_count(ctx.client.list_documents, archive_status=v2_all),
        jobs=_count(ctx.client.list_jobs, archive_status=v2_all),
        v3_resources=_count(ctx.v3.list_resources, archive_status=v3_all) if ctx.v3 else -1,
    )


def take_baseline(ctx: TestContext, per_call_timeout_s: float = 15.0) -> PlatformCounts:
    """Measure counts before the run onto ctx.baseline (see _measure_counts).

    WARNING: counts drift if others are active on the env; use a dedicated perf env.
    """
    ctx.baseline = _measure_counts(ctx, per_call_timeout_s)
    counts = " ".join(f"{k}={v}" for k, v in asdict(ctx.baseline).items() if k not in ("taken_at", "env"))
    ctx._log.info(f"Baseline: {counts}")
    return ctx.baseline


def recheck_baseline(ctx: TestContext, per_call_timeout_s: float = 15.0) -> list[tuple[str, int, int]]:
    """Re-measure after the run; record drift onto ctx.drift / ctx.final_counts.

    Archive is a soft-delete, so cleanup never shrinks the footprint:
    expected = baseline + resources created this run. Drift means something
    unaccounted — concurrent activity, an untracked create, or a hard delete.
    Types that are -1 on either side are skipped. No-op without a baseline.
    """
    if ctx.baseline is None:
        return []

    final = ctx.final_counts = _measure_counts(ctx, per_call_timeout_s)
    drift: list[tuple[str, int, int]] = []
    for fieldname, tracked_key in _COUNT_FIELD_TO_TRACKED.items():
        base, actual = getattr(ctx.baseline, fieldname), getattr(final, fieldname)
        expected = base + len(ctx._tracked.get(tracked_key, []))
        if base >= 0 and actual >= 0 and actual != expected:
            drift.append((fieldname, expected, actual))
    ctx.drift = drift

    added = sum(len(v) for v in ctx._tracked.values())
    if added:
        ctx._log.warning(
            f"This run added {added} resource(s) to the {ctx.env} footprint; archive is a soft-delete "
            "so they persist and keep costing on every scan — repeated runs degrade the env."
        )
    return drift


# ---------------------------------------------------------------------------
# Results writer
# ---------------------------------------------------------------------------

def write_results(ctx: TestContext, suites_run: list[str]) -> Path:
    _RESULTS_DIR.mkdir(exist_ok=True)
    out_path = _RESULTS_DIR / f"run_{ctx.run_id}.json"
    payload = {
        "run_id": ctx.run_id,
        "env": ctx.env,
        "suites_run": suites_run,
        **({"baseline": asdict(ctx.baseline)} if ctx.baseline else {}),
        **({"final_counts": asdict(ctx.final_counts)} if ctx.final_counts else {}),
        **({"drift": [
            {"entity": f, "expected": e, "actual": a} for f, e, a in ctx.drift
        ]} if ctx.drift else {}),
        "steps": [
            {
                "suite": r.suite,
                "description": r.description,
                "status": r.status,
                "duration_s": round(r.duration_s, 4),
                **({"error": r.error} if r.error else {}),
                **({"platform_state": asdict(r.platform_state)} if r.platform_state else {}),
            }
            for r in ctx._results
        ],
        "summary": ctx.summary(),
        "cleanup_done": not ctx.no_cleanup,
        "tracked_resources": {k: [rid for rid, _ in v] for k, v in ctx._tracked.items()},
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

def _ensure_helpers_importable() -> None:
    """Add istari-labs-helpers to sys.path so the runner works from the repo root."""
    import sys
    try:
        import istari_labs_helpers  # noqa: F401
        return
    except ImportError:
        pass
    helpers_dir = _UAT_ROOT.parent / "istari-labs-helpers"
    if helpers_dir.exists() and str(helpers_dir) not in sys.path:
        sys.path.insert(0, str(helpers_dir))


def build_context(env: str, no_cleanup: bool, run_id: str, log: logging.Logger) -> TestContext:
    """Load .env.{env}, create platform + v3 client, return populated TestContext."""
    _ensure_helpers_importable()

    from istari_digital_client.configuration import Configuration
    from istari_digital_client.v3_client import V3Client
    from istari_labs_helpers import IstariPlatform

    env_file = _UAT_ROOT.parent / "istari-labs-helpers" / f".env.{env}"
    if not env_file.exists():
        raise FileNotFoundError(
            f"{env_file} not found. Create it with ISTARI_REGISTRY_URL and "
            "ISTARI_PERSONAL_ACCESS_TOKEN."
        )

    load_dotenv(env_file, override=True)
    url = os.getenv("ISTARI_REGISTRY_URL", "")
    token = os.getenv("ISTARI_PERSONAL_ACCESS_TOKEN", "")
    if not url or not token or token.startswith("your_"):
        raise ValueError(f"Credentials not set in {env_file.name}")

    platform = IstariPlatform.from_env(str(env_file))
    v3 = V3Client(Configuration(registry_url=url, registry_auth_token=token))
    return TestContext(
        run_id=run_id, env=env, no_cleanup=no_cleanup, _log=log,
        platform=platform, client=platform.client, v3=v3,
    )
