"""
common.py — shared UAT infrastructure.

Core pieces:
  TestContext   — holds clients, run state, resource tracking, and cleanup
  @ctx.step     — immediate-execution decorator; the decorated function body
                  is the example SDK call; result becomes the variable value
  setup_logging — terminal + rolling file handler (keeps last 10 run logs)
  write_results — JSON results file per run

Usage in a suite:

    def run(ctx: TestContext) -> None:

        @ctx.step("upload_model — create a model from a local file")
        def model():
            return ctx.platform.upload_model(
                ctx.data("dummy.txt"),
                external_id=f"{ctx.run_id}-v2-model",
                display_name="UAT v2 Model",
            )
        # 'model' is now ModelView | None (None if the step failed)

        if model:
            ctx.track("model", model.id)

        @ctx.step("get_model — fetch model by id")
        def fetched():
            assert model, "depends on upload"
            return ctx.platform.get_model(model.id)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

_UAT_ROOT = Path(__file__).parent
_RESULTS_DIR = _UAT_ROOT / "results"
_LOG_MAX_BYTES = 1 * 1024 * 1024   # 1 MB per file
_LOG_BACKUP_COUNT = 9               # uat.log + 9 backups = 10 total


# ---------------------------------------------------------------------------
# StepResult
# ---------------------------------------------------------------------------

class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass
class StepResult:
    suite: str
    description: str
    status: Status
    error: str = ""


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(run_id: str) -> logging.Logger:
    """Configure a logger that writes to terminal and a rotating log file.

    The file handler rotates at _LOG_MAX_BYTES and keeps _LOG_BACKUP_COUNT
    backups, giving ~10 MB of history total.
    """
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
        _RESULTS_DIR / "uat.log",
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


# ---------------------------------------------------------------------------
# TestContext
# ---------------------------------------------------------------------------

@dataclass
class TestContext:
    """Shared state for a full UAT run.

    Passed into every suite's run() function. Suites use ctx.step() to wrap
    each SDK call, ctx.track() to register resources for cleanup, and
    ctx.shared to pass objects between suites.
    """
    run_id: str
    env: str
    no_cleanup: bool
    _log: logging.Logger = field(repr=False)

    # populated by runner after env load
    platform: Any = field(default=None, repr=False)   # IstariPlatform
    client: Any = field(default=None, repr=False)      # raw v2 Client
    v3: Any = field(default=None, repr=False)          # V3Client

    # cross-suite resource sharing — suites read/write freely
    shared: dict[str, Any] = field(default_factory=dict, repr=False)

    # internal state
    _current_suite: str = field(default="", repr=False)
    _results: list[StepResult] = field(default_factory=list, repr=False)
    # tracked[entity_type] = [(id, extra)] — extra used for v3 comments (resource_id)
    _tracked: dict[str, list[tuple[str, Any]]] = field(default_factory=dict, repr=False)

    # ── resource tracking ────────────────────────────────────────────────

    def track(self, entity_type: str, resource_id: str, extra: Any = None) -> None:
        """Register a resource for cleanup. extra is used for v3 comment (resource_id)."""
        self._tracked.setdefault(entity_type, []).append((resource_id, extra))

    def register(self, entity_type: str, resource: Any, share_key: str | None = None) -> None:
        """Track a resource for cleanup and optionally share it across suites.

        Replaces the repeated pattern:
            if result:
                ctx.track("model", result.id)
                ctx.shared["model"] = result

        With:
            ctx.register("model", result)          # uses entity_type as share_key
            ctx.register("model", result, "v2_model")  # explicit share_key
        """
        if resource is None:
            return
        rid = getattr(resource, "resource_id", None) or getattr(resource, "id", None)
        self.track(entity_type, rid)
        self.shared[share_key or entity_type] = resource

    # ── @step decorator ──────────────────────────────────────────────────

    def step(self, description: str) -> Callable:
        """Immediate-execution decorator.

        The decorated function is called once immediately. Its return value
        becomes the value of the decorated name. Logs and records PASS/FAIL.

            @ctx.step("get_model — fetch by id")
            def model():
                return ctx.platform.get_model(some_id)
            # 'model' is now ModelView | None
        """
        def decorator(fn: Callable) -> Any:
            self._log.info(f"  [{self._current_suite}] {description}")
            try:
                result = fn()
                self._record(description, Status.PASS)
                self._log.debug(f"    PASS")
                return result
            except AssertionError as exc:
                msg = str(exc) or "assertion failed"
                self._record(description, Status.FAIL, msg)
                self._log.error(f"    FAIL  {msg}")
                return None
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                self._record(description, Status.FAIL, msg)
                self._log.error(f"    FAIL  {msg}")
                return None
        return decorator

    def skip(self, description: str, reason: str = "") -> None:
        """Record a step as skipped (dependency not met, feature unavailable, etc.)."""
        self._record(description, Status.SKIP, reason)
        self._log.warning(f"  [{self._current_suite}] SKIP  {description}" + (f" — {reason}" if reason else ""))

    # ── data path ────────────────────────────────────────────────────────

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
        seen = set(order)
        remaining = [k for k in self._tracked if k not in seen]
        for entity_type in order + remaining:
            for (rid, extra) in reversed(self._tracked.get(entity_type, [])):
                self._archive_one(entity_type, rid, extra)

    def _archive_one(self, entity_type: str, rid: str, extra: Any) -> None:
        try:
            c = self.client
            v3 = self.v3
            if entity_type == "model":
                c.archive_model(rid)
            elif entity_type == "file":
                c.archive_file(rid)
            elif entity_type == "artifact":
                c.archive_artifact(rid)
            elif entity_type == "job":
                c.archive_job(rid)
            elif entity_type == "system":
                c.archive_system(rid)
            elif entity_type == "document":
                c.archive_document(rid)
            elif entity_type == "configuration":
                c.archive_configuration(rid)
            elif entity_type == "v3_resource":
                v3.archive_resource(rid)
            elif entity_type == "v3_comment":
                # extra = parent resource_id
                v3.archive_comment(resource_id=extra, comment_id=rid)
            self._log.debug(f"  archived {entity_type} {rid}")
        except Exception as exc:
            self._log.warning(f"  cleanup failed for {entity_type} {rid}: {exc}")

    # ── results ──────────────────────────────────────────────────────────

    def _record(self, description: str, status: Status, error: str = "") -> None:
        self._results.append(StepResult(
            suite=self._current_suite,
            description=description,
            status=status,
            error=error,
        ))

    def summary(self) -> dict[Status, int]:
        counts: dict[Status, int] = {s: 0 for s in Status}
        for r in self._results:
            counts[r.status] += 1
        return counts

    def has_failures(self) -> bool:
        return any(r.status == Status.FAIL for r in self._results)


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
        "steps": [
            {
                "suite": r.suite,
                "description": r.description,
                "status": r.status,
                **({"error": r.error} if r.error else {}),
            }
            for r in ctx._results
        ],
        "summary": ctx.summary(),
        "cleanup_done": not ctx.no_cleanup,
        "tracked_resources": {
            k: [rid for rid, _ in v]
            for k, v in ctx._tracked.items()
        },
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

def _ensure_helpers_importable() -> None:
    """Add istari-labs-helpers to sys.path if not already importable.

    Allows the UAT runner to be invoked from the repo root without a separate
    venv activation step, as long as the istari-labs-helpers venv is being used.
    """
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
    config = Configuration(registry_url=url, registry_auth_token=token)
    v3 = V3Client(config)

    ctx = TestContext(
        run_id=run_id,
        env=env,
        no_cleanup=no_cleanup,
        _log=log,
        platform=platform,
        client=platform.client,
        v3=v3,
    )
    return ctx
