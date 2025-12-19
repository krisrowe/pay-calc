"""RSU vesting schedule CLI commands."""

from datetime import date, datetime
from pathlib import Path

import click


@click.group("rsus")
def rsus():
    """RSU vesting schedule commands.

    View and manage RSU vesting data exported from Equity Awards Center.

    \b
    Usage:
    1. Export CSV from your brokerage equity awards portal
    2. Import with: pay-calc rsus import <path-to-csv>
    """
    pass


@rsus.command("import")
@click.argument("source", type=click.Path(exists=True))
def rsus_import(source):
    """Import an equity export CSV into the data/rsus/ folder.

    SOURCE is the path to the EquityAwardsCenter export CSV file.
    The filename must match: EquityAwardsCenter_EquityDetails_*.csv
    """
    from paycalc.sdk.rsus import import_export

    result = import_export(Path(source))

    if "error" in result:
        raise click.ClickException(result["error"])

    click.echo(f"Imported: {result['dest_path']}")
    click.echo(f"  Future vest dates: {result['future_vest_dates']}")
    click.echo(f"  Future shares: {result['future_shares']}")


@rsus.command("list")
def rsus_list():
    """List imported equity export files."""
    from paycalc.sdk.rsus import list_exports, get_rsus_path, find_latest_export

    exports = list_exports()

    if not exports:
        click.echo(f"No exports found in {get_rsus_path()}")
        click.echo("\nTo import an export:")
        click.echo("  pay-calc rsus import <path-to-csv>")
        return

    latest = find_latest_export()
    click.echo(f"Exports in {get_rsus_path()}:\n")
    for exp in exports:
        marker = " (active)" if latest and exp['filename'] == latest.name else ""
        click.echo(f"  {exp['filename']}{marker}")
        click.echo(f"    Modified: {exp['modified']}")

    if len(exports) > 1:
        click.echo()
        click.secho("Note: Multiple exports found. Using most recent by modification time.", fg="yellow")


@rsus.command("show")
@click.option("--start", type=str, help="Start date (YYYY-MM-DD). Defaults to Jan 1 of current year.")
@click.option("--end", type=str, help="End date (YYYY-MM-DD). Defaults to Dec 31 of current year.")
@click.option("--price", type=float, help="Stock price for value estimates.")
@click.option("--net", is_flag=True, help="Show net after estimated tax withholding (requires --price).")
@click.option("--annual", is_flag=True, help="Summarize by year with net and tax rate (requires --price).")
@click.option("--future-grant", type=float, help="Project annual future grants of $N value (requires --annual and --price).")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def rsus_show(start, end, price, net, annual, future_grant, as_json):
    """Show RSU vesting projection for a date range.

    Summarizes vesting by month with share counts and optional value estimates.

    \b
    Examples:
      pay-calc rsus show                        # Current year
      pay-calc rsus show --start 2025-01-01     # From Jan 2025
      pay-calc rsus show --price 303.29         # With value estimates
      pay-calc rsus show --price 303 --net      # With tax withholding
      pay-calc rsus show --price 303 --annual --start 2025-12-01 --end 2029-12-31
      pay-calc rsus show --price 180 --annual --future-grant 90000 --end 2029-12-31
    """
    import json as json_mod
    from paycalc.sdk.rsus import get_vesting_projection, count_exports

    # Parse dates
    start_date = None
    end_date = None

    if start:
        try:
            start_date = datetime.strptime(start, "%Y-%m-%d").date()
        except ValueError:
            raise click.BadParameter(f"Invalid date format '{start}'. Use YYYY-MM-DD.")

    if end:
        try:
            end_date = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError:
            raise click.BadParameter(f"Invalid date format '{end}'. Use YYYY-MM-DD.")

    if net and not price:
        raise click.UsageError("--net requires --price to calculate withholding")

    if annual and not price:
        raise click.UsageError("--annual requires --price to calculate values")

    if future_grant and not annual:
        raise click.UsageError("--future-grant requires --annual for year-by-year projections")

    if future_grant and not price:
        raise click.UsageError("--future-grant requires --price to convert dollar amount to shares")

    # Convert future grant dollar amount to shares
    future_grant_shares = None
    if future_grant and price:
        future_grant_shares = int(future_grant / price)

    result = get_vesting_projection(
        start_date=start_date,
        end_date=end_date,
        price=price,
        calculate_taxes=net,
        annual=annual,
        future_grant=future_grant_shares,
        future_grant_value=future_grant
    )

    if "error" in result:
        raise click.ClickException(result["error"])

    if as_json:
        # Remove formatted field for JSON output
        output = {k: v for k, v in result.items() if k != "formatted"}
        click.echo(json_mod.dumps(output, indent=2))
    else:
        click.echo(f"RSU Vesting: {result['start_date']} to {result['end_date']}")
        click.echo(f"Source: {result['source_file']}")
        if price:
            click.echo(f"Stock price: ${price:,.2f}")

        # Warn if multiple exports exist
        num_exports = count_exports()
        if num_exports > 1:
            click.secho(f"Note: {num_exports} exports found, using most recent. Run 'rsus list' to see all.", fg="yellow")

        click.echo()
        click.echo(result["formatted"])

        # Show tax details if calculated (but not for annual view - it's already included)
        if "taxes" in result and not annual:
            taxes = result["taxes"]
            click.echo()
            click.echo("Tax Withholding Breakdown:")
            click.echo(f"  Federal ({taxes['fed_rate']*100:.1f}%):  ${taxes['fed_tax']:>12,.2f}")
            click.echo(f"    Rate source: {taxes['fed_rate_source']}")
            if taxes['ss_capped']:
                click.echo(f"  Social Security:      ${taxes['ss_tax']:>12,.2f}  (capped - YTD wages exceed ${taxes['ss_wage_base']:,})")
            else:
                click.echo(f"  Social Security ({taxes['ss_rate']*100:.1f}%): ${taxes['ss_tax']:>12,.2f}")
            click.echo(f"  Medicare ({taxes['medicare_rate']*100:.2f}%):    ${taxes['medicare_tax']:>12,.2f}")
            click.echo(f"  ----------------------------------------")
            click.echo(f"  Total withheld:       ${taxes['total_tax']:>12,.2f}  ({taxes['effective_rate']*100:.1f}% effective)")
            click.echo(f"  Net proceeds:         ${taxes['net']:>12,.2f}")
