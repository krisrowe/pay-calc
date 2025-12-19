"""W-2 generation from pay stub analysis data."""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import get_data_path


# Social Security wage base limits by year
SS_WAGE_BASE = {
    "2024": 168600,
    "2025": 176100,
    "2026": 178800,  # Projected
}


def generate_w2_from_analysis(
    year: str,
    party: str,
    final_stub_date: Optional[str] = None,
    employer_filter: Optional[str] = None,
    data_dir: Optional[Path] = None,
) -> dict:
    """Generate W-2 data from pay stub analysis.

    Args:
        year: Tax year (4 digits)
        party: 'him' or 'her'
        final_stub_date: If provided, bypasses year-end coverage check
        employer_filter: Optional employer name filter (substring match)
        data_dir: Override data directory

    Returns:
        dict with W-2 form data ready for output

    Raises:
        FileNotFoundError: If analysis data doesn't exist
        ValueError: If data doesn't cover full year and no final_stub_date
    """
    data_path = data_dir or get_data_path()

    # Load analysis data
    analysis_file = data_path / f"{year}_{party}_pay_all.json"
    if not analysis_file.exists():
        raise FileNotFoundError(
            f"Analysis data not found: {analysis_file}\n"
            f"Run 'pay-calc analysis {year} {party}' first."
        )

    with open(analysis_file) as f:
        analysis_data = json.load(f)

    summary = analysis_data.get("summary", {})
    ytd = analysis_data.get("ytd_breakdown", {})
    date_range = summary.get("date_range", {})

    # Check year coverage
    end_date_str = date_range.get("end", "")
    if end_date_str:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
        end_month = end_date.month

        # If not December, require final_stub_date confirmation
        if end_month != 12 and not final_stub_date:
            raise ValueError(
                f"Analysis data only covers through {end_date_str}. "
                f"Provide final_stub_date to confirm this is complete."
            )

    # Extract W-2 box values from analysis
    earnings = ytd.get("earnings", {})
    taxes = ytd.get("taxes", {})

    # Box 1: Wages, tips, other compensation (FIT taxable wages)
    total_gross = summary.get("final_ytd", {}).get("gross", 0)
    pretax_401k = earnings.get("401k Pre-Tax", 0)
    fit_taxable = summary.get("final_ytd", {}).get("fit_taxable_wages", total_gross - pretax_401k)

    # Box 2: Federal income tax withheld
    federal_withheld = taxes.get("Federal Income Tax", 0)

    # Box 3: Social Security wages (capped at SS wage base)
    ss_wage_base = SS_WAGE_BASE.get(year, 176100)
    medicare_wages = summary.get("final_ytd", {}).get("gross", 0)
    ss_wages = min(medicare_wages, ss_wage_base)

    # Box 4: Social Security tax withheld
    ss_tax = taxes.get("Social Security", 0)

    # Box 5: Medicare wages and tips
    medicare_wages_tips = medicare_wages

    # Box 6: Medicare tax withheld
    medicare_tax = taxes.get("Medicare", 0)

    # Build W-2 form data
    w2_data = {
        "wages_tips_other_comp": round(fit_taxable, 2),
        "federal_income_tax_withheld": round(federal_withheld, 2),
        "social_security_wages": round(ss_wages, 2),
        "social_security_tax_withheld": round(ss_tax, 2),
        "medicare_wages_and_tips": round(medicare_wages_tips, 2),
        "medicare_tax_withheld": round(medicare_tax, 2),
    }

    # Determine employer(s) from stubs
    stubs = analysis_data.get("stubs", [])
    employers = set()
    for stub in stubs:
        emp = stub.get("employer", "")
        if employer_filter and employer_filter.lower() not in emp.lower():
            continue
        employers.add(emp)

    employer_name = ", ".join(sorted(employers)) if employers else "Unknown"
    if employer_filter:
        employer_name = employer_filter

    # Build output structure
    form = {
        "employer": employer_name,
        "source_type": "analysis",
        "source_file": str(analysis_file.name),
        "data": w2_data,
    }

    return {
        "year": year,
        "party": party,
        "generated_from": "analysis",
        "analysis_date_range": date_range,
        "forms": [form],
    }


def generate_w2_with_projection(
    year: str,
    party: str,
    include_projection: bool = False,
    stock_price: Optional[float] = None,
    final_stub_date: Optional[str] = None,
    employer_filter: Optional[str] = None,
    data_dir: Optional[Path] = None,
) -> dict:
    """Generate W-2 data with optional projection for year-end.

    Returns a structured result with up to 3 sections:
    - ytd_w2: W-2 box values from latest stub YTD data
    - projected_additional: Projected income and withholding for remainder of year
    - projected_w2: W-2 box values including projections

    Args:
        year: Tax year (4 digits)
        party: 'him' or 'her'
        include_projection: Whether to include year-end projections
        stock_price: Stock price for RSU valuation (use with include_projection)
        final_stub_date: If provided, bypasses year-end coverage check
        employer_filter: Optional employer name filter
        data_dir: Override data directory

    Returns:
        dict with ytd_w2, and optionally projected_additional and projected_w2
    """
    # Get base W-2 data
    w2_data = generate_w2_from_analysis(
        year=year,
        party=party,
        final_stub_date=final_stub_date,
        employer_filter=employer_filter,
        data_dir=data_dir,
    )

    date_range = w2_data.get("analysis_date_range", {})
    form = w2_data["forms"][0]
    ytd_w2 = form["data"]

    result = {
        "year": year,
        "party": party,
        "date_range": date_range,
        "source_file": form["source_file"],
        "employer": form["employer"],
        "ytd_w2": ytd_w2,
    }

    if include_projection:
        from .income_projection import generate_projection

        data_path = data_dir or get_data_path()
        analysis_file = data_path / f"{year}_{party}_pay_all.json"

        if analysis_file.exists():
            with open(analysis_file) as f:
                analysis_data = json.load(f)

            stubs = analysis_data.get("stubs", [])
            if stubs:
                proj = generate_projection(stubs, year, party=party, stock_price=stock_price)

                if proj and proj.get("days_remaining", 0) > 0:
                    additional = proj.get("projected_additional", {})
                    projected_total = proj.get("projected_total", {})

                    result["projected_additional"] = {
                        "days_remaining": proj.get("days_remaining"),
                        "income": {
                            "regular_pay": additional.get("regular_pay", 0),
                            "stock_grants": additional.get("stock_grants", 0),
                            "total_gross": additional.get("total_gross", 0),
                        },
                        "withholding": {
                            "federal": additional.get("federal_withheld", 0),
                            "social_security": additional.get("ss_withheld", 0),
                            "medicare": additional.get("medicare_withheld", 0),
                            "total": additional.get("total_taxes", 0),
                        },
                        "stock_price_used": stock_price,
                        "warnings": proj.get("stock_grant_info", {}).get("warnings", []),
                    }

                    result["projected_w2"] = {
                        "wages_tips_other_comp": projected_total.get("gross", 0),
                        "federal_income_tax_withheld": projected_total.get("federal_withheld", 0),
                        "social_security_wages": projected_total.get("ss_wages", 0),
                        "social_security_tax_withheld": projected_total.get("ss_withheld", 0),
                        "medicare_wages_and_tips": projected_total.get("medicare_wages", 0),
                        "medicare_tax_withheld": projected_total.get("medicare_withheld", 0),
                    }

    return result


def save_w2_forms(w2_data: dict, output_path: Optional[Path] = None) -> Path:
    """Save W-2 form data to JSON file.

    Args:
        w2_data: W-2 data dict from generate_w2_from_analysis
        output_path: Optional output path (default: data_dir/{year}_{party}_w2_forms.json)

    Returns:
        Path to saved file
    """
    if output_path is None:
        data_path = get_data_path()
        year = w2_data["year"]
        party = w2_data["party"]
        output_path = data_path / f"{year}_{party}_w2_forms.json"

    with open(output_path, "w") as f:
        json.dump(w2_data, f, indent=2)

    return output_path
