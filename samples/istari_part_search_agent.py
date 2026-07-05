"""
Istari Part Search Agent
========================
Single-file pydantic-ai pipeline that accepts the LLM provider as input.
IstariCapability (Pydantic v2 BaseModel) handles all Istari SDK interactions.

Supported providers:
  --provider anthropic   Uses AnthropicModel (default: claude-opus-4-5)
  --provider openai      Uses OpenAIModel    (default: gpt-4o)

Two-stage pydantic-ai pipeline:
  1. extract_agent   → ExtractedRequirements   (classify raw Cameo requirements)
  2. transform_agent → list[PartSearchResult]  (map to part-search-spec schema)

Credentials (flags or environment variables):
  --provider anthropic --api-key sk-ant-...   / ANTHROPIC_API_KEY
  --provider openai    --api-key sk-...        / OPENAI_API_KEY
  --istari-url      / ISTARI_REGISTRY_URL
  --istari-token    / ISTARI_REGISTRY_AUTH_TOKEN

Usage:
  python istari_part_search_agent.py --requirements-id <UUID> --provider anthropic --api-key sk-ant-...
  python istari_part_search_agent.py --requirements-id <UUID> --provider openai    --api-key sk-...
  python istari_part_search_agent.py --requirements-id <UUID> --provider openai    --model gpt-4o-mini --dry-run
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

from pydantic_ai import Agent
from istari_digital_client import Client, Configuration, V3Client
from istari_digital_client.v3.models.new_revision_relationship_dto import (
    NewRevisionRelationshipDto,
)
from istari_digital_client.v3.models.resource_type_dto import ResourceTypeDto
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

HERE = Path(__file__).parent

PROVIDER_DEFAULTS = {
    "anthropic": "claude-opus-4-5",
    "openai":    "gpt-4o",
    "genesis":   "gpt-4o",  # override with --model for the specific Genesis model name
}

GENESIS_BASE_URL = "https://api.ai.us.lmco.com/v1"


# ════════════════════════════════════════════════════════════════════════════
# Pydantic v2 data models
# ════════════════════════════════════════════════════════════════════════════

class RawRequirement(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id:     str
    name:   str = ""
    text:   str = ""
    req_id: str = ""

    @model_validator(mode="before")
    @classmethod
    def _extract_from_cameo(cls, data: dict) -> dict:
        tags = data.get("tags", {})
        if not data.get("text"):
            data["text"] = tags.get("Text", "")
        if not data.get("req_id"):
            data["req_id"] = tags.get("Id", "")
        return data

    def is_actionable(self) -> bool:
        return bool(self.text and self.text.strip())


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
    parameters: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Nexar attribute shortname → value pairs. All values must be strings. "
            "Valid shortnames: resistance, capacitance, inductance, tolerance, "
            "voltage_rating, current_rating, power_rating, case_package, "
            "temperature_coefficient, dielectric, mounting_style, frequency, "
            "load_capacitance, forward_voltage, luminous_intensity, output_current, "
            "supply_voltage_min, supply_voltage_max, color, material, alloy, temper."
        ),
    )
    output: OutputSpec | None = None

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
    Pydantic v2 model encapsulating Istari authentication and all SDK operations.
    Using BaseModel makes auth config validatable and injectable as a typed dependency.
    SDK clients are stored as PrivateAttr and initialised in model_post_init.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    registry_url: str = Field(description="Istari registry base URL")
    pat:          str = Field(description="Personal Access Token", repr=False)

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
        config = Configuration(registry_url=self.registry_url, registry_auth_token=self.pat)
        self._v3 = V3Client(config)
        self._v2 = Client(config)

    def fetch_requirements(self, resource_id: str) -> list[RawRequirement]:
        resource = self._v3.get_resource(resource_id)
        raw = json.loads(self._v3.get_content(resource))
        if not isinstance(raw, list):
            raw = [raw]
        return [RawRequirement.model_validate(r) for r in raw if isinstance(r, dict)]

    def get_revision_id(self, resource_id: str) -> str:
        return self._v3.get_resource(resource_id).file_revision_id

    def get_resource_name(self, resource_id: str) -> str | None:
        try:
            return self._v3.get_resource(resource_id).display_name
        except Exception:
            return None

    def upload_model(self, path: Path, display_name: str, description: str):
        return self._v3.create_resource(
            path=path,
            resource_type=ResourceTypeDto.MODEL,
            display_name=display_name,
            description=description,
            version_name="v1.0",
            external_identifier=f"part-search-agent/{display_name}",
        )

    def link_resources(self, from_revision_id: str, to_revision_id: str) -> None:
        PRODUCES_TYPE_ID = "fea9bd01-81bc-4db4-9aff-289bdd9745c4"
        self._v3.create_revision_relationship(
            NewRevisionRelationshipDto(
                relationship_type_id=PRODUCES_TYPE_ID,
                left_revision_id=from_revision_id,
                right_revision_id=to_revision_id,
            )
        )


# ════════════════════════════════════════════════════════════════════════════
# Schema loader
# ════════════════════════════════════════════════════════════════════════════

def load_schema(path: Path = HERE / "part-search-spec.schema.json") -> str:
    if not path.exists():
        raise FileNotFoundError(f"Schema file not found: {path}")
    return json.dumps(json.loads(path.read_text()), indent=2)


# ════════════════════════════════════════════════════════════════════════════
# pydantic-ai Agent system prompts
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


def build_transform_prompt(schema_text: str) -> str:
    return f"""You are a procurement engineer converting engineering requirements into
structured part-search specs conforming to the Nexar/Octopart part-search-spec schema.

OUTPUT SCHEMA
-------------
Every part_search you produce must conform to this JSON Schema:

{schema_text}

KEY RULES
---------
- part_search.search.category_id is REQUIRED. Nexar/Octopart numeric IDs:
    Chip Resistors (SMD): 140    Through-hole Resistors: 3
    Ceramic Capacitors:   456    Electrolytic Capacitors: 58
    Inductors / Chokes:   57     MOSFETs:                 77
    Diodes / Rectifiers:  68     Op-Amps:                 12
    Microcontrollers:     2      Connectors (generic):    21
    USB Connectors:       298    LEDs (SMD):              85
    Crystals/Oscillators: 30     Fuses:                   172
    Motor Drivers:        515    Power Regulators:        92

- part_search.parameters: REQUIRED, min 1 entry. A dict mapping Nexar attribute
  shortname to string value. All values must be strings (quote numbers).
  Shortnames: resistance, capacitance, inductance, tolerance, voltage_rating,
  current_rating, power_rating, case_package, temperature_coefficient, dielectric,
  mounting_style, frequency, load_capacitance, forward_voltage, luminous_intensity,
  output_current, supply_voltage_min, supply_voltage_max, color, material, alloy, temper.

- lifecycle_status: ["Active"] by default.
- has_datasheet: true for electronic components.
- limit: 10 by default.
- output.groups: ["basic","specs","offers","datasheet","lifecycle"] always.
- source_req_id: preserve exactly.
- priority: default "medium"; safety-critical → "critical"."""


# ════════════════════════════════════════════════════════════════════════════
# Agent factory  —  provider-agnostic
# ════════════════════════════════════════════════════════════════════════════

def build_agents(
    provider: str,
    api_key: str,
    model_name: str,
    schema_text: str,
) -> tuple[Agent, Agent]:
    """
    Build the two pydantic-ai Agents for the requested provider.
    Supported providers: 'anthropic', 'openai', 'genesis'.
    """
    if provider == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider
        llm = AnthropicModel(model_name, provider=AnthropicProvider(api_key=api_key))

    elif provider == "openai":
        from pydantic_ai.models.openai import OpenAIModel
        from pydantic_ai.providers.openai import OpenAIProvider
        llm = OpenAIModel(model_name, provider=OpenAIProvider(api_key=api_key))

    elif provider == "genesis":
        # AI Genesis Factory — OpenAI-compatible endpoint hosted by Lockheed Martin
        from pydantic_ai.models.openai import OpenAIModel
        from pydantic_ai.providers.openai import OpenAIProvider
        llm = OpenAIModel(
            model_name,
            provider=OpenAIProvider(api_key=api_key, base_url=GENESIS_BASE_URL),
        )

    else:
        raise ValueError(f"Unknown provider '{provider}'. Choose 'anthropic', 'openai', or 'genesis'.")

    extract_agent: Agent = Agent(
        llm,
        output_type=ExtractedRequirements,
        system_prompt=EXTRACT_SYSTEM,
    )

    transform_agent: Agent = Agent(
        llm,
        output_type=list[PartSearchResult],
        system_prompt=build_transform_prompt(schema_text),
    )

    return extract_agent, transform_agent


# ════════════════════════════════════════════════════════════════════════════
# Pipeline stages
# ════════════════════════════════════════════════════════════════════════════

def classify_requirements(
    requirements: list[RawRequirement],
    agent: Agent,
) -> ExtractedRequirements:
    """Stage 2 — classify requirements via pydantic-ai extract_agent."""
    req_text = "\n".join(f"[{r.req_id or r.id}] {r.text}" for r in requirements)
    return agent.run_sync(f"Classify these requirements:\n\n{req_text}").output


def transform_requirements(
    typed_requirements: list[str],
    agent: Agent,
) -> list[PartSearchResult]:
    """Stage 3 — map typed requirements to PartSearchResult via pydantic-ai transform_agent."""
    return agent.run_sync(
        "Convert each requirement to a PartSearchResult:\n\n"
        + "\n".join(typed_requirements)
    ).output


# ════════════════════════════════════════════════════════════════════════════
# Pipeline
# ════════════════════════════════════════════════════════════════════════════

BATCH_SIZE = 20


def run_pipeline(
    requirements_id: str,
    extract_agent: Agent,
    transform_agent: Agent,
    istari: IstariCapability,
    model_name: str,
    dry_run: bool = False,
) -> list[PartSearchResult]:

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
    print(f"\n[2/4] Classifying requirements ({model_name}) ...")
    extracted = classify_requirements(actionable, extract_agent)

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
    print(f"\n[3/4] Transforming to part-search specs ({model_name}) ...")
    results: list[PartSearchResult] = []
    batches = [all_typed[i:i+BATCH_SIZE] for i in range(0, len(all_typed), BATCH_SIZE)]

    for i, batch in enumerate(batches, 1):
        print(f"      Batch {i}/{len(batches)} ({len(batch)} requirements) ...")
        results.extend(transform_requirements(batch, transform_agent))

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
                f"Part search spec [{r.source_req_id}] — pydantic-ai agent output",
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
        "model":                 model_name,
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

    if not dry_run and uploaded:
        print(f"\n{'═'*60}")
        print("Digital Thread")
        print(f"{'═'*60}")
        src_name = istari.get_resource_name(requirements_id) or requirements_id
        print(f"\n  {src_name}  (id={requirements_id})")
        print(f"  └─ produces:")
        for i, entry in enumerate(uploaded):
            connector = "└─" if i == len(uploaded) - 1 else "├─"
            print(f"     {connector} {entry['file']}  (id={entry['resource_id']})")

    return results


# ════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Istari Part Search Agent — pydantic-ai, provider-agnostic.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--requirements-id", metavar="UUID", required=True,
                   help="Istari resource ID of the requirements.json")
    p.add_argument("--provider", choices=["anthropic", "openai", "genesis"], default="anthropic",
                   help="LLM provider to use (default: anthropic)")
    p.add_argument("--api-key", default=None,
                   help="API key for the chosen provider "
                        "(overrides ANTHROPIC_API_KEY / OPENAI_API_KEY)")
    p.add_argument("--model", default=None,
                   help="Model name (default: claude-opus-4-5 for anthropic, gpt-4o for openai)")
    p.add_argument("--istari-url",   default=None,
                   help="e.g. https://fileservice-v2.demo.istari.app")
    p.add_argument("--istari-token", default=None, help="Istari Personal Access Token")
    p.add_argument("--dry-run", action="store_true",
                   help="Classify and transform but write locally only, no Istari upload")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Resolve API key: explicit flag → env var for chosen provider
    env_key_name = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai":    "OPENAI_API_KEY",
        "genesis":   "GENESIS_API_KEY",
    }[args.provider]
    api_key = args.api_key or os.environ.get(env_key_name)
    istari_url   = args.istari_url   or os.environ.get("ISTARI_REGISTRY_URL")
    istari_token = args.istari_token or os.environ.get("ISTARI_REGISTRY_AUTH_TOKEN")
    model_name   = args.model        or PROVIDER_DEFAULTS[args.provider]

    missing = {k for k, v in {
        env_key_name:                 api_key,
        "ISTARI_REGISTRY_URL":        istari_url,
        "ISTARI_REGISTRY_AUTH_TOKEN": istari_token,
    }.items() if not v}
    if missing:
        for k in missing:
            print(f"ERROR: missing {k}", file=sys.stderr)
        sys.exit(1)

    schema_text = load_schema()

    # Validate Istari credentials via Pydantic before any API calls
    istari = IstariCapability(registry_url=istari_url, pat=istari_token)

    # Build provider-specific pydantic-ai agents
    extract_agent, transform_agent = build_agents(args.provider, api_key, model_name, schema_text)

    print("Istari Part Search Agent")
    print("=" * 60)
    print(f"  Provider:     {args.provider}")
    print(f"  Model:        {model_name}")
    print(f"  Istari:       {istari.registry_url}")
    print(f"  Requirements: {args.requirements_id}")
    print(f"  Dry run:      {args.dry_run}")

    run_pipeline(
        requirements_id=args.requirements_id,
        extract_agent=extract_agent,
        transform_agent=transform_agent,
        istari=istari,
        model_name=model_name,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
