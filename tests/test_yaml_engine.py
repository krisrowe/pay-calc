#!/usr/bin/env python3
"""
Test YAML parser engine.

Tests that YAML-driven parsers correctly extract data from PDFs.
"""

import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from processors.engine import (
    get_parser_cache,
    extract_text_from_pdf,
    YAMLParser,
    YAMLProcessor,
)


def test_parser_cache():
    """Test that parser cache loads YAML files."""
    cache = get_parser_cache("parsers/")
    cache.load_all()

    parsers = cache.get_all_parsers()
    print(f"Loaded {len(parsers)} parser(s)")

    for p in parsers:
        print(f"  - {p.get('_source_file')}: type={p.get('type')}")

    return len(parsers) > 0


def test_qualifier_matching():
    """Test that qualifier patterns match expected text."""
    cache = get_parser_cache("parsers/")

    # Sample text patterns that should match different parsers
    test_cases = [
        # (text_sample, expected_type)
        ("Document 123456 Pay Summary Gross", "stub"),  # Format A
        ("Check Number: 12345 Pay Date: 01/15/2024", "stub"),  # Format B
    ]

    results = []
    for text, expected_type in test_cases:
        parser = cache.find_matching_parser(text)
        if parser:
            matched_type = parser.get("type")
            success = matched_type == expected_type
            results.append(success)
            status = "✓" if success else "✗"
            print(f"  Text '{text[:40]}...' -> {parser.get('_source_file')} ({matched_type}) {status}")
        else:
            results.append(False)
            print(f"  Text '{text[:40]}...' -> NO MATCH ✗")

    return all(results)


def test_pdf_extraction(pdf_path: str):
    """Test extracting data from a PDF using YAML parser."""
    print(f"\nTesting: {Path(pdf_path).name}")

    try:
        result = YAMLProcessor.process(pdf_path, "Test Employer")

        # Check required fields
        required = ["pay_date", "net_pay", "earnings", "taxes"]
        missing = [f for f in required if f not in result or result[f] is None]

        if missing:
            print(f"  Missing fields: {missing}")
            return False

        print(f"  ✓ pay_date: {result.get('pay_date')}")
        print(f"  ✓ net_pay: {result.get('net_pay')}")
        print(f"  ✓ earnings: {len(result.get('earnings', []))} items")
        print(f"  ✓ taxes: {len(result.get('taxes', {}))} categories")
        return True

    except Exception as e:
        print(f"  Error: {e}")
        return False


def find_test_pdfs():
    """Find PDFs for testing."""
    base = Path(__file__).parent.parent
    locations = [base / "cache", base / "source-refs"]

    pdfs = []
    for loc in locations:
        if loc.exists():
            pdfs.extend(loc.rglob("*.pdf"))
    return pdfs


def main():
    print("=" * 60)
    print("YAML Parser Engine Test")
    print("=" * 60)

    # Test 1: Parser cache
    print("\n[1] Testing parser cache...")
    if not test_parser_cache():
        print("  ✗ No parsers loaded")
        return 1
    print("  ✓ Parser cache works")

    # Test 2: Qualifier matching
    print("\n[2] Testing qualifier matching...")
    if not test_qualifier_matching():
        print("  ✗ Qualifier matching failed")

    # Test 3: PDF extraction
    print("\n[3] Testing PDF extraction...")
    pdfs = find_test_pdfs()

    if not pdfs:
        print("  No test PDFs found")
        return 0

    print(f"  Found {len(pdfs)} PDF(s)")

    success_count = 0
    for pdf in pdfs[:5]:  # Test first 5
        if test_pdf_extraction(str(pdf)):
            success_count += 1

    print(f"\n{'=' * 60}")
    print(f"Results: {success_count}/{min(len(pdfs), 5)} PDFs extracted successfully")

    return 0


if __name__ == "__main__":
    sys.exit(main())
