"""Pay Calc CLI - Command-line interface for pay and tax projections."""

import sys
from pathlib import Path

import click

from paycalc import __version__
from paycalc.sdk import ConfigNotFoundError

# Add parent directory to path for importing existing modules
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from .profile_commands import profile as profile_group


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


# Add profile subcommand group
cli.add_command(profile_group)


@cli.command("w2-extract")
@click.argument("year")
@click.option("--cache", is_flag=True, help="Cache downloaded files locally for reuse.")
@click.option("--output-dir", "-o", type=click.Path(), help="Output directory for W-2 JSON files (default: XDG data dir)")
def w2_extract(year, cache, output_dir):
    """Extract W-2 data from PDFs stored in Google Drive.

    Downloads W-2 PDFs and manual JSON files from the configured
    Google Drive folder for YEAR, parses them, and outputs
    aggregated W-2 data to XDG data directory or --output-dir.
    """
    from paycalc.sdk import validate_profile, ProfileNotFoundError

    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    # Validate profile has required configuration
    try:
        validation = validate_profile()
        validation.require_feature("w2_extract")
    except (ProfileNotFoundError, ConfigNotFoundError) as e:
        raise click.ClickException(str(e))

    from drive_sync import sync_w2_pay_records, load_config
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

    # Sync files from Drive
    try:
        source_dir = sync_w2_pay_records(year, use_cache=cache)
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
@click.option("--cache", is_flag=True, help="Cache downloaded pay stub PDFs locally.")
@click.option("--through-date", type=str, help="Only process pay stubs through this date (YYYY-MM-DD). Useful for comparing against historical baselines.")
def analysis(year, party, cache, through_date):
    """Analyze pay stubs and validate YTD totals.

    Downloads pay stub PDFs from Google Drive for the specified party,
    validates continuity (gaps, employer changes), and reports
    YTD totals including 401k contributions.

    \b
    Output: YYYY_party_full.json (e.g., 2025_him_full.json)

    YEAR should be a 4-digit year (e.g., 2025).
    PARTY is 'him' or 'her'.
    """
    from paycalc.sdk import validate_profile, ProfileNotFoundError

    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    # Validate profile has required configuration
    try:
        validation = validate_profile()
        validation.require_feature("pay_stubs")
    except (ProfileNotFoundError, ConfigNotFoundError) as e:
        raise click.ClickException(str(e))

    if through_date:
        # Validate date format
        from datetime import datetime
        try:
            datetime.strptime(through_date, "%Y-%m-%d")
        except ValueError:
            raise click.BadParameter(f"Invalid date format '{through_date}'. Use YYYY-MM-DD.")

    import subprocess

    # Call analysis.py with appropriate arguments
    cmd = ["python3", "analysis.py", year, party]
    if cache:
        cmd.append("--cache-paystubs")
    if through_date:
        cmd.extend(["--through-date", through_date])

    try:
        result = subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"Analysis failed with exit code {e.returncode}")


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


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
