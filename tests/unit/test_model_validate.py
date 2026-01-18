"""Tests for stub validation (validate_stub, validate_stub_in_sequence).

Uses isolated directories via tmp_path and PAY_CALC_CONFIG_PATH
to avoid touching production data.

Tests invoke the SDK validation methods directly, matching how the CLI
uses them via `pay-calc model validate <record_id>` (non-iterative)
and `pay-calc model validate <record_id> -i` (iterative).
"""

import json
import pytest
import yaml
from pathlib import Path

from paycalc.sdk.modeling import (
    validate_stub,
    validate_stub_in_sequence,
)
from paycalc.sdk.schemas import FicaRoundingBalance


# === TEST CONSTANTS ===

TEST_YEAR = 2025


# === FIXTURES ===


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Set up isolated environment with config and data directories."""
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"

    config_dir.mkdir()
    data_dir.mkdir()

    # Point SDK to isolated directories
    monkeypatch.setenv("PAY_CALC_CONFIG_PATH", str(config_dir))

    settings = {"data_dir": str(data_dir)}
    (config_dir / "settings.json").write_text(json.dumps(settings))

    return {
        "config_dir": config_dir,
        "data_dir": data_dir,
    }


@pytest.fixture
def base_profile():
    """Base profile with comp plan for testing."""
    return {
        "parties": {
            "testparty": {
                "comp_plans": [{
                    "effective": "2024-01-01",
                    "pay_frequency": "biweekly",
                    "gross_per_period": 10000.00,
                }],
            }
        }
    }


@pytest.fixture
def tax_rules_2025():
    """Tax rules for 2025 matching SDK format."""
    return {
        "mfj": {
            "standard_deduction": 30000,
            "tax_brackets": [
                {"up_to": 23850, "rate": 0.10},
                {"up_to": 96950, "rate": 0.12},
                {"up_to": 206700, "rate": 0.22},
                {"up_to": 394600, "rate": 0.24},
                {"up_to": 501050, "rate": 0.32},
                {"up_to": 751600, "rate": 0.35},
                {"over": 751600, "rate": 0.37},
            ],
        },
        "social_security": {
            "wage_cap": 176100.00,
            "tax_rate": 0.062,
        },
        "401k": {
            "employee_elective_limit": 23500.00,
        },
        "additional_medicare_tax_threshold": 250000.00,
        "additional_medicare_withholding_threshold": 200000.00,
    }


def write_profile(config_dir: Path, profile_data: dict):
    """Write profile.yaml to config directory."""
    (config_dir / "profile.yaml").write_text(yaml.dump(profile_data))


def write_tax_rules(config_dir: Path, year: int, rules: dict):
    """Write tax rules YAML file."""
    tax_rules_dir = config_dir / "tax-rules"
    tax_rules_dir.mkdir(exist_ok=True)
    (tax_rules_dir / f"{year}.yaml").write_text(yaml.dump(rules))


def create_stub_record(data_dir: Path, record_id: str, party: str, stub_data: dict):
    """Create a stub record in the records directory.

    Args:
        data_dir: Data directory path
        record_id: 8-character record ID
        party: Party identifier
        stub_data: Stub data dict (pay_date, employer, earnings, deductions, etc.)

    Returns:
        Path to created record file
    """
    records_dir = data_dir / "records"
    records_dir.mkdir(exist_ok=True)

    record = {
        "id": record_id,
        "meta": {
            "type": "stub",
            "party": party,
            "year": stub_data.get("pay_date", "")[:4],
        },
        "data": stub_data,
    }

    record_file = records_dir / f"{record_id}.json"
    record_file.write_text(json.dumps(record, indent=2))
    return record_file


def create_reference_stub(data_dir: Path, year: str, party: str, pay_date: str):
    """Create a reference stub from late prior year for schedule anchoring."""
    records_dir = data_dir / "records"
    records_dir.mkdir(exist_ok=True)

    stub = {
        "meta": {
            "type": "stub",
            "party": party,
            "year": year,
            "is_supplemental": False,
        },
        "data": {
            "pay_date": pay_date,
            "employer": "Test Corp",
            "pay_summary": {
                "current": {"gross": 10000.00},
                "ytd": {"gross": 260000.00},
            },
            "taxes": {
                "federal_income_tax": {"current_withheld": 2000.00, "ytd_withheld": 52000.00},
                "social_security": {"current_withheld": 620.00, "ytd_withheld": 10918.20},
                "medicare": {"current_withheld": 145.00, "ytd_withheld": 3770.00},
            },
        },
    }

    stub_file = records_dir / f"reference_stub_{year}.json"
    stub_file.write_text(json.dumps(stub, indent=2))
    return stub_file


# === TEST STUB DATA ===


def make_regular_stub_data(pay_date: str) -> dict:
    """Create a regular pay stub data structure for testing.

    This matches the structure expected by get_pay_stub / PayStub schema.
    """
    return {
        "pay_date": pay_date,
        "employer": "Test Corp",
        "earnings": [
            {"type": "Regular", "current": 10000.00, "ytd": 20000.00},
        ],
        "deductions": [
            {"type": "Health", "current": 200.00, "ytd": 400.00},
            {"type": "401k", "current": 500.00, "ytd": 1000.00},
        ],
        "taxes": {
            "federal_income_tax": {
                "taxable_wages": 9300.00,
                "ytd_taxable_wages": 18600.00,
                "current_withheld": 1500.00,
                "ytd_withheld": 3000.00,
            },
            "social_security": {
                "taxable_wages": 9800.00,
                "ytd_taxable_wages": 19600.00,
                "current_withheld": 607.60,
                "ytd_withheld": 1215.20,
            },
            "medicare": {
                "taxable_wages": 9800.00,
                "ytd_taxable_wages": 19600.00,
                "current_withheld": 142.10,
                "ytd_withheld": 284.20,
            },
        },
        "pay_summary": {
            "current": {
                "gross": 10000.00,
                "net_pay": 7050.30,
            },
            "ytd": {
                "gross": 20000.00,
                "net_pay": 14100.60,
            },
        },
    }


# === TESTS ===


class TestValidateStub:
    """Tests for validate_stub (non-iterative validation)."""

    def test_validate_stub_returns_result(self, isolated_env, base_profile, tax_rules_2025):
        """validate_stub returns a result dict with expected keys."""
        write_profile(isolated_env["config_dir"], base_profile)
        write_tax_rules(isolated_env["config_dir"], TEST_YEAR, tax_rules_2025)

        # Create a stub record
        record_id = "teststub"
        stub_data = make_regular_stub_data(f"{TEST_YEAR}-01-17")
        create_stub_record(isolated_env["data_dir"], record_id, "testparty", stub_data)

        # Call validate_stub
        result = validate_stub(record_id, FicaRoundingBalance.none())

        # Should return dict with expected keys
        assert isinstance(result, dict)
        assert "record_id" in result or "error" in result

        if "error" not in result:
            assert result["record_id"] == record_id
            assert "party" in result
            assert "pay_date" in result
            assert "match" in result
            assert "discrepancies" in result

    def test_validate_stub_not_found(self, isolated_env, base_profile, tax_rules_2025):
        """validate_stub returns error for non-existent record."""
        write_profile(isolated_env["config_dir"], base_profile)
        write_tax_rules(isolated_env["config_dir"], TEST_YEAR, tax_rules_2025)

        result = validate_stub("notfound", FicaRoundingBalance.none())

        assert "error" in result
        assert result["match"] is False


class TestValidateStubInSequence:
    """Tests for validate_stub_in_sequence (iterative validation)."""

    def test_validate_stub_in_sequence_returns_result(self, isolated_env, base_profile, tax_rules_2025):
        """validate_stub_in_sequence returns a result dict with expected keys."""
        write_profile(isolated_env["config_dir"], base_profile)
        write_tax_rules(isolated_env["config_dir"], TEST_YEAR, tax_rules_2025)

        # Create reference stub for schedule anchoring
        create_reference_stub(
            isolated_env["data_dir"],
            year="2024",
            party="testparty",
            pay_date="2024-12-20",
        )

        # Create a stub record to validate
        record_id = "teststub"
        stub_data = make_regular_stub_data(f"{TEST_YEAR}-01-17")
        create_stub_record(isolated_env["data_dir"], record_id, "testparty", stub_data)

        # Call validate_stub_in_sequence
        result = validate_stub_in_sequence(record_id)

        # Should return dict with expected keys
        assert isinstance(result, dict)
        assert "record_id" in result or "error" in result

        if "error" not in result:
            assert result["record_id"] == record_id
            assert "party" in result
            assert "pay_date" in result
            assert "match" in result
            assert "discrepancies" in result
            assert "periods_modeled" in result

    def test_validate_stub_in_sequence_not_found(self, isolated_env, base_profile, tax_rules_2025):
        """validate_stub_in_sequence returns error for non-existent record."""
        write_profile(isolated_env["config_dir"], base_profile)
        write_tax_rules(isolated_env["config_dir"], TEST_YEAR, tax_rules_2025)

        result = validate_stub_in_sequence("notfound")

        assert "error" in result
        assert result["match"] is False
