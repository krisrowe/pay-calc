"""W-2 generation from pay stub analysis data."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import get_data_path, load_profile


# Social Security wage base limits by year
SS_WAGE_BASE = {
    "2024": 168600,
    "2025": 176100,
    "2026": 178800,  # Projected
}


@dataclass
class StubValidationResult:
    """Result of validating a stub for W-2 conversion."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_stub_for_w2(
    stub: dict[str, Any],
    party: Optional[str] = None,
) -> StubValidationResult:
    """Validate that a stub has required fields for W-2 conversion.

    Required fields:
    - YTD gross wages
    - Federal income tax withheld
    - Social Security tax withheld
    - Medicare tax withheld

    Warnings (not errors):
    - Missing 401k deduction (unusual)
    - No RSU income when RSUs enabled for party

    Args:
        stub: Pay stub dict with pay_summary and taxes sections
        party: Optional party name to check RSU configuration

    Returns:
        StubValidationResult with valid flag, errors, and warnings
    """
    errors = []
    warnings = []

    pay_summary = stub.get("pay_summary", {})
    ytd = pay_summary.get("ytd", {})
    taxes = stub.get("taxes", {})

    # Required: YTD gross
    ytd_gross = ytd.get("gross", 0)
    if not ytd_gross or ytd_gross <= 0:
        errors.append("Missing or zero YTD gross wages")

    # Required: Federal income tax withheld
    fed_tax = taxes.get("federal_income_tax", {})
    fed_withheld = fed_tax.get("ytd_withheld", 0)
    if fed_withheld is None:
        errors.append("Missing federal income tax withheld")

    # Required: Social Security tax withheld
    ss_tax = taxes.get("social_security", {})
    ss_withheld = ss_tax.get("ytd_withheld", 0)
    if ss_withheld is None:
        errors.append("Missing Social Security tax withheld")

    # Required: Medicare tax withheld
    medicare_tax = taxes.get("medicare", {})
    medicare_withheld = medicare_tax.get("ytd_withheld", 0)
    if medicare_withheld is None:
        errors.append("Missing Medicare tax withheld")

    # Warning: No 401k deduction (unusual but not error)
    deductions = stub.get("deductions", [])
    pretax_401k = 0
    if isinstance(deductions, list):
        for ded in deductions:
            if "401" in ded.get("type", "").lower() or "k pretax" in ded.get("type", "").lower():
                pretax_401k = ded.get("ytd_amount", 0)
                break
    elif isinstance(deductions, dict):
        pretax_401k = deductions.get("401k_pretax", 0)
    if not pretax_401k and ytd_gross and ytd_gross > 50000:
        warnings.append("No 401k deduction found (unusual for high earners)")

    # Warning: RSUs enabled but no RSU income visible
    if party:
        try:
            profile = load_profile()
            parties = profile.get("parties", {})
            party_config = parties.get(party, {})

            # Check if any company has RSUs configured
            rsus_configured = False
            for company in party_config.get("companies", []):
                future_exp = company.get("future_expectations", {})
                if future_exp.get("rsus"):
                    rsus_configured = True
                    break

            if rsus_configured:
                # Check for RSU-related earnings in stub
                earnings = stub.get("earnings", {})
                has_rsu_income = any(
                    "rsu" in k.lower() or "stock" in k.lower()
                    for k in earnings.keys()
                )
                # Also check current gross for stock-type stubs
                pay_type = stub.get("_pay_type", "")
                if not has_rsu_income and pay_type != "stock_grant":
                    # Check YTD for any RSU income accumulated
                    ytd_breakdown = stub.get("ytd_breakdown", {})
                    ytd_earnings = ytd_breakdown.get("earnings", {})
                    has_ytd_rsu = any(
                        "rsu" in k.lower() or "stock" in k.lower()
                        for k in ytd_earnings.keys()
                    )
                    if not has_ytd_rsu:
                        warnings.append(
                            "RSUs configured for party but no RSU income found in stub"
                        )
        except Exception:
            pass  # Profile load failed, skip RSU check

    return StubValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )


def stub_to_w2(
    stub: dict[str, Any],
    year: str,
    party: Optional[str] = None,
    employer: Optional[str] = None,
    validate: bool = True,
) -> dict[str, Any]:
    """Convert a final pay stub to W-2 format.

    Takes a stub dict directly as input and extracts W-2 box values.
    This is the core conversion function - no file lookups, no projections.

    Args:
        stub: Pay stub dict with pay_summary and taxes sections
        year: Tax year (4 digits)
        party: Optional party name for validation (RSU checks)
        employer: Optional employer name override
        validate: Whether to validate stub fields (default True)

    Returns:
        dict with:
        - w2: W-2 box values
        - validation: StubValidationResult (if validate=True)
        - source: metadata about the source stub

    Raises:
        ValueError: If validation fails with errors (when validate=True)
    """
    # Validate if requested
    validation = None
    if validate:
        validation = validate_stub_for_w2(stub, party=party)
        if not validation.valid:
            raise ValueError(
                f"Stub validation failed: {'; '.join(validation.errors)}"
            )

    # Extract values from stub
    pay_summary = stub.get("pay_summary", {})
    ytd = pay_summary.get("ytd", {})
    taxes = stub.get("taxes", {})

    # Box 1: Wages, tips, other compensation (FIT taxable wages)
    ytd_gross = ytd.get("gross", 0)
    fit_taxable = ytd.get("fit_taxable_wages", ytd_gross)

    # Box 2: Federal income tax withheld
    fed_withheld = taxes.get("federal_income_tax", {}).get("ytd_withheld", 0)

    # Box 3: Social Security wages (capped at SS wage base)
    ss_wage_base = SS_WAGE_BASE.get(year, 176100)
    ss_wages = min(ytd_gross, ss_wage_base)

    # Box 4: Social Security tax withheld
    ss_withheld = taxes.get("social_security", {}).get("ytd_withheld", 0)

    # Box 5: Medicare wages and tips
    # Subtract pretax health insurance (dental, medical, vision) from gross
    health_insurance_ytd = 0
    deductions = stub.get("deductions", [])
    if isinstance(deductions, list):
        for ded in deductions:
            ded_type = ded.get("type", "").lower()
            if any(t in ded_type for t in ["dental", "medical", "vision"]):
                health_insurance_ytd += ded.get("ytd_amount", 0)
    medicare_wages = ytd_gross - health_insurance_ytd

    # Box 6: Medicare tax withheld
    medicare_withheld = taxes.get("medicare", {}).get("ytd_withheld", 0)

    # Build W-2 data
    w2_data = {
        "wages_tips_other_comp": round(fit_taxable, 2),
        "federal_income_tax_withheld": round(fed_withheld, 2),
        "social_security_wages": round(ss_wages, 2),
        "social_security_tax_withheld": round(ss_withheld, 2),
        "medicare_wages_and_tips": round(medicare_wages, 2),
        "medicare_tax_withheld": round(medicare_withheld, 2),
    }

    # Determine employer from stub if not provided
    if not employer:
        employer = stub.get("employer", "Unknown")

    result = {
        "year": year,
        "party": party,
        "employer": employer,
        "w2": w2_data,
        "source": {
            "type": "stub",
            "pay_date": stub.get("pay_date"),
            "ytd_gross": ytd_gross,
        },
    }

    if validation:
        result["validation"] = {
            "valid": validation.valid,
            "errors": validation.errors,
            "warnings": validation.warnings,
        }

    return result


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

                    # Additional gross wages
                    additional_gross = additional.get("total_gross", 0)

                    # SS wages for additional: only taxable amount up to wage base
                    # If ytd_w2 is already at cap, additional SS wages = 0
                    ss_wage_base = SS_WAGE_BASE.get(year, 176100)
                    ytd_ss_wages = ytd_w2.get("social_security_wages", 0)
                    remaining_ss_cap = max(0, ss_wage_base - ytd_ss_wages)
                    additional_ss_wages = round(min(additional_gross, remaining_ss_cap), 2)

                    # Medicare wages for additional = all additional gross
                    additional_medicare_wages = round(additional_gross, 2)

                    # projected_additional_w2: same W-2 format as ytd_w2
                    result["projected_additional_w2"] = {
                        "wages_tips_other_comp": round(additional.get("total_gross", 0), 2),
                        "federal_income_tax_withheld": round(additional.get("federal_withheld", 0), 2),
                        "social_security_wages": additional_ss_wages,
                        "social_security_tax_withheld": round(additional.get("ss_withheld", 0), 2),
                        "medicare_wages_and_tips": additional_medicare_wages,
                        "medicare_tax_withheld": round(additional.get("medicare_withheld", 0), 2),
                    }

                    # projected_w2: simple sum of ytd_w2 + projected_additional_w2
                    add_w2 = result["projected_additional_w2"]
                    result["projected_w2"] = {
                        "wages_tips_other_comp": round(ytd_w2["wages_tips_other_comp"] + add_w2["wages_tips_other_comp"], 2),
                        "federal_income_tax_withheld": round(ytd_w2["federal_income_tax_withheld"] + add_w2["federal_income_tax_withheld"], 2),
                        "social_security_wages": round(ytd_w2["social_security_wages"] + add_w2["social_security_wages"], 2),
                        "social_security_tax_withheld": round(ytd_w2["social_security_tax_withheld"] + add_w2["social_security_tax_withheld"], 2),
                        "medicare_wages_and_tips": round(ytd_w2["medicare_wages_and_tips"] + add_w2["medicare_wages_and_tips"], 2),
                        "medicare_tax_withheld": round(ytd_w2["medicare_tax_withheld"] + add_w2["medicare_tax_withheld"], 2),
                    }

                    # Include projection metadata separately
                    result["projection_info"] = {
                        "days_remaining": proj.get("days_remaining"),
                        "stock_price_used": stock_price,
                        "income_breakdown": {
                            "regular_pay": additional.get("regular_pay", 0),
                            "stock_grants": additional.get("stock_grants", 0),
                        },
                        "warnings": proj.get("stock_grant_info", {}).get("warnings", []),
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
