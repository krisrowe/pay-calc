#!/usr/bin/env python3
"""
Test YAML parser engine.

Tests that YAML-driven parsers correctly extract data from PDFs.
Uses synthetic fixtures from tests/fixtures/.
"""

import pytest
from pathlib import Path

from processors.engine import (
    get_parser_cache,
    extract_text_from_pdf,
    YAMLParser,
    YAMLProcessor,
    ParserCache,
)


FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def test_parser_cache():
    """Load test parsers from fixtures directory."""
    parsers_dir = FIXTURES_DIR
    cache = ParserCache(str(parsers_dir))
    cache.load_all()
    return cache


class TestParserCache:
    """Tests for parser cache loading."""

    def test_loads_yaml_parsers(self, test_parser_cache):
        """Test that parser cache loads YAML files."""
        parsers = test_parser_cache.get_all_parsers()
        assert len(parsers) > 0, "No parsers loaded"

    def test_parser_has_required_fields(self, test_parser_cache):
        """Test that loaded parsers have required fields."""
        parsers = test_parser_cache.get_all_parsers()
        for p in parsers:
            assert "type" in p, f"Parser missing 'type': {p.get('_source_file')}"
            assert "qualifier" in p, f"Parser missing 'qualifier': {p.get('_source_file')}"


class TestQualifierMatching:
    """Tests for qualifier pattern matching."""

    def test_acme_stub_qualifies(self, test_parser_cache):
        """Test that Acme stub text matches the acme parser."""
        # Text that should match acme_stub_parser.yaml
        text = """
        EARNINGS STATEMENT
        Pay Date: 2025-06-15
        Employer: Acme Corp
        """
        parser = test_parser_cache.find_matching_parser(text)
        assert parser is not None, "No parser matched Acme stub text"
        assert parser.get("type") == "stub"

    def test_non_matching_text_returns_none(self, test_parser_cache):
        """Test that unrecognized text returns no match."""
        text = "This is random text that should not match any parser"
        parser = test_parser_cache.find_matching_parser(text)
        assert parser is None, f"Unexpected match: {parser}"


class TestPDFExtraction:
    """Tests for PDF text extraction and parsing."""

    def test_extract_text_from_stub_pdf(self):
        """Test that text can be extracted from stub PDFs."""
        pdf_path = FIXTURES_DIR / "stub_2025-06-15.pdf"
        assert pdf_path.exists(), f"Fixture missing: {pdf_path}"

        text = extract_text_from_pdf(str(pdf_path))
        assert text, "No text extracted from PDF"
        assert "EARNINGS STATEMENT" in text or "Pay Date" in text

    def test_extract_text_from_image_pdf_returns_empty(self):
        """Test that image-based PDF returns empty text."""
        pdf_path = FIXTURES_DIR / "w2_image_based.pdf"
        assert pdf_path.exists(), f"Fixture missing: {pdf_path}"

        text = extract_text_from_pdf(str(pdf_path))
        assert text.strip() == "", f"Expected empty text from image PDF, got: {text[:50]}"

    def test_yaml_processor_extracts_stub_fields(self, test_parser_cache, monkeypatch):
        """Test that YAMLProcessor extracts expected fields from stub PDF."""
        import processors.engine as engine
        monkeypatch.setattr(engine, "get_parser_cache", lambda *a, **kw: test_parser_cache)

        pdf_path = FIXTURES_DIR / "stub_2025-06-15.pdf"
        result = YAMLProcessor.process(str(pdf_path), "Acme Corp")

        assert result is not None, "YAMLProcessor returned None"
        assert "pay_date" in result, f"Missing pay_date: {result}"
        assert result["pay_date"] == "2025-06-15"
