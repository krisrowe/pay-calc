"""Benefits/deductions resolution.

Resolves benefits deduction settings from multiple sources with fallback chain.
Benefits follow calendar year boundaries (annual enrollment cycles).

Resolution order:
1. Override file (if provided)
2. Prior stub from same year (extract deductions)
3. Registered benefits_plan for target year
4. Most recent prior year stub
5. Registered benefits_plan for prior year
6. Continue backwards until found
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import load_profile, get_data_path


def parse_date(date_str: str) -> date:
    """Parse a date string in YYYY-MM-DD format."""
    if isinstance(date_str, date):
        return date_str
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def get_registered_benefits_plans(party: str) -> List[Dict[str, Any]]:
    """Get registered benefits plan configurations for a party.

    Args:
        party: Party identifier ('him' or 'her')

    Returns:
        List of benefits plans sorted by year (newest first)
    """
    profile = load_profile(require_exists=False)
    parties = profile.get("parties", {})
    party_config = parties.get(party, {})
    plans = party_config.get("benefits_plans", [])

    # Sort by year descending (newest first)
    sorted_plans = sorted(
        plans,
        key=lambda x: x.get("year", 0),
        reverse=True,
    )

    return sorted_plans


def get_benefits_from_plan(year: int, party: str) -> Optional[Dict[str, Any]]:
    """Get benefits plan for a specific year.

    Args:
        year: Calendar year
        party: Party identifier

    Returns:
        Benefits plan dict or None if not found
    """
    plans = get_registered_benefits_plans(party)
    for plan in plans:
        if plan.get("year") == year:
            # Return copy without 'year' key
            return {k: v for k, v in plan.items() if k != "year"}
    return None


def extract_benefits_from_stub(stub: Dict[str, Any]) -> Dict[str, Any]:
    """Extract benefits deduction amounts from a pay stub.

    Looks for common pretax deductions: health, dental, vision, FSA, HSA.

    Args:
        stub: Pay stub dict with deductions

    Returns:
        Benefits dict with deduction amounts
    """
    benefits = {}
    deductions = stub.get("deductions", [])

    # Handle both list and dict formats
    if isinstance(deductions, dict):
        deductions = [{"type": k, **v} if isinstance(v, dict) else {"type": k, "current_amount": v}
                      for k, v in deductions.items()]

    # Map deduction types to benefit keys
    type_mapping = {
        "health": ["health", "medical", "health insurance", "medical insurance"],
        "dental": ["dental", "dental insurance"],
        "vision": ["vision", "vision insurance"],
        "fsa": ["fsa", "flex", "flexible spending"],
        "hsa": ["hsa", "health savings"],
    }

    for ded in deductions:
        ded_type = (ded.get("type") or ded.get("name") or "").lower()
        amount = ded.get("current_amount") or ded.get("amount") or 0

        if amount <= 0:
            continue

        for benefit_key, patterns in type_mapping.items():
            if any(p in ded_type for p in patterns):
                benefits[f"pretax_{benefit_key}"] = amount
                break

    return benefits


def find_latest_stub_for_year(party: str, year: int) -> Optional[Tuple[Dict[str, Any], str]]:
    """Find the latest pay stub for a party in a given year.

    Args:
        party: Party identifier
        year: Calendar year

    Returns:
        Tuple of (stub_dict, pay_date) or None if not found
    """
    data_path = get_data_path()
    analysis_file = data_path / f"{year}_{party}_pay_all.json"

    if not analysis_file.exists():
        return None

    try:
        with open(analysis_file) as f:
            data = json.load(f)

        stubs = data.get("stubs", [])
        if not stubs:
            return None

        # Find latest by pay_date
        latest = max(stubs, key=lambda s: s.get("pay_date", ""))
        pay_date = latest.get("pay_date", "")

        return (latest, pay_date)
    except Exception:
        return None


def resolve_benefits(
    party: str,
    target_date: date,
    override_path: Optional[Path] = None,
    use_actuals: bool = True,
) -> Dict[str, Any]:
    """Resolve benefits deductions for a given date.

    Resolution order (fallback chain):
    1. Override file (if provided)
    2. Prior stub from same year (if use_actuals=True)
    3. Registered benefits_plan for target year
    4. Most recent prior year stub
    5. Registered benefits_plan for prior year
    6. Continue backwards until found or default

    Args:
        party: Party identifier
        target_date: Date to resolve benefits for
        override_path: Optional path to benefits override JSON file
        use_actuals: Whether to check actual stubs (default True)

    Returns:
        Dict with:
            - benefits: Benefits deduction amounts dict
            - source: Source metadata for provenance tracking
    """
    target_year = target_date.year

    # 1. Check override file
    if override_path:
        benefits = load_benefits_file(override_path)
        return {
            "benefits": benefits,
            "source": {
                "type": "override",
                "path": str(override_path),
            },
        }

    # 2. Check prior stub from same year
    if use_actuals:
        result = find_latest_stub_for_year(party, target_year)
        if result:
            stub, pay_date = result
            stub_date = parse_date(pay_date)
            # Only use if stub is before target date
            if stub_date < target_date:
                benefits = extract_benefits_from_stub(stub)
                if benefits:
                    return {
                        "benefits": benefits,
                        "source": {
                            "type": "prior_stub",
                            "stub_date": pay_date,
                            "note": f"Extracted from {target_year} stub",
                        },
                    }

    # 3. Check registered benefits plan for target year
    plan = get_benefits_from_plan(target_year, party)
    if plan:
        return {
            "benefits": plan,
            "source": {
                "type": "registered",
                "year": target_year,
                "note": f"parties.{party}.benefits_plans[{target_year}]",
            },
        }

    # 4-6. Fallback to prior years
    for prior_year in range(target_year - 1, target_year - 5, -1):
        # Try prior year stub
        if use_actuals:
            result = find_latest_stub_for_year(party, prior_year)
            if result:
                stub, pay_date = result
                benefits = extract_benefits_from_stub(stub)
                if benefits:
                    return {
                        "benefits": benefits,
                        "source": {
                            "type": "fallback_stub",
                            "stub_date": pay_date,
                            "year": prior_year,
                            "note": f"Using {prior_year} stub (prior year fallback)",
                        },
                    }

        # Try prior year plan
        plan = get_benefits_from_plan(prior_year, party)
        if plan:
            return {
                "benefits": plan,
                "source": {
                    "type": "fallback_plan",
                    "year": prior_year,
                    "note": f"Using {prior_year} benefits plan (prior year fallback)",
                },
            }

    # 7. No benefits found - return empty
    return {
        "benefits": {},
        "source": {
            "type": "not_found",
            "note": "No benefits configuration found",
        },
    }


def load_benefits_file(path: Path) -> Dict[str, Any]:
    """Load benefits from a JSON file.

    Args:
        path: Path to benefits JSON file

    Returns:
        Benefits dict

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file is invalid JSON
    """
    if not path.exists():
        raise FileNotFoundError(f"Benefits file not found: {path}")

    with open(path) as f:
        data = json.load(f)

    validate_benefits(data)
    return data


def validate_benefits(benefits: Dict[str, Any]) -> None:
    """Validate benefits dict.

    Args:
        benefits: Benefits to validate

    Raises:
        ValueError: If validation fails
    """
    for key, value in benefits.items():
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"Benefit '{key}' must be a non-negative number, got: {value}")


def get_total_pretax_deductions(benefits: Dict[str, Any]) -> float:
    """Calculate total pretax deductions from benefits.

    Args:
        benefits: Benefits dict

    Returns:
        Total pretax deduction amount
    """
    total = 0
    for key, value in benefits.items():
        if key.startswith("pretax_") and isinstance(value, (int, float)):
            total += value
    return total
