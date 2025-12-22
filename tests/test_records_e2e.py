"""End-to-end tests for records import → list → show workflow.

Tests the full lifecycle using production code with synthetic inputs:
- Real PDF reading (PyPDF2)
- Real text extraction
- Real YAML parser loading and matching
- Real validation and storage

Only network I/O (Gemini OCR) is stubbed.

Test scenario:
- 5 PDFs in temp folder:
  - 2 pay stubs (text-parseable, match acme_stub_parser.yaml)
  - 1 W-2 (triggers OCR fallback - mocked)
  - 1 W-4 (no matching parser - discarded)
  - 1 image-based W-2 (no extractable text, direct OCR)

See tests/fixtures/README.md for design philosophy.
"""

import json
import shutil
import pytest
from pathlib import Path
from unittest.mock import patch
import yaml


# --- Fixtures Directory ---

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Expected test outcomes
EXPECTED_STUBS = 2  # stub_2025-06-15.pdf, stub_2025-06-30.pdf
EXPECTED_W2S = 2    # w2_2024.pdf + w2_image_based.pdf (both via mocked OCR)
EXPECTED_DISCARDED = 1  # w4_2025.pdf

# Mock W-2 data returned by Gemini OCR
W2_2024_OCR_RESULT = {
    "tax_year": 2024,
    "employer_name": "Acme Corp",
    "employer_ein": "12-3456789",
    "wages": 120000.00,
    "federal_tax_withheld": 18000.00,
    "social_security_wages": 120000.00,
    "social_security_tax": 7440.00,
    "medicare_wages": 120000.00,
    "medicare_tax": 1740.00,
}

# Different tax year for image-based W-2 to avoid content-based ID collision
W2_IMAGE_OCR_RESULT = {
    "tax_year": 2023,
    "employer_name": "Acme Corp",
    "employer_ein": "12-3456789",
    "wages": 110000.00,
    "federal_tax_withheld": 16500.00,
    "social_security_wages": 110000.00,
    "social_security_tax": 6820.00,
    "medicare_wages": 110000.00,
    "medicare_tax": 1595.00,
}


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """Set up isolated environment with synthetic fixtures.

    Copies fixtures to temp directory and configures environment.
    Production code runs unchanged - only inputs are synthetic.
    """
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    pdf_dir = tmp_path / "pdfs"
    parsers_dir = config_dir / "parsers"

    config_dir.mkdir()
    data_dir.mkdir()
    pdf_dir.mkdir()
    parsers_dir.mkdir()

    # Point production code to temp directories
    monkeypatch.setenv("PAY_CALC_CONFIG_PATH", str(config_dir))

    # Settings - tells code where to store records
    settings = {"data_dir": str(data_dir)}
    (config_dir / "settings.json").write_text(json.dumps(settings))

    # Profile - defines Acme Corp employer for party detection
    profile = {
        "parties": {
            "him": {
                "companies": [
                    {"name": "Acme Corp", "keywords": ["Acme", "Acme Corp"]}
                ]
            }
        }
    }
    (config_dir / "profile.yaml").write_text(yaml.dump(profile))

    # Copy YAML parser from fixtures - production code will load it
    shutil.copy(FIXTURES_DIR / "acme_stub_parser.yaml", parsers_dir / "acme.yaml")

    # Copy PDF fixtures
    for pdf in FIXTURES_DIR.glob("*.pdf"):
        shutil.copy(pdf, pdf_dir / pdf.name)

    # Production code loads parsers from "parsers/" relative to CWD
    # Reset global cache and patch get_parser_cache to use test parsers
    import processors.engine as parser_engine
    parser_engine._parser_cache = None
    test_cache = parser_engine.ParserCache(str(parsers_dir))
    test_cache.load_all()
    monkeypatch.setattr(parser_engine, "get_parser_cache", lambda parsers_dir="parsers/": test_cache)

    return {
        "config_dir": config_dir,
        "data_dir": data_dir,
        "pdf_dir": pdf_dir,
        "parsers_dir": parsers_dir,
    }


@pytest.fixture
def mock_gemini():
    """Stub Gemini to prevent network I/O and track OCR code paths.

    This is the ONLY mock - everything else runs as production code.
    Returns W-2 data for W-2 files, None for others.

    Tracks both:
    - gemini_calls: Files sent to OCR
    - text_parse_attempts: Files where YAML text parsing was attempted
    """
    gemini_calls = []
    text_parse_attempts = []

    def mock_process_file(prompt: str, file_path: str, timeout: int = 120) -> dict:
        gemini_calls.append({"file": file_path, "prompt": prompt})
        # Return W-2 data for W-2 files (different data to avoid ID collision)
        if "w2_image" in str(file_path).lower():
            return W2_IMAGE_OCR_RESULT
        elif "w2" in str(file_path).lower():
            return W2_2024_OCR_RESULT
        return None

    # Wrap _parse_text_pdf_auto to track calls without changing behavior
    from paycalc.sdk import records as records_module
    original_parse = records_module._parse_text_pdf_auto

    def tracking_parse(text, pdf_path):
        text_parse_attempts.append({"file": str(pdf_path), "text_len": len(text)})
        return original_parse(text, pdf_path)

    with patch("paycalc.gemini_client.process_file", mock_process_file):
        with patch.object(records_module, "_parse_text_pdf_auto", tracking_parse):
            yield {
                "calls": gemini_calls,
                "text_parse_attempts": text_parse_attempts
            }


class TestRecordsE2E:
    """End-to-end tests for import → list → show workflow."""

    def test_list_before_import_returns_zero(self, test_env, mock_gemini):
        """Verify list returns 0 records before any import."""
        from paycalc.sdk import records

        result = records.list_records()
        assert len(result) == 0, f"Expected 0 records before import, got {len(result)}"

    def test_import_and_list_counts(self, test_env, mock_gemini):
        """Import 4 files, verify list returns correct counts by type."""
        from paycalc.sdk import records

        # Import all PDFs from folder
        stats = records.import_from_folder_auto(str(test_env["pdf_dir"]))

        # Verify import stats
        assert stats.get("imported", 0) > 0, f"Expected some imports, got {stats}"

        # List all records
        all_records = records.list_records()
        assert len(all_records) == stats["imported"], \
            f"list_records() found {len(all_records)} but import reported {stats['imported']}"

        # List by type
        stubs = records.list_records(type_filter="stub")
        w2s = records.list_records(type_filter="w2")

        # Verify type counts
        assert len(stubs) == EXPECTED_STUBS, f"Expected {EXPECTED_STUBS} stubs, got {len(stubs)}"
        assert len(w2s) == EXPECTED_W2S, f"Expected {EXPECTED_W2S} W-2s, got {len(w2s)}"
        assert len(stubs) + len(w2s) == len(all_records)

    def test_gemini_invoked_for_ocr_fallback(self, test_env, mock_gemini):
        """Verify Gemini OCR is called when text parsing fails."""
        from paycalc.sdk import records

        records.import_from_folder_auto(str(test_env["pdf_dir"]))

        # Gemini should have been called for W-2 and W-4 (text parsing fails)
        assert len(mock_gemini["calls"]) >= 1, \
            f"Expected Gemini to be called for OCR fallback, got 0 calls"

    def test_show_record_has_expected_fields(self, test_env, mock_gemini):
        """Verify imported records have expected data fields."""
        from paycalc.sdk import records

        records.import_from_folder_auto(str(test_env["pdf_dir"]))

        # Get all records
        all_records = records.list_records()
        assert len(all_records) > 0, "No records found after import"

        # Each record should have meta and data
        for record in all_records:
            assert "meta" in record, f"Record missing 'meta': {record}"
            assert "data" in record, f"Record missing 'data': {record}"
            assert "id" in record, f"Record missing 'id': {record}"

            meta = record["meta"]
            assert meta.get("type") in ("stub", "w2"), f"Unexpected type: {meta}"
            assert meta.get("year"), f"Record missing year: {meta}"
            assert meta.get("party"), f"Record missing party: {meta}"

    def test_image_based_pdf_triggers_direct_ocr(self, test_env, mock_gemini):
        """Verify image-based PDFs with no extractable text go directly to OCR.

        The w2_image_based.pdf fixture contains only an image - no text layer.
        Text extraction yields empty string, so YAML parsers are skipped entirely
        and Gemini OCR is invoked immediately.

        Contrast with text-based W-2 (w2_2024.pdf) which has extractable text,
        attempts YAML parsing (fails), then falls back to OCR.
        """
        from paycalc.sdk import records

        # Import just the image-based PDF
        image_pdf = test_env["pdf_dir"] / "w2_image_based.pdf"
        assert image_pdf.exists(), f"Image-based PDF fixture missing: {image_pdf}"

        # Import all PDFs
        stats = records.import_from_folder_auto(str(test_env["pdf_dir"]))

        # Verify image-based PDF went to Gemini
        image_ocr_calls = [
            c for c in mock_gemini["calls"]
            if "w2_image_based" in str(c["file"])
        ]
        assert len(image_ocr_calls) >= 1, \
            f"Expected Gemini OCR call for image-based PDF, got: {mock_gemini['calls']}"

        # Verify image-based PDF did NOT attempt text parsing (no extractable text)
        image_parse_attempts = [
            a for a in mock_gemini["text_parse_attempts"]
            if "w2_image_based" in str(a["file"])
        ]
        assert len(image_parse_attempts) == 0, \
            f"Image-based PDF should NOT attempt text parsing, but did: {image_parse_attempts}"

    def test_text_w2_attempts_parsing_before_ocr_fallback(self, test_env, mock_gemini):
        """Verify text-based W-2 attempts YAML parsing before OCR fallback.

        The w2_2024.pdf has extractable text but no matching YAML parser.
        It should:
        1. Extract text successfully
        2. Attempt text parsing (which fails - no W-2 parser)
        3. Fall back to Gemini OCR

        This is different from image-based PDFs which skip step 2 entirely.
        """
        from paycalc.sdk import records

        records.import_from_folder_auto(str(test_env["pdf_dir"]))

        # Find text-based W-2 (not image-based)
        text_w2_parse_attempts = [
            a for a in mock_gemini["text_parse_attempts"]
            if "w2_2024" in str(a["file"])
        ]

        # Text-based W-2 SHOULD attempt parsing (has extractable text)
        assert len(text_w2_parse_attempts) >= 1, \
            f"Text-based W-2 should attempt text parsing, but didn't: {mock_gemini['text_parse_attempts']}"

        # And it SHOULD also go to Gemini (parsing fails, falls back)
        text_w2_ocr_calls = [
            c for c in mock_gemini["calls"]
            if "w2_2024" in str(c["file"])
        ]
        assert len(text_w2_ocr_calls) >= 1, \
            f"Text-based W-2 should fall back to OCR, got: {mock_gemini['calls']}"
