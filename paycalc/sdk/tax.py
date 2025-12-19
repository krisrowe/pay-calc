"""Tax projection calculations and output generation."""

import csv
import io
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal, Optional, Union

import yaml


def load_tax_rules(year: str) -> dict:
    """Load tax rules for a specific year from tax-rules/YYYY.yaml."""
    # Use package-relative path so it works regardless of CWD
    package_root = Path(__file__).parent.parent.parent  # sdk -> paycalc -> project root
    config_file = package_root / "tax-rules" / f"{year}.yaml"
    if not config_file.exists():
        raise FileNotFoundError(f"Tax rules file not found for year {year}: {config_file}")

    with open(config_file, "r") as f:
        return yaml.safe_load(f)


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


def calculate_additional_medicare_tax(total_medicare_wages: float, threshold: float) -> float:
    """Calculate the Additional Medicare Tax amount (0.9% of excess wages)."""
    excess_medicare_wages = max(0, total_medicare_wages - threshold)
    return excess_medicare_wages * 0.009


def load_party_w2_data(data_dir: Path, year: str, party: str) -> dict:
    """Load and aggregate W-2 data for a party from the data directory.

    Data sources (in order of preference):
    1. W-2 JSON files (YYYY_party_w2_forms.json) - year-end
    2. Analysis JSON files (YYYY_party_pay_all.json) - mid-year
    """
    w2_file = data_dir / f"{year}_{party}_w2_forms.json"
    analysis_file = data_dir / f"{year}_{party}_pay_all.json"

    # Try W-2 file first (year-end)
    if w2_file.exists():
        with open(w2_file, "r") as f:
            w2_data = json.load(f)

        aggregated_data = defaultdict(float)
        for form in w2_data.get("forms", []):
            for key, value in form.get("data", {}).items():
                aggregated_data[key] += value

        return dict(aggregated_data)

    # Fall back to analysis file (mid-year)
    if analysis_file.exists():
        with open(analysis_file, "r") as f:
            analysis_data = json.load(f)

        # Extract YTD totals from analysis summary
        summary = analysis_data.get("summary", {})
        final_ytd = summary.get("final_ytd", {})

        # Map analysis fields to W-2 equivalent fields
        return {
            "wages_tips_other_comp": final_ytd.get("gross", 0),
            "federal_income_tax_withheld": final_ytd.get("federal_withheld", 0),
            "medicare_wages_and_tips": final_ytd.get("fit_taxable_wages", final_ytd.get("gross", 0)),
            "medicare_tax_withheld": 0,  # Not tracked separately in analysis
        }

    raise FileNotFoundError(
        f"No income data found for {party} ({year}).\n"
        f"  Tried: {w2_file}\n"
        f"  Tried: {analysis_file}\n"
        f"Run 'pay-calc w2-extract {year}' or 'pay-calc analysis {year} {party}' first."
    )


def generate_projection(year: str, data_dir: Path = None, tax_rules: dict = None) -> dict:
    """Generate tax projection data for a given year.

    Args:
        year: Tax year (e.g., "2024")
        data_dir: Directory containing W-2 data files. Defaults to XDG data path.
        tax_rules: Optional pre-loaded tax rules (loads from file if not provided)

    Returns:
        Dictionary with all projection data
    """
    from .config import get_data_path

    if data_dir is None:
        data_dir = get_data_path()

    if tax_rules is None:
        tax_rules = load_tax_rules(year)

    him_data = load_party_w2_data(data_dir, year, "him")
    her_data = load_party_w2_data(data_dir, year, "her")

    him_wages = him_data["wages_tips_other_comp"]
    her_wages = her_data["wages_tips_other_comp"]
    him_fed_withheld = him_data["federal_income_tax_withheld"]
    her_fed_withheld = her_data["federal_income_tax_withheld"]
    him_medicare_wages = him_data.get("medicare_wages_and_tips", him_wages)
    her_medicare_wages = her_data.get("medicare_wages_and_tips", her_wages)
    him_medicare_withheld = him_data.get("medicare_tax_withheld", 0)
    her_medicare_withheld = her_data.get("medicare_tax_withheld", 0)

    combined_wages = him_wages + her_wages
    combined_medicare_wages = him_medicare_wages + her_medicare_wages
    combined_medicare_withheld = him_medicare_withheld + her_medicare_withheld

    # Tax rules structure: mfj.standard_deduction, mfj.tax_brackets, additional_medicare_tax_threshold
    standard_deduction = tax_rules["mfj"]["standard_deduction"]
    final_taxable_income = max(0, combined_wages - standard_deduction)

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

    tentative_tax_per_return = federal_income_tax_assessed + (-medicare_refund)
    total_withheld = him_fed_withheld + her_fed_withheld
    final_refund = total_withheld - tentative_tax_per_return

    return {
        "year": year,
        "him_wages": him_wages,
        "her_wages": her_wages,
        "him_fed_withheld": him_fed_withheld,
        "her_fed_withheld": her_fed_withheld,
        "combined_wages": combined_wages,
        "standard_deduction": standard_deduction,
        "final_taxable_income": final_taxable_income,
        "tax_brackets": tax_rules["mfj"]["tax_brackets"],
        "federal_income_tax_assessed": federal_income_tax_assessed,
        "combined_medicare_wages": combined_medicare_wages,
        "combined_medicare_withheld": combined_medicare_withheld,
        "total_medicare_taxes_assessed": total_medicare_taxes_assessed,
        "medicare_refund": medicare_refund,
        "tentative_tax_per_return": tentative_tax_per_return,
        "final_refund": final_refund,
    }


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


def generate_tax_projection(
    year: str,
    data_dir: Optional[Path] = None,
    output_format: Literal["json", "csv"] = "json",
) -> Union[dict, str]:
    """Generate tax projection data.

    Main entry point for tax projection. Returns structured data (json)
    or formatted CSV string.

    Args:
        year: Tax year (e.g., "2024")
        data_dir: Directory containing W-2 data files. Defaults to XDG data path.
        output_format: "json" returns dict (default), "csv" returns CSV string.

    Returns:
        dict (json format) or str (csv format)
    """
    from .config import get_data_path

    if data_dir is None:
        data_dir = get_data_path()

    projection = generate_projection(year, data_dir)

    if output_format == "csv":
        return projection_to_csv_string(projection)

    return projection


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
