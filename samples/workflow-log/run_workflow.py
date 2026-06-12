"""A local verification battery — the work that happens *outside* Istari.

`run_battery` parses the bracket's fillet radius out of the STEP bytes, runs ten
checks, and writes a result artifact for each into ``out_dir``. The stress check
is physics-driven: peak stress scales as 1/sqrt(fillet), so the small R3 fillet
pushes peak stress over the allowable and fails, while R5 passes. Everything else
passes. `write_junit` renders the run as a JUnit XML report.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import matplotlib
matplotlib.use("Agg")  # headless — no display needed
import matplotlib.pyplot as plt
import numpy as np

import bracket_step

ALLOWABLE_STRESS_MPA = 276.0   # 6061-T6 yield
_STRESS_K = 550.0              # geometry/load constant for peak = K / sqrt(fillet)


@dataclass
class CheckResult:
    name: str
    passed: bool
    summary: str
    artifact_path: Path


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2))


def _thermal_map(path: Path, fillet_mm: float) -> None:
    """Render a synthetic steady-state thermal field as a heat map PNG."""
    n = 96
    y, x = np.mgrid[0:n, 0:n]
    # A hot spot near the loaded fillet; a tighter fillet runs slightly hotter.
    cx, cy = n * 0.62, n * 0.40
    r2 = (x - cx) ** 2 + (y - cy) ** 2
    peak_c = 78.0 + (5.0 - fillet_mm) * 3.0
    field = 24.0 + (peak_c - 24.0) * np.exp(-r2 / (2 * (n * 0.22) ** 2))
    field += np.random.default_rng(7).normal(0, 0.6, field.shape)

    fig, ax = plt.subplots(figsize=(4.2, 3.4))
    im = ax.imshow(field, cmap="inferno", origin="lower")
    ax.set_title(f"Thermal map — peak {field.max():.1f}°C")
    ax.set_xticks([])
    ax.set_yticks([])
    fig.colorbar(im, ax=ax, label="°C", fraction=0.046, pad=0.04)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def run_battery(source_bytes, out_dir) -> list[CheckResult]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fillet = bracket_step.parse_fillet_mm(source_bytes)

    peak_stress = _STRESS_K / math.sqrt(fillet)
    stress_ok = peak_stress <= ALLOWABLE_STRESS_MPA
    safety_factor = ALLOWABLE_STRESS_MPA / peak_stress
    mass_kg = 0.42 + 0.03 * fillet

    results: list[CheckResult] = []

    # 01 — mass properties
    p = out / "01_mass_properties.json"
    _write_json(p, {"check": "mass_properties", "mass_kg": round(mass_kg, 3),
                    "limit_kg": 1.20, "cg_mm": [12.4, 8.1, 5.0]})
    results.append(CheckResult("mass properties", mass_kg <= 1.20,
                               f"{mass_kg:.3f} kg (limit 1.20)", p))

    # 02 — geometry / interference
    p = out / "02_geometry_check.json"
    _write_json(p, {"check": "geometry", "watertight": True, "interferences": 0,
                    "min_wall_mm": 4.0})
    results.append(CheckResult("geometry check", True, "watertight, 0 interferences", p))

    # 03 — modal analysis
    p = out / "03_modal_analysis.json"
    f1 = 180.0 + 6.0 * fillet
    _write_json(p, {"check": "modal", "first_mode_hz": round(f1, 1), "min_hz": 150.0})
    results.append(CheckResult("modal analysis", f1 >= 150.0,
                               f"1st mode {f1:.0f} Hz (min 150)", p))

    # 04 — thermal (rendered heat map)
    p = out / "04_thermal_map.png"
    _thermal_map(p, fillet)
    results.append(CheckResult("thermal map", True, "peak under 95°C limit", p))

    # 05 — stress analysis (the physics-driven verdict)
    p = out / "05_stress_analysis.json"
    _write_json(p, {"check": "stress", "fillet_radius_mm": fillet,
                    "peak_stress_MPa": round(peak_stress, 1),
                    "allowable_MPa": ALLOWABLE_STRESS_MPA,
                    "safety_factor": round(safety_factor, 3),
                    "passed": stress_ok})
    results.append(CheckResult(
        "stress analysis", stress_ok,
        f"peak {peak_stress:.0f} MPa vs {ALLOWABLE_STRESS_MPA:.0f} allowable "
        f"(SF {safety_factor:.2f})", p))

    # 06 — fatigue life (driven by geometry, not the stress-margin verdict)
    p = out / "06_fatigue_life.json"
    cycles = 1.0e6 * (1.4 + 0.10 * fillet)
    _write_json(p, {"check": "fatigue", "predicted_cycles": int(cycles),
                    "required_cycles": 1_000_000})
    results.append(CheckResult("fatigue life", cycles >= 1_000_000,
                               f"{cycles/1e6:.2f}M cycles (req 1.0M)", p))

    # 07 — deflection
    p = out / "07_deflection.json"
    defl = 1.8 - 0.06 * fillet
    _write_json(p, {"check": "deflection", "max_deflection_mm": round(defl, 3),
                    "limit_mm": 2.0})
    results.append(CheckResult("deflection", defl <= 2.0,
                               f"{defl:.2f} mm (limit 2.0)", p))

    # 08 — buckling
    p = out / "08_buckling.json"
    _write_json(p, {"check": "buckling", "load_factor": 3.4, "min_factor": 2.0})
    results.append(CheckResult("buckling", True, "load factor 3.4 (min 2.0)", p))

    # 09 — tolerance stackup
    p = out / "09_tolerance_stackup.json"
    _write_json(p, {"check": "tolerance_stackup", "worst_case_mm": 0.12, "budget_mm": 0.20})
    results.append(CheckResult("tolerance stackup", True, "0.12 mm (budget 0.20)", p))

    # 10 — material compliance
    p = out / "10_material_compliance.json"
    _write_json(p, {"check": "material_compliance", "alloy": "6061-T6",
                    "rohs": True, "reach": True})
    results.append(CheckResult("material compliance", True, "6061-T6, RoHS+REACH ok", p))

    return results


def write_junit(results: list[CheckResult], path, src_name: str) -> Path:
    path = Path(path)
    n_fail = sum(not r.passed for r in results)
    lines = ['<?xml version="1.0" encoding="utf-8"?>']
    lines.append(
        f'<testsuite name="verification_battery" tests="{len(results)}" '
        f'failures="{n_fail}" errors="0" file="{escape(src_name)}">')
    for r in results:
        lines.append(f'  <testcase classname="battery" name="{escape(r.name)}">')
        if not r.passed:
            lines.append(f'    <failure message="{escape(r.summary)}">'
                         f'{escape(r.name)} failed: {escape(r.summary)}</failure>')
        else:
            lines.append(f'    <system-out>{escape(r.summary)}</system-out>')
        lines.append('  </testcase>')
    lines.append('</testsuite>')
    path.write_text("\n".join(lines))
    return path
