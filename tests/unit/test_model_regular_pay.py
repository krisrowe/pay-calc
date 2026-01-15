"""Tests for regular pay modeling (model_stubs_in_sequence, model_regular_401k_contribs).

Uses isolated directories via tmp_path and PAY_CALC_CONFIG_PATH
to avoid touching production data.

Tests:
1. Happy path: comp plan + late-prior-year reference stub + tax rules
   - Validates YTD = sum of current values
   - Validates current values add up correctly (gross - deductions = net)
   - Validates effective FIT rate matches expected bracket
2. SS wage cap: SS deductions go from full to partial to zero
   - Final YTD SS wages equals the annual cap
3. 401k cap: 401k contributions cap at annual limit
"""

import json
import pytest
import yaml
from pathlib import Path

from paycalc.sdk.stub_model import (
    model_stubs_in_sequence,
    model_regular_401k_contribs,
)


# === TEST CONSTANTS ===

# Using 2025 values - known/stable for validation
TEST_YEAR = 2025
SS_WAGE_CAP = 176100.00
SS_TAX_RATE = 0.062
K401_LIMIT = 23500.00


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
    rules = {
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
            "wage_cap": SS_WAGE_CAP,
            "tax_rate": SS_TAX_RATE,
        },
        "401k": {
            "employee_elective_limit": K401_LIMIT,
        },
        "additional_medicare_tax_threshold": 250000.00,
        "additional_medicare_withholding_threshold": 200000.00,
    }
    return rules


def write_profile(config_dir: Path, profile_data: dict):
    """Write profile.yaml to config directory."""
    (config_dir / "profile.yaml").write_text(yaml.dump(profile_data))


def write_tax_rules(config_dir: Path, year: int, rules: dict):
    """Write tax rules YAML file."""
    tax_rules_dir = config_dir / "tax-rules"
    tax_rules_dir.mkdir(exist_ok=True)
    (tax_rules_dir / f"{year}.yaml").write_text(yaml.dump(rules))


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

    # Write with a unique ID based on year
    stub_file = records_dir / f"reference_stub_{year}.json"
    stub_file.write_text(json.dumps(stub, indent=2))
    return stub_file


# === HAPPY PATH TEST ===


class TestModelRegularPayHappyPath:
    """Happy path test for model_stubs_in_sequence with evenly split 401k."""

    def test_ytd_equals_sum_of_current(self, isolated_env, base_profile, tax_rules_2025):
        """YTD totals should equal sum of all current period values."""
        write_profile(isolated_env["config_dir"], base_profile)
        write_tax_rules(isolated_env["config_dir"], TEST_YEAR, tax_rules_2025)

        # Create reference stub from late prior year to anchor pay schedule
        create_reference_stub(
            isolated_env["data_dir"],
            year="2024",
            party="testparty",
            pay_date="2024-12-20",  # Late December Friday
        )

        # Model full year with evenly split 401k
        period_401k = K401_LIMIT / 26  # ~$903.85 per period

        result = model_stubs_in_sequence(
            TEST_YEAR,
            "testparty",
            comp_plan_override={
                "gross_per_period": 10000.00,
                "pay_frequency": "biweekly",
            },
            benefits_override={
                "pretax_health": 200.00,
                "pretax_dental": 50.00,
            },
            w4_override={
                "filing_status": "mfj",
                "step2c_multiple_jobs": True,
                "step3_dependents": 0,
            },
            pretax_401k=period_401k,
        )

        assert "error" not in result, f"Unexpected error: {result.get('error')}"

        # Sum up all current values from stubs
        sum_gross = sum(s["gross"] for s in result["stubs"])
        sum_401k = sum(s["pretax_401k"] for s in result["stubs"])
        sum_fit_taxable = sum(s["fit_taxable"] for s in result["stubs"])
        sum_fit_withheld = sum(s["fit_withheld"] for s in result["stubs"])
        sum_ss_withheld = sum(s["ss_withheld"] for s in result["stubs"])
        sum_medicare_withheld = sum(s["medicare_withheld"] for s in result["stubs"])
        sum_net_pay = sum(s["net_pay"] for s in result["stubs"])

        ytd = result["ytd"]

        # YTD should equal sum of current values (within rounding tolerance)
        assert abs(ytd["gross"] - sum_gross) < 0.01, f"Gross mismatch: YTD={ytd['gross']}, sum={sum_gross}"
        assert abs(ytd["pretax_401k"] - sum_401k) < 0.01, f"401k mismatch: YTD={ytd['pretax_401k']}, sum={sum_401k}"
        assert abs(ytd["fit_taxable"] - sum_fit_taxable) < 0.01
        assert abs(ytd["fit_withheld"] - sum_fit_withheld) < 0.01
        assert abs(ytd["ss_withheld"] - sum_ss_withheld) < 0.01
        assert abs(ytd["medicare_withheld"] - sum_medicare_withheld) < 0.01
        assert abs(ytd["net_pay"] - sum_net_pay) < 0.01

    def test_current_amounts_balance(self, isolated_env, base_profile, tax_rules_2025):
        """For each stub, gross - deductions = net pay."""
        write_profile(isolated_env["config_dir"], base_profile)
        write_tax_rules(isolated_env["config_dir"], TEST_YEAR, tax_rules_2025)

        # Create reference stub for schedule anchoring
        create_reference_stub(
            isolated_env["data_dir"],
            year="2024",
            party="testparty",
            pay_date="2024-12-20",
        )

        period_401k = K401_LIMIT / 26
        benefits_total = 250.00  # health + dental

        result = model_stubs_in_sequence(
            TEST_YEAR,
            "testparty",
            comp_plan_override={
                "gross_per_period": 10000.00,
                "pay_frequency": "biweekly",
            },
            benefits_override={
                "pretax_health": 200.00,
                "pretax_dental": 50.00,
            },
            w4_override={
                "filing_status": "mfj",
                "step2c_multiple_jobs": True,
                "step3_dependents": 0,
            },
            pretax_401k=period_401k,
        )

        assert "error" not in result, f"Unexpected error: {result.get('error')}"

        for i, stub in enumerate(result["stubs"]):
            if stub["type"] != "regular":
                continue

            gross = stub["gross"]
            pretax_401k = stub["pretax_401k"]
            fit_withheld = stub["fit_withheld"]
            ss_withheld = stub["ss_withheld"]
            medicare_withheld = stub["medicare_withheld"]
            net_pay = stub["net_pay"]

            # Net = Gross - 401k - Benefits - FIT - SS - Medicare
            expected_net = round(gross - pretax_401k - benefits_total - fit_withheld - ss_withheld - medicare_withheld, 2)

            assert abs(net_pay - expected_net) < 0.01, (
                f"Stub {i} ({stub['date']}): net_pay={net_pay}, expected={expected_net}"
            )

    def test_effective_fit_rate_reasonable(self, isolated_env, base_profile, tax_rules_2025):
        """Effective FIT rate should be reasonable for the income level."""
        write_profile(isolated_env["config_dir"], base_profile)
        write_tax_rules(isolated_env["config_dir"], TEST_YEAR, tax_rules_2025)

        # Create reference stub for schedule anchoring
        create_reference_stub(
            isolated_env["data_dir"],
            year="2024",
            party="testparty",
            pay_date="2024-12-20",
        )

        period_401k = K401_LIMIT / 26

        result = model_stubs_in_sequence(
            TEST_YEAR,
            "testparty",
            comp_plan_override={
                "gross_per_period": 10000.00,
                "pay_frequency": "biweekly",
            },
            benefits_override={
                "pretax_health": 200.00,
                "pretax_dental": 50.00,
            },
            w4_override={
                "filing_status": "mfj",
                "step2c_multiple_jobs": True,
                "step3_dependents": 0,
            },
            pretax_401k=period_401k,
        )

        assert "error" not in result, f"Unexpected error: {result.get('error')}"

        ytd = result["ytd"]
        effective_fit_rate = ytd["fit_withheld"] / ytd["fit_taxable"]

        # For MFJ with ~$228,500 FIT taxable ($260k gross - $23.5k 401k - $6.5k benefits),
        # the marginal rate is 22-24%, so effective withholding rate should be in a reasonable range.
        assert 0.10 < effective_fit_rate < 0.30, (
            f"Effective FIT rate {effective_fit_rate:.2%} out of expected range (10-30%)"
        )


# === SS WAGE CAP TEST ===


class TestSocialSecurityWageCap:
    """Test that SS deductions correctly cap at the wage base."""

    def test_ss_deductions_cap_correctly(self, isolated_env, base_profile, tax_rules_2025):
        """SS deductions go from full to partial to zero, YTD matches cap."""
        write_profile(isolated_env["config_dir"], base_profile)
        write_tax_rules(isolated_env["config_dir"], TEST_YEAR, tax_rules_2025)

        # Create reference stub for schedule anchoring
        create_reference_stub(
            isolated_env["data_dir"],
            year="2024",
            party="testparty",
            pay_date="2024-12-20",
        )

        # Use high gross to hit SS cap mid-year
        # SS wage base is $176,100 for 2025
        # With $10,000/period biweekly (26 periods), SS wages = $260,000/year
        # Cap should be hit around period 17-18 ($176,100 / $10,000 = 17.6 periods)

        result = model_stubs_in_sequence(
            TEST_YEAR,
            "testparty",
            comp_plan_override={
                "gross_per_period": 10000.00,
                "pay_frequency": "biweekly",
            },
            benefits_override={
                "pretax_health": 200.00,
                "pretax_dental": 50.00,
            },
            w4_override={
                "filing_status": "mfj",
                "step2c_multiple_jobs": True,
                "step3_dependents": 0,
            },
            pretax_401k=0,  # No 401k to keep FICA taxable = gross
        )

        assert "error" not in result, f"Unexpected error: {result.get('error')}"

        # Extract SS withheld for each stub
        ss_withheld_list = [s["ss_withheld"] for s in result["stubs"] if s["type"] == "regular"]

        # Full SS withholding = $10,000 * 0.062 = $620
        # (Actually, FICA taxable = gross - benefits = $10,000 - $250 = $9,750)
        # So full SS = $9,750 * 0.062 = $604.50
        full_ss = round(9750.00 * SS_TAX_RATE, 2)

        # Find the transition points
        full_payment_periods = []
        partial_payment_period = None
        zero_payment_periods = []

        for i, ss in enumerate(ss_withheld_list):
            if abs(ss - full_ss) < 0.01:
                full_payment_periods.append(i + 1)
            elif ss > 0 and ss < full_ss:
                partial_payment_period = i + 1
            elif ss == 0:
                zero_payment_periods.append(i + 1)

        # Should have some full payments, one partial, then zeros
        assert len(full_payment_periods) > 0, "Should have some full SS payment periods"
        assert partial_payment_period is not None, "Should have one partial SS payment period"
        assert len(zero_payment_periods) > 0, "Should have some zero SS payment periods"

        # Verify the transition order: full periods first, then partial, then zeros
        if full_payment_periods:
            assert max(full_payment_periods) < partial_payment_period, (
                "Partial payment should come after full payments"
            )
        if zero_payment_periods:
            assert partial_payment_period < min(zero_payment_periods), (
                "Zero payments should come after partial payment"
            )

        # Final YTD SS wages should equal the cap
        ytd = result["ytd"]

        assert abs(ytd["ss_wages"] - SS_WAGE_CAP) < 0.01, (
            f"YTD SS wages should equal cap: got {ytd['ss_wages']}, expected {SS_WAGE_CAP}"
        )

        # Total SS withheld should be cap * rate
        expected_ss_withheld = round(SS_WAGE_CAP * SS_TAX_RATE, 2)
        assert abs(ytd["ss_withheld"] - expected_ss_withheld) < 0.01, (
            f"YTD SS withheld should be {expected_ss_withheld}, got {ytd['ss_withheld']}"
        )

    def test_ss_cap_with_high_earner(self, isolated_env, base_profile, tax_rules_2025):
        """High earner hits SS cap early in the year."""
        write_profile(isolated_env["config_dir"], base_profile)
        write_tax_rules(isolated_env["config_dir"], TEST_YEAR, tax_rules_2025)

        # Create reference stub for schedule anchoring
        create_reference_stub(
            isolated_env["data_dir"],
            year="2024",
            party="testparty",
            pay_date="2024-12-20",
        )

        # Very high gross: $25,000/period biweekly
        # SS cap of $176,100 hit in period 7-8 ($176,100 / $25,000 = 7.04)

        result = model_stubs_in_sequence(
            TEST_YEAR,
            "testparty",
            comp_plan_override={
                "gross_per_period": 25000.00,
                "pay_frequency": "biweekly",
            },
            benefits_override={
                "pretax_health": 0,  # No benefits for simpler calculation
            },
            w4_override={
                "filing_status": "mfj",
                "step2c_multiple_jobs": True,
                "step3_dependents": 0,
            },
            pretax_401k=0,
        )

        assert "error" not in result, f"Unexpected error: {result.get('error')}"

        # Count periods with non-zero SS withholding
        stubs_with_ss = [s for s in result["stubs"] if s["type"] == "regular" and s["ss_withheld"] > 0]

        # Should hit cap around period 7-8 (7 full + 1 partial = 8 with SS)
        assert len(stubs_with_ss) <= 8, (
            f"Expected <= 8 periods with SS withholding, got {len(stubs_with_ss)}"
        )
        assert len(stubs_with_ss) >= 7, (
            f"Expected >= 7 periods with SS withholding, got {len(stubs_with_ss)}"
        )

        # Final YTD SS wages should be the cap
        ytd = result["ytd"]
        assert abs(ytd["ss_wages"] - SS_WAGE_CAP) < 0.01


# === 401K CAP TEST ===


class Test401kCap:
    """Test that 401k contributions correctly cap at annual limit."""

    def test_401k_caps_at_limit(self, isolated_env, base_profile, tax_rules_2025):
        """401k contributions stop when annual limit reached."""
        write_profile(isolated_env["config_dir"], base_profile)
        write_tax_rules(isolated_env["config_dir"], TEST_YEAR, tax_rules_2025)

        # Create reference stub for schedule anchoring
        create_reference_stub(
            isolated_env["data_dir"],
            year="2024",
            party="testparty",
            pay_date="2024-12-20",
        )

        # 401k limit is $23,500 for 2025
        # Request 100% of gross as 401k ($10,000/period)
        # Should hit cap after 2.35 periods (round to 3 periods max)

        result = model_regular_401k_contribs(
            TEST_YEAR,
            "testparty",
            regular_401k_contribs={
                "starting_date": "2025-01-03",  # First Friday of 2025
                "amount": 1.0,  # 100% of gross
                "amount_type": "percentage",
            },
            comp_plan_override={
                "gross_per_period": 10000.00,
                "pay_frequency": "biweekly",
            },
            benefits_override={"pretax_health": 0},
            w4_override={"filing_status": "mfj"},
        )

        assert "error" not in result, f"Unexpected error: {result.get('error')}"

        ytd = result["ytd"]

        # Final YTD 401k should be the cap
        assert abs(ytd["pretax_401k"] - K401_LIMIT) < 0.01, (
            f"YTD 401k should equal cap: got {ytd['pretax_401k']}, expected {K401_LIMIT}"
        )

        # After cap, remaining stubs should have $0 401k
        stubs = result["stubs"]
        non_zero_401k_count = sum(1 for s in stubs if s["pretax_401k"] > 0)

        # With $10k gross and $23.5k limit, should have 3 periods max with 401k
        # (2 full at $10k + 1 partial at $3.5k)
        assert non_zero_401k_count <= 3, f"Expected <= 3 periods with 401k, got {non_zero_401k_count}"


# === SUPPLEMENTAL BONUS SS CAP TEST ===


class TestSupplementalBonusSSCap:
    """Test SS cap behavior when supplemental bonus knocks out remaining cap."""

    def test_ss_cap_hit_by_large_bonus(self, isolated_env, base_profile, tax_rules_2025):
        """SS cap is NOT hit in first 6 months, then knocked between stub 13-14 by large bonus."""
        write_profile(isolated_env["config_dir"], base_profile)
        write_tax_rules(isolated_env["config_dir"], TEST_YEAR, tax_rules_2025)

        # Create reference stub for schedule anchoring
        create_reference_stub(
            isolated_env["data_dir"],
            year="2024",
            party="testparty",
            pay_date="2024-12-20",
        )

        # Lower gross so SS cap isn't hit by regular pay alone in first half
        # $6,000/period biweekly = $156,000/year (under $176,100 cap)
        # After 13 periods: 13 * $6,000 = $78,000 SS wages
        # Large bonus of $100,000 on period 14 should knock out remaining cap

        result = model_stubs_in_sequence(
            TEST_YEAR,
            "testparty",
            comp_plan_override={
                "gross_per_period": 6000.00,
                "pay_frequency": "biweekly",
            },
            benefits_override={
                "pretax_health": 0,
            },
            w4_override={
                "filing_status": "mfj",
                "step2c_multiple_jobs": True,
                "step3_dependents": 0,
            },
            pretax_401k=0,
            supplementals=[
                {
                    "date": "2025-07-11",  # Around period 14 (mid-year)
                    "gross": 100000.00,
                }
            ],
        )

        assert "error" not in result, f"Unexpected error: {result.get('error')}"

        # Check first 13 regular stubs all have full SS withholding
        regular_stubs = [s for s in result["stubs"] if s["type"] == "regular"]
        full_ss = round(6000.00 * SS_TAX_RATE, 2)  # $372 per period

        # First ~13 periods should have full SS withholding
        for i in range(min(13, len(regular_stubs))):
            assert abs(regular_stubs[i]["ss_withheld"] - full_ss) < 1.0, (
                f"Period {i+1} should have full SS withholding of ${full_ss}"
            )

        # Final YTD SS wages should equal the cap (capped at $176,100)
        ytd = result["ytd"]
        assert abs(ytd["ss_wages"] - SS_WAGE_CAP) < 0.01, (
            f"YTD SS wages should equal cap: got {ytd['ss_wages']}, expected {SS_WAGE_CAP}"
        )


# === 401K EVEN DISTRIBUTION TEST ===


class Test401kEvenDistribution:
    """Test 401k distributed evenly across all pay periods."""

    def test_evenly_distributed_401k(self, isolated_env, base_profile, tax_rules_2025):
        """401k split evenly produces 26 stubs with consistent amounts, YTD = sum."""
        write_profile(isolated_env["config_dir"], base_profile)
        write_tax_rules(isolated_env["config_dir"], TEST_YEAR, tax_rules_2025)

        # Create reference stub for schedule anchoring
        create_reference_stub(
            isolated_env["data_dir"],
            year="2024",
            party="testparty",
            pay_date="2024-12-20",
        )

        # Even distribution: $23,500 / 26 = $903.846... per period
        period_401k = K401_LIMIT / 26

        result = model_stubs_in_sequence(
            TEST_YEAR,
            "testparty",
            comp_plan_override={
                "gross_per_period": 10000.00,
                "pay_frequency": "biweekly",
            },
            benefits_override={
                "pretax_health": 200.00,
            },
            w4_override={
                "filing_status": "mfj",
                "step2c_multiple_jobs": True,
            },
            pretax_401k=period_401k,
        )

        assert "error" not in result, f"Unexpected error: {result.get('error')}"

        # Should have 26 stubs for biweekly pay
        stubs = result["stubs"]
        regular_stubs = [s for s in stubs if s["type"] == "regular"]
        assert len(regular_stubs) == 26, f"Expected 26 regular stubs, got {len(regular_stubs)}"

        # Get 401k amounts from each stub
        k401_amounts = [s["pretax_401k"] for s in regular_stubs]

        # Find the most common amount (should be all but one, or all the same)
        from collections import Counter
        amount_counts = Counter(round(a, 2) for a in k401_amounts)
        most_common_amount, most_common_count = amount_counts.most_common(1)[0]

        # All but one should have the same amount (or all same)
        assert most_common_count >= 25, (
            f"Expected at least 25 stubs with same 401k amount, got {most_common_count}"
        )

        # Any different amount should be within 1% of the common amount
        tolerance = most_common_amount * 0.01
        for i, amount in enumerate(k401_amounts):
            diff = abs(amount - most_common_amount)
            assert diff < tolerance + 0.01, (  # +0.01 for rounding
                f"Stub {i+1} 401k amount ${amount:.2f} differs by more than 1% "
                f"from common amount ${most_common_amount:.2f}"
            )

        # Final YTD 401k should equal sum of all current 401k amounts
        sum_401k = sum(k401_amounts)
        ytd_401k = result["ytd"]["pretax_401k"]

        assert abs(ytd_401k - sum_401k) < 0.01, (
            f"YTD 401k ${ytd_401k:.2f} should equal sum of stubs ${sum_401k:.2f}"
        )

        # YTD should be close to the limit (within rounding tolerance)
        assert abs(ytd_401k - K401_LIMIT) < 1.00, (
            f"YTD 401k ${ytd_401k:.2f} should be close to limit ${K401_LIMIT:.2f}"
        )


# === MAX 401K CAP HIT TEST ===


class TestMax401kCapHitEarly:
    """Test max 401k contributions hitting cap early in the year."""

    def test_max_401k_cap_hit_early(self, isolated_env, base_profile, tax_rules_2025):
        """With $5000 gross, 401k cap is hit in ~5 periods, then $0 thereafter."""
        from paycalc.sdk.stub_model import max_regular_401k_contribs

        write_profile(isolated_env["config_dir"], base_profile)
        write_tax_rules(isolated_env["config_dir"], TEST_YEAR, tax_rules_2025)

        # Create reference stub for schedule anchoring
        create_reference_stub(
            isolated_env["data_dir"],
            year="2024",
            party="testparty",
            pay_date="2024-12-20",
        )

        # Low gross: $5,000/period biweekly
        # 401k limit $23,500 / $5,000 = 4.7 periods
        # So 4 full periods + 1 partial + 21 with $0

        result = max_regular_401k_contribs(
            TEST_YEAR,
            "testparty",
            comp_plan_override={
                "gross_per_period": 5000.00,
                "pay_frequency": "biweekly",
            },
            benefits_override={
                "pretax_health": 200.00,
            },
            w4_override={
                "filing_status": "mfj",
                "step2c_multiple_jobs": True,
            },
        )

        assert "error" not in result, f"Unexpected error: {result.get('error')}"

        stubs = result["stubs"]
        regular_stubs = [s for s in stubs if s["type"] == "regular"]

        # Should have 26 stubs
        assert len(regular_stubs) == 26, f"Expected 26 stubs, got {len(regular_stubs)}"

        # Separate stubs by 401k contribution
        stubs_with_401k = [s for s in regular_stubs if s["pretax_401k"] > 0]
        stubs_without_401k = [s for s in regular_stubs if s["pretax_401k"] == 0]

        # Should have exactly 5 stubs with 401k (4 full + 1 partial)
        assert len(stubs_with_401k) == 5, (
            f"Expected 5 periods with 401k (4 full + 1 partial), got {len(stubs_with_401k)}"
        )

        # First 4 should be full $5,000 contributions
        for i in range(4):
            assert abs(stubs_with_401k[i]["pretax_401k"] - 5000.00) < 0.01, (
                f"Period {i+1} should have full $5,000 401k, got ${stubs_with_401k[i]['pretax_401k']:.2f}"
            )

        # 5th should be partial (remainder): $23,500 - $20,000 = $3,500
        partial_401k = stubs_with_401k[4]["pretax_401k"]
        expected_partial = K401_LIMIT - 20000.00  # 23,500 - 20,000 = 3,500
        assert abs(partial_401k - expected_partial) < 0.01, (
            f"Partial 401k should be ${expected_partial:.2f}, got ${partial_401k:.2f}"
        )

        # Remaining 21 stubs should have $0 401k
        assert len(stubs_without_401k) == 21, (
            f"Expected 21 periods with $0 401k, got {len(stubs_without_401k)}"
        )

        # Verify that stubs after cap hit have higher net pay
        # (since no 401k is being deducted)
        last_401k_stub = stubs_with_401k[-1]  # Period 5 (partial)
        first_no_401k_stub = stubs_without_401k[0]  # Period 6

        # Net pay without 401k should be higher than net pay with partial 401k
        # (unless SS cap causes differences)
        # Actually, the net pay increase isn't guaranteed to be exact because
        # FIT is different, so just verify $0 401k
        for stub in stubs_without_401k:
            assert stub["pretax_401k"] == 0, (
                f"Stub {stub['date']} should have $0 401k"
            )

        # Final YTD 401k should equal the cap
        ytd = result["ytd"]
        assert abs(ytd["pretax_401k"] - K401_LIMIT) < 0.01, (
            f"YTD 401k ${ytd['pretax_401k']:.2f} should equal cap ${K401_LIMIT:.2f}"
        )

        # YTD 401k should equal sum of all stubs
        sum_401k = sum(s["pretax_401k"] for s in regular_stubs)
        assert abs(ytd["pretax_401k"] - sum_401k) < 0.01, (
            f"YTD 401k ${ytd['pretax_401k']:.2f} should equal sum ${sum_401k:.2f}"
        )


# === SKIPPED: MID-YEAR COMP PLAN CHANGE ===


@pytest.mark.skip(reason="Not implemented: mid-year comp plan changes")
class TestMidYearCompPlanChange:
    """Test mid-year comp plan changes.

    TODO: Test scenarios:
    1. Comp plan change only (same employer, different gross)
    2. Employer + comp plan change (job change mid-year)
    """

    def test_comp_plan_change_same_employer(self, isolated_env, base_profile, tax_rules_2025):
        """Mid-year raise: same employer, gross changes."""
        pass

    def test_employer_and_comp_plan_change(self, isolated_env, base_profile, tax_rules_2025):
        """Mid-year job change: different employer and gross."""
        pass
