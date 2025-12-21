"""Tax projection calculations and output generation."""

import csv
import io
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal, Optional, Union

import yaml


def _get_tax_rules_dir() -> Path:
    """Get the tax-rules directory path."""
    package_root = Path(__file__).parent.parent.parent  # sdk -> paycalc -> project root
    return package_root / "tax-rules"


def _get_available_years() -> list[int]:
    """Get sorted list of available tax rule years (descending)."""
    rules_dir = _get_tax_rules_dir()
    years = [int(p.stem) for p in rules_dir.glob("*.yaml") if p.stem.isdigit()]
    return sorted(years, reverse=True)


def load_tax_rules(year: str) -> dict:
    """Load tax rules for a specific year from tax-rules/YYYY.yaml."""
    config_file = _get_tax_rules_dir() / f"{year}.yaml"
    if not config_file.exists():
        raise FileNotFoundError(f"Tax rules file not found for year {year}: {config_file}")

    with open(config_file, "r") as f:
        return yaml.safe_load(f)


def get_tax_rule(year: str, key: str, nested_key: str = None) -> Any:
    """Get a tax rule value with fallback to prior years.

    Looks for the key in the requested year's rules. If not found, falls back
    to prior years in descending order until a value is found.

    Args:
        year: Tax year to look up (e.g., "2024")
        key: Top-level key in the YAML (e.g., "additional_medicare_withholding_threshold")
        nested_key: Optional nested key (e.g., "wage_cap" under "social_security")

    Returns:
        The value from the YAML file

    Raises:
        KeyError: If no year has the requested key defined
    """
    rules_dir = _get_tax_rules_dir()
    available_years = _get_available_years()
    target_year = int(year)

    # Filter to years <= requested year, sorted descending
    candidate_years = [y for y in available_years if y <= target_year]

    # If no years <= target, try all years in descending order (edge case)
    if not candidate_years:
        candidate_years = available_years

    for check_year in candidate_years:
        config_file = rules_dir / f"{check_year}.yaml"
        if not config_file.exists():
            continue

        with open(config_file, "r") as f:
            rules = yaml.safe_load(f)

        if nested_key:
            if key in rules and isinstance(rules[key], dict) and nested_key in rules[key]:
                return rules[key][nested_key]
        else:
            if key in rules:
                return rules[key]

    # No year has this value defined
    if nested_key:
        raise KeyError(f"Tax rule '{key}.{nested_key}' not defined in any tax-rules/*.yaml file")
    else:
        raise KeyError(f"Tax rule '{key}' not defined in any tax-rules/*.yaml file")


def calculate_federal_income_tax(taxable_income: float, tax_brackets: list) -> float:
    """Calculate federal income tax based on taxable income and tax brackets."""
    tax_owed = 0.0
    previous_bracket_max = 0.0

    sorted_brackets = sorted(tax_brackets, key=lambda b: b.get("up_to", float("inf")))

    for bracket in sorted_brackets:
        rate = bracket["rate"]

        if "up_to" in bracket:
            current_bracket_max = bracket["up_to"]
            if taxable_income > previous_bracket_max:
                income_in_this_bracket = min(taxable_income, current_bracket_max) - previous_bracket_max
                tax_owed += income_in_this_bracket * rate
            previous_bracket_max = current_bracket_max

        elif "over" in bracket:
            if taxable_income > bracket["over"]:
                income_in_this_bracket = taxable_income - bracket["over"]
                tax_owed += income_in_this_bracket * rate

    return tax_owed


def calculate_additional_medicare_withheld(medicare_wages: float, year: str) -> float:
    """Calculate Additional Medicare Tax withheld per W-2 (0.9% of wages over threshold).

    Employers withhold 0.9% on wages over $200k per employee, regardless of
    filing status. This is the Form 8959 Line 25c component.

    Args:
        medicare_wages: Medicare wages from W-2 Box 5
        year: Tax year (for looking up threshold from tax rules)

    Returns:
        Additional Medicare tax withheld amount
    """
    threshold = get_tax_rule(year, "additional_medicare_withholding_threshold")
    if medicare_wages <= threshold:
        return 0.0
    return (medicare_wages - threshold) * 0.009


def calculate_additional_medicare_tax(total_medicare_wages: float, threshold: float) -> float:
    """Calculate the Additional Medicare Tax amount (0.9% of excess wages)."""
    excess_medicare_wages = max(0, total_medicare_wages - threshold)
    return excess_medicare_wages * 0.009


def calculate_ss_overpayment(ss_tax_withheld: float, ss_wage_cap: float, ss_tax_rate: float) -> float:
    """Calculate SS tax overpayment for a single taxpayer.

    When someone has multiple employers, each employer withholds SS independently.
    If total SS wages exceed the cap, excess SS tax should be refunded.

    Args:
        ss_tax_withheld: Total SS tax withheld across all employers
        ss_wage_cap: Maximum wages subject to SS tax (e.g., $176,100 for 2025)
        ss_tax_rate: SS tax rate (e.g., 0.062)

    Returns:
        Excess SS tax paid (credit/refund amount), or 0 if no overpayment
    """
    max_ss_tax = ss_wage_cap * ss_tax_rate
    return max(0, ss_tax_withheld - max_ss_tax)


def load_party_w2_data(
    data_dir: Path,
    year: str,
    party: str,
    allow_projection: bool = False,
    stock_price: Optional[float] = None,
) -> dict:
    """Load W-2 data for a party, generating from stubs if needed.

    Uses generate_w2() which handles per-employer logic:
    1. Official W-2 records (most authoritative)
    2. Latest stub from records (if December, year complete)
    3. Projection from latest stub (if allow_projection and not December)

    Args:
        data_dir: Directory containing W-2/analysis data (unused, kept for compatibility)
        year: Tax year (4 digits)
        party: 'him' or 'her'
        allow_projection: If True and stub data incomplete, include income projection
        stock_price: Stock price for RSU valuation (required with allow_projection for RSU parties)

    Returns:
        dict with keys:
        - data: W-2 box values (wages, withholding, etc.)
        - sources: list of per-employer source descriptions
        - employers: list of per-employer W-2 details
        - projection_warnings: any warnings from projection
    """
    from .w2 import generate_w2

    result = generate_w2(
        year=year,
        party=party,
        allow_projection=allow_projection,
        stock_price=stock_price,
    )

    return {
        "data": result["w2"],
        "sources": result["sources"],
        "employers": result["employers"],
        "projection_warnings": result.get("projection_warnings"),
    }


def generate_projection(
    year: str,
    data_dir: Path = None,
    tax_rules: dict = None,
    allow_projection: bool = False,
    stock_price: Optional[float] = None,
) -> dict:
    """Generate tax projection data for a given year.

    Args:
        year: Tax year (e.g., "2024")
        data_dir: Directory containing W-2 data files. Defaults to XDG data path.
        tax_rules: Optional pre-loaded tax rules (loads from file if not provided)
        allow_projection: If True, allow income projection for incomplete stub data
        stock_price: Stock price for RSU valuation (used with allow_projection)

    Returns:
        Dictionary with all projection data including data_sources metadata
    """
    from .config import get_data_path, get_setting
    from .supplemental import get_multiple_supplemental_values

    if data_dir is None:
        data_dir = get_data_path()

    if tax_rules is None:
        tax_rules = load_tax_rules(year)

    # Get supplemental values (non-wage income, credits, etc.) with source tracking
    supplemental_lookups = {
        "interest_income": ("income.line_2b_taxable_interest", "tax_years.{year}.interest_income"),
        "dividend_income": ("income.line_3b_ordinary_dividends", "tax_years.{year}.dividend_income"),
        "capital_gain_loss": ("income.line_7_capital_gain_loss", "tax_years.{year}.capital_gain_loss"),
        "schedule_1_income": ("income.line_8_schedule_1_income", "tax_years.{year}.schedule_1_income"),
        "qbi_deduction": ("deductions.line_13_qbi_deduction", "tax_years.{year}.qbi_deduction"),
        "child_care_credit": ("schedule_3.part_1.line_2_child_care_credit", "tax_years.{year}.child_care_credit"),
        "niit": ("schedule_2.part_2.line_7_net_investment_income_tax", "tax_years.{year}.niit"),
        "other_taxes": ("schedule_2.part_2.line_18_other_taxes", "tax_years.{year}.other_taxes"),
    }
    supplemental = get_multiple_supplemental_values(year, supplemental_lookups, data_dir)

    # Load W-2 data for both parties (with source tracking)
    him_result = load_party_w2_data(
        data_dir, year, "him",
        allow_projection=allow_projection,
        stock_price=stock_price,
    )
    her_result = load_party_w2_data(
        data_dir, year, "her",
        allow_projection=allow_projection,
        stock_price=stock_price,
    )

    him_data = him_result["data"]
    her_data = her_result["data"]

    him_wages = him_data.get("wages", 0)
    her_wages = her_data.get("wages", 0)
    him_fed_withheld = him_data.get("federal_tax_withheld", 0)
    her_fed_withheld = her_data.get("federal_tax_withheld", 0)
    him_medicare_wages = him_data.get("medicare_wages", him_wages)
    her_medicare_wages = her_data.get("medicare_wages", her_wages)
    him_medicare_withheld = him_data.get("medicare_tax", 0)
    her_medicare_withheld = her_data.get("medicare_tax", 0)
    him_ss_withheld = him_data.get("social_security_tax", 0)
    her_ss_withheld = her_data.get("social_security_tax", 0)

    combined_wages = him_wages + her_wages
    combined_medicare_wages = him_medicare_wages + her_medicare_wages
    combined_medicare_withheld = him_medicare_withheld + her_medicare_withheld

    # Calculate total income including supplemental (non-wage) income
    non_wage_income = (
        supplemental["interest_income"].value
        + supplemental["dividend_income"].value
        + supplemental["capital_gain_loss"].value  # Can be negative
        + supplemental["schedule_1_income"].value
    )
    total_income = combined_wages + non_wage_income

    # Tax rules structure: mfj.standard_deduction, mfj.tax_brackets, additional_medicare_tax_threshold
    standard_deduction = tax_rules["mfj"]["standard_deduction"]
    qbi_deduction = supplemental["qbi_deduction"].value
    total_deductions = standard_deduction + qbi_deduction
    final_taxable_income = max(0, total_income - total_deductions)

    federal_income_tax_assessed = calculate_federal_income_tax(
        final_taxable_income, tax_rules["mfj"]["tax_brackets"]
    )

    medicare_threshold = tax_rules["additional_medicare_tax_threshold"]
    additional_medicare_tax = calculate_additional_medicare_tax(
        combined_medicare_wages, medicare_threshold
    )
    base_medicare_rate = 0.0145
    base_medicare_tax = combined_medicare_wages * base_medicare_rate
    total_medicare_taxes_assessed = base_medicare_tax + additional_medicare_tax
    medicare_refund = combined_medicare_withheld - total_medicare_taxes_assessed

    # Calculate SS overpayment (when multiple employers cause over-withholding)
    ss_rules = tax_rules.get("social_security", {})
    ss_wage_cap = ss_rules.get("wage_cap")
    ss_tax_rate = ss_rules.get("tax_rate")
    ss_defaults_used = []
    if ss_wage_cap is None:
        ss_wage_cap = 184500  # 2026 default
        ss_defaults_used.append(f"SS wage cap defaulted to $184,500 (2026) - add social_security.wage_cap to tax-rules/{year}.yaml")
    if ss_tax_rate is None:
        ss_tax_rate = 0.062
        ss_defaults_used.append(f"SS tax rate defaulted to 6.2% - add social_security.tax_rate to tax-rules/{year}.yaml")

    # Check if SS overpayment is disabled (for debugging)
    disable_ss_overpayment = get_setting("disable_ss_overpayment", False)
    if disable_ss_overpayment:
        ss_defaults_used.append("SS overpayment calculation disabled via settings.json (disable_ss_overpayment: true)")
        him_ss_overpayment = 0.0
        her_ss_overpayment = 0.0
    else:
        him_ss_overpayment = calculate_ss_overpayment(him_ss_withheld, ss_wage_cap, ss_tax_rate)
        her_ss_overpayment = calculate_ss_overpayment(her_ss_withheld, ss_wage_cap, ss_tax_rate)
    total_ss_overpayment = him_ss_overpayment + her_ss_overpayment

    # Form 8959 withholding (Additional Medicare withheld at $200k per-employee threshold)
    # This is credited as a payment on Line 25c of Form 1040
    him_additional_medicare_withheld = calculate_additional_medicare_withheld(him_medicare_wages, year)
    her_additional_medicare_withheld = calculate_additional_medicare_withheld(her_medicare_wages, year)
    form_8959_withholding = him_additional_medicare_withheld + her_additional_medicare_withheld

    # Credits and additional taxes from supplemental sources
    child_care_credit = supplemental["child_care_credit"].value
    niit = supplemental["niit"].value
    other_taxes = supplemental["other_taxes"].value
    additional_taxes = niit + other_taxes

    # Calculate total tax (Line 24)
    # Line 24 = Line 16 (income tax) + Schedule 2 taxes - credits
    # Note: Base Medicare (1.45%) is payroll tax, not on 1040 Line 24
    # Only Additional Medicare Tax (0.9% on wages over $250k MFJ) goes to Schedule 2
    tentative_tax_per_return = (
        federal_income_tax_assessed          # Line 16
        + additional_medicare_tax            # Schedule 2 Line 6
        + additional_taxes                   # NIIT (Line 7) + Other taxes (Line 18)
        - child_care_credit                  # Schedule 3 credits
    )
    total_withheld = him_fed_withheld + her_fed_withheld
    # SS overpayment and Form 8959 withholding are credits that increase refund
    final_refund = total_withheld - tentative_tax_per_return + total_ss_overpayment + form_8959_withholding

    # Build data sources with per-employer details
    data_sources = {
        "him": {
            "sources": him_result["sources"],
            "employers": him_result["employers"],
        },
        "her": {
            "sources": her_result["sources"],
            "employers": her_result["employers"],
        },
    }

    # Include projection warnings if present
    if him_result.get("projection_warnings"):
        data_sources["him"]["projection_warnings"] = him_result["projection_warnings"]
    if her_result.get("projection_warnings"):
        data_sources["her"]["projection_warnings"] = her_result["projection_warnings"]

    # Convert supplemental values to dicts with source metadata
    def _supp_to_dict(sv):
        return {"value": sv.value, "source": sv.source, "year": sv.year}

    result = {
        "year": year,
        "him_wages": him_wages,
        "her_wages": her_wages,
        "him_fed_withheld": him_fed_withheld,
        "her_fed_withheld": her_fed_withheld,
        "combined_wages": combined_wages,
        "non_wage_income": non_wage_income,
        "total_income": total_income,
        "standard_deduction": standard_deduction,
        "qbi_deduction": qbi_deduction,
        "total_deductions": total_deductions,
        "final_taxable_income": final_taxable_income,
        "tax_brackets": tax_rules["mfj"]["tax_brackets"],
        "federal_income_tax_assessed": federal_income_tax_assessed,
        "combined_medicare_wages": combined_medicare_wages,
        "combined_medicare_withheld": combined_medicare_withheld,
        "total_medicare_taxes_assessed": total_medicare_taxes_assessed,
        "additional_medicare_tax": additional_medicare_tax,  # Schedule 2 Line 6
        "medicare_refund": medicare_refund,
        "him_ss_withheld": him_ss_withheld,
        "her_ss_withheld": her_ss_withheld,
        "him_ss_overpayment": him_ss_overpayment,
        "her_ss_overpayment": her_ss_overpayment,
        "total_ss_overpayment": total_ss_overpayment,
        "him_additional_medicare_withheld": him_additional_medicare_withheld,
        "her_additional_medicare_withheld": her_additional_medicare_withheld,
        "form_8959_withholding": form_8959_withholding,
        "child_care_credit": child_care_credit,
        "niit": niit,
        "other_taxes": other_taxes,
        "additional_taxes": additional_taxes,
        "tentative_tax_per_return": tentative_tax_per_return,
        "final_refund": final_refund,
        "data_sources": data_sources,
        # Supplemental values with source tracking
        "supplemental": {
            name: _supp_to_dict(sv) for name, sv in supplemental.items()
        },
    }
    if ss_defaults_used:
        result["ss_warnings"] = ss_defaults_used
    return result


def _write_projection_rows(writer, projection: dict) -> None:
    """Write tax projection rows to a CSV writer.

    Internal function used by both file and string CSV generation.
    """
    writer.writerow(["", "", "INCOME TAX BRACKETS (MFJ)", "", "", "HIM", ""])
    writer.writerow(["", "", "Applied to income of", f'${projection["final_taxable_income"]:,.2f}', "", "Wages:", f'${projection["him_wages"]:,.2f}'])
    writer.writerow(["", "Earnings Above", "Rate / Bracket", "Tax Assessed", "", "Fed Tax Withheld:", f'${projection["him_fed_withheld"]:,.2f}'])

    previous_bracket_max = 0
    for bracket in projection["tax_brackets"]:
        rate = bracket["rate"]
        row = ["", "", "", ""]

        if "up_to" in bracket:
            row[1] = f"${previous_bracket_max:,.2f}"
            current_bracket_max = bracket["up_to"]
            income_in_bracket = min(projection["final_taxable_income"], current_bracket_max) - previous_bracket_max
            if income_in_bracket < 0:
                income_in_bracket = 0
            tax_assessed = income_in_bracket * rate
            row[3] = f"${tax_assessed:,.2f}"
            previous_bracket_max = current_bracket_max
        elif "over" in bracket:
            row[1] = f'${bracket["over"]:,.2f}'
            if projection["final_taxable_income"] > bracket["over"]:
                income_in_bracket = projection["final_taxable_income"] - bracket["over"]
                tax_assessed = income_in_bracket * rate
            else:
                tax_assessed = 0
            row[3] = f"${tax_assessed:,.2f}"

        row[2] = f"{rate:.0%}"
        writer.writerow(row)

    writer.writerow(["", "", "Total Assessed", f'${projection["federal_income_tax_assessed"]:,.2f}', "", "", ""])
    writer.writerow([])

    writer.writerow(["", "", "", "", "", "HER", ""])
    writer.writerow(["", "", "", "", "", "Wages:", f'${projection["her_wages"]:,.2f}'])
    writer.writerow(["", "", "", "", "", "Fed Tax Withheld:", f'${projection["her_fed_withheld"]:,.2f}'])
    writer.writerow([])

    writer.writerow(["", "", "", "", "", "TAXABLE INCOME", ""])
    writer.writerow(["", "", "", "", "", "His wages per W-2", f'${projection["him_wages"]:,.2f}'])
    writer.writerow(["", "", "", "", "", "Her wages per W-2", f'${projection["her_wages"]:,.2f}'])
    writer.writerow(["", "", "", "", "", "Combined gross income", f'${projection["combined_wages"]:,.2f}'])
    writer.writerow(["", "", "", "", "", "Standard deduction", f'-${projection["standard_deduction"]:,.2f}'])
    writer.writerow(["", "", "", "", "", "Taxable income", f'${projection["final_taxable_income"]:,.2f}'])
    writer.writerow([])

    writer.writerow(["", "", "", "", "", "MEDICARE TAXES OVER OR UNDERPAID", ""])
    writer.writerow(["", "", "", "", "", "Total medicare wages (his and hers)", f'${projection["combined_medicare_wages"]:,.2f}'])
    writer.writerow(["", "", "", "", "", "Total medicare taxes withheld", f'${projection["combined_medicare_withheld"]:,.2f}'])
    writer.writerow(["", "", "", "", "", "Total medicare taxes assessed", f'-${projection["total_medicare_taxes_assessed"]:,.2f}'])
    writer.writerow(["", "", "", "", "", "Refund on medicare taxes withheld (or amount owed if negative)", f'${projection["medicare_refund"]:,.2f}'])
    writer.writerow([])

    writer.writerow(["", "", "", "", "", "TAX RETURN / REFUND PROJECTION", ""])
    writer.writerow(["", "", "", "", "", "Federal Income Tax", f'-${projection["federal_income_tax_assessed"]:,.2f}'])
    writer.writerow(["", "", "", "", "", "Additional Medicare Tax", f'${projection["medicare_refund"]:,.2f}'])
    writer.writerow(["", "", "", "", "", "Tentative tax per tax return", f'-${projection["tentative_tax_per_return"]:,.2f}'])
    writer.writerow(["", "", "", "", "", "His income tax withheld", f'${projection["him_fed_withheld"]:,.2f}'])
    writer.writerow(["", "", "", "", "", "Her income tax withheld", f'${projection["her_fed_withheld"]:,.2f}'])
    writer.writerow(["", "", "", "", "", "Refund (or owed, if negative)", f'${projection["final_refund"]:,.2f}'])


def projection_to_csv_string(projection: dict) -> str:
    """Convert tax projection to CSV string.

    Args:
        projection: Projection data from generate_projection()

    Returns:
        CSV formatted string
    """
    output = io.StringIO()
    writer = csv.writer(output)
    _write_projection_rows(writer, projection)
    return output.getvalue()


def write_projection_csv(projection: dict, output_path: Path) -> Path:
    """Write tax projection to CSV file.

    Args:
        projection: Projection data from generate_projection()
        output_path: Path to output CSV file

    Returns:
        Path to the written file
    """
    with open(output_path, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        _write_projection_rows(writer, projection)

    return output_path


def format_data_sources(data_sources: dict) -> str:
    """Format data sources metadata for display.

    Used by CLI for text output and stderr output for CSV/JSON.

    Args:
        data_sources: The data_sources dict from generate_projection()

    Returns:
        Multi-line string describing data sources
    """
    lines = []

    for party_key in ["him", "her"]:
        party_info = data_sources.get(party_key, {})
        party_label = "Him" if party_key == "him" else "Her"

        # Show per-employer sources
        sources = party_info.get("sources", [])
        employers = party_info.get("employers", [])

        if sources:
            lines.append(f"{party_label}:")
            for source_desc in sources:
                lines.append(f"  - {source_desc}")

            # Show projection warnings if present
            proj_warnings = party_info.get("projection_warnings", [])
            if proj_warnings:
                for w in proj_warnings:
                    lines.append(f"  ⚠ {w}")
        else:
            lines.append(f"{party_label}: no data")

    return "\n".join(lines)


def generate_tax_projection(
    year: str,
    data_dir: Optional[Path] = None,
    output_format: Literal["json", "csv"] = "json",
    allow_projection: bool = False,
    stock_price: Optional[float] = None,
) -> Union[dict, str]:
    """Generate tax projection data.

    Main entry point for tax projection. Returns structured data (json)
    or formatted CSV string.

    Args:
        year: Tax year (e.g., "2024")
        data_dir: Directory containing W-2 data files. Defaults to XDG data path.
        output_format: "json" returns dict (default), "csv" returns CSV string.
        allow_projection: If True and stub data incomplete, include income projection
        stock_price: Stock price for RSU valuation (used with allow_projection)

    Returns:
        dict (json format) or str (csv format)
        - JSON includes data_sources with source, detail, and projection_info
        - CSV is formatted for spreadsheet import
    """
    from .config import get_data_path

    if data_dir is None:
        data_dir = get_data_path()

    projection = generate_projection(
        year,
        data_dir,
        allow_projection=allow_projection,
        stock_price=stock_price,
    )

    if output_format == "csv":
        return projection_to_csv_string(projection)

    return projection


def load_form_1040(year: str, data_dir: Optional[Path] = None) -> Optional[dict]:
    """Load Form 1040 JSON for a given year if it exists.

    Searches for form_1040_{year}.json in the records directory.

    Args:
        year: Tax year (4 digits)
        data_dir: Directory containing records. Defaults to XDG data path.

    Returns:
        Parsed 1040 data dict (unwrapped from 'data' key if present), or None if not found
    """
    from .config import get_data_path

    if data_dir is None:
        data_dir = get_data_path()

    # Look for 1040 in records directory
    form_path = data_dir / "records" / f"form_1040_{year}.json"
    if form_path.exists():
        with open(form_path) as f:
            raw = json.load(f)
        # Unwrap 'data' key if present (record format has meta + data)
        if "data" in raw and "income" in raw.get("data", {}):
            return raw["data"]
        return raw

    return None


def reconcile_tax_return(
    year: str,
    data_dir: Optional[Path] = None,
) -> dict:
    """Reconcile pay-calc tax projection against actual Form 1040.

    Compares computed values against actual 1040 line items and identifies
    gaps between pay-calc calculations and the official tax return.

    Prerequisites:
    - Form 1040 must exist for the year
    - Official W-2 records must exist for all parties listed in the 1040
    - Stub-generated or projected W-2 data is NOT acceptable

    Args:
        year: Tax year (4 digits)
        data_dir: Directory containing data. Defaults to XDG data path.

    Returns:
        dict with:
        - projection: The full tax projection (or None if validation failed)
        - form_1040: The loaded 1040 data (or None)
        - comparisons: List of line-item comparisons
        - summary: Overall reconciliation status
        - gaps: Items in 1040 not tracked by pay-calc

    Raises:
        ValueError: If Form 1040 is missing or W-2 records are insufficient
    """
    from .config import get_data_path
    from .records import list_records

    if data_dir is None:
        data_dir = get_data_path()

    # Load actual 1040 FIRST
    form_1040 = load_form_1040(year, data_dir)

    if form_1040 is None:
        raise ValueError(
            f"No Form 1040 found for {year}.\n"
            f"Import with: pay-calc records import <file>"
        )

    # Check we have at least some W-2 records for this year
    # NOTE: The 1040 only has combined totals (Line 1a = sum of all W-2s).
    # We cannot detect which parties SHOULD have W-2s from the 1040 alone.
    # We just verify we have some W-2 records and they're all official.
    all_w2_records = list_records(year=year, type_filter="w2")

    if not all_w2_records:
        raise ValueError(
            f"No W-2 records found for {year}.\n"
            f"Import W-2s with: pay-calc records import <file>"
        )

    # Generate projection (will use official W-2 records)
    projection = generate_projection(
        year,
        data_dir,
        allow_projection=False,  # Never allow projection for validation
        stock_price=None,
    )

    # Verify all data sources are official W-2s (not stub-generated)
    non_official_sources = []
    data_sources = projection.get("data_sources", {})
    for party_key, party_info in data_sources.items():
        for source in party_info.get("sources", []):
            if "official W-2" not in source:
                non_official_sources.append(f"{party_key}: {source}")

    if non_official_sources:
        sources_list = "\n  ".join(non_official_sources)
        raise ValueError(
            f"W-2 validation requires official W-2 records only.\n"
            f"Non-official sources found:\n"
            f"  {sources_list}\n"
            f"Import official W-2s with: pay-calc records import <file>"
        )

    # Build line-item comparisons
    comparisons = []
    gaps = []

    def compare(label: str, calc_value: float, actual_value: float, tolerance: float = 1.0, notes: str = "") -> dict:
        """Compare calculated vs actual value."""
        delta = calc_value - actual_value
        match = abs(delta) <= tolerance
        return {
            "line": label,
            "calculated": calc_value,
            "actual": actual_value,
            "delta": delta,
            "match": match,
            "notes": notes,
        }

    income = form_1040.get("income", {})
    deductions = form_1040.get("deductions", {})
    tax_credits = form_1040.get("tax_and_credits", {})
    payments = form_1040.get("payments", {})
    refund_owed = form_1040.get("refund_or_owed", {})
    schedule_2 = form_1040.get("schedule_2", {})
    schedule_3 = form_1040.get("schedule_3", {})

    # Core income comparison
    comparisons.append(compare(
        "Wages (Line 1a)",
        projection["combined_wages"],
        income.get("line_1a_wages", 0),
    ))

    # Non-wage income - now tracked via supplemental values
    comparisons.append(compare(
        "Interest income (Line 2b)",
        projection["supplemental"]["interest_income"]["value"],
        income.get("line_2b_taxable_interest", 0),
    ))
    comparisons.append(compare(
        "Dividends (Line 3b)",
        projection["supplemental"]["dividend_income"]["value"],
        income.get("line_3b_ordinary_dividends", 0),
    ))
    comparisons.append(compare(
        "Capital gain/loss (Line 7)",
        projection["supplemental"]["capital_gain_loss"]["value"],
        income.get("line_7_capital_gain_loss", 0),
    ))
    comparisons.append(compare(
        "Schedule 1 income (Line 8)",
        projection["supplemental"]["schedule_1_income"]["value"],
        income.get("line_8_schedule_1_income", 0),
    ))

    # Total income comparison - now uses total_income including non-wage
    comparisons.append(compare(
        "Total income (Line 9)",
        projection["total_income"],
        income.get("line_9_total_income", 0),
    ))

    # Deductions
    comparisons.append(compare(
        "Standard deduction (Line 12a)",
        projection["standard_deduction"],
        deductions.get("line_12a_standard_deduction", 0),
    ))

    # QBI deduction - now tracked
    comparisons.append(compare(
        "QBI deduction (Line 13)",
        projection["qbi_deduction"],
        deductions.get("line_13_qbi_deduction", 0),
    ))

    # Taxable income
    comparisons.append(compare(
        "Taxable income (Line 15)",
        projection["final_taxable_income"],
        deductions.get("line_15_taxable_income", 0),
    ))

    # Tax calculated
    comparisons.append(compare(
        "Tax (Line 16)",
        projection["federal_income_tax_assessed"],
        tax_credits.get("line_16_tax", 0),
    ))

    # Credits (Schedule 3)
    part1 = schedule_3.get("part_1", {})
    comparisons.append(compare(
        "Child care credit (Sch 3 Line 2)",
        projection["child_care_credit"],
        part1.get("line_2_child_care_credit", 0),
    ))

    # Other nonrefundable credits not yet tracked
    other_credits = part1.get("line_8_total", 0) - part1.get("line_2_child_care_credit", 0)
    if other_credits > 0:
        gaps.append({
            "item": "Other nonrefundable credits",
            "line": "Schedule 3 Line 8",
            "amount": other_credits,
        })

    # Schedule 2 - Additional taxes
    part2 = schedule_2.get("part_2", {})
    additional_medicare = part2.get("line_6_additional_medicare_tax", 0)
    comparisons.append(compare(
        "Additional Medicare Tax (Sch 2 Line 6)",
        max(0, projection["total_medicare_taxes_assessed"] - projection["combined_medicare_wages"] * 0.0145),
        additional_medicare,
        tolerance=1.0,
    ))

    # NIIT - now tracked
    comparisons.append(compare(
        "NIIT (Sch 2 Line 7)",
        projection["niit"],
        part2.get("line_7_net_investment_income_tax", 0),
    ))

    # Other taxes - now tracked
    comparisons.append(compare(
        "Other taxes (Sch 2 Line 18)",
        projection["other_taxes"],
        part2.get("line_18_other_taxes", 0),
    ))

    # Total tax
    comparisons.append(compare(
        "Total tax (Line 24)",
        projection["tentative_tax_per_return"],
        tax_credits.get("line_24_total_tax", 0),
    ))

    # Payments
    comparisons.append(compare(
        "W-2 withholding (Line 25a)",
        projection["him_fed_withheld"] + projection["her_fed_withheld"],
        payments.get("line_25a_w2_withholding", 0),
    ))

    # Form 8959 withholding (Line 25c)
    comparisons.append(compare(
        "Form 8959 withholding (Line 25c)",
        projection["form_8959_withholding"],
        payments.get("line_25c_other_withholding", 0),
    ))

    comparisons.append(compare(
        "Total payments (Line 33)",
        projection["him_fed_withheld"] + projection["her_fed_withheld"] + projection["total_ss_overpayment"] + projection["form_8959_withholding"],
        payments.get("line_33_total_payments", 0),
    ))

    # Final refund/owed
    actual_refund = refund_owed.get("line_34_overpaid", 0)
    if actual_refund == 0:
        actual_refund = -refund_owed.get("line_37_owed", 0)

    comparisons.append(compare(
        "Refund (Line 34/35)",
        projection["final_refund"],
        actual_refund,
        notes="Final reconciliation",
    ))

    # Calculate summary
    total_gap = projection["final_refund"] - actual_refund
    gaps_total = sum(g.get("amount", 0) for g in gaps if g.get("amount", 0) > 0)
    credits_missed = sum(g.get("amount", 0) for g in gaps if "credit" in g.get("item", "").lower())
    payments_missed = sum(g.get("amount", 0) for g in gaps if "withholding" in g.get("item", "").lower())

    # Income lines are simple numbers (2b, 3b, 7, 8) not "Schedule X Line Y"
    income_lines = ["2b", "3b", "7", "8", "13"]  # Include QBI deduction as it affects income
    untracked_income = sum(
        g.get("amount", 0) for g in gaps
        if g.get("line", "") in income_lines
    )

    # Gap as % of taxable income (more meaningful than % of refund)
    taxable_income = projection["final_taxable_income"]
    gap_pct = abs(total_gap) / taxable_income * 100 if taxable_income > 0 else 0

    summary = {
        "status": "reconciled" if abs(total_gap) < 1.0 else "gap",
        "calculated_refund": projection["final_refund"],
        "actual_refund": actual_refund,
        "gap": total_gap,
        "gap_pct": gap_pct,
        "gap_pct_basis": "taxable_income",
        "untracked_income": untracked_income,
        "untracked_credits": credits_missed,
        "untracked_payments": payments_missed,
    }

    return {
        "projection": projection,
        "form_1040": form_1040,
        "comparisons": comparisons,
        "summary": summary,
        "gaps": gaps,
    }


def generate_tax_projection_file(
    year: str,
    data_dir: Optional[Path] = None,
    output_path: Optional[Path] = None,
) -> Path:
    """Generate tax projection and write to CSV file.

    Legacy function for file-based output. Use generate_tax_projection()
    for programmatic access.

    Args:
        year: Tax year (e.g., "2024")
        data_dir: Directory containing W-2 data files. Defaults to XDG data path.
        output_path: Path for output CSV. Defaults to {data_dir}/{year}_tax_projection.csv

    Returns:
        Path to the generated CSV file
    """
    from .config import get_data_path

    if data_dir is None:
        data_dir = get_data_path()

    projection = generate_projection(year, data_dir)

    if output_path is None:
        output_path = data_dir / f"{year}_tax_projection.csv"

    return write_projection_csv(projection, output_path)
