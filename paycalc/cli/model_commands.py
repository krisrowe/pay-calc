"""Model command group for pay stub modeling and validation."""

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click
from rich.console import Console
from rich.table import Table

from paycalc.sdk.records import get_record, list_records
from paycalc.sdk.stub_model import (
    model_stub,
    model_stubs_in_sequence,
    model_regular_401k_contribs,
    max_regular_401k_contribs,
    get_first_regular_pay_date,
)
from paycalc.sdk.config import normalize_deduction_type
from paycalc.sdk.comp import identify_salary_changes
from paycalc.sdk.modeling import (
    validate_stub,
    is_supplemental_stub,
    extract_inputs_from_stub,
    extract_actuals_from_stub,
)


def _is_supplemental_stub_UNUSED(data: Dict[str, Any]) -> bool:
    """Determine if a stub is supplemental pay (bonus, RSU, etc.) vs regular.

    A stub is considered supplemental if it has non-zero current amounts for
    earnings types like Bonus, RSU, Spot Bonus, Peer Bonus, etc.
    """
    SUPPLEMENTAL_KEYWORDS = (
        "bonus", "rsu", "stock", "gsu", "peer", "spot", "supplemental",
        "award", "equity", "vesting", "grant",
    )

    earnings = data.get("earnings", [])
    if isinstance(earnings, dict):
        earnings = [{"type": k, **v} if isinstance(v, dict) else {"type": k, "current_amount": v}
                    for k, v in earnings.items()]

    for earn in earnings:
        raw_type = (earn.get("type") or earn.get("name") or "").lower()
        amount = earn.get("current_amount") or earn.get("amount") or earn.get("current") or 0

        if amount > 0:
            for keyword in SUPPLEMENTAL_KEYWORDS:
                if keyword in raw_type:
                    return True

    return False


def build_history_from_prior_stubs(
    party: str,
    year: str,
    target_pay_date: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Query prior stubs and build supplementals + special_deductions + comp_plan_history.

    Args:
        party: Party identifier
        year: Tax year
        target_pay_date: Target pay date (YYYY-MM-DD) - excludes this and later

    Returns:
        Tuple of (supplementals, special_deductions, comp_plan_history) lists
    """
    all_stubs = list_records(year=year, party=party, type_filter="stub")

    supplementals = []
    special_deductions = []

    for record in all_stubs:
        data = record.get("data", {})
        stub_date = data.get("pay_date", "")

        if not stub_date or stub_date >= target_pay_date:
            continue

        inputs = extract_inputs_from_stub(data)

        if is_supplemental_stub(data):
            supp_entry = {
                "date": stub_date,
                "gross": inputs["gross"],
            }
            if inputs["pretax_401k"] > 0:
                supp_entry["401k"] = inputs["pretax_401k"]
            supplementals.append(supp_entry)
        else:
            if inputs["pretax_401k"] > 0:
                special_deductions.append({
                    "date": stub_date,
                    "401k": inputs["pretax_401k"],
                })

    supplementals.sort(key=lambda x: x["date"])
    special_deductions.sort(key=lambda x: x["date"])

    comp_plan_history = identify_salary_changes(
        f"{year}-01-01",
        f"{year}-12-31",
        party=party,
    )

    return supplementals, special_deductions, comp_plan_history


def extract_inputs_from_stub(data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract model inputs from a pay stub's data."""
    pay_summary = data.get("pay_summary", {})
    current = pay_summary.get("current", {})

    gross = current.get("gross", 0)
    pay_frequency = "biweekly"

    deductions = data.get("deductions", [])
    pretax_401k = 0
    benefits = {}
    imputed_income = 0

    # Extract imputed income from earnings (e.g., Group Term Life)
    # Imputed income is added to gross but offset in deductions - it's not real cash
    earnings = data.get("earnings", [])
    for earning in earnings:
        raw_type = (earning.get("type") or earning.get("name") or "").lower()
        amount = earning.get("current_amount") or earning.get("amount") or 0
        # Group Term Life over $50k is imputed income (taxable but not cash)
        if "group term life" in raw_type or "gtl" in raw_type:
            imputed_income += amount

    if isinstance(deductions, dict):
        deductions = [
            {"type": k, **v} if isinstance(v, dict) else {"type": k, "current_amount": v}
            for k, v in deductions.items()
        ]

    for ded in deductions:
        raw_type = ded.get("type") or ded.get("name") or ""
        amount = ded.get("current_amount") or ded.get("amount") or ded.get("current") or 0

        if amount <= 0:
            continue

        canonical = normalize_deduction_type(raw_type)

        if canonical == "401k":
            pretax_401k = amount
        elif canonical == "health":
            benefits["pretax_health"] = amount
        elif canonical == "dental":
            benefits["pretax_dental"] = amount
        elif canonical == "vision":
            benefits["pretax_vision"] = amount
        elif canonical == "fsa":
            benefits["pretax_fsa"] = amount
        elif canonical == "hsa":
            benefits["pretax_hsa"] = amount
        # Note: life insurance deductions (GTL offset, Vol Life) are NOT pretax
        # GTL is imputed income (handled separately), Vol Life is post-tax
        elif canonical == "disability":
            benefits["pretax_disability"] = amount

    return {
        "gross": gross,
        "pay_frequency": pay_frequency,
        "pretax_401k": pretax_401k,
        "benefits": benefits,
        "imputed_income": imputed_income,
    }


def extract_actuals_from_stub(data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract actual values from a pay stub for comparison."""
    pay_summary = data.get("pay_summary", {})
    current_pay = pay_summary.get("current", {})
    ytd_pay = pay_summary.get("ytd", {})
    taxes = data.get("taxes", {})

    fit = taxes.get("federal_income_tax", {})
    ss = taxes.get("social_security", {})
    med = taxes.get("medicare", {})

    pretax_total = 0
    deductions = data.get("deductions", [])
    if isinstance(deductions, dict):
        deductions = [
            {"type": k, **v} if isinstance(v, dict) else {"type": k, "current_amount": v}
            for k, v in deductions.items()
        ]
    for ded in deductions:
        amount = ded.get("current_amount") or ded.get("amount") or ded.get("current") or 0
        pretax_total += amount

    fit_taxable = ytd_pay.get("fit_taxable_wages", 0)
    # Prefer stub's fit_taxable_wages if available; else calculate from gross - deductions
    current_fit_taxable = current_pay.get("fit_taxable_wages") or (current_pay.get("gross", 0) - pretax_total)

    return {
        "current": {
            "gross": current_pay.get("gross", 0),
            "fit_taxable": current_fit_taxable,
            "fit_withheld": fit.get("current_withheld", 0),
            "ss_withheld": ss.get("current_withheld", 0),
            "medicare_withheld": med.get("current_withheld", 0),
            "net_pay": data.get("net_pay", 0),
        },
        "ytd": {
            "gross": ytd_pay.get("gross", 0),
            "fit_taxable": fit_taxable,
            "fit_withheld": fit.get("ytd_withheld", 0),
            # Note: ss_wages/medicare_wages excluded - stubs don't reliably store YTD FICA wages
            "ss_withheld": ss.get("ytd_withheld", 0),
            "medicare_withheld": med.get("ytd_withheld", 0),
        },
    }


def compare_values(
    modeled: Dict[str, Any],
    actual: Dict[str, Any],
    prefix: str = "",
    diffs_only: bool = True,
) -> List[Tuple[str, float, float, float]]:
    """Compare modeled vs actual values.

    Args:
        modeled: Modeled values dict
        actual: Actual values dict
        prefix: Field name prefix for nested dicts
        diffs_only: If True, only return mismatches. If False, return all comparisons.

    Returns:
        List of (field_name, modeled, actual, diff) tuples
    """
    results = []

    for key, actual_val in actual.items():
        if isinstance(actual_val, dict):
            modeled_sub = modeled.get(key, {})
            sub_prefix = f"{prefix}{key}." if prefix else f"{key}."
            results.extend(compare_values(modeled_sub, actual_val, sub_prefix, diffs_only))
        else:
            modeled_val = modeled.get(key, 0)
            diff = round(modeled_val - actual_val, 2)
            field_name = f"{prefix}{key}" if prefix else key
            if not diffs_only or abs(diff) >= 0.01:
                results.append((field_name, modeled_val, actual_val, diff))

    return results


def print_comparison_table(
    comparisons: List[Tuple[str, float, float, float]],
    diffs_only: bool = False,
) -> None:
    """Print a comparison table using rich.

    Args:
        comparisons: List of (field, modeled, actual, diff) tuples
        diffs_only: If True, only diffs were included (affects header message)
    """
    console = Console()

    # Check if any diffs exist
    has_diffs = any(abs(diff) >= 0.01 for _, _, _, diff in comparisons)

    if not comparisons:
        console.print("\n✓ All values match!", style="green")
        return

    if diffs_only:
        if has_diffs:
            console.print("\nDiscrepancies found:", style="bold red")
        else:
            console.print("\n✓ All values match!", style="green")
            return
    else:
        if has_diffs:
            console.print("\nComparison (discrepancies highlighted):", style="bold")
        else:
            console.print("\n✓ All values match!", style="bold green")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Field", style="cyan")
    table.add_column("Modeled", justify="right")
    table.add_column("Actual", justify="right")
    table.add_column("Diff", justify="right")
    table.add_column("% Diff", justify="right")

    for field, modeled, actual, diff in comparisons:
        is_match = abs(diff) < 0.01

        if is_match:
            diff_str = "✓"
            pct_str = ""
            row_style = "dim"
        else:
            diff_str = f"+{diff:,.2f}" if diff > 0 else f"{diff:,.2f}"
            if actual != 0:
                pct = (diff / actual) * 100
                pct_str = f"+{pct:.1f}%" if pct > 0 else f"{pct:.1f}%"
            else:
                pct_str = "N/A"

            # Color code based on magnitude of % diff
            if abs(diff) < 0.01:
                row_style = "green"
            elif actual != 0 and abs((diff / actual) * 100) < 2:
                row_style = "yellow"
            else:
                row_style = "red"

        table.add_row(
            field,
            f"{modeled:,.2f}",
            f"{actual:,.2f}",
            diff_str,
            pct_str,
            style=row_style,
        )

    console.print(table)


@click.group()
def model():
    """Pay stub modeling commands.

    Tools for modeling pay stubs and validating model accuracy
    against real stub records.
    """
    pass


@model.command("validate")
@click.argument("regular_pay_stub_record_id", metavar="<regular-pay-stub-record-id>")
@click.option("--iterative", "-i", is_flag=True,
              help="Use iterative model (model_stubs_in_sequence) for accurate YTD")
@click.option("--reference-date", "-r", type=str,
              help="Reference pay date to anchor schedule (YYYY-MM-DD)")
@click.option("--supplementals", "-s", type=click.Path(exists=True),
              help="JSON file with supplemental pay events [{date, gross, 401k?}, ...]")
@click.option("--special-deductions", type=click.Path(exists=True),
              help="JSON file with per-date 401k overrides [{date, 401k}, ...]")
@click.option("--no-auto-history", is_flag=True,
              help="Disable auto-building history from prior stubs")
@click.option("--diffs-only", is_flag=True,
              help="Only show discrepancies (default shows full comparison)")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def validate(
    regular_pay_stub_record_id: str,
    iterative: bool,
    reference_date: Optional[str],
    supplementals: Optional[str],
    special_deductions: Optional[str],
    no_auto_history: bool,
    diffs_only: bool,
    verbose: bool,
    output_json: bool,
):
    """Validate pay stub model against a real stub from records.

    Loads an actual pay stub record and attempts to recreate it using
    the model. Compares current and YTD values to identify discrepancies
    in modeling logic.

    \b
    The command will:
    1. Load the specified stub record
    2. Extract gross, 401k, and benefits from the stub (as inputs)
    3. Call the model with those inputs
    4. Compare modeled outputs to actual stub values
    5. Report any discrepancies

    \b
    With --iterative, uses model_stubs_in_sequence() which processes each
    pay period sequentially for accurate YTD calculations. This mode
    auto-builds history from prior stubs unless --no-auto-history.

    \b
    Examples:
      pay-calc model validate 5e868d28
      pay-calc model validate 5e868d28 -i -v
      pay-calc model validate 5e868d28 -i --no-auto-history
    """
    record_id = regular_pay_stub_record_id

    # Load supplementals and special_deductions if provided
    supp_data = None
    special_ded_data = None
    if supplementals:
        with open(supplementals) as f:
            supp_data = json.load(f)
    if special_deductions:
        with open(special_deductions) as f:
            special_ded_data = json.load(f)

    # Load the record
    record = get_record(record_id)
    if not record:
        raise click.ClickException(f"Record '{record_id}' not found")

    meta = record.get("meta", {})
    if meta.get("type") != "stub":
        raise click.ClickException(f"Record '{record_id}' is not a stub (type={meta.get('type')})")

    data = record.get("data", {})
    pay_date = data.get("pay_date", "unknown")
    party = meta.get("party", "unknown")

    # Check if this is a supplemental stub (not regular pay)
    if is_supplemental_stub(data):
        gross = data.get("pay_summary", {}).get("current", {}).get("gross", 0)
        raise click.ClickException(
            f"Record '{record_id}' is a supplemental stub (bonus/RSU), not regular pay.\n"
            f"  Pay date: {pay_date}\n"
            f"  Gross: ${gross:,.2f}\n"
            f"Use a regular pay stub record ID for validation."
        )

    if verbose:
        click.echo(f"Record: {record_id}")
        click.echo(f"Party: {party}")
        click.echo(f"Pay date: {pay_date}")
        click.echo()

    # Extract inputs from stub
    inputs = extract_inputs_from_stub(data)

    if verbose:
        click.echo("Extracted inputs:")
        click.echo(f"  Gross: ${inputs['gross']:,.2f}")
        click.echo(f"  Pay frequency: {inputs['pay_frequency']}")
        click.echo(f"  401k: ${inputs['pretax_401k']:,.2f}")
        click.echo(f"  Benefits: {inputs['benefits']}")
        if inputs.get('imputed_income'):
            click.echo(f"  Imputed income (GTL): ${inputs['imputed_income']:,.2f}")
        click.echo()

    # Build overrides for model
    comp_plan_override = {
        "gross_per_period": inputs["gross"],
        "pay_frequency": inputs["pay_frequency"],
    }

    benefits_override = inputs["benefits"] if inputs["benefits"] else None
    imputed_income = inputs.get("imputed_income", 0)

    # Call model (iterative or single-period)
    if iterative:
        # Auto-build history from prior stubs if not disabled and not provided
        auto_supplementals = None
        auto_special_deductions = None
        comp_plan_history = None
        if not no_auto_history and supp_data is None and special_ded_data is None:
            year = pay_date[:4]
            auto_supplementals, auto_special_deductions, comp_plan_history = build_history_from_prior_stubs(
                party, year, pay_date
            )
            # Write to temp files for debugging/inspection
            tmp_dir = tempfile.mkdtemp(prefix="pay-calc-validate-")
            if auto_supplementals:
                supp_file = Path(tmp_dir) / "supplementals.json"
                with open(supp_file, "w") as f:
                    json.dump(auto_supplementals, f, indent=2)
                supp_data = auto_supplementals
                if verbose:
                    click.echo(f"  Auto-built supplementals: {supp_file}")
            if auto_special_deductions:
                sd_file = Path(tmp_dir) / "special_deductions.json"
                with open(sd_file, "w") as f:
                    json.dump(auto_special_deductions, f, indent=2)
                special_ded_data = auto_special_deductions
                if verbose:
                    click.echo(f"  Auto-built special_deductions: {sd_file}")
            if comp_plan_history:
                cph_file = Path(tmp_dir) / "comp_plan_history.json"
                with open(cph_file, "w") as f:
                    json.dump(comp_plan_history, f, indent=2)
                if verbose:
                    click.echo(f"  Auto-built comp_plan_history: {cph_file}")

        # Fail if no comp plan history and no prior stubs allowed
        if not comp_plan_history and not no_auto_history:
            raise click.ClickException(
                "No comp plan history could be derived from prior stubs.\n"
                "Either provide --no-auto-history with explicit overrides, or ensure stubs exist."
            )

        if verbose:
            click.echo("Using iterative model (model_stubs_in_sequence)...")
            if supp_data:
                click.echo(f"  Supplementals: {len(supp_data)} events")
            if special_ded_data:
                click.echo(f"  Special deductions: {len(special_ded_data)} dates")
            if comp_plan_history:
                click.echo(f"  Comp plan history: {len(comp_plan_history)} entries")

        # Extract year from pay_date for full-year modeling
        year_int = int(pay_date[:4])

        result = model_stubs_in_sequence(
            year_int,
            party,
            comp_plan_override=comp_plan_override,
            comp_plan_history=comp_plan_history,
            benefits_override=benefits_override,
            pretax_401k=inputs["pretax_401k"],
            supplementals=supp_data,
            special_deductions=special_ded_data,
        )

        # Find the stub matching the target pay_date
        if result.get("stubs"):
            for stub in result["stubs"]:
                if stub.get("date") == pay_date:
                    result["current"] = stub
                    break
            else:
                result["current"] = result["stubs"][-1]  # Fallback to last stub
        else:
            result["current"] = {}
        model_name = "model_stubs_in_sequence"
    else:
        result = model_stub(
            pay_date,
            party,
            comp_plan_override=comp_plan_override,
            benefits_override=benefits_override,
            pretax_401k=inputs["pretax_401k"],
            imputed_income=imputed_income,
        )
        model_name = "model_stub"

    if "error" in result:
        raise click.ClickException(f"Error from {model_name}: {result['error']}")

    if verbose and iterative:
        click.echo(f"  Periods modeled: {result.get('periods_modeled', 'N/A')}")
        if result.get('supplementals_included'):
            click.echo(f"  Supplementals included: {result.get('supplementals_included')}")

    # Extract actuals from stub
    actuals = extract_actuals_from_stub(data)

    # Build modeled values in same structure
    # Note: ss_wages/medicare_wages excluded from YTD comparison since stubs
    # don't reliably store YTD FICA wages (only current taxable_wages and ytd_withheld)
    modeled = {
        "current": {
            "gross": result["current"]["gross"],
            "fit_taxable": result["current"]["fit_taxable"],
            "fit_withheld": result["current"]["fit_withheld"],
            "ss_withheld": result["current"]["ss_withheld"],
            "medicare_withheld": result["current"]["medicare_withheld"],
            "net_pay": result["current"]["net_pay"],
        },
        "ytd": {
            "gross": result["ytd"]["gross"],
            "fit_taxable": result["ytd"]["fit_taxable"],
            "fit_withheld": result["ytd"]["fit_withheld"],
            "ss_withheld": result["ytd"]["ss_withheld"],
            "medicare_withheld": result["ytd"]["medicare_withheld"],
        },
    }

    # Compare
    comparisons = compare_values(modeled, actuals, diffs_only=diffs_only)

    if output_json:
        output = {
            "record_id": record_id,
            "party": party,
            "pay_date": pay_date,
            "model": model_name,
            "periods_modeled": result.get("periods_modeled"),
            "inputs": inputs,
            "modeled": modeled,
            "actual": actuals,
            "discrepancies": [
                {"field": f, "modeled": m, "actual": a, "diff": d}
                for f, m, a, d in comparisons
                if abs(d) >= 0.01  # Only include actual discrepancies in JSON
            ],
            "match": not any(abs(d) >= 0.01 for _, _, _, d in comparisons),
        }
        click.echo(json.dumps(output, indent=2))
    else:
        mode_info = f", {result.get('periods_modeled')} periods" if iterative else ""
        click.echo(f"Validating stub {record_id} ({party}, {pay_date}{mode_info})")
        print_comparison_table(comparisons, diffs_only=diffs_only)

        # Check for actual discrepancies (not just comparison rows)
        has_diffs = any(abs(d) >= 0.01 for _, _, _, d in comparisons)
        if has_diffs:
            if iterative:
                click.echo("\nPossible causes for discrepancies:")
                click.echo("  - Gaps in prior stub history (missing imported stubs)")
                click.echo("  - Target stub is partial period or has payroll adjustments")
                if not no_auto_history:
                    click.echo("  - Comp plan history may not reflect actual gross for this period")
                    click.echo("    (derived from stubs; check if target stub has unusual gross)")
            raise SystemExit(1)


@model.command("stub")
@click.argument("pay_date", metavar="<pay-date>")
@click.argument("party", type=click.Choice(["him", "her"]))
@click.option("--gross", "-g", type=float, default=None,
              help="Override gross pay per period")
@click.option("--supplementals", "-s", type=click.Path(exists=True),
              help="JSON file with supplemental pay events [{date, gross, 401k?}, ...]")
@click.option("--special-deductions", type=click.Path(exists=True),
              help="JSON file with per-date 401k overrides [{date, 401k}, ...]")
@click.option("--no-auto-history", is_flag=True,
              help="Disable auto-building history from prior stubs")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def stub_cmd(
    pay_date: str,
    party: str,
    gross: Optional[float],
    supplementals: Optional[str],
    special_deductions: Optional[str],
    no_auto_history: bool,
    output_json: bool,
):
    """Model a pay stub for a given date (iterative, YTD-aware).

    Models all pay periods from start of year to target date to ensure
    accurate YTD values (SS wage cap, 401k limits, supplementals).

    Auto-builds history from prior imported stubs unless --no-auto-history.

    \b
    Examples:
      pay-calc model stub 2026-01-02 him
      pay-calc model stub 2026-01-02 him --special-deductions overrides.json
      pay-calc model stub 2026-01-02 him --json
    """
    year = pay_date[:4]
    comp_override = None
    if gross is not None:
        comp_override = {"gross_per_period": gross, "pay_frequency": "biweekly"}

    # Load supplementals and special_deductions from files if provided
    supp_data = None
    special_ded_data = None
    if supplementals:
        with open(supplementals) as f:
            supp_data = json.load(f)
    if special_deductions:
        with open(special_deductions) as f:
            special_ded_data = json.load(f)

    # Auto-build history from prior stubs if not disabled and not provided
    comp_plan_history = None
    if not no_auto_history and supp_data is None and special_ded_data is None:
        auto_supp, auto_special, comp_plan_history = build_history_from_prior_stubs(
            party, year, pay_date
        )
        if supp_data is None:
            supp_data = auto_supp
        if special_ded_data is None:
            special_ded_data = auto_special

    # Extract year from pay_date for full-year modeling
    from datetime import datetime
    pay_date_obj = datetime.strptime(pay_date, "%Y-%m-%d")

    result = model_stubs_in_sequence(
        pay_date_obj.year,
        party,
        comp_plan_override=comp_override,
        comp_plan_history=comp_plan_history,
        supplementals=supp_data,
        special_deductions=special_ded_data,
    )

    # Find the stub matching the target pay_date
    if result.get("stubs"):
        matching_stub = None
        for stub in result["stubs"]:
            if stub.get("date") == pay_date:
                matching_stub = stub
                break
        if matching_stub:
            result["stubs"] = [matching_stub]

    if "error" in result:
        raise click.ClickException(f"Error: {result['error']}")

    if output_json:
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        console = Console()
        cur = result["stubs"][-1] if result.get("stubs") else {}
        ytd = result["ytd"]

        console.print(f"\n[bold]Modeled Stub: {pay_date} ({party})[/bold]")
        console.print(f"Period #{result.get('periods_modeled', '?')}")

        table = Table(show_header=True, header_style="bold")
        table.add_column("Field")
        table.add_column("Current", justify="right")
        table.add_column("YTD", justify="right")

        table.add_row("Gross", f"${cur.get('gross', 0):,.2f}", f"${ytd.get('gross', 0):,.2f}")
        table.add_row("401k Pretax", f"${cur.get('pretax_401k', 0):,.2f}", f"${ytd.get('pretax_401k', 0):,.2f}")
        table.add_row("FIT Taxable", f"${cur.get('fit_taxable', 0):,.2f}", f"${ytd.get('fit_taxable', 0):,.2f}")
        table.add_row("FIT Withheld", f"${cur.get('fit_withheld', 0):,.2f}", f"${ytd.get('fit_withheld', 0):,.2f}")
        # Calculate effective FIT rate
        cur_fit_rate = (cur.get('fit_withheld', 0) / cur.get('fit_taxable', 1)) * 100 if cur.get('fit_taxable') else 0
        ytd_fit_rate = (ytd.get('fit_withheld', 0) / ytd.get('fit_taxable', 1)) * 100 if ytd.get('fit_taxable') else 0
        table.add_row("FIT Rate", f"{cur_fit_rate:.1f}%", f"{ytd_fit_rate:.1f}%")
        table.add_row("SS Withheld", f"${cur.get('ss_withheld', 0):,.2f}", f"${ytd.get('ss_withheld', 0):,.2f}")
        table.add_row("Medicare", f"${cur.get('medicare_withheld', 0):,.2f}", f"${ytd.get('medicare_withheld', 0):,.2f}")
        table.add_row("Net Pay", f"[green]${cur.get('net_pay', 0):,.2f}[/green]", f"${ytd.get('net_pay', 0):,.2f}")

        console.print(table)

        # Show sources if available
        sources = result.get("sources", {})
        if sources:
            console.print("\n[dim]Sources:[/dim]")
            for key, src in sources.items():
                if isinstance(src, dict) and "source" in src:
                    console.print(f"  [dim]{key}: {src['source'].get('type', 'unknown')}[/dim]")


@model.command("regular-pay")
@click.argument("year", type=int)
@click.argument("party", type=click.Choice(["him", "her"]))
@click.option("--contrib-401k", type=float, help="401k contribution per period (absolute $)")
@click.option("--contrib-401k-pct", type=float, help="401k contribution as % of gross (e.g., 0.10 for 10%)")
@click.option("--format", "output_format", type=click.Choice(["table", "json"]), default="table",
              help="Output format (default: table)")
def regular_pay(year: int, party: str, contrib_401k: float,
                contrib_401k_pct: float, output_format: str):
    """Model regular pay stubs for a calendar year.

    Shows each pay period's gross, 401k, FIT, FICA, and net pay in a table
    with totals that add up.

    \b
    Examples:
      pay-calc model regular-pay 2026 him
      pay-calc model regular-pay 2026 him --contrib-401k-pct 0.10
      pay-calc model regular-pay 2026 him --format json
    """
    console = Console(width=140)

    # Get first pay date for display info
    first_pay_result = get_first_regular_pay_date(party, year)
    if not first_pay_result.get("success"):
        err = first_pay_result.get("error", {})
        raise click.ClickException(err.get("message", "Could not determine first pay date"))

    starting_date = first_pay_result["date"]
    frequency = first_pay_result.get("frequency", "biweekly")
    employer = first_pay_result.get("employer", "Unknown")

    # Build 401k config if specified
    regular_401k_contribs = None
    if contrib_401k is not None:
        regular_401k_contribs = {
            "starting_date": starting_date,
            "amount": contrib_401k,
            "amount_type": "absolute",
        }
    elif contrib_401k_pct is not None:
        regular_401k_contribs = {
            "starting_date": starting_date,
            "amount": contrib_401k_pct,
            "amount_type": "percentage",
        }

    # Call SDK with year (models full calendar year)
    result = model_regular_401k_contribs(
        year,
        party,
        regular_401k_contribs=regular_401k_contribs,
    )

    if "error" in result:
        raise click.ClickException(result["error"])

    # JSON output
    if output_format == "json":
        click.echo(json.dumps(result, indent=2))
        return

    # Rich table output
    stubs = result.get("stubs", [])
    ytd = result.get("ytd", {})

    console.print(f"\n[bold]Regular Pay Model: {party} {year}[/bold]")
    console.print(f"Employer: {employer}")
    console.print(f"Pay frequency: {frequency}")
    console.print(f"Periods: {result.get('periods_modeled', len(stubs))}")
    console.print()

    table = Table(title=f"Pay Stubs for {year}", expand=True)
    table.add_column("Date", style="cyan")
    table.add_column("Gross", justify="right")
    table.add_column("401k", justify="right", style="green")
    table.add_column("FIT Taxable", justify="right")
    table.add_column("FIT", justify="right")
    table.add_column("SS", justify="right")
    table.add_column("Medicare", justify="right")
    table.add_column("Net Pay", justify="right", style="yellow")

    # Running totals for verification
    totals = {
        "gross": 0, "401k": 0, "fit_taxable": 0, "fit": 0,
        "ss": 0, "medicare": 0, "net": 0
    }

    for stub in stubs:
        gross = stub.get("gross", 0)
        k401 = stub.get("pretax_401k", 0)
        fit_taxable = stub.get("fit_taxable", 0)
        fit = stub.get("fit_withheld", 0)
        ss = stub.get("ss_withheld", 0)
        medicare = stub.get("medicare_withheld", 0)
        net = stub.get("net_pay", 0)

        totals["gross"] += gross
        totals["401k"] += k401
        totals["fit_taxable"] += fit_taxable
        totals["fit"] += fit
        totals["ss"] += ss
        totals["medicare"] += medicare
        totals["net"] += net

        table.add_row(
            stub.get("date", ""),
            f"${gross:,.2f}",
            f"${k401:,.2f}",
            f"${fit_taxable:,.2f}",
            f"${fit:,.2f}",
            f"${ss:,.2f}",
            f"${medicare:,.2f}",
            f"${net:,.2f}",
        )

    # Add totals row
    table.add_section()
    table.add_row(
        "TOTAL",
        f"${totals['gross']:,.2f}",
        f"${totals['401k']:,.2f}",
        f"${totals['fit_taxable']:,.2f}",
        f"${totals['fit']:,.2f}",
        f"${totals['ss']:,.2f}",
        f"${totals['medicare']:,.2f}",
        f"${totals['net']:,.2f}",
        style="bold",
    )

    # Add YTD row from SDK for comparison
    table.add_row(
        "YTD (SDK)",
        f"${ytd.get('gross', 0):,.2f}",
        f"${ytd.get('pretax_401k', 0):,.2f}",
        f"${ytd.get('fit_taxable', 0):,.2f}",
        f"${ytd.get('fit_withheld', 0):,.2f}",
        f"${ytd.get('ss_withheld', 0):,.2f}",
        f"${ytd.get('medicare_withheld', 0):,.2f}",
        f"${ytd.get('net_pay', 0):,.2f}",
        style="dim",
    )

    console.print(table)

    # Check if totals match YTD
    diff_gross = abs(totals["gross"] - ytd.get("gross", 0))
    diff_net = abs(totals["net"] - ytd.get("net_pay", 0))
    if diff_gross > 0.01 or diff_net > 0.01:
        console.print("\n[red]⚠ Totals don't match YTD - check for rounding[/red]")
    else:
        console.print("\n[green]✓ Totals match YTD[/green]")


@model.command("max-401k")
@click.argument("year", type=int)
@click.argument("party", type=click.Choice(["him", "her"]))
@click.option("--compare/--no-compare", default=True,
              help="Show comparison vs $0 401k (default: --compare)")
@click.option("--format", "output_format", type=click.Choice(["table", "json"]), default="table",
              help="Output format (default: table)")
def max_401k(year: int, party: str, compare: bool, output_format: str):
    """Model pay stubs with max 401k contributions.

    Shows what happens if you contribute 100% to 401k (capped at IRS limit).
    With --compare (default), also shows net pay difference vs $0 401k.

    \b
    Examples:
      pay-calc model max-401k 2026 him
      pay-calc model max-401k 2026 him --no-compare
      pay-calc model max-401k 2026 him --format json
    """
    console = Console(width=140)

    # Get first pay date for display info
    first_pay_result = get_first_regular_pay_date(party, year)
    if not first_pay_result.get("success"):
        err = first_pay_result.get("error", {})
        raise click.ClickException(err.get("message", "Could not determine first pay date"))

    frequency = first_pay_result.get("frequency", "biweekly")
    employer = first_pay_result.get("employer", "Unknown")

    # Model max 401k scenario
    result_max = max_regular_401k_contribs(year, party)

    if "error" in result_max:
        raise click.ClickException(result_max["error"])

    # JSON output
    if output_format == "json":
        if compare:
            # Also model $0 401k for comparison
            result_zero = model_stubs_in_sequence(year, party)
            output = {
                "max_401k": result_max,
                "zero_401k": result_zero,
            }
        else:
            output = result_max
        click.echo(json.dumps(output, indent=2))
        return

    # Rich table output
    stubs = result_max.get("stubs", [])
    ytd = result_max.get("ytd", {})

    console.print(f"\n[bold]Max 401k Model: {party} {year}[/bold]")
    console.print(f"Employer: {employer}")
    console.print(f"Pay frequency: {frequency}")
    console.print(f"Periods: {result_max.get('periods_modeled', len(stubs))}")
    console.print()

    table = Table(title=f"Max 401k - Per Check Breakdown", expand=True)
    table.add_column("Date", style="cyan")
    table.add_column("Gross", justify="right")
    table.add_column("401k", justify="right", style="green")
    table.add_column("FIT", justify="right")
    table.add_column("SS", justify="right")
    table.add_column("Medicare", justify="right")
    table.add_column("Net Pay", justify="right", style="yellow")

    for stub in stubs:
        if stub.get("type") == "regular":
            table.add_row(
                stub.get("date", ""),
                f"${stub.get('gross', 0):,.2f}",
                f"${stub.get('pretax_401k', 0):,.2f}",
                f"${stub.get('fit_withheld', 0):,.2f}",
                f"${stub.get('ss_withheld', 0):,.2f}",
                f"${stub.get('medicare_withheld', 0):,.2f}",
                f"${stub.get('net_pay', 0):,.2f}",
            )

    # Add totals
    table.add_section()
    table.add_row(
        "TOTAL",
        f"${ytd.get('gross', 0):,.2f}",
        f"${ytd.get('pretax_401k', 0):,.2f}",
        f"${ytd.get('fit_withheld', 0):,.2f}",
        f"${ytd.get('ss_withheld', 0):,.2f}",
        f"${ytd.get('medicare_withheld', 0):,.2f}",
        f"${ytd.get('net_pay', 0):,.2f}",
        style="bold",
    )

    console.print(table)

    # Comparison table if requested
    if compare:
        console.print()

        # Model $0 401k scenario
        result_zero = model_stubs_in_sequence(year, party)
        if "error" in result_zero:
            console.print(f"[red]Could not model $0 401k scenario: {result_zero['error']}[/red]")
            return

        ytd_zero = result_zero.get("ytd", {})
        ytd_max = result_max.get("ytd", {})

        num_periods = result_max.get("periods_modeled", len(stubs))

        comp_table = Table(title=f"Comparison: {num_periods} Pay Periods", expand=False)
        comp_table.add_column("Scenario", style="cyan")
        comp_table.add_column("Gross", justify="right")
        comp_table.add_column("401k", justify="right")
        comp_table.add_column("Net Pay", justify="right", style="yellow")

        comp_table.add_row(
            "A: $0 401k",
            f"${ytd_zero.get('gross', 0):,.2f}",
            f"${ytd_zero.get('pretax_401k', 0):,.2f}",
            f"${ytd_zero.get('net_pay', 0):,.2f}",
        )
        comp_table.add_row(
            "B: Max 401k",
            f"${ytd_max.get('gross', 0):,.2f}",
            f"${ytd_max.get('pretax_401k', 0):,.2f}",
            f"${ytd_max.get('net_pay', 0):,.2f}",
        )
        comp_table.add_section()

        gross_diff = ytd_zero.get('gross', 0) - ytd_max.get('gross', 0)
        k401_diff = ytd_zero.get('pretax_401k', 0) - ytd_max.get('pretax_401k', 0)
        net_diff = ytd_zero.get('net_pay', 0) - ytd_max.get('net_pay', 0)

        comp_table.add_row(
            "Difference (A - B)",
            f"${gross_diff:,.2f}",
            f"${k401_diff:,.2f}",
            f"${net_diff:,.2f}",
            style="bold red",
        )

        console.print(comp_table)

        console.print()
        console.print("[bold]Bottom Line:[/bold]")
        console.print(f"  401k contributed: [green]${ytd_max.get('pretax_401k', 0):,.2f}[/green]")
        console.print(f"  Net pay reduction: [red]${net_diff:,.2f}[/red]")
        tax_savings = ytd_max.get('pretax_401k', 0) - net_diff
        console.print(f"  Tax savings: [green]${tax_savings:,.2f}[/green]")
