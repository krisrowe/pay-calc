"""Pay Calc CLI - Command-line interface for pay and tax projections."""

import json
import sys
from pathlib import Path

import click

from paycalc import __version__
from paycalc.sdk import ConfigNotFoundError

# Add parent directory to path for importing existing modules
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from .profile_commands import profile as profile_group
from .stubs_commands import stubs as stubs_group
from .records_commands import records_cli as records_group
from .settings_commands import settings as settings_group
from .rsus_commands import rsus as rsus_group
from .withhold_commands import withhold as withhold_group


@click.group()
@click.version_option(version=__version__, prog_name="pay-calc")
def cli():
    """Pay Calc - Personal pay and tax projection tools.

    Commands for extracting W-2 data, processing pay stubs,
    and generating tax projections.

    Configuration is loaded from (in order):

    \b
    1. PAY_CALC_CONFIG_PATH environment variable
    2. settings.json 'profile' key (if set via CLI)
    3. ~/.config/pay-calc/profile.yaml (XDG default)

    Run 'pay-calc profile show' to see profile status and readiness.
    """
    pass


# Add subcommand groups
cli.add_command(profile_group)
cli.add_command(settings_group)
cli.add_command(stubs_group)
cli.add_command(records_group, name="records")
cli.add_command(rsus_group)
cli.add_command(withhold_group)


@cli.command("w2-extract")
@click.argument("year", required=False)
@click.option("--cache", is_flag=True, help="Cache downloaded files locally for reuse.")
@click.option("--output-dir", "-o", type=click.Path(), help="Output directory for W-2 JSON files (default: XDG data dir)")
def w2_extract(year, cache, output_dir):
    """Extract W-2 data from PDFs stored in Google Drive.

    Downloads W-2 PDFs and manual JSON files from ALL configured
    pay_records folders, parses them, and outputs aggregated W-2
    data to XDG data directory or --output-dir.

    If YEAR is provided, only processes files matching that year.
    """
    from paycalc.sdk import validate_profile, ProfileNotFoundError

    if year and (not year.isdigit() or len(year) != 4):
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    # Validate profile has required configuration
    try:
        validation = validate_profile()
        validation.require_feature("w2_extract")
    except (ProfileNotFoundError, ConfigNotFoundError) as e:
        raise click.ClickException(str(e))

    from drive_sync import sync_pay_records, load_config
    from extract_w2 import (
        find_company_and_party_from_keywords,
        extract_text_from_pdf,
        parse_w2_text,
    )
    from paycalc.sdk import get_data_path
    from collections import defaultdict
    import json

    data_dir = Path(output_dir) if output_dir else get_data_path()
    data_dir.mkdir(parents=True, exist_ok=True)

    try:
        config = load_config()
    except ConfigNotFoundError as e:
        raise click.ClickException(str(e))

    # Sync files from Drive (all configured folders)
    try:
        source_dir = sync_pay_records(use_cache=cache)
    except ValueError as e:
        raise click.ClickException(str(e))

    processed_sources = defaultdict(list)

    # Process W-2 PDFs
    pdf_files = list(source_dir.glob("*W-2*.pdf")) + list(source_dir.glob("*W2*.pdf"))
    unidentified_pdfs = []

    click.echo(f"\nFound {len(pdf_files)} W-2 PDF(s) for {year}...")
    for pdf_path in pdf_files:
        company, party = find_company_and_party_from_keywords(pdf_path.name, config)

        pdf_text = ""
        if not company:
            pdf_text = extract_text_from_pdf(pdf_path)
            company, party = find_company_and_party_from_keywords(pdf_text, config)

        if not company:
            unidentified_pdfs.append(pdf_path.name)
            continue

        click.echo(f"  Identified '{pdf_path.name}' as '{company['name']}' ({party})")
        if not pdf_text:
            pdf_text = extract_text_from_pdf(pdf_path)

        w2_data = parse_w2_text(pdf_text)
        if not w2_data:
            click.echo(f"    Warning: Could not parse financial data from {pdf_path.name}. Skipping.")
            continue

        w2_form = {
            "employer": company["name"],
            "source_type": "pdf",
            "source_file": pdf_path.name,
            "data": w2_data,
        }
        processed_sources[party].append(w2_form)

    if unidentified_pdfs:
        click.echo("\nError: Could not identify an employer for the following PDF(s):")
        for pdf_name in unidentified_pdfs:
            click.echo(f"  - {pdf_name}")
        raise click.ClickException("Unidentified PDFs found. Add keywords to profile.yaml.")

    # Process manual W-2 JSON files
    manual_files = list(source_dir.glob(f"{year}_manual-w2_*.json"))
    click.echo(f"\nFound {len(manual_files)} manual W-2 file(s)...")

    for manual_path in manual_files:
        with open(manual_path, "r") as f:
            manual_data = json.load(f)

        employer_name = manual_data.get("employer")
        if not employer_name:
            click.echo(f"Warning: Manual file {manual_path.name} is missing 'employer' key. Skipping.")
            continue

        company_info, party = find_company_and_party_from_keywords(employer_name, config)

        if not company_info:
            click.echo(f"Warning: Could not find company config for employer '{employer_name}' in {manual_path.name}. Skipping.")
            continue
        employer_name_from_config = company_info["name"]

        # Check for conflicts
        if any(src.get("employer") == employer_name_from_config for src in processed_sources[party]):
            raise click.ClickException(
                f"Conflict for employer '{employer_name_from_config}'. A PDF was already processed for {party}. "
                f"Please remove the conflicting manual file: {manual_path.name}"
            )

        click.echo(f"  Processing manual file '{manual_path.name}' for {party} ({employer_name_from_config})...")
        w2_form = {
            "employer": employer_name_from_config,
            "source_type": "manual",
            "source_file": manual_path.name,
            "data": manual_data["data"],
        }
        processed_sources[party].append(w2_form)

    # Write output files
    click.echo("\nWriting final output files...")
    for party, forms in processed_sources.items():
        if not forms:
            continue

        final_output = {"year": year, "party": party, "forms": forms}
        output_file = data_dir / f"{year}_{party}_w2_forms.json"

        with open(output_file, "w") as f:
            json.dump(final_output, f, indent=2)

        click.echo(f"  Successfully aggregated {len(forms)} W-2 form(s) to {output_file}")
        click.echo(json.dumps(final_output, indent=2))

    click.echo("\nExtraction complete.")


@cli.command("taxes")
@click.argument("year")
@click.option("--output", "-o", type=click.Path(), help="Output CSV path (default: XDG data dir)")
@click.option("--data-dir", type=click.Path(exists=True), help="Directory containing W-2 or analysis JSON files (default: XDG data dir)")
def taxes(year, output, data_dir):
    """Calculate federal tax liability and refund/owed amount.

    Loads income data for both parties (him + her), applies tax brackets,
    and calculates federal income tax, medicare taxes, and projected
    refund or amount owed.

    \b
    Data sources (in order of preference):
    1. W-2 JSON files (YYYY_party_w2_forms.json) - for year-end
    2. Analysis JSON files (YYYY_party_full.json) - mid-year fallback

    For mid-year projections, run 'analysis' for each party first.

    Output is written to XDG data directory by default, or to --output path.
    """
    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    from paycalc.sdk import generate_tax_projection, get_data_path

    data_path = Path(data_dir) if data_dir else get_data_path()
    output_path = Path(output) if output else None

    try:
        click.echo(f"Loading income data for {year}...")
        result_path = generate_tax_projection(year, data_dir=data_path, output_path=output_path)
        click.echo(f"\nSuccessfully generated tax calculation: {result_path}")

    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Error generating tax calculation: {e}")


@cli.command("w2-generate")
@click.argument("year")
@click.argument("party", type=click.Choice(["him", "her"]))
@click.option("--final-stub-date", type=str, help="Date of final pay stub if not Dec 31 (YYYY-MM-DD).")
@click.option("--output", "-o", type=click.Path(), help="Output JSON path (default: XDG data dir)")
@click.option("--employer", type=str, help="Filter to specific employer (substring match).")
@click.option("--include-projection", is_flag=True, help="Include projected income to year-end.")
@click.option("--price", type=float, help="Stock price for RSU projection (required with --include-projection if RSUs enabled).")
def w2_generate(year, party, final_stub_date, output, employer, include_projection, price):
    """Generate W-2 JSON from pay stub analysis data.

    Creates W-2 form data from analyzed pay stubs. Requires analysis
    data to exist (run 'pay-calc analysis YEAR PARTY' first).

    \b
    If pay stubs don't cover the full year (through December), you must
    either:
      1. Provide --final-stub-date to confirm the data is complete
      2. Wait until year-end stubs are imported

    \b
    With --include-projection, shows three sections:
      1. YTD W-2 (from latest stub)
      2. Projected additional income by type
      3. Combined W-2 (YTD + projected)

    \b
    Output format matches w2-extract output for use with 'taxes' command.

    Examples:
      pay-calc w2-generate 2025 him
      pay-calc w2-generate 2025 him --employer "Employer A LLC"
      pay-calc w2-generate 2025 him --final-stub-date 2025-12-19
      pay-calc w2-generate 2025 him --include-projection --price 175.50
    """
    from paycalc.sdk import generate_w2_from_analysis, save_w2_forms, get_data_path
    from paycalc.sdk.income_projection import generate_projection, is_rsus_enabled

    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    try:
        w2_data = generate_w2_from_analysis(
            year=year,
            party=party,
            final_stub_date=final_stub_date,
            employer_filter=employer,
        )
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except ValueError as e:
        # Convert ValueError to user-friendly message
        end_date = str(e).split("through ")[1].split(".")[0] if "through" in str(e) else "unknown"
        raise click.ClickException(
            f"Analysis data only covers through {end_date}.\n"
            f"Either:\n"
            f"  1. Import more pay stubs and re-run analysis\n"
            f"  2. Use --final-stub-date {end_date} to confirm this is the final stub"
        )

    # Save to file
    output_path = Path(output) if output else None
    saved_path = save_w2_forms(w2_data, output_path)

    # Display results
    date_range = w2_data.get("analysis_date_range", {})
    form = w2_data["forms"][0]
    w2_box = form["data"]

    # Section 1: YTD W-2
    click.echo("=" * 60)
    click.echo("SECTION 1: YTD W-2 (from latest stub)")
    click.echo("=" * 60)
    click.echo(f"  Source: {form['source_file']}")
    click.echo(f"  Date range: {date_range.get('start')} to {date_range.get('end')}")
    click.echo(f"  Employer(s): {form['employer']}")
    click.echo()
    _print_w2_boxes(w2_box)

    # Section 2 & 3: Projection (if requested)
    if include_projection:
        # Load analysis data for projection
        data_path = get_data_path()
        analysis_file = data_path / f"{year}_{party}_pay_all.json"

        if analysis_file.exists():
            with open(analysis_file) as f:
                analysis_data = json.load(f)

            stubs = analysis_data.get("stubs", [])
            if stubs:
                proj = generate_projection(stubs, year, party=party, stock_price=price)

                if proj and proj.get("days_remaining", 0) > 0:
                    additional = proj.get("projected_additional", {})

                    # Section 2: Projected Additional Income
                    click.echo()
                    click.echo("=" * 60)
                    click.echo(f"SECTION 2: Projected Additional Income ({proj.get('days_remaining')} days remaining)")
                    click.echo("=" * 60)
                    click.echo(f"  Regular Pay:    ${additional.get('regular_pay', 0):>12,.2f}")
                    click.echo(f"  Stock Vesting:  ${additional.get('stock_grants', 0):>12,.2f}")
                    click.echo(f"  ─────────────────────────────")
                    click.echo(f"  Total:          ${additional.get('total_gross', 0):>12,.2f}")
                    click.echo(f"  Est. Taxes:     ${additional.get('taxes', 0):>12,.2f}")

                    # Show warnings if any
                    stock_info = proj.get("stock_grant_info", {})
                    warnings = stock_info.get("warnings", [])
                    if warnings:
                        click.echo()
                        click.echo("  Warnings:")
                        for w in warnings:
                            click.echo(f"    - {w}")

                    # Section 3: Combined W-2
                    click.echo()
                    click.echo("=" * 60)
                    click.echo("SECTION 3: Combined W-2 (YTD + Projected)")
                    click.echo("=" * 60)

                    # Calculate combined values
                    combined_w2 = {
                        "wages_tips_other_comp": w2_box["wages_tips_other_comp"] + additional.get("total_gross", 0),
                        "federal_income_tax_withheld": w2_box["federal_income_tax_withheld"] + additional.get("taxes", 0),
                        "social_security_wages": min(
                            w2_box["medicare_wages_and_tips"] + additional.get("total_gross", 0),
                            176100  # 2025 SS wage base
                        ),
                        "social_security_tax_withheld": w2_box["social_security_tax_withheld"],  # Estimate same
                        "medicare_wages_and_tips": w2_box["medicare_wages_and_tips"] + additional.get("total_gross", 0),
                        "medicare_tax_withheld": w2_box["medicare_tax_withheld"],  # Estimate same
                    }
                    _print_w2_boxes(combined_w2)
                else:
                    click.echo()
                    click.echo("No additional projection - year appears complete.")

    click.echo()
    click.echo(f"Output: {saved_path}")


def _print_w2_boxes(w2_box: dict):
    """Print W-2 box values."""
    click.echo("W-2 Box Values:")
    click.echo(f"  Box 1 (Wages):           ${w2_box['wages_tips_other_comp']:>12,.2f}")
    click.echo(f"  Box 2 (Federal WH):      ${w2_box['federal_income_tax_withheld']:>12,.2f}")
    click.echo(f"  Box 3 (SS Wages):        ${w2_box['social_security_wages']:>12,.2f}")
    click.echo(f"  Box 4 (SS Tax):          ${w2_box['social_security_tax_withheld']:>12,.2f}")
    click.echo(f"  Box 5 (Medicare Wages):  ${w2_box['medicare_wages_and_tips']:>12,.2f}")
    click.echo(f"  Box 6 (Medicare Tax):    ${w2_box['medicare_tax_withheld']:>12,.2f}")


@cli.command("analysis")
@click.argument("year")
@click.argument("party", type=click.Choice(["him", "her"]))
@click.option("--through-date", type=str, help="Only process pay stubs through this date (YYYY-MM-DD).")
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text", help="Output format.")
def analysis(year, party, through_date, output_format):
    """Analyze imported pay stubs and validate YTD totals.

    Reads pay stubs from records storage (imported via 'records import'),
    validates continuity (gaps, employer changes), and reports
    YTD totals including 401k contributions.

    \b
    Prerequisite: Import records first:
        pay-calc records import

    \b
    Output: ~/.local/share/pay-calc/YYYY_party_full.json

    YEAR should be a 4-digit year (e.g., 2025).
    PARTY is 'him' or 'her'.
    """
    from paycalc.sdk import get_data_path, detect_gaps, check_first_stub_ytd
    from datetime import datetime
    import json

    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    if through_date:
        try:
            datetime.strptime(through_date, "%Y-%m-%d")
        except ValueError:
            raise click.BadParameter(f"Invalid date format '{through_date}'. Use YYYY-MM-DD.")

    # Load stubs from records storage
    records_dir = get_data_path() / "records" / year / party
    if not records_dir.exists():
        raise click.ClickException(
            f"No records found for {year}/{party}.\n"
            f"Run 'pay-calc records import' first."
        )

    stub_files = sorted(records_dir.glob("*.json"))
    if not stub_files:
        raise click.ClickException(
            f"No record files in {records_dir}.\n"
            f"Run 'pay-calc records import' first."
        )

    # Load all stubs (unwrap from records format)
    all_stubs = []
    for stub_file in stub_files:
        with open(stub_file) as f:
            record = json.load(f)
            # Records have meta/data wrapper; extract the data portion
            if "data" in record and "meta" in record:
                stub = record["data"]
                stub["_meta"] = record["meta"]
            else:
                stub = record  # Legacy format without wrapper
            stub["_source_file"] = stub_file.name
            all_stubs.append(stub)

    click.echo(f"Loaded {len(all_stubs)} records from {records_dir}")

    # Sort by pay_date
    all_stubs.sort(key=lambda s: (s.get("pay_date", ""), s.get("pay_summary", {}).get("ytd", {}).get("gross", 0)))

    # Filter by through_date if specified
    if through_date:
        original_count = len(all_stubs)
        all_stubs = [s for s in all_stubs if s.get("pay_date", "") <= through_date]
        filtered = original_count - len(all_stubs)
        if filtered > 0:
            click.echo(f"Filtered out {filtered} stubs after {through_date}")

    # Import analysis functions
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from analysis import (
        validate_year_totals,
        validate_stub_deltas,
        generate_summary,
        generate_401k_contributions,
        generate_imputed_income_summary,
        generate_ytd_breakdown,
        print_text_report,
        identify_pay_type,
    )

    # Add pay type to stubs that don't have it
    for stub in all_stubs:
        if "_pay_type" not in stub:
            stub["_pay_type"] = identify_pay_type(stub)

    # Validate gaps using SDK
    gap_analysis = detect_gaps(all_stubs, year, filter_regular_only=True)
    gap_errors, gap_warnings = gap_analysis.to_errors_warnings()

    # Check first stub YTD
    ytd_error = check_first_stub_ytd(all_stubs)
    if ytd_error:
        gap_errors.insert(0, ytd_error)

    # Validate totals
    totals_errors, totals_warnings, totals_comparison = validate_year_totals(all_stubs)

    # Validate per-stub deltas
    delta_errors, delta_warnings = validate_stub_deltas(all_stubs)

    # Combine errors and warnings
    errors = gap_errors + totals_errors + delta_errors
    warnings = gap_warnings + totals_warnings + delta_warnings

    # Build report
    report = {
        "summary": generate_summary(all_stubs, year),
        "errors": errors,
        "warnings": warnings,
        "totals_validation": totals_comparison,
        "contributions_401k": generate_401k_contributions(all_stubs),
        "imputed_income": generate_imputed_income_summary(all_stubs),
        "ytd_breakdown": generate_ytd_breakdown(all_stubs),
        "stubs": all_stubs
    }

    # Output
    if output_format == "json":
        click.echo(json.dumps(report, indent=2))
    else:
        print_text_report(report)

    # Save to data directory
    data_dir = get_data_path()
    data_dir.mkdir(parents=True, exist_ok=True)
    output_file = data_dir / f"{year}_{party}_pay_all.json"

    with open(output_file, "w") as f:
        json.dump(report, f, indent=2)

    click.echo(f"\nSaved to: {output_file}")


@cli.command("projection")
@click.argument("year")
@click.argument("party", type=click.Choice(["him", "her"]))
@click.option("--input", "-i", "input_file", type=click.Path(exists=True), help="Input JSON from analysis (default: XDG data dir)")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON instead of formatted text")
@click.option("--price", type=float, help="Stock price for RSU value projection")
def projection(year, party, input_file, output_json, price):
    """Project year-end totals from partial year pay data.

    Reads pay stub data from analysis output and projects year-end
    totals based on observed pay patterns (regular pay cadence, stock
    vesting schedule).

    \b
    Prerequisite: Run analysis first to generate the input file.
        pay-calc analysis <year> <party>

    \b
    Input:  YYYY_party_pay_all.json (from analysis)
    Output: Year-end projection report

    Use this for mid-year tax planning when full W-2 data is not yet available.

    YEAR should be a 4-digit year (e.g., 2025).
    PARTY is 'him' or 'her'.
    """
    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    from paycalc.sdk import get_data_path
    from paycalc.sdk.income_projection import generate_projection

    # Determine input file path
    if input_file:
        input_path = Path(input_file)
    else:
        input_path = get_data_path() / f"{year}_{party}_pay_all.json"

    # Check if input file exists with clear guidance
    if not input_path.exists():
        click.echo(f"Error: Input file not found: {input_path}", err=True)
        click.echo("", err=True)
        click.echo("Run analysis first to generate the required input:", err=True)
        click.echo(f"    pay-calc analysis {year} {party}", err=True)
        click.echo("", err=True)
        click.echo("Then run projection again:", err=True)
        click.echo(f"    pay-calc projection {year} {party}", err=True)
        raise SystemExit(1)

    # Load analysis data
    with open(input_path) as f:
        analysis_data = json.load(f)

    stubs = analysis_data.get("stubs", [])
    if not stubs:
        raise click.ClickException(f"No pay stub data found in {input_path}")

    # Generate projection using SDK (pass party to enable RSU SDK if configured)
    proj = generate_projection(stubs, year, party=party, stock_price=price)

    if output_json:
        click.echo(json.dumps(proj, indent=2))
        return

    if not proj:
        click.echo("No projection data available - year may be complete.")
        return

    # Format text output
    _print_projection_report(proj, analysis_data)


def _print_projection_report(proj: dict, analysis_data: dict):
    """Print formatted projection report."""
    summary = analysis_data.get("summary", {})
    ytd_breakdown = analysis_data.get("ytd_breakdown", {})
    contrib_401k = analysis_data.get("contributions_401k", {}).get("yearly_totals", {})

    click.echo("=" * 60)
    click.echo(f"YEAR-END PROJECTION (as of {proj.get('as_of_date', 'N/A')}, {proj.get('days_remaining', 0)} days remaining)")
    click.echo("=" * 60)
    click.echo()

    actual = proj.get("actual", {})
    additional = proj.get("projected_additional", {})
    total = proj.get("projected_total", {})

    # Get 401k totals
    total_401k = contrib_401k.get("total", 0)
    total_annual_limit = 70000  # Default, could load from tax rules

    needed_401k = max(0, total_annual_limit - total_401k)
    reg_proj = additional.get("regular_pay", 0)
    projected_401k_add = min(needed_401k, reg_proj)
    projected_401k_total = total_401k + projected_401k_add

    actual_total_comp = actual.get('gross', 0) + total_401k
    projected_total_comp = total.get('gross', 0) + projected_401k_total

    # Main projection table
    click.echo(f"  {'Category':<25} {'Actual':>14} {'Projected Add':>14} {'Est. Total':>14}")
    click.echo(f"  {'─' * 25} {'─' * 14} {'─' * 14} {'─' * 14}")

    # Gross
    click.echo(f"  {'Gross':<25} ${actual.get('gross', 0):>13,.2f} ${additional.get('total_gross', 0):>13,.2f} ${total.get('gross', 0):>13,.2f}")

    # Break down by type
    ytd_earnings = ytd_breakdown.get("earnings", {}) if ytd_breakdown else {}
    actual_regular = ytd_earnings.get("Regular Pay", 0)
    actual_stock = ytd_earnings.get("Goog Stock Unit", 0)
    actual_other = actual.get('gross', 0) - actual_regular - actual_stock

    stock_proj = additional.get("stock_grants", 0)
    reg_proj_display = reg_proj - projected_401k_add

    click.echo(f"    {'└ Regular Pay':<23} ${actual_regular:>13,.2f} ${reg_proj_display:>13,.2f}")
    click.echo(f"    {'└ Stock Vesting':<23} ${actual_stock:>13,.2f} ${stock_proj:>13,.2f}")
    click.echo(f"    {'└ Other (bonuses, etc)':<23} ${actual_other:>13,.2f} {'$0.00':>14}")

    if total_401k > 0 or projected_401k_add > 0:
        click.echo(f"  {'+ 401k Contributions':<25} ${total_401k:>13,.2f} ${projected_401k_add:>13,.2f} ${projected_401k_total:>13,.2f}")

    projected_comp_add = additional.get('total_gross', 0) + projected_401k_add
    click.echo(f"  {'─' * 25} {'─' * 14} {'─' * 14} {'─' * 14}")
    click.echo(f"  {'Total Compensation':<25} ${actual_total_comp:>13,.2f} ${projected_comp_add:>13,.2f} ${projected_total_comp:>13,.2f}")

    click.echo(f"  {'Taxes Withheld':<25} ${actual.get('taxes_withheld', 0):>13,.2f} ${additional.get('taxes', 0):>13,.2f} ${total.get('taxes_withheld', 0):>13,.2f}")
    click.echo(f"  {'─' * 25} {'─' * 14} {'─' * 14} {'─' * 14}")

    # Pattern info
    reg_info = proj.get("regular_pay_info", {})
    stock_info = proj.get("stock_grant_info", {})

    if reg_info:
        click.echo(f"\n  Regular Pay Pattern:")
        click.echo(f"    Frequency: {reg_info.get('frequency', 'unknown')} ({reg_info.get('interval_days', 0)} days)")
        click.echo(f"    Last pay date: {reg_info.get('last_pay_date', 'unknown')}")
        click.echo(f"    Last amount: ${reg_info.get('last_amount', 0):,.2f}")
        click.echo(f"    Remaining periods: {reg_info.get('remaining_periods', 0)}")

    if stock_info:
        click.echo(f"\n  Stock Vesting Pattern:")
        source = stock_info.get('source', 'unknown')
        if source == 'rsu_sdk':
            click.echo(f"    Source: RSU Schedule (from SDK)")
            after_date = stock_info.get('after_date', '')
            if after_date:
                click.echo(f"    Projecting after: {after_date}")
            click.echo(f"    Projected shares: {stock_info.get('rsu_shares', 0):,.4f}")
            if stock_info.get('price'):
                click.echo(f"    Stock price: ${stock_info.get('price'):,.2f}")
                click.echo(f"    Projected value: ${stock_info.get('projected', 0):,.2f}")
            months = stock_info.get('months_covered', [])
            if months:
                click.echo(f"    Months with vests: {', '.join(months)}")
            warnings = stock_info.get('warnings', [])
            if warnings:
                click.echo(f"    Warnings:")
                for w in warnings:
                    click.echo(f"      - {w}")
        else:
            # Stub-inference format
            month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
            click.echo(f"    Frequency: {stock_info.get('frequency', 'unknown')}")
            remaining = stock_info.get('remaining_months', [])
            remaining_names = [month_names[m-1] for m in remaining]
            click.echo(f"    Remaining months: {', '.join(remaining_names) if remaining_names else 'none'}")
            click.echo(f"    Avg per vest: ${stock_info.get('avg_vesting', 0):,.2f}")
            click.echo(f"    Remaining vests: {stock_info.get('remaining_vests', 0)}")

    click.echo("\n" + "=" * 60)


@cli.command("stock-quote")
@click.argument("ticker")
@click.option("--last-closed", is_flag=True, required=True, help="Get the last closing price (required).")
def stock_quote(ticker, last_closed):
    """Get the last closing price for a stock ticker.

    Uses AI to look up the most recent closing price. Requires --last-closed
    flag (real-time quotes not supported).

    Examples:
      pay-calc stock-quote GOOG --last-closed
      pay-calc stock-quote AAPL --last-closed
    """
    from gemini_client import get_stock_quote

    try:
        price = get_stock_quote(ticker)
        click.echo(f"{price:.2f}")
    except ValueError as e:
        raise click.ClickException(str(e))
    except RuntimeError as e:
        raise click.ClickException(f"Failed to get quote: {e}")


@cli.command("reset")
@click.option("--force", is_flag=True, help="Skip confirmation prompt.")
def reset(force: bool):
    """Reset all pay-calc data (stubs, analysis output, cache).

    This removes all data from the data directory (~/.local/share/pay-calc/)
    but preserves configuration (~/.config/pay-calc/).

    Use this to start fresh before re-importing pay stubs.
    """
    from paycalc.sdk import get_data_path
    import shutil

    data_dir = get_data_path()

    if not data_dir.exists():
        click.echo(f"Data directory does not exist: {data_dir}")
        return

    # Count what will be deleted
    stubs_dir = data_dir / "stubs"
    stub_count = 0
    analysis_count = 0
    other_files = []

    if stubs_dir.exists():
        for year_dir in stubs_dir.iterdir():
            if year_dir.is_dir():
                for party_dir in year_dir.iterdir():
                    if party_dir.is_dir():
                        stub_count += len(list(party_dir.glob("*.json")))

    for f in data_dir.glob("*.json"):
        if "_full.json" in f.name or "_pay_all.json" in f.name or "_w2" in f.name:
            analysis_count += 1
        else:
            other_files.append(f.name)

    # Show what will be deleted
    click.echo(f"Data directory: {data_dir}")
    click.echo(f"\nWill delete:")
    click.echo(f"  - {stub_count} stub files")
    click.echo(f"  - {analysis_count} analysis/W-2 output files")
    if other_files:
        click.echo(f"  - {len(other_files)} other files: {', '.join(other_files[:5])}")
        if len(other_files) > 5:
            click.echo(f"    ... and {len(other_files) - 5} more")

    click.echo(f"\nConfiguration preserved: ~/.config/pay-calc/")

    if not force:
        click.confirm("\nProceed with reset?", abort=True)

    # Delete the data directory contents
    if stubs_dir.exists():
        shutil.rmtree(stubs_dir)
        click.echo("Deleted stubs directory")

    for f in data_dir.glob("*.json"):
        f.unlink()
        click.echo(f"Deleted {f.name}")

    # Also clear any subdirectories (cache, etc.) except stubs (already handled)
    for item in data_dir.iterdir():
        if item.is_dir() and item.name != "stubs":
            shutil.rmtree(item)
            click.echo(f"Deleted {item.name}/")

    click.echo(click.style("\nReset complete.", fg='green'))


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
