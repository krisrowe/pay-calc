"""Pay Calc CLI - Command-line interface for pay and tax projections."""

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
cli.add_command(stubs_group)
cli.add_command(records_group, name="records")


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


@cli.command("analysis")
@click.argument("year")
@click.argument("party", type=click.Choice(["him", "her"]))
@click.option("--through-date", type=str, help="Only process pay stubs through this date (YYYY-MM-DD).")
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text", help="Output format.")
def analysis(year, party, through_date, output_format):
    """Analyze imported pay stubs and validate YTD totals.

    Reads pay stubs from local storage (imported via 'stubs import'),
    validates continuity (gaps, employer changes), and reports
    YTD totals including 401k contributions.

    \b
    Prerequisite: Import stubs first:
        pay-calc stubs import <year> <party> <source>

    \b
    Output: ~/.local/share/pay-calc/YYYY_party_pay_all.json

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

    # Load stubs from local storage
    stubs_dir = get_data_path() / "stubs" / year / party
    if not stubs_dir.exists():
        raise click.ClickException(
            f"No stubs found for {year}/{party}.\n"
            f"Run 'pay-calc stubs import {year} {party} <source>' first."
        )

    stub_files = sorted(stubs_dir.glob("*.json"))
    if not stub_files:
        raise click.ClickException(
            f"No stub files in {stubs_dir}.\n"
            f"Run 'pay-calc stubs import {year} {party} <source>' first."
        )

    # Load all stubs
    all_stubs = []
    for stub_file in stub_files:
        with open(stub_file) as f:
            stub = json.load(f)
            stub["_source_file"] = stub_file.name
            all_stubs.append(stub)

    click.echo(f"Loaded {len(all_stubs)} stubs from {stubs_dir}")

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
        "ytd_breakdown": generate_ytd_breakdown(all_stubs) if not errors else None,
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

    # Always include ytd_breakdown in saved file
    if report["ytd_breakdown"] is None:
        report["ytd_breakdown"] = generate_ytd_breakdown(all_stubs)

    with open(output_file, "w") as f:
        json.dump(report, f, indent=2)

    click.echo(f"\nSaved to: {output_file}")


@cli.command("projection")
@click.argument("year")
@click.argument("party", type=click.Choice(["him", "her"]))
@click.option("--input", "-i", "input_file", type=click.Path(exists=True), help="Input JSON from analysis (default: XDG data dir)")
def projection(year, party, input_file):
    """Project year-end totals from partial year pay data.

    Reads pay stub data from analysis output and projects year-end
    totals based on observed pay patterns (regular pay cadence, stock
    vesting schedule).

    \b
    Prerequisite: Run analysis first to generate the input file.
        pay-calc analysis <year> <party> --cache

    \b
    Input:  YYYY_party_full.json (from analysis)
    Output: Year-end projection report

    Use this for mid-year tax planning when full W-2 data is not yet available.

    YEAR should be a 4-digit year (e.g., 2025).
    PARTY is 'him' or 'her'.
    """
    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    from paycalc.sdk import get_data_path

    # Determine input file path
    if input_file:
        input_path = Path(input_file)
    else:
        input_path = get_data_path() / f"{year}_{party}_full.json"

    # Check if input file exists with clear guidance
    if not input_path.exists():
        click.echo(f"Error: Input file not found: {input_path}", err=True)
        click.echo("", err=True)
        click.echo("Run analysis first to generate the required input:", err=True)
        click.echo(f"    pay-calc analysis {year} {party} --cache", err=True)
        click.echo("", err=True)
        click.echo("Then run projection again:", err=True)
        click.echo(f"    pay-calc projection {year} {party}", err=True)
        raise SystemExit(1)

    import subprocess

    # Call projection.py with the input file
    cmd = ["python3", "projection.py", str(input_path)]

    try:
        result = subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"Projection failed with exit code {e.returncode}")


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
