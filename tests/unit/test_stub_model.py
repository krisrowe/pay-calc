"""Tests for pay stub modeling.

Uses isolated directories via tmp_path and PAY_CALC_CONFIG_PATH
to avoid touching production data.
"""

import json
import pytest
import yaml
from pathlib import Path

from paycalc.sdk.stub_model import model_stub


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
    """Base profile with comp, benefits, w4 for testing overrides."""
    return {
        "parties": {
            "him": {
                "comp_plans": [{
                    "effective": "2026-01-01",
                    "pay_frequency": "biweekly",
                    "gross_per_period": 5000.00,
                    "target_401k_pct": 0.10,
                }],
                "benefits_plans": [{
                    "year": 2026,
                    "pretax_health": 200.00,
                    "pretax_dental": 25.00,
                }],
                "w4s": [{
                    "effective": "2026-01-01",
                    "filing_status": "mfj",
                    "step3_dependents": 4000,
                }],
            }
        }
    }


def write_profile(config_dir: Path, profile_data: dict):
    """Write profile.yaml to config directory."""
    (config_dir / "profile.yaml").write_text(yaml.dump(profile_data))


# === BASIC TESTS ===


def test_model_first_period_all_registered(isolated_env, base_profile):
    """Model period 1 with comp, benefits, w4 all from registered profile."""
    write_profile(isolated_env["config_dir"], base_profile)

    result = model_stub("2026-01-10", "him")

    # Should succeed
    assert "error" not in result, f"Unexpected error: {result.get('error')}"

    # Check structure
    assert result["party"] == "him"
    assert result["period_number"] == 1
    assert result["periods_per_year"] == 26

    # Check calculations
    current = result["current"]
    assert current["gross"] == 5000.00
    assert current["pretax_401k"] == 500.00  # 10%
    assert current["pretax_benefits"] == 225.00  # 200 + 25
    assert current["fit_taxable"] == 4275.00  # 5000 - 500 - 225
    assert current["net_pay"] > 0

    # Check provenance
    sources = result["sources"]
    assert sources["comp_plan"]["type"] == "registered"
    assert sources["benefits"]["type"] == "registered"
    assert sources["w4"]["type"] == "registered"


# === COMP PLAN OVERRIDE TESTS ===


class TestCompPlanOverride:
    """Tests for --comp-plan override validation."""

    def test_comp_plan_override_changes_gross(self, isolated_env, base_profile):
        """Override comp plan changes gross and downstream calculations."""
        write_profile(isolated_env["config_dir"], base_profile)

        # Without override - uses profile (gross=5000)
        without = model_stub("2026-01-10", "him")
        assert "error" not in without
        assert without["current"]["gross"] == 5000.00
        assert without["current"]["pretax_401k"] == 500.00  # 10% of 5000
        assert without["sources"]["comp_plan"]["type"] == "registered"

        # With override - different gross
        with_override = model_stub(
            "2026-01-10", "him",
            comp_plan_override={
                "gross_per_period": 8000.00,
                "pay_frequency": "biweekly",
                "target_401k_pct": 0.05,
            }
        )
        assert "error" not in with_override
        assert with_override["current"]["gross"] == 8000.00
        assert with_override["current"]["pretax_401k"] == 400.00  # 5% of 8000
        assert with_override["sources"]["comp_plan"]["type"] == "override"

        # Verify they're different
        assert without["current"]["gross"] != with_override["current"]["gross"]
        assert without["current"]["net_pay"] != with_override["current"]["net_pay"]

    def test_comp_plan_override_rejects_unknown_field(self, isolated_env, base_profile):
        """Unknown field in comp plan override causes validation error."""
        write_profile(isolated_env["config_dir"], base_profile)

        result = model_stub(
            "2026-01-10", "him",
            comp_plan_override={
                "gross_per_period": 5000.00,
                "typo_field": 123,  # Unknown field
            }
        )

        assert "error" in result
        assert "typo_field" in result["error"]
        assert "validation_errors" in result


# === BENEFITS OVERRIDE TESTS ===


class TestBenefitsOverride:
    """Tests for --benefits override validation."""

    def test_benefits_override_changes_deductions(self, isolated_env, base_profile):
        """Override benefits changes pretax deductions and FIT taxable."""
        write_profile(isolated_env["config_dir"], base_profile)

        # Without override - uses profile (health=200, dental=25)
        without = model_stub("2026-01-10", "him")
        assert "error" not in without
        assert without["current"]["pretax_benefits"] == 225.00
        assert without["current"]["fit_taxable"] == 4275.00  # 5000 - 500 - 225
        assert without["sources"]["benefits"]["type"] == "registered"

        # With override - higher benefits
        with_override = model_stub(
            "2026-01-10", "him",
            benefits_override={
                "pretax_health": 500.00,
                "pretax_dental": 50.00,
                "pretax_fsa": 200.00,
            }
        )
        assert "error" not in with_override
        assert with_override["current"]["pretax_benefits"] == 750.00
        assert with_override["current"]["fit_taxable"] == 3750.00  # 5000 - 500 - 750
        assert with_override["sources"]["benefits"]["type"] == "override"

        # Verify they're different
        assert without["current"]["pretax_benefits"] != with_override["current"]["pretax_benefits"]
        assert without["current"]["fit_taxable"] != with_override["current"]["fit_taxable"]

    def test_benefits_override_rejects_unknown_field(self, isolated_env, base_profile):
        """Unknown field in benefits override causes validation error."""
        write_profile(isolated_env["config_dir"], base_profile)

        result = model_stub(
            "2026-01-10", "him",
            benefits_override={
                "pretax_health": 200.00,
                "pretax_typo": 50.00,  # Unknown field
            }
        )

        assert "error" in result
        assert "pretax_typo" in result["error"]
        assert "validation_errors" in result


# === W4 OVERRIDE TESTS ===


class TestW4Override:
    """Tests for --w4 override validation."""

    def test_w4_override_changes_withholding(self, isolated_env, base_profile):
        """Override W-4 changes FIT withholding."""
        write_profile(isolated_env["config_dir"], base_profile)

        # Without override - uses profile (mfj, step3_dependents=4000)
        without = model_stub("2026-01-10", "him")
        assert "error" not in without
        assert without["sources"]["w4"]["type"] == "registered"
        fit_without = without["current"]["fit_withheld"]

        # With override - single filer, no dependents (higher withholding)
        with_override = model_stub(
            "2026-01-10", "him",
            w4_override={
                "filing_status": "single",
                "step3_dependents": 0,
            }
        )
        assert "error" not in with_override
        assert with_override["sources"]["w4"]["type"] == "override"
        fit_with = with_override["current"]["fit_withheld"]

        # Single with no dependents should have higher withholding than MFJ with dependents
        assert fit_with > fit_without

    def test_w4_override_rejects_unknown_field(self, isolated_env, base_profile):
        """Unknown field in W-4 override causes validation error."""
        write_profile(isolated_env["config_dir"], base_profile)

        result = model_stub(
            "2026-01-10", "him",
            w4_override={
                "filing_status": "mfj",
                "allowances": 5,  # Old W-4 field, not valid in 2020+ schema
            }
        )

        assert "error" in result
        assert "allowances" in result["error"]
        assert "validation_errors" in result
