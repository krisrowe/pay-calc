"""
RSU vesting schedule parser for Equity Awards Center exports.

Parses CSV exports and provides vesting projections.
"""

import json
import shutil
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Callable

import yaml

from .config import get_data_path


def get_tax_rules(year: int) -> Dict:
    """
    Load tax rules for a given year from tax_rules.yaml.

    Args:
        year: Tax year to load rules for

    Returns:
        Dict with tax rules for that year
    """
    config_path = Path(__file__).parent.parent / "config" / "tax_rules.yaml"

    with open(config_path) as f:
        all_rules = yaml.safe_load(f)

    # Try exact year, fall back to most recent year available
    if year in all_rules:
        return all_rules[year]

    # Fall back to most recent year
    available_years = sorted([y for y in all_rules.keys() if isinstance(y, int)], reverse=True)
    if available_years:
        return all_rules[available_years[0]]

    # Hardcoded fallback (should never reach here)
    return {
        "ss_wage_base": 176100,
        "ss_rate": 0.062,
        "medicare_rate": 0.0145,
        "medicare_additional_rate": 0.009,
        "medicare_additional_threshold": 200000,
        "supplemental_rate": 0.22
    }


def get_rsus_path() -> Path:
    """Get the rsus subdirectory of the data path."""
    rsus_path = get_data_path() / "rsus"
    rsus_path.mkdir(parents=True, exist_ok=True)
    return rsus_path


def find_rsu_tax_rate_from_records() -> Optional[Dict]:
    """
    Search pay records for RSU-related earnings and extract federal withholding rate.

    Uses the SDK's list_records to find stubs with RSU earnings (e.g., stock unit vesting),
    sorted by pay_date descending to get the most recent.

    Returns:
        Dict with rate, source record, and date if found, else None
    """
    from .records import list_records

    # RSU-related earnings type patterns (case-insensitive)
    RSU_PATTERNS = ["stock unit", "rsu", "equity", "vest", "restricted stock"]

    # Get all stubs, sorted by date descending
    try:
        records = list_records(type_filter="stub")
    except Exception:
        return None

    if not records:
        return None

    # Sort by pay_date descending (most recent first)
    records.sort(
        key=lambda r: r.get("data", {}).get("pay_date", ""),
        reverse=True
    )

    for record in records:
        result = _check_record_for_rsu_rate(record, RSU_PATTERNS)
        if result:
            return result

    return None


def _get_current_amount(item: Dict) -> float:
    """Get current amount from earnings/deductions, handling different field names."""
    return item.get("current_amount") or item.get("current") or 0


def _check_record_for_rsu_rate(record: Dict, rsu_patterns: List[str]) -> Optional[Dict]:
    """Check a single record for RSU earnings and extract fed withholding rate."""
    try:
        data = record.get("data", {})
        earnings = data.get("earnings", [])
        taxes = data.get("taxes", {})

        # Look for RSU-related earnings
        rsu_earnings = []
        for e in earnings:
            etype = (e.get("type") or "").lower()
            if any(pattern in etype for pattern in rsu_patterns):
                rsu_earnings.append(e)

        if not rsu_earnings:
            return None

        # Calculate total RSU gross
        rsu_gross = sum(_get_current_amount(e) for e in rsu_earnings)
        if rsu_gross <= 0:
            return None

        # Find federal withholding from taxes section
        fed_tax_info = taxes.get("federal_income_tax", {})
        fed_withholding = fed_tax_info.get("current_withheld", 0)

        if fed_withholding <= 0:
            return None

        # Calculate total gross for this pay period
        total_gross = sum(_get_current_amount(e) for e in earnings)
        if total_gross <= 0:
            return None

        # Calculate effective rate
        rate = fed_withholding / total_gross

        # Only use if it's in a reasonable range (15-45%)
        if 0.15 <= rate <= 0.45:
            return {
                "rate": rate,
                "record_id": record.get("id"),
                "pay_date": data.get("pay_date"),
                "rsu_gross": rsu_gross,
                "rsu_earnings_types": [e.get("type") for e in rsu_earnings],
                "total_gross": total_gross,
                "fed_withholding": fed_withholding
            }
    except (KeyError, TypeError):
        pass

    return None


def get_ytd_wages_from_records(year: int) -> float:
    """
    Get YTD wages from pay records for Social Security wage base calculation.

    Returns total wages earned in the specified year.
    """
    records_path = get_data_path() / "records" / str(year)
    if not records_path.exists():
        return 0.0

    total_wages = 0.0

    for person_dir in records_path.iterdir():
        if not person_dir.is_dir():
            continue
        for json_file in person_dir.glob("*.json"):
            try:
                with open(json_file) as f:
                    record = json.load(f)
                data = record.get("data", {})
                # Use pay_summary YTD if available (most accurate)
                pay_summary = data.get("pay_summary", {})
                ytd = pay_summary.get("ytd", {})
                if ytd.get("gross"):
                    # Return YTD from most recent record
                    ytd_gross = ytd.get("gross", 0)
                    if ytd_gross > total_wages:
                        total_wages = ytd_gross
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

    return total_wages


def calculate_rsu_taxes(
    gross: float,
    year: int,
    fed_rate: Optional[float] = None,
    fed_rate_source: Optional[str] = None,
    ytd_wages: float = 0.0,
    include_state: bool = False,
    state_rate: float = 0.0
) -> Dict:
    """
    Calculate estimated tax withholding for RSU income.

    Args:
        gross: Gross RSU income
        year: Tax year for rules lookup
        fed_rate: Federal withholding rate (defaults to supplemental rate from tax_rules.yaml)
        fed_rate_source: Description of where fed_rate came from
        ytd_wages: YTD wages for SS wage base calculation
        include_state: Whether to include state tax
        state_rate: State tax rate if include_state is True

    Returns:
        Dict with tax breakdown and net amount
    """
    rules = get_tax_rules(year)

    if fed_rate is None:
        fed_rate = rules["supplemental_rate"]
        fed_rate_source = f"tax_rules.yaml ({year} supplemental_rate)"

    # Federal withholding
    fed_tax = gross * fed_rate

    # Social Security (6.2% up to wage base)
    ss_wage_base = rules["ss_wage_base"]
    ss_rate = rules["ss_rate"]
    ss_remaining_base = max(0, ss_wage_base - ytd_wages)
    ss_taxable = min(gross, ss_remaining_base)
    ss_tax = ss_taxable * ss_rate

    # Medicare (1.45% on all, plus 0.9% additional over threshold)
    medicare_rate = rules["medicare_rate"]
    medicare_additional_rate = rules["medicare_additional_rate"]
    medicare_additional_threshold = rules["medicare_additional_threshold"]

    medicare_tax = gross * medicare_rate
    if ytd_wages + gross > medicare_additional_threshold:
        # Additional Medicare on amount over threshold
        additional_base = max(0, (ytd_wages + gross) - medicare_additional_threshold)
        # But only on this RSU income, not prior income
        additional_base = min(additional_base, gross)
        medicare_tax += additional_base * medicare_additional_rate

    # State tax
    state_tax = gross * state_rate if include_state else 0.0

    total_tax = fed_tax + ss_tax + medicare_tax + state_tax
    net = gross - total_tax

    return {
        "gross": gross,
        "fed_tax": fed_tax,
        "fed_rate": fed_rate,
        "fed_rate_source": fed_rate_source,
        "ss_tax": ss_tax,
        "ss_taxable": ss_taxable,
        "ss_rate": ss_rate,
        "ss_wage_base": ss_wage_base,
        "ss_capped": ss_taxable < gross,
        "medicare_tax": medicare_tax,
        "medicare_rate": medicare_rate,
        "state_tax": state_tax,
        "state_rate": state_rate,
        "total_tax": total_tax,
        "net": net,
        "effective_rate": total_tax / gross if gross > 0 else 0
    }


def find_latest_export() -> Optional[Path]:
    """Find the most recent equity export CSV file."""
    rsus_path = get_rsus_path()
    csv_files = list(rsus_path.glob("EquityAwardsCenter_EquityDetails_*.csv"))
    if not csv_files:
        return None
    # Sort by modification time, newest first
    return max(csv_files, key=lambda p: p.stat().st_mtime)


def count_exports() -> int:
    """Count how many equity export CSV files exist."""
    rsus_path = get_rsus_path()
    return len(list(rsus_path.glob("EquityAwardsCenter_EquityDetails_*.csv")))


def list_exports() -> List[Dict]:
    """List all equity export CSV files in the data/rsus/ folder."""
    rsus_path = get_rsus_path()
    csv_files = list(rsus_path.glob("EquityAwardsCenter_EquityDetails_*.csv"))

    results = []
    for f in sorted(csv_files, key=lambda p: p.stat().st_mtime, reverse=True):
        stat = f.stat()
        results.append({
            "filename": f.name,
            "path": str(f),
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()
        })
    return results


def import_export(source_path: Path) -> Dict:
    """
    Import an equity export CSV into the data/rsus/ folder.

    Args:
        source_path: Path to the CSV file to import

    Returns:
        Dict with import result
    """
    source_path = Path(source_path).expanduser().resolve()

    if not source_path.exists():
        return {"error": f"File not found: {source_path}"}

    if not source_path.name.startswith("EquityAwardsCenter_EquityDetails_"):
        return {
            "error": f"Invalid filename. Expected 'EquityAwardsCenter_EquityDetails_*.csv'",
            "filename": source_path.name
        }

    rsus_path = get_rsus_path()
    dest_path = rsus_path / source_path.name

    # Check if already exists
    if dest_path.exists():
        return {
            "error": "File already exists",
            "dest_path": str(dest_path)
        }

    # Copy file
    shutil.copy2(source_path, dest_path)

    # Parse to validate and get summary
    vests = parse_equity_export(dest_path)
    total_shares = sum(vests.values())
    future_vests = {d: s for d, s in vests.items() if d >= date.today()}
    future_shares = sum(future_vests.values())

    return {
        "imported": True,
        "dest_path": str(dest_path),
        "total_vest_dates": len(vests),
        "future_vest_dates": len(future_vests),
        "future_shares": future_shares
    }


def parse_equity_export(csv_path: Path) -> Dict[date, int]:
    """
    Parse equity export CSV and extract future vesting schedule.

    Args:
        csv_path: Path to the CSV export file

    Returns:
        Dict mapping vest dates to share counts
    """
    vests = defaultdict(int)

    with open(csv_path) as f:
        lines = f.readlines()

    # Parse vesting lines in the RESTRICTED STOCK UNITS section
    in_rsu_section = False
    for line in lines:
        if '*** RESTRICTED STOCK UNITS ***' in line:
            in_rsu_section = True
        elif '*** EQUITY AWARD SHARES ***' in line:
            break  # Stop at historical section

        if in_rsu_section and line.strip().startswith(','):
            # Parse vest date line like: ' ,"12-25-2025","40"'
            parts = line.strip().split(',')
            if len(parts) >= 3:
                date_str = parts[1].strip().strip('"')
                shares_str = parts[2].strip().strip('"')
                try:
                    vest_date = datetime.strptime(date_str, "%m-%d-%Y").date()
                    shares = int(shares_str)
                    vests[vest_date] += shares
                except (ValueError, TypeError):
                    pass

    return dict(vests)


def get_vesting_in_range(
    vests: Dict[date, int],
    start_date: date,
    end_date: date
) -> Dict[date, int]:
    """Filter vests to those within the date range (inclusive)."""
    return {
        d: shares for d, shares in vests.items()
        if start_date <= d <= end_date
    }


def summarize_by_month(vests: Dict[date, int]) -> Dict[Tuple[int, int], int]:
    """
    Aggregate vest shares by (year, month).

    Returns:
        Dict mapping (year, month) tuples to total shares
    """
    by_month = defaultdict(int)
    for vest_date, shares in vests.items():
        key = (vest_date.year, vest_date.month)
        by_month[key] += shares
    return dict(by_month)


def summarize_by_year(vests: Dict[date, int]) -> Dict[int, int]:
    """
    Aggregate vest shares by year.

    Returns:
        Dict mapping year to total shares
    """
    by_year = defaultdict(int)
    for vest_date, shares in vests.items():
        by_year[vest_date.year] += shares
    return dict(by_year)


def analyze_grant_pattern(csv_path: Path) -> Dict:
    """
    Analyze existing grants to determine typical grant timing and vesting pattern.

    Returns:
        Dict with:
        - grant_month: Typical month grants are issued (most common)
        - grant_day: Typical day grants vest on (most common)
        - vest_duration_months: Typical vesting duration (usually 48)
        - last_vest_date: Last vest date in existing data
        - grants: List of individual grant info
    """
    with open(csv_path) as f:
        lines = f.readlines()

    grants = []
    current_grant = None
    in_rsu_section = False
    seen_header = False

    for line in lines:
        if '*** RESTRICTED STOCK UNITS ***' in line:
            in_rsu_section = True
            continue
        elif '*** EQUITY AWARD SHARES ***' in line:
            break

        if not in_rsu_section:
            continue

        # Skip the header line
        if 'Award Date,Symbol' in line:
            seen_header = True
            continue

        # Award line format: "01-05-2022",TICKER,RSU,"$0.00","1,040",...
        # First field is the award date (starts with quote and date pattern)
        if seen_header and line.strip().startswith('"') and not line.strip().startswith('",'):
            parts = line.strip().split(',')
            if len(parts) >= 1:
                date_str = parts[0].strip().strip('"')
                try:
                    award_date = datetime.strptime(date_str, "%m-%d-%Y").date()
                    if current_grant:
                        grants.append(current_grant)
                    current_grant = {
                        "award_date": award_date,
                        "vests": []
                    }
                except ValueError:
                    pass

        # Vest line format: ,"12-25-2025","40" (starts with space then comma)
        elif (line.strip().startswith(',') or line.strip().startswith(' ,')) and current_grant:
            parts = line.strip().split(',')
            if len(parts) >= 3:
                date_str = parts[1].strip().strip('"')
                shares_str = parts[2].strip().strip('"')
                try:
                    vest_date = datetime.strptime(date_str, "%m-%d-%Y").date()
                    shares = int(shares_str)
                    current_grant["vests"].append({"date": vest_date, "shares": shares})
                except (ValueError, TypeError):
                    pass

    if current_grant:
        grants.append(current_grant)

    # Analyze patterns
    if not grants:
        return {"error": "No grants found"}

    # Find most common grant month
    grant_months = [g["award_date"].month for g in grants]
    most_common_month = max(set(grant_months), key=grant_months.count) if grant_months else 1

    # Find most common vest day
    vest_days = []
    for g in grants:
        for v in g["vests"]:
            vest_days.append(v["date"].day)
    most_common_day = max(set(vest_days), key=vest_days.count) if vest_days else 25

    # Find last vest date
    all_vest_dates = []
    for g in grants:
        for v in g["vests"]:
            all_vest_dates.append(v["date"])
    last_vest_date = max(all_vest_dates) if all_vest_dates else None

    # Calculate vesting duration (months from award to last vest in each grant)
    durations = []
    for g in grants:
        if g["vests"]:
            first_vest = min(v["date"] for v in g["vests"])
            last_vest = max(v["date"] for v in g["vests"])
            months = (last_vest.year - first_vest.year) * 12 + (last_vest.month - first_vest.month) + 1
            durations.append(months)

    typical_duration = max(set(durations), key=durations.count) if durations else 48

    return {
        "grant_month": most_common_month,
        "vest_day": most_common_day,
        "vest_duration_months": typical_duration,
        "last_vest_date": last_vest_date,
        "grants": grants
    }


def project_future_grants(
    annual_shares: int,
    start_year: int,
    end_date: date,
    grant_month: int = 1,
    vest_day: int = 25,
    vest_duration_months: int = 48
) -> Dict[date, int]:
    """
    Project future grants vesting over time.

    Simulates new annual grants of the specified size, vesting evenly over
    the vest_duration_months period.

    Args:
        annual_shares: Number of shares granted each year
        start_year: First year to project a grant (typically next full year)
        end_date: Don't project vests beyond this date
        grant_month: Month grants are typically issued
        vest_day: Day of month vests occur
        vest_duration_months: Duration of vesting in months (typically 48)

    Returns:
        Dict mapping vest dates to share counts (from projected grants only)
    """
    projected_vests = defaultdict(int)

    # Calculate shares per vest (evenly distributed)
    shares_per_vest = annual_shares // vest_duration_months
    remainder = annual_shares % vest_duration_months

    current_year = start_year
    while True:
        # Grant date for this year
        grant_date = date(current_year, grant_month, 1)

        # First vest is typically month after grant
        first_vest_month = grant_month + 1 if grant_month < 12 else 1
        first_vest_year = current_year if grant_month < 12 else current_year + 1

        # Generate vest dates
        for i in range(vest_duration_months):
            vest_month = first_vest_month + i
            vest_year = first_vest_year + (vest_month - 1) // 12
            vest_month = ((vest_month - 1) % 12) + 1

            # Determine actual vest day (handle month-end edge cases)
            import calendar
            max_day = calendar.monthrange(vest_year, vest_month)[1]
            actual_vest_day = min(vest_day, max_day)

            vest_date = date(vest_year, vest_month, actual_vest_day)

            # Stop if past end date
            if vest_date > end_date:
                break

            # Add remainder shares to first vests
            extra = 1 if i < remainder else 0
            projected_vests[vest_date] += shares_per_vest + extra

        # Move to next year
        current_year += 1

        # Stop if grant date is past end date (no more grants to project)
        if date(current_year, grant_month, 1) > end_date:
            break

    return dict(projected_vests)


def format_month_summary(
    by_month: Dict[Tuple[int, int], int],
    price: Optional[float] = None,
    taxes: Optional[Dict] = None
) -> str:
    """
    Format monthly vesting summary as a table.

    Args:
        by_month: Dict mapping (year, month) to share counts
        price: Optional stock price for value calculation
        taxes: Optional tax calculation results for net display

    Returns:
        Formatted table string
    """
    lines = []

    if price and taxes:
        lines.append(f"{'Month':<12} {'Shares':>8} {'Gross':>14} {'Net':>14}")
        lines.append("-" * 52)
    elif price:
        lines.append(f"{'Month':<12} {'Shares':>10} {'Est. Value':>15}")
        lines.append("-" * 40)
    else:
        lines.append(f"{'Month':<12} {'Shares':>10}")
        lines.append("-" * 25)

    total_shares = 0
    total_value = 0.0

    for (year, month) in sorted(by_month.keys()):
        shares = by_month[(year, month)]
        month_name = date(year, month, 1).strftime("%Y-%m")
        total_shares += shares

        if price:
            value = shares * price
            total_value += value
            if taxes:
                # Calculate net for this month's shares proportionally
                month_net = value * (taxes["net"] / taxes["gross"]) if taxes["gross"] > 0 else 0
                lines.append(f"{month_name:<12} {shares:>8} ${value:>13,.2f} ${month_net:>13,.2f}")
            else:
                lines.append(f"{month_name:<12} {shares:>10} ${value:>14,.2f}")
        else:
            lines.append(f"{month_name:<12} {shares:>10}")

    if price and taxes:
        lines.append("-" * 52)
        lines.append(f"{'TOTAL':<12} {total_shares:>8} ${taxes['gross']:>13,.2f} ${taxes['net']:>13,.2f}")
    elif price:
        lines.append("-" * 40)
        lines.append(f"{'TOTAL':<12} {total_shares:>10} ${total_value:>14,.2f}")
    else:
        lines.append("-" * 25)
        lines.append(f"{'TOTAL':<12} {total_shares:>10}")

    return "\n".join(lines)


def format_annual_summary(
    by_year: Dict[int, int],
    price: float,
    fed_rate: float,
    fed_rate_source: str,
    ytd_wages: float = 0.0,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    projected_by_year: Optional[Dict[int, int]] = None,
    future_grant_shares: Optional[int] = None,
    future_grant_value: Optional[float] = None
) -> str:
    """
    Format annual vesting summary with tax calculations per year.

    Args:
        by_year: Dict mapping year to share counts (from existing grants)
        price: Stock price for value calculation
        fed_rate: Federal withholding rate
        fed_rate_source: Description of where fed_rate came from
        ytd_wages: YTD wages for first year's SS calculation
        start_date: Start of range (to detect partial years)
        end_date: End of range (to detect partial years)
        projected_by_year: Optional dict mapping year to projected share counts (future grants)
        future_grant_shares: Annual shares assumed for future grants (for display)
        future_grant_value: Annual dollar value for future grants (for display)

    Returns:
        Formatted table string
    """
    lines = []

    has_projected = projected_by_year and any(projected_by_year.values())

    # Get tax rules for flat rate calculation
    sorted_granted_years = sorted(by_year.keys()) if by_year else []
    rules = get_tax_rules(sorted_granted_years[0] if sorted_granted_years else date.today().year)
    fica_rate = rules["ss_rate"] + rules["medicare_rate"]  # 6.2% + 1.45% = 7.65%
    total_rate = fed_rate + fica_rate

    def get_year_label(year: int, all_years: List[int]) -> str:
        """Generate year label with partial year indicator if needed."""
        year_label = str(year)
        if start_date and end_date:
            year_start = date(year, 1, 1)
            year_end = date(year, 12, 31)
            partial_note = ""

            if year == all_years[0] and start_date > year_start:
                partial_note = f"{start_date.strftime('%b')}+"
            if year == all_years[-1] and end_date < year_end:
                if partial_note:
                    partial_note = f"{start_date.strftime('%b')}-{end_date.strftime('%b')}"
                else:
                    partial_note = f"-{end_date.strftime('%b')}"

            if partial_note:
                year_label = f"{year} ({partial_note})"
        return year_label

    # Section 1: Already Granted
    lines.append("ALREADY GRANTED")
    lines.append(f"{'Year':<12} {'Shares':>8} {'Gross':>14} {'Net':>14}")
    lines.append("-" * 52)

    granted_total_shares = 0
    granted_total_gross = 0.0
    granted_total_net = 0.0

    for year in sorted_granted_years:
        shares = by_year[year]
        gross = shares * price
        total_tax = gross * total_rate
        net = gross - total_tax

        granted_total_shares += shares
        granted_total_gross += gross
        granted_total_net += net

        year_label = get_year_label(year, sorted_granted_years)
        lines.append(f"{year_label:<12} {shares:>8} ${gross:>13,.2f} ${net:>13,.2f}")

    lines.append("-" * 52)
    lines.append(f"{'TOTAL':<12} {granted_total_shares:>8} ${granted_total_gross:>13,.2f} ${granted_total_net:>13,.2f}")

    # Section 2: Projected Future Grants (if any)
    if has_projected:
        lines.append("")
        lines.append("PROJECTED FUTURE GRANTS")
        lines.append(f"{'Year':<12} {'Shares':>8} {'Gross':>14} {'Net':>14}")
        lines.append("-" * 52)

        projected_total_shares = 0
        projected_total_gross = 0.0
        projected_total_net = 0.0

        sorted_projected_years = sorted(projected_by_year.keys())
        for year in sorted_projected_years:
            shares = projected_by_year[year]
            if shares == 0:
                continue
            gross = shares * price
            total_tax = gross * total_rate
            net = gross - total_tax

            projected_total_shares += shares
            projected_total_gross += gross
            projected_total_net += net

            year_label = get_year_label(year, sorted_projected_years)
            lines.append(f"{year_label:<12} {shares:>8} ${gross:>13,.2f} ${net:>13,.2f}")

        lines.append("-" * 52)
        lines.append(f"{'TOTAL':<12} {projected_total_shares:>8} ${projected_total_gross:>13,.2f} ${projected_total_net:>13,.2f}")

        # Section 3: Combined Total
        lines.append("")
        lines.append("COMBINED TOTAL")
        combined_shares = granted_total_shares + projected_total_shares
        combined_gross = granted_total_gross + projected_total_gross
        combined_net = granted_total_net + projected_total_net
        lines.append(f"{'Granted':<12} {granted_total_shares:>8} ${granted_total_gross:>13,.2f} ${granted_total_net:>13,.2f}")
        lines.append(f"{'Projected':<12} {projected_total_shares:>8} ${projected_total_gross:>13,.2f} ${projected_total_net:>13,.2f}")
        lines.append("-" * 52)
        lines.append(f"{'TOTAL':<12} {combined_shares:>8} ${combined_gross:>13,.2f} ${combined_net:>13,.2f}")

    lines.append("")
    lines.append(f"Withholding: {fed_rate*100:.0f}% federal + {fica_rate*100:.2f}% FICA = {total_rate*100:.2f}%")
    lines.append(f"Federal source: {fed_rate_source}")

    # Caveats
    lines.append("")
    lines.append("Caveats:")
    lines.append("  * Stock price assumed constant at projection price")
    lines.append("  * Net may be higher if SS wage base ($176k) exceeded")
    lines.append("  * Net may be lower if additional Medicare (0.9% over $200k) applies")
    if has_projected:
        if future_grant_value:
            lines.append(f"  * Projected assumes ${future_grant_value:,.0f}/yr granted annually, vesting over 4 years")
        elif future_grant_shares:
            lines.append(f"  * Projected assumes {future_grant_shares:,} shares granted annually, vesting over 4 years")

    return "\n".join(lines)


def get_vesting_projection(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    price: Optional[float] = None,
    csv_path: Optional[Path] = None,
    calculate_taxes: bool = False,
    annual: bool = False,
    future_grant: Optional[int] = None,
    future_grant_value: Optional[float] = None
) -> Dict:
    """
    Get RSU vesting projection for a date range.

    Args:
        start_date: Start of range (defaults to Jan 1 of current year)
        end_date: End of range (defaults to Dec 31 of current year)
        price: Optional stock price for value estimates
        csv_path: Optional path to CSV (defaults to latest in data/rsus/)
        calculate_taxes: Whether to include tax withholding estimates
        annual: If True, summarize by year instead of month (requires price)
        future_grant: Optional annual shares for projected future grants
        future_grant_value: Optional annual dollar value for future grants (for display)

    Returns:
        Dict with vesting data and formatted summary
    """
    # Default to current year
    today = date.today()
    if start_date is None:
        start_date = date(today.year, 1, 1)
    if end_date is None:
        end_date = date(today.year, 12, 31)

    # Find CSV file
    if csv_path is None:
        csv_path = find_latest_export()

    if csv_path is None or not csv_path.exists():
        return {
            "error": "No RSU export found. Save EquityAwardsCenter CSV export to data/rsus/",
            "rsus_path": str(get_rsus_path())
        }

    # Parse and filter
    all_vests = parse_equity_export(csv_path)
    vests_in_range = get_vesting_in_range(all_vests, start_date, end_date)
    by_month = summarize_by_month(vests_in_range)
    by_year = summarize_by_year(vests_in_range)

    # Handle future grant projections
    projected_by_year = None
    grant_pattern = None
    if future_grant and annual:
        grant_pattern = analyze_grant_pattern(csv_path)
        if "error" not in grant_pattern:
            # Determine projection boundary: don't project beyond existing data
            last_vest = grant_pattern.get("last_vest_date")
            if last_vest:
                # End date is capped at last vest date from existing grants
                effective_end = min(end_date, last_vest)

                # Determine first year to project (year after today, but could be adjusted)
                next_full_year = today.year + 1

                projected_vests = project_future_grants(
                    annual_shares=future_grant,
                    start_year=next_full_year,
                    end_date=effective_end,
                    grant_month=grant_pattern["grant_month"],
                    vest_day=grant_pattern["vest_day"],
                    vest_duration_months=grant_pattern["vest_duration_months"]
                )

                # Filter to range and summarize by year
                projected_in_range = get_vesting_in_range(projected_vests, start_date, effective_end)
                projected_by_year = summarize_by_year(projected_in_range)

    total_shares = sum(vests_in_range.values())
    total_projected_shares = sum(projected_by_year.values()) if projected_by_year else 0
    total_value = total_shares * price if price else None

    # Get fed rate info (needed for annual view or tax calculations)
    rate_info = None
    fed_rate = None
    fed_rate_source = None
    ytd_wages = 0.0

    if price and (calculate_taxes or annual):
        year = start_date.year
        rate_info = find_rsu_tax_rate_from_records()
        if rate_info:
            fed_rate = rate_info["rate"]
            fed_rate_source = f"stub {rate_info['record_id']} ({rate_info['pay_date']})"
        else:
            rules = get_tax_rules(year)
            fed_rate = rules["supplemental_rate"]
            fed_rate_source = f"tax_rules.yaml ({year} supplemental_rate)"

        ytd_wages = get_ytd_wages_from_records(year)

    # Tax calculations if price provided and taxes requested
    taxes = None
    if price and calculate_taxes and not annual:
        taxes = calculate_rsu_taxes(
            gross=total_value,
            year=start_date.year,
            fed_rate=fed_rate,
            fed_rate_source=fed_rate_source,
            ytd_wages=ytd_wages
        )

    # Format output
    if annual and price:
        formatted = format_annual_summary(
            by_year=by_year,
            price=price,
            fed_rate=fed_rate,
            fed_rate_source=fed_rate_source,
            ytd_wages=ytd_wages,
            start_date=start_date,
            end_date=end_date,
            projected_by_year=projected_by_year,
            future_grant_shares=future_grant,
            future_grant_value=future_grant_value
        )
    else:
        formatted = format_month_summary(by_month, price, taxes)

    result = {
        "source_file": csv_path.name,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "total_shares": total_shares,
        "total_value": total_value,
        "price": price,
        "by_month": {f"{y}-{m:02d}": s for (y, m), s in sorted(by_month.items())},
        "by_year": {str(y): s for y, s in sorted(by_year.items())},
        "vests": {d.isoformat(): s for d, s in sorted(vests_in_range.items())},
        "formatted": formatted
    }

    if taxes:
        result["taxes"] = taxes

    if fed_rate_source:
        result["fed_rate"] = fed_rate
        result["fed_rate_source"] = fed_rate_source

    return result
