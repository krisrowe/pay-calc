"""Pay Calc CLI - Command-line interface for pay and tax projections."""

import sys
from pathlib import Path

import click

from paycalc import __version__
from paycalc.sdk import ConfigNotFoundError

# Add parent directory to path for importing existing modules
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from .config_commands import config as config_group


@click.group()
@click.version_option(version=__version__, prog_name="pay-calc")
def cli():
    """Pay Calc - Personal pay and tax projection tools.

    Commands for extracting W-2 data, processing pay stubs,
    and generating tax projections.

    Configuration is loaded from (in order):

    \b
    1. PAY_CALC_CONFIG_PATH environment variable
    2. ./pay-calc/config.yaml in current directory
    3. ~/.config/pay-calc/config.yaml (XDG default)

    Run 'pay-calc config init' to create a new configuration.
    """
    pass


# Add config subcommand group
cli.add_command(config_group)


@cli.command("w2-extract")
@click.argument("year")
@click.option("--cache", is_flag=True, help="Cache downloaded files locally for reuse.")
def w2_extract(year, cache):
    """Extract W-2 data from PDFs stored in Google Drive.

    Downloads W-2 PDFs and manual JSON files from the configured
    Google Drive folder for YEAR, parses them, and outputs
    aggregated W-2 data to XDG data directory.
    """
    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    from drive_sync import sync_w2_pay_records, load_config
    from extract_w2 import (
        find_company_and_party_from_keywords,
        extract_text_from_pdf,
        parse_w2_text,
    )
    from paycalc.sdk import get_data_path
    from collections import defaultdict
    import json

    data_dir = get_data_path()

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
        raise click.ClickException("Unidentified PDFs found. Add keywords to config.yaml.")

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


@cli.command("tax-projection")
@click.argument("year")
def tax_projection(year):
    """Generate tax projection from W-2 data.

    Reads W-2 data from XDG data directory and calculates federal
    income tax, medicare taxes, and projected refund or amount owed.

    Output is written to XDG data directory.
    """
    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    from paycalc.sdk import generate_tax_projection, get_data_path

    data_dir = get_data_path()

    try:
        output_path = generate_tax_projection(year)
        click.echo(f"Loading W-2 data for him from {data_dir / f'{year}_him_w2_forms.json'}...")
        click.echo(f"Loading W-2 data for her from {data_dir / f'{year}_her_w2_forms.json'}...")
        click.echo(f"\nSuccessfully generated tax projection CSV: {output_path}")

    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Error generating tax projection: {e}")


@cli.command("pay-projection")
@click.argument("year")
@click.option("--cache", is_flag=True, help="Cache downloaded pay stub PDFs locally.")
def pay_projection(year, cache):
    """Process pay stubs and project year-end compensation.

    Downloads pay stub PDFs from Google Drive, extracts earnings
    and deduction data, and projects full-year totals including
    401k contributions.

    YEAR should be a 4-digit year (e.g., 2025).
    """
    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    import subprocess

    # Call process_year.py with appropriate arguments
    cmd = ["python3", "process_year.py", year]
    if cache:
        cmd.append("--cache-paystubs")

    try:
        result = subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"Pay projection failed with exit code {e.returncode}")


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
