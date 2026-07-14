"""Minimal CAD vs requirements validation for the cookbook notebook."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

_BETWEEN = re.compile(
    r"between\s+([-\d.]+)\s*(?:[a-zA-Z°]+)?\s+and\s+([-\d.]+)",
    re.I,
)
_BOUNDS = re.compile(r"\[\[\s*([-\d.]+)\s*;\s*([-\d.]+)\s*\]\]")


@dataclass
class Row:
    requirement: str
    bounds: str
    parameter_value: str
    passed: bool

    @property
    def test_name(self) -> str:
        return self.requirement.split("::")[-1]

    @property
    def failure_message(self) -> str:
        return f"value {self.parameter_value} not in bounds {self.bounds}"


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def fetch_artifact(platform: Any, job_id: str, artifact_name: str) -> Any:
    """Read a JSON product from a completed extraction job."""
    job = platform.get_job(job_id)
    product = job.find_product(filename=artifact_name) or job.find_product(name=artifact_name)
    if product is None:
        raise FileNotFoundError(
            f"{artifact_name!r} not on job {job_id}. "
            f"Products: {[p.filename or p.name for p in job.get_products()]}"
        )
    print(f"  {artifact_name!r} from job {job_id} — revision {product.revision_id}")
    return product.read_json()


def _bounds_from_text(text: str) -> tuple[float, float] | None:
    m = _BETWEEN.search(text) or _BOUNDS.search(text)
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def _bounded_requirements(data: list[dict]) -> list[dict]:
    out = []
    for item in data:
        if item.get("type") != "Requirement":
            continue
        text = (item.get("text") or (item.get("tags") or {}).get("Text") or "").strip()
        bounds = _bounds_from_text(text)
        if not bounds:
            continue
        qn = item.get("qualified_name", "")
        parts = qn.split("::")
        out.append(
            {
                "qualified_name": qn,
                "bounds": bounds,
                "bounds_str": f"[[{bounds[0]};{bounds[1]}]]",
                "leaf": parts[-1],
                "context": parts[-2] if len(parts) >= 2 else "",
            }
        )
    return out


def _parameters(data: list[dict]) -> list[dict]:
    return [
        {
            "name": p["name"],
            "leaf": p["name"].split("\\")[-1],
            "value": float(p["value"]),
            "unit": p.get("unit") or "",
        }
        for p in data
    ]


def validate(requirements: list[dict], parameters: list[dict]) -> list[Row]:
    """Comparable pairs only: one CAD param per bounded requirement."""
    rows: list[Row] = []
    for req in requirements:
        matches = [
            p
            for p in parameters
            if p["leaf"] == req["leaf"]
            and (not req["context"] or req["context"].upper() in p["name"].upper())
        ]
        if len(matches) != 1:
            continue
        p = matches[0]
        lo, hi = req["bounds"]
        passed = lo <= p["value"] <= hi
        rows.append(
            Row(
                requirement=req["qualified_name"],
                bounds=req["bounds_str"],
                parameter_value=f"{p['value']:g}{p['unit']}",
                passed=passed,
            )
        )
    return rows


def render_html(rows: list[Row], *, title: str = "Validation report") -> str:
    def tr(r: Row) -> str:
        ok = "pass" if r.passed else "fail"
        icon = "✓" if r.passed else "✗"
        return (
            f'<tr class="{ok}"><td class="status">{icon}</td>'
            f"<td>{r.requirement}</td><td><code>{r.bounds}</code></td>"
            f"<td><code>{r.parameter_value}</code></td></tr>"
        )

    body = "\n".join(tr(r) for r in rows) or (
        '<tr><td colspan="4">No comparable requirement / parameter pairs.</td></tr>'
    )
    n_fail = sum(1 for r in rows if not r.passed)
    summary = (
        f"<p class='pass'>All {len(rows)} checks passed.</p>"
        if rows and not n_fail
        else f"<p class='fail'>{n_fail} failed.</p>" if n_fail else ""
    )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"/>
<title>{title}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #d0d7de;padding:.5rem;text-align:left}}
tr.pass{{background:#dafbe1}} tr.fail{{background:#ffebe9}}
.status{{width:2rem;text-align:center;font-weight:bold}}
.pass{{color:#1a7f37}} .fail{{color:#cf222e}}
</style></head><body>
<h1>{title}</h1>{summary}
<table><thead><tr><th></th><th>Requirement</th><th>Bounds</th><th>Parameter value</th></tr></thead>
<tbody>{body}</tbody></table></body></html>"""


def run(requirements_data: list, parameters_data: list) -> list[Row]:
    """Parse extraction JSON and return validation rows."""
    return validate(_bounded_requirements(requirements_data), _parameters(parameters_data))


def render_junit_xml(
    rows: list[Row],
    *,
    suite_name: str = "CAD parameter validation",
    classname: str = "validation.cad_parameters",
) -> str:
    """Return a JUnit XML report string (same shape Istari previews in the web app)."""
    failures = sum(not r.passed for r in rows)
    cases = []
    for r in rows:
        body = (
            f'\n    <failure message="{escape(r.failure_message)}"/>\n  '
            if not r.passed
            else ""
        )
        cases.append(
            f'  <testcase classname="{escape(classname)}" '
            f'name="{escape(r.test_name)}">{body}</testcase>'
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuite name="{escape(suite_name)}" '
        f'tests="{len(rows)}" failures="{failures}" errors="0">\n'
        + "\n".join(cases)
        + "\n</testsuite>\n"
    )


def preview_junit_html(
    rows: list[Row],
    *,
    suite_name: str = "CAD parameter validation",
    xml_path: str | Path | None = None,
) -> str:
    """Notebook-friendly HTML summary of a JUnit report (mirrors the Istari web-app preview)."""
    failures = sum(not r.passed for r in rows)
    passed = len(rows) - failures
    verdict = "All checks passed" if rows and not failures else f"{failures} failed, {passed} passed"
    verdict_class = "pass" if rows and not failures else "fail"

    def tr(r: Row) -> str:
        cls = "pass" if r.passed else "fail"
        status = "Passed" if r.passed else "Failed"
        message = (
            ""
            if r.passed
            else f'<div class="message">{r.failure_message}</div>'
        )
        return (
            f'<tr class="{cls}"><td class="status">{status}</td>'
            f"<td><code>{r.test_name}</code></td>"
            f"<td>{r.requirement}</td>"
            f"<td><code>{r.bounds}</code></td>"
            f"<td><code>{r.parameter_value}</code></td>"
            f"<td>{message}</td></tr>"
        )

    body = "\n".join(tr(r) for r in rows) or (
        '<tr><td colspan="6">No comparable requirement / parameter pairs.</td></tr>'
    )
    saved = (
        f"<p class='meta'>Saved to <code>{Path(xml_path).name}</code> — "
        "upload as an artifact to preview with the Istari Digital web-app JUnit viewer.</p>"
        if xml_path
        else ""
    )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"/>
<title>{suite_name}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem}}
.meta{{color:#57606a;font-size:.9rem}}
.summary{{padding:.75rem 1rem;border-radius:6px;margin:1rem 0;font-weight:600}}
.summary.pass{{background:#dafbe1;color:#1a7f37}}
.summary.fail{{background:#ffebe9;color:#cf222e}}
table{{border-collapse:collapse;width:100%;font-size:.9rem}}
th,td{{border:1px solid #d0d7de;padding:.5rem;text-align:left;vertical-align:top}}
th{{background:#f6f8fa}}
tr.pass{{background:#f6ffed}}
tr.fail{{background:#fff5f5}}
.status{{font-weight:600;white-space:nowrap}}
tr.pass .status{{color:#1a7f37}}
tr.fail .status{{color:#cf222e}}
.message{{color:#cf222e;font-size:.85rem;margin-top:.25rem}}
</style></head><body>
<h1>{suite_name}</h1>
<p class="meta">{len(rows)} tests · {failures} failures · 0 errors</p>
<div class="summary {verdict_class}">{verdict}</div>
{saved}
<table><thead><tr>
<th>Status</th><th>Test</th><th>Requirement</th><th>Bounds</th><th>Value</th><th>Message</th>
</tr></thead><tbody>{body}</tbody></table>
</body></html>"""
