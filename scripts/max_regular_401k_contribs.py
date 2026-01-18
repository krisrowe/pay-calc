#!/usr/bin/env python3
"""Model max 401k contributions and compare net pay scenarios.

Uses SDK max_regular_401k_contribs to model pay periods with max 401k
contributions and compares net pay between contributing $0 vs maxing out.

Usage:
    python scripts/max_regular_401k_contribs.py <party> <year>

Output shows:
    - Each check's gross, 401k, net pay through first $0 401k period
    - Side-by-side comparison of with vs without 401k for those periods
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from rich.console import Console
from rich.table import Table

from paycalc.sdk.modeling import (
    get_first_regular_pay_date,
    model_401k_max_frontload,
    model_stubs_in_sequence,
)


def main():
    parser = argparse.ArgumentParser(
        description="Model max 401k contributions and compare net pay"
    )
    parser.add_argument("party", help="Party identifier (him/her)")
    parser.add_argument("year", type=int, help="Tax year")

    args = parser.parse_args()

    console = Console(width=120)

    # Get first pay date from SDK
    first_pay_result = get_first_regular_pay_date(args.party, args.year)
    if not first_pay_result.get("success"):
        console.print(f"[red]Error: {first_pay_result.get('error', {}).get('message', 'Unknown error')}[/red]")
        sys.exit(1)

    first_pay_date = first_pay_result["date"]
    frequency = first_pay_result.get("frequency", "biweekly")
    employer = first_pay_result.get("employer", "Unknown")

    console.print(f"\n[bold]401k Max-Out Comparison[/bold]")
    console.print(f"Party: {args.party}, Year: {args.year}")
    console.print(f"Employer: {employer}")
    console.print(f"First pay date: {first_pay_date}, Frequency: {frequency}")
    console.print()

    # Model A: Max 401k from first pay date
    result_max = model_401k_max_frontload(
        args.year,
        args.party,
        starting_date=first_pay_date,
    )

    if "error" in result_max:
        console.print(f"[red]Error modeling max 401k: {result_max['error']}[/red]")
        sys.exit(1)

    # TODO: Move this logic to SDK (e.g., compare_401k_scenarios) to keep script thin
    # Find periods to display: all 401k periods + first $0 401k period
    regular_stubs_max = [s for s in result_max["stubs"] if s.get("type") == "regular"]

    # Find first period with $0 401k
    first_zero_idx = None
    for i, stub in enumerate(regular_stubs_max):
        if stub["pretax_401k"] == 0:
            first_zero_idx = i
            break

    # Show through first $0 period (or all if no $0 found)
    if first_zero_idx is not None:
        display_count = first_zero_idx + 1
    else:
        display_count = len(regular_stubs_max)

    stubs_to_show = regular_stubs_max[:display_count]

    # Model B: $0 401k (no special deductions) - full year
    result_zero = model_stubs_in_sequence(
        args.year,
        args.party,
    )

    if "error" in result_zero:
        console.print(f"[red]Error modeling $0 401k: {result_zero['error']}[/red]")
        sys.exit(1)

    regular_stubs_zero = [s for s in result_zero["stubs"] if s.get("type") == "regular"]

    # Build date lookup for $0 scenario
    zero_by_date = {s["date"]: s for s in regular_stubs_zero}

    # Side-by-side comparison table
    table = Table(title=f"401k Comparison: Periods 1-{display_count}", expand=True)
    table.add_column("#", style="dim", justify="right")
    table.add_column("Date", style="cyan")
    table.add_column("Gross", justify="right")
    table.add_column("401k (Max)", justify="right", style="green")
    table.add_column("Net (Max)", justify="right", style="yellow")
    table.add_column("401k ($0)", justify="right", style="dim")
    table.add_column("Net ($0)", justify="right", style="yellow")

    # Accumulators for totals
    total_gross = 0
    total_401k_max = 0
    total_net_max = 0
    total_401k_zero = 0
    total_net_zero = 0

    for i, stub_max in enumerate(stubs_to_show, 1):
        stub_zero = zero_by_date.get(stub_max["date"], {})

        gross = stub_max["gross"]
        k401_max = stub_max["pretax_401k"]
        net_max = stub_max["net_pay"]
        k401_zero = stub_zero.get("pretax_401k", 0)
        net_zero = stub_zero.get("net_pay", 0)

        total_gross += gross
        total_401k_max += k401_max
        total_net_max += net_max
        total_401k_zero += k401_zero
        total_net_zero += net_zero

        table.add_row(
            str(i),
            stub_max["date"],
            f"${gross:,.2f}",
            f"${k401_max:,.2f}",
            f"${net_max:,.2f}",
            f"${k401_zero:,.2f}",
            f"${net_zero:,.2f}",
        )

    # Add totals
    table.add_section()
    table.add_row(
        "",
        "TOTAL",
        f"${total_gross:,.2f}",
        f"${total_401k_max:,.2f}",
        f"${total_net_max:,.2f}",
        f"${total_401k_zero:,.2f}",
        f"${total_net_zero:,.2f}",
        style="bold",
    )

    console.print(table)

    # Summary
    console.print()
    net_diff = total_net_zero - total_net_max
    console.print(f"[bold]Through Period {display_count}:[/bold]")
    console.print(f"  401k contributed (max): [green]${total_401k_max:,.2f}[/green]")
    console.print(f"  Net pay with max 401k:  [yellow]${total_net_max:,.2f}[/yellow]")
    console.print(f"  Net pay with $0 401k:   [yellow]${total_net_zero:,.2f}[/yellow]")
    console.print(f"  Net pay difference:     [red]${net_diff:,.2f}[/red] more without 401k")

    if first_zero_idx is not None:
        console.print()
        console.print(f"[dim]401k limit reached in period {first_zero_idx + 1} ({regular_stubs_max[first_zero_idx]['date']})[/dim]")


if __name__ == "__main__":
    main()
