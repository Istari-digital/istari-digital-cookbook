"""
Istari Part Selector Agent
==========================
Pydantic-ai pipeline that reads Istari requirements and Silicon Expert part
datasheet JSON artifacts, reasons over them to select the best part(s), and
outputs OrCAD Capture CIS XML files ready for import into Cadence.

Pipeline (4 stages):
  1. Fetch requirements.json from Istari (by model ID or resource ID)
  2. Discover SE datasheet JSON artifacts on the same Istari model automatically
  3. LLM selects best part(s) satisfying the requirements
  4. Generate OrCAD CIS XML files + OLB symbol library JSON, upload to Istari

Supported providers:
  --provider anthropic   (default: claude-opus-4-5)
  --provider openai      (default: gpt-4o)
  --provider genesis     (default: gpt-4o, LM AI Genesis Factory)

Credentials (flags > .env > environment variables):
  --istari-url    / ISTARI_REGISTRY_URL
  --istari-token  / ISTARI_REGISTRY_AUTH_TOKEN
  --api-key       / ANTHROPIC_API_KEY | OPENAI_API_KEY | GENESIS_API_KEY

Usage:
  # Requirements + datasheets on the same model — fully automatic discovery:
  python istari_part_selector_agent.py \\
      --model-id <UUID> --provider anthropic

  # Pin a specific requirements resource, still auto-discover datasheets on the model:
  python istari_part_selector_agent.py \\
      --model-id <UUID> --requirements-id <UUID> --provider openai --model gpt-4o

  # Dry run (write XML locally only, no Istari upload):
  python istari_part_selector_agent.py \\
      --model-id <UUID> --dry-run
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
from xml.dom import minidom
import xml.etree.ElementTree as ET

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
    "genesis":   "gpt-4o",
}

GENESIS_BASE_URL = "https://api.ai.us.lmco.com/v1"

# Artifact filename patterns used to identify SE datasheet JSON files
DATASHEET_PATTERNS = ("datasheet", "se_part", "se-part", "silicon_expert", "siliconexpert", "part_data")


# ════════════════════════════════════════════════════════════════════════════
# Pydantic v2 data models — input
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

    @property
    def display_id(self) -> str:
        return self.req_id or self.id


class SEParametric(BaseModel):
    model_config = ConfigDict(extra="allow")
    name:  str = ""
    value: str = ""
    unit:  str | None = None

    def as_string(self) -> str:
        parts = [self.name, self.value]
        if self.unit:
            parts.append(self.unit)
        return " ".join(p for p in parts if p)


class SEPartDatasheet(BaseModel):
    """Flexible model for a Silicon Expert part datasheet stored as JSON in Istari."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    part_number:    str             = Field(default="", alias="partNumber")
    manufacturer:   str             = Field(default="")
    description:    str             = Field(default="")
    category:       str             = Field(default="")
    lifecycle:      str             = Field(default="Active")
    rohs_compliant: bool | None     = Field(default=None, alias="rohsCompliant")
    reach_compliant: bool | None    = Field(default=None, alias="reachCompliant")
    parametrics:    list[SEParametric] = Field(default_factory=list)
    datasheet_url:  str | None      = Field(default=None, alias="datasheetUrl")

    # Populated after loading — not from JSON
    source_file: str = Field(default="", exclude=True)

    def summary(self) -> str:
        """Return a compact text summary for the LLM prompt."""
        lines = [
            f"Part: {self.part_number}",
            f"  Manufacturer: {self.manufacturer}",
            f"  Description:  {self.description}",
            f"  Category:     {self.category}",
            f"  Lifecycle:    {self.lifecycle}",
            f"  RoHS:         {'Yes' if self.rohs_compliant else 'No' if self.rohs_compliant is False else 'Unknown'}",
        ]
        if self.parametrics:
            lines.append("  Parametrics:")
            for p in self.parametrics:
                lines.append(f"    - {p.as_string()}")
        if self.datasheet_url:
            lines.append(f"  Datasheet: {self.datasheet_url}")
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# Pydantic v2 data models — LLM output
# ════════════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════════
# OLB symbol library models
# ════════════════════════════════════════════════════════════════════════════

class OLBPin(BaseModel):
    """One pin in an OrCAD schematic symbol."""
    number:   str = Field(description="Pin number as a string, e.g. '1', '2', 'A1'")
    name:     str = Field(description="Pin name, e.g. 'VCC', 'GND', 'IN+', 'OUT', 'SDA'")
    type:     Literal["Power", "Input", "Output", "Passive", "Bidirectional", "OpenCollector",
                      "OpenEmitter", "NotConnected", "3State"] = Field(
        description="OrCAD pin electrical type"
    )
    shape:    Literal["Line", "Short", "Clock", "InvertedClock", "DotClock",
                      "Dot", "ZeroLength"] = Field(
        default="Line",
        description="OrCAD pin shape. Use 'Short' for hidden power/GND pins (sets visible=false)."
    )
    group:    str = Field(
        default="1",
        description="Pin group number as a string — '1' for single-body, '1'/'2'/… for multi-section ICs"
    )
    position: Literal["Top", "Bottom", "Left", "Right"] = Field(
        description=(
            "Side of the symbol body this pin is placed on. "
            "Convention: Power/VCC → Top, GND → Bottom, Inputs → Left, Outputs → Right"
        )
    )
    visible:  bool = Field(
        default=True,
        description=(
            "Whether the pin stub is visible on the schematic symbol. "
            "Set false for hidden power/GND pins that use shape='Short'."
        )
    )


class SelectedPart(BaseModel):
    """A part selected by the LLM to satisfy one or more requirements."""
    model_config = ConfigDict(populate_by_name=True)

    # Core identification
    mpn:          str = Field(description="Manufacturer part number, exactly as in the datasheet")
    manufacturer: str = Field(description="Manufacturer name")
    description:  str = Field(description="Short human-readable description of the part")
    part_type:    Literal["Electrical", "Mechanical", "IC", "Connector", "Passive", "Other"] = \
        Field(description="OrCAD CIS part type")

    # OrCAD part metadata
    part_name:     str = Field(
        description=(
            "Short base name used as the filename and OrCAD symbol name — "
            "the model/family identifier without grade/package suffix. "
            "Example: MPN 'MIC28516T-E/PHA' → part_name 'MIC28516T'. "
            "Strip trailing grade codes like -E, /PHA, -T, -TR, -ND, etc. "
            "Use only alphanumeric characters and hyphens."
        )
    )
    num_sections:  str = Field(
        default="1",
        description="Number of schematic sections/gates (as string). '1' for most parts; '2' for dual op-amps, etc."
    )
    section_style: str = Field(
        default="1",
        description="OrCAD section style. '1' = homogeneous single section."
    )
    package_type:  str | None = Field(
        default=None,
        description="Base package family, e.g. 'TSSOP', 'SOT-23', 'DIP', 'QFN' (without pin count suffix)"
    )

    # OrCAD schematic info
    reference_designator: str = Field(
        description="Standard reference designator prefix: R, C, L, U, J, Q, D, F, SW, etc."
    )
    value:          str | None = Field(default=None, description="Component value, e.g. '4.7k', '100nF', '5V 1A'")
    package:        str | None = Field(default=None, description="Package/footprint name, e.g. '0402', 'SOT-23', 'DIP-8'")
    schematic_part: str | None = Field(default=None, description="OrCAD schematic symbol name if known")
    footprint:      str | None = Field(default=None, description="PCB footprint name if known")

    # Key electrical properties (only populate what applies)
    resistance:        str | None = None
    capacitance:       str | None = None
    inductance:        str | None = None
    tolerance:         str | None = None
    voltage_rating:    str | None = None
    current_rating:    str | None = None
    power_rating:      str | None = None
    frequency:         str | None = None
    temp_min:          str | None = None
    temp_max:          str | None = None
    supply_voltage_min: str | None = None
    supply_voltage_max: str | None = None
    forward_voltage:   str | None = None
    output_current:    str | None = None

    # Compliance
    lifecycle:    str = Field(default="Active")
    rohs_status:  str = Field(default="Compliant")
    reach_status: str = Field(default="Compliant")

    # Source
    datasheet_url: str | None = None

    # Traceability — critical for digital thread
    source_requirement_ids: list[str] = Field(
        min_length=1,
        description="List of requirement IDs (req_id/id) that this part satisfies"
    )
    selection_rationale: str = Field(
        description="Concise explanation of why this part was chosen over alternatives"
    )

    # Catch-all for extra parametrics not in the fixed fields
    additional_specs: dict[str, str] = Field(
        default_factory=dict,
        description="Any additional parametrics from the datasheet not covered by the fixed fields"
    )

    # OrCAD OLB symbol pin definitions
    pins: list[OLBPin] = Field(
        default_factory=list,
        description=(
            "Pin definitions for the OrCAD schematic symbol. "
            "Infer from part type and datasheet data. "
            "Resistors/caps/inductors: 2 Passive pins (1=Left, 2=Right). "
            "Diodes: Anode=Left Passive, Cathode=Right Passive. "
            "MOSFETs: Gate=Left Input, Drain=Top Output, Source=Bottom Passive. "
            "Op-amps: IN+=Left Input, IN-=Left Input, OUT=Right Output, V+=Top Power, V-=Bottom Power. "
            "ICs: derive from number-of-pins parametric; group power/gnd separately from signal pins."
        )
    )


class PartSelectionOutput(BaseModel):
    """Top-level output from the selection agent."""
    selected_parts:    list[SelectedPart] = Field(
        description="Parts selected to satisfy the requirements. Multiple parts are allowed."
    )
    unmatched_req_ids: list[str] = Field(
        default_factory=list,
        description="Requirement IDs that could not be matched to any available datasheet"
    )
    notes: str | None = Field(
        default=None,
        description="Any overall notes about the selection, gaps, or assumptions"
    )


# ════════════════════════════════════════════════════════════════════════════
# IstariCapability — Pydantic v2 model encapsulating auth + SDK
# ════════════════════════════════════════════════════════════════════════════

class IstariCapability(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    registry_url: str = Field(description="Istari registry base URL")
    pat:          str = Field(description="Personal Access Token", repr=False)

    _v3: V3Client = PrivateAttr()
    _v2: Client   = PrivateAttr()

    @field_validator("registry_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
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

    # ── Requirements ─────────────────────────────────────────────────────────

    def find_requirements_resource_id(self, model_id: str) -> str:
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
            names = []
            for artifact in (model.artifacts or []):
                try:
                    names.append(artifact.file.revision.name or "?")
                except Exception:
                    names.append("?")
            raise ValueError(
                f"No requirements.json artifact found on model '{model_id}'.\n"
                f"Available: {names}"
            )
        if len(candidates) > 1:
            print(f"  [warn] Multiple requirements artifacts: {[c[0] for c in candidates]} — using '{candidates[0][0]}'")
        fname, resource_id = candidates[0]
        print(f"  [model] requirements artifact: '{fname}'  (resource_id={resource_id})")
        return resource_id

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

    # ── SE Datasheets ─────────────────────────────────────────────────────────

    def find_datasheet_resource_ids(self, model_id: str) -> list[tuple[str, str]]:
        """Find SE datasheet JSON artifacts on a model. Returns [(filename, resource_id)]."""
        model = self._v2.get_model(model_id=model_id)
        found = []
        for artifact in (model.artifacts or []):
            try:
                fname = artifact.file.revision.name or ""
            except Exception:
                fname = ""
            fname_lower = fname.lower()
            is_json = fname_lower.endswith(".json")
            is_datasheet = any(pat in fname_lower for pat in DATASHEET_PATTERNS)
            resource_id = None
            try:
                resource_id = artifact.file.resource_id
            except Exception:
                pass
            if is_json and is_datasheet and resource_id:
                found.append((fname, resource_id))
        return found

    def fetch_datasheet_json(self, resource_id: str) -> dict | list:
        """Download a JSON artifact from Istari and return parsed content."""
        resource = self._v3.get_resource(resource_id)
        return json.loads(self._v3.get_content(resource))

    # ── Upload / Link ─────────────────────────────────────────────────────────

    def upload_resource(self, path: Path, display_name: str, description: str):
        return self._v3.create_resource(
            path=path,
            resource_type=ResourceTypeDto.MODEL,
            display_name=display_name,
            description=description,
            version_name="v1.0",
            external_identifier=f"part-selector-agent/{display_name}",
        )

    def _get_produces_type_id(self) -> str:
        try:
            page = self._v3.list_revision_relationship_types(size=100)
            for rt in (page.items or []):
                name = (getattr(rt, "name", "") or "").lower()
                if name in ("produces", "produce"):
                    print(f"  [link] resolved 'produces' type id: {rt.id}")
                    return rt.id
            names = [getattr(rt, "name", "?") for rt in (page.items or [])]
            print(f"  [link] WARNING: 'produces' not found. Available: {names}")
        except Exception as exc:
            print(f"  [link] WARNING: could not list relationship types: {exc}")
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
# SE datasheet loader
# ════════════════════════════════════════════════════════════════════════════

def _parse_se_json(raw: dict | list, source_file: str) -> list[SEPartDatasheet]:
    """
    Parse SE JSON into a list of SEPartDatasheet objects.
    Handles multiple SE API response shapes:
      - A single part dict
      - A list of part dicts
      - {"products": [...]} wrapper
      - {"data": {...}} or {"results": [...]} wrappers
    """
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        # Try common SE wrapper keys
        for key in ("products", "results", "data", "parts", "items"):
            if key in raw and isinstance(raw[key], (list, dict)):
                inner = raw[key]
                items = inner if isinstance(inner, list) else [inner]
                break
        else:
            items = [raw]
    else:
        return []

    parts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            # Normalise parametrics: accept list[dict] or dict[str, str]
            parametrics_raw = item.get("parametrics", item.get("attributes", []))
            if isinstance(parametrics_raw, dict):
                parametrics_raw = [{"name": k, "value": v} for k, v in parametrics_raw.items()]
            item["parametrics"] = parametrics_raw

            part = SEPartDatasheet.model_validate(item)
            part.source_file = source_file
            parts.append(part)
        except Exception:
            continue
    return parts


def load_datasheets_from_istari(
    istari: IstariCapability,
    model_id: str | None,
    resource_ids: list[str],
) -> list[SEPartDatasheet]:
    """Load all SE datasheet JSONs from Istari and return parsed parts."""
    to_fetch: list[tuple[str, str]] = []  # [(label, resource_id)]

    if model_id:
        print(f"  [datasheets] Scanning artifacts on model {model_id} ...")
        found = istari.find_datasheet_resource_ids(model_id)
        if not found:
            print(f"  [datasheets] WARNING: no SE datasheet JSON artifacts found on model {model_id}")
            print(f"               Expected filenames containing: {DATASHEET_PATTERNS}")
        else:
            print(f"  [datasheets] Found {len(found)} datasheet artifact(s):")
            for fname, rid in found:
                print(f"               '{fname}'  (resource_id={rid})")
            to_fetch.extend(found)

    for rid in resource_ids:
        label = istari.get_resource_name(rid) or rid
        to_fetch.append((label, rid))

    if not to_fetch:
        return []

    all_parts: list[SEPartDatasheet] = []
    for label, rid in to_fetch:
        try:
            raw = istari.fetch_datasheet_json(rid)
            parts = _parse_se_json(raw, source_file=label)
            print(f"  [datasheets] '{label}': {len(parts)} part(s) parsed")
            all_parts.extend(parts)
        except Exception as exc:
            print(f"  [datasheets] WARNING: could not load '{label}': {exc}")

    return all_parts


# ════════════════════════════════════════════════════════════════════════════
# OrCAD Capture CIS XML generator
# ════════════════════════════════════════════════════════════════════════════

def part_to_cis_xml(part: SelectedPart) -> str:
    """Serialize a SelectedPart to OrCAD Capture CIS XML (pretty-printed)."""

    root = ET.Element("CISDatabase")
    root.set("version", "1.0")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")

    comp = ET.SubElement(root, "Component")

    def attr(name: str, value: str | bool | None) -> None:
        if value is None or value == "":
            return
        a = ET.SubElement(comp, "Attribute")
        a.set("name", name)
        if isinstance(value, bool):
            a.text = "Yes" if value else "No"
        else:
            a.text = str(value)

    # Primary identification
    attr("Part_Number",  part.mpn)
    attr("MPN",          part.mpn)
    attr("Part_Type",    part.part_type)
    attr("Description",  part.description)
    attr("Manufacturer", part.manufacturer)

    # OrCAD schematic
    attr("Reference",      part.reference_designator)
    attr("Value",          part.value)
    attr("Package",        part.package)
    attr("Schematic_Part", part.schematic_part or part.reference_designator)
    attr("Footprint",      part.footprint)

    # Electrical properties (only those with values)
    attr("Resistance",         part.resistance)
    attr("Capacitance",        part.capacitance)
    attr("Inductance",         part.inductance)
    attr("Tolerance",          part.tolerance)
    attr("Voltage_Rating",     part.voltage_rating)
    attr("Current_Rating",     part.current_rating)
    attr("Power_Rating",       part.power_rating)
    attr("Frequency",          part.frequency)
    attr("Temperature_Min",    part.temp_min)
    attr("Temperature_Max",    part.temp_max)
    attr("Supply_Voltage_Min", part.supply_voltage_min)
    attr("Supply_Voltage_Max", part.supply_voltage_max)
    attr("Forward_Voltage",    part.forward_voltage)
    attr("Output_Current",     part.output_current)

    # Compliance
    attr("Lifecycle",  part.lifecycle)
    attr("RoHS",       part.rohs_status)
    attr("REACH",      part.reach_status)

    # Source reference
    attr("Datasheet_URL",       part.datasheet_url)
    attr("Source_Requirements", ", ".join(part.source_requirement_ids))
    attr("Selection_Rationale", part.selection_rationale)

    # Additional parametrics
    for k, v in (part.additional_specs or {}).items():
        attr(k, v)

    xml_bytes = ET.tostring(root, encoding="unicode", xml_declaration=False)
    dom = minidom.parseString(f'<?xml version="1.0" encoding="UTF-8"?>{xml_bytes}')
    return dom.toprettyxml(indent="  ", encoding=None).lstrip('<?xml version="1.0" ?>\n').strip()


def parts_to_cis_xml_bundle(parts: list[SelectedPart]) -> str:
    """Serialize all selected parts into a single CIS XML file."""
    root = ET.Element("CISDatabase")
    root.set("version", "1.0")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")

    for part in parts:
        comp = ET.SubElement(root, "Component")

        def attr(name: str, value: str | bool | None, _comp=comp) -> None:
            if value is None or value == "":
                return
            a = ET.SubElement(_comp, "Attribute")
            a.set("name", name)
            a.text = "Yes" if value is True else "No" if value is False else str(value)

        attr("Part_Number",  part.mpn)
        attr("MPN",          part.mpn)
        attr("Part_Type",    part.part_type)
        attr("Description",  part.description)
        attr("Manufacturer", part.manufacturer)
        attr("Reference",    part.reference_designator)
        attr("Value",        part.value)
        attr("Package",      part.package)
        attr("Schematic_Part", part.schematic_part or part.reference_designator)
        attr("Footprint",    part.footprint)
        attr("Resistance",   part.resistance)
        attr("Capacitance",  part.capacitance)
        attr("Inductance",   part.inductance)
        attr("Tolerance",    part.tolerance)
        attr("Voltage_Rating",     part.voltage_rating)
        attr("Current_Rating",     part.current_rating)
        attr("Power_Rating",       part.power_rating)
        attr("Frequency",          part.frequency)
        attr("Temperature_Min",    part.temp_min)
        attr("Temperature_Max",    part.temp_max)
        attr("Supply_Voltage_Min", part.supply_voltage_min)
        attr("Supply_Voltage_Max", part.supply_voltage_max)
        attr("Forward_Voltage",    part.forward_voltage)
        attr("Output_Current",     part.output_current)
        attr("Lifecycle",  part.lifecycle)
        attr("RoHS",       part.rohs_status)
        attr("REACH",      part.reach_status)
        attr("Datasheet_URL",       part.datasheet_url)
        attr("Source_Requirements", ", ".join(part.source_requirement_ids))
        attr("Selection_Rationale", part.selection_rationale)
        for k, v in (part.additional_specs or {}).items():
            attr(k, v)

    xml_bytes = ET.tostring(root, encoding="unicode", xml_declaration=False)
    dom = minidom.parseString(f'<?xml version="1.0" encoding="UTF-8"?>{xml_bytes}')
    return dom.toprettyxml(indent="  ", encoding=None)


# ════════════════════════════════════════════════════════════════════════════
# OrCAD OLB symbol library JSON generator
# ════════════════════════════════════════════════════════════════════════════

def parts_to_olb_json(parts: list[SelectedPart], library_name: str = "selected_parts") -> str:
    """
    Serialize selected parts to an OrCAD OLB library JSON.
    Format matches the OrCAD schematic symbol library structure.
    """
    olb_parts = []
    for part in parts:
        pins = [
            {
                "number":   p.number,
                "name":     p.name,
                "type":     p.type,
                "shape":    p.shape,
                "group":    p.group,
                "position": p.position,
            }
            for p in (part.pins or [])
        ]
        # Fall back to two generic passive pins if LLM produced none
        if not pins:
            pins = [
                {"number": "1", "name": "1", "type": "Passive",
                 "shape": "Line", "group": "G1", "position": "Left"},
                {"number": "2", "name": "2", "type": "Passive",
                 "shape": "Line", "group": "G1", "position": "Right"},
            ]
        olb_parts.append({
            "name":        part.part_name,
            "description": part.description,
            "reference":   part.reference_designator,
            "value":       part.value or part.mpn,
            "pins":        pins,
        })

    library = {
        "library": {
            "path": f"libraries/{library_name}.olb",
            "parts": olb_parts,
        }
    }
    return json.dumps(library, indent=2)


def part_to_single_json(part: SelectedPart) -> str:
    """
    Serialize one SelectedPart to the single-part JSON format used by Cadence OrCAD.
    Matches the MIC28516T.json example exactly:
      { "name", "num_sections", "prefix", "section_style", "mfg_pn", "partNumber",
        "manufacturer", "value", "description", "package_type", "pcb_footprint", "pins" }
    Output file is named {part.part_name}.json.
    """
    pins = []
    for p in (part.pins or []):
        pins.append({
            "number":   p.number,
            "name":     p.name,
            "type":     p.type.upper(),
            "visible":  p.visible,
            "shape":    p.shape.upper(),
            "group":    p.group,
            "position": p.position.upper(),
        })
    if not pins:
        pins = [
            {"number": "1", "name": "1", "type": "PASSIVE",
             "visible": True, "shape": "LINE", "group": "1", "position": "LEFT"},
            {"number": "2", "name": "2", "type": "PASSIVE",
             "visible": True, "shape": "LINE", "group": "1", "position": "RIGHT"},
        ]

    obj = {
        "name":          part.part_name,
        "num_sections":  part.num_sections,
        "prefix":        part.reference_designator,
        "section_style": part.section_style,
        "mfg_pn":        part.mpn,
        "partNumber":    part.mpn,
        "manufacturer":  part.manufacturer,
        "value":         part.value or part.part_name,
        "description":   part.description,
        "package_type":  part.package_type or (part.package or "").split("-")[0] or "",
        "pcb_footprint": part.footprint or part.package or "",
        "pins":          pins,
    }
    return json.dumps(obj, indent=2)


def parts_to_parts_json(parts: list[SelectedPart]) -> str:
    """
    Serialize selected parts to the flat parts-list JSON format used by Cadence
    OrCAD part import tools.  All type/shape/position values are UPPERCASE.
    """
    out = []
    for part in parts:
        safe_mpn = part.mpn.replace("/", "-").replace("\\", "-").replace(" ", "_")

        # Build pin list — UPPERCASE field values, include visible flag
        pins = []
        for p in (part.pins or []):
            pins.append({
                "number":   p.number,
                "name":     p.name,
                "type":     p.type.upper(),
                "visible":  p.visible,
                "shape":    p.shape.upper(),
                "group":    p.group,
                "position": p.position.upper(),
            })
        if not pins:
            pins = [
                {"number": "1", "name": "1", "type": "PASSIVE",
                 "visible": True, "shape": "LINE", "group": "1", "position": "LEFT"},
                {"number": "2", "name": "2", "type": "PASSIVE",
                 "visible": True, "shape": "LINE", "group": "1", "position": "RIGHT"},
            ]

        out.append({
            "name":          part.part_name,
            "num_sections":  part.num_sections,
            "prefix":        part.reference_designator,
            "section_style": part.section_style,
            "mfg_pn":        part.mpn,
            "partNumber":    part.mpn,
            "manufacturer":  part.manufacturer,
            "value":         part.value or part.mpn,
            "description":   part.description,
            "package_type":  part.package_type or (part.package or "").split("-")[0] or "",
            "pcb_footprint": part.footprint or part.package or "",
            "pins":          pins,
        })
    return json.dumps(out, indent=2)


# ════════════════════════════════════════════════════════════════════════════
# LLM agent
# ════════════════════════════════════════════════════════════════════════════

SELECTION_SYSTEM = """\
You are a senior hardware procurement and design engineer specialising in
component selection for embedded systems and electronic assemblies.

CRITICAL DATA PROVENANCE RULE
------------------------------
You MUST only reason from the requirements text and Silicon Expert datasheet data
supplied in this prompt. Do NOT introduce knowledge from training data, general
engineering judgment, or any source not present in the input.
- Every selected part must come from the datasheet data provided — do not invent
  or suggest parts not listed in the "AVAILABLE PARTS" section.
- Every attribute you populate (value, tolerance, voltage rating, pin names, etc.)
  must be traceable to a field in the provided datasheet JSON.
- If a requirement cannot be matched to any part in the provided datasheets, add
  its ID to unmatched_req_ids. Do not guess or substitute a similar part.
- selection_rationale must cite specific parametric values from the datasheet
  that satisfy specific requirement text — not general engineering reasoning.
- Do not add, modify, or interpolate requirement IDs.

Given:
  1. A list of engineering requirements (from a Cameo/SysML model)
  2. A set of Silicon Expert part datasheets (parametric data as JSON)

Your task is to select the best available part(s) from the provided datasheets
to satisfy the requirements.

Rules:
- Every requirement must be addressed. If no datasheet part can satisfy a
  requirement, add its ID to unmatched_req_ids and explain in notes.
- One part may satisfy multiple requirements — list all requirement IDs it
  covers in source_requirement_ids.
- Prefer Active lifecycle, RoHS-compliant parts.
- For each selected part, provide a concise but complete selection_rationale
  explaining why this specific part was chosen (key parameters that match).
- Populate every known electrical/mechanical attribute from the datasheet data.
- Use the standard reference designator prefix (R, C, L, U, Q, D, J, F, SW …).
- Keep part_type consistent with OrCAD CIS conventions:
    Passive     → resistors, capacitors, inductors, crystals
    IC          → microcontrollers, op-amps, drivers, regulators
    Connector   → connectors, sockets
    Mechanical  → washers, fasteners, heatsinks, structural parts
    Electrical  → diodes, transistors, MOSFETs, fuses
    Other       → anything else
- additional_specs: capture any important parametrics not already in the
  fixed fields (e.g. ESD rating, quiescent current, gain-bandwidth product).
- Be conservative — only select parts for which there is clear datasheet evidence.

For the 'pins' field on each selected part, generate OrCAD schematic symbol
pin definitions using these conventions:
  Passives (R, C, L):  pin 1 Left Passive, pin 2 Right Passive
  Diodes:              Anode (A) Left Passive, Cathode (K) Right Passive
  BJT:                 Base Left Input, Collector Top Output, Emitter Bottom Passive
  MOSFET:              Gate (G) Left Input, Drain (D) Top Output, Source (S) Bottom Passive
  Op-amp:              IN+ Left Input, IN- Left Input, OUT Right Output,
                       V+ Top Power, V- Bottom Power (group G1 for signal, G2 for power)
  Voltage regulator:   IN Left Input, OUT Right Output, GND/ADJ Bottom Power
  Microcontroller:     VCC/VDD Top Power, GND Bottom Power,
                       remaining signal pins Left Input or Right Output based on function
  Connector:           all pins are Passive, alternating Left/Right or sequential Left
  Crystal/oscillator:  1/IN Left Passive, 2/GND Bottom Power, 3/OUT Right Output,
                       4/VCC Top Power (if 4-pin)
  For ICs with many pins, use the number-of-pins parametric and distribute
  signal pins Left (inputs) and Right (outputs), power on Top, ground on Bottom.
  Always include at least one pin per part.
  Pin group is a numeric string: '1' for all pins on a single-section part.
  Set visible=false and shape='Short' for hidden GND/power pins (e.g. exposed pad).
  Set package_type to the base family without pin count ('TSSOP', 'SOT-23', 'QFN').
  Set num_sections to '2' for dual op-amps, '4' for quad gates, etc.
"""


def _make_openai_llm(model_name: str, api_key: str, base_url: str | None = None):
    try:
        from pydantic_ai.models.openai import OpenAIModel as _Model
    except ImportError:
        from pydantic_ai.models.openai import OpenAIChatModel as _Model  # type: ignore[no-redef]
    from pydantic_ai.providers.openai import OpenAIProvider as _Provider
    kwargs: dict = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return _Model(model_name, provider=_Provider(**kwargs))


def build_selection_agent(provider: str, api_key: str, model_name: str) -> Agent:
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
        llm = _make_openai_llm(model_name, api_key, base_url=GENESIS_BASE_URL)

    else:
        raise ValueError(f"Unknown provider '{provider}'. Choose 'anthropic', 'openai', or 'genesis'.")

    return Agent(llm, output_type=PartSelectionOutput, system_prompt=SELECTION_SYSTEM)


# ════════════════════════════════════════════════════════════════════════════
# Pipeline
# ════════════════════════════════════════════════════════════════════════════

def run_pipeline(
    selection_agent: Agent,
    istari: IstariCapability,
    model_name: str,
    model_id: str,
    dry_run: bool = False,
    requirements_id: str | None = None,
) -> PartSelectionOutput:
    """
    Run the part selection pipeline.

    Args:
        model_id: Istari model ID — scanned for both requirements and datasheet artifacts.
        requirements_id: Optional specific resource ID for requirements. If omitted,
            the agent auto-discovers the requirements.json artifact on model_id.
    """

    # ── Step 1: Fetch requirements ────────────────────────────────────────────
    if requirements_id:
        print(f"\n[1/4] Using pinned requirements resource (resource_id={requirements_id}) ...")
        print(f"      Datasheet artifacts will be discovered from model {model_id}")
    else:
        print(f"\n[1/4] Locating requirements artifact on model {model_id} ...")
        requirements_id = istari.find_requirements_resource_id(model_id)

    req_revision_id = istari.get_revision_id(requirements_id)
    all_reqs        = istari.fetch_requirements(requirements_id)
    actionable      = [r for r in all_reqs if r.is_actionable()]
    print(f"      revision_id={req_revision_id}")
    print(f"      {len(all_reqs)} total, {len(actionable)} with text")

    if not actionable:
        print("No actionable requirements — nothing to select.")
        return PartSelectionOutput(selected_parts=[], unmatched_req_ids=[])

    # ── Step 2: Discover SE datasheets on the same model ─────────────────────
    print(f"\n[2/4] Discovering Silicon Expert datasheet artifacts on model {model_id} ...")

    found = istari.find_datasheet_resource_ids(model_id)
    if not found:
        print(f"ERROR: no SE datasheet JSON artifacts found on model {model_id}.")
        print(f"       Expected artifact filenames containing one of: {DATASHEET_PATTERNS}")
        print(f"       Upload your SE datasheet JSON files as artifacts on this model and retry.")
        sys.exit(1)

    print(f"      Found {len(found)} datasheet artifact(s):")
    for fname, rid in found:
        print(f"      '{fname}'  (resource_id={rid})")

    # [(resource_id, label)] — used later for UUID provenance
    datasheet_resource_ids_used: list[tuple[str, str]] = list(found)

    # Load directly from the already-discovered resource IDs (no second model scan)
    datasheets = load_datasheets_from_istari(
        istari,
        model_id=None,
        resource_ids=[rid for rid, _ in found],
    )
    print(f"      {len(datasheets)} part datasheet(s) loaded")

    if not datasheets:
        print("ERROR: datasheet artifacts were found but could not be parsed.")
        sys.exit(1)

    # ── Step 3: LLM selection ─────────────────────────────────────────────────
    print(f"\n[3/4] Selecting parts ({model_name}) ...")

    req_block = "\n".join(
        f"[{r.display_id}] {r.text}" for r in actionable
    )
    ds_block = "\n\n".join(d.summary() for d in datasheets)

    prompt = (
        "REQUIREMENTS\n"
        "============\n"
        f"{req_block}\n\n"
        "AVAILABLE PARTS (Silicon Expert datasheets)\n"
        "===========================================\n"
        f"{ds_block}\n\n"
        "Select the best part(s) from the available datasheets to satisfy the requirements above."
    )

    result: PartSelectionOutput = selection_agent.run_sync(prompt).output

    print(f"      ✓ {len(result.selected_parts)} part(s) selected, "
          f"{len(result.unmatched_req_ids)} requirement(s) unmatched")
    for p in result.selected_parts:
        print(f"      {p.mpn}  ({p.manufacturer})  covers={p.source_requirement_ids}")
    if result.unmatched_req_ids:
        print(f"      unmatched: {result.unmatched_req_ids}")
    if result.notes:
        print(f"      notes: {result.notes}")

    # ── Step 4: Generate XML + upload ─────────────────────────────────────────
    print(f"\n[4/4] Generating OrCAD Capture CIS XML and uploading ...")

    local_out = HERE / "output"
    local_out.mkdir(exist_ok=True)

    uploaded: list[dict] = []

    # Individual per-part files: {part_name}.json (Cadence format) + {part_name}.cis.xml
    for part in result.selected_parts:
        safe_name = part.part_name.replace("/", "-").replace("\\", "-").replace(" ", "_")

        # --- {part_name}.json  (matches MIC28516T.json example format) ---
        part_json_fname   = f"{safe_name}.json"
        part_json_content = part_to_single_json(part)
        (local_out / part_json_fname).write_text(part_json_content, encoding="utf-8")

        if dry_run:
            print(f"  [dry-run] {part_json_fname} written locally")
        else:
            tmp = Path(tempfile.gettempdir()) / part_json_fname
            tmp.write_text(part_json_content, encoding="utf-8")
            resource = istari.upload_resource(
                tmp, part_json_fname,
                f"OrCAD part JSON [{part.mpn}] — pydantic-ai selector agent",
            )
            tmp.unlink(missing_ok=True)
            istari.link_resources(req_revision_id, resource.file_revision_id)
            uploaded.append({
                "mpn":         part.mpn,
                "file":        part_json_fname,
                "resource_id": resource.resource_id,
                "revision_id": resource.file_revision_id,
                "req_ids":     part.source_requirement_ids,
            })
            print(f"  ✓ {part_json_fname}")
            print(f"      resource_id={resource.resource_id}")
            print(f"      linked: {req_revision_id[:8]}… --[produces]--> {resource.file_revision_id[:8]}…")

        # --- {part_name}.cis.xml ---
        cis_fname   = f"{safe_name}.cis.xml"
        xml_content = part_to_cis_xml(part)
        (local_out / cis_fname).write_text(xml_content, encoding="utf-8")

        if dry_run:
            print(f"  [dry-run] {cis_fname} written locally")
        else:
            tmp = Path(tempfile.gettempdir()) / cis_fname
            tmp.write_text(xml_content, encoding="utf-8")
            resource = istari.upload_resource(
                tmp, cis_fname,
                f"OrCAD CIS part [{part.mpn}] — pydantic-ai selector agent",
            )
            tmp.unlink(missing_ok=True)
            istari.link_resources(req_revision_id, resource.file_revision_id)
            uploaded.append({
                "mpn":         part.mpn,
                "file":        cis_fname,
                "resource_id": resource.resource_id,
                "revision_id": resource.file_revision_id,
                "req_ids":     part.source_requirement_ids,
            })
            print(f"  ✓ {cis_fname}")
            print(f"      resource_id={resource.resource_id}")
            print(f"      linked: {req_revision_id[:8]}… --[produces]--> {resource.file_revision_id[:8]}…")

    # OLB symbol library JSON
    olb_fname = "selected_parts.olb.json"
    olb_content = parts_to_olb_json(result.selected_parts)
    (local_out / olb_fname).write_text(olb_content, encoding="utf-8")

    if dry_run:
        print(f"  [dry-run] {olb_fname} written locally")
    else:
        tmp = Path(tempfile.gettempdir()) / olb_fname
        tmp.write_text(olb_content, encoding="utf-8")
        resource = istari.upload_resource(
            tmp, olb_fname,
            "OrCAD OLB symbol library — selected parts",
        )
        tmp.unlink(missing_ok=True)
        istari.link_resources(req_revision_id, resource.file_revision_id)
        uploaded.append({
            "mpn":         "OLB_library",
            "file":        olb_fname,
            "resource_id": resource.resource_id,
            "revision_id": resource.file_revision_id,
            "req_ids":     [],
        })
        print(f"  ✓ {olb_fname}  →  resource_id={resource.resource_id}")

    # Parts list JSON (flat array format for Cadence OrCAD import)
    parts_fname = "selected_parts.json"
    parts_content = parts_to_parts_json(result.selected_parts)
    (local_out / parts_fname).write_text(parts_content, encoding="utf-8")

    if dry_run:
        print(f"  [dry-run] {parts_fname} written locally")
    else:
        tmp = Path(tempfile.gettempdir()) / parts_fname
        tmp.write_text(parts_content, encoding="utf-8")
        resource = istari.upload_resource(
            tmp, parts_fname,
            "OrCAD parts list JSON — selected parts",
        )
        tmp.unlink(missing_ok=True)
        istari.link_resources(req_revision_id, resource.file_revision_id)
        uploaded.append({
            "mpn":         "parts_list",
            "file":        parts_fname,
            "resource_id": resource.resource_id,
            "revision_id": resource.file_revision_id,
            "req_ids":     [],
        })
        print(f"  ✓ {parts_fname}  →  resource_id={resource.resource_id}")

    # Bundle XML (all parts in one file)
    bundle_fname = "selected_parts_bundle.cis.xml"
    bundle_xml = parts_to_cis_xml_bundle(result.selected_parts)
    (local_out / bundle_fname).write_text(bundle_xml, encoding="utf-8")

    if dry_run:
        print(f"  [dry-run] {bundle_fname} written locally")
    else:
        tmp = Path(tempfile.gettempdir()) / bundle_fname
        tmp.write_text(bundle_xml, encoding="utf-8")
        resource = istari.upload_resource(
            tmp, bundle_fname,
            "OrCAD CIS part bundle — all selected parts",
        )
        tmp.unlink(missing_ok=True)
        istari.link_resources(req_revision_id, resource.file_revision_id)
        uploaded.append({
            "mpn":         "bundle",
            "file":        bundle_fname,
            "resource_id": resource.resource_id,
            "revision_id": resource.file_revision_id,
            "req_ids":     [],
        })
        print(f"  ✓ {bundle_fname}  →  resource_id={resource.resource_id}")

    # ── UUID provenance record ────────────────────────────────────────────────
    uuid_provenance: list[dict] = []

    uuid_provenance.append({
        "uuid":  model_id,
        "role":  "source_model",
        "type":  "model",
        "label": "Istari model containing requirements and datasheet artifacts",
    })

    uuid_provenance.append({
        "uuid":  requirements_id,
        "role":  "requirements_source",
        "type":  "resource",
        "label": istari.get_resource_name(requirements_id) or "requirements.json",
    })
    uuid_provenance.append({
        "uuid":  req_revision_id,
        "role":  "requirements_revision",
        "type":  "revision",
        "label": "requirements.json revision used for LLM reasoning",
    })

    for rid, label in datasheet_resource_ids_used:
        uuid_provenance.append({
            "uuid":  rid,
            "role":  "datasheet_input",
            "type":  "resource",
            "label": label,
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

    # Summary JSON — written first with full provenance, then uploaded last
    # so the summary's own resource_id can be appended to provenance and the
    # local file updated before the run completes.
    summary_fname = "part_selection_summary.json"

    def _build_summary(extra_provenance: list[dict] | None = None) -> dict:
        return {
            "requirements_source":   requirements_id,
            "requirements_revision": req_revision_id,
            "model":                 model_name,
            "total_requirements":    len(actionable),
            "parts_selected":        len(result.selected_parts),
            "unmatched_req_ids":     result.unmatched_req_ids,
            "notes":                 result.notes,
            "outputs": uploaded if not dry_run else [
                {"mpn": p.mpn, "file": f"{p.part_name}.json"}
                for p in result.selected_parts
            ],
            "uuid_provenance": uuid_provenance + (extra_provenance or []),
        }

    # Write preliminary summary (without its own resource_id — not known yet)
    (local_out / summary_fname).write_text(json.dumps(_build_summary(), indent=2))

    if not dry_run:
        tmp = Path(tempfile.gettempdir()) / summary_fname
        tmp.write_text(json.dumps(_build_summary(), indent=2))
        resource = istari.upload_resource(tmp, summary_fname, "Part Selector Agent run summary")
        tmp.unlink(missing_ok=True)
        istari.link_resources(req_revision_id, resource.file_revision_id)
        print(f"  ✓ {summary_fname}  →  resource_id={resource.resource_id}")

        # Now that we know the summary's own IDs, add them to provenance and
        # rewrite the local file so it is self-documenting.
        summary_provenance = [
            {
                "uuid":  resource.resource_id,
                "role":  "summary_resource",
                "type":  "resource",
                "label": summary_fname,
            },
            {
                "uuid":  resource.file_revision_id,
                "role":  "summary_revision",
                "type":  "revision",
                "label": f"{summary_fname} (revision)",
            },
        ]
        uuid_provenance.extend(summary_provenance)
        final_summary = _build_summary()  # uuid_provenance now includes summary IDs
        (local_out / summary_fname).write_text(json.dumps(final_summary, indent=2))

    # Print summary
    print(f"\n{'─'*60}")
    print(f"Done. {len(result.selected_parts)} part(s) selected.")
    if result.unmatched_req_ids:
        print(f"Unmatched requirements: {result.unmatched_req_ids}")
    print(f"Local output: {local_out}/")

    if not dry_run and uploaded:
        print(f"\n{'═'*60}")
        print("Digital Thread")
        print(f"{'═'*60}")
        src_name = istari.get_resource_name(requirements_id) or requirements_id
        print(f"\n  {src_name}  (id={requirements_id})")
        print(f"  └─ produces:")
        # Include all uploaded + summary in thread view
        thread_entries = [
            e for e in uuid_provenance
            if e["role"] in ("output_resource", "summary_resource")
        ]
        for i, entry in enumerate(thread_entries):
            connector = "└─" if i == len(thread_entries) - 1 else "├─"
            print(f"     {connector} {entry['label']}  (id={entry['uuid']})")

    # ── UUID Provenance Summary ───────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print("UUID Provenance — Istari resources used in this run")
    print(f"{'═'*60}")
    col_w = 38
    print(f"  {'UUID':<{col_w}}  {'Role':<24}  Label")
    print(f"  {'-'*col_w}  {'-'*24}  {'-'*30}")
    for entry in uuid_provenance:
        uuid_str = entry["uuid"] or "N/A"
        print(f"  {uuid_str:<{col_w}}  {entry['role']:<24}  {entry['label']}")
    print(f"\n  All UUIDs also recorded in: {local_out / 'part_selection_summary.json'}")
    print(f"  (field: uuid_provenance)")

    return result


# ════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Istari Part Selector Agent — pydantic-ai, provider-agnostic.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Model / requirements source
    src = p.add_argument_group("Istari model (required)")
    src.add_argument("--model-id", metavar="UUID", required=True,
                     help=(
                         "Istari model ID. The agent scans this model for both the "
                         "requirements.json artifact and all SE datasheet JSON artifacts automatically."
                     ))
    src.add_argument("--requirements-id", metavar="UUID", default=None,
                     help=(
                         "Optional: pin a specific requirements resource ID instead of "
                         "auto-discovering it from --model-id. Datasheets are still "
                         "discovered from --model-id automatically."
                     ))

    # LLM
    llm = p.add_argument_group("LLM provider")
    llm.add_argument("--provider", choices=["anthropic", "openai", "genesis"], default="anthropic",
                     help="LLM provider (default: anthropic)")
    llm.add_argument("--api-key", default=None,
                     help="API key for the chosen provider")
    llm.add_argument("--model", default=None,
                     help="LLM model name (default: see PROVIDER_DEFAULTS)")

    # Istari
    auth = p.add_argument_group("Istari auth")
    auth.add_argument("--istari-url",   default=None, help="Istari registry URL")
    auth.add_argument("--istari-token", default=None, help="Istari Personal Access Token")

    # Misc
    p.add_argument("--env-file", default=None, metavar="PATH",
                   help="Path to a .env file with credentials (default: .env next to this script)")
    p.add_argument("--dry-run", action="store_true",
                   help="Select parts and write XML locally; skip Istari upload")
    return p.parse_args()


def _load_env(env_file: str | None) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("WARNING: python-dotenv not installed — .env file skipped.", file=sys.stderr)
        return
    path = Path(env_file) if env_file else HERE / ".env"
    if path.exists():
        load_dotenv(dotenv_path=path, override=False)
        print(f"  [config] Loaded credentials from {path}")
    elif env_file:
        print(f"ERROR: .env file not found: {path}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    args = parse_args()
    _load_env(args.env_file)

    env_key_name = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai":    "OPENAI_API_KEY",
        "genesis":   "GENESIS_API_KEY",
    }[args.provider]

    api_key      = args.api_key     or os.environ.get(env_key_name)
    istari_url   = args.istari_url  or os.environ.get("ISTARI_REGISTRY_URL")
    istari_token = args.istari_token or os.environ.get("ISTARI_REGISTRY_AUTH_TOKEN")
    model_name   = args.model       or PROVIDER_DEFAULTS[args.provider]

    missing = {k for k, v in {
        env_key_name:                 api_key,
        "ISTARI_REGISTRY_URL":        istari_url,
        "ISTARI_REGISTRY_AUTH_TOKEN": istari_token,
    }.items() if not v}
    if missing:
        for k in missing:
            print(f"ERROR: missing {k}", file=sys.stderr)
        sys.exit(1)

    istari = IstariCapability(registry_url=istari_url, pat=istari_token)
    agent  = build_selection_agent(args.provider, api_key, model_name)

    print("Istari Part Selector Agent")
    print("=" * 60)
    print(f"  Provider:    {args.provider}")
    print(f"  LLM model:   {model_name}")
    print(f"  Istari:      {istari.registry_url}")
    print(f"  Model:       {args.model_id}")
    if args.requirements_id:
        print(f"  Requirements ID (pinned): {args.requirements_id}")
    else:
        print(f"  Requirements: auto-discovered from model")
    print(f"  Datasheets:  auto-discovered from model")
    print(f"  Dry run:     {args.dry_run}")

    run_pipeline(
        selection_agent=agent,
        istari=istari,
        model_name=model_name,
        model_id=args.model_id,
        dry_run=args.dry_run,
        requirements_id=args.requirements_id,
    )


if __name__ == "__main__":
    main()
