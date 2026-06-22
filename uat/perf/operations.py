"""Catalog of measurable endpoint operations — *what* the perf harness times.

An Operation is one named endpoint call timed `repeat` times. Three optional hooks:
  • setup       — runs once before the reps (untimed): build a read fixture, or
                  look up metadata the call needs. Never the thing being measured.
  • iterations  — derive the rep count from run state instead of `--repeat`
                  (used by the relationship chain, whose length = pool − 1).

Two upload styles, deliberately distinct:
  • v2 `add_model` / `add_file` — independent upload testers; each timed call
    creates one resource and nothing consumes it.
  • v3 `create_resource` — also an independent upload measurement, but each call
    additionally pools its revision id so `create_revision_relationship` can chain
    the pool afterwards (parent→child→…). That keeps every relationship sample a
    single timed endpoint hit, with the inputs produced by the (already-timed)
    upload phase rather than an untimed prep step.

Created resources call ctx.track(...) so shared cleanup archives them. (Archive is
a soft-delete, so the footprint still grows — see uat/README.md; that growth is what
the baseline tracks alongside latency.)
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from istari_digital_client.v3.models.new_revision_relationship_dto import NewRevisionRelationshipDto
from istari_digital_client.v3.models.resource_type_dto import ResourceTypeDto

from uat.common import TestContext

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# shared-state keys this module owns
_REV_POOL = "perf_v3_revs"      # revision ids left behind by create_resource
_REL_TYPE = "perf_rel_type"     # the `produces` relationship type id (looked up once)
_REL_CURSOR = "perf_rel_cursor"  # next pool index the relationship chain will link
_UPLOAD_PATH = "upload_path"    # file the upload ops send (set by --upload-mb)


# ── junk payload (for heavy repeated uploads) ─────────────────────────────────

def junk_path(size_mb: int) -> Path:
    return _DATA_DIR / f"junk_{size_mb}mb.bin"


def make_junk_file(size_mb: int, path: Path | None = None) -> Path:
    """Write `size_mb` MB of random bytes (idempotent if already that exact size).

    Random (not zeros) so it isn't trivially compressible. NB: uploading the *same*
    file repeatedly may hit the platform's content dedup (we've seen token-SHA
    conflicts), so repeated-identical uploads can measure dedup rather than full
    transfer — salt per upload if you need true transfer numbers.
    """
    path = path or junk_path(size_mb)
    target = size_mb * 1024 * 1024
    if path.exists() and path.stat().st_size == target:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    written, chunk = 0, 1024 * 1024
    with path.open("wb") as f:
        while written < target:
            n = min(chunk, target - written)
            f.write(os.urandom(n))
            written += n
    return path


def _upload_src(ctx: TestContext) -> Path:
    """What the upload ops send: the --upload-mb junk file if set, else dummy.txt."""
    return ctx.shared.get(_UPLOAD_PATH) or ctx.data("dummy.txt")


def measure_network() -> dict:
    """macOS `networkQuality` → {ul_mbps, dl_mbps, rpm}; {} if unavailable.

    Saturates the link for ~20s, so call it BEFORE an upload run (never during) — it
    would otherwise contend with the uploads and corrupt both readings.
    """
    try:
        out = subprocess.run(["networkQuality", "-c"], capture_output=True, text=True, timeout=120)
        d = json.loads(out.stdout)
    except Exception:  # noqa: BLE001 — tool missing / non-macOS / timeout → no data
        return {}
    res: dict = {}
    if d.get("ul_throughput"):
        res["ul_mbps"] = round(d["ul_throughput"] / 1e6, 1)
    if d.get("dl_throughput"):
        res["dl_mbps"] = round(d["dl_throughput"] / 1e6, 1)
    if d.get("responsiveness"):
        res["rpm"] = round(d["responsiveness"])
    return res


@dataclass
class Operation:
    name: str
    call: Callable[[TestContext], Any]                       # one timed endpoint hit
    setup: Callable[[TestContext], None] | None = None       # once, before the reps (untimed)
    iterations: Callable[[TestContext, int], int] | None = None  # rep count from state; default --repeat


# ── read fixture ─────────────────────────────────────────────────────────────

def _ensure_model(ctx: TestContext) -> None:
    """Upload one model for read-style ops to target (once per env)."""
    if not ctx.shared.get("perf_model"):
        model = ctx.platform.upload_model(
            ctx.data("dummy.txt"),
            external_id=f"{ctx.run_id}-perf-fixture",
            display_name="perf fixture",
        )
        ctx.register("model", model, "perf_model")


# ── independent upload operations (each creates + tracks one resource) ────────

def _add_model(ctx: TestContext) -> Any:
    """Raw v2 model upload endpoint — independent, nothing consumes the result."""
    model = ctx.client.add_model(
        path=_upload_src(ctx),
        external_identifier=f"{ctx.run_id}-perf-{uuid4().hex[:8]}",
        display_name="perf upload",
    )
    ctx.track("model", model.id)
    return model


def _add_file(ctx: TestContext) -> Any:
    """Raw v2 file upload endpoint — independent."""
    file = ctx.client.add_file(path=str(_upload_src(ctx)), display_name="perf file")
    ctx.track("file", file.id)
    return file


def _create_resource(ctx: TestContext) -> Any:
    """v3 resource upload — independent measurement that also pools its revision id
    so a later create_revision_relationship chain can link the pool."""
    resource = ctx.v3.create_resource(
        path=_upload_src(ctx),
        resource_type=ResourceTypeDto.MODEL,
        display_name="perf v3 resource",
    )
    ctx.track("v3_resource", resource.resource_id)
    ctx.shared.setdefault(_REV_POOL, []).append(resource.file_revision_id)
    return resource


# ── relationship chain (consumes the create_resource pool) ────────────────────

def _ensure_produces_type(ctx: TestContext) -> None:
    """Look up the `produces` relationship type id once (untimed metadata read)."""
    types = ctx.v3.list_revision_relationship_types().items or []
    rel_type = next((t for t in types if t.name.lower() == "produces"), types[0] if types else None)
    if not rel_type:
        raise RuntimeError("no revision relationship types available")
    ctx.shared[_REL_TYPE] = rel_type.id


def _chain_links(ctx: TestContext, repeat: int) -> int:
    """A chain over N pooled revisions has N−1 links (so N−1 timed creates)."""
    return max(0, len(ctx.shared.get(_REV_POOL, [])) - 1)


def _create_relationship(ctx: TestContext) -> Any:
    """Link the next consecutive pair from the pool: revs[i] `produces` revs[i+1].
    Consecutive distinct revisions → never self-referential, never a duplicate edge."""
    revs = ctx.shared[_REV_POOL]
    i = ctx.shared.get(_REL_CURSOR, 0)
    ctx.shared[_REL_CURSOR] = i + 1
    return ctx.v3.create_revision_relationship(
        new_revision_relationship_dto=NewRevisionRelationshipDto(
            left_revision_id=revs[i],
            right_revision_id=revs[i + 1],
            relationship_type_id=ctx.shared[_REL_TYPE],
        )
    )


# ── registry (create_resource precedes the chain so the pool exists) ──────────

OPERATIONS: dict[str, Operation] = {
    op.name: op
    for op in [
        # uploads
        Operation("add_model", _add_model),
        Operation("add_file", _add_file),
        Operation("create_resource", _create_resource),
        # relationship chain over the create_resource pool (N revisions → N−1 links)
        Operation("create_revision_relationship", _create_relationship,
                  setup=_ensure_produces_type, iterations=_chain_links),
        # reads
        Operation("get_model", lambda c: c.client.get_model(c.shared["perf_model"].id), setup=_ensure_model),
        Operation("list_models", lambda c: c.client.list_models(size=10)),
        Operation("list_files", lambda c: c.client.list_files(size=10)),
        Operation("list_systems", lambda c: c.client.list_systems(size=10)),
        Operation("list_resources", lambda c: c.v3.list_resources(size=10)),
    ]
}
