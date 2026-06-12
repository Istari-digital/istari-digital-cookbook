"""Requirement suite for the tradespace sweep (Scenario B).

A real pytest file. Each candidate's design point arrives via environment
variables (TS_FILLET_MM, TS_THICKNESS_MM); the suite recomputes the governing
metrics and asserts the three requirements. ``record_property`` writes the
design point and metrics into the JUnit report so the test artifact itself
carries the parameters it was run against.

Requirements (a candidate must satisfy all three):
  - stress:     safety factor >= 1.5
  - mass:       <= 1.05 kg
  - deflection: <= 2.5 mm
"""
import math
import os

import pytest

FILLET_MM = float(os.environ.get("TS_FILLET_MM", "3"))
THICKNESS_MM = float(os.environ.get("TS_THICKNESS_MM", "4"))

ALLOWABLE_STRESS_MPA = 276.0
SF_MIN = 1.5
MASS_MAX_KG = 1.05
DEFLECTION_MAX_MM = 2.5


def _metrics(fillet_mm: float, thickness_mm: float) -> dict:
    peak = 1500.0 / (math.sqrt(fillet_mm) * thickness_mm)
    return {
        "peak_stress_MPa": round(peak, 1),
        "safety_factor": round(ALLOWABLE_STRESS_MPA / peak, 3),
        "mass_kg": round(0.30 + 0.10 * thickness_mm + 0.03 * fillet_mm, 3),
        "deflection_mm": round(45.0 / (fillet_mm * thickness_mm), 3),
        "stiffness_index": round(fillet_mm * thickness_mm, 1),
    }


@pytest.fixture(autouse=True)
def _record_design_point(record_property):
    record_property("fillet_radius_mm", FILLET_MM)
    record_property("wall_thickness_mm", THICKNESS_MM)
    for k, v in _metrics(FILLET_MM, THICKNESS_MM).items():
        record_property(k, v)


def test_stress_safety_factor():
    m = _metrics(FILLET_MM, THICKNESS_MM)
    assert m["safety_factor"] >= SF_MIN, (
        f"safety factor {m['safety_factor']} < {SF_MIN} "
        f"(peak {m['peak_stress_MPa']} MPa)")


def test_mass_budget():
    m = _metrics(FILLET_MM, THICKNESS_MM)
    assert m["mass_kg"] <= MASS_MAX_KG, f"mass {m['mass_kg']} kg > {MASS_MAX_KG}"


def test_deflection_limit():
    m = _metrics(FILLET_MM, THICKNESS_MM)
    assert m["deflection_mm"] <= DEFLECTION_MAX_MM, (
        f"deflection {m['deflection_mm']} mm > {DEFLECTION_MAX_MM}")
