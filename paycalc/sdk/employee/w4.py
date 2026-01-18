"""W-4 configuration resolution.

Resolves W-4 settings from registered profiles or override files.
W-4s have arbitrary effective dates (can change anytime).
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import list_w4s


def parse_date(date_str: str) -> date:
    """Parse a date string in YYYY-MM-DD format."""
    if isinstance(date_str, date):
        return date_str
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def get_registered_w4s(party: str) -> List[Dict[str, Any]]:
    """Get registered W-4 configurations for a party.

    Args:
        party: Party identifier ('him' or 'her')

    Returns:
        List of W-4 configs sorted by effective date (newest first)
    """
    # Load from w4s config file (not profile)
    w4s = list_w4s(party)

    # Sort by effective date descending (newest first)
    # When dates are equal, prefer the one that appears later in the file (most recently added)
    sorted_w4s = sorted(
        enumerate(w4s),
        key=lambda x: (parse_date(x[1].get("effective_date", x[1].get("effective", "1900-01-01"))), x[0]),
        reverse=True,
    )

    return [w4 for _, w4 in sorted_w4s]


def resolve_w4(
    party: str,
    target_date: date,
    override_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Resolve W-4 settings effective on a given date.

    Resolution order:
    1. Override file (if provided)
    2. Registered W-4 effective on target_date

    Args:
        party: Party identifier
        target_date: Date to find effective W-4 for
        override_path: Optional path to W-4 override JSON file

    Returns:
        Dict with:
            - settings: W-4 settings dict
            - source: Source metadata for provenance tracking
    """
    # 1. Check override file
    if override_path:
        w4_settings = load_w4_file(override_path)
        return {
            "settings": w4_settings,
            "source": {
                "type": "override",
                "path": str(override_path),
            },
        }

    # 2. Find registered W-4 effective on target date
    w4s = get_registered_w4s(party)

    for w4 in w4s:
        effective = parse_date(w4.get("effective_date", w4.get("effective", "1900-01-01")))
        if effective <= target_date:
            # Make a copy without date-related keys for settings
            settings = {k: v for k, v in w4.items() if k not in ("effective", "effective_date")}
            return {
                "settings": settings,
                "source": {
                    "type": "registered",
                    "effective": effective.isoformat(),
                    "note": f"parties.{party}.w4s",
                },
            }

    # 3. No W-4 found - return defaults
    return {
        "settings": get_default_w4(),
        "source": {
            "type": "default",
            "note": "No W-4 registered for party; using defaults",
        },
    }


def load_w4_file(path: Path) -> Dict[str, Any]:
    """Load W-4 settings from a JSON file.

    Args:
        path: Path to W-4 JSON file

    Returns:
        W-4 settings dict

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file is invalid JSON or missing required fields
    """
    if not path.exists():
        raise FileNotFoundError(f"W-4 file not found: {path}")

    with open(path) as f:
        data = json.load(f)

    # Validate required fields
    validate_w4(data)
    return data


def validate_w4(w4: Dict[str, Any]) -> None:
    """Validate W-4 settings dict.

    Args:
        w4: W-4 settings to validate

    Raises:
        ValueError: If validation fails
    """
    # filing_status must be valid if present
    filing = w4.get("filing_status")
    if filing and filing not in ("mfj", "single", "hoh"):
        raise ValueError(f"Invalid filing_status: {filing}. Must be 'mfj', 'single', or 'hoh'")

    # pay_frequency must be valid if present
    freq = w4.get("pay_frequency")
    valid_freqs = ("weekly", "biweekly", "semimonthly", "monthly")
    if freq and freq not in valid_freqs:
        raise ValueError(f"Invalid pay_frequency: {freq}. Must be one of {valid_freqs}")

    # Numeric fields must be non-negative
    numeric_fields = [
        "step3_dependents",
        "step4a_other_income",
        "step4b_deductions",
        "step4c_extra_withholding",
    ]
    for field in numeric_fields:
        value = w4.get(field)
        if value is not None and (not isinstance(value, (int, float)) or value < 0):
            raise ValueError(f"{field} must be a non-negative number, got: {value}")


def get_default_w4() -> Dict[str, Any]:
    """Get default W-4 settings.

    Returns:
        Default W-4 dict (MFJ, biweekly, no adjustments)
    """
    return {
        "filing_status": "mfj",
        "pay_frequency": "biweekly",
        "step2_checkbox": False,
        "step3_dependents": 0,
        "step4a_other_income": 0,
        "step4b_deductions": 0,
        "step4c_extra_withholding": 0,
    }


def merge_w4_with_defaults(w4: Dict[str, Any]) -> Dict[str, Any]:
    """Merge W-4 settings with defaults for any missing fields.

    Maps user-friendly config file keys to internal keys:
    - extra_withholding -> step4c_extra_withholding
    - multiple_jobs -> step2_checkbox
    - dependents -> step3_dependents
    - filing_status: married -> mfj

    Args:
        w4: Partial W-4 settings (may use either config or internal keys)

    Returns:
        Complete W-4 settings with defaults filled in (internal key format)
    """
    # Map user-friendly config keys to internal keys
    key_mapping = {
        "extra_withholding": "step4c_extra_withholding",
        "multiple_jobs": "step2_checkbox",
        "dependents": "step3_dependents",
        "other_income": "step4a_other_income",
        "deductions": "step4b_deductions",
    }

    # Map filing status values
    filing_status_mapping = {
        "married": "mfj",
        "single": "single",
        "head_of_household": "single",  # Uses single rates
    }

    # Transform input to internal format
    normalized = {}
    for key, value in w4.items():
        if key == "filing_status" and value in filing_status_mapping:
            normalized["filing_status"] = filing_status_mapping[value]
        elif key in key_mapping:
            normalized[key_mapping[key]] = value
        else:
            # Pass through keys that are already in internal format
            normalized[key] = value

    defaults = get_default_w4()
    return {**defaults, **normalized}
