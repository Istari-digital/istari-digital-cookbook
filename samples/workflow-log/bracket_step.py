"""Synthetic STEP (ISO-10303-21) files for the workflow-log walkthrough.

These aren't real solid models — they're well-formed STEP text whose single
design parameter, the mounting fillet radius, is embedded so the rest of the
demo can read it back and drive a physics-based verdict. A small fillet (R3)
concentrates stress and fails the battery; the larger R5 redesign passes.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

_TEMPLATE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('Mounting bracket - parametric'),'2;1');
FILE_NAME('bracket.step','{ts}',('engineer'),('Istari Digital'),
  'Open CASCADE','demo','');
FILE_SCHEMA(('AUTOMOTIVE_DESIGN {{ 1 0 10303 214 1 1 1 1 }}'));
ENDSEC;
DATA;
/* design parameter: fillet_radius_mm = {fillet:.1f} */
/* design parameter: wall_thickness_mm = 4.0 */
#1 = APPLICATION_CONTEXT('automotive design');
#2 = PRODUCT('bracket','Mounting bracket','',(#3));
#3 = PRODUCT_CONTEXT('',#1,'mechanical');
#10 = MANIFOLD_SOLID_BREP('bracket_solid',#11);
#11 = CLOSED_SHELL('',(#12));
#12 = ADVANCED_FACE('fillet_face',(),#13,.T.);
#13 = CYLINDRICAL_SURFACE('mounting_fillet',#14,{fillet:.1f});
#14 = AXIS2_PLACEMENT_3D('',#15,#16,#17);
#15 = CARTESIAN_POINT('',(0.0,0.0,0.0));
#16 = DIRECTION('',(0.0,0.0,1.0));
#17 = DIRECTION('',(1.0,0.0,0.0));
ENDSEC;
END-ISO-10303-21;
"""

_FILLET_RE = re.compile(r"fillet_radius_mm\s*=\s*([0-9]+(?:\.[0-9]+)?)")


def _render(fillet_mm: float) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return _TEMPLATE.format(ts=ts, fillet=fillet_mm)


def bad() -> str:
    """Initial design — R3 fillet, intentionally too small (fails stress)."""
    return _render(3.0)


def good() -> str:
    """Redesign — R5 fillet, relieves the stress concentration (passes)."""
    return _render(5.0)


def parse_fillet_mm(data) -> float:
    """Read the fillet radius back out of STEP bytes or text."""
    text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
    m = _FILLET_RE.search(text)
    if not m:
        raise ValueError("no fillet_radius_mm parameter found in STEP file")
    return float(m.group(1))
