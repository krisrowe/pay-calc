"""Withholding analysis commands."""

import click
import math
from pathlib import Path


# 2025/2026 tax brackets (MFJ) - estimated with inflation adjustment
TAX_BRACKETS_2025 = [
    (23200, 0.10),
    (94300, 0.12),
    (201050, 0.22),
    (383900, 0.24),
    (487450, 0.32),
    (731200, 0.35),
    (float('inf'), 0.37)
]

TAX_BRACKETS_2026 = [
    (23850, 0.10),
    (96950, 0.12),
    (206700, 0.22),
    (394600, 0.24),
    (501050, 0.32),
    (751600, 0.35),
    (float('inf'), 0.37)
]

STD_DEDUCTION = {
    "2025": 31500,
    "2026": 32300,
}

# IRS Pub 15-T 2025 Percentage Method Tables (Annual)
# For computing withholding from W-4 inputs
# Format: (income_threshold, base_tax, marginal_rate)
WITHHOLDING_TABLES_2025 = {
    "mfj": [
        (14600, 0, 0.00),        # Standard deduction zone
        (28250, 0, 0.10),
        (100500, 1365, 0.12),
        (207050, 10035, 0.22),
        (400400, 33476, 0.24),
        (519550, 79880, 0.32),
        (762950, 118008, 0.35),
        (float('inf'), 203208, 0.37)
    ],
    "single": [
        (7300, 0, 0.00),
        (17125, 0, 0.10),
        (53250, 983, 0.12),
        (106525, 5318, 0.22),
        (203200, 17043, 0.24),
        (262775, 40265, 0.32),
        (631700, 59332, 0.35),
        (float('inf'), 188486, 0.37)
    ]
}

PAY_PERIODS = {
    "weekly": 52,
    "biweekly": 26,
    "semimonthly": 24,
    "monthly": 12
}


def load_w4_settings(party: str) -> dict:
    """Load W-4 settings from profile.yaml."""
    from paycalc.sdk import get_data_path
    import yaml

    # profile.yaml is in parent of data directory
    data_path = get_data_path()
    profile_path = data_path.parent / "profile.yaml"
    if not profile_path.exists():
        # Also try in data directory itself
        profile_path = data_path / "profile.yaml"
    if not profile_path.exists():
        return None

    try:
        with open(profile_path) as f:
            profile = yaml.safe_load(f)
        return profile.get("w4", {}).get(party)
    except Exception:
        return None


def calc_withholding_per_period(gross_per_period: float, w4: dict) -> float:
    """Calculate federal withholding per pay period using IRS Pub 15-T percentage method."""
    filing = w4.get("filing_status", "mfj")
    freq = w4.get("pay_frequency", "biweekly")
    periods = PAY_PERIODS.get(freq, 26)

    step3 = w4.get("step3_dependents", 0)
    step4a = w4.get("step4a_other_income", 0)
    step4b = w4.get("step4b_deductions", 0)
    step4c = w4.get("step4c_extra_withholding", 0)

    # Step 1: Annualize wages
    annual_wages = gross_per_period * periods

    # Step 2: Adjust for Step 4(a) and 4(b)
    adjusted_annual = annual_wages + step4a - step4b

    # Step 3: Look up tentative annual withholding from tables
    table = WITHHOLDING_TABLES_2025.get(filing, WITHHOLDING_TABLES_2025["mfj"])
    tentative_annual = 0
    prev_threshold = 0
    for threshold, base_tax, rate in table:
        if adjusted_annual <= threshold:
            tentative_annual = base_tax + (adjusted_annual - prev_threshold) * rate
            break
        prev_threshold = threshold

    # Step 4: Divide by pay periods
    tentative_per_period = tentative_annual / periods

    # Step 5: Subtract prorated Step 3 credits
    credit_per_period = step3 / periods
    withholding = max(0, tentative_per_period - credit_per_period)

    # Step 6: Add Step 4(c) extra withholding
    withholding += step4c

    return withholding


def calc_federal_tax(income: float, brackets: list) -> float:
    """Calculate federal income tax using progressive brackets."""
    tax = 0
    prev_limit = 0
    for limit, rate in brackets:
        if income <= prev_limit:
            break
        taxable_in_bracket = min(income, limit) - prev_limit
        tax += taxable_in_bracket * rate
        prev_limit = limit
    return tax


def get_prior_year_bonus(year: int, party: str) -> tuple:
    """Get bonus total from prior year analysis. Returns (amount, source_description)."""
    from paycalc.sdk import get_data_path
    import json

    prior_year = str(year - 1)
    data_path = get_data_path() / f"{prior_year}_{party}_pay_all.json"

    if not data_path.exists():
        return 0, None

    try:
        with open(data_path) as f:
            data = json.load(f)

        # Sum bonus amounts from ytd_breakdown
        ytd = data.get("ytd_breakdown", {}).get("earnings", {})
        bonus_total = 0
        bonus_types = []
        for key, value in ytd.items():
            key_lower = key.lower()
            if "bonus" in key_lower and value > 0:
                bonus_total += value
                bonus_types.append(key)

        if bonus_total > 0:
            return bonus_total, f"{prior_year} analysis ({', '.join(bonus_types)})"
        return 0, None
    except Exception:
        return 0, None


def get_prior_year_supplemental_rate(year: int, party: str) -> tuple:
    """Get supplemental withholding rate from prior year RSU/bonus stubs. Returns (rate, source_description)."""
    from paycalc.sdk import get_data_path
    import json
    import os

    prior_year = str(year - 1)
    records_path = get_data_path() / "records" / prior_year / party

    if not records_path.exists():
        return None, None

    # Look for RSU or bonus stubs
    supplemental_rates = []

    try:
        for filename in os.listdir(records_path):
            if not filename.endswith(".json"):
                continue
            filepath = records_path / filename
            with open(filepath) as f:
                record = json.load(f)

            # Get nested data (records have meta/data structure)
            data = record.get("data", record)  # fallback to record if no data key
            meta = record.get("meta", {})

            # Skip non-stubs
            record_type = meta.get("type") or record.get("record_type")
            if record_type != "stub":
                continue

            # Check earnings for RSU or bonus
            earnings = data.get("earnings", [])
            if isinstance(earnings, dict):
                earnings = [{"type": k, **v} if isinstance(v, dict) else {"type": k, "current_amount": v}
                           for k, v in earnings.items()]

            has_supplemental = False
            supplemental_gross = 0
            for earning in earnings:
                name = (earning.get("type") or earning.get("name") or "").lower()
                if "stock" in name or "rsu" in name or "bonus" in name:
                    has_supplemental = True
                    supplemental_gross += earning.get("current_amount") or earning.get("amount") or 0

            if not has_supplemental or supplemental_gross <= 0:
                continue

            # Get federal tax withheld
            taxes = data.get("taxes", {})
            fed_tax = taxes.get("federal_income_tax", {}) or taxes.get("federal_income", {})
            fed_withheld = fed_tax.get("current_withheld") or fed_tax.get("current") or 0

            # Get gross pay
            pay_summary = data.get("pay_summary", {})
            current = pay_summary.get("current", {})
            gross = current.get("gross") or current.get("gross_pay") or 0

            if gross > 0 and fed_withheld > 0:
                rate = fed_withheld / gross
                # Only consider reasonable supplemental rates (15-40%)
                if 0.15 <= rate <= 0.40:
                    supplemental_rates.append(rate)

        if supplemental_rates:
            # Use the most common rate (mode) or average
            avg_rate = sum(supplemental_rates) / len(supplemental_rates)
            return avg_rate, f"{prior_year} RSU/bonus stubs (n={len(supplemental_rates)})"

        return None, None
    except Exception:
        return None, None


@click.group()
def withhold():
    """Analyze withholding and recommend adjustments."""
    pass


@withhold.command("calc")
@click.argument("year")
@click.argument("party", default="him", required=False)
@click.option("--salary", type=float, help="Annual salary (default: $202K from current pay rate)")
@click.option("--bonus", type=float, help="Expected bonus (default: prior year total)")
@click.option("--other-salary", type=float, default=110000, help="Other party's annual salary (default: $110,000)")
@click.option("--future-grant", type=float, default=0, help="Annual future RSU grant value")
@click.option("--price", type=float, default=180, help="Stock price for RSU valuation (default: $180)")
@click.option("--salary-rate", type=float, default=None, help="Salary withholding rate (default: calculated from W-4)")
@click.option("--supplemental-rate", type=float, default=None, help="Supplemental income withholding rate (default: prior year or IRS min 22%)")
@click.option("--other-rate", type=float, default=0.068, help="Other party's salary withholding rate (default: 6.8%)")
def calc(year, party, salary, bonus, other_salary, future_grant, price, salary_rate, supplemental_rate, other_rate):
    """Calculate recommended additional withholding for a tax year.

    Analyzes expected income from salary, bonus, and RSUs for PARTY,
    calculates tax liability, and recommends additional withholding
    per paycheck to avoid owing at tax time.

    Assumes the other party's withholding remains unchanged.

    PARTY defaults to 'him'.

    Examples:

    \b
      pay-calc withhold calc 2026
      pay-calc withhold calc 2026 --bonus 50000 --future-grant 80000
      pay-calc withhold calc 2026 --salary-rate 0.22
    """
    from paycalc.sdk.rsus import find_latest_export, parse_equity_export, get_vesting_in_range, project_future_grants
    from datetime import date

    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    year_int = int(year)

    # Get tax brackets and standard deduction
    if year == "2025":
        brackets = TAX_BRACKETS_2025
        std_deduction = STD_DEDUCTION["2025"]
    else:
        brackets = TAX_BRACKETS_2026
        std_deduction = STD_DEDUCTION.get(year, STD_DEDUCTION["2026"])

    # Load W-4 settings
    w4_settings = load_w4_settings(party)
    other_party = "her" if party == "him" else "him"
    other_w4_settings = load_w4_settings(other_party)

    # Calculate salary (default from typical Employer A LLC biweekly)
    salary_source = None
    biweekly_gross = 5000.00  # default
    if salary is None:
        salary = biweekly_gross * 26  # ~$202K
        salary_source = "default ($7,800.00 biweekly)"
    else:
        biweekly_gross = salary / 26
        salary_source = "command line"

    # Calculate salary withholding rate from W-4 if not specified
    salary_rate_source = None
    if salary_rate is None and w4_settings:
        # Prefer observed rate from stubs if available
        observed = w4_settings.get("observed_salary_rate")
        if observed:
            salary_rate = observed
            salary_rate_source = f"observed from stubs"
        else:
            wh_per_period = calc_withholding_per_period(biweekly_gross, w4_settings)
            salary_rate = wh_per_period / biweekly_gross if biweekly_gross > 0 else 0
            step3 = w4_settings.get("step3_dependents", 0)
            salary_rate_source = f"W-4 calc ({w4_settings.get('filing_status', 'mfj').upper()}, ${step3:,} Step 3)"
    elif salary_rate is None:
        salary_rate = 0.084  # fallback
        salary_rate_source = "default (no W-4 config)"
    else:
        salary_rate_source = "command line"

    # Get bonus - from arg or prior year
    bonus_source = None
    if bonus is None:
        bonus, bonus_source = get_prior_year_bonus(year_int, party)
        if bonus_source is None:
            bonus = 0
            bonus_source = "default (no prior year data)"
    else:
        bonus_source = "command line"

    # Get supplemental rate - from arg, prior year, or IRS minimum
    supp_source = None
    if supplemental_rate is None:
        supplemental_rate, supp_source = get_prior_year_supplemental_rate(year_int, party)
        if supp_source is None:
            supplemental_rate = 0.22  # IRS minimum for supplemental wages
            supp_source = "IRS minimum (no prior year data)"
    else:
        supp_source = "command line"

    # Get RSU vesting data
    total_rsu = 0
    total_shares = 0
    granted_value = 0
    future_value = 0
    granted_shares = 0
    future_shares = 0
    rsu_source = None

    try:
        csv_path = find_latest_export()
        if csv_path is None:
            raise FileNotFoundError("No RSU export found")

        vesting_data = parse_equity_export(csv_path)
        start_date = date(year_int, 1, 1)
        end_date = date(year_int, 12, 31)

        # Already granted RSUs
        granted_vests = get_vesting_in_range(vesting_data, start_date, end_date)
        granted_shares = sum(granted_vests.values())
        granted_value = granted_shares * price

        # Future grant projections
        if future_grant > 0:
            annual_shares = int(future_grant / price)
            future_vests = project_future_grants(annual_shares, year_int, end_date)
            future_shares = sum(future_vests.values())
            future_value = future_shares * price

        total_rsu = granted_value + future_value
        total_shares = granted_shares + future_shares

        if future_grant > 0:
            rsu_source = f"RSU export + ${future_grant:,.0f}/yr future grant"
        else:
            rsu_source = "RSU export (granted only)"

    except Exception as e:
        click.echo(f"Warning: Could not load RSU data: {e}", err=True)
        total_rsu = 0
        rsu_source = "default (RSU load failed)"

    # Calculate totals
    party_total = salary + bonus + total_rsu
    combined_gross = party_total + other_salary
    taxable_income = combined_gross - std_deduction

    # Calculate tax liability
    federal_tax = calc_federal_tax(taxable_income, brackets)

    # Additional Medicare tax (0.9% on wages over $250K MFJ)
    additional_medicare = max(0, combined_gross - 250000) * 0.009

    total_tax = federal_tax + additional_medicare

    # Calculate expected withholding
    party_salary_wh = salary * salary_rate
    party_bonus_wh = bonus * supplemental_rate
    party_rsu_wh = total_rsu * supplemental_rate
    other_wh = other_salary * other_rate

    total_wh = party_salary_wh + party_bonus_wh + party_rsu_wh + other_wh
    shortfall = total_tax - total_wh

    # Calculate recommendation
    pay_periods = 26
    additional_per_period = shortfall / pay_periods if shortfall > 0 else 0
    recommended = math.ceil(additional_per_period / 50) * 50 if additional_per_period > 0 else 0

    # Labels based on party
    party_label = party.capitalize()
    other_label = "Her" if party == "him" else "Him"

    # Output report
    click.echo("=" * 70)
    click.echo(f"{year} WITHHOLDING ANALYSIS ({party_label})")
    click.echo("=" * 70)

    click.echo(f"\nDATA SOURCES")
    click.echo("-" * 50)
    click.echo(f"Salary: {salary_source}")
    click.echo(f"Salary WH rate: {salary_rate_source} ({salary_rate*100:.1f}%)")
    click.echo(f"Bonus: {bonus_source}")
    click.echo(f"RSUs: {rsu_source}")
    click.echo(f"Supplemental rate: {supp_source} ({supplemental_rate*100:.1f}%)")

    click.echo(f"\nINCOME PROJECTION")
    click.echo("-" * 50)
    click.echo(f"{party_label} salary:                      ${salary:>12,.2f}")
    click.echo(f"{party_label} bonus:                       ${bonus:>12,.2f}")
    if total_rsu > 0:
        click.echo(f"{party_label} RSUs ({total_shares} sh @ ${price:.0f}):      ${total_rsu:>12,.2f}")
        if future_value > 0:
            click.echo(f"  (granted: ${granted_value:,.0f}, projected: ${future_value:,.0f})")
    click.echo(f"{party_label} total:                       ${party_total:>12,.2f}")
    click.echo(f"{other_label} salary (unchanged):          ${other_salary:>12,.2f}")
    click.echo(f"Combined gross:                  ${combined_gross:>12,.2f}")
    click.echo(f"Standard deduction ({year}):      ${-std_deduction:>12,.2f}")
    click.echo(f"Taxable income:                  ${taxable_income:>12,.2f}")

    click.echo(f"\nTAX LIABILITY")
    click.echo("-" * 50)
    click.echo(f"Federal income tax:              ${federal_tax:>12,.2f}")
    click.echo(f"Additional Medicare (0.9%):      ${additional_medicare:>12,.2f}")
    click.echo(f"Total tax liability:             ${total_tax:>12,.2f}")

    click.echo(f"\nEXPECTED WITHHOLDING")
    click.echo("-" * 50)
    click.echo(f"{party_label} salary @ {salary_rate*100:.1f}%:              ${party_salary_wh:>12,.2f}")
    if bonus > 0:
        click.echo(f"{party_label} bonus @ {supplemental_rate*100:.0f}%:               ${party_bonus_wh:>12,.2f}")
    if total_rsu > 0:
        click.echo(f"{party_label} RSUs @ {supplemental_rate*100:.0f}%:                ${party_rsu_wh:>12,.2f}")
    click.echo(f"{other_label} salary @ {other_rate*100:.1f}% (unchanged):  ${other_wh:>12,.2f}")
    click.echo(f"Total expected withholding:      ${total_wh:>12,.2f}")

    click.echo(f"\nSHORTFALL:                       ${shortfall:>12,.2f}")

    if shortfall > 0:
        click.echo(f"\nRECOMMENDED W-4 STEP 4(c) ({party_label})")
        click.echo("-" * 50)
        click.echo(f"Extra withholding per paycheck:  ${recommended:>12,.0f}")

        # Alternative: Uniform rate with different supplemental rates
        if party_total > 0 and (bonus > 0 or total_rsu > 0):
            supp_income = bonus + total_rsu
            click.echo(f"\nALTERNATIVE: Adjust supplemental WH rate")
            click.echo("-" * 50)

            for test_supp_rate in [0.22, 0.25, 0.30, 0.35]:
                supp_wh = supp_income * test_supp_rate
                salary_wh_needed = total_tax - other_wh - supp_wh
                additional_needed = salary_wh_needed - party_salary_wh
                additional_per_pay = additional_needed / pay_periods if additional_needed > 0 else 0
                additional_rounded = math.ceil(additional_per_pay / 50) * 50 if additional_per_pay > 0 else 0
                click.echo(f"Supp @ {test_supp_rate*100:.0f}%: extra ${additional_rounded:>6,.0f}/check")
    else:
        click.echo(f"\nNo additional withholding needed - projected refund: ${-shortfall:,.2f}")
