"""Parametric mounting-bracket STEP solids for the workflow-log walkthrough.

Geometry is a simple **L-bracket** (base + upright flange): plate
thickness and a circular mounting boss sized by fillet radius. Files are real
Open CASCADE STEP solids so FreeCAD / Open CAD ``@istari:extract`` can produce
a 3D view. Design parameters are also embedded in a ``DESIGN_PARAM`` comment
for the synthetic verification battery.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Union

import cadquery as cq

_FILLET_RE = re.compile(r"FILLET_RADIUS_MM\s*=\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_THICKNESS_RE = re.compile(r"WALL_THICKNESS_MM\s*=\s*(\d+(?:\.\d+)?)", re.IGNORECASE)

# Defaults for Scenario A (fail → revise → pass on fillet alone).
_DEFAULT_THICKNESS_MM = 3.0
_BAD_FILLET_MM = 3.0
_GOOD_FILLET_MM = 5.0

_BASE_W = 60.0
_BASE_D = 40.0
_HEIGHT = 50.0
_HOLE_DIAM_MM = 8.0
_BOSS_HEIGHT_MM = 3.0


def _solid(fillet_radius_mm: float, wall_thickness_mm: float) -> cq.Workplane:
    t = float(wall_thickness_mm)
    r = float(fillet_radius_mm)
    if t <= 0:
        raise ValueError("wall_thickness_mm must be positive")
    if r <= 0:
        raise ValueError("fillet_radius_mm must be positive")

    base = cq.Workplane("XY").box(_BASE_W, _BASE_D, t, centered=(True, True, False))
    upright = (
        cq.Workplane("XY")
        .transformed(offset=(0, -_BASE_D / 2 + t / 2, t))
        .box(_BASE_W, t, _HEIGHT, centered=(True, True, False))
    )
    solid = base.union(upright)

    # Inner knee fillet — clamped so Open CASCADE can build the blend.
    knee = min(r, 0.45 * t)
    if knee > 0.2:
        solid = solid.edges(
            cq.selectors.NearestToPointSelector((0, -_BASE_D / 2 + t, t))
        ).fillet(knee)

    # Mounting boss sized by fillet radius (visible in extract views).
    boss_r = max(r, _HOLE_DIAM_MM / 2 + 2.0)
    boss = (
        cq.Workplane("XY")
        .transformed(offset=(0, _BASE_D * 0.12, t))
        .circle(boss_r)
        .extrude(_BOSS_HEIGHT_MM)
    )
    solid = solid.union(boss)
    solid = (
        solid.faces(">Z")
        .workplane(centerOption="CenterOfMass")
        .hole(_HOLE_DIAM_MM)
    )
    return solid


def _embed_params(step_text: str, fillet_mm: float, thickness_mm: float) -> str:
    marker = "DATA;\n"
    note = (
        f"DATA;\n"
        f"/* DESIGN_PARAM FILLET_RADIUS_MM={fillet_mm:.1f} "
        f"WALL_THICKNESS_MM={thickness_mm:.1f} */\n"
        f"/* L-bracket mounting boss radius follows fillet; plate thickness "
        f"follows wall thickness */\n"
    )
    if marker not in step_text:
        return step_text + "\n" + note.replace("DATA;\n", "")
    return step_text.replace(marker, note, 1)


def write_bracket(
    path: Union[str, Path],
    fillet_radius_mm: float,
    wall_thickness_mm: float = _DEFAULT_THICKNESS_MM,
) -> Path:
    """Export a real STEP solid and embed design parameters for the demo checks."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    solid = _solid(fillet_radius_mm, wall_thickness_mm)
    # Export via a temp path so CadQuery always writes binary-clean text STEP.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / "bracket.step"
        cq.exporters.export(solid, str(tmp_path))
        text = tmp_path.read_text(encoding="utf-8", errors="replace")
    path.write_text(_embed_params(text, fillet_radius_mm, wall_thickness_mm), encoding="utf-8")
    return path


def render(fillet_radius_mm: float, wall_thickness_mm: float = _DEFAULT_THICKNESS_MM) -> str:
    """Return STEP text for *fillet_radius_mm* / *wall_thickness_mm*."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "bracket.step"
        write_bracket(p, fillet_radius_mm, wall_thickness_mm)
        return p.read_text(encoding="utf-8")


def bad() -> str:
    """Initial design — R3 fillet (stress check fails), default wall thickness."""
    return render(_BAD_FILLET_MM, _DEFAULT_THICKNESS_MM)


def good() -> str:
    """Redesign — R5 fillet (stress check passes), default wall thickness."""
    return render(_GOOD_FILLET_MM, _DEFAULT_THICKNESS_MM)


def parse_fillet_mm(source: Union[str, bytes]) -> float:
    text = source.decode("utf-8", errors="replace") if isinstance(source, bytes) else source
    match = _FILLET_RE.search(text)
    if not match:
        raise ValueError("FILLET_RADIUS_MM not found in STEP payload")
    return float(match.group(1))


def parse_wall_thickness_mm(source: Union[str, bytes]) -> float:
    text = source.decode("utf-8", errors="replace") if isinstance(source, bytes) else source
    match = _THICKNESS_RE.search(text)
    if not match:
        raise ValueError("WALL_THICKNESS_MM not found in STEP payload")
    return float(match.group(1))


def wall_thickness_mm() -> float:
    return _DEFAULT_THICKNESS_MM
