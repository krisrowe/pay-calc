"""Tests for pay stub modeling.

Uses isolated directories via tmp_path and PAY_CALC_CONFIG_PATH
to avoid touching production data.
"""

import json
import pytest
import yaml
from pathlib import Path

from paycalc.sdk.modeling import model_stub
from paycalc.sdk.schemas import DeductionTotals, PaySummary


# Zero YTD for period 1 tests
ZERO_YTD = PaySummary.zero()

# Default deductions for tests (health=200, dental=25, 401k=500)
DEFAULT_DEDUCTIONS = DeductionTotals(
    fully_pretax=225.00,  # health + dental
    retirement=500.00,    # 401k
    post_tax=0,
)


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
    """Model period 1 with comp, w4 from profile, deductions passed directly."""
    from paycalc.sdk.modeling.schemas import ModelResult

    write_profile(isolated_env["config_dir"], base_profile)

    result = model_stub(
        "2026-01-10", "him",
        prior_ytd=ZERO_YTD,
        current_deductions=DEFAULT_DEDUCTIONS,
    )

    # Should succeed (ModelResult, not error dict)
    assert isinstance(result, ModelResult), f"Unexpected error: {result.get('error') if isinstance(result, dict) else result}"

    # Check calculations via PaySummary structure
    assert result.current.gross == 5000.00
    assert result.current.deductions.retirement == 500.00  # 401k
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
        without = model_stub(
            "2026-01-10", "him",
            prior_ytd=ZERO_YTD,
            current_deductions=DEFAULT_DEDUCTIONS,
        )
        assert isinstance(without, ModelResult), f"Error: {without.get('error') if isinstance(without, dict) else without}"
        assert without.current.gross == 5000.00
        assert without.current.deductions.retirement == 500.00

        # With override - different gross, different deductions
        override_deductions = DeductionTotals(
            fully_pretax=225.00,
            retirement=400.00,  # different 401k
            post_tax=0,
        )
        with_override = model_stub(
            "2026-01-10", "him",
            prior_ytd=ZERO_YTD,
            current_deductions=override_deductions,
            comp_plan_override={
                "gross_per_period": 8000.00,
                "pay_frequency": "biweekly",
            }
        )
        assert isinstance(with_override, ModelResult), f"Error: {with_override.get('error') if isinstance(with_override, dict) else with_override}"
        assert with_override.current.gross == 8000.00
        assert with_override.current.deductions.retirement == 400.00

        # Verify they're different
        assert without.current.gross != with_override.current.gross
        assert without.current.net_pay != with_override.current.net_pay

    def test_comp_plan_override_rejects_unknown_field(self, isolated_env, base_profile):
        """Unknown field in comp plan override causes validation error."""
        write_profile(isolated_env["config_dir"], base_profile)

        result = model_stub(
            "2026-01-10", "him",
            prior_ytd=ZERO_YTD,
            current_deductions=DEFAULT_DEDUCTIONS,
            comp_plan_override={
                "gross_per_period": 5000.00,
                "typo_field": 123,  # Unknown field
            }
        )

        assert "error" in result
        assert "typo_field" in result["error"]
        assert "validation_errors" in result


# === DEDUCTIONS TESTS ===


class TestDeductions:
    """Tests for deduction inputs affecting tax calculations."""

    def test_different_deductions_change_taxable(self, isolated_env, base_profile):
        """Different deduction values change FIT taxable wages."""
        from paycalc.sdk.modeling.schemas import ModelResult

        write_profile(isolated_env["config_dir"], base_profile)

        # With default deductions
        with_default = model_stub(
            "2026-01-10", "him",
            prior_ytd=ZERO_YTD,
            current_deductions=DEFAULT_DEDUCTIONS,
        )
        assert isinstance(with_default, ModelResult), f"Error: {with_default.get('error') if isinstance(with_default, dict) else with_default}"
        assert with_default.current.deductions.fully_pretax == 225.00
        assert with_default.current.taxable.fit == 4275.00  # 5000 - 500 - 225

        # With higher deductions
        higher_deductions = DeductionTotals(
            fully_pretax=750.00,  # higher pretax
            retirement=500.00,
            post_tax=0,
        )
        with_higher = model_stub(
            "2026-01-10", "him",
            prior_ytd=ZERO_YTD,
            current_deductions=higher_deductions,
        )
        assert isinstance(with_higher, ModelResult), f"Error: {with_higher.get('error') if isinstance(with_higher, dict) else with_higher}"
        assert with_higher.current.deductions.fully_pretax == 750.00
        assert with_higher.current.taxable.fit == 3750.00  # 5000 - 500 - 750

        # Verify they're different
        assert with_default.current.deductions.fully_pretax != with_higher.current.deductions.fully_pretax
        assert with_default.current.taxable.fit != with_higher.current.taxable.fit

    def test_post_tax_deductions_affect_net_not_taxable(self, isolated_env, base_profile):
        """Post-tax deductions reduce net pay but not taxable wages."""
        from paycalc.sdk.modeling.schemas import ModelResult

        write_profile(isolated_env["config_dir"], base_profile)

        # Without post-tax
        without_post_tax = model_stub(
            "2026-01-10", "him",
            prior_ytd=ZERO_YTD,
            current_deductions=DEFAULT_DEDUCTIONS,
        )
        assert isinstance(without_post_tax, ModelResult)

        # With post-tax
        with_post_tax = DeductionTotals(
            fully_pretax=225.00,
            retirement=500.00,
            post_tax=100.00,  # adds post-tax deduction
        )
        with_post_tax_result = model_stub(
            "2026-01-10", "him",
            prior_ytd=ZERO_YTD,
            current_deductions=with_post_tax,
        )
        assert isinstance(with_post_tax_result, ModelResult)

        # Taxable wages should be the same (post-tax doesn't affect them)
        assert without_post_tax.current.taxable.fit == with_post_tax_result.current.taxable.fit

        # Net pay should be lower by the post-tax amount
        assert with_post_tax_result.current.net_pay == without_post_tax.current.net_pay - 100.00


# === W4 OVERRIDE TESTS ===


class TestW4Override:
    """Tests for --w4 override validation."""

    def test_w4_override_changes_withholding(self, isolated_env, base_profile):
        """Override W-4 changes FIT withholding."""
        from paycalc.sdk.modeling.schemas import ModelResult

        write_profile(isolated_env["config_dir"], base_profile)

        # Without override - uses profile W-4 (MFJ with $4000 dependents credit)
        without = model_stub(
            "2026-01-10", "him",
            prior_ytd=ZERO_YTD,
            current_deductions=DEFAULT_DEDUCTIONS,
        )
        assert isinstance(without, ModelResult), f"Error: {without.get('error') if isinstance(without, dict) else without}"
        fit_without = without.current.withheld.fit

        # With override - single filer, no dependents (higher withholding)
        with_override = model_stub(
            "2026-01-10", "him",
            prior_ytd=ZERO_YTD,
            current_deductions=DEFAULT_DEDUCTIONS,
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
            current_deductions=DEFAULT_DEDUCTIONS,
            w4_override={
                "filing_status": "mfj",
                "allowances": 5,  # Old W-4 field, not valid in 2020+ schema
            }
        )

        assert "error" in result
        assert "allowances" in result["error"]
        assert "validation_errors" in result
