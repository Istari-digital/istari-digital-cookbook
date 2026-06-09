"""
Istari Part Search Agent  —  Pydantic v2 edition
=================================================
Functionally identical to istari_part_search_agent.py but built entirely on
**Pydantic v2** + the **Anthropic SDK** directly, without pydantic-ai.

Key architectural differences from v1:
  - No pydantic-ai dependency. LLM calls use anthropic.messages.parse() with
    Pydantic v2 output_format, giving the same schema enforcement without the
    pydantic-ai Agent wrapper.
  - IstariCapability is a proper Pydantic v2 BaseModel with PrivateAttr SDK
    clients, model_post_init initialisation, and field validators — making
    auth configuration first-class and validatable rather than a plain class.
  - Single-class capability pattern: all Istari operations (fetch, upload,
    link, thread) live on IstariCapability and are callable directly.

Pipeline stages (same as v1):
  1. FETCH    — Download requirements.json via IstariCapability.
  2. CLASSIFY — Claude classifies requirements into typed categories.
  3. TRANSFORM — Claude maps each typed requirement to a PartSearchSpec.
  4. UPLOAD   — IstariCapability uploads outputs and links the digital thread.

Credentials (flags or environment variables):
  --anthropic-key   / ANTHROPIC_API_KEY
  --istari-url      / ISTARI_REGISTRY_URL
  --istari-token    / ISTARI_REGISTRY_AUTH_TOKEN

Usage:
  python istari_part_search_agent_v2.py --requirements-id <UUID>
  python istari_part_search_agent_v2.py --requirements-id <UUID> --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import warnings
from pathlib import Path
from typing import Literal

import anthropic
from istari_digital_client import Client, Configuration, V3Client
from istari_digital_client.v3.models.new_revision_relationship_dto import (
    NewRevisionRelationshipDto,
)
from istari_digital_client.v3.models.resource_type_dto import ResourceTypeDto
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

HERE = Path(__file__).parent


# ════════════════════════════════════════════════════════════════════════════
# Pydantic v2 data models
# ════════════════════════════════════════════════════════════════════════════

class RawRequirement(BaseModel):
    """One requirement as exported from Cameo/SysML."""
    model_config = ConfigDict(populate_by_name=True)

    id:     str
    name:   str = ""
    text:   str = ""
    req_id: str = ""

    @model_validator(mode="before")
    @classmethod
    def _extract_from_cameo(cls, data: dict) -> dict:
        """Pull text and req_id from nested Cameo tags if not at top level."""
        tags = data.get("tags", {})
        if not data.get("text"):
            data["text"] = tags.get("Text", "")
        if not data.get("req_id"):
            data["req_id"] = tags.get("Id", "")
        return data

    def is_actionable(self) -> bool:
        return bool(self.text and self.text.strip())


# ── Extract agent output ──────────────────────────────────────────────────────

class HardwareReq(BaseModel):
    id:       str
    text:     str
    category: Literal["structural", "mechanical", "electrical", "thermal", "fastener", "other"]


class MaterialReq(BaseModel):
    id:          str
    text:        str
    spec_number: str | None = None


class PerformanceReq(BaseModel):
    id:    str
    text:  str
    value: float | None = None
    unit:  str | None = None


class ExtractedRequirements(BaseModel):
    hardware:    list[HardwareReq]
    material:    list[MaterialReq]
    performance: list[PerformanceReq]
    skipped_ids: list[str]


# ── Transform agent output (part-search-spec.schema.json) ─────────────────────

OutputGroup = Literal["basic", "specs", "offers", "datasheet", "lifecycle", "cad", "compliance"]


class SearchSpec(BaseModel):
    category_id:      str = Field(description="Octopart/Nexar numeric category ID — required")
    q:                str | None = None
    category:         str | None = None
    distributors:     list[str] | None = Field(default=None, min_length=1)
    limit:            int | None = Field(default=None, ge=1, le=100)
    start:            int | None = Field(default=None, ge=0)
    manufacturers:    list[str] | None = Field(default=None, min_length=1)
    in_stock:         bool | None = None
    has_datasheet:    bool | None = None
    lifecycle_status: list[Literal["Active","NRND","Obsolete","Discontinued","Unknown"]] | None = Field(default=None, min_length=1)
    sort:             str | None = None
    sort_direction:   Literal["asc", "desc"] | None = None

    @field_validator("category_id")
    @classmethod
    def _category_id_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("category_id must not be empty")
        return v


class OutputSpec(BaseModel):
    groups: list[OutputGroup] | None = None


class PartSearchSpec(BaseModel):
    search:     SearchSpec
    parameters: dict[str, str] = Field(min_length=1)
    output:     OutputSpec | None = None

    @field_validator("parameters")
    @classmethod
    def _all_values_are_strings(cls, v: dict) -> dict:
        for key, val in v.items():
            if not isinstance(val, str):
                raise ValueError(f"parameters['{key}'] must be a string, got {type(val).__name__}")
        return v


class PartSearchResult(BaseModel):
    source_req_id: str
    priority:      Literal["critical", "high", "medium", "low"] = "medium"
    part_search:   PartSearchSpec


# ════════════════════════════════════════════════════════════════════════════
# IstariCapability  —  Pydantic v2 model encapsulating auth + SDK
# ════════════════════════════════════════════════════════════════════════════

class IstariCapability(BaseModel):
    """
    Pydantic v2 model that encapsulates Istari authentication and all
    SDK operations. Using BaseModel makes the auth config validatable,
    serialisable, and injectable as a typed dependency.

    SDK clients are stored as PrivateAttr and initialised in
    model_post_init so they are available immediately after construction
    without being exposed as public fields.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    registry_url: str = Field(description="Istari registry base URL")
    pat:          str = Field(description="Personal Access Token", repr=False)

    # Private SDK clients — not part of the public model schema
    _v3: V3Client = PrivateAttr()
    _v2: Client   = PrivateAttr()

    @field_validator("registry_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("pat")
    @classmethod
    def _pat_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Istari PAT must not be empty")
        return v

    def model_post_init(self, __context: object) -> None:
        """Initialise SDK clients after Pydantic validation."""
        config = Configuration(registry_url=self.registry_url, registry_auth_token=self.pat)
        self._v3 = V3Client(config)
        self._v2 = Client(config)

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def fetch_requirements(self, resource_id: str) -> list[RawRequirement]:
        """Download a requirements.json resource and parse it."""
        resource = self._v3.get_resource(resource_id)
        raw = json.loads(self._v3.get_content(resource))
        if not isinstance(raw, list):
            raw = [raw]
        return [RawRequirement.model_validate(r) for r in raw if isinstance(r, dict)]

    def get_revision_id(self, resource_id: str) -> str:
        return self._v3.get_resource(resource_id).file_revision_id

    # ── Upload ────────────────────────────────────────────────────────────────

    def upload_model(self, path: Path, display_name: str, description: str):
        """Upload a file as a MODEL resource (visible in Istari UI)."""
        return self._v3.create_resource(
            path=path,
            resource_type=ResourceTypeDto.MODEL,
            display_name=display_name,
            description=description,
            version_name="v1.0",
            external_identifier=f"part-search-agent/{display_name}",
        )

    # ── Relationships ─────────────────────────────────────────────────────────

    def link_resources(self, from_revision_id: str, to_revision_id: str) -> None:
        """Create a produces → relationship between two revisions."""
        PRODUCES_TYPE_ID = "fea9bd01-81bc-4db4-9aff-289bdd9745c4"
        self._v3.create_revision_relationship(
            NewRevisionRelationshipDto(
                relationship_type_id=PRODUCES_TYPE_ID,
                left_revision_id=from_revision_id,
                right_revision_id=to_revision_id,
            )
        )

    def get_digital_thread(self, resource_id: str, depth: int = 3) -> dict:
        """Return a nested dict representing the digital thread for a resource."""
        def _rev_label(rev) -> str:
            return rev.display_name or rev.name or rev.file_revision_id[:8]

        def _walk(revision_id: str, visited: set, d: int) -> dict:
            if d == 0 or revision_id in visited:
                return {}
            visited.add(revision_id)
            try:
                items = self._v3.list_revision_relationships(revision_id=revision_id).items or []
            except Exception:
                return {}

            node: dict = {"produces": [], "derived_from": []}
            for rel in items:
                right_rev = rel.right_revision
                left_rev  = rel.left_revision
                if left_rev.file_revision_id == revision_id:
                    child = {"name": _rev_label(right_rev), "revision_id": right_rev.file_revision_id}
                    child.update(_walk(right_rev.file_revision_id, visited, d - 1))
                    node["produces"].append(child)
                else:
                    parent = {"name": _rev_label(left_rev), "revision_id": left_rev.file_revision_id}
                    parent.update(_walk(left_rev.file_revision_id, visited, d - 1))
                    node["derived_from"].append(parent)
            return {k: v for k, v in node.items() if v}

        resource = self._v3.get_resource(resource_id)
        thread = {
            "name":        resource.display_name or resource.name,
            "resource_id": resource_id,
            "revision_id": resource.file_revision_id,
        }
        thread.update(_walk(resource.file_revision_id, set(), depth))
        return thread

    def print_digital_thread(self, resource_id: str, depth: int = 3) -> None:
        """Pretty-print the digital thread for a resource."""
        thread = self.get_digital_thread(resource_id, depth)

        def _render(node: dict, prefix: str = "", is_last: bool = True) -> None:
            connector = "└─ " if is_last else "├─ "
            print(f"{prefix}{connector}{node['name']}")
            child_prefix = prefix + ("   " if is_last else "│  ")
            produces = node.get("produces", [])
            derived  = node.get("derived_from", [])
            if derived:
                print(f"{child_prefix}{'└─' if not produces else '├─'} ← derived from:")
                d_prefix = child_prefix + ("   " if not produces else "│  ")
                for i, p in enumerate(derived):
                    _render(p, d_prefix, i == len(derived) - 1)
            if produces:
                print(f"{child_prefix}└─ → produces:")
                for i, c in enumerate(produces):
                    _render(c, child_prefix + "   ", i == len(produces) - 1)

        print(f"\n  {thread['name']}  (resource_id={thread['resource_id']})")
        produces = thread.get("produces", [])
        derived  = thread.get("derived_from", [])
        if derived:
            print("  ← derived from:")
            for i, p in enumerate(derived):
                _render(p, "    ", i == len(derived) - 1)
        if produces:
            print("  → produces:")
            for i, c in enumerate(produces):
                _render(c, "    ", i == len(produces) - 1)
        if not derived and not produces:
            print("  (no relationships found)")


# ════════════════════════════════════════════════════════════════════════════
# Schema loader
# ════════════════════════════════════════════════════════════════════════════

def load_schema(path: Path = HERE / "part-search-spec.schema.json") -> str:
    if not path.exists():
        raise FileNotFoundError(f"Schema file not found: {path}")
    return json.dumps(json.loads(path.read_text()), indent=2)


# ════════════════════════════════════════════════════════════════════════════
# Claude calls  (Anthropic SDK + Pydantic v2 structured output)
# ════════════════════════════════════════════════════════════════════════════

EXTRACT_SYSTEM = """\
You are a systems engineering analyst specialising in hardware BOM generation.

Given a list of raw Cameo/SysML requirements, classify each into:
- hardware: requirements describing a physical part, component, or assembly
- material: requirements referencing a material spec (AMS, MIL-SPEC, ASTM, etc.)
- performance: requirements with measurable values and units
- skipped_ids: IDs that are functional/behavioural/geometric design constraints
  on custom-manufactured parts — not commercially procurable

Rules:
- Only skip if a requirement clearly cannot drive a part search.
- Preserve original requirement IDs exactly."""


def classify_requirements(
    requirements: list[RawRequirement],
    claude: anthropic.Anthropic,
) -> ExtractedRequirements:
    """Stage 2 — Extract: classify requirements via Claude structured output."""
    req_text = "\n".join(f"[{r.req_id or r.id}] {r.text}" for r in requirements)
    response = claude.messages.parse(
        model="claude-opus-4-8",
        max_tokens=4096,
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": EXTRACT_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"Classify these requirements:\n\n{req_text}"}],
        output_format=ExtractedRequirements,
    )
    return response.parsed_output


def transform_requirements(
    typed_requirements: list[str],
    schema_text: str,
    claude: anthropic.Anthropic,
) -> list[PartSearchResult]:
    """Stage 3 — Transform: map typed requirements to PartSearchResult via Claude."""
    system_prompt = f"""You are a procurement engineer converting engineering requirements into
structured part-search specs conforming to the Nexar/Octopart part-search-spec schema.

OUTPUT SCHEMA
-------------
Every part_search you produce must conform to this JSON Schema:

{schema_text}

KEY RULES
---------
- part_search.search.category_id is REQUIRED. Nexar/Octopart numeric IDs:
    Chip Resistors (SMD): 140    Ceramic Capacitors:   456
    Inductors / Chokes:   57     MOSFETs:              77
    Diodes / Rectifiers:  68     Op-Amps:              12
    Microcontrollers:     2      Connectors (generic): 21
    USB Connectors:       298    LEDs (SMD):           85
    Crystals/Oscillators: 30     Fuses:                172
    Motor Drivers:        515    Power Regulators:     92

- part_search.parameters: REQUIRED, min 1 entry, all values must be strings.
  Nexar shortnames: resistance, capacitance, tolerance, voltage_rating,
  current_rating, power_rating, case_package, dielectric, mounting_style,
  frequency, load_capacitance, forward_voltage, luminous_intensity,
  output_current, supply_voltage_min, supply_voltage_max, color, material.

- lifecycle_status: ["Active"] by default.
- has_datasheet: true for electronic components.
- limit: 10 by default.
- output.groups: ["basic","specs","offers","datasheet","lifecycle"] always.
- source_req_id: preserve exactly.
- priority: default "medium"; safety-critical → "critical"."""

    response = claude.messages.parse(
        model="claude-opus-4-8",
        max_tokens=8192,
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": "Convert each requirement to a PartSearchResult:\n\n" + "\n".join(typed_requirements),
        }],
        output_format=list[PartSearchResult],
    )
    return response.parsed_output


# ════════════════════════════════════════════════════════════════════════════
# Pipeline
# ════════════════════════════════════════════════════════════════════════════

BATCH_SIZE = 20


def run_pipeline(
    requirements_id: str,
    claude: anthropic.Anthropic,
    istari: IstariCapability,
    schema_text: str,
    dry_run: bool = False,
) -> list[PartSearchResult]:
    """
    Run the full pipeline synchronously.
    v2 uses a plain function (not async) since anthropic.messages.parse()
    is synchronous and IstariCapability is synchronous.
    """

    # ── Step 1: Fetch ─────────────────────────────────────────────────────────
    print(f"\n[1/4] Downloading requirements.json (resource_id={requirements_id}) ...")
    req_revision_id = istari.get_revision_id(requirements_id)
    all_reqs        = istari.fetch_requirements(requirements_id)
    actionable      = [r for r in all_reqs if r.is_actionable()]
    print(f"      revision_id={req_revision_id}")
    print(f"      {len(all_reqs)} total, {len(actionable)} with text")

    if not actionable:
        print("No actionable requirements.")
        return []

    # ── Step 2: Classify ──────────────────────────────────────────────────────
    print(f"\n[2/4] Classifying requirements (Claude structured output) ...")
    extracted = classify_requirements(actionable, claude)

    print(f"      hardware={len(extracted.hardware)}  "
          f"material={len(extracted.material)}  "
          f"performance={len(extracted.performance)}  "
          f"skipped={len(extracted.skipped_ids)}")
    if extracted.skipped_ids:
        print(f"      skipped: {extracted.skipped_ids}")

    all_typed = (
        [f"[HW:{r.id}]   {r.text}" for r in extracted.hardware] +
        [f"[MAT:{r.id}]  {r.text}  spec={r.spec_number or 'N/A'}" for r in extracted.material] +
        [f"[PERF:{r.id}] {r.text}  value={r.value}{r.unit or ''}" for r in extracted.performance]
    )

    if not all_typed:
        print("\nAll requirements were skipped — no procurable parts found.")
        return []

    # ── Step 3: Transform ─────────────────────────────────────────────────────
    print(f"\n[3/4] Transforming to part-search specs (Claude structured output) ...")
    results: list[PartSearchResult] = []
    batches = [all_typed[i:i+BATCH_SIZE] for i in range(0, len(all_typed), BATCH_SIZE)]

    for i, batch in enumerate(batches, 1):
        print(f"      Batch {i}/{len(batches)} ({len(batch)} requirements) ...")
        results.extend(transform_requirements(batch, schema_text, claude))

    print(f"      ✓ {len(results)} specs generated")
    for r in results:
        print(f"      [{r.source_req_id}] category_id={r.part_search.search.category_id}  "
              f"params={list(r.part_search.parameters.keys())}  ({r.priority})")

    # ── Step 4: Upload + link ─────────────────────────────────────────────────
    print(f"\n[4/4] Uploading results to Istari and linking to requirements ...")

    local_out = HERE / "output"
    local_out.mkdir(exist_ok=True)
    uploaded: list[dict] = []

    for r in results:
        spec_data = {
            "$schema": "./part-search-spec.schema.json",
            **r.part_search.model_dump(exclude_none=True),
        }
        fname = f"{r.source_req_id}_part_search.json"
        (local_out / fname).write_text(json.dumps(spec_data, indent=2))

        if dry_run:
            print(f"  [dry-run] {fname} written locally")
        else:
            tmp = Path(f"/tmp/{fname}")
            tmp.write_text(json.dumps(spec_data, indent=2))
            resource = istari.upload_model(
                tmp, fname,
                f"Part search spec [{r.source_req_id}] — pydantic-v2 agent output",
            )
            tmp.unlink(missing_ok=True)
            istari.link_resources(req_revision_id, resource.file_revision_id)
            uploaded.append({
                "req_id":      r.source_req_id,
                "file":        fname,
                "resource_id": resource.resource_id,
                "revision_id": resource.file_revision_id,
            })
            print(f"  ✓ {fname}")
            print(f"      resource_id={resource.resource_id}")
            print(f"      linked: {req_revision_id[:8]}… --[produces]--> {resource.file_revision_id[:8]}…")

    summary = {
        "requirements_source":   requirements_id,
        "requirements_revision": req_revision_id,
        "total_requirements":    len(actionable),
        "classified":            len(all_typed),
        "skipped":               len(extracted.skipped_ids),
        "skipped_ids":           extracted.skipped_ids,
        "specs_generated":       len(results),
        "outputs": uploaded if not dry_run else [
            {"req_id": r.source_req_id, "file": f"{r.source_req_id}_part_search.json"}
            for r in results
        ],
    }
    summary_fname = "part_search_summary.json"
    (local_out / summary_fname).write_text(json.dumps(summary, indent=2))

    if not dry_run:
        tmp = Path(f"/tmp/{summary_fname}")
        tmp.write_text(json.dumps(summary, indent=2))
        resource = istari.upload_model(tmp, summary_fname, "Part Search Agent run summary")
        tmp.unlink(missing_ok=True)
        istari.link_resources(req_revision_id, resource.file_revision_id)
        print(f"  ✓ {summary_fname}  →  resource_id={resource.resource_id}")

    print(f"\n{'─'*60}")
    print(f"Done. {len(results)} spec(s) generated, "
          f"{len(extracted.skipped_ids)} requirements skipped.")
    print(f"Local output: {local_out}/")

    # ── Digital thread ────────────────────────────────────────────────────────
    if not dry_run and uploaded:
        print(f"\n{'═'*60}")
        print("Digital Thread")
        print(f"{'═'*60}")
        print("\nRequirements file and its outputs:\n")
        istari.print_digital_thread(requirements_id)
        print("\nPer-output file lineage:")
        for entry in uploaded:
            print()
            istari.print_digital_thread(entry["resource_id"])

    return results


# ════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Istari Part Search Agent v2 — Pydantic v2 + Anthropic SDK.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--requirements-id", metavar="UUID", required=True,
                   help="Istari resource ID of the requirements.json")
    p.add_argument("--anthropic-key", default=None)
    p.add_argument("--istari-url",    default=None,
                   help="e.g. https://fileservice-v2.demo.istari.app")
    p.add_argument("--istari-token",  default=None, help="Istari Personal Access Token")
    p.add_argument("--dry-run", action="store_true",
                   help="Classify and transform but write locally only, no Istari upload")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    api_key      = args.anthropic_key or os.environ.get("ANTHROPIC_API_KEY")
    istari_url   = args.istari_url    or os.environ.get("ISTARI_REGISTRY_URL")
    istari_token = args.istari_token  or os.environ.get("ISTARI_REGISTRY_AUTH_TOKEN")

    missing = {k for k, v in {
        "ANTHROPIC_API_KEY":          api_key,
        "ISTARI_REGISTRY_URL":        istari_url,
        "ISTARI_REGISTRY_AUTH_TOKEN": istari_token,
    }.items() if not v}
    if missing:
        for k in missing:
            print(f"ERROR: missing {k}", file=sys.stderr)
        sys.exit(1)

    schema_path = HERE / "part-search-spec.schema.json"
    schema_text = load_schema(schema_path)

    # Construct typed, validated capability and client
    istari = IstariCapability(registry_url=istari_url, pat=istari_token)
    claude = anthropic.Anthropic(api_key=api_key)

    print("Istari Part Search Agent  (Pydantic v2 edition)")
    print("=" * 60)
    print(f"  Istari:       {istari.registry_url}")
    print(f"  Schema:       {schema_path}")
    print(f"  Requirements: {args.requirements_id}")
    print(f"  Dry run:      {args.dry_run}")

    run_pipeline(
        requirements_id=args.requirements_id,
        claude=claude,
        istari=istari,
        schema_text=schema_text,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
