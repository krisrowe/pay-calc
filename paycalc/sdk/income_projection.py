"""Income projection from pay stub analysis data.

Generates year-end income projections based on observed pay patterns
(regular pay cadence) and RSU vesting schedules (from RSU SDK).
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional

from .config import get_data_path, load_profile


def is_rsus_enabled(party: str) -> bool:
    """Check if RSUs are enabled for a party in the profile.

    Checks party-level rsus_enabled first, then falls back to checking
    if any company for that party has rsus_enabled: true.

    Returns False if not defined or explicitly set to false.
    """
    try:
        profile = load_profile()
        parties = profile.get("parties", {})
        party_config = parties.get(party, {})

        # Check party-level setting first
        if party_config.get("rsus_enabled"):
            return True

        # Check company-level settings
        companies = party_config.get("companies", [])
        for company in companies:
            if company.get("rsus_enabled"):
                return True

        return False
    except Exception:
        return False


def get_rsu_projection(
    year: str,
    price: Optional[float] = None,
    after_date: Optional[date] = None,
) -> Dict[str, Any]:
    """Get RSU vesting projection for remaining period of a year.

    Args:
        year: Tax year (4 digits)
        price: Optional stock price for value calculation
        after_date: Optional date to project from (e.g., latest pay stub date).
                    If not provided, uses today's date for current year.

    Returns:
        Dict with rsu_shares, rsu_gross, months_covered, warnings
    """
    from .rsus import get_vesting_projection, find_latest_export

    year_int = int(year)

    # Determine start date for projection
    if after_date:
        # Use provided date (e.g., latest pay stub date)
        # Add 1 day so we don't double-count vests on that exact date
        start_date = after_date + timedelta(days=1)
    else:
        # Fall back to today for current year, or Jan 1 for future years
        today = date.today()
        if today.year == year_int:
            start_date = today
        else:
            start_date = date(year_int, 1, 1)
    end_date = date(year_int, 12, 31)

    # Check if RSU data is available
    csv_path = find_latest_export()
    if csv_path is None:
        return {
            "rsu_shares": 0,
            "rsu_gross": 0,
            "months_covered": [],
            "warnings": ["No RSU export file found"]
        }

    try:
        proj = get_vesting_projection(
            start_date=start_date,
            end_date=end_date,
            price=price
        )

        if "error" in proj:
            return {
                "rsu_shares": 0,
                "rsu_gross": 0,
                "months_covered": [],
                "warnings": [proj["error"]]
            }

        # Check which months have vesting data
        by_month = proj.get("by_month", {})
        months_covered = list(by_month.keys())

        # Check for gaps - warn if any future month in year is missing
        warnings = []
        current_month = start_date.month
        for m in range(current_month, 13):
            month_key = f"{year}-{m:02d}"
            if month_key not in by_month:
                warnings.append(f"No RSU vesting data for {month_key}")

        return {
            "rsu_shares": proj.get("total_shares", 0),
            "rsu_gross": proj.get("total_value", 0) if price else 0,
            "months_covered": months_covered,
            "warnings": warnings,
            "by_month": by_month,
            "price": price
        }
    except Exception as e:
        return {
            "rsu_shares": 0,
            "rsu_gross": 0,
            "months_covered": [],
            "warnings": [str(e)]
        }


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


def generate_projection(
    stubs: List[Dict[str, Any]],
    year: str,
    party: Optional[str] = None,
    stock_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Generate year-end projection based on pay patterns and RSU schedule.

    Args:
        stubs: List of pay stub dicts from analysis
        year: Tax year (4 digits)
        party: Optional party name ('him'/'her') to check RSU configuration
        stock_price: Optional stock price for RSU value calculation

    Returns:
        Projection dict with actual, projected_additional, projected_total
    """
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

    # Analyze RSU/stock grant projection
    stock_projection = 0.0
    stock_info = {}
    rsu_warnings = []

    # Use RSU SDK if party has RSUs enabled
    if party and is_rsus_enabled(party):
        # Pass latest stub date so we only project vests after that date
        last_stub_date = last_date.date() if hasattr(last_date, 'date') else last_date
        rsu_proj = get_rsu_projection(year, price=stock_price, after_date=last_stub_date)
        stock_projection = rsu_proj.get("rsu_gross", 0)
        rsu_warnings = rsu_proj.get("warnings", [])

        if stock_projection > 0 or rsu_proj.get("rsu_shares", 0) > 0:
            stock_info = {
                "source": "rsu_sdk",
                "frequency": "from_schedule",
                "after_date": last_stub_date.isoformat() if hasattr(last_stub_date, 'isoformat') else str(last_stub_date),
                "rsu_shares": rsu_proj.get("rsu_shares", 0),
                "months_covered": rsu_proj.get("months_covered", []),
                "price": stock_price,
                "projected": stock_projection,
                "warnings": rsu_warnings
            }
    else:
        # Fall back to stub-based inference if RSUs not configured
        stock_stubs = [s for s in stubs if s.get("_pay_type") == "stock_grant"]
        stock_stubs.sort(key=lambda s: parse_pay_date(s.get("pay_date", "")))

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
                    "source": "stub_inference",
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


def generate_income_projection(
    year: str,
    party: str,
    data_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Generate income projection from analysis data.

    Convenience wrapper that loads analysis data and generates projection.

    Args:
        year: Tax year (4 digits)
        party: 'him' or 'her'
        data_dir: Override data directory

    Returns:
        Projection dict with actual, projected_additional, projected_total

    Raises:
        FileNotFoundError: If analysis data doesn't exist
    """
    data_path = data_dir or get_data_path()
    analysis_file = data_path / f"{year}_{party}_pay_all.json"

    if not analysis_file.exists():
        raise FileNotFoundError(
            f"Analysis data not found: {analysis_file}\n"
            f"Run 'pay-calc analysis {year} {party}' first."
        )

    with open(analysis_file) as f:
        analysis_data = json.load(f)

    stubs = analysis_data.get("stubs", [])
    if not stubs:
        return {}

    return generate_projection(stubs, year, party=party)
