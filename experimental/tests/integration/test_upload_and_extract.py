"""Integration tests: upload a spreadsheet and run an extraction job.

Requires a live Istari platform. Set credentials in .env:

    ISTARI_PAT=your_token
    ISTARI_ENVIRONMENT_URL=https://fileservice-v2.demo.istari.app

Run with:

    uv run pytest tests/integration/ -v -m integration
"""

from pathlib import Path

import pytest

from istari_experimental import IstariPlatform, JobDefinition

pytestmark = pytest.mark.integration

XLSX_FILE = Path(__file__).parent / "Group3-UAS-Requirements.xlsx"

# Products written by open_spreadsheet @istari:extract on this file
EXPECTED_PRODUCTS = {
    "named_cells.json",
    "worksheet_data.json",
    "workbook.pdf",
    "workbook.html",
}


def test_platform_connects(platform: IstariPlatform):
    """Verify the client can reach the platform."""
    assert repr(platform).startswith("IstariPlatform")


def test_upload_xlsx(platform: IstariPlatform):
    """Upload the UAS requirements spreadsheet and verify the model is created."""
    model = platform.upload_model(
        XLSX_FILE,
        external_id="fluent-client-test-uas-requirements",
        display_name="Group3-UAS-Requirements (fluent client test)",
    )
    assert model.id
    assert model.name


def test_upload_extract_and_download_product(platform: IstariPlatform, tmp_path: Path):
    """Full end-to-end: upload -> extract -> check products -> download one."""
    model = platform.upload_model(
        XLSX_FILE,
        external_id="fluent-client-test-uas-e2e",
        display_name="Group3-UAS-Requirements (fluent client e2e)",
    )

    job = model.run_job(
        JobDefinition(
            function="@istari:extract",
            tool_name="open_spreadsheet",
        ),
        timeout=300,
    )

    # Race-safe: each Product points to the exact FileRevision the agent wrote.
    products = job.get_products()
    product_names = {p.name for p in products}

    missing = EXPECTED_PRODUCTS - product_names
    assert not missing, f"Missing products: {missing}. Got: {product_names}"

    import json

    named_cells = job.find_product(name="named_cells.json")
    assert named_cells is not None
    assert named_cells.revision.id  # exact revision pointer is preserved

    data = json.loads(named_cells.read_text())
    assert isinstance(data, dict)
    assert len(data) > 0

    dest = named_cells.download(tmp_path)
    assert dest.exists()
    assert dest.stat().st_size > 0
