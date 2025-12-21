"""Supplemental value lookup with fallback across years and sources.

Provides a common routine for looking up values that may come from:
1. Prior/other year Form 1040 records
2. Profile YAML tax_years configuration
3. Default values

Search order prioritizes:
- Nearest year first (earlier wins when equidistant)
- 1040 records over YAML for same year
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class SupplementalValue:
    """A value with its source metadata."""
    value: float
    source: str  # "1040", "yaml", or "default"
    year: Optional[str]  # Source year, None for default


def _load_1040_for_year(year: str, data_dir: Path) -> Optional[dict]:
    """Load Form 1040 record for a given year if it exists."""
    from .tax import load_form_1040
    try:
        return load_form_1040(year, data_dir)
    except Exception:
        return None


def _load_profile_yaml() -> dict:
    """Load the profile.yaml configuration."""
    from .config import get_profile_path
    profile_path = get_profile_path(require_exists=False)
    if not profile_path.exists():
        return {}
    with open(profile_path) as f:
        return yaml.safe_load(f) or {}


def _get_nested_value(data: dict, path: str) -> Optional[float]:
    """Get a nested value using dot notation path.

    Args:
        data: The dictionary to query
        path: Dot-separated path like "income.line_2b_taxable_interest"
              or "tax_years.2024.interest_income"

    Returns:
        The value if found, None otherwise
    """
    if not data or not path:
        return None

    keys = path.split(".")
    current = data

    for key in keys:
        if not isinstance(current, dict):
            return None
        if key not in current:
            return None
        current = current[key]

    if current is None:
        return None

    try:
        return float(current)
    except (TypeError, ValueError):
        return None


def _generate_year_search_order(target_year: int, max_distance: int = 10) -> list[int]:
    """Generate list of years to search in priority order.

    Order: same year, then expanding outward with earlier years first.
    Example for 2025: [2025, 2024, 2026, 2023, 2027, 2022, 2028, ...]
    """
    years = [target_year]
    for distance in range(1, max_distance + 1):
        years.append(target_year - distance)  # Earlier first
        years.append(target_year + distance)
    return years


def get_supplemental_value(
    year: str,
    form_1040_path: str,
    yaml_path: str,
    data_dir: Optional[Path] = None,
    default: float = 0.0,
) -> SupplementalValue:
    """Get a supplemental value with fallback across years and sources.

    Searches for a value in this priority order:
    1. Same year 1040
    2. Same year yaml
    3. Prior year 1040 (year - 1)
    4. Prior year yaml
    5. Next year 1040 (year + 1)
    6. Next year yaml
    7. Continue expanding outward...
    8. Default to provided default value

    Args:
        year: Target tax year (e.g., "2025")
        form_1040_path: Dot-path to value in 1040 record
                        e.g., "income.line_2b_taxable_interest"
        yaml_path: Dot-path to value in profile.yaml
                   e.g., "tax_years.{year}.interest_income"
                   Use {year} as placeholder for the year being checked
        data_dir: Directory containing records (defaults to config)
        default: Default value if nothing found (default 0.0)

    Returns:
        SupplementalValue with value and source metadata
    """
    from .config import get_data_path

    if data_dir is None:
        data_dir = get_data_path()

    target_year = int(year)
    search_years = _generate_year_search_order(target_year)

    # Load profile yaml once
    profile = _load_profile_yaml()

    for check_year in search_years:
        check_year_str = str(check_year)

        # Try 1040 first
        form_1040 = _load_1040_for_year(check_year_str, data_dir)
        if form_1040:
            value = _get_nested_value(form_1040, form_1040_path)
            if value is not None:
                logger.debug(
                    f"Found {form_1040_path} = {value} from 1040 year {check_year}"
                )
                return SupplementalValue(value=value, source="1040", year=check_year_str)

        # Try yaml (substitute {year} placeholder)
        yaml_path_resolved = yaml_path.replace("{year}", check_year_str)
        value = _get_nested_value(profile, yaml_path_resolved)
        if value is not None:
            logger.debug(
                f"Found {yaml_path_resolved} = {value} from yaml year {check_year}"
            )
            return SupplementalValue(value=value, source="yaml", year=check_year_str)

    # Nothing found, use default
    logger.debug(
        f"No value found for {form_1040_path}, defaulting to {default}"
    )
    return SupplementalValue(value=default, source="default", year=None)


def get_multiple_supplemental_values(
    year: str,
    lookups: dict[str, tuple[str, str]],
    data_dir: Optional[Path] = None,
) -> dict[str, SupplementalValue]:
    """Get multiple supplemental values in one call.

    Args:
        year: Target tax year
        lookups: Dict mapping field names to (form_1040_path, yaml_path) tuples
        data_dir: Directory containing records

    Returns:
        Dict mapping field names to SupplementalValue objects

    Example:
        lookups = {
            "interest_income": ("income.line_2b_taxable_interest", "tax_years.{year}.interest_income"),
            "dividend_income": ("income.line_3b_ordinary_dividends", "tax_years.{year}.dividend_income"),
        }
        results = get_multiple_supplemental_values("2025", lookups)
    """
    results = {}
    for field_name, (form_path, yaml_path) in lookups.items():
        results[field_name] = get_supplemental_value(
            year, form_path, yaml_path, data_dir
        )
    return results
