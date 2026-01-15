#!/usr/bin/env python3
"""Model max 401k contributions and compare net pay scenarios.

Models multiple pay periods with max 401k contributions and compares
net pay between contributing $0 vs maxing out early.

Usage:
    python scripts/max_regular_401k_contribs.py <party> <year> --gross <amount> --benefits <amount> --imputed <amount>

Output shows:
    - Each check's gross, FIT, FICA, 401k, net pay
    - The FIT rate used for calculations
    - Forgone net pay (what normal net pay would be)
    - Net pay difference between scenarios
"""

import argparse
import sys
from datetime import date
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from rich.console import Console
from rich.table import Table

from paycalc.sdk.stub_model import model_stub
from paycalc.sdk.w4 import resolve_w4, merge_w4_with_defaults

# Constants
SS_RATE = 0.062
MEDICARE_RATE = 0.0145
DEFAULT_401K_MAX = 23500  # 2025 default


def get_fit_rate_from_w4(party: str, target_date: date) -> float:
    """Get the effective FIT rate based on W-4 settings.

    Returns the marginal rate that would apply for typical income levels.
    This is approximate - actual withholding depends on exact taxable wages.
    """
    w4_result = resolve_w4(party, target_date)
    w4_settings = merge_w4_with_defaults(w4_result["settings"])

    # Determine if using single or MFJ tables
    uses_single = (
        w4_settings.get("filing_status") == "single" or
        w4_settings.get("step2_checkbox", False) or
        w4_settings.get("step2c_multiple_jobs", False)
    )

    # For typical $7800 gross biweekly, approximate the marginal rate
    # With step2c checked: uses single tables, roughly 22% bracket
    # Without step2c: uses MFJ tables, roughly 12% bracket
    if uses_single:
        return 0.21  # Approximate for step2c/single
    else:
        return 0.12  # Approximate for MFJ


def calculate_period(
    gross: float,
    pretax_401k: float,
    benefits: float,
    imputed_income: float,
    party: str,
    pay_date: str,
) -> dict:
    """Calculate a single pay period with given 401k contribution.

    Args:
        gross: Gross pay
        pretax_401k: 401k contribution for this period
        benefits: Pretax benefits (health, dental, etc.)
        imputed_income: Imputed income (GTL, etc.) - taxable but not cash
        party: Party identifier
        pay_date: Pay date string

    Returns:
        Dict with calculated values
    """
    # Use the model for accurate calculations
    result = model_stub(
        pay_date,
        party,
        comp_plan_override={
            "gross_per_period": gross,
            "pay_frequency": "biweekly",
        },
        benefits_override={
            "pretax_health": benefits,  # Simplified - all in health
        },
        pretax_401k=pretax_401k,
        imputed_income=imputed_income,
    )

    if "error" in result:
        raise ValueError(f"Model error: {result['error']}")

    current = result["current"]
    return {
        "gross": gross,
        "fit_taxable": current["fit_taxable"],
        "fit_withheld": current["fit_withheld"],
        "ss_withheld": current["ss_withheld"],
        "medicare_withheld": current["medicare_withheld"],
        "pretax_401k": current["pretax_401k"],
        "net_pay": current["net_pay"],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Model max 401k contributions and compare net pay"
    )
    parser.add_argument("party", help="Party identifier (him/her)")
    parser.add_argument("year", type=int, help="Tax year")
    parser.add_argument("--gross", type=float, required=True,
                        help="Gross pay per period")
    parser.add_argument("--benefits", type=float, required=True,
                        help="Pretax benefits per period")
    parser.add_argument("--imputed", type=float, required=True,
                        help="Imputed income per period (e.g., GTL)")
    parser.add_argument("--max-401k", type=float, default=None,
                        help=f"Annual 401k max (default: {DEFAULT_401K_MAX})")

    args = parser.parse_args()

    # Get 401k max for year
    max_401k = args.max_401k
    if max_401k is None:
        # Use known limits
        limits = {2025: 23500, 2026: 24500}
        max_401k = limits.get(args.year, DEFAULT_401K_MAX)

    console = Console(width=120)

    # Get FIT rate from W-4 for display
    target_date = date(args.year, 1, 15)  # Mid-January
    fit_rate = get_fit_rate_from_w4(args.party, target_date)

    console.print(f"\n[bold]401k Max-Out Calculator[/bold]")
    console.print(f"Party: {args.party}, Year: {args.year}")
    console.print(f"401k annual max: ${max_401k:,.2f}")
    console.print(f"Gross per period: ${args.gross:,.2f}")
    console.print(f"Pretax benefits: ${args.benefits:,.2f}")
    console.print(f"Imputed income (GTL): ${args.imputed:,.2f}")
    console.print(f"Effective FIT rate: {fit_rate*100:.1f}%")
    console.print()

    # Calculate "maximum" net pay (with $0 401k contribution)
    # This represents what you'd take home if you didn't contribute to 401k at all
    # The forgone net is based on this maximum possible take-home
    zero_401k_result = calculate_period(
        args.gross, 0.0, args.benefits, args.imputed,
        args.party, f"{args.year}-01-17"  # Typical pay date
    )
    max_possible_net = zero_401k_result["net_pay"]
    max_possible_fit = zero_401k_result["fit_withheld"]
    # Calculate FIT rate for the zero-401k scenario
    if zero_401k_result["fit_taxable"] > 0:
        zero_401k_fit_rate = zero_401k_result["fit_withheld"] / zero_401k_result["fit_taxable"]
    else:
        zero_401k_fit_rate = fit_rate

    console.print(f"Baseline: Max net pay with $0 401k = ${max_possible_net:,.2f}")
    console.print(f"Baseline FIT (no 401k): ${max_possible_fit:,.2f} ({zero_401k_fit_rate*100:.1f}%)")

    # Update variables used later
    normal_net = max_possible_net
    normal_fit = max_possible_fit
    normal_fit_rate = zero_401k_fit_rate
    console.print()

    # Calculate max 401k per period (max out in fewest checks possible)
    # Available for 401k each period = gross - benefits - min_for_fica
    # But we also need to cover FIT on any remaining taxable income

    remaining_401k = max_401k
    periods = []
    check_num = 1

    # Simulate Jan pay dates (every 2 weeks)
    pay_dates = [
        f"{args.year}-01-03",
        f"{args.year}-01-17",
        f"{args.year}-01-31",
        f"{args.year}-02-14",
        f"{args.year}-02-28",
    ]

    while remaining_401k > 0 and check_num <= len(pay_dates):
        pay_date = pay_dates[check_num - 1]

        # Calculate max we can contribute this period
        # gross - benefits - imputed = available for 401k + FICA + FIT + net
        available = args.gross - args.benefits

        # For max 401k: try to contribute as much as possible
        # We need to leave enough for FICA at minimum
        fica = args.gross * (SS_RATE + MEDICARE_RATE)

        # Max 401k this period (can't exceed remaining or gross-benefits-fica)
        max_this_period = min(remaining_401k, available - fica)

        # But if we do max, there may be nothing left for FIT, so FIT=0
        # and net_pay = gross - benefits - 401k - fica

        result = calculate_period(
            args.gross, max_this_period, args.benefits, args.imputed,
            args.party, pay_date
        )

        # If net pay is very small or negative, reduce 401k
        if result["net_pay"] < 0:
            # Find the 401k amount that leaves net_pay = 0
            # Binary search or iterative
            low, high = 0, max_this_period
            while high - low > 1:
                mid = (low + high) / 2
                test = calculate_period(
                    args.gross, mid, args.benefits, args.imputed,
                    args.party, pay_date
                )
                if test["net_pay"] >= 0:
                    low = mid
                else:
                    high = mid
            max_this_period = low
            result = calculate_period(
                args.gross, max_this_period, args.benefits, args.imputed,
                args.party, pay_date
            )

        # Calculate actual FIT rate for this period
        # When FIT withheld is very small (<$1), show the expected rate from W-4
        # since the calculated rate is meaningless at near-zero taxable income
        if result["fit_withheld"] < 1.0:
            actual_fit_rate = fit_rate  # Use expected rate when effectively 0 withheld
        elif result["fit_taxable"] > 0:
            actual_fit_rate = result["fit_withheld"] / result["fit_taxable"]
        else:
            actual_fit_rate = fit_rate

        # Calculate forgone net pay (difference from normal)
        forgone = normal_net - result["net_pay"]

        periods.append({
            "check": check_num,
            "gross": args.gross,
            "benefits": args.benefits,
            "fit_withheld": result["fit_withheld"],
            "fica": result["ss_withheld"] + result["medicare_withheld"],
            "pretax_401k": max_this_period,
            "net_pay": result["net_pay"],
            "fit_rate": actual_fit_rate,
            "forgone_fit": normal_fit,
            "forgone_fit_rate": normal_fit_rate,
            "forgone_net": forgone,
        })

        remaining_401k -= max_this_period
        check_num += 1

    # Build rich table
    # Columns: Gross = Benefits + 401k + FIT + FICA + Imputed + Net
    # (Imputed is phantom income that reduces net but isn't real cash)
    table = Table(title="Max 401k Early - Per Check Breakdown", expand=True)
    table.add_column("Chk", justify="center", style="cyan", width=5)
    table.add_column("Gross", justify="right", no_wrap=True)
    table.add_column("Benefits", justify="right", no_wrap=True)
    table.add_column("401k", justify="right", style="green", no_wrap=True)
    table.add_column("FIT (Rate)", justify="right", no_wrap=True)
    table.add_column("FICA", justify="right", no_wrap=True)
    table.add_column("Imputed", justify="right", no_wrap=True, style="dim")
    table.add_column("Net Pay", justify="right", style="yellow", no_wrap=True)
    table.add_column("FIT (Rate)", justify="right", no_wrap=True, style="dim")  # Forgone FIT
    table.add_column("Forgone Net", justify="right", style="red", no_wrap=True)

    total_401k = 0
    total_forgone = 0

    for p in periods:
        # Combine FIT amount and rate for actual (max 401k) scenario
        fit_str = f"${p['fit_withheld']:,.2f} ({p['fit_rate']*100:.0f}%)"
        # Combine FIT amount and rate for forgone (no 401k) scenario
        forgone_fit_str = f"${p['forgone_fit']:,.2f} ({p['forgone_fit_rate']*100:.0f}%)"
        table.add_row(
            str(p["check"]),
            f"${p['gross']:,.2f}",
            f"${p['benefits']:,.2f}",
            f"${p['pretax_401k']:,.2f}",
            fit_str,
            f"${p['fica']:,.2f}",
            f"${args.imputed:,.2f}",
            f"${p['net_pay']:,.2f}",
            forgone_fit_str,
            f"${p['forgone_net']:,.2f}",
        )
        total_401k += p["pretax_401k"]
        total_forgone += p["forgone_net"]

    # Add totals row
    table.add_section()
    table.add_row(
        "TOTAL",
        "",
        "",
        f"${total_401k:,.2f}",
        "",
        "",
        "",
        "",
        "",
        f"${total_forgone:,.2f}",
        style="bold",
    )

    console.print(table)

    # Now calculate the simple comparison:
    # Model A: N checks with $0 401k
    # Model B: N checks with max 401k (what we calculated above)
    num_checks = len(periods)

    # Model A: $0 401k for each check
    model_a_gross = 0
    model_a_401k = 0
    model_a_net = 0
    for i, pay_date in enumerate(pay_dates[:num_checks]):
        result = calculate_period(
            args.gross, 0.0, args.benefits, args.imputed,
            args.party, pay_date
        )
        model_a_gross += args.gross
        model_a_401k += 0
        model_a_net += result["net_pay"]

    # Model B: Max 401k (already calculated)
    model_b_gross = sum(p["gross"] for p in periods)
    model_b_401k = total_401k
    model_b_net = sum(p["net_pay"] for p in periods)

    # Build comparison table
    console.print()
    console.print(f"[bold]Comparison: {num_checks} Checks[/bold]")

    comp_table = Table(title=f"Total Over {num_checks} Checks", expand=False)
    comp_table.add_column("Scenario", style="cyan")
    comp_table.add_column("Gross", justify="right")
    comp_table.add_column("401k", justify="right")
    comp_table.add_column("Net Pay", justify="right", style="yellow")

    comp_table.add_row(
        "A: $0 401k",
        f"${model_a_gross:,.2f}",
        f"${model_a_401k:,.2f}",
        f"${model_a_net:,.2f}",
    )
    comp_table.add_row(
        f"B: Max 401k (${max_401k:,.0f})",
        f"${model_b_gross:,.2f}",
        f"${model_b_401k:,.2f}",
        f"${model_b_net:,.2f}",
    )
    comp_table.add_section()
    comp_table.add_row(
        "Difference (A - B)",
        f"${model_a_gross - model_b_gross:,.2f}",
        f"${model_a_401k - model_b_401k:,.2f}",
        f"${model_a_net - model_b_net:,.2f}",
        style="bold red",
    )

    console.print(comp_table)

    console.print()
    console.print(f"[bold]Bottom Line:[/bold]")
    console.print(f"  Net pay difference over {num_checks} checks: [bold red]${model_a_net - model_b_net:,.2f}[/bold red]")


if __name__ == "__main__":
    main()
