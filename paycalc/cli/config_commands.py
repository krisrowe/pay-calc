"""Config CLI commands for Pay Calc.

Thin pass-through to SDK for party-specific configurations with effective dates:
- suppl-rates: Supplemental withholding rates
- w4s: W-4 configurations
- comp: Compensation plans

All logic lives in paycalc.sdk.party_config - CLI only handles formatting.
"""

import json

import click


# =============================================================================
# CONFIG command group
# =============================================================================


@click.group()
def config():
    """Manage party-specific configurations.

    \b
    Subcommands:
      suppl-rates  Supplemental withholding rates (per party, with effective dates)
      w4s          W-4 configurations (per party, with effective dates)
      comp         Compensation plans (per party, with effective dates)

    \b
    All configurations support effective dates so historical changes are tracked.
    Use --format=json for machine-readable output.
    """
    pass


# =============================================================================
# SUPPL-RATES subgroup
# =============================================================================


@config.group("suppl-rates")
def suppl_rates():
    """Manage supplemental withholding rates.

    Supplemental income (bonuses, RSU vesting) is often withheld at a flat
    rate rather than W-4 rates. Default is 22% (IRS minimum), but employers
    may use higher rates.

    \b
    Examples:
      pay-calc config suppl-rates list him
      pay-calc config suppl-rates set him 0.30 --effective-date 2025-01-01
    """
    pass


@suppl_rates.command("list")
@click.argument("party", type=click.Choice(["him", "her"]))
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def suppl_rates_list(party, fmt):
    """List supplemental withholding rates for PARTY."""
    from paycalc.sdk.party_config import list_suppl_rates

    rates = list_suppl_rates(party)

    if fmt == "json":
        click.echo(json.dumps(rates, indent=2))
        return

    if not rates:
        click.echo(f"No supplemental rates configured for {party}.")
        click.echo()
        click.echo("Set a rate with:")
        click.echo(f"  pay-calc config suppl-rates set {party} 0.30 --effective-date 2025-01-01")
        return

    click.echo(f"Supplemental rates for {party}:")
    click.echo()
    click.echo(f"  {'Effective Date':<14}  {'Rate':>6}")
    click.echo(f"  {'-'*14}  {'-'*6}")
    for r in rates:
        click.echo(f"  {r['effective_date']:<14}  {r['rate']:>5.0%}")


@suppl_rates.command("set")
@click.argument("party", type=click.Choice(["him", "her"]))
@click.argument("rate", type=float)
@click.option("--effective-date", "-d", required=True, help="Date rate becomes effective (YYYY-MM-DD)")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def suppl_rates_set(party, rate, effective_date, fmt):
    """Set supplemental withholding rate for PARTY.

    RATE is a decimal (e.g., 0.30 for 30%).
    """
    from paycalc.sdk.party_config import set_suppl_rate

    try:
        result = set_suppl_rate(party, rate, effective_date)
    except ValueError as e:
        raise click.ClickException(str(e))

    if fmt == "json":
        click.echo(json.dumps(result, indent=2))
        return

    click.echo(f"{result['action'].title()} rate for {party}: {rate:.0%} effective {effective_date}")
    click.echo(f"Saved to: {result['path']}")


@suppl_rates.command("delete")
@click.argument("party", type=click.Choice(["him", "her"]))
@click.argument("effective_date")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def suppl_rates_delete(party, effective_date, fmt):
    """Delete a supplemental rate entry by effective date."""
    from paycalc.sdk.party_config import delete_suppl_rate

    result = delete_suppl_rate(party, effective_date)

    if fmt == "json":
        click.echo(json.dumps(result, indent=2))
        return

    if result["deleted"]:
        click.echo(f"Deleted rate for {party} effective {effective_date}")
    else:
        click.echo(f"No rate found for {party} effective {effective_date}")


# =============================================================================
# W4S subgroup
# =============================================================================


@config.group("w4s")
def w4s():
    """Manage W-4 configurations.

    W-4 settings affect regular pay withholding calculations.
    Track changes when filing status or allowances change.

    \b
    Examples:
      pay-calc config w4s list him
      pay-calc config w4s set him --filing-status married --effective-date 2025-01-01
    """
    pass


@w4s.command("list")
@click.argument("party", type=click.Choice(["him", "her"]))
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def w4s_list(party, fmt):
    """List W-4 configurations for PARTY."""
    from paycalc.sdk.party_config import list_w4s

    entries = list_w4s(party)

    if fmt == "json":
        click.echo(json.dumps(entries, indent=2))
        return

    if not entries:
        click.echo(f"No W-4 configurations for {party}.")
        click.echo()
        click.echo("Set a W-4 with:")
        click.echo(f"  pay-calc config w4s set {party} --filing-status married --effective-date 2025-01-01")
        return

    click.echo(f"W-4 configurations for {party}:")
    click.echo()
    for entry in entries:
        click.echo(f"  Effective: {entry.get('effective_date', '?')}")
        if "filing_status" in entry:
            click.echo(f"    Filing status: {entry['filing_status']}")
        if "allowances" in entry:
            click.echo(f"    Allowances: {entry['allowances']}")
        if "extra_withholding" in entry:
            click.echo(f"    Extra withholding: ${entry['extra_withholding']:,.2f}")
        if "multiple_jobs" in entry:
            click.echo(f"    Multiple jobs: {entry['multiple_jobs']}")
        if "dependents" in entry:
            click.echo(f"    Dependents (Step 3): ${entry['dependents']:,.0f}")
        if "other_income" in entry:
            click.echo(f"    Other income (Step 4a): ${entry['other_income']:,.2f}")
        if "deductions" in entry:
            click.echo(f"    Deductions (Step 4b): ${entry['deductions']:,.2f}")
        if "note" in entry:
            click.echo(f"    Note: {entry['note']}")
        click.echo()


@w4s.command("set")
@click.argument("party", type=click.Choice(["him", "her"]))
@click.option("--effective-date", "-d", required=True, help="Date W-4 becomes effective (YYYY-MM-DD)")
@click.option("--filing-status", "-s", type=click.Choice(["single", "married", "head_of_household"]))
@click.option("--allowances", "-a", type=int, help="Number of allowances (pre-2020 W-4)")
@click.option("--extra-withholding", "-e", type=float, help="Additional withholding per period (Step 4c)")
@click.option("--multiple-jobs/--no-multiple-jobs", default=None, help="Multiple jobs checkbox (Step 2c)")
@click.option("--dependents", type=float, help="Annual dependent tax credit (Step 3)")
@click.option("--other-income", type=float, help="Other annual income not from jobs (Step 4a)")
@click.option("--deductions", type=float, help="Deductions exceeding standard deduction (Step 4b)")
@click.option("--note", "-n", type=str, help="Note about source of this W-4 config (e.g., 'derived from stub abc123')")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def w4s_set(party, effective_date, filing_status, allowances, extra_withholding, multiple_jobs,
            dependents, other_income, deductions, note, fmt):
    """Set W-4 configuration for PARTY.

    \b
    Examples:
      # Basic setup with filing status
      pay-calc config w4s set him -d 2025-01-01 -s married

      # With dependent credits (Step 3) - e.g., $5000 for 2 children
      pay-calc config w4s set him -d 2025-01-01 -s married --dependents 5000

      # Derive and import W-4 from latest regular stub
      STUB_ID=$(pay-calc records list 2025 him --type stub --regular \\
        --format json | jq -r '.[-1].id')
      DERIVED=$(pay-calc config w4s derive $STUB_ID --format json)
      pay-calc config w4s set him -d 2025-01-01 \\
        --dependents $(echo $DERIVED | jq '.derived.step3_dependents') \\
        --note "Derived from stub $STUB_ID"
    """
    from paycalc.sdk.party_config import set_w4

    try:
        result = set_w4(
            party,
            effective_date,
            filing_status=filing_status,
            allowances=allowances,
            extra_withholding=extra_withholding,
            multiple_jobs=multiple_jobs,
            dependents=dependents,
            other_income=other_income,
            deductions=deductions,
            note=note,
        )
    except ValueError as e:
        raise click.ClickException(str(e))

    if fmt == "json":
        click.echo(json.dumps(result, indent=2))
        return

    click.echo(f"{result['action'].title()} W-4 for {party} effective {effective_date}")
    click.echo(f"Saved to: {result['path']}")


@w4s.command("derive")
@click.argument("record_id")
@click.option("--max-dependents", "-m", type=int, default=8,
              help="Max dependents to try when matching (default: 8)")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def w4s_derive(record_id, max_dependents, fmt):
    """Derive W-4 settings from a pay stub's actual FIT withholding.

    Systematically searches all W-4 configurations:
    1. Tries all combinations: filing status x step2 checkbox x dependents (0-max)
    2. If no exact match, uses extra withholding to force match

    \b
    Examples:
      # Derive W-4 from a specific stub
      pay-calc config w4s derive f7e3eaf9

      # Limit search to max 3 dependents
      pay-calc config w4s derive f7e3eaf9 --max-dependents 3

      # Output as JSON for piping
      pay-calc config w4s derive f7e3eaf9 --format=json
    """
    from paycalc.sdk.party_config import derive_w4_from_stub
    from rich.console import Console
    from rich.table import Table

    try:
        result = derive_w4_from_stub(record_id, max_dependents=max_dependents)
    except ValueError as e:
        raise click.ClickException(str(e))

    if fmt == "json":
        click.echo(json.dumps(result, indent=2))
        return

    console = Console()

    # Header info
    console.print(f"\n[bold]W-4 Derivation for {result['party']}[/bold]")
    console.print(f"Source stub: {result['record_id']} ({result['pay_date']})")

    # Stub values
    console.print(f"\n[cyan]Observed:[/cyan] FIT taxable ${result['fit_taxable']:,.2f}, "
                  f"withheld ${result['fit_withheld']:,.2f} ({result['effective_rate_pct']:.2f}%)")

    # All matches table
    all_matches = result.get("all_matches", [])
    analysis = result["analysis"]

    if all_matches:
        console.print(f"\n[green]Matching W-4 Configurations[/green] ({len(all_matches)} found):")
        match_table = Table(box=None)
        match_table.add_column("", style="dim", width=3)
        match_table.add_column("Configuration", style="cyan")
        match_table.add_column("Credits", justify="right")
        match_table.add_column("Extra WH", justify="right")
        match_table.add_column("Expected", justify="right")
        match_table.add_column("Diff", justify="right")

        for i, m in enumerate(all_matches):
            marker = "[green]>[/green]" if i == 0 else ""
            extra_str = f"${m['step4c_extra_withholding']}" if m['step4c_extra_withholding'] else "-"
            match_table.add_row(
                marker,
                m["description"],
                f"${m['step3_credits']:,}",
                extra_str,
                f"${m['expected_withholding']:,.2f}",
                f"${m['diff']:.2f}",
            )

        console.print(match_table)
    else:
        console.print(f"\n[red]No matching W-4 configuration found[/red]")

    # Best match summary
    derived = result["derived"]
    category = derived.get("match_category", "unknown")
    if category == "standard":
        cat_style = "[green]standard W-4[/green]"
    elif category == "custom_credits":
        cat_style = "[yellow]custom credits[/yellow]"
    elif category == "extra_withholding":
        cat_style = "[yellow]extra withholding[/yellow]"
    else:
        cat_style = "[red]no match[/red]"

    console.print(f"\n[bold]Best Match:[/bold] {derived['description']} ({cat_style})")
    console.print(f"  Step 3 Credits: ${derived['step3_credits']:,}")
    if derived["step4c_extra_withholding"]:
        console.print(f"  Step 4c Extra: ${derived['step4c_extra_withholding']}/period")

    console.print(f"\n[dim]Searched {analysis['total_combinations']} combinations ({analysis['configs_tried']} configs x {analysis['credit_increments_tried']} credit amounts)[/dim]")


# =============================================================================
# COMP subgroup
# =============================================================================


@config.group("comp")
def comp():
    """Manage compensation plans.

    Track gross pay per period and employer changes over time.
    Used by YTD modeling to project pay periods.

    \b
    Examples:
      pay-calc config comp list him
      pay-calc config comp set him --gross-per-period 5000.00 --effective-date 2025-01-03
    """
    pass


@comp.command("list")
@click.argument("party", type=click.Choice(["him", "her"]))
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def comp_list(party, fmt):
    """List compensation plans for PARTY."""
    from paycalc.sdk.party_config import list_comp

    entries = list_comp(party)

    if fmt == "json":
        click.echo(json.dumps(entries, indent=2))
        return

    if not entries:
        click.echo(f"No compensation plans for {party}.")
        click.echo()
        click.echo("Set a comp plan with:")
        click.echo(f"  pay-calc config comp set {party} --gross-per-period 5000.00 --effective-date 2025-01-03")
        return

    click.echo(f"Compensation plans for {party}:")
    click.echo()
    click.echo(f"  {'Effective':<12}  {'Gross/Period':>14}  {'Regular Pay':>14}  {'Employer'}")
    click.echo(f"  {'-'*12}  {'-'*14}  {'-'*14}  {'-'*20}")
    for entry in entries:
        gross = entry.get("gross_per_period", 0)
        regular = entry.get("regular_pay", 0)
        employer = entry.get("employer", "")[:20]
        click.echo(f"  {entry.get('effective_date', '?'):<12}  ${gross:>13,.2f}  ${regular:>13,.2f}  {employer}")


@comp.command("set")
@click.argument("party", type=click.Choice(["him", "her"]))
@click.option("--effective-date", "-d", required=True, help="Date comp plan becomes effective (YYYY-MM-DD)")
@click.option("--gross-per-period", "-g", type=float, help="Total gross per pay period")
@click.option("--regular-pay", "-r", type=float, help="Regular pay component per period")
@click.option("--employer", "-e", type=str, help="Employer name")
@click.option("--source-record", type=str, help="Record ID this was derived from")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def comp_set(party, effective_date, gross_per_period, regular_pay, employer, source_record, fmt):
    """Set compensation plan for PARTY."""
    from paycalc.sdk.party_config import set_comp

    try:
        result = set_comp(
            party,
            effective_date,
            gross_per_period=gross_per_period,
            regular_pay=regular_pay,
            employer=employer,
            source_record=source_record,
        )
    except ValueError as e:
        raise click.ClickException(str(e))

    if fmt == "json":
        click.echo(json.dumps(result, indent=2))
        return

    click.echo(f"{result['action'].title()} comp plan for {party} effective {effective_date}")
    click.echo(f"Saved to: {result['path']}")
