"""The demo's local verification battery — the work Istari never sees.

``run_battery`` runs ten named checks against the bracket geometry and writes
one artifact file per check. Every check is synthetic except for the spirit of
the **stress** check, which is genuinely driven by the parsed fillet radius:
a fillet concentrates stress as ~1/sqrt(r), so the R3 design exceeds the
allowable while the R5 redesign passes. All other checks pass for both designs.

``write_junit`` summarizes the battery as a JUnit XML report — the artifact a
real CI verification job would produce.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import bracket_step

# 6061-T6 aluminium, conservative allowable (MPa)
ALLOWABLE_STRESS_MPA = 276.0
# Stress-concentration model: peak = K / sqrt(fillet radius)
STRESS_K = 500.0


@dataclass
class Result:
    name: str
    passed: bool
    summary: str
    artifact_path: Path


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2))


def _thermal_map_png(path: Path, fillet_mm: float) -> None:
    """Render a synthetic steady-state thermal map of the bracket web."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    x, z = np.meshgrid(np.linspace(0, 80, 160), np.linspace(0, 60, 120))
    # hot spot at the mounting boss, decaying toward the fillet edge
    temp = 22.0 + 61.0 * np.exp(-(((x - 18) ** 2) / 350 + ((z - 42) ** 2) / 220))
    temp += 8.0 * np.exp(-(((x - 64) ** 2) / 500 + ((z - 12) ** 2) / 400))

    fig, ax = plt.subplots(figsize=(6, 4.2), dpi=110)
    im = ax.imshow(temp, origin="lower", extent=(0, 80, 0, 60), cmap="inferno")
    fig.colorbar(im, ax=ax, label="temperature (°C)")
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("z (mm)")
    ax.set_title(f"Bracket web — steady state, R{fillet_mm:.0f} fillet")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def run_battery(source_bytes: bytes, out_dir: Path) -> list[Result]:
    """Run the ten-check verification battery; one artifact file per check."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    r = bracket_step.parse_fillet_mm(source_bytes)
    peak = STRESS_K / math.sqrt(r)
    stress_ok = peak <= ALLOWABLE_STRESS_MPA
    mass = round(0.42 + 0.03 * r, 3)

    results: list[Result] = []

    def check(idx: int, name: str, passed: bool, summary: str,
              filename: str, payload: dict | None = None) -> None:
        path = out_dir / f"{idx:02d}_{filename}"
        if payload is not None:
            _write_json(path, {"check": name, "result": "PASS" if passed else "FAIL",
                               "summary": summary, **payload})
        results.append(Result(name=name, passed=passed, summary=summary,
                              artifact_path=path))

    check(1, "geometry_validation", True,
          "watertight BREP, no self-intersections",
          "geometry_validation.json",
          {"shells": 1, "open_edges": 0, "self_intersections": 0,
           "fillet_radius_mm": r})

    check(2, "mass_properties", True,
          f"mass {mass} kg within 0.30–0.60 kg budget",
          "mass_properties.json",
          {"mass_kg": mass, "budget_kg": [0.30, 0.60],
           "cg_mm": [31.2, 0.0, 24.8]})

    check(3, "modal_analysis", True,
          "first mode 412 Hz, above 250 Hz floor",
          "modal_analysis.json",
          {"modes_hz": [412.4, 688.1, 1039.6], "floor_hz": 250.0})

    # 04 — thermal map, rendered as a real PNG (displayed inline by the notebook)
    thermal_path = out_dir / "04_thermal_map.png"
    _thermal_map_png(thermal_path, r)
    results.append(Result(name="thermal_steady_state", passed=True,
                          summary="peak 83 °C, below 120 °C limit",
                          artifact_path=thermal_path))

    check(5, "stress_analysis", stress_ok,
          f"peak {peak:.1f} MPa vs allowable {ALLOWABLE_STRESS_MPA:.0f} MPa "
          f"(R{r:.0f} fillet)",
          "stress_analysis.json",
          {"fillet_radius_mm": r,
           "peak_stress_MPa": round(peak, 1),
           "allowable_MPa": ALLOWABLE_STRESS_MPA,
           "safety_factor": round(ALLOWABLE_STRESS_MPA / peak, 2),
           "location": "web-to-base fillet"})

    check(6, "fatigue_life", True,
          "2.1e6 cycles at limit load, above 1e6 requirement",
          "fatigue_life.json",
          {"cycles_to_failure": 2.1e6, "required_cycles": 1.0e6,
           "load_spectrum": "MIL-STD-810H, table 516.8-II"})

    check(7, "deflection", True,
          "tip deflection 0.42 mm, below 1.0 mm limit",
          "deflection.json",
          {"tip_deflection_mm": 0.42, "limit_mm": 1.0,
           "load_case": "100 N lateral at free flange"})

    check(8, "buckling", True,
          "buckling SF 4.7, above 2.0 requirement",
          "buckling.json",
          {"buckling_safety_factor": 4.7, "required": 2.0})

    check(9, "coating_coverage", True,
          "anodize coverage 99.6 %, above 98 % spec",
          "coating_coverage.json",
          {"coverage_pct": 99.6, "spec_pct": 98.0, "process": "MIL-A-8625 Type II"})

    check(10, "interface_fit", True,
          "all 4 bolt holes within H7 tolerance",
          "interface_fit.json",
          {"holes_checked": 4, "out_of_tolerance": 0, "fit_class": "H7"})

    return results


def write_junit(results: list[Result], path: Path, src_name: str) -> Path:
    """Write the battery results as a JUnit XML report; returns the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    failures = sum(not r.passed for r in results)
    cases = []
    for r in results:
        body = (
            f'\n    <failure message="{escape(r.summary)}"/>\n  '
            if not r.passed else ""
        )
        cases.append(
            f'  <testcase classname="verification.{escape(src_name)}" '
            f'name="{escape(r.name)}">{body}</testcase>'
        )

    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuite name="verification-battery [{escape(src_name)}]" '
        f'tests="{len(results)}" failures="{failures}" errors="0">\n'
        + "\n".join(cases)
        + "\n</testsuite>\n"
    )
    return path
