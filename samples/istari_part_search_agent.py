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
  python istari_part_search_agent.py --model-id <UUID> --provider anthropic --api-key sk-ant-...
  python istari_part_search_agent.py --model-id <UUID> --provider openai    --api-key sk-...
  python istari_part_search_agent.py --model-id <UUID> --provider genesis   --api-key <JWT> --model gpt-4o
  python istari_part_search_agent.py --model-id <UUID> --provider openai    --model gpt-4o-mini --dry-run

The agent resolves the requirements.json artifact from the given Istari model automatically.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
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


# ── Nexar / Octopart models ───────────────────────────────────────────────────

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


# ── SiliconExpert models ──────────────────────────────────────────────────────

class SEFilter(BaseModel):
    name:  str = Field(description="SiliconExpert attribute name, e.g. 'Resistance', 'Voltage Rating'")
    value: str = Field(description="Attribute value as a string with units, e.g. '4.7 kOhm', '50 V'")


class SEPartSearchSpec(BaseModel):
    """Matches the SiliconExpert ProductAPI request body schema directly."""
    model_config = ConfigDict(populate_by_name=True)

    category:      str = Field(description="SiliconExpert taxonomy category name — required")
    keyword:       str | None = None
    manufacturer:  str | None = None
    filters:       list[SEFilter] = Field(
        min_length=1,
        description=(
            "Parametric attribute filters as [{name, value}] pairs. "
            "Use SiliconExpert attribute names exactly: 'Resistance', 'Capacitance', "
            "'Inductance', 'Tolerance', 'Voltage Rating', 'Current Rating', "
            "'Power Rating', 'Case/Package', 'Temperature Coefficient', 'Dielectric', "
            "'Mounting Style', 'Frequency', 'Forward Voltage', 'Output Current', "
            "'Supply Voltage Min', 'Supply Voltage Max', 'Number of Pins', "
            "'Operating Temperature Min', 'Operating Temperature Max'. "
            "All values must be strings with units where applicable."
        ),
    )
    lifecycle:      list[Literal["Active","NRND","EOL","Obsolete","Unknown"]] | None = Field(default=None, min_length=1)
    rohsCompliant:  bool | None = None
    reachCompliant: bool | None = None
    pageSize:       int | None = Field(default=None, ge=1, le=100)
    pageNumber:     int | None = Field(default=None, ge=1)
    fields:         list[Literal["lifecycle","compliance","parametrics","pricing","alternates","pcn","environmental"]] | None = None

    @field_validator("category")
    @classmethod
    def _category_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("category must not be empty")
        return v


class SEPartSearchResult(BaseModel):
    source_req_id: str
    priority:      Literal["critical", "high", "medium", "low"] = "medium"
    part_search:   SEPartSearchSpec


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

    def find_requirements_resource_id(self, model_id: str) -> str:
        """
        Given a model ID, find the resource ID of its requirements.json artifact.
        Searches model artifacts for any file whose name contains 'requirements'
        and ends with '.json'.
        """
        model = self._v2.get_model(model_id=model_id)
        candidates = []
        for artifact in (model.artifacts or []):
            try:
                fname = artifact.file.revision.name or ""
            except Exception:
                fname = ""
            if "requirement" in fname.lower() and fname.lower().endswith(".json"):
                resource_id = artifact.file.resource_id
                if resource_id:
                    candidates.append((fname, resource_id))

        if not candidates:
            # List artifact filenames to help the user debug
            names = []
            for artifact in (model.artifacts or []):
                try:
                    names.append(artifact.file.revision.name or "?")
                except Exception:
                    names.append("?")
            raise ValueError(
                f"No requirements.json artifact found on model '{model_id}'.\n"
                f"Available artifact files: {names}"
            )

        if len(candidates) > 1:
            names = [c[0] for c in candidates]
            print(f"  [warn] Multiple requirements artifacts found: {names} — using '{candidates[0][0]}'")

        fname, resource_id = candidates[0]
        print(f"  [model] Found requirements artifact: '{fname}'  (resource_id={resource_id})")
        return resource_id

    def get_model_display_name(self, model_id: str) -> str | None:
        try:
            model = self._v2.get_model(model_id=model_id)
            return model.file.revision.display_name or model.file.revision.name
        except Exception:
            return None

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

    def _get_produces_type_id(self) -> str:
        """Look up the 'produces' relationship type ID for this Istari instance."""
        try:
            page = self._v3.list_revision_relationship_types(size=100)
            for rt in (page.items or []):
                name = (getattr(rt, "name", "") or "").lower()
                if name in ("produces", "produce"):
                    print(f"  [link] resolved 'produces' type id: {rt.id}")
                    return rt.id
            # Show available types if produces not found
            names = [getattr(rt, "name", "?") for rt in (page.items or [])]
            print(f"  [link] WARNING: 'produces' type not found. Available types: {names}")
        except Exception as exc:
            print(f"  [link] WARNING: could not list relationship types: {exc}")
        # Fall back to the known demo instance UUID
        fallback = "fea9bd01-81bc-4db4-9aff-289bdd9745c4"
        print(f"  [link] using fallback type id: {fallback}")
        return fallback

    def link_resources(self, from_revision_id: str, to_revision_id: str) -> None:
        produces_type_id = self._get_produces_type_id()
        self._v3.create_revision_relationship(
            NewRevisionRelationshipDto(
                relationship_type_id=produces_type_id,
                left_revision_id=from_revision_id,
                right_revision_id=to_revision_id,
            )
        )


# ════════════════════════════════════════════════════════════════════════════
# Schema loader
# ════════════════════════════════════════════════════════════════════════════

SCHEMA_FILES = {
    "nexar":         "part-search-spec.schema.json",
    "siliconexpert": "se-part-search-spec.schema.json",
}


def load_schema(search_api: str = "nexar") -> str:
    fname = SCHEMA_FILES.get(search_api)
    if not fname:
        raise ValueError(f"Unknown search_api '{search_api}'. Choose 'nexar' or 'siliconexpert'.")
    path = HERE / fname
    if not path.exists():
        raise FileNotFoundError(f"Schema file not found: {path}")
    return json.dumps(json.loads(path.read_text()), indent=2)


# ════════════════════════════════════════════════════════════════════════════
# pydantic-ai Agent system prompts
# ════════════════════════════════════════════════════════════════════════════

EXTRACT_SYSTEM = """\
You are a systems engineering analyst specialising in hardware BOM generation.

CRITICAL DATA PROVENANCE RULE
------------------------------
You MUST only reason from the requirement text provided in this prompt.
Do NOT introduce knowledge from training data, external standards, or assumed
context that is not explicitly present in the supplied requirements.
Every classification must be traceable directly to text found in the input.
If a requirement's intent is ambiguous from its text alone, classify conservatively
and note the ambiguity in skipped_ids rather than assuming missing context.

Given a list of raw Cameo/SysML requirements, classify each into:
- hardware: requirements describing a physical part, component, or assembly
- material: requirements referencing a material spec (AMS, MIL-SPEC, ASTM, etc.)
- performance: requirements with measurable values and units
- skipped_ids: IDs that are functional/behavioural/geometric design constraints
  on custom-manufactured parts — not commercially procurable

Rules:
- Only skip if a requirement clearly cannot drive a part search.
- Preserve original requirement IDs exactly."""


_DATA_PROVENANCE_RULE = """\
CRITICAL DATA PROVENANCE RULE
------------------------------
You MUST only derive part-search parameters from the requirement text supplied in
this prompt. Do NOT invent, assume, or infer values from training data, general
engineering knowledge, or context not present in the input.
- Every filter value, category, or parameter must be traceable to words or numbers
  in the provided requirement text.
- If the requirement does not state a specific value for a field, omit that field
  rather than guessing a typical industry value.
- Do not add requirements that were not in the input.
- Preserve source_req_id exactly as supplied — never fabricate IDs.
"""


def build_transform_prompt(schema_text: str, search_api: str = "nexar") -> str:
    if search_api == "siliconexpert":
        return f"""You are a procurement engineer converting engineering requirements into
structured part-search specs for the SiliconExpert ProductAPI.

{_DATA_PROVENANCE_RULE}

OUTPUT SCHEMA
-------------
Every part_search you produce must conform to this JSON Schema:

{schema_text}

KEY RULES
---------
- part_search.category is REQUIRED. Use exact SiliconExpert taxonomy strings:
    Resistors - Fixed              Resistors - Variable (Potentiometers)
    Capacitors - Ceramic (MLCC)    Capacitors - Aluminum Electrolytic
    Capacitors - Tantalum          Capacitors - Film
    Inductors - Fixed              Inductors - Chokes & Filters
    Transistors - MOSFET           Transistors - Bipolar (BJT)
    Diodes - General Purpose       Diodes - Zener
    Diodes - Schottky              Diodes - Bridge Rectifiers
    Integrated Circuits - Op Amps              Integrated Circuits - Microcontrollers
    Integrated Circuits - Voltage Regulators (Linear)
    Integrated Circuits - Voltage Regulators (Switching)
    Integrated Circuits - Logic Gates          Integrated Circuits - Motor Drivers
    Connectors - PCB               Connectors - USB
    Crystals & Oscillators         Fuses & Circuit Breakers
    LEDs - Standard                Switches - Tactile

- part_search.filters: REQUIRED, min 1 entry. A list of {{name, value}} objects.
  Use SiliconExpert attribute names exactly (title-case, spaces):
    "Resistance"          "Capacitance"        "Inductance"
    "Tolerance"           "Voltage Rating"     "Current Rating"
    "Power Rating"        "Case/Package"       "Temperature Coefficient"
    "Dielectric"          "Mounting Style"     "Frequency"
    "Forward Voltage"     "Output Current"     "Supply Voltage Min"
    "Supply Voltage Max"  "Number of Pins"     "Operating Temperature Min"
    "Operating Temperature Max"
  All values must be strings with units where applicable.
  Example: [{{"name": "Resistance", "value": "4.7 kOhm"}}, {{"name": "Tolerance", "value": "1%"}}]

- part_search.lifecycle: ["Active"] by default.
- part_search.rohsCompliant: true for all electronic components by default.
- part_search.pageSize: 10 by default.
- part_search.fields: ["lifecycle", "compliance", "parametrics"] always.
- source_req_id: preserve exactly.
- priority: default "medium"; safety-critical → "critical"."""

    # Default: Nexar/Octopart
    return f"""You are a procurement engineer converting engineering requirements into
structured part-search specs conforming to the Nexar/Octopart part-search-spec schema.

{_DATA_PROVENANCE_RULE}

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

def _make_openai_llm(model_name: str, api_key: str, base_url: str | None = None):
    """Construct an OpenAI-compatible pydantic-ai model, handling v1 and v2 API differences.

    pydantic-ai v1: OpenAIModel + OpenAIProvider(api_key, base_url)
    pydantic-ai v2: OpenAIChatModel + AsyncOpenAI(api_key, base_url) passed as openai_client
    """
    # Try v1 class name first, fall back to v2 rename
    try:
        from pydantic_ai.models.openai import OpenAIModel as _Model
    except ImportError:
        from pydantic_ai.models.openai import OpenAIChatModel as _Model  # type: ignore[no-redef]

    from pydantic_ai.providers.openai import OpenAIProvider as _Provider
    kwargs: dict = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return _Model(model_name, provider=_Provider(**kwargs))


def build_agents(
    provider: str,
    api_key: str,
    model_name: str,
    schema_text: str,
    search_api: str = "nexar",
) -> tuple[Agent, Agent]:
    """
    Build the two pydantic-ai Agents for the requested provider.
    Supported providers: 'anthropic', 'openai', 'genesis'.
    Supported search_api: 'nexar', 'siliconexpert'.
    """
    if provider == "anthropic":
        try:
            from pydantic_ai.models.anthropic import AnthropicModel as _Model
        except ImportError:
            from pydantic_ai.models.anthropic import AnthropicChatModel as _Model  # type: ignore[no-redef]
        try:
            from pydantic_ai.providers.anthropic import AnthropicProvider as _Provider
        except ImportError:
            from pydantic_ai.providers.anthropic import AnthropicChatProvider as _Provider  # type: ignore[no-redef]
        llm = _Model(model_name, provider=_Provider(api_key=api_key))

    elif provider == "openai":
        llm = _make_openai_llm(model_name, api_key)

    elif provider == "genesis":
        # AI Genesis Factory — OpenAI-compatible endpoint hosted by Lockheed Martin
        llm = _make_openai_llm(model_name, api_key, base_url=GENESIS_BASE_URL)

    else:
        raise ValueError(f"Unknown provider '{provider}'. Choose 'anthropic', 'openai', or 'genesis'.")

    transform_output_type = (
        list[SEPartSearchResult] if search_api == "siliconexpert" else list[PartSearchResult]
    )

    extract_agent: Agent = Agent(
        llm,
        output_type=ExtractedRequirements,
        system_prompt=EXTRACT_SYSTEM,
    )

    transform_agent: Agent = Agent(
        llm,
        output_type=transform_output_type,
        system_prompt=build_transform_prompt(schema_text, search_api),
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
) -> list:
    """Stage 3 — map typed requirements to PartSearchResult (or SEPartSearchResult) via transform_agent."""
    return agent.run_sync(
        "Convert each requirement to a PartSearchResult:\n\n"
        + "\n".join(typed_requirements)
    ).output


# ════════════════════════════════════════════════════════════════════════════
# Pipeline
# ════════════════════════════════════════════════════════════════════════════

BATCH_SIZE = 20


def run_pipeline(
    extract_agent: Agent,
    transform_agent: Agent,
    istari: IstariCapability,
    model_name: str,
    dry_run: bool = False,
    model_id: str | None = None,
    requirements_id: str | None = None,
    search_api: str = "nexar",
) -> list:

    # ── Step 1: Fetch ─────────────────────────────────────────────────────────
    if requirements_id:
        print(f"\n[1/4] Downloading requirements.json (resource_id={requirements_id}) ...")
        model_id = model_id or "N/A"
    else:
        print(f"\n[1/4] Locating requirements.json for model {model_id} ...")
        requirements_id = istari.find_requirements_resource_id(model_id)
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
    api_label = "SiliconExpert" if search_api == "siliconexpert" else "Nexar/Octopart"
    print(f"\n[3/4] Transforming to {api_label} part-search specs ({model_name}) ...")
    results: list = []
    batches = [all_typed[i:i+BATCH_SIZE] for i in range(0, len(all_typed), BATCH_SIZE)]

    for i, batch in enumerate(batches, 1):
        print(f"      Batch {i}/{len(batches)} ({len(batch)} requirements) ...")
        results.extend(transform_requirements(batch, transform_agent))

    print(f"      ✓ {len(results)} specs generated")
    for r in results:
        if search_api == "siliconexpert":
            filter_names = [f.name for f in r.part_search.filters]
            print(f"      [{r.source_req_id}] category='{r.part_search.category}'  "
                  f"filters={filter_names}  ({r.priority})")
        else:
            print(f"      [{r.source_req_id}] category_id={r.part_search.search.category_id}  "
                  f"params={list(r.part_search.parameters.keys())}  ({r.priority})")

    # ── Step 4: Upload + link ─────────────────────────────────────────────────
    print(f"\n[4/4] Uploading results to Istari and linking to requirements ...")

    local_out = HERE / "output"
    local_out.mkdir(exist_ok=True)
    uploaded: list[dict] = []

    schema_ref = (
        "./se-part-search-spec.schema.json"
        if search_api == "siliconexpert"
        else "./part-search-spec.schema.json"
    )
    file_suffix = "_se_part_search.json" if search_api == "siliconexpert" else "_part_search.json"

    for r in results:
        spec_data = {
            "$schema": schema_ref,
            **r.part_search.model_dump(exclude_none=True),
        }
        fname = f"{r.source_req_id}{file_suffix}"
        (local_out / fname).write_text(json.dumps(spec_data, indent=2))

        if dry_run:
            print(f"  [dry-run] {fname} written locally")
        else:
            tmp = Path(tempfile.gettempdir()) / fname
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

    # ── UUID provenance record ────────────────────────────────────────────────
    # Every Istari UUID the agent read from or wrote to, with its role.
    uuid_provenance: list[dict] = [
        {
            "uuid":  requirements_id,
            "role":  "requirements_source",
            "type":  "resource",
            "label": istari.get_resource_name(requirements_id) or "requirements.json",
        },
        {
            "uuid":  req_revision_id,
            "role":  "requirements_revision",
            "type":  "revision",
            "label": "requirements.json (revision used for reasoning)",
        },
    ]
    if model_id and model_id != "N/A":
        uuid_provenance.insert(0, {
            "uuid":  model_id,
            "role":  "source_model",
            "type":  "model",
            "label": "Istari model containing the requirements artifact",
        })
    for entry in uploaded:
        uuid_provenance.append({
            "uuid":  entry["resource_id"],
            "role":  "output_resource",
            "type":  "resource",
            "label": entry["file"],
        })
        uuid_provenance.append({
            "uuid":  entry["revision_id"],
            "role":  "output_revision",
            "type":  "revision",
            "label": f"{entry['file']} (revision)",
        })

    summary = {
        **({"model_id": model_id} if model_id and model_id != "N/A" else {}),
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
        "uuid_provenance": uuid_provenance,
    }
    summary_fname = "part_search_summary.json"
    (local_out / summary_fname).write_text(json.dumps(summary, indent=2))

    if not dry_run:
        tmp = Path(tempfile.gettempdir()) / summary_fname
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

    # ── UUID Provenance Summary ───────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print("UUID Provenance — Istari resources used in this run")
    print(f"{'═'*60}")
    col_w = 38
    print(f"  {'UUID':<{col_w}}  {'Role':<22}  Label")
    print(f"  {'-'*col_w}  {'-'*22}  {'-'*30}")
    for entry in uuid_provenance:
        uuid_str = entry["uuid"] or "N/A"
        print(f"  {uuid_str:<{col_w}}  {entry['role']:<22}  {entry['label']}")
    print(f"\n  All UUIDs are also recorded in: {local_out / 'part_search_summary.json'}")
    print(f"  (field: uuid_provenance)")

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
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--model-id", metavar="UUID",
                     help="Istari model ID — the agent finds the requirements.json artifact automatically")
    src.add_argument("--requirements-id", metavar="UUID",
                     help="Istari resource ID of the requirements.json (use instead of --model-id)")
    p.add_argument("--provider", choices=["anthropic", "openai", "genesis"], default="anthropic",
                   help="LLM provider to use (default: anthropic)")
    p.add_argument("--api-key", default=None,
                   help="API key for the chosen provider "
                        "(overrides ANTHROPIC_API_KEY / OPENAI_API_KEY)")
    p.add_argument("--model", default=None,
                   help="LLM model name (default: claude-opus-4-5 for anthropic, gpt-4o for openai/genesis)")
    p.add_argument("--istari-url",   default=None,
                   help="e.g. https://fileservice-v2.demo.istari.app")
    p.add_argument("--istari-token", default=None, help="Istari Personal Access Token")
    p.add_argument("--search-api", choices=["nexar", "siliconexpert"], default="nexar",
                   help="Part search API format for output (default: nexar)")
    p.add_argument("--env-file", default=None, metavar="PATH",
                   help="Path to a .env file with credentials (default: .env next to this script)")
    p.add_argument("--dry-run", action="store_true",
                   help="Classify and transform but write locally only, no Istari upload")
    return p.parse_args()


def _load_env(env_file: str | None) -> None:
    """Load a .env file into os.environ. CLI flags take priority (loaded before this)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("WARNING: python-dotenv not installed — .env file will not be loaded.", file=sys.stderr)
        print("         Run: pip install python-dotenv", file=sys.stderr)
        return

    path = Path(env_file) if env_file else HERE / ".env"
    if path.exists():
        load_dotenv(dotenv_path=path, override=False)  # override=False: env vars already set win
        print(f"  [config] Loaded credentials from {path}")
    elif env_file:
        # User explicitly asked for a file that doesn't exist — hard error
        print(f"ERROR: .env file not found: {path}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    args = parse_args()

    # Load .env before resolving credentials so env vars from the file are available.
    # Values already in the environment (or set via CLI flags stored in os.environ) take priority.
    _load_env(args.env_file)

    # Resolve API key: CLI flag → .env / environment variable for chosen provider
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

    schema_text = load_schema(args.search_api)

    # Validate Istari credentials via Pydantic before any API calls
    istari = IstariCapability(registry_url=istari_url, pat=istari_token)

    # Build provider-specific pydantic-ai agents
    extract_agent, transform_agent = build_agents(
        args.provider, api_key, model_name, schema_text, args.search_api
    )

    api_label = "SiliconExpert" if args.search_api == "siliconexpert" else "Nexar/Octopart"
    print("Istari Part Search Agent")
    print("=" * 60)
    print(f"  Provider:        {args.provider}")
    print(f"  LLM model:       {model_name}")
    print(f"  Search API:      {api_label}")
    print(f"  Istari:          {istari.registry_url}")
    if args.model_id:
        print(f"  Istari model:    {args.model_id}")
    else:
        print(f"  Requirements ID: {args.requirements_id}")
    print(f"  Dry run:         {args.dry_run}")

    run_pipeline(
        extract_agent=extract_agent,
        transform_agent=transform_agent,
        istari=istari,
        model_name=model_name,
        dry_run=args.dry_run,
        model_id=args.model_id,
        requirements_id=args.requirements_id,
        search_api=args.search_api,
    )


if __name__ == "__main__":
    main()
