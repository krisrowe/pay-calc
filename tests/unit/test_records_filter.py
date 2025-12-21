"""Tests for records filtering via JSONPath expressions.

Tests the matches_jsonpath() function and list_records() data_filter parameter.
"""

import json
from pathlib import Path
import pytest

from paycalc.sdk.records import matches_jsonpath


class TestMatchesJsonpath:
    """Unit tests for the matches_jsonpath() function."""

    def test_regex_on_attribute_value_and_name_limitation(self):
        """Regex works on attribute VALUES but NOT on attribute NAMES.

        JSONPath supports: $.items[?field=~"pattern"] - regex on VALUE of 'field'
        JSONPath does NOT support regex on attribute names themselves.

        This test demonstrates both:
        1. Regex matching on VALUES works as expected
        2. You cannot filter "find fields whose NAME contains X" via JSONPath
        """
        data = {
            "deductions": [
                # VALUE "401k Pre-Tax" contains "401" - regex on value WILL match
                {"category": "401k Pre-Tax", "current_amount": 500},
                # VALUE "Health Insurance" does NOT contain "401"
                {"category": "Health Insurance", "current_amount": 200},
            ]
        }

        # REGEX ON VALUE: category's VALUE contains "401"
        # This SHOULD match - "401k Pre-Tax" contains "401"
        assert matches_jsonpath(data, '$.deductions[?category=~".*401.*"]') is True

        # REGEX ON VALUE: category's VALUE contains "HSA"
        # This should NOT match - no category value contains "HSA"
        assert matches_jsonpath(data, '$.deductions[?category=~".*HSA.*"]') is False

        # ATTRIBUTE NAME matching: JSONPath uses exact names, not regex
        # We can access "current_amount" by exact name, but can't say "find fields named *_amount"
        # This works - exact field name access:
        assert matches_jsonpath(data, '$.deductions[?current_amount>0]') is True

        # There's no JSONPath syntax for "fields whose NAME matches regex"
        # You must know the exact field name to reference it

    def test_filter_on_specific_object_property(self):
        """Filter array items by specific property values.

        JSONPath filter expressions work on arrays. This finds items
        matching both a category AND an amount condition.
        """
        data = {
            "taxes": [
                {"category": "federal", "current_withheld": 1500.00},
                {"category": "state", "current_withheld": 310.00},
                {"category": "medicare", "current_withheld": 72.50},
            ]
        }
        # Find federal tax with withheld > 1000
        assert matches_jsonpath(data, '$.taxes[?category=="federal" & current_withheld>1000]') is True
        # Federal doesn't have > 2000
        assert matches_jsonpath(data, '$.taxes[?category=="federal" & current_withheld>2000]') is False
        # State has > 300
        assert matches_jsonpath(data, '$.taxes[?category=="state" & current_withheld>300]') is True
        # Medicare doesn't have > 100
        assert matches_jsonpath(data, '$.taxes[?category=="medicare" & current_withheld>100]') is False

    def test_has_nonzero_amount_for_field(self):
        """Core use case: find records with non-zero amount for a field.

        Data includes edge cases that should NOT match, mixed with one that should:
        - Missing field entirely (no match)
        - Zero int (no match)
        - Zero float (no match)
        - Positive amount (MATCH)

        One query `amount>0` tests all: returns True only because Payment exists.

        Note: null values cause jsonpath-ng to fail the entire query, so they're
        excluded. Real pay stub data uses 0 for "no amount", not null.
        """
        data = {
            "items": [
                {"label": "Missing"},                        # no amount field
                {"label": "ZeroInt", "amount": 0},           # zero int
                {"label": "ZeroFloat", "amount": 0.0},       # zero float
                {"label": "Payment", "amount": 250.00},      # positive - MATCH
            ]
        }

        # Returns True only because Payment has amount>0
        # Missing, ZeroInt, ZeroFloat all correctly excluded
        assert matches_jsonpath(data, '$.items[?amount>0]') is True

    def test_nothing_found_returns_false(self):
        """Filter that matches nothing returns False."""
        data = {
            "earnings": [
                {"category": "Regular", "current_amount": 5000},
                {"category": "Overtime", "current_amount": 200},
            ]
        }
        # No Bonus category exists
        assert matches_jsonpath(data, '$.earnings[?category=="Bonus"]') is False
        # No negative amounts
        assert matches_jsonpath(data, '$.earnings[?current_amount<0]') is False
        # Path doesn't exist
        assert matches_jsonpath(data, '$.deductions[?amount>0]') is False

    def test_syntax_error_returns_false(self):
        """Invalid JSONPath syntax returns False, does not raise."""
        data = {"items": [{"amount": 100}]}

        assert matches_jsonpath(data, '$.items[?amount>') is False  # Incomplete
        assert matches_jsonpath(data, '$.[invalid') is False  # Bad syntax
        assert matches_jsonpath(data, 'not jsonpath at all') is False  # Not JSONPath


class TestListRecordsWithFilter:
    """Integration tests for list_records() with data_filter parameter."""

    @pytest.fixture
    def isolated_records_dir(self, tmp_path, monkeypatch):
        """Set up isolated config and data directories with test records."""
        import yaml

        config_dir = tmp_path / "config"
        data_dir = tmp_path / "data"
        records_dir = data_dir / "records"
        config_dir.mkdir()
        data_dir.mkdir()
        records_dir.mkdir()

        monkeypatch.setenv("PAY_CALC_CONFIG_PATH", str(config_dir))

        settings = {"data_dir": str(data_dir)}
        (config_dir / "settings.json").write_text(json.dumps(settings))

        profile = {
            "parties": {
                "him": {"companies": [{"name": "Acme Corp", "keywords": ["Acme"]}]}
            }
        }
        (config_dir / "profile.yaml").write_text(yaml.dump(profile))

        return records_dir

    def _write_record(self, records_dir: Path, record_id: str, meta: dict, data: dict):
        """Helper to write a record JSON file."""
        record = {"meta": meta, "data": data}
        (records_dir / f"{record_id}.json").write_text(json.dumps(record))

    def test_combined_meta_and_content_filter(self, isolated_records_dir):
        """Combines CLI meta filter (record type) with JSONPath content filter.

        meta.type = "stub" filters by record type (stub vs w2)
        data_filter JSONPath filters by data content (401k deduction)
        """
        from paycalc.sdk.records import list_records

        # Stub with 401k deduction
        self._write_record(
            isolated_records_dir, "stub_401k",
            {"type": "stub", "year": "2025", "party": "him"},
            {
                "pay_date": "2025-01-15",
                "employer": "Acme Corp",
                "deductions": [
                    {"category": "401k Pre-Tax", "current_amount": 500, "ytd_amount": 500},
                ],
                "pay_summary": {"current": {"gross": 5000}, "ytd": {"gross": 5000}},
            }
        )

        # W-2 record (different meta.type)
        self._write_record(
            isolated_records_dir, "w2_record",
            {"type": "w2", "year": "2024", "party": "him"},
            {
                "tax_year": 2024,
                "employer_name": "Acme Corp",
                "wages": 120000,
                "federal_tax_withheld": 25000,
            }
        )

        # Stub without 401k
        self._write_record(
            isolated_records_dir, "stub_no_401k",
            {"type": "stub", "year": "2025", "party": "him"},
            {
                "pay_date": "2025-01-31",
                "employer": "Acme Corp",
                "deductions": [
                    {"category": "Health Insurance", "current_amount": 200, "ytd_amount": 400},
                ],
                "pay_summary": {"current": {"gross": 5000}, "ytd": {"gross": 10000}},
            }
        )

        # Filter: meta.type=stub AND has 401k deduction in data
        results = list_records(
            type_filter="stub",
            data_filter='$.deductions[?category=~".*401.*" & current_amount>0]'
        )

        assert len(results) == 1
        assert results[0]["id"] == "stub_401k"
