"""Party-specific configuration management with effective dates.

Manages configurations that vary by party and have effective dates:
- Supplemental withholding rates
- W-4 settings
- Compensation plans

All configs are stored in ~/.config/pay-calc/{config-type}/{party}.yaml
"""

from datetime import date, datetime
from pathlib import Path
from typing import Optional

import yaml

from .config import get_config_dir


# =============================================================================
# SUPPLEMENTAL WITHHOLDING RATES
# =============================================================================


def _get_suppl_rates_path(party: str) -> Path:
    """Get path to supplemental rates config file for a party."""
    return get_config_dir() / "supplemental-rates" / f"{party}.yaml"


def list_suppl_rates(party: str) -> list[dict]:
    """List all supplemental withholding rates for a party.

    Args:
        party: Party identifier ('him' or 'her')

    Returns:
        List of rate entries sorted by effective_date descending.
        Each entry: {effective_date: str, rate: float}
        Returns empty list if no config exists.
    """
    path = _get_suppl_rates_path(party)

    if not path.exists():
        return []

    with open(path) as f:
        config = yaml.safe_load(f) or {}

    rates = config.get("rates", [])

    # Sort by effective date descending (most recent first)
    return sorted(
        rates,
        key=lambda r: r.get("effective_date", "1900-01-01"),
        reverse=True,
    )


def set_suppl_rate(party: str, rate: float, effective_date: str) -> dict:
    """Set or update a supplemental withholding rate for a party.

    If a rate with the same effective_date exists, it's updated.
    Otherwise, a new entry is added.

    Args:
        party: Party identifier ('him' or 'her')
        rate: Withholding rate (e.g., 0.30 for 30%)
        effective_date: Date rate becomes effective (YYYY-MM-DD)

    Returns:
        Dict with:
            - rates: Updated list of all rates
            - action: 'added' or 'updated'
            - path: Path to config file
    """
    # Validate rate
    if not 0 <= rate <= 1:
        raise ValueError(f"Rate must be between 0 and 1, got {rate}")

    # Validate date format
    try:
        datetime.strptime(effective_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date format '{effective_date}'. Use YYYY-MM-DD.")

    path = _get_suppl_rates_path(party)

    # Load existing config
    if path.exists():
        with open(path) as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}

    rates = config.get("rates", [])

    # Check if date already exists
    action = "added"
    for entry in rates:
        if entry.get("effective_date") == effective_date:
            entry["rate"] = rate
            action = "updated"
            break
    else:
        rates.append({"effective_date": effective_date, "rate": rate})

    config["rates"] = rates

    # Save
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)

    return {
        "rates": list_suppl_rates(party),  # Return sorted
        "action": action,
        "path": str(path),
    }


def delete_suppl_rate(party: str, effective_date: str) -> dict:
    """Delete a supplemental rate entry by effective date.

    Args:
        party: Party identifier
        effective_date: Date of entry to remove

    Returns:
        Dict with rates (remaining) and deleted (bool)
    """
    path = _get_suppl_rates_path(party)

    if not path.exists():
        return {"rates": [], "deleted": False}

    with open(path) as f:
        config = yaml.safe_load(f) or {}

    rates = config.get("rates", [])
    original_len = len(rates)
    rates = [r for r in rates if r.get("effective_date") != effective_date]

    deleted = len(rates) < original_len

    if deleted:
        config["rates"] = rates
        with open(path, "w") as f:
            yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)

    return {
        "rates": list_suppl_rates(party),
        "deleted": deleted,
    }


# =============================================================================
# W-4 CONFIGURATIONS
# =============================================================================


def _get_w4_path(party: str) -> Path:
    """Get path to W-4 config file for a party."""
    return get_config_dir() / "w4s" / f"{party}.yaml"


def list_w4s(party: str) -> list[dict]:
    """List all W-4 configurations for a party.

    Args:
        party: Party identifier ('him' or 'her')

    Returns:
        List of W-4 entries sorted by effective_date descending.
        Each entry: {effective_date, filing_status, allowances, extra_withholding, ...}
    """
    path = _get_w4_path(party)

    if not path.exists():
        return []

    with open(path) as f:
        config = yaml.safe_load(f) or {}

    entries = config.get("w4s", [])

    return sorted(
        entries,
        key=lambda e: e.get("effective_date", "1900-01-01"),
        reverse=True,
    )


def set_w4(
    party: str,
    effective_date: str,
    filing_status: Optional[str] = None,
    allowances: Optional[int] = None,
    extra_withholding: Optional[float] = None,
    multiple_jobs: Optional[bool] = None,
    dependents: Optional[float] = None,
    other_income: Optional[float] = None,
    deductions: Optional[float] = None,
    note: Optional[str] = None,
) -> dict:
    """Set or update W-4 configuration for a party.

    Args:
        party: Party identifier
        effective_date: Date W-4 becomes effective
        filing_status: 'single', 'married', 'head_of_household'
        allowances: Number of allowances (pre-2020 W-4)
        extra_withholding: Additional amount to withhold per period (Step 4c)
        multiple_jobs: Whether multiple jobs checkbox is checked (Step 2c)
        dependents: Annual dependent tax credit claimed (Step 3)
        other_income: Other annual income not from jobs (Step 4a)
        deductions: Deductions exceeding standard deduction (Step 4b)
        note: Optional note about the source of this W-4 config

    Returns:
        Dict with w4s list, action ('added'/'updated'), path
    """
    # Validate date
    try:
        datetime.strptime(effective_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date format '{effective_date}'. Use YYYY-MM-DD.")

    # Validate filing status
    valid_statuses = {"single", "married", "head_of_household"}
    if filing_status and filing_status not in valid_statuses:
        raise ValueError(f"Invalid filing_status '{filing_status}'. Use one of: {valid_statuses}")

    path = _get_w4_path(party)

    # Load existing
    if path.exists():
        with open(path) as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}

    entries = config.get("w4s", [])

    # Find existing entry for date
    existing = None
    for entry in entries:
        if entry.get("effective_date") == effective_date:
            existing = entry
            break

    if existing:
        action = "updated"
        # Update only provided fields
        if filing_status is not None:
            existing["filing_status"] = filing_status
        if allowances is not None:
            existing["allowances"] = allowances
        if extra_withholding is not None:
            existing["extra_withholding"] = extra_withholding
        if multiple_jobs is not None:
            existing["multiple_jobs"] = multiple_jobs
        if dependents is not None:
            existing["dependents"] = dependents
        if other_income is not None:
            existing["other_income"] = other_income
        if deductions is not None:
            existing["deductions"] = deductions
        if note is not None:
            existing["note"] = note
    else:
        action = "added"
        new_entry = {"effective_date": effective_date}
        if filing_status is not None:
            new_entry["filing_status"] = filing_status
        if allowances is not None:
            new_entry["allowances"] = allowances
        if extra_withholding is not None:
            new_entry["extra_withholding"] = extra_withholding
        if multiple_jobs is not None:
            new_entry["multiple_jobs"] = multiple_jobs
        if dependents is not None:
            new_entry["dependents"] = dependents
        if other_income is not None:
            new_entry["other_income"] = other_income
        if deductions is not None:
            new_entry["deductions"] = deductions
        if note is not None:
            new_entry["note"] = note
        entries.append(new_entry)

    config["w4s"] = entries

    # Save
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)

    return {
        "w4s": list_w4s(party),
        "action": action,
        "path": str(path),
    }


# =============================================================================
# COMPENSATION PLANS
# =============================================================================


def _get_comp_path(party: str) -> Path:
    """Get path to compensation plan config file for a party."""
    return get_config_dir() / "comp" / f"{party}.yaml"


def list_comp(party: str) -> list[dict]:
    """List all compensation plan entries for a party.

    Args:
        party: Party identifier

    Returns:
        List of comp entries sorted by effective_date descending.
        Each entry: {effective_date, gross_per_period, regular_pay, employer, ...}
    """
    path = _get_comp_path(party)

    if not path.exists():
        return []

    with open(path) as f:
        config = yaml.safe_load(f) or {}

    entries = config.get("comp", [])

    return sorted(
        entries,
        key=lambda e: e.get("effective_date", "1900-01-01"),
        reverse=True,
    )


def set_comp(
    party: str,
    effective_date: str,
    gross_per_period: Optional[float] = None,
    regular_pay: Optional[float] = None,
    employer: Optional[str] = None,
    source_record: Optional[str] = None,
) -> dict:
    """Set or update a compensation plan entry.

    Args:
        party: Party identifier
        effective_date: Date comp plan becomes effective
        gross_per_period: Total gross per pay period
        regular_pay: Regular pay component per period
        employer: Employer name
        source_record: Record ID this was derived from (optional)

    Returns:
        Dict with comp list, action, path
    """
    # Validate date
    try:
        datetime.strptime(effective_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date format '{effective_date}'. Use YYYY-MM-DD.")

    path = _get_comp_path(party)

    # Load existing
    if path.exists():
        with open(path) as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}

    entries = config.get("comp", [])

    # Find existing
    existing = None
    for entry in entries:
        if entry.get("effective_date") == effective_date:
            existing = entry
            break

    if existing:
        action = "updated"
        if gross_per_period is not None:
            existing["gross_per_period"] = gross_per_period
        if regular_pay is not None:
            existing["regular_pay"] = regular_pay
        if employer is not None:
            existing["employer"] = employer
        if source_record is not None:
            existing["source_record"] = source_record
    else:
        action = "added"
        new_entry = {"effective_date": effective_date}
        if gross_per_period is not None:
            new_entry["gross_per_period"] = gross_per_period
        if regular_pay is not None:
            new_entry["regular_pay"] = regular_pay
        if employer is not None:
            new_entry["employer"] = employer
        if source_record is not None:
            new_entry["source_record"] = source_record
        entries.append(new_entry)

    config["comp"] = entries

    # Save
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)

    return {
        "comp": list_comp(party),
        "action": action,
        "path": str(path),
    }


# =============================================================================
# RESOLUTION HELPERS (for use by other SDK modules)
# =============================================================================


def resolve_suppl_rate_for_date(party: str, target_date) -> Optional[dict]:
    """Find the effective supplemental rate for a specific date.

    Args:
        party: Party identifier
        target_date: Date to find rate for (date object or YYYY-MM-DD string)

    Returns:
        Dict with {rate, effective_date} or None if no rate configured
    """
    if isinstance(target_date, str):
        target_date = datetime.strptime(target_date, "%Y-%m-%d").date()

    rates = list_suppl_rates(party)

    for entry in rates:  # Already sorted descending
        entry_date_str = entry.get("effective_date", "1900-01-01")
        entry_date = datetime.strptime(entry_date_str, "%Y-%m-%d").date()

        if entry_date <= target_date:
            return {
                "rate": entry["rate"],
                "effective_date": entry_date_str,
            }

    return None


def resolve_w4_for_date(party: str, target_date) -> Optional[dict]:
    """Find the effective W-4 configuration for a specific date."""
    if isinstance(target_date, str):
        target_date = datetime.strptime(target_date, "%Y-%m-%d").date()

    w4s = list_w4s(party)

    for entry in w4s:
        entry_date_str = entry.get("effective_date", "1900-01-01")
        entry_date = datetime.strptime(entry_date_str, "%Y-%m-%d").date()

        if entry_date <= target_date:
            return entry

    return None


def resolve_comp_for_date(party: str, target_date) -> Optional[dict]:
    """Find the effective compensation plan for a specific date."""
    if isinstance(target_date, str):
        target_date = datetime.strptime(target_date, "%Y-%m-%d").date()

    comps = list_comp(party)

    for entry in comps:
        entry_date_str = entry.get("effective_date", "1900-01-01")
        entry_date = datetime.strptime(entry_date_str, "%Y-%m-%d").date()

        if entry_date <= target_date:
            return entry

    return None


# =============================================================================
# W-4 DERIVATION FROM STUBS
# =============================================================================


def derive_w4_from_stub(
    record_id: str,
    max_dependents: int = 8,
) -> dict:
    """Derive W-4 settings from a pay stub's actual withholding.

    Systematically searches all W-4 configurations to find one that matches:
    1. Try all combinations of filing status + step2 checkbox + dependents (0-max)
    2. If no exact match, use 0 dependents + extra withholding to force match

    Args:
        record_id: Pay stub record ID
        max_dependents: Maximum number of dependents to try (default 8)

    Returns:
        Dict with:
            - record_id: Source record
            - pay_date: Stub pay date
            - fit_taxable: Current period FIT taxable wages
            - fit_withheld: Current period FIT withheld
            - derived: Dict of derived W-4 settings
            - all_matches: All W-4 configs that match within tolerance
            - analysis: Calculation breakdown
    """
    from .records import get_record
    from .withholding import calc_withholding_per_period

    # IRS 2024+ W-4 credits:
    # - $2000 per qualifying child under 17
    # - $500 per other dependent
    # Search in $500 increments to catch mixed scenarios (e.g., 2 kids + 2 others = $5000)
    CREDIT_INCREMENT = 500
    MAX_CREDITS = max_dependents * 2000

    record = get_record(record_id)
    if not record:
        raise ValueError(f"Record not found: {record_id}")

    meta = record.get("meta", {})
    record_type = meta.get("type")
    if record_type != "stub":
        raise ValueError(f"Record {record_id} is not a stub (type: {record_type})")

    data = record.get("data", {})
    pay_summary = data.get("pay_summary", {})
    current = pay_summary.get("current", {})
    taxes = data.get("taxes", {})

    # Get actual values from stub - check multiple possible locations
    fit = taxes.get("federal_income_tax", {}) or taxes.get("federal_income", {})

    # FIT taxable: try pay_summary.current.fit_taxable_wages first, then taxes.federal_income_tax.taxable_wages
    fit_taxable = current.get("fit_taxable_wages") or fit.get("taxable_wages") or 0

    # FIT withheld: try current_withheld, then current
    fit_withheld = fit.get("current_withheld") or fit.get("current") or 0

    if fit_taxable <= 0:
        raise ValueError(f"No FIT taxable wages found in stub {record_id}")

    pay_date = data.get("pay_date", "")
    year = pay_date[:4] if pay_date else "2025"

    # Calculate effective rate from stub
    effective_rate = (fit_withheld / fit_taxable * 100) if fit_taxable > 0 else 0

    # W-4 configurations to try:
    # - filing_status: 'mfj' (married) or 'single'
    # - step2_checkbox: True = spouse works / multiple jobs (uses higher rates)
    # - step3_dependents: 0 to max_dependents * $2000
    configs_to_try = [
        # (description, filing_status, step2_checkbox)
        ("MFJ, spouse doesn't work", "mfj", False),
        ("MFJ, spouse works (Step 2)", "mfj", True),
        ("Single", "single", False),
    ]

    all_matches = []
    best_match = None

    # Phase 1: Try all filing status / step2 / credit combinations
    # Use $500 increments to catch mixed child + other dependent scenarios
    for desc, filing, step2 in configs_to_try:
        for credits in range(0, MAX_CREDITS + CREDIT_INCREMENT, CREDIT_INCREMENT):
            w4 = {
                "filing_status": filing,
                "pay_frequency": "biweekly",
                "step2_checkbox": step2,
                "step3_dependents": credits,
                "step4c_extra_withholding": 0,
            }

            expected_withholding = calc_withholding_per_period(fit_taxable, w4, year)
            diff = abs(expected_withholding - fit_withheld)

            # Check for match within $2/period tolerance
            if diff < 2:
                match = {
                    "description": desc,
                    "filing_status": filing,
                    "step2_checkbox": step2,
                    "step3_credits": credits,
                    "step4c_extra_withholding": 0,
                    "expected_withholding": round(expected_withholding, 2),
                    "diff": round(diff, 2),
                    "match_type": "exact",
                }
                all_matches.append(match)

                # Keep first (simplest) match as best
                if not best_match:
                    best_match = match

    # Phase 2: If no exact match, force match with 0 dependents + extra withholding
    if not best_match:
        # Try each filing status config with extra withholding
        for desc, filing, step2 in configs_to_try:
            w4_base = {
                "filing_status": filing,
                "pay_frequency": "biweekly",
                "step2_checkbox": step2,
                "step3_dependents": 0,
                "step4c_extra_withholding": 0,
            }

            base_withholding = calc_withholding_per_period(fit_taxable, w4_base, year)

            # Extra needed to match actual (can be positive or negative conceptually,
            # but W-4 Step 4c is only additional withholding - positive values)
            extra_needed = fit_withheld - base_withholding

            if extra_needed >= 0:
                # Round to nearest $10
                extra_rounded = round(extra_needed / 10) * 10

                w4_base["step4c_extra_withholding"] = extra_rounded
                expected_withholding = calc_withholding_per_period(fit_taxable, w4_base, year)
                diff = abs(expected_withholding - fit_withheld)

                if diff < 5:  # Within $5 tolerance
                    match = {
                        "description": f"{desc} + extra withholding",
                        "filing_status": filing,
                        "step2_checkbox": step2,
                        "num_dependents": 0,
                        "step3_credits": 0,
                        "step4c_extra_withholding": extra_rounded,
                        "expected_withholding": round(expected_withholding, 2),
                        "diff": round(diff, 2),
                        "match_type": "forced_extra",
                    }
                    all_matches.append(match)

                    if not best_match:
                        best_match = match
                        break

    # Phase 3: Fallback - raw calculation
    if not best_match:
        # Just report what we observed
        best_match = {
            "description": "No match found",
            "filing_status": "unknown",
            "step2_checkbox": False,
            "num_dependents": None,
            "step3_credits": 0,
            "step4c_extra_withholding": 0,
            "expected_withholding": 0,
            "diff": fit_withheld,
            "match_type": "none",
        }

    # Determine if match is "standard" (multiple of $2000) or "custom"
    is_standard_credits = best_match["step3_credits"] % 2000 == 0
    match_category = "no_match"
    if best_match["match_type"] == "exact":
        match_category = "standard" if is_standard_credits else "custom_credits"
    elif best_match["match_type"] == "forced_extra":
        match_category = "extra_withholding"

    return {
        "record_id": record_id,
        "pay_date": pay_date,
        "party": meta.get("party"),
        "fit_taxable": round(fit_taxable, 2),
        "fit_withheld": round(fit_withheld, 2),
        "effective_rate_pct": round(effective_rate, 2),
        "derived": {
            "filing_status": best_match["filing_status"],
            "step2_checkbox": best_match["step2_checkbox"],
            "step3_credits": best_match["step3_credits"],
            "step4c_extra_withholding": best_match["step4c_extra_withholding"],
            "match_type": best_match["match_type"],
            "match_category": match_category,
            "description": best_match["description"],
        },
        "all_matches": all_matches,
        "analysis": {
            "credit_increments_tried": (MAX_CREDITS // CREDIT_INCREMENT) + 1,
            "configs_tried": len(configs_to_try),
            "total_combinations": len(configs_to_try) * ((MAX_CREDITS // CREDIT_INCREMENT) + 1),
            "matches_found": len(all_matches),
            "max_credits": MAX_CREDITS,
        },
    }
