"""Pytest requirement suite for the tradespace sweep (Scenario B)."""

from __future__ import annotations

import math
import os

import pytest

ALLOWABLE_STRESS_MPA = 276.0
MASS_LIMIT_KG = 0.90
STIFFNESS_MIN = 12.0


@pytest.fixture
def design_point():
    fillet = float(os.environ["TS_FILLET_MM"])
    thickness = float(os.environ["TS_THICKNESS_MM"])
    peak = 1500.0 / (math.sqrt(fillet) * thickness)
    return {
        "fillet_radius_mm": fillet,
        "wall_thickness_mm": thickness,
        "peak_stress_MPa": peak,
        "safety_factor": 276.0 / peak,
        "mass_kg": 0.30 + 0.10 * thickness + 0.03 * fillet,
        "stiffness_index": fillet * thickness,
    }


def test_stress_margin(design_point, record_property):
    record_property("fillet_radius_mm", design_point["fillet_radius_mm"])
    record_property("wall_thickness_mm", design_point["wall_thickness_mm"])
    assert design_point["peak_stress_MPa"] <= ALLOWABLE_STRESS_MPA


def test_mass_budget(design_point):
    assert design_point["mass_kg"] <= MASS_LIMIT_KG


def test_stiffness(design_point):
    assert design_point["stiffness_index"] >= STIFFNESS_MIN
