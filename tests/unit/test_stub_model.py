"""Tests for pay stub modeling.

Uses isolated directories via tmp_path and PAY_CALC_CONFIG_PATH
to avoid touching production data.
"""

import json
import pytest
import yaml
from pathlib import Path

from paycalc.sdk.modeling import model_stub


# Zero YTD for period 1 tests
ZERO_YTD = {
    "gross": 0,
    "fit_taxable": 0,
    "fit_withheld": 0,
    "ss_wages": 0,
    "ss_withheld": 0,
    "medicare_wages": 0,
    "medicare_withheld": 0,
    "pretax_401k": 0,
}

# Default benefits for tests (matching base_profile)
DEFAULT_BENEFITS = {
    "pretax_health": 200.00,
    "pretax_dental": 25.00,
}


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
    from paycalc.sdk.modeling.schemas import ModelResult

    write_profile(isolated_env["config_dir"], base_profile)

    result = model_stub("2026-01-10", "him", prior_ytd=ZERO_YTD, benefits=DEFAULT_BENEFITS)

    # Should succeed (ModelResult, not error dict)
    assert isinstance(result, ModelResult), f"Unexpected error: {result.get('error') if isinstance(result, dict) else result}"

    # Check calculations via PaySummary structure
    assert result.current.gross == 5000.00
    assert result.current.deductions.retirement == 500.00  # 10% 401k
    assert result.current.deductions.fully_pretax == 225.00  # health + dental
    assert result.current.taxable.fit == 4275.00  # 5000 - 500 - 225
    assert result.current.net_pay > 0


# === COMP PLAN OVERRIDE TESTS ===


class TestCompPlanOverride:
    """Tests for --comp-plan override validation."""

    def test_comp_plan_override_changes_gross(self, isolated_env, base_profile):
        """Override comp plan changes gross and downstream calculations."""
        from paycalc.sdk.modeling.schemas import ModelResult

        write_profile(isolated_env["config_dir"], base_profile)

        # Without override - uses profile (gross=5000)
        without = model_stub("2026-01-10", "him", prior_ytd=ZERO_YTD, benefits=DEFAULT_BENEFITS)
        assert isinstance(without, ModelResult), f"Error: {without.get('error') if isinstance(without, dict) else without}"
        assert without.current.gross == 5000.00
        assert without.current.deductions.retirement == 500.00  # 10% of 5000

        # With override - different gross
        with_override = model_stub(
            "2026-01-10", "him",
            prior_ytd=ZERO_YTD,
            benefits=DEFAULT_BENEFITS,
            comp_plan_override={
                "gross_per_period": 8000.00,
                "pay_frequency": "biweekly",
                "target_401k_pct": 0.05,
            }
        )
        assert isinstance(with_override, ModelResult), f"Error: {with_override.get('error') if isinstance(with_override, dict) else with_override}"
        assert with_override.current.gross == 8000.00
        assert with_override.current.deductions.retirement == 400.00  # 5% of 8000

        # Verify they're different
        assert without.current.gross != with_override.current.gross
        assert without.current.net_pay != with_override.current.net_pay

    def test_comp_plan_override_rejects_unknown_field(self, isolated_env, base_profile):
        """Unknown field in comp plan override causes validation error."""
        write_profile(isolated_env["config_dir"], base_profile)

        result = model_stub(
            "2026-01-10", "him",
            prior_ytd=ZERO_YTD,
            benefits=DEFAULT_BENEFITS,
            comp_plan_override={
                "gross_per_period": 5000.00,
                "typo_field": 123,  # Unknown field
            }
        )

        assert "error" in result
        assert "typo_field" in result["error"]
        assert "validation_errors" in result


# === BENEFITS TESTS ===


class TestBenefits:
    """Tests for benefits validation."""

    def test_different_benefits_change_deductions(self, isolated_env, base_profile):
        """Different benefits values change pretax deductions and FIT taxable."""
        from paycalc.sdk.modeling.schemas import ModelResult

        write_profile(isolated_env["config_dir"], base_profile)

        # With default benefits (health=200, dental=25)
        with_default = model_stub("2026-01-10", "him", prior_ytd=ZERO_YTD, benefits=DEFAULT_BENEFITS)
        assert isinstance(with_default, ModelResult), f"Error: {with_default.get('error') if isinstance(with_default, dict) else with_default}"
        assert with_default.current.deductions.fully_pretax == 225.00
        assert with_default.current.taxable.fit == 4275.00  # 5000 - 500 - 225

        # With higher benefits
        higher_benefits = {
            "pretax_health": 500.00,
            "pretax_dental": 50.00,
            "pretax_fsa": 200.00,
        }
        with_higher = model_stub(
            "2026-01-10", "him",
            prior_ytd=ZERO_YTD,
            benefits=higher_benefits,
        )
        assert isinstance(with_higher, ModelResult), f"Error: {with_higher.get('error') if isinstance(with_higher, dict) else with_higher}"
        assert with_higher.current.deductions.fully_pretax == 750.00
        assert with_higher.current.taxable.fit == 3750.00  # 5000 - 500 - 750

        # Verify they're different
        assert with_default.current.deductions.fully_pretax != with_higher.current.deductions.fully_pretax
        assert with_default.current.taxable.fit != with_higher.current.taxable.fit

    def test_benefits_rejects_unknown_field(self, isolated_env, base_profile):
        """Unknown field in benefits causes validation error."""
        write_profile(isolated_env["config_dir"], base_profile)

        result = model_stub(
            "2026-01-10", "him",
            prior_ytd=ZERO_YTD,
            benefits={
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
        from paycalc.sdk.modeling.schemas import ModelResult

        write_profile(isolated_env["config_dir"], base_profile)

        # Without override - uses profile or defaults
        without = model_stub("2026-01-10", "him", prior_ytd=ZERO_YTD, benefits=DEFAULT_BENEFITS)
        assert isinstance(without, ModelResult), f"Error: {without.get('error') if isinstance(without, dict) else without}"
        fit_without = without.current.withheld.fit

        # With override - single filer, no dependents (higher withholding)
        with_override = model_stub(
            "2026-01-10", "him",
            prior_ytd=ZERO_YTD,
            benefits=DEFAULT_BENEFITS,
            w4_override={
                "filing_status": "single",
                "step3_dependents": 0,
            }
        )
        assert isinstance(with_override, ModelResult), f"Error: {with_override.get('error') if isinstance(with_override, dict) else with_override}"
        fit_with = with_override.current.withheld.fit

        # Single with no dependents should have higher withholding than MFJ with dependents
        assert fit_with > fit_without

    def test_w4_override_rejects_unknown_field(self, isolated_env, base_profile):
        """Unknown field in W-4 override causes validation error."""
        write_profile(isolated_env["config_dir"], base_profile)

        result = model_stub(
            "2026-01-10", "him",
            prior_ytd=ZERO_YTD,
            benefits=DEFAULT_BENEFITS,
            w4_override={
                "filing_status": "mfj",
                "allowances": 5,  # Old W-4 field, not valid in 2020+ schema
            }
        )

        assert "error" in result
        assert "allowances" in result["error"]
        assert "validation_errors" in result
