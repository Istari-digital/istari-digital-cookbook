"""Synthetic bracket STEP payloads for the workflow log walkthrough.

The demo does not need real CAD geometry — only a stable fillet parameter embedded
in the file so external checks can read it back after download from Istari.
"""

from __future__ import annotations

import re
from typing import Union

_FILLET_RE = re.compile(r"FILLET_RADIUS_MM\s*=\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_WALL_THICKNESS_MM = 3.0


def _step(fillet_radius_mm: float) -> str:
    return f"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('bracket demo'), '2;1');
FILE_NAME('bracket.step','{fillet_radius_mm:.1f}',(''),(''),'Istari workflow log demo','','');
FILE_SCHEMA(('CONFIG_CONTROL_DESIGN'));
ENDSEC;
DATA;
/* DESIGN_PARAM FILLET_RADIUS_MM={fillet_radius_mm:.1f} WALL_THICKNESS_MM={_WALL_THICKNESS_MM:.1f} */
ENDSEC;
END-ISO-10303-21;
"""


def bad() -> str:
    """Initial design — R3 fillet (stress check fails)."""
    return _step(3.0)


def good() -> str:
    """Redesign — R5 fillet (stress check passes)."""
    return _step(5.0)


def parse_fillet_mm(source: Union[str, bytes]) -> float:
    text = source.decode("utf-8", errors="replace") if isinstance(source, bytes) else source
    match = _FILLET_RE.search(text)
    if not match:
        raise ValueError("FILLET_RADIUS_MM not found in STEP payload")
    return float(match.group(1))


def wall_thickness_mm() -> float:
    return _WALL_THICKNESS_MM
