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
    deductions = stub.get("deductions", [])

    # Box 1: Wages, tips, other compensation (FIT taxable wages)
    # FIT taxable = gross - pretax deductions (401k, FSA, HSA, etc.)
    ytd_gross = ytd.get("gross", 0)

    # Check if fit_taxable_wages is explicitly provided
    fit_taxable_explicit = ytd.get("fit_taxable_wages")
    if fit_taxable_explicit is not None:
        fit_taxable = fit_taxable_explicit
    else:
        # Calculate from gross minus pretax deductions
        pretax_total = 0
        if isinstance(deductions, list):
            for ded in deductions:
                ded_type = ded.get("type", "").lower()
                # Pretax deductions: 401k, FSA, HSA, retirement plans
                if any(t in ded_type for t in ["401", "403", "fsa", "hsa", "tsp", "retirement"]):
                    pretax_total += ded.get("ytd_amount", 0)
        fit_taxable = ytd_gross - pretax_total

    # Box 2: Federal income tax withheld
    # Handle both schemas: federal_income_tax.ytd_withheld OR federal_income.ytd
    fed_tax = taxes.get("federal_income_tax", {}) or taxes.get("federal_income", {})
    fed_withheld = fed_tax.get("ytd_withheld", 0) or fed_tax.get("ytd", 0)

    # Calculate FICA-exempt deductions (reduce SS and Medicare wages)
    # These are Section 125 cafeteria plan benefits exempt from FICA:
    # - FSA (Flexible Spending Accounts - medical and dependent care)
    # - Health insurance premiums (dental, medical, vision)
    fica_exempt_total = 0
    if isinstance(deductions, list):
        for ded in deductions:
            ded_type = ded.get("type", "").lower()
            # FSA (dependent care, medical FSA)
            if "fsa" in ded_type:
                fica_exempt_total += ded.get("ytd_amount", 0)
            # Health insurance premiums
            elif any(t in ded_type for t in ["dental", "medical", "vision"]):
                fica_exempt_total += ded.get("ytd_amount", 0)

    # Box 3: Social Security wages (gross minus FICA-exempt, capped at SS wage base)
    ss_wage_base = SS_WAGE_BASE.get(year, 176100)
    ss_wages = min(ytd_gross - fica_exempt_total, ss_wage_base)

    # Box 4: Social Security tax withheld
    ss_tax = taxes.get("social_security", {})
    ss_withheld = ss_tax.get("ytd_withheld", 0) or ss_tax.get("ytd", 0)

    # Box 5: Medicare wages and tips (gross minus FICA-exempt)
    medicare_wages = ytd_gross - fica_exempt_total

    # Box 6: Medicare tax withheld
    medicare_tax = taxes.get("medicare", {})
    medicare_withheld = medicare_tax.get("ytd_withheld", 0) or medicare_tax.get("ytd", 0)

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


def _normalize_employer(name: str) -> str:
    """Normalize employer name for matching.

    Removes trailing LLC/Inc suffixes and normalizes case/whitespace.
    """
    import re
    # Remove trailing ", LLC" or " LLC" etc.
    normalized = re.sub(r'[,\s]+(LLC|Inc|Corp|Corporation)\.?$', '', name, flags=re.IGNORECASE)
    # Normalize whitespace and case
    normalized = ' '.join(normalized.split()).upper()
    # Normalize & vs and
    normalized = normalized.replace(' AND ', ' & ')
    return normalized


def generate_w2(
    year: str,
    party: str,
    allow_projection: bool = False,
    stock_price: Optional[float] = None,
) -> dict:
    """Generate W-2 data for a party, per-employer.

    For each employer:
    1. Check for official W-2 record → use it
    2. Else get latest stub → generate W-2 from it
    3. If stub incomplete (not December) → project if allowed

    Args:
        year: Tax year (4 digits)
        party: 'him' or 'her'
        allow_projection: If True, project to year-end when stub data incomplete
        stock_price: Stock price for RSU valuation (used with projection)

    Returns:
        dict with:
        - w2: aggregated W-2 box values (sum of all employers)
        - employers: list of per-employer results with source info
        - sources: summary of data sources used

    Raises:
        FileNotFoundError: If no W-2 or stub data found
        ValueError: If incomplete year and allow_projection=False
    """
    from collections import defaultdict
    from .records import list_records

    # Get all W-2 records and stubs
    w2_records = list_records(year=year, party=party, type_filter="w2")
    stub_records = list_records(year=year, party=party, type_filter="stub")

    if not w2_records and not stub_records:
        raise FileNotFoundError(
            f"No W-2 or stub records found for {party} ({year}).\n"
            f"Import pay stubs with 'pay-calc records import'."
        )

    # Group stubs by normalized employer name
    stubs_by_employer = defaultdict(list)
    employer_display_names = {}  # normalized -> display name
    for stub in stub_records:
        employer = stub.get("data", {}).get("employer", "Unknown")
        normalized = _normalize_employer(employer)
        stubs_by_employer[normalized].append(stub)
        employer_display_names[normalized] = employer  # keep original for display

    # Track which employers have official W-2s (by normalized name)
    w2s_by_employer = defaultdict(list)
    for w2 in w2_records:
        data = w2.get("data", {})
        employer = data.get("employer_name") or w2.get("employer", "Unknown")
        normalized = _normalize_employer(employer)
        w2s_by_employer[normalized].append(w2)
        employer_display_names[normalized] = employer  # keep original for display

    # All employers from both sources (normalized)
    all_employers = set(stubs_by_employer.keys()) | set(w2s_by_employer.keys())

    # Find the most recent employer (based on latest pay stub date across all stubs)
    # Only the current employer should be projected; past employers use their final stub
    most_recent_employer = None
    most_recent_date = ""
    for employer, emp_stubs in stubs_by_employer.items():
        for stub in emp_stubs:
            pay_date = stub.get("data", {}).get("pay_date", "")
            if pay_date > most_recent_date:
                most_recent_date = pay_date
                most_recent_employer = employer

    # Process each employer
    employer_results = []
    aggregated_w2 = defaultdict(float)
    sources_used = []
    projection_warnings = []

    for employer_key in sorted(all_employers):
        display_name = employer_display_names.get(employer_key, employer_key)

        # Check for official W-2 first (already grouped by normalized name)
        employer_w2s = w2s_by_employer.get(employer_key, [])

        if employer_w2s:
            # Use official W-2
            emp_data = defaultdict(float)
            for w2 in employer_w2s:
                data = w2.get("data", {})
                emp_data["wages_tips_other_comp"] += data.get("wages", 0)
                emp_data["federal_income_tax_withheld"] += data.get("federal_tax_withheld", 0)
                emp_data["social_security_wages"] += data.get("social_security_wages", 0)
                emp_data["social_security_tax_withheld"] += data.get("social_security_tax", 0)
                emp_data["medicare_wages_and_tips"] += data.get("medicare_wages", 0)
                emp_data["medicare_tax_withheld"] += data.get("medicare_tax", 0)

            employer_results.append({
                "employer": display_name,
                "source": "official_w2",
                "source_detail": f"{len(employer_w2s)} W-2(s)",
                "w2": dict(emp_data),
            })
            sources_used.append(f"{display_name}: official W-2")

            # Add to aggregate
            for k, v in emp_data.items():
                aggregated_w2[k] += v

            continue

        # No official W-2 - generate from stubs
        employer_stubs = stubs_by_employer.get(employer_key, [])
        if not employer_stubs:
            continue

        # Sort stubs by pay_date
        def get_pay_date(record):
            return record.get("data", {}).get("pay_date", "")

        stubs_sorted = sorted(employer_stubs, key=get_pay_date)
        latest_stub_record = stubs_sorted[-1]
        latest_stub = latest_stub_record.get("data", {})
        stub_id = latest_stub_record.get("id", "unknown")
        pay_date_str = latest_stub.get("pay_date", "")

        # Check if year complete (December stub) OR this is a past employer (not the most recent)
        is_current_employer = (employer_key == most_recent_employer)
        year_complete = False
        if pay_date_str:
            pay_date = datetime.strptime(pay_date_str, "%Y-%m-%d")
            year_complete = pay_date.month == 12

        # Past employers: use final stub as-is (no projection needed)
        # Current employer with December stub: use it
        if year_complete or not is_current_employer:
            # Generate from stub
            w2_result = stub_to_w2(
                stub=latest_stub,
                year=year,
                party=party,
                validate=True,
            )

            source_note = "final" if not is_current_employer else "stub"
            employer_results.append({
                "employer": display_name,
                "source": source_note,
                "source_detail": f"Stub {stub_id} ({pay_date_str})",
                "w2": w2_result["w2"],
            })
            sources_used.append(f"{display_name}: {source_note} {pay_date_str}")

            for k, v in w2_result["w2"].items():
                aggregated_w2[k] += v

        elif allow_projection:
            # Project to year-end
            from .income_projection import generate_projection
            from datetime import date

            # Extract stub data for projection
            employer_stub_data = [s.get("data", {}) for s in employer_stubs]

            projection = generate_projection(
                stubs=employer_stub_data,
                year=year,
                party=party,
                stock_price=stock_price,
            )

            projected_stub = projection.get("stub", latest_stub)

            w2_result = stub_to_w2(
                stub=projected_stub,
                year=year,
                party=party,
                validate=False,
            )

            # Calculate days remaining
            latest_date = datetime.strptime(pay_date_str, "%Y-%m-%d").date()
            year_end = date(int(year), 12, 31)
            days_remaining = (year_end - latest_date).days

            employer_results.append({
                "employer": display_name,
                "source": "projection",
                "source_detail": f"Stub {stub_id} ({pay_date_str}) + {days_remaining} days",
                "w2": w2_result["w2"],
                "projection_info": {
                    "ytd_stub_date": pay_date_str,
                    "days_remaining": days_remaining,
                },
            })
            sources_used.append(f"{display_name}: projected from {pay_date_str}")

            # Collect warnings
            projection_warnings.extend(projection.get("config_warnings", []))

            for k, v in w2_result["w2"].items():
                aggregated_w2[k] += v

        else:
            raise ValueError(
                f"Latest stub for {display_name} is from {pay_date_str} (not December). "
                f"Set allow_projection=True to project to year-end."
            )

    return {
        "w2": dict(aggregated_w2),
        "employers": employer_results,
        "sources": sources_used,
        "projection_warnings": projection_warnings if projection_warnings else None,
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
