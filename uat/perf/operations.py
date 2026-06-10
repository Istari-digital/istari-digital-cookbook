"""Catalog of measurable endpoint operations — *what* the perf harness times.

An Operation is one named endpoint call. `setup` (optional) runs once before the
repeated calls to build a fixture (e.g. a model to GET) so the measured call
stays a single endpoint hit — that is what one latency sample should isolate.

Operations that create resources call ctx.track(...) so the shared cleanup
archives them afterwards. (Archive is a soft-delete, so the footprint still grows
— see uat/README.md; that is exactly what the baseline tracks alongside latency.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from uuid import uuid4

from istari_digital_client.v3.models.resource_type_dto import ResourceTypeDto

from uat.common import TestContext


@dataclass
class Operation:
    name: str
    call: Callable[[TestContext], Any]                   # one timed endpoint hit
    setup: Callable[[TestContext], None] | None = None   # once, before the repeats


# ── fixtures ─────────────────────────────────────────────────────────────────

def _ensure_model(ctx: TestContext) -> None:
    """Upload one model for read-style ops to target (once per env)."""
    if not ctx.shared.get("perf_model"):
        model = ctx.platform.upload_model(
            ctx.data("dummy.txt"),
            external_id=f"{ctx.run_id}-perf-fixture",
            display_name="perf fixture",
        )
        ctx.register("model", model, "perf_model")


# ── write operations (each creates + tracks a resource) ──────────────────────

def _upload_model(ctx: TestContext) -> Any:
    model = ctx.platform.upload_model(
        ctx.data("dummy.txt"),
        external_id=f"{ctx.run_id}-perf-{uuid4().hex[:8]}",
        display_name="perf upload",
    )
    ctx.track("model", model.id)
    return model


def _add_file(ctx: TestContext) -> Any:
    file = ctx.client.add_file(path=str(ctx.data("dummy.txt")), display_name="perf file")
    ctx.track("file", file.id)
    return file


def _create_resource(ctx: TestContext) -> Any:
    resource = ctx.v3.create_resource(
        path=ctx.data("dummy.txt"),
        resource_type=ResourceTypeDto.MODEL,
        display_name="perf v3 resource",
    )
    ctx.track("v3_resource", resource.resource_id)
    return resource


# ── registry ─────────────────────────────────────────────────────────────────

OPERATIONS: dict[str, Operation] = {
    op.name: op
    for op in [
        # writes
        Operation("upload_model", _upload_model),
        Operation("add_file", _add_file),
        Operation("create_resource", _create_resource),
        # reads
        Operation("get_model", lambda c: c.client.get_model(c.shared["perf_model"].id), setup=_ensure_model),
        Operation("list_models", lambda c: c.client.list_models(size=10)),
        Operation("list_files", lambda c: c.client.list_files(size=10)),
        Operation("list_systems", lambda c: c.client.list_systems(size=10)),
        Operation("list_resources", lambda c: c.v3.list_resources(size=10)),
    ]
}
