"""Federal income tax withholding calculations.

Implements IRS Pub 15-T Percentage Method for computing FIT withholding
based on W-4 inputs. Core logic used by both stub modeling and withholding
analysis tools.
"""

import math
from typing import Dict, Any, Optional
from .other import load_tax_rules


def truncate_cents(amount: float) -> float:
    """Truncate to 2 decimal places (standard payroll rounding for taxes).

    Payroll systems typically truncate tax withholdings rather than round.
    Example: 394.66596 -> 394.66 (not 394.67)
    """
    return math.floor(amount * 100) / 100


def round_with_compensation(amount: float, accumulated_remainder: float) -> tuple:
    """Round to cents while compensating for accumulated fractional error.

    Payroll systems track fractional cents and alternate between truncate/round
    to keep cumulative error near zero over the year. This mimics that behavior.

    Args:
        amount: Raw calculated amount (e.g., 394.66596)
        accumulated_remainder: Running total of fractional cents from prior periods

    Returns:
        Tuple of (rounded_amount, new_accumulated_remainder)

    Example:
        # Period 1: 394.66596, remainder 0
        round_with_compensation(394.66596, 0)  # -> (394.67, -0.00404)
        # Period 2: 394.66596, remainder -0.00404
        round_with_compensation(394.66596, -0.00404)  # -> (394.66, 0.00192)
    """
    # Add current fractional part to accumulated remainder
    truncated = math.floor(amount * 100) / 100
    fractional = amount - truncated  # e.g., 0.00596
    total_remainder = accumulated_remainder + fractional

    # If remainder >= 0.5 cents, round up and subtract 1 cent from remainder
    if total_remainder >= 0.005:
        return (truncated + 0.01, total_remainder - 0.01)
    else:
        return (truncated, total_remainder)


# Pay periods by frequency
PAY_PERIODS = {
    "weekly": 52,
    "biweekly": 26,
    "semimonthly": 24,
    "monthly": 12,
}

# IRS Pub 15-T 2025 Percentage Method Tables (Annual)
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

# 2026 tables from IRS Pub 15-T (2026)
# Standard Withholding Rate Schedules (Step 2 checkbox NOT checked)
WITHHOLDING_TABLES_2026 = {
    "mfj": [
        (19300, 0, 0.00),         # Standard deduction zone
        (44100, 0, 0.10),         # 10% bracket: $0 + 10% of excess over $19,300
        (117150, 2480, 0.12),     # 12% bracket: $2,480 + 12% of excess over $44,100
        (223700, 11246, 0.22),    # 22% bracket
        (424300, 34687, 0.24),    # 24% bracket
        (545100, 82831, 0.32),    # 32% bracket
        (801050, 121087, 0.35),   # 35% bracket
        (float('inf'), 210670, 0.37)
    ],
    # Form W-4 Step 2 Checkbox tables (when Step 2(c) is checked)
    # These are roughly half the MFJ thresholds
    "single": [
        (9650, 0, 0.00),
        (22050, 0, 0.10),
        (58575, 1240, 0.12),
        (111850, 5623, 0.22),
        (212150, 17343, 0.24),
        (272550, 41415, 0.32),
        (400525, 60543, 0.35),
        (float('inf'), 105335, 0.37)
    ]
}


def get_withholding_tables(year: str) -> Dict[str, list]:
    """Get withholding tables for a given year."""
    if year == "2025":
        return WITHHOLDING_TABLES_2025
    else:
        return WITHHOLDING_TABLES_2026


def get_pay_periods(frequency: str) -> int:
    """Get number of pay periods for a frequency."""
    return PAY_PERIODS.get(frequency, 26)


def calc_withholding_per_period(
    gross_per_period: float,
    w4: Dict[str, Any],
    year: str = "2026",
) -> float:
    """Calculate federal withholding per pay period using IRS Pub 15-T percentage method.

    Args:
        gross_per_period: Gross pay for the period (after pretax deductions for FIT)
        w4: W-4 settings dict with keys:
            - filing_status: 'mfj' or 'single' (default: 'mfj')
            - pay_frequency: 'weekly', 'biweekly', 'semimonthly', 'monthly' (default: 'biweekly')
            - step2_checkbox: bool - Two jobs/spouse works checkbox (default: False)
            - step3_dependents: float - Annual dependent credit (default: 0)
            - step4a_other_income: float - Other income (default: 0)
            - step4b_deductions: float - Deductions beyond standard (default: 0)
            - step4c_extra_withholding: float - Extra withholding per period (default: 0)
        year: Tax year for table lookup (default: '2026')

    Returns:
        Federal withholding amount for the period
    """
    filing = w4.get("filing_status", "mfj")
    freq = w4.get("pay_frequency", "biweekly")
    periods = PAY_PERIODS.get(freq, 26)
    step2_checkbox = w4.get("step2_checkbox", False)

    step3 = w4.get("step3_dependents", 0)
    step4a = w4.get("step4a_other_income", 0)
    step4b = w4.get("step4b_deductions", 0)
    step4c = w4.get("step4c_extra_withholding", 0)

    # Step 1: Annualize wages
    annual_wages = gross_per_period * periods

    # Step 2: Adjust for Step 4(a) and 4(b)
    adjusted_annual = annual_wages + step4a - step4b

    # Step 3: Look up tentative annual withholding from tables
    # If Step 2(c) checkbox is checked, use Single table (halves MFJ brackets)
    tables = get_withholding_tables(year)
    if step2_checkbox and filing == "mfj":
        table = tables["single"]
    else:
        table = tables.get(filing, tables["mfj"])

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


def calc_ss_withholding(
    gross: float,
    ytd_ss_wages: float = 0,
    year: str = "2026",
) -> Dict[str, float]:
    """Calculate Social Security withholding for a period.

    Args:
        gross: Gross pay for the period
        ytd_ss_wages: Year-to-date SS wages before this period
        year: Tax year for wage cap lookup

    Returns:
        Dict with:
            - taxable: SS taxable wages for this period
            - withheld: SS tax withheld
            - rate: SS tax rate used
            - capped: Whether wage cap was reached
    """
    # Load SS rules (cached)
    rules = load_tax_rules(year)
    ss_wage_cap = rules.social_security.wage_cap
    ss_rate = rules.social_security.tax_rate

    # Calculate taxable amount respecting cap
    remaining_cap = max(0, ss_wage_cap - ytd_ss_wages)
    taxable = min(gross, remaining_cap)
    withheld = truncate_cents(taxable * ss_rate)

    return {
        "taxable": taxable,
        "withheld": withheld,
        "rate": ss_rate,
        "capped": ytd_ss_wages + gross > ss_wage_cap,
        "wage_cap": ss_wage_cap,
    }


def calc_medicare_withholding(
    gross: float,
    ytd_medicare_wages: float = 0,
    year: str = "2026",
) -> Dict[str, float]:
    """Calculate Medicare withholding for a period.

    Args:
        gross: Gross pay for the period
        ytd_medicare_wages: Year-to-date Medicare wages before this period
        year: Tax year for threshold lookup

    Returns:
        Dict with:
            - taxable: Medicare taxable wages (always equals gross)
            - base_withheld: Base Medicare tax (1.45%)
            - additional_withheld: Additional Medicare tax (0.9% over threshold)
            - withheld: Total Medicare withheld
            - over_threshold: Whether additional Medicare applies
    """
    MEDICARE_RATE = 0.0145
    ADDITIONAL_RATE = 0.009

    # Additional Medicare threshold (for withholding, per-employee)
    rules = load_tax_rules(year)
    threshold = rules.additional_medicare_withholding_threshold

    base_withheld = truncate_cents(gross * MEDICARE_RATE)

    # Calculate additional Medicare
    new_ytd = ytd_medicare_wages + gross
    if new_ytd > threshold:
        # Wages over threshold this period
        if ytd_medicare_wages >= threshold:
            # Already over threshold, all of this period is additional
            additional_wages = gross
        else:
            # Crossing threshold this period
            additional_wages = new_ytd - threshold
        additional_withheld = truncate_cents(additional_wages * ADDITIONAL_RATE)
    else:
        additional_withheld = 0

    return {
        "taxable": gross,
        "base_withheld": base_withheld,
        "additional_withheld": additional_withheld,
        "withheld": base_withheld + additional_withheld,
        "over_threshold": new_ytd > threshold,
        "threshold": threshold,
    }


def calc_period_taxes(
    fit_taxable: float,
    gross: float,
    w4: Dict[str, Any],
    ytd_ss_wages: float = 0,
    ytd_medicare_wages: float = 0,
    year: str = "2026",
    fica_taxable: Optional[float] = None,
) -> Dict[str, Any]:
    """Calculate all taxes for a single pay period.

    Args:
        fit_taxable: FIT taxable wages (gross minus ALL pretax deductions)
        gross: Gross pay
        w4: W-4 settings
        ytd_ss_wages: Prior YTD SS wages
        ytd_medicare_wages: Prior YTD Medicare wages
        year: Tax year
        fica_taxable: FICA taxable wages (gross minus Section 125 benefits only,
                      NOT minus 401k). If None, defaults to gross for backwards
                      compatibility.

    Returns:
        Dict with fit, ss, medicare breakdown

    Note:
        401k reduces FIT but NOT FICA. Section 125 cafeteria plan benefits
        (health, dental, vision, FSA, HSA) reduce BOTH FIT and FICA.
    """
    # Use fica_taxable for SS/Medicare if provided, otherwise fall back to gross
    fica_wages = fica_taxable if fica_taxable is not None else gross

    fit_withheld = calc_withholding_per_period(fit_taxable, w4, year)
    ss = calc_ss_withholding(fica_wages, ytd_ss_wages, year)
    medicare = calc_medicare_withholding(fica_wages, ytd_medicare_wages, year)

    return {
        "fit_withheld": round(fit_withheld, 2),
        "ss": ss,
        "medicare": medicare,
        "total_withheld": round(fit_withheld + ss["withheld"] + medicare["withheld"], 2),
    }
