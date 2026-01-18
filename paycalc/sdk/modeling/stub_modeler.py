"""Pay stub modeling.

Models a hypothetical pay stub for a given date, calculating all
earnings, deductions, and taxes based on comp plan, benefits, and W-4.

Supports:
- Pure projection from comp plan (default)
- Actual YTD baseline from prior stubs (--use-actuals)
- Override files for comp plan, benefits, W-4
- Individual field overrides (e.g., --pretax-401k 0)
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..employee.comp_plan import (
    resolve_comp_plan,
    calc_period_number,
    calc_401k_for_period,
    get_pay_periods_per_year,
)
from ..employee.benefits import (
    resolve_benefits,
    get_total_pretax_deductions,
    find_latest_stub_for_year,
)
from ..employee.w4 import resolve_w4, merge_w4_with_defaults
from ..taxes.withholding import (
    calc_withholding_per_period,
    calc_ss_withholding,
    calc_medicare_withholding,
    truncate_cents,
    round_with_compensation,
)
from ..taxes.other import load_tax_rules
from ..schemas import (
    validate_comp_plan_override,
    validate_benefits_override,
    validate_w4_override,
    validate_prior_ytd,
    FicaRoundingBalance,
    PaySummary,
    TaxAmounts,
    DeductionTotals,
)
from .schemas import ModelResult, RetirementElectionHistory, RetirementElectionItem
from pydantic import ValidationError


def parse_date(date_str: str) -> date:
    """Parse a date string in YYYY-MM-DD format."""
    if isinstance(date_str, date):
        return date_str
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def get_period_days(frequency: str) -> int:
    """Get number of days between pay periods for a frequency."""
    return {
        "weekly": 7,
        "biweekly": 14,
        "semimonthly": None,  # Variable, handled separately
        "monthly": None,  # Variable, handled separately
    }.get(frequency, 14)


def generate_pay_dates(
    target_date: date,
    frequency: str = "biweekly",
    reference_pay_date: Optional[date] = None,
) -> List[date]:
    """Generate all pay dates from start of year through target date.

    Args:
        target_date: End date (inclusive)
        frequency: Pay frequency
        reference_pay_date: A known pay date to anchor the schedule.
                           If None, assumes first pay date is first Friday
                           of the year for biweekly.

    Returns:
        List of pay dates in chronological order
    """
    year = target_date.year
    year_start = date(year, 1, 1)

    if frequency in ("weekly", "biweekly"):
        period_days = get_period_days(frequency)

        if reference_pay_date:
            # Work backwards/forwards from reference to find first pay date of year
            ref = reference_pay_date
            while ref > year_start:
                ref -= timedelta(days=period_days)
            # ref is now at or before year_start, move forward to first pay date
            while ref < year_start:
                ref += timedelta(days=period_days)
            first_pay_date = ref
        else:
            # Default: first Friday of the year (common pay day)
            first_pay_date = year_start
            while first_pay_date.weekday() != 4:  # 4 = Friday
                first_pay_date += timedelta(days=1)

        # Generate all pay dates through target
        pay_dates = []
        current = first_pay_date
        while current <= target_date:
            pay_dates.append(current)
            current += timedelta(days=period_days)

        return pay_dates

    elif frequency == "semimonthly":
        # 1st and 15th (or next business day), simplified
        pay_dates = []
        for month in range(1, 13):
            for day in (1, 15):
                try:
                    pay_date = date(year, month, day)
                    if pay_date <= target_date:
                        pay_dates.append(pay_date)
                except ValueError:
                    pass
        return pay_dates

    elif frequency == "monthly":
        # Last day of month or 1st, simplified to 1st
        pay_dates = []
        for month in range(1, 13):
            pay_date = date(year, month, 1)
            if pay_date <= target_date:
                pay_dates.append(pay_date)
        return pay_dates

    else:
        # Default to biweekly
        return generate_pay_dates(target_date, "biweekly", reference_pay_date)


def get_ytd_from_stub(stub: Dict[str, Any]) -> Dict[str, float]:
    """Extract YTD values from a pay stub.

    Args:
        stub: Pay stub dict

    Returns:
        Dict with YTD values for gross, taxes, etc.
    """
    pay_summary = stub.get("pay_summary", {})
    ytd = pay_summary.get("ytd", {})
    taxes = stub.get("taxes", {})

    return {
        "gross": ytd.get("gross", 0),
        "fit_taxable": ytd.get("fit_taxable_wages", 0),
        "fit_withheld": taxes.get("federal_income_tax", {}).get("ytd_withheld", 0),
        "ss_wages": taxes.get("social_security", {}).get("ytd_wages", ytd.get("gross", 0)),
        "ss_withheld": taxes.get("social_security", {}).get("ytd_withheld", 0),
        "medicare_wages": taxes.get("medicare", {}).get("ytd_wages", ytd.get("gross", 0)),
        "medicare_withheld": taxes.get("medicare", {}).get("ytd_withheld", 0),
        "pretax_401k": 0,  # Will be calculated from deductions if needed
    }


def get_first_regular_pay_date(party: str, year: Optional[int] = None) -> Dict[str, Any]:
    """Get the first regular pay date of the year for a party.

    Calculates based on:
    1. Most recent regular pay stub for the party (any year) as reference
    2. Comp plan for pay frequency (if available), else default biweekly
    3. Working backwards/forwards from reference to find first pay date of target year

    Args:
        party: Party identifier ('him' or 'her')
        year: Target year (defaults to current year)

    Returns:
        Dict with:
            - success: True if date found, False otherwise
            - date: First pay date string (YYYY-MM-DD) if success
            - frequency: Pay frequency used
            - employer: Employer from stub or comp plan
            - reference_date: The stub date used as reference
            - error: Dict with code and message if not success
              - code: "no_pay_stub" | "other"
              - message: Human-readable error description
    """
    from ..records import list_records

    if year is None:
        year = date.today().year

    # Find most recent regular pay stub for this party (any year)
    # Try current year first, then previous years
    reference_date = None
    employer = ""

    for search_year in [year, year - 1, year - 2]:
        records = list_records(
            year=str(search_year),
            party=party,
            type_filter="stub",
        )

        for rec in records:
            data = rec.get("data", {})
            meta = rec.get("meta", {})
            # Skip supplemental stubs
            if meta.get("is_supplemental"):
                continue
            stub_date = data.get("pay_date")
            if stub_date:
                reference_date = parse_date(stub_date)
                employer = data.get("employer", "")
                break

        if reference_date:
            break

    if not reference_date:
        return {
            "success": False,
            "error": {
                "code": "no_pay_stub",
                "message": f"No regular pay stub found for party '{party}'",
            },
        }

    # Get pay frequency from comp plan if available, else default biweekly
    target_date = date(year, 12, 31)
    comp_result = resolve_comp_plan(party, target_date)

    if comp_result["plan"]:
        frequency = comp_result["plan"].get("pay_frequency", "biweekly")
        # Use comp plan employer if available
        if not employer:
            employer = comp_result["plan"].get("employer", "")
    else:
        frequency = "biweekly"

    # Generate pay dates from reference to find first of year
    pay_dates = generate_pay_dates(
        date(year, 12, 31),
        frequency,
        reference_date,
    )

    if not pay_dates:
        return {
            "success": False,
            "error": {
                "code": "other",
                "message": f"Could not generate pay dates for {year}",
            },
        }

    first_date = pay_dates[0]
    return {
        "success": True,
        "date": first_date.strftime("%Y-%m-%d"),
        "frequency": frequency,
        "employer": employer,
        "reference_date": reference_date.strftime("%Y-%m-%d"),
    }


def model_stub(
    date: str,
    party: str,
    *,
    prior_ytd: Dict[str, float],
    benefits: Dict[str, Any],
    comp_plan_override: Optional[Dict[str, Any]] = None,
    w4_override: Optional[Dict[str, Any]] = None,
    pretax_401k: Optional[float] = None,
    imputed_income: float = 0,
    fica_balance: Optional[FicaRoundingBalance] = None,
) -> Dict[str, Any]:
    """Model a single pay stub given prior YTD values.

    Calculates current period taxes and amounts, then adds to prior YTD.

    Resolution sources (in priority order):
    - Comp plan: override > registered (by effective date)
    - W-4: override > registered (by effective date) > defaults

    Args:
        date: Target pay date (YYYY-MM-DD)
        party: Party identifier ('him' or 'her')
        prior_ytd: Prior period's YTD values (required). For period 1, pass all zeros.
        benefits: Benefits/deductions dict (required). Must have at least one field
            set (e.g., pretax_health=0 if no benefits).
        comp_plan_override: Override comp plan dict
        w4_override: Override W-4 dict
        pretax_401k: Override 401k amount (e.g., 0 to model without 401k)
        imputed_income: Imputed income amount (e.g., Group Term Life)
        fica_balance: FICA rounding balance from prior period. If None, defaults
            to FicaRoundingBalance.none() (zero remainder, no auto-adjust).

    Returns:
        Dict with:
            - pay_date: Target date
            - party: Party identifier
            - period_number: Pay period within year
            - current: Current period amounts
            - ytd: Year-to-date amounts (prior + current)
            - fica_balance: FicaRoundingBalance to pass to next period
            - sources: Provenance tracking for all inputs
            - warnings: List of warning messages
    """
    target_date = parse_date(date)
    year = str(target_date.year)
    warnings = []

    # === VALIDATE INPUTS ===
    # Pydantic validation ensures unknown fields cause errors (no silent ignoring)

    try:
        if comp_plan_override:
            comp_plan_override = validate_comp_plan_override(comp_plan_override)
        if w4_override:
            w4_override = validate_w4_override(w4_override)
    except ValidationError as e:
        # Format pydantic errors for user-friendly output
        errors = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            msg = err["msg"]
            errors.append(f"{loc}: {msg}")
        return {
            "error": f"Invalid override: {'; '.join(errors)}",
            "validation_errors": e.errors(),
        }

    # Validate required benefits (must have at least one field set)
    try:
        from ..schemas import Benefits
        validated_benefits = Benefits.model_validate(benefits)
        benefits = validated_benefits.model_dump(exclude_none=True)
    except ValidationError as e:
        errors = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            msg = err["msg"]
            errors.append(f"{loc}: {msg}")
        return {
            "error": f"Invalid benefits: {'; '.join(errors)}",
            "validation_errors": e.errors(),
        }

    # === RESOLVE INPUTS ===

    # 1. Resolve comp plan
    if comp_plan_override:
        comp_result = {
            "plan": comp_plan_override,
            "source": {"type": "override", "note": "Provided via parameter"},
        }
    else:
        comp_result = resolve_comp_plan(party, target_date)

    comp_plan = comp_result["plan"]
    if not comp_plan:
        return {
            "error": f"No comp plan found for party '{party}' on {date}",
            "sources": {"comp_plan": comp_result["source"]},
        }

    # 2. Benefits already validated above

    # 3. Resolve W-4
    if w4_override:
        w4_result = {
            "settings": w4_override,
            "source": {"type": "override", "note": "Provided via parameter"},
        }
    else:
        w4_result = resolve_w4(party, target_date)

    w4 = merge_w4_with_defaults(w4_result["settings"])

    # 4. Determine pay frequency and period
    frequency = comp_plan.get("pay_frequency", "biweekly")
    periods_per_year = get_pay_periods_per_year(frequency)
    period_number = calc_period_number(target_date, frequency)

    # === VALIDATE PRIOR YTD ===

    try:
        ytd_baseline = validate_prior_ytd(prior_ytd)
    except ValidationError as e:
        errors = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            msg = err["msg"]
            errors.append(f"{loc}: {msg}")
        return {
            "error": f"Invalid prior_ytd: {'; '.join(errors)}",
            "validation_errors": e.errors(),
        }

    # === CALCULATE CURRENT PERIOD ===

    gross = comp_plan["gross_per_period"]

    # 401k contribution target (use override if provided, otherwise calculate)
    if pretax_401k is None:
        pretax_401k_target = calc_401k_for_period(gross, comp_plan, ytd_baseline.get("pretax_401k", 0), year)
    else:
        pretax_401k_target = pretax_401k

    # Benefits deductions (Section 125 cafeteria plan)
    # Note: Benefits amounts may vary slightly across periods due to rounding or
    # annual enrollment timing. If exact match is needed, extract from actual stubs
    # rather than using benefits_plan which stores typical per-period amounts.
    pretax_benefits = get_total_pretax_deductions(benefits)

    # Imputed income (e.g., Group Term Life > $50k) - added to gross for tax purposes
    # but not real cash. Use from benefits if not explicitly provided.
    if imputed_income == 0:
        imputed_income = benefits.get("imputed_income", 0)

    # FICA taxable wages (gross minus Section 125 only, NOT 401k)
    # 401k reduces FIT but not FICA; Section 125 reduces both
    fica_taxable = gross - pretax_benefits

    # === CALCULATE FICA FIRST (doesn't depend on 401k) ===
    # This allows us to cap 401k at available cash before calculating FIT

    ss_result = calc_ss_withholding(fica_taxable, ytd_baseline["ss_wages"], year)
    medicare_result = calc_medicare_withholding(fica_taxable, ytd_baseline["medicare_wages"], year)

    # Apply FICA rounding compensation (IRS Form 941 line 7 / Form 944 line 6)
    # Payroll systems track fractional cents to minimize cumulative rounding error.
    if fica_balance is None:
        fica_balance = FicaRoundingBalance.none()

    # Calculate raw (unrounded) FICA amounts
    raw_ss = ss_result["taxable"] * ss_result["rate"]
    # Medicare: base (1.45%) + additional (0.9% over threshold) on full taxable
    raw_medicare_base = medicare_result["taxable"] * 0.0145
    raw_medicare_additional = medicare_result.get("additional_withheld", 0)
    raw_medicare = raw_medicare_base + raw_medicare_additional

    # Apply round_with_compensation to get compensated values
    ss_withheld, new_ss_remainder = round_with_compensation(raw_ss, fica_balance.ss)
    medicare_withheld, new_medicare_remainder = round_with_compensation(raw_medicare, fica_balance.medicare)

    # Build new fica_balance for next period
    new_fica_balance = FicaRoundingBalance(ss=new_ss_remainder, medicare=new_medicare_remainder)

    # === CAP 401k AT IRS ANNUAL LIMIT ===
    annual_401k_limit = load_tax_rules(year).retirement_401k.employee_elective_limit
    ytd_401k = ytd_baseline.get("pretax_401k", 0)
    remaining_irs_limit = max(0, annual_401k_limit - ytd_401k)
    pretax_401k = min(pretax_401k_target, remaining_irs_limit)

    if pretax_401k < pretax_401k_target and remaining_irs_limit < pretax_401k_target:
        warnings.append(f"401k capped at IRS limit (${annual_401k_limit:,.0f} annual, ${remaining_irs_limit:,.2f} remaining)")

    # === CAP 401k AT AVAILABLE CASH ===
    # Payroll systems prevent 401k from exceeding available funds after FICA
    available_for_401k = gross - pretax_benefits - ss_withheld - medicare_withheld - imputed_income
    pretax_401k_after_irs = pretax_401k
    pretax_401k = min(pretax_401k, max(0, available_for_401k))
    pretax_401k = round(pretax_401k, 2)

    if pretax_401k < pretax_401k_after_irs:
        shortfall = round(pretax_401k_after_irs - pretax_401k, 2)
        warnings.append(f"401k capped at ${pretax_401k:.2f} (${shortfall:.2f} exceeds available cash)")

    # === CALCULATE FIT WITH CAPPED 401k ===
    fit_taxable = gross - pretax_401k - pretax_benefits
    fit_withheld_raw = calc_withholding_per_period(fit_taxable, w4, year)
    fit_withheld_raw = round(fit_withheld_raw, 2)

    # Cap FIT at remaining available cash
    available_for_fit = gross - pretax_401k - pretax_benefits - ss_withheld - medicare_withheld - imputed_income
    fit_withheld = min(fit_withheld_raw, max(0, available_for_fit))
    fit_withheld = round(fit_withheld, 2)

    if fit_withheld < fit_withheld_raw:
        fit_shortfall = round(fit_withheld_raw - fit_withheld, 2)
        warnings.append(f"FIT capped at ${fit_withheld:.2f} (${fit_shortfall:.2f} couldn't be withheld - no cash available)")

    # Recalculate total taxes with capped FIT and compensated FICA
    total_taxes = fit_withheld + ss_withheld + medicare_withheld

    # Net pay (imputed income is added to gross but offset - not real cash)
    total_deductions = pretax_401k + pretax_benefits + total_taxes + imputed_income
    net_pay = gross - total_deductions

    # === BUILD CURRENT PERIOD ===

    # Post-tax deductions: imputed income offset (GTL) is the main one
    # Other post-tax (Roth 401k, after-tax 401k) not currently modeled
    post_tax_deductions = imputed_income

    current = PaySummary(
        gross=round(gross, 2),
        deductions=DeductionTotals(
            fully_pretax=round(pretax_benefits, 2),
            retirement=round(pretax_401k, 2),
            post_tax=round(post_tax_deductions, 2),
        ),
        taxable=TaxAmounts(
            fit=round(fit_taxable, 2),
            ss=round(ss_result["taxable"], 2),
            medicare=round(medicare_result["taxable"], 2),
        ),
        withheld=TaxAmounts(
            fit=fit_withheld,
            ss=ss_withheld,
            medicare=medicare_withheld,
        ),
        net_pay=round(net_pay, 2),
    )

    # === BUILD YTD ===

    # YTD deduction tracking - prior_ytd doesn't have breakdown, so derive from prior totals
    # Prior fully_pretax = prior gross - prior fit_taxable - prior retirement
    # This works because: fit_taxable = gross - fully_pretax - retirement
    prior_retirement = ytd_baseline.get("pretax_401k", 0)
    prior_fully_pretax = ytd_baseline["gross"] - ytd_baseline["fit_taxable"] - prior_retirement
    # Prior post_tax not tracked, assume 0 for now
    prior_post_tax = 0

    ytd_gross = round(ytd_baseline["gross"] + gross, 2)
    ytd_retirement = round(prior_retirement + pretax_401k, 2)
    ytd_fully_pretax = round(prior_fully_pretax + pretax_benefits, 2)
    ytd_post_tax = round(prior_post_tax + post_tax_deductions, 2)
    ytd_fit_taxable = round(ytd_baseline["fit_taxable"] + fit_taxable, 2)
    ytd_ss_wages = round(min(
        ytd_baseline["ss_wages"] + fica_taxable,
        ss_result["wage_cap"]
    ), 2)
    ytd_medicare_wages = round(ytd_baseline["medicare_wages"] + fica_taxable, 2)
    ytd_fit_withheld = round(ytd_baseline["fit_withheld"] + fit_withheld, 2)
    ytd_ss_withheld = round(ytd_baseline["ss_withheld"] + ss_withheld, 2)
    ytd_medicare_withheld = round(ytd_baseline["medicare_withheld"] + medicare_withheld, 2)

    # Calculate YTD net pay
    ytd_total_deductions = ytd_fully_pretax + ytd_retirement + ytd_post_tax
    ytd_total_withheld = ytd_fit_withheld + ytd_ss_withheld + ytd_medicare_withheld
    ytd_net_pay = round(ytd_gross - ytd_total_deductions - ytd_total_withheld, 2)

    ytd = PaySummary(
        gross=ytd_gross,
        deductions=DeductionTotals(
            fully_pretax=ytd_fully_pretax,
            retirement=ytd_retirement,
            post_tax=ytd_post_tax,
        ),
        taxable=TaxAmounts(
            fit=ytd_fit_taxable,
            ss=ytd_ss_wages,
            medicare=ytd_medicare_wages,
        ),
        withheld=TaxAmounts(
            fit=ytd_fit_withheld,
            ss=ytd_ss_withheld,
            medicare=ytd_medicare_withheld,
        ),
        net_pay=ytd_net_pay,
    )

    # === ADD WARNINGS ===

    if ss_result["capped"]:
        warnings.append(f"Social Security wage cap reached (${ss_result['wage_cap']:,.0f})")

    if medicare_result["over_threshold"]:
        warnings.append(f"Additional Medicare tax applies (wages over ${medicare_result['threshold']:,.0f})")

    return ModelResult(
        current=current,
        ytd=ytd,
        fica_balance=new_fica_balance,
        warnings=warnings,
    )


def model_stubs_in_sequence(
    year: int,
    party: str,
    *,
    comp_plan_override: Optional[Dict[str, Any]] = None,
    comp_plan_history: Optional[List[Dict[str, Any]]] = None,
    benefits_override: Optional[Dict[str, Any]] = None,
    w4_override: Optional[Dict[str, Any]] = None,
    supplementals: Optional[List[Dict[str, Any]]] = None,
    retirement_elections: Optional["RetirementElectionHistory"] = None,
    return_last_stub_only: bool = False,
) -> Dict[str, Any]:
    """Model all pay stubs for a calendar year.

    Models the full calendar year, automatically finding a reference pay date
    from the party's latest stub in the prior year (if available).

    This is the correct approach for accurate YTD calculations because it:
    - Properly handles SS wage cap (stops withholding once cap is reached)
    - Properly handles 401k contribution limits (IRS cap + available cash)
    - Accumulates each period's taxes based on actual prior YTD
    - Incorporates supplemental pay (bonuses, RSUs) at correct dates

    Args:
        year: Calendar year to model (e.g., 2025)
        party: Party identifier
        comp_plan_override: Override comp plan dict (used for ALL periods if no history)
        comp_plan_history: List of comp plan entries with effective dates, sorted ascending.
            Each entry has: effective_date, gross_per_period. When provided, the correct
            gross is looked up for each pay date based on effective dates.
        benefits_override: Override benefits dict (resolves from profile if not provided)
        w4_override: Override W-4 dict
        supplementals: List of supplemental pay stubs (bonuses, RSUs, etc.). Each object
            represents one pay stub. Multiple supplementals may share the same date.
            - date: Pay date (YYYY-MM-DD)
            - gross: Gross amount
            - type: (optional) "bonus" or "rsu" - affects 401k election lookup
        retirement_elections: History of 401k contribution elections. Uses the latest
            change effective on or before each pay date. Elections specify desired
            contribution (percentage or absolute); actual contribution is capped at
            IRS limit and available cash by model_stub.
        return_last_stub_only: If True, stubs array contains only the final stub

    Returns:
        Dict with:
            - stubs: List of stub results ordered by date (or just last if return_last_stub_only)
            - ytd: Accumulated year-to-date amounts
            - periods_modeled: Number of regular periods iterated
            - supplementals_included: Number of supplemental events processed
            - all_warnings: Aggregated warnings from all periods
    """
    from ..config import resolve_supplemental_rate

    def get_gross_for_date(pay_date: date) -> Optional[float]:
        """Look up gross_per_period from comp_plan_history for a given date."""
        if not comp_plan_history:
            return None
        # Find the most recent entry effective on or before pay_date
        date_str = pay_date.strftime("%Y-%m-%d")
        applicable = None
        for entry in comp_plan_history:
            if entry["effective_date"] <= date_str:
                applicable = entry
            else:
                break  # History is sorted ascending, stop when we pass the date
        return applicable["gross_per_period"] if applicable else None

    def get_retirement_election(pay_date: date, pay_type: str) -> Optional[RetirementElectionItem]:
        """Look up retirement election for a date and pay type (regular or bonus)."""
        if not retirement_elections or not retirement_elections.changes:
            return None
        # Find the most recent change effective on or before pay_date
        date_str = pay_date.strftime("%Y-%m-%d")
        applicable = None
        for change in retirement_elections.changes:
            if change.date <= date_str:
                applicable = change
            else:
                break  # Changes are in chronological order
        if not applicable:
            return None
        # Return the election for the requested pay type
        if pay_type == "regular":
            return applicable.regular
        elif pay_type == "bonus":
            return applicable.bonus
        return None

    # Target is end of the calendar year
    target = date(year, 12, 31)

    # Get reference pay date from get_first_regular_pay_date
    first_pay_result = get_first_regular_pay_date(party, year)
    if not first_pay_result.get("success"):
        error = first_pay_result.get("error", {})
        return {
            "error": f"Cannot determine pay schedule: {error.get('message', 'no reference stub found')}",
        }
    ref_date = parse_date(first_pay_result["reference_date"])

    # Determine pay frequency from comp plan
    if comp_plan_override:
        frequency = comp_plan_override.get("pay_frequency", "biweekly")
    else:
        comp_result = resolve_comp_plan(party, target)
        if comp_result["plan"]:
            frequency = comp_result["plan"].get("pay_frequency", "biweekly")
        else:
            frequency = "biweekly"

    # Resolve benefits if not provided
    if benefits_override is None:
        benefits_result = resolve_benefits(party, target, use_actuals=True)
        benefits = benefits_result.get("benefits", {})
        if not benefits:
            # Use empty benefits with at least one field to satisfy validation
            benefits = {"pretax_health": 0}
    else:
        benefits = benefits_override

    # Generate all pay dates from start of year through target
    pay_dates = generate_pay_dates(target, frequency, ref_date)

    if not pay_dates:
        return {
            "error": f"No pay dates found for year {year}",
        }

    # Build combined event list: regular pay dates + supplementals
    # Each event: (date, type, data)
    events = []
    for pd in pay_dates:
        events.append((pd, "regular", None))

    supplementals_count = 0
    if supplementals:
        for supp in supplementals:
            supp_date = parse_date(supp["date"])
            # Only include supplementals within the year and up to target
            if supp_date.year == year and supp_date <= target:
                events.append((supp_date, "supplemental", supp))
                supplementals_count += 1

    # Sort events by date
    events.sort(key=lambda e: e[0])

    # Initialize YTD accumulator
    ytd_accum = {
        "gross": 0.0,
        "fit_taxable": 0.0,
        "fit_withheld": 0.0,
        "ss_wages": 0.0,
        "ss_withheld": 0.0,
        "medicare_wages": 0.0,
        "medicare_withheld": 0.0,
        "pretax_401k": 0.0,
        "net_pay": 0.0,
    }

    # Initialize FICA rounding balance (starts fresh each calendar year)
    fica_balance = FicaRoundingBalance.none()

    all_warnings = []
    all_stubs = []
    regular_periods = 0

    # Iterate through each event (regular pay or supplemental)
    for event_date, event_type, event_data in events:
        if event_type == "regular":
            # Build prior YTD from accumulated values
            prior_ytd = {
                "gross": ytd_accum["gross"],
                "fit_taxable": ytd_accum["fit_taxable"],
                "fit_withheld": ytd_accum["fit_withheld"],
                "ss_wages": ytd_accum["ss_wages"],
                "ss_withheld": ytd_accum["ss_withheld"],
                "medicare_wages": ytd_accum["medicare_wages"],
                "medicare_withheld": ytd_accum["medicare_withheld"],
                "pretax_401k": ytd_accum["pretax_401k"],
            }

            # Build period-specific comp_plan_override
            # If comp_plan_history provided, look up gross for this date
            date_str = event_date.strftime("%Y-%m-%d")
            period_comp_plan = comp_plan_override
            history_gross = get_gross_for_date(event_date)
            if history_gross is not None:
                # Merge history gross with any other comp_plan_override settings
                period_comp_plan = {
                    **(comp_plan_override or {}),
                    "gross_per_period": history_gross,
                }

            # Determine desired 401k for this period from retirement elections
            period_401k = None  # Default: let model_stub use comp plan
            election = get_retirement_election(event_date, "regular")
            if election:
                # Get gross for calculating percentage
                if period_comp_plan:
                    gross = period_comp_plan.get("gross_per_period", 0)
                else:
                    cp_result = resolve_comp_plan(party, event_date)
                    gross = cp_result["plan"].get("gross_per_period", 0) if cp_result["plan"] else 0
                # Calculate desired 401k
                if election.amount_type == "percentage":
                    period_401k = round(gross * election.amount, 2)
                else:
                    period_401k = election.amount

            # Model this single period
            result = model_stub(
                date_str,
                party,
                prior_ytd=prior_ytd,
                benefits=benefits,
                comp_plan_override=period_comp_plan,
                w4_override=w4_override,
                pretax_401k=period_401k,
                fica_balance=fica_balance,
            )

            # model_stub returns dict with "error" on failure, ModelResult on success
            if isinstance(result, dict) and "error" in result:
                return result

            # Chain FICA rounding balance to next period
            fica_balance = result.fica_balance

            # Accumulate current period into YTD using PaySummary fields
            current = result.current

            ytd_accum["gross"] += current.gross
            ytd_accum["fit_taxable"] += current.taxable.fit
            ytd_accum["fit_withheld"] += current.withheld.fit
            ytd_accum["ss_withheld"] += current.withheld.ss
            ytd_accum["medicare_withheld"] += current.withheld.medicare
            ytd_accum["pretax_401k"] += current.deductions.retirement
            ytd_accum["ss_wages"] += current.taxable.ss
            ytd_accum["medicare_wages"] += current.taxable.medicare
            ytd_accum["net_pay"] += current.net_pay

            # Collect warnings
            if result.warnings:
                for w in result.warnings:
                    if w not in all_warnings:
                        all_warnings.append(w)

            # Track stub with values from model_stub
            all_stubs.append({
                "date": date_str,
                "type": "regular",
                "gross": current.gross,
                "pretax_401k": current.deductions.retirement,
                "fit_taxable": current.taxable.fit,
                "fit_withheld": current.withheld.fit,
                "ss_withheld": current.withheld.ss,
                "medicare_withheld": current.withheld.medicare,
                "net_pay": current.net_pay,
            })
            regular_periods += 1

        elif event_type == "supplemental":
            # Process supplemental pay (bonus, RSU, etc.)
            supp_gross = event_data["gross"]
            supp_401k = event_data.get("401k", 0)

            # Get supplemental withholding rate
            supp_rate_result = resolve_supplemental_rate(party, event_date)
            supp_fit_rate = supp_rate_result["rate"]

            # FIT taxable = gross minus 401k (if any)
            supp_fit_taxable = supp_gross - supp_401k

            # Supplemental FIT: flat rate on FIT taxable
            supp_fit = truncate_cents(supp_fit_taxable * supp_fit_rate)

            # FICA taxable = gross (401k doesn't reduce FICA)
            supp_fica_taxable = supp_gross

            # SS: get taxable amount and rate (respecting cap)
            ss_result = calc_ss_withholding(
                supp_fica_taxable,
                ytd_accum["ss_wages"],
                str(event_date.year),
            )

            # Medicare: get taxable amount and rates
            medicare_result = calc_medicare_withholding(
                supp_fica_taxable,
                ytd_accum["medicare_wages"],
                str(event_date.year),
            )

            # Apply FICA rounding compensation (IRS Form 941 line 7)
            raw_ss = ss_result["taxable"] * ss_result["rate"]
            raw_medicare = medicare_result["taxable"] * 0.0145 + medicare_result.get("additional_withheld", 0)
            supp_ss_withheld, new_ss_remainder = round_with_compensation(raw_ss, fica_balance.ss)
            supp_medicare_withheld, new_medicare_remainder = round_with_compensation(raw_medicare, fica_balance.medicare)
            fica_balance = FicaRoundingBalance(ss=new_ss_remainder, medicare=new_medicare_remainder)

            # Accumulate supplemental into YTD
            ytd_accum["gross"] += supp_gross
            ytd_accum["fit_taxable"] += supp_fit_taxable
            ytd_accum["fit_withheld"] += supp_fit
            ytd_accum["ss_wages"] += ss_result["taxable"]
            ytd_accum["ss_withheld"] += supp_ss_withheld
            ytd_accum["medicare_wages"] += medicare_result["taxable"]
            ytd_accum["medicare_withheld"] += supp_medicare_withheld
            ytd_accum["pretax_401k"] += supp_401k
            # Supplemental net pay = gross - 401k - all taxes
            supp_net = supp_gross - supp_401k - supp_fit - supp_ss_withheld - supp_medicare_withheld
            ytd_accum["net_pay"] += supp_net

            # Add warning about SS cap if reached
            if ss_result["capped"]:
                cap_warning = f"Social Security wage cap reached (${ss_result['wage_cap']:,.0f})"
                if cap_warning not in all_warnings:
                    all_warnings.append(cap_warning)

            # Track supplemental stub (with compensated FICA values)
            all_stubs.append({
                "date": event_date.strftime("%Y-%m-%d"),
                "type": "supplemental",
                "gross": supp_gross,
                "pretax_401k": supp_401k,
                "fit_taxable": supp_fit_taxable,
                "fit_withheld": supp_fit,
                "ss_withheld": supp_ss_withheld,
                "medicare_withheld": supp_medicare_withheld,
                "net_pay": supp_net,
            })

    # Sort stubs by date (events were already sorted, but be explicit)
    all_stubs.sort(key=lambda s: s["date"])

    # Build return structure
    stubs_to_return = [all_stubs[-1]] if return_last_stub_only and all_stubs else all_stubs

    return {
        "party": party,
        "year": year,
        "stubs": stubs_to_return,
        "ytd": {
            "gross": round(ytd_accum["gross"], 2),
            "fit_taxable": round(ytd_accum["fit_taxable"], 2),
            "fit_withheld": round(ytd_accum["fit_withheld"], 2),
            "ss_wages": round(ytd_accum["ss_wages"], 2),
            "ss_withheld": round(ytd_accum["ss_withheld"], 2),
            "medicare_wages": round(ytd_accum["medicare_wages"], 2),
            "medicare_withheld": round(ytd_accum["medicare_withheld"], 2),
            "pretax_401k": round(ytd_accum["pretax_401k"], 2),
            "net_pay": round(ytd_accum["net_pay"], 2),
        },
        "periods_modeled": regular_periods,
        "supplementals_included": supplementals_count,
        "all_warnings": all_warnings,
    }


def model_regular_401k_contribs(
    year: int,
    party: str,
    *,
    regular_401k_contribs: Optional[Dict[str, Any]] = None,
    comp_plan_override: Optional[Dict[str, Any]] = None,
    benefits_override: Optional[Dict[str, Any]] = None,
    w4_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Model regular pay stubs with configurable 401k contributions.

    Wrapper around model_stubs_in_sequence that converts 401k config
    into RetirementElectionHistory. The elections specify DESIRED contributions;
    model_stub handles capping at IRS limit and available cash.

    Args:
        year: Calendar year to model (e.g., 2025)
        party: Party identifier
        regular_401k_contribs: 401k contribution config:
            - starting_date: Date to start applying contribution (YYYY-MM-DD)
            - amount: Contribution amount
            - amount_type: "absolute" (fixed dollar) or "percentage" (of gross)
        comp_plan_override: Override comp plan dict
        benefits_override: Override benefits dict
        w4_override: Override W-4 dict

    Returns:
        Dict with stubs array, ytd totals, etc.
    """
    from .schemas import RetirementElectionChange

    # Build retirement_elections from regular_401k_contribs config
    retirement_elections = None
    if regular_401k_contribs:
        # Effective date: use starting_date or Jan 1 of the year
        effective_date = regular_401k_contribs.get("starting_date", f"{year}-01-01")
        contrib_amount = regular_401k_contribs.get("amount", 0.0)
        contrib_type = regular_401k_contribs.get("amount_type", "absolute")

        # Create single election change effective from the starting date
        election = RetirementElectionItem(
            type="pretax",
            amount=contrib_amount,
            amount_type=contrib_type,
        )
        change = RetirementElectionChange(
            date=effective_date,
            regular=election,
        )
        retirement_elections = RetirementElectionHistory(changes=[change])

    return model_stubs_in_sequence(
        year,
        party,
        comp_plan_override=comp_plan_override,
        benefits_override=benefits_override,
        w4_override=w4_override,
        retirement_elections=retirement_elections,
    )


def model_401k_max_frontload(
    year: int,
    party: str,
    *,
    starting_date: Optional[str] = None,
    comp_plan_override: Optional[Dict[str, Any]] = None,
    benefits_override: Optional[Dict[str, Any]] = None,
    w4_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Model regular pay stubs with max 401k contributions (ASAP approach).

    Max 401k = gross - pretax benefits - FICA - imputed income
    This targets $0 net pay, hitting the IRS limit as quickly as possible.

    Imputed income (e.g., Group Term Life > $50k) is added to gross for tax
    purposes but doesn't represent real cash. It must be subtracted from
    available cash when calculating max 401k.

    Use Cases:
        1. Mid-year or late-year start: Employee joins late in year and wants
           to max out 401k quickly to capture available employer matching
           before year-end. Spreading contributions evenly would leave
           matching benefits on the table.

        2. Generous matching + supplemental income (rare): Employers offering
           large matching (e.g., 50% on all contributions) combined with
           material supplemental income (bonuses, RSU). Maxing out early
           ensures capturing full employer match before potential separation.
           Additionally, earlier contributions have more time in market to
           grow, potentially gaining thousands in returns vs waiting.

        Note: The typical "50% match on first 6% of income" without regular
        supplemental income does NOT benefit from this approach.

    Limitations:
        - Does NOT factor in 401k contributions from prior W-2 employers
          in the same calendar year. Payroll systems don't see other
          employers' contributions, so neither does this model.
        - Sequence modeling assumes single employer throughout the year.

    Args:
        year: Calendar year to model (e.g., 2025)
        party: Party identifier
        starting_date: Date to start max contributions (YYYY-MM-DD).
            Defaults to first pay date of the year. Use this to model
            mid-year scenarios (e.g., new hire, mid-year decision to max out).
        comp_plan_override: Override comp plan dict
        benefits_override: Override benefits dict (uses comp plan if not provided)
        w4_override: Override W-4 dict

    Returns:
        Dict with stubs array, ytd totals, etc.
    """
    from ..employee.benefits import resolve_benefits, get_total_pretax_deductions

    # Get first pay date
    first_pay_result = get_first_regular_pay_date(party, year)
    if not first_pay_result.get("success"):
        error = first_pay_result.get("error", {})
        return {
            "error": f"Cannot determine pay schedule: {error.get('message', 'no reference stub found')}",
        }

    effective_start = starting_date or first_pay_result["date"]
    start_date_obj = parse_date(effective_start)

    # Get gross from comp plan or override
    if comp_plan_override:
        gross = comp_plan_override.get("gross_per_period", 0)
    else:
        comp_result = resolve_comp_plan(party, start_date_obj)
        gross = comp_result["plan"].get("gross_per_period", 0) if comp_result["plan"] else 0

    # Get benefits from override or resolve (fallback to stubs if no config)
    if benefits_override:
        benefits = benefits_override
    else:
        benefits_result = resolve_benefits(party, start_date_obj, use_actuals=True)
        benefits = benefits_result.get("benefits", {})

    # Calculate max 401k per period = gross - pretax benefits - FICA - imputed income
    # This targets $0 net pay
    total_benefits = get_total_pretax_deductions(benefits)
    imputed_income = benefits.get("imputed_income", 0)

    # FICA is calculated on (gross - section125 benefits), not reduced by 401k
    fica_taxable = gross - total_benefits
    ss_tax = round(fica_taxable * 0.062, 2)  # 6.2% Social Security
    medicare_tax = round(fica_taxable * 0.0145, 2)  # 1.45% Medicare
    total_fica = ss_tax + medicare_tax

    # Imputed income (e.g., GTL) is taxable but not real cash - must subtract
    max_401k_per_period = round(gross - total_benefits - total_fica - imputed_income, 2)

    if max_401k_per_period <= 0:
        return {
            "error": f"No room for 401k: gross ${gross:.2f} - benefits ${total_benefits:.2f} - FICA ${total_fica:.2f} - imputed ${imputed_income:.2f} = ${max_401k_per_period:.2f}",
        }

    return model_regular_401k_contribs(
        year,
        party,
        regular_401k_contribs={
            "starting_date": effective_start,
            "amount": max_401k_per_period,
            "amount_type": "absolute",
        },
        comp_plan_override=comp_plan_override,
        benefits_override=benefits_override,
        w4_override=w4_override,
    )


def model_401k_max_spread_evenly(
    year: int,
    party: str,
    *,
    target_annual: Optional[float] = None,
    comp_plan_override: Optional[Dict[str, Any]] = None,
    benefits_override: Optional[Dict[str, Any]] = None,
    w4_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Model regular pay stubs with 401k spread evenly across all pay periods.

    Divides the target annual amount across all pay periods for the year.
    Rounds per-period amount UP to ensure total meets/exceeds target;
    the IRS cap in model_stub handles the final limit.

    Args:
        year: Calendar year to model (e.g., 2025)
        party: Party identifier
        target_annual: Target annual 401k contribution. Defaults to IRS limit.
        comp_plan_override: Override comp plan dict
        benefits_override: Override benefits dict
        w4_override: Override W-4 dict

    Returns:
        Dict with stubs array, ytd totals, etc.
    """
    import math

    # Get IRS limit for the year
    rules = load_tax_rules(str(year))
    irs_limit = rules.retirement_401k.employee_elective_limit

    # Use IRS limit if no target specified
    annual_target = target_annual if target_annual is not None else irs_limit

    # Get pay frequency from comp plan
    if comp_plan_override:
        frequency = comp_plan_override.get("pay_frequency", "biweekly")
    else:
        first_pay_result = get_first_regular_pay_date(party, year)
        if not first_pay_result.get("success"):
            error = first_pay_result.get("error", {})
            return {
                "error": f"Cannot determine pay schedule: {error.get('message', 'no reference stub found')}",
            }
        frequency = first_pay_result.get("frequency", "biweekly")

    # Get number of pay periods
    periods_per_year = get_pay_periods_per_year(frequency)

    # Calculate per-period amount, rounding UP to 2 decimals to ensure we hit target
    # Example: $23,500 / 26 = $903.846... -> round up to $903.85
    # 26 * $903.85 = $23,500.10 (slightly over, IRS cap will handle)
    raw_per_period = annual_target / periods_per_year
    per_period = math.ceil(raw_per_period * 100) / 100  # Round up to 2 decimals

    return model_regular_401k_contribs(
        year,
        party,
        regular_401k_contribs={
            "starting_date": f"{year}-01-01",
            "amount": per_period,
            "amount_type": "absolute",
        },
        comp_plan_override=comp_plan_override,
        benefits_override=benefits_override,
        w4_override=w4_override,
    )
