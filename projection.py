#!/usr/bin/env python3
"""
Year-end projection from pay stub analysis data.

Reads the analysis JSON output and projects year-end totals based on
observed pay patterns (regular pay cadence, stock vesting schedule).

Usage:
    python3 projection.py <analysis_json> [--format text|json]

Example:
    python3 projection.py data/2025_pay_stubs_full.json
    python3 projection.py data/2025_pay_stubs_full.json --format json
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List
import yaml


def load_tax_rules(year: str) -> tuple[dict, str, bool]:
    """Load tax rules for a specific year, falling back to closest year if needed.

    Returns: (rules_dict, year_used, exact_match)
    """
    rules_dir = Path(__file__).parent / "tax-rules"
    rules_path = rules_dir / f"{year}.yaml"

    if rules_path.exists():
        with open(rules_path) as f:
            return yaml.safe_load(f), year, True

    # Find closest configured year
    available_years = sorted([
        int(p.stem) for p in rules_dir.glob("*.yaml") if p.stem.isdigit()
    ])

    if not available_years:
        return {}, year, False

    target_year = int(year)
    closest_year = min(available_years, key=lambda y: abs(y - target_year))

    with open(rules_dir / f"{closest_year}.yaml") as f:
        return yaml.safe_load(f), str(closest_year), False


def parse_pay_date(date_str: str) -> datetime:
    """Parse a pay date string into a datetime object."""
    if not date_str:
        return datetime.min

    formats = ["%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return datetime.min


def detect_employer_segments(stubs: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Split stubs into segments by employer based on YTD resets."""
    if not stubs:
        return []

    segments = []
    current_segment = []
    prev_ytd = 0

    for stub in stubs:
        ytd_gross = stub.get("pay_summary", {}).get("ytd", {}).get("gross", 0)

        # Detect YTD reset (employer change)
        if prev_ytd > 10000 and ytd_gross < prev_ytd * 0.5:
            if current_segment:
                segments.append(current_segment)
            current_segment = [stub]
        else:
            current_segment.append(stub)

        prev_ytd = ytd_gross

    if current_segment:
        segments.append(current_segment)

    return segments


def generate_projection(stubs: List[Dict[str, Any]], year: str) -> Dict[str, Any]:
    """Generate year-end projection based on observed pay patterns."""
    if not stubs:
        return {}

    year_int = int(year)
    year_end = datetime(year_int, 12, 31)

    # Get the last stub and its date
    last_stub = stubs[-1]
    last_date_str = last_stub.get("pay_date", "")
    last_date = parse_pay_date(last_date_str)
    if last_date == datetime.min:
        return {}

    # Calculate days remaining in year
    days_remaining = (year_end - last_date).days
    if days_remaining <= 0:
        return {}  # Year complete, no projection needed

    # Analyze regular pay pattern
    regular_stubs = [s for s in stubs if s.get("_pay_type") == "regular"]
    regular_stubs.sort(key=lambda s: parse_pay_date(s.get("pay_date", "")))

    regular_projection = 0.0
    regular_info = {}
    if len(regular_stubs) >= 2:
        dates = [parse_pay_date(s.get("pay_date", "")) for s in regular_stubs]
        dates = [d for d in dates if d != datetime.min]

        if len(dates) >= 2:
            intervals = [(dates[i+1] - dates[i]).days for i in range(len(dates)-1)]
            avg_interval = sum(intervals) / len(intervals)

            # Round to nearest common pay frequency
            if 12 <= avg_interval <= 16:
                pay_interval = 14  # Biweekly
                frequency = "biweekly"
            elif 6 <= avg_interval <= 8:
                pay_interval = 7  # Weekly
                frequency = "weekly"
            elif 28 <= avg_interval <= 32:
                pay_interval = 30  # Monthly
                frequency = "monthly"
            else:
                pay_interval = round(avg_interval)
                frequency = f"~{pay_interval} days"

            last_regular = regular_stubs[-1]
            last_regular_current = last_regular.get("pay_summary", {}).get("current", {}).get("gross", 0)
            last_regular_date = parse_pay_date(last_regular.get("pay_date", ""))

            # Count remaining pay periods
            remaining_periods = 0
            if last_regular_date != datetime.min:
                next_pay = last_regular_date
                while True:
                    next_pay = next_pay + timedelta(days=pay_interval)
                    if next_pay > year_end:
                        break
                    remaining_periods += 1

            regular_projection = last_regular_current * remaining_periods

            regular_info = {
                "interval_days": pay_interval,
                "frequency": frequency,
                "last_pay_date": last_regular_date.strftime("%Y-%m-%d") if last_regular_date != datetime.min else None,
                "last_amount": last_regular_current,
                "remaining_periods": remaining_periods,
                "projected": regular_projection
            }

    # Analyze RSU/stock grant pattern
    stock_stubs = [s for s in stubs if s.get("_pay_type") == "stock_grant"]
    stock_stubs.sort(key=lambda s: parse_pay_date(s.get("pay_date", "")))

    stock_projection = 0.0
    stock_info = {}
    if stock_stubs:
        from collections import defaultdict
        monthly_totals = defaultdict(float)
        for s in stock_stubs:
            d = parse_pay_date(s.get("pay_date", ""))
            if d != datetime.min:
                month_key = d.month
                current_gross = s.get("pay_summary", {}).get("current", {}).get("gross", 0)
                monthly_totals[month_key] += current_gross

        months_with_vests = sorted(monthly_totals.keys())
        avg_vesting = sum(monthly_totals.values()) / len(monthly_totals) if monthly_totals else 0

        last_stock_date = parse_pay_date(stock_stubs[-1].get("pay_date", ""))

        if len(months_with_vests) >= 8:
            frequency = "monthly"
            remaining_vest_months = [m for m in range(1, 13) if m not in months_with_vests and m <= 12]
        else:
            frequency = "quarterly" if len(months_with_vests) <= 4 else "irregular"
            remaining_vest_months = [m for m in months_with_vests if m > last_stock_date.month]

        remaining_vests = len(remaining_vest_months)

        if remaining_vests > 0:
            stock_projection = avg_vesting * remaining_vests
            stock_info = {
                "frequency": frequency,
                "months_with_vests": months_with_vests,
                "avg_vesting": avg_vesting,
                "remaining_months": remaining_vest_months,
                "remaining_vests": remaining_vests,
                "projected": stock_projection
            }

    # Get actual YTD totals combined across all employer segments
    segments = detect_employer_segments(stubs)
    actual_gross = 0.0
    actual_fit_taxable = 0.0
    actual_taxes = 0.0
    for segment in segments:
        if segment:
            seg_ytd = segment[-1].get("pay_summary", {}).get("ytd", {})
            actual_gross += seg_ytd.get("gross", 0)
            actual_fit_taxable += seg_ytd.get("fit_taxable_wages", 0)
            actual_taxes += seg_ytd.get("taxes", 0)

    # Calculate projected totals
    total_projection = regular_projection + stock_projection
    projected_gross = actual_gross + total_projection

    # Estimate projected taxes (use effective rate from actuals)
    effective_tax_rate = actual_taxes / actual_gross if actual_gross > 0 else 0.25
    projected_additional_taxes = total_projection * effective_tax_rate
    projected_total_taxes = actual_taxes + projected_additional_taxes

    return {
        "as_of_date": last_date_str,
        "days_remaining": days_remaining,
        "actual": {
            "gross": actual_gross,
            "fit_taxable_wages": actual_fit_taxable,
            "taxes_withheld": actual_taxes
        },
        "projected_additional": {
            "regular_pay": regular_projection,
            "stock_grants": stock_projection,
            "total_gross": total_projection,
            "taxes": projected_additional_taxes
        },
        "projected_total": {
            "gross": projected_gross,
            "taxes_withheld": projected_total_taxes
        },
        "regular_pay_info": regular_info,
        "stock_grant_info": stock_info
    }


def print_text_report(projection: Dict[str, Any], analysis_data: Dict[str, Any]):
    """Print projection report as formatted text."""
    if not projection:
        print("No projection data available - year may be complete.")
        return

    summary = analysis_data.get("summary", {})
    ytd_breakdown = analysis_data.get("ytd_breakdown", {})
    contrib_401k = analysis_data.get("contributions_401k", {}).get("yearly_totals", {})

    print("=" * 60)
    print(f"YEAR-END PROJECTION (as of {projection.get('as_of_date', 'N/A')}, {projection.get('days_remaining', 0)} days remaining)")
    print("=" * 60)
    print()

    actual = projection.get("actual", {})
    additional = projection.get("projected_additional", {})
    total = projection.get("projected_total", {})

    # Load tax rules for 401k limits
    report_year = summary.get("year", "2025")
    tax_rules, rules_year, exact_match = load_tax_rules(report_year)
    k401_limits = tax_rules.get("401k", {})
    total_annual_limit = k401_limits.get("total_annual_limit", 70000)

    if not exact_match:
        print(f"  ⚠ Tax rules for {report_year} not found, using {rules_year} rules")
        print()

    # Get 401k totals
    total_401k = contrib_401k.get("total", 0)

    # Project additional 401k needed to reach total annual limit, but cap it
    # at the amount of projected regular pay available.
    needed_401k = max(0, total_annual_limit - total_401k)
    reg_proj = additional.get("regular_pay", 0)
    projected_401k_add = min(needed_401k, reg_proj)
    projected_401k_total = total_401k + projected_401k_add

    # Calculate total compensation (gross + all 401k)
    actual_total_comp = actual.get('gross', 0) + total_401k
    projected_total_comp = total.get('gross', 0) + projected_401k_total

    # Main projection table
    print(f"  {'Category':<25} {'Actual':>14} {'Projected Add':>14} {'Est. Total':>14}")
    print(f"  {'─' * 25} {'─' * 14} {'─' * 14} {'─' * 14}")

    # Gross
    print(f"  {'Gross':<25} ${actual.get('gross', 0):>13,.2f} ${additional.get('total_gross', 0):>13,.2f} ${total.get('gross', 0):>13,.2f}")

    # Break down by type
    ytd_earnings = ytd_breakdown.get("earnings", {}) if ytd_breakdown else {}
    actual_regular = ytd_earnings.get("Regular Pay", 0)
    actual_stock = ytd_earnings.get("Goog Stock Unit", 0)
    actual_other = actual.get('gross', 0) - actual_regular - actual_stock

    stock_proj = additional.get("stock_grants", 0)

    # Reduce regular pay projection by 401k projection (401k comes out of regular pay)
    reg_proj_display = reg_proj - projected_401k_add
    print(f"    {'└ Regular Pay':<23} ${actual_regular:>13,.2f} ${reg_proj_display:>13,.2f}")
    print(f"    {'└ Stock Vesting':<23} ${actual_stock:>13,.2f} ${stock_proj:>13,.2f}")
    print(f"    {'└ Other (bonuses, etc)':<23} ${actual_other:>13,.2f} {'$0.00':>14}")

    # 401k
    if total_401k > 0 or projected_401k_add > 0:
        print(f"  {'+ 401k Contributions':<25} ${total_401k:>13,.2f} ${projected_401k_add:>13,.2f} ${projected_401k_total:>13,.2f}")

    # Total Compensation
    projected_comp_add = additional.get('total_gross', 0) + projected_401k_add
    print(f"  {'─' * 25} {'─' * 14} {'─' * 14} {'─' * 14}")
    print(f"  {'Total Compensation':<25} ${actual_total_comp:>13,.2f} ${projected_comp_add:>13,.2f} ${projected_total_comp:>13,.2f}")

    # Taxes
    print(f"  {'Taxes Withheld':<25} ${actual.get('taxes_withheld', 0):>13,.2f} ${additional.get('taxes', 0):>13,.2f} ${total.get('taxes_withheld', 0):>13,.2f}")
    print(f"  {'─' * 25} {'─' * 14} {'─' * 14} {'─' * 14}")

    # Pattern info
    reg_info = projection.get("regular_pay_info", {})
    stock_info = projection.get("stock_grant_info", {})

    if reg_info:
        print(f"\n  Regular Pay Pattern:")
        print(f"    Frequency: {reg_info.get('frequency', 'unknown')} ({reg_info.get('interval_days', 0)} days)")
        print(f"    Last pay date: {reg_info.get('last_pay_date', 'unknown')}")
        print(f"    Last amount: ${reg_info.get('last_amount', 0):,.2f}")
        print(f"    Remaining periods: {reg_info.get('remaining_periods', 0)}")

    if stock_info:
        print(f"\n  Stock Vesting Pattern:")
        month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        print(f"    Frequency: {stock_info.get('frequency', 'unknown')}")
        remaining = stock_info.get('remaining_months', [])
        remaining_names = [month_names[m-1] for m in remaining]
        print(f"    Remaining months: {', '.join(remaining_names) if remaining_names else 'none'}")
        print(f"    Avg per vest: ${stock_info.get('avg_vesting', 0):,.2f}")
        print(f"    Remaining vests: {stock_info.get('remaining_vests', 0)}")

    print("\n" + "=" * 60)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 projection.py <analysis_json> [--format text|json]", file=sys.stderr)
        print("  analysis_json: Path to pay stub analysis JSON (from analysis.py)", file=sys.stderr)
        print("  --format: Output format (default: text)", file=sys.stderr)
        sys.exit(1)

    input_file = Path(sys.argv[1])
    output_format = "text"

    if "--format" in sys.argv:
        idx = sys.argv.index("--format")
        if idx + 1 < len(sys.argv):
            output_format = sys.argv[idx + 1]

    if not input_file.exists():
        print(f"Error: Input file not found: {input_file}", file=sys.stderr)
        print("", file=sys.stderr)
        print("Run pay-analysis first to generate the required input:", file=sys.stderr)
        print(f"    pay-calc pay-analysis <year> --cache", file=sys.stderr)
        sys.exit(1)

    # Load analysis data
    with open(input_file) as f:
        analysis_data = json.load(f)

    stubs = analysis_data.get("stubs", [])
    if not stubs:
        print(f"Error: No pay stub data found in {input_file}", file=sys.stderr)
        sys.exit(1)

    year = analysis_data.get("summary", {}).get("year", "2025")

    # Generate projection
    projection = generate_projection(stubs, year)

    # Output
    if output_format == "json":
        print(json.dumps(projection, indent=2))
    else:
        print_text_report(projection, analysis_data)


if __name__ == "__main__":
    main()
