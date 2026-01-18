"""Compensation plan resolution.

Resolves comp plan settings from registered profiles or override files.
Comp plans have arbitrary effective dates (raises, job changes).
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import load_profile


def parse_date(date_str: str) -> date:
    """Parse a date string in YYYY-MM-DD format."""
    if isinstance(date_str, date):
        return date_str
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def get_registered_comp_plans(party: str) -> List[Dict[str, Any]]:
    """Get registered comp plan configurations for a party.

    Args:
        party: Party identifier ('him' or 'her')

    Returns:
        List of comp plans sorted by effective date (newest first)
    """
    profile = load_profile(require_exists=False)
    parties = profile.get("parties", {})
    party_config = parties.get(party, {})
    comp_plans = party_config.get("comp_plans", [])

    # Sort by effective date descending (newest first)
    sorted_plans = sorted(
        comp_plans,
        key=lambda x: parse_date(x.get("effective", "1900-01-01")),
        reverse=True,
    )

    return sorted_plans


def resolve_comp_plan(
    party: str,
    target_date: date,
    override_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Resolve comp plan effective on a given date.

    Resolution order:
    1. Override file (if provided)
    2. Registered comp plan effective on target_date

    Args:
        party: Party identifier
        target_date: Date to find effective comp plan for
        override_path: Optional path to comp plan override JSON file

    Returns:
        Dict with:
            - plan: Comp plan settings dict
            - source: Source metadata for provenance tracking
    """
    # 1. Check override file
    if override_path:
        plan = load_comp_plan_file(override_path)
        return {
            "plan": plan,
            "source": {
                "type": "override",
                "path": str(override_path),
            },
        }

    # 2. Find registered comp plan effective on target date
    plans = get_registered_comp_plans(party)

    for plan in plans:
        effective = parse_date(plan.get("effective", "1900-01-01"))
        if effective <= target_date:
            # Make a copy without the 'effective' key for plan settings
            settings = {k: v for k, v in plan.items() if k != "effective"}
            return {
                "plan": settings,
                "source": {
                    "type": "registered",
                    "effective": effective.isoformat(),
                    "note": f"parties.{party}.comp_plans",
                },
            }

    # 3. No comp plan found
    return {
        "plan": None,
        "source": {
            "type": "not_found",
            "note": f"No comp plan registered for party '{party}'",
        },
    }


def load_comp_plan_file(path: Path) -> Dict[str, Any]:
    """Load comp plan from a JSON file.

    Args:
        path: Path to comp plan JSON file

    Returns:
        Comp plan dict

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file is invalid JSON or missing required fields
    """
    if not path.exists():
        raise FileNotFoundError(f"Comp plan file not found: {path}")

    with open(path) as f:
        data = json.load(f)

    validate_comp_plan(data)
    return data


def validate_comp_plan(plan: Dict[str, Any]) -> None:
    """Validate comp plan dict.

    Args:
        plan: Comp plan to validate

    Raises:
        ValueError: If validation fails
    """
    # Required fields
    if "gross_per_period" not in plan:
        raise ValueError("Comp plan must include 'gross_per_period'")

    gross = plan["gross_per_period"]
    if not isinstance(gross, (int, float)) or gross <= 0:
        raise ValueError(f"gross_per_period must be a positive number, got: {gross}")

    # pay_frequency must be valid if present
    freq = plan.get("pay_frequency")
    valid_freqs = ("weekly", "biweekly", "semimonthly", "monthly")
    if freq and freq not in valid_freqs:
        raise ValueError(f"Invalid pay_frequency: {freq}. Must be one of {valid_freqs}")

    # 401k can be percentage or fixed amount
    k401 = plan.get("target_401k_pct")
    if k401 is not None:
        if not isinstance(k401, (int, float)) or not (0 <= k401 <= 1):
            raise ValueError(f"target_401k_pct must be between 0 and 1, got: {k401}")

    k401_fixed = plan.get("target_401k_amount")
    if k401_fixed is not None:
        if not isinstance(k401_fixed, (int, float)) or k401_fixed < 0:
            raise ValueError(f"target_401k_amount must be non-negative, got: {k401_fixed}")


def get_pay_periods_per_year(frequency: str) -> int:
    """Get number of pay periods per year for a frequency.

    Args:
        frequency: Pay frequency string

    Returns:
        Number of pay periods per year
    """
    periods = {
        "weekly": 52,
        "biweekly": 26,
        "semimonthly": 24,
        "monthly": 12,
    }
    return periods.get(frequency, 26)


def calc_period_number(target_date: date, frequency: str = "biweekly") -> int:
    """Calculate pay period number for a date within the year.

    Assumes periods start from Jan 1 of the year.

    Args:
        target_date: Date to calculate period for
        frequency: Pay frequency

    Returns:
        Period number (1-based)
    """
    year_start = date(target_date.year, 1, 1)
    days_elapsed = (target_date - year_start).days

    if frequency == "weekly":
        return (days_elapsed // 7) + 1
    elif frequency == "biweekly":
        return (days_elapsed // 14) + 1
    elif frequency == "semimonthly":
        # Approximate: 2 periods per month
        month = target_date.month
        is_second_half = target_date.day > 15
        return (month - 1) * 2 + (2 if is_second_half else 1)
    elif frequency == "monthly":
        return target_date.month
    else:
        # Default to biweekly
        return (days_elapsed // 14) + 1


def calc_401k_for_period(
    gross: float,
    plan: Dict[str, Any],
    ytd_401k: float = 0,
    year: str = "2026",
) -> float:
    """Calculate 401k contribution for a period, respecting annual limit.

    Args:
        gross: Gross pay for the period
        plan: Comp plan with 401k settings
        ytd_401k: Year-to-date 401k contributions before this period
        year: Tax year for limit lookup

    Returns:
        401k contribution amount for the period
    """
    from ..taxes import load_tax_rules

    # Get annual limit
    annual_limit = load_tax_rules(year).retirement_401k.employee_elective_limit

    # Determine target contribution
    if "target_401k_amount" in plan:
        target = plan["target_401k_amount"]
    elif "target_401k_pct" in plan:
        target = gross * plan["target_401k_pct"]
    else:
        target = 0

    # Cap at remaining annual limit
    remaining = max(0, annual_limit - ytd_401k)
    return min(target, remaining)
