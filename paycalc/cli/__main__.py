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


def _format_tax_projection_text(proj: dict) -> str:
    """Format tax projection as ASCII tables for terminal display."""
    lines = []

    # Header
    lines.append(f"TAX PROJECTION FOR {proj['year']}")
    lines.append("=" * 60)
    lines.append("")

    # Income section - show all components that build to taxable income
    # Column adds up: wages + supplemental income = total income - deductions = taxable
    lines.append("INCOME")
    lines.append("-" * 60)

    # Wages with him/her breakdown shown to the side
    him_wages = proj['him_wages']
    her_wages = proj['her_wages']
    combined_wages = proj['combined_wages']
    lines.append(f"  {'Wages (Line 1a)':<24} ${combined_wages:>12,.2f}  (Him: ${him_wages:,.0f} / Her: ${her_wages:,.0f})")

    # Supplemental income items (with source metadata)
    supplemental = proj.get("supplemental", {})
    income_items = [
        ("interest_income", "Interest income"),
        ("dividend_income", "Dividends"),
        ("short_term_gain_loss", "Short-term cap gain/loss"),
        ("long_term_gain_loss", "Long-term cap gain/loss"),
        ("schedule_1_income", "Schedule 1 income"),
    ]
    def _format_source(info):
        source = info.get("source", "")
        year = info.get("year", "")
        if source == "1040":
            return f"  ({year} Form 1040)" if year else "  (Form 1040)"
        elif source:
            return f"  ({year} {source})" if year else f"  ({source})"
        return ""

    for name, label in income_items:
        info = supplemental.get(name, {})
        value = info.get("value", 0)
        if value != 0:
            source_str = _format_source(info)
            if value >= 0:
                lines.append(f"  {label:<24} ${value:>12,.2f}{source_str}")
            else:
                lines.append(f"  {label:<24}-${abs(value):>12,.2f}{source_str}")

    lines.append(f"  {'Total income (Line 9)':<24} ${proj['total_income']:>12,.2f}")
    lines.append(f"  {'Standard deduction':<24}-${proj['standard_deduction']:>12,.2f}")

    # QBI deduction if present
    qbi_info = supplemental.get("qbi_deduction", {})
    qbi_value = qbi_info.get("value", 0)
    if qbi_value > 0:
        source_str = _format_source(qbi_info)
        lines.append(f"  {'QBI deduction (Line 13)':<24}-${qbi_value:>12,.2f}{source_str}")

    lines.append("  " + "-" * 38)
    lines.append(f"  {'Taxable income (Line 15)':<24} ${proj['final_taxable_income']:>12,.2f}")
    lines.append("")

    # Tax brackets
    lines.append("FEDERAL INCOME TAX BRACKETS (MFJ)")
    lines.append("-" * 60)
    lines.append(f"  {'Bracket':<20} {'Rate':>8} {'Tax Assessed':>15}")
    lines.append(f"  {'-'*20} {'-'*8} {'-'*15}")

    previous_max = 0
    for bracket in proj["tax_brackets"]:
        rate = bracket["rate"]
        if "up_to" in bracket:
            bracket_max = bracket["up_to"]
            income_in_bracket = min(proj["final_taxable_income"], bracket_max) - previous_max
            income_in_bracket = max(0, income_in_bracket)
            tax = income_in_bracket * rate
            bracket_str = f"${previous_max:,.0f} - ${bracket_max:,.0f}"
            previous_max = bracket_max
        elif "over" in bracket:
            income_in_bracket = max(0, proj["final_taxable_income"] - bracket["over"])
            tax = income_in_bracket * rate
            bracket_str = f"Over ${bracket['over']:,.0f}"
        lines.append(f"  {bracket_str:<20} {rate:>7.0%} ${tax:>14,.2f}")

    lines.append(f"  {'-'*20} {'-'*8} {'-'*15}")
    lines.append(f"  {'Total Federal Tax':<20} {'':<8} ${proj['federal_income_tax_assessed']:>14,.2f}")
    lines.append("")

    # Medicare section
    lines.append("MEDICARE TAXES")
    lines.append("-" * 40)
    lines.append(f"  Combined medicare wages:   ${proj['combined_medicare_wages']:>12,.2f}")
    lines.append(f"  Medicare taxes withheld:   ${proj['combined_medicare_withheld']:>12,.2f}")
    lines.append(f"  Medicare taxes assessed:  -${proj['total_medicare_taxes_assessed']:>12,.2f}")
    medicare_label = "Medicare refund:" if proj["medicare_refund"] >= 0 else "Medicare owed:"
    lines.append(f"  {medicare_label:<25} ${proj['medicare_refund']:>12,.2f}")
    lines.append("")

    # SS overpayment section (only show if there's overpayment)
    total_ss_overpayment = proj.get("total_ss_overpayment", 0)
    if total_ss_overpayment > 0:
        lines.append("SOCIAL SECURITY OVERPAYMENT")
        lines.append("-" * 40)
        him_ss_overpayment = proj.get('him_ss_overpayment', 0)
        her_ss_overpayment = proj.get('her_ss_overpayment', 0)
        if him_ss_overpayment > 0:
            lines.append(f"  Party 1 SS withheld:       ${proj.get('him_ss_withheld', 0):>12,.2f}")
            lines.append(f"  Party 1 SS overpayment:    ${him_ss_overpayment:>12,.2f}")
        if her_ss_overpayment > 0:
            lines.append(f"  Party 2 SS withheld:       ${proj.get('her_ss_withheld', 0):>12,.2f}")
            lines.append(f"  Party 2 SS overpayment:    ${her_ss_overpayment:>12,.2f}")
        lines.append(f"  Total SS credit:           ${total_ss_overpayment:>12,.2f}")
        lines.append("")

    # Withholding section
    lines.append("WITHHOLDING")
    lines.append("-" * 40)
    lines.append(f"  His fed tax withheld:      ${proj['him_fed_withheld']:>12,.2f}")
    lines.append(f"  Her fed tax withheld:      ${proj['her_fed_withheld']:>12,.2f}")
    total_withheld = proj["him_fed_withheld"] + proj["her_fed_withheld"]
    lines.append(f"  Total withheld:            ${total_withheld:>12,.2f}")
    lines.append("")

    # Final result - show all components that sum to refund
    lines.append("TAX RETURN PROJECTION")
    lines.append("-" * 40)
    lines.append(f"  Federal income tax:       -${proj['federal_income_tax_assessed']:>12,.2f}")

    # Additional Medicare tax (Schedule 2 Line 6)
    additional_medicare = proj.get('additional_medicare_tax', 0)
    if additional_medicare > 0:
        lines.append(f"  Additional Medicare tax:  -${additional_medicare:>12,.2f}")

    # Supplemental taxes/credits that affect Line 24 (with source metadata)
    supplemental = proj.get("supplemental", {})

    def _get_source_str(info):
        source = info.get("source", "")
        year = info.get("year", "")
        if source == "1040":
            return f"  ({year} Form 1040)" if year else "  (Form 1040)"
        elif source:
            return f"  ({year} {source})" if year else f"  ({source})"
        return ""

    niit_info = supplemental.get("niit", {})
    niit = niit_info.get("value", 0)
    if niit > 0:
        lines.append(f"  NIIT (Sched 2 Line 7):    -${niit:>12,.2f}{_get_source_str(niit_info)}")

    other_info = supplemental.get("other_taxes", {})
    other_taxes = other_info.get("value", 0)
    if other_taxes > 0:
        lines.append(f"  Other taxes (Sched 2):    -${other_taxes:>12,.2f}{_get_source_str(other_info)}")

    # Child care credit is calculated from expenses - get source from expenses
    expenses_info = supplemental.get("child_care_expenses", {})
    child_care_credit = proj.get("child_care_credit", 0)
    if child_care_credit > 0:
        lines.append(f"  Child care credit:         ${child_care_credit:>12,.2f}{_get_source_str(expenses_info)}")

    lines.append(f"  Tentative tax (Line 24):  -${proj['tentative_tax_per_return']:>12,.2f}")
    lines.append(f"  Total withheld:            ${total_withheld:>12,.2f}")

    # Form 8959 additional Medicare withheld (Line 25c)
    form_8959_withheld = proj.get('form_8959_withholding', 0)
    if form_8959_withheld > 0:
        lines.append(f"  Form 8959 withheld:        ${form_8959_withheld:>12,.2f}")

    if total_ss_overpayment > 0:
        lines.append(f"  SS overpayment credit:     ${total_ss_overpayment:>12,.2f}")

    lines.append("  " + "=" * 38)
    if proj["final_refund"] >= 0:
        lines.append(f"  REFUND:                    ${proj['final_refund']:>12,.2f}")
    else:
        lines.append(f"  OWED:                     -${abs(proj['final_refund']):>12,.2f}")

    # Data sources section - use SDK helper for consistent formatting
    data_sources = proj.get("data_sources", {})
    if data_sources:
        from paycalc.sdk.tax import format_data_sources
        lines.append("")
        lines.append("DATA SOURCES")
        lines.append("-" * 60)
        # Indent each line from the SDK helper
        for line in format_data_sources(data_sources).split("\n"):
            lines.append(f"  {line}")

    # Supplemental income items now shown in INCOME section above
    # Taxes/credits shown in TAX RETURN PROJECTION section

    return "\n".join(lines)


@cli.group("tax")
def tax_group():
    """Tax projection and validation commands.

    \b
    Commands:
      project   Calculate tax liability from W-2 data
      convert   Convert projection JSON to Form 1040 schema
      compare   Compare two Form 1040 objects
      validate  Shortcut: project → convert → compare with actual 1040
    """
    pass


@tax_group.command("project")
@click.argument("year")
@click.option("--format", "output_format", type=click.Choice(["text", "json", "csv"]), default="text",
              help="Output format (default: text)")
@click.option("--data-dir", type=click.Path(exists=True),
              help="Directory containing W-2 or analysis JSON files (default: XDG data dir)")
@click.option("--ytd-final", "ytd_final", type=click.Choice(["all", "him", "her"]), default=None,
              help="Use stub YTD as-is: all (both parties), him, or her")
def tax_project(year, output_format, data_dir, ytd_final):
    """Calculate federal tax liability and refund/owed amount.

    Loads income data for both parties (him + her), applies tax brackets,
    and calculates federal income tax, medicare taxes, and projected
    refund or amount owed. All values are rounded to whole dollars per
    IRS rules.

    \b
    Data sources (per employer, in order of preference):
    1. Official W-2 records (imported W-2 forms)
    2. Latest stub from records → projected to year-end (default)
    3. Latest stub as-is (if --ytd-final)

    \b
    The --ytd-final option controls income projection:
      --ytd-final=all    Use final YTD for both parties (no projection)
      --ytd-final=him    Use final YTD for him, project her
      --ytd-final=her    Use final YTD for her, project him

    \b
    Output formats:
      --format=text  ASCII tables (default, for terminal viewing)
      --format=json  JSON object (for piping to tax convert)
      --format=csv   CSV format (for spreadsheet import)

    \b
    Pipeline usage:
      pay-calc tax project 2024 --format=json | pay-calc tax convert
    """
    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    from paycalc.sdk import generate_tax_projection, get_data_path
    from paycalc.sdk.tax import format_data_sources

    data_path = Path(data_dir) if data_dir else get_data_path()

    # --ytd-final controls per-party projection:
    #   None         -> project both parties (default)
    #   "all"        -> ytd-final for both (no projection)
    #   "him"/"her"  -> ytd-final for that party only
    ytd_final_party = ytd_final  # None, "all", "him", or "her"

    try:
        if output_format == "text":
            # Get JSON, format as ASCII tables (includes data sources in output)
            projection = generate_tax_projection(
                year, data_dir=data_path, output_format="json",
                ytd_final_party=ytd_final_party
            )
            click.echo(_format_tax_projection_text(projection))
        elif output_format == "json":
            # JSON output already includes data_sources in the response object
            projection = generate_tax_projection(
                year, data_dir=data_path, output_format="json",
                ytd_final_party=ytd_final_party
            )
            click.echo(json.dumps(projection, indent=2))
        else:  # csv
            # Get JSON for data sources, convert to CSV for output
            from paycalc.sdk.tax import projection_to_csv_string
            projection = generate_tax_projection(
                year, data_dir=data_path, output_format="json",
                ytd_final_party=ytd_final_party
            )
            csv_output = projection_to_csv_string(projection)
            click.echo(csv_output)
            # Print data sources to stderr for visibility
            data_sources = projection.get("data_sources", {})
            if data_sources:
                click.echo("\n--- Data Sources ---", err=True)
                click.echo(format_data_sources(data_sources), err=True)

    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Error generating tax calculation: {e}")


@tax_group.command("convert")
def tax_convert():
    """Convert projection JSON to Form 1040 schema format.

    Reads projection JSON from stdin (piped from tax project --format=json),
    validates the projection schema, and outputs Form 1040 schema JSON.

    \b
    Usage:
      pay-calc tax project 2024 --format=json | pay-calc tax convert

    \b
    The output can be:
    - Compared against an actual 1040 using: | pay-calc tax compare <actual.json>
    - Saved for later comparison
    """
    import sys
    from paycalc.sdk import projection_to_1040, ProjectionSchemaError

    if sys.stdin.isatty():
        raise click.ClickException(
            "No projection data provided. Pipe projection JSON to this command.\n"
            "Example: pay-calc tax project 2024 --format=json | pay-calc tax convert"
        )

    try:
        stdin_data = sys.stdin.read()
        projection = json.loads(stdin_data)
    except json.JSONDecodeError as e:
        raise click.ClickException(f"Invalid JSON input: {e}")

    try:
        form_1040 = projection_to_1040(projection)
        click.echo(json.dumps(form_1040, indent=2))
    except ProjectionSchemaError as e:
        raise click.ClickException(str(e))


@tax_group.command("compare")
@click.argument("actual_file", type=click.Path(exists=True))
def tax_compare(actual_file):
    """Compare two Form 1040 objects (calculated vs actual).

    Reads calculated 1040 JSON from stdin and compares against ACTUAL_FILE.
    Both must be in Form 1040 schema format.

    \b
    Usage:
      pay-calc tax project 2024 --format=json | pay-calc tax convert | pay-calc tax compare actual_1040.json

    \b
    Or with files:
      cat calculated_1040.json | pay-calc tax compare actual_1040.json
    """
    import sys
    from paycalc.sdk import compare_1040

    if sys.stdin.isatty():
        raise click.ClickException(
            "No calculated 1040 data provided. Pipe 1040 JSON to this command.\n"
            "Example: pay-calc tax project 2024 --format=json | pay-calc tax convert | pay-calc tax compare actual.json"
        )

    try:
        stdin_data = sys.stdin.read()
        calculated = json.loads(stdin_data)
    except json.JSONDecodeError as e:
        raise click.ClickException(f"Invalid JSON input from stdin: {e}")

    try:
        with open(actual_file) as f:
            actual = json.load(f)
    except json.JSONDecodeError as e:
        raise click.ClickException(f"Invalid JSON in {actual_file}: {e}")

    result = compare_1040(calculated, actual)
    click.echo(json.dumps(result, indent=2))


def _format_reconciliation_text(result: dict) -> str:
    """Format tax reconciliation as ASCII table for terminal display."""
    lines = []
    summary = result["summary"]
    comparisons = result["comparisons"]
    gaps = result["gaps"]
    year = result["projection"]["year"]

    # Header
    lines.append(f"TAX VALIDATION FOR {year}")
    lines.append("=" * 70)
    lines.append("")

    # Summary
    if summary.get("status") == "no_1040":
        lines.append(f"⚠ {summary.get('message', 'No Form 1040 found')}")
        lines.append("")
        lines.append("Run 'pay-calc tax projection YEAR' to see calculated values.")
        return "\n".join(lines)

    # Line-by-line comparison table
    lines.append(f"{'Line Item':<35} {'Calculated':>14} {'Actual':>14} {'Δ':>10} {'Status':<6}")
    lines.append("-" * 70)

    for comp in comparisons:
        calc = comp["calculated"]
        actual = comp["actual"]
        delta = comp["delta"]
        match = comp["match"]

        calc_str = f"${calc:,.2f}" if calc != 0 else "$0.00"
        actual_str = f"${actual:,.2f}" if actual != 0 else "$0.00"

        if match:
            delta_str = "—"
            status = "✓"
        else:
            delta_str = f"${delta:+,.0f}"
            status = "✗"

        lines.append(f"{comp['line']:<35} {calc_str:>14} {actual_str:>14} {delta_str:>10} {status:<6}")

    lines.append("-" * 70)
    lines.append("")

    # Gaps section
    if gaps:
        lines.append("ITEMS NOT TRACKED BY PAY-CALC")
        lines.append("-" * 40)
        for gap in gaps:
            amount = gap.get("amount", 0)
            sign = "-" if amount < 0 else "+"
            lines.append(f"  {gap['item']:<30} {sign}${abs(amount):>10,.2f}  (Line {gap['line']})")
        lines.append("")

    # Final summary
    lines.append("SUMMARY")
    lines.append("-" * 40)
    lines.append(f"  Calculated refund:    ${summary['calculated_refund']:>12,.2f}")
    lines.append(f"  Actual refund:        ${summary['actual_refund']:>12,.2f}")
    lines.append(f"  Gap:                  ${summary['gap']:>+12,.2f}")
    lines.append(f"  Gap % of taxable inc: {summary['gap_pct']:>12.4f}%")
    lines.append("")

    if summary["untracked_income"] != 0:
        lines.append(f"  Untracked income:     ${summary['untracked_income']:>+12,.2f}")
    if summary["untracked_credits"] > 0:
        lines.append(f"  Untracked credits:    ${summary['untracked_credits']:>12,.2f}")
    if summary["untracked_payments"] > 0:
        lines.append(f"  Untracked payments:   ${summary['untracked_payments']:>12,.2f}")

    return "\n".join(lines)


def _format_compare_text(result: dict) -> str:
    """Format compare_1040 result as ASCII table for terminal display."""
    lines = []
    summary = result["summary"]
    comparisons = result["comparisons"]
    year = result.get("year", "")

    # Header
    lines.append(f"TAX COMPARISON FOR {year}")
    lines.append("=" * 70)
    lines.append("")

    # Line-by-line comparison table
    lines.append(f"{'Line Item':<35} {'Calculated':>12} {'Actual':>12} {'Δ':>8} {'OK':<4}")
    lines.append("-" * 70)

    for comp in comparisons:
        calc = comp["calculated"]
        actual = comp["actual"]
        delta = comp["delta"]
        match = comp["match"]

        calc_str = f"${calc:,}" if calc != 0 else "$0"
        actual_str = f"${actual:,}" if actual != 0 else "$0"

        if match:
            delta_str = "—"
            status = "✓"
        else:
            delta_str = f"${delta:+,}"
            status = "✗"

        lines.append(f"{comp['line']:<35} {calc_str:>12} {actual_str:>12} {delta_str:>8} {status:<4}")

    lines.append("-" * 70)
    lines.append("")

    # Summary
    lines.append("SUMMARY")
    lines.append("-" * 40)
    matches = summary['total_comparisons'] - summary['mismatches']
    matching = summary.get('matching', {})
    match_count = matching.get('count', summary['total_comparisons'] - summary['mismatches'])
    match_total = matching.get('total', summary['total_comparisons'])
    lines.append(f"  Status:             {summary['status'].upper()} ({match_count:2}/{match_total:2})")

    # Render amounts from structured data
    amounts = summary.get('amounts', [])
    for amt in amounts:
        caption = amt['caption']
        value = amt['value']
        subtract = amt.get('subtract', False)
        sign = "-" if subtract else " "
        lines.append(f"  {caption + ':':<20}{sign}${value:>10,}")

    lines.append(f"  {'-' * 34}")

    # Render variance from structured data
    var_data = summary.get('variance', {})
    var_amount = var_data.get('amount', abs(summary.get('gap', 0)))
    favorable = var_data.get('favorable')
    if favorable is True:
        lines.append(f"  {'Variance (+):':<20} ${var_amount:>10,}")
    elif favorable is False:
        lines.append(f"  {'Variance (-):':<20}-${var_amount:>10,}")
    else:
        lines.append(f"  {'Variance:':<20} ${var_amount:>10,}")

    return "\n".join(lines)


@tax_group.command("validate")
@click.argument("year")
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text",
              help="Output format (default: text)")
@click.option("--data-dir", type=click.Path(exists=True),
              help="Directory containing data (default: XDG data dir)")
def tax_validate(year, output_format, data_dir):
    """Shortcut: generate projection, convert to 1040, compare with actual.

    Equivalent to:
      pay-calc tax project YEAR --format=json | pay-calc tax convert | pay-calc tax compare <1040_file>

    But looks up the actual Form 1040 from records automatically.

    \b
    Prerequisites:
    1. Import W-2s: pay-calc records import <w2_file>
    2. Import Form 1040: pay-calc records import <form_1040_file>

    \b
    Output shows:
    - Line-by-line comparison (calculated vs actual)
    - Summary with gap analysis
    """
    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    from paycalc.sdk import (
        generate_projection, projection_to_1040, compare_1040,
        load_form_1040, get_data_path
    )

    data_path = Path(data_dir) if data_dir else get_data_path()

    # Step 1: Load actual 1040 from records
    actual_1040 = load_form_1040(year, data_dir=data_path)
    if actual_1040 is None:
        raise click.ClickException(
            f"No Form 1040 found for {year}.\n"
            f"Import with: pay-calc records import <form_1040_file>"
        )

    try:
        # Step 2: Generate projection
        projection = generate_projection(year, data_dir=data_path)

        # Step 3: Convert to 1040 format
        calculated_1040 = projection_to_1040(projection)

        # Step 4: Compare
        result = compare_1040(calculated_1040, {"data": actual_1040})

        if output_format == "json":
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo(_format_compare_text(result))

    except ValueError as e:
        # Missing 1040 or W-2 records - exit 1 with clear message
        raise click.ClickException(str(e))
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


@cli.command("w2-generate")
@click.argument("year")
@click.option("--party", type=click.Choice(["him", "her"]), help="Party for validation (optional)")
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text", help="Output format (default: text)")
def w2_generate(year, party, output_format):
    """Convert a pay stub to W-2 format.

    Takes a stub (piped JSON) and converts it to W-2 box values.
    Does not care if the stub is real or projected.

    \b
    Input modes:
    1. Piped JSON: pay-calc projection 2025 him --format json | jq '.stub' | pay-calc w2-generate 2025
    2. Piped from file: cat stub.json | pay-calc w2-generate 2025

    \b
    Examples:
      # From projection:
      pay-calc projection 2025 him --format json | jq '.stub' | pay-calc w2-generate 2025

      # From a stub file:
      cat final_stub.json | pay-calc w2-generate 2025 --party him
    """
    from paycalc.sdk.w2 import stub_to_w2

    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    # Read stub from stdin
    if sys.stdin.isatty():
        raise click.ClickException(
            "No stub data provided. Pipe a stub JSON to this command.\n"
            "Example: pay-calc projection 2025 him --format json | jq '.stub' | pay-calc w2-generate 2025"
        )

    try:
        stub = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        raise click.ClickException(f"Invalid JSON from stdin: {e}")

    # Validate it looks like a stub
    if "pay_summary" not in stub:
        raise click.ClickException("Input JSON doesn't look like a stub (missing 'pay_summary')")

    try:
        result = stub_to_w2(stub, year, party=party)
    except ValueError as e:
        raise click.ClickException(str(e))

    if output_format == "json":
        click.echo(json.dumps(result, indent=2))
        return

    # Text format
    w2 = result["w2"]
    click.echo("=" * 50)
    click.echo(f"W-2 for {year} - {result['employer']}")
    click.echo("=" * 50)
    _print_w2_boxes(w2)

    validation = result.get("validation", {})
    warnings = validation.get("warnings", [])
    if warnings:
        click.echo()
        click.echo("Warnings:")
        for w in warnings:
            click.echo(f"  - {w}")


def _print_w2_boxes(w2_box: dict):
    """Print W-2 box values."""
    click.echo("W-2 Box Values:")
    click.echo(f"  Box 1 (Wages):           ${w2_box['wages']:>12,.2f}")
    click.echo(f"  Box 2 (Federal WH):      ${w2_box['federal_tax_withheld']:>12,.2f}")
    click.echo(f"  Box 3 (SS Wages):        ${w2_box['social_security_wages']:>12,.2f}")
    click.echo(f"  Box 4 (SS Tax):          ${w2_box['social_security_tax']:>12,.2f}")
    click.echo(f"  Box 5 (Medicare Wages):  ${w2_box['medicare_wages']:>12,.2f}")
    click.echo(f"  Box 6 (Medicare Tax):    ${w2_box['medicare_tax']:>12,.2f}")


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
@click.option("--employer", type=str, help="Filter to specific employer (finds latest stub for employer)")
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text", help="Output format (default: text)")
@click.option("--price", type=float, help="Stock price for RSU value projection")
def projection(year, party, employer, output_format, price):
    """Project year-end totals from partial year pay data.

    Reads pay stub data and projects year-end totals based on observed
    pay patterns (regular pay cadence, stock vesting schedule).

    \b
    Input modes:
    1. Piped JSON: echo '{"stubs": [...]}' | pay-calc projection 2025 him
    2. Employer lookup: pay-calc projection 2025 him --employer "Employer A"
    3. Default: Uses analysis file from 'pay-calc analysis YEAR PARTY'

    \b
    Output includes a 'stub' property containing the projected Y/E stub
    in standard stub format. Extract with: --format json | jq '.stub'

    Use this for mid-year tax planning when full W-2 data is not yet available.
    """
    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    from paycalc.sdk import get_data_path
    from paycalc.sdk.income_projection import generate_projection

    analysis_data = None
    stubs = []

    # Check for piped input - peek at stdin buffer to see if there's actual content
    has_stdin = False
    if not sys.stdin.isatty():
        # Try to peek at stdin buffer
        try:
            first_char = sys.stdin.buffer.peek(1)
            has_stdin = bool(first_char)
        except Exception:
            has_stdin = False

    if has_stdin:
        try:
            piped_data = json.load(sys.stdin)
            # Could be a single stub or analysis data with stubs array
            if "stubs" in piped_data:
                stubs = piped_data["stubs"]
                analysis_data = piped_data
            elif "pay_summary" in piped_data:
                # Single stub piped in
                stubs = [piped_data]
                analysis_data = {"stubs": stubs}
            else:
                raise click.ClickException("Piped JSON must have 'stubs' array or be a single stub with 'pay_summary'")
        except json.JSONDecodeError as e:
            raise click.ClickException(f"Invalid JSON from stdin: {e}")
    else:
        # No piped input - load from analysis file
        input_path = get_data_path() / f"{year}_{party}_pay_all.json"

        if not input_path.exists():
            click.echo(f"Error: Analysis file not found: {input_path}", err=True)
            click.echo("", err=True)
            click.echo("Run analysis first to generate the required input:", err=True)
            click.echo(f"    pay-calc analysis {year} {party}", err=True)
            click.echo("", err=True)
            click.echo("Or pipe in stub data:", err=True)
            click.echo(f"    cat stub.json | pay-calc projection {year} {party}", err=True)
            raise SystemExit(1)

        with open(input_path) as f:
            analysis_data = json.load(f)

        stubs = analysis_data.get("stubs", [])

    if not stubs:
        raise click.ClickException("No pay stub data found")

    # Filter by employer if specified
    if employer:
        employer_lower = employer.lower()
        stubs = [s for s in stubs if employer_lower in s.get("employer", "").lower()]
        if not stubs:
            raise click.ClickException(f"No stubs found for employer matching '{employer}'")

    # Generate projection using SDK (pass party to enable RSU SDK if configured)
    proj = generate_projection(stubs, year, party=party, stock_price=price)

    if output_format == "json":
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
    # Extract totals from stub property
    stub = proj.get("stub", {})
    stub_ytd = stub.get("pay_summary", {}).get("ytd", {})
    stub_taxes = stub.get("taxes", {})
    total = {
        "gross": stub_ytd.get("gross", 0),
        "taxes_withheld": (
            stub_taxes.get("federal_income_tax", {}).get("ytd_withheld", 0) +
            stub_taxes.get("social_security", {}).get("ytd_withheld", 0) +
            stub_taxes.get("medicare", {}).get("ytd_withheld", 0)
        ),
    }

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

    # Display config warnings (raise ignored, missing bonus, etc.)
    config_warnings = proj.get("config_warnings", [])
    if config_warnings:
        click.echo(f"\n  Configuration Warnings:")
        for w in config_warnings:
            click.echo(f"    - {w}")

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
    from paycalc.gemini_client import get_stock_quote

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
