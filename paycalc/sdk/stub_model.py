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

from .comp_plan import (
    resolve_comp_plan,
    calc_period_number,
    calc_401k_for_period,
    get_pay_periods_per_year,
)
from .benefits import (
    resolve_benefits,
    get_total_pretax_deductions,
    find_latest_stub_for_year,
)
from .w4 import resolve_w4, merge_w4_with_defaults
from .withholding import (
    calc_period_taxes,
    calc_ss_withholding,
    calc_medicare_withholding,
    truncate_cents,
    round_with_compensation,
)
from .tax import load_tax_rules
from .schemas import (
    validate_comp_plan_override,
    validate_benefits_override,
    validate_w4_override,
    validate_prior_ytd,
)
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
    from .records import list_records

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

    Returns:
        Dict with:
            - pay_date: Target date
            - party: Party identifier
            - period_number: Pay period within year
            - current: Current period amounts
            - ytd: Year-to-date amounts (prior + current)
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
        from .schemas import Benefits
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

    # 401k contribution (use override if provided, otherwise calculate)
    if pretax_401k is None:
        pretax_401k = calc_401k_for_period(gross, comp_plan, ytd_baseline.get("pretax_401k", 0), year)

    # Benefits deductions (Section 125 cafeteria plan)
    # Note: Benefits amounts may vary slightly across periods due to rounding or
    # annual enrollment timing. If exact match is needed, extract from actual stubs
    # rather than using benefits_plan which stores typical per-period amounts.
    pretax_benefits = get_total_pretax_deductions(benefits)

    # Imputed income (e.g., Group Term Life > $50k) - added to gross for tax purposes
    # but not real cash. Use from benefits if not explicitly provided.
    if imputed_income == 0:
        imputed_income = benefits.get("imputed_income", 0)

    # FIT taxable wages (gross minus ALL pretax deductions)
    fit_taxable = gross - pretax_401k - pretax_benefits

    # FICA taxable wages (gross minus Section 125 only, NOT 401k)
    # 401k reduces FIT but not FICA; Section 125 reduces both
    fica_taxable = gross - pretax_benefits

    # Calculate taxes
    taxes = calc_period_taxes(
        fit_taxable=fit_taxable,
        gross=gross,
        w4=w4,
        ytd_ss_wages=ytd_baseline["ss_wages"],
        ytd_medicare_wages=ytd_baseline["medicare_wages"],
        year=year,
        fica_taxable=fica_taxable,
    )

    # Cap FIT at available cash (payroll can't withhold into negative net pay)
    # Available = gross - pretax deductions - FICA - imputed income (GTL is not real cash)
    available_for_fit = gross - pretax_401k - pretax_benefits - taxes["ss"]["withheld"] - taxes["medicare"]["withheld"] - imputed_income
    fit_withheld_raw = taxes["fit_withheld"]
    fit_withheld = min(fit_withheld_raw, max(0, available_for_fit))
    fit_withheld = round(fit_withheld, 2)

    if fit_withheld < fit_withheld_raw:
        fit_shortfall = round(fit_withheld_raw - fit_withheld, 2)
        warnings.append(f"FIT capped at ${fit_withheld:.2f} (${fit_shortfall:.2f} couldn't be withheld - no cash available)")

    # Recalculate total taxes with capped FIT
    total_taxes = fit_withheld + taxes["ss"]["withheld"] + taxes["medicare"]["withheld"]

    # Net pay (imputed income is added to gross but offset - not real cash)
    total_deductions = pretax_401k + pretax_benefits + total_taxes + imputed_income
    net_pay = gross - total_deductions

    # === BUILD CURRENT PERIOD ===

    current = {
        "gross": round(gross, 2),
        "pretax_401k": round(pretax_401k, 2),
        "pretax_benefits": round(pretax_benefits, 2),
        "fit_taxable": round(fit_taxable, 2),
        "fit_withheld": fit_withheld,
        "ss_taxable": taxes["ss"]["taxable"],
        "ss_withheld": taxes["ss"]["withheld"],
        "medicare_taxable": taxes["medicare"]["taxable"],
        "medicare_withheld": taxes["medicare"]["withheld"],
        "total_taxes": round(total_taxes, 2),
        "net_pay": round(net_pay, 2),
    }

    # Add benefits breakdown if available
    for key, value in benefits.items():
        if key.startswith("pretax_"):
            current[key] = round(value, 2)

    # === BUILD YTD ===

    # Add current period to prior YTD baseline
    # Note: SS/Medicare wages use fica_taxable (gross - Section 125), not gross
    ytd = {
        "gross": round(ytd_baseline["gross"] + gross, 2),
        "pretax_401k": round(ytd_baseline["pretax_401k"] + pretax_401k, 2),
        "fit_taxable": round(ytd_baseline["fit_taxable"] + fit_taxable, 2),
        "fit_withheld": round(ytd_baseline["fit_withheld"] + fit_withheld, 2),
        "ss_wages": round(min(
            ytd_baseline["ss_wages"] + fica_taxable,
            taxes["ss"]["wage_cap"]
        ), 2),
        "ss_withheld": round(ytd_baseline["ss_withheld"] + taxes["ss"]["withheld"], 2),
        "medicare_wages": round(ytd_baseline["medicare_wages"] + fica_taxable, 2),
        "medicare_withheld": round(ytd_baseline["medicare_withheld"] + taxes["medicare"]["withheld"], 2),
    }

    # === BUILD SOURCES ===

    sources = {
        "comp_plan": comp_result["source"],
        "w4": w4_result["source"],
        "tax_rules": {"year": year, "path": f"tax-rules/{year}.yaml"},
    }

    # === ADD WARNINGS ===

    if taxes["ss"]["capped"]:
        warnings.append(f"Social Security wage cap reached (${taxes['ss']['wage_cap']:,.0f})")

    if taxes["medicare"]["over_threshold"]:
        warnings.append(f"Additional Medicare tax applies (wages over ${taxes['medicare']['threshold']:,.0f})")

    return {
        "pay_date": date,
        "party": party,
        "period_number": period_number,
        "periods_per_year": periods_per_year,
        "current": current,
        "ytd": ytd,
        "sources": sources,
        "warnings": warnings,
    }


def model_stubs_in_sequence(
    year: int,
    party: str,
    *,
    comp_plan_override: Optional[Dict[str, Any]] = None,
    comp_plan_history: Optional[List[Dict[str, Any]]] = None,
    benefits_override: Optional[Dict[str, Any]] = None,
    w4_override: Optional[Dict[str, Any]] = None,
    pretax_401k: Optional[float] = None,
    supplementals: Optional[List[Dict[str, Any]]] = None,
    special_deductions: Optional[List[Dict[str, Any]]] = None,
    return_last_stub_only: bool = False,
) -> Dict[str, Any]:
    """Model all pay stubs for a calendar year.

    Models the full calendar year, automatically finding a reference pay date
    from the party's latest stub in the prior year (if available).

    This is the correct approach for accurate YTD calculations because it:
    - Properly handles SS wage cap (stops withholding once cap is reached)
    - Properly handles 401k contribution limits
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
        pretax_401k: Override 401k amount per period (applies to ALL periods)
        supplementals: List of supplemental pay stubs (bonuses, RSUs, etc.). Each object
            represents one pay stub. Multiple supplementals may share the same date.
            - date: Pay date (YYYY-MM-DD)
            - gross: Gross amount
            - 401k: (optional) 401k contribution for THIS stub
        special_deductions: List of per-date deduction overrides for regular pay, each with:
            - date: Pay date (YYYY-MM-DD) - MUST align to a regular pay date
            - 401k: 401k contribution for that specific date
        return_last_stub_only: If True, stubs array contains only the final stub

    Returns:
        Dict with:
            - stubs: List of stub results ordered by date (or just last if return_last_stub_only)
            - ytd: Accumulated year-to-date amounts
            - periods_modeled: Number of regular periods iterated
            - supplementals_included: Number of supplemental events processed
            - all_warnings: Aggregated warnings from all periods
    """
    from .config import resolve_supplemental_rate

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
    if benefits is None:
        benefits_result = resolve_benefits(party, target, use_actuals=True)
        benefits = benefits_result.get("benefits", {})
        if not benefits:
            # Use empty benefits with at least one field to satisfy validation
            benefits = {"pretax_health": 0}

    # Generate all pay dates from start of year through target
    pay_dates = generate_pay_dates(target, frequency, ref_date)

    if not pay_dates:
        return {
            "error": f"No pay dates found for year {year}",
        }

    # Build special_deductions lookup (keyed by date string)
    # and validate all dates align to pay dates
    special_deductions_by_date = {}
    if special_deductions:
        pay_date_set = {pd.strftime("%Y-%m-%d") for pd in pay_dates}
        for sd in special_deductions:
            sd_date_str = sd["date"]
            if sd_date_str not in pay_date_set:
                return {
                    "error": f"special_deductions date '{sd_date_str}' does not align to any regular pay date. "
                             f"Valid pay dates: {sorted(pay_date_set)[:5]}{'...' if len(pay_date_set) > 5 else ''}",
                }
            special_deductions_by_date[sd_date_str] = sd

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

    # Fractional cent compensation tracking for FICA taxes.
    # Payroll systems track sub-penny remainders and alternate between truncate/round
    # to keep cumulative error near zero. E.g., if true SS is $394.66596/period:
    #   Period 1: round up to $394.67, remainder = -0.00404
    #   Period 2: truncate to $394.66, remainder = +0.00192
    # This keeps YTD accurate to the penny over many periods.
    # Tracking resets at start of each calendar year (this function models one year).
    fica_remainder = {
        "ss": 0.0,
        "medicare": 0.0,
    }

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

            # Determine 401k for this period:
            # 1. Check special_deductions for this date
            # 2. Fall back to global pretax_401k override
            # 3. Fall back to comp plan calculation (None)
            date_str = event_date.strftime("%Y-%m-%d")
            period_401k = pretax_401k  # Default to global override
            if date_str in special_deductions_by_date:
                period_401k = special_deductions_by_date[date_str].get("401k", period_401k)

            # Build period-specific comp_plan_override
            # If comp_plan_history provided, look up gross for this date
            period_comp_plan = comp_plan_override
            history_gross = get_gross_for_date(event_date)
            if history_gross is not None:
                # Merge history gross with any other comp_plan_override settings
                period_comp_plan = {
                    **(comp_plan_override or {}),
                    "gross_per_period": history_gross,
                }

            # Model this single period
            result = model_stub(
                date_str,
                party,
                prior_ytd=prior_ytd,
                benefits=benefits,
                comp_plan_override=period_comp_plan,
                w4_override=w4_override,
                pretax_401k=period_401k,
            )

            if "error" in result:
                return result

            # Accumulate current period into YTD
            current = result["current"]

            # Apply FICA compensation: recalculate with fractional tracking
            # to match payroll's alternating truncate/round behavior.
            # SS rate is 6.2%, Medicare rate is 1.45%
            ss_taxable = current.get("ss_taxable", current["gross"])
            medicare_taxable = current.get("medicare_taxable", current["gross"])

            raw_ss = ss_taxable * 0.062
            raw_medicare = medicare_taxable * 0.0145

            compensated_ss, fica_remainder["ss"] = round_with_compensation(
                raw_ss, fica_remainder["ss"]
            )
            compensated_medicare, fica_remainder["medicare"] = round_with_compensation(
                raw_medicare, fica_remainder["medicare"]
            )

            # Use compensated values for accumulation and stub tracking
            ytd_accum["gross"] += current["gross"]
            ytd_accum["fit_taxable"] += current["fit_taxable"]
            ytd_accum["fit_withheld"] += current["fit_withheld"]
            ytd_accum["ss_withheld"] += compensated_ss
            ytd_accum["medicare_withheld"] += compensated_medicare
            ytd_accum["pretax_401k"] += current.get("pretax_401k", 0)
            ytd_accum["ss_wages"] += ss_taxable
            ytd_accum["medicare_wages"] += medicare_taxable
            ytd_accum["net_pay"] += current.get("net_pay", 0)

            # Collect warnings
            if result.get("warnings"):
                for w in result["warnings"]:
                    if w not in all_warnings:
                        all_warnings.append(w)

            # Track stub with compensated FICA values
            all_stubs.append({
                "date": date_str,
                "type": "regular",
                "gross": current["gross"],
                "pretax_401k": current.get("pretax_401k", 0),
                "fit_taxable": current["fit_taxable"],
                "fit_withheld": current["fit_withheld"],
                "ss_withheld": compensated_ss,
                "medicare_withheld": compensated_medicare,
                "net_pay": current.get("net_pay", 0),
            })
            regular_periods += 1

        elif event_type == "supplemental":
            # Process supplemental pay (bonus, RSU, etc.)
            # TODO: Apply round_with_compensation here too (see GitHub issue)
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

            # SS: full rate on FICA taxable (respecting cap)
            ss_result = calc_ss_withholding(
                supp_fica_taxable,
                ytd_accum["ss_wages"],
                str(event_date.year),
            )

            # Medicare: full rate on FICA taxable
            medicare_result = calc_medicare_withholding(
                supp_fica_taxable,
                ytd_accum["medicare_wages"],
                str(event_date.year),
            )

            # Accumulate supplemental into YTD
            ytd_accum["gross"] += supp_gross
            ytd_accum["fit_taxable"] += supp_fit_taxable
            ytd_accum["fit_withheld"] += supp_fit
            ytd_accum["ss_wages"] += ss_result["taxable"]
            ytd_accum["ss_withheld"] += ss_result["withheld"]
            ytd_accum["medicare_wages"] += medicare_result["taxable"]
            ytd_accum["medicare_withheld"] += medicare_result["withheld"]
            ytd_accum["pretax_401k"] += supp_401k
            # Supplemental net pay = gross - 401k - all taxes
            supp_net = supp_gross - supp_401k - supp_fit - ss_result["withheld"] - medicare_result["withheld"]
            ytd_accum["net_pay"] += supp_net

            # Add warning about SS cap if reached
            if ss_result["capped"]:
                cap_warning = f"Social Security wage cap reached (${ss_result['wage_cap']:,.0f})"
                if cap_warning not in all_warnings:
                    all_warnings.append(cap_warning)

            # Track supplemental stub
            all_stubs.append({
                "date": event_date.strftime("%Y-%m-%d"),
                "type": "supplemental",
                "gross": supp_gross,
                "pretax_401k": supp_401k,
                "fit_taxable": supp_fit_taxable,
                "fit_withheld": supp_fit,
                "ss_withheld": ss_result["withheld"],
                "medicare_withheld": medicare_result["withheld"],
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
    into special_deductions format. Automatically caps contributions at
    the IRS annual limit from tax rules.

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
    from .tax import get_tax_rule

    year_str = str(year)
    target = date(year, 12, 31)

    # Get 401k annual limit from tax rules (with fallback to prior years)
    try:
        annual_401k_limit = get_tax_rule(year_str, "401k", "employee_elective_limit")
    except KeyError:
        annual_401k_limit = 23500  # Fallback default

    # Get reference pay date from get_first_regular_pay_date
    first_pay_result = get_first_regular_pay_date(party, year)
    if not first_pay_result.get("success"):
        error = first_pay_result.get("error", {})
        return {
            "error": f"Cannot determine pay schedule: {error.get('message', 'no reference stub found')}",
        }
    ref_date = parse_date(first_pay_result["reference_date"])

    # Build special_deductions from regular_401k_contribs config
    special_deductions = None
    if regular_401k_contribs:
        contrib_start = None
        if "starting_date" in regular_401k_contribs:
            contrib_start = parse_date(regular_401k_contribs["starting_date"])
        contrib_amount = regular_401k_contribs.get("amount", 0.0)
        contrib_type = regular_401k_contribs.get("amount_type", "absolute")

        # Determine pay frequency from comp plan
        if comp_plan_override:
            frequency = comp_plan_override.get("pay_frequency", "biweekly")
        else:
            comp_result = resolve_comp_plan(party, target)
            frequency = comp_result["plan"].get("pay_frequency", "biweekly") if comp_result["plan"] else "biweekly"

        # Generate pay dates to build special_deductions
        pay_dates = generate_pay_dates(target, frequency, ref_date)

        special_deductions = []
        cumulative_401k = 0.0

        for pay_date in pay_dates:
            if contrib_start and pay_date >= contrib_start:
                if contrib_type == "percentage":
                    # Get gross to calculate percentage
                    if comp_plan_override:
                        gross = comp_plan_override.get("gross_per_period", 0)
                    else:
                        comp_result = resolve_comp_plan(party, pay_date)
                        gross = comp_result["plan"].get("gross_per_period", 0) if comp_result["plan"] else 0
                    period_401k = round(gross * contrib_amount, 2)
                else:
                    period_401k = contrib_amount

                # Cap at annual limit
                remaining = annual_401k_limit - cumulative_401k
                if period_401k > remaining:
                    period_401k = max(0, remaining)

                cumulative_401k += period_401k

                special_deductions.append({
                    "date": pay_date.strftime("%Y-%m-%d"),
                    "401k": period_401k,
                })

    return model_stubs_in_sequence(
        year,
        party,
        comp_plan_override=comp_plan_override,
        benefits_override=benefits_override,
        w4_override=w4_override,
        special_deductions=special_deductions,
    )


def max_regular_401k_contribs(
    year: int,
    party: str,
    *,
    starting_date: Optional[str] = None,
    comp_plan_override: Optional[Dict[str, Any]] = None,
    benefits_override: Optional[Dict[str, Any]] = None,
    w4_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Model regular pay stubs with max 401k contributions.

    Max 401k = gross - pretax benefits - FICA - imputed income
    This targets $0 net pay.

    Imputed income (e.g., Group Term Life > $50k) is added to gross for tax
    purposes but doesn't represent real cash. It must be subtracted from
    available cash when calculating max 401k.

    Args:
        year: Calendar year to model (e.g., 2025)
        party: Party identifier
        starting_date: Date to start max contributions (YYYY-MM-DD).
            Defaults to first pay date of the year.
        comp_plan_override: Override comp plan dict
        benefits_override: Override benefits dict (uses comp plan if not provided)
        w4_override: Override W-4 dict

    Returns:
        Dict with stubs array, ytd totals, etc.
    """
    from .benefits import resolve_benefits, get_total_pretax_deductions

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
