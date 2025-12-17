#!/usr/bin/env python3
"""
Process a full year of pay stubs from Google Drive.

Downloads multi-period PDF files, splits them into individual pay periods,
processes each one, validates for gaps, and generates a year summary.

Usage:
    python3 process_year.py <year> [--format text|json]

Requirements:
    - gwsa CLI installed and configured with Drive access
    - PyPDF2 and PyYAML (see requirements.txt)
"""

import sys
import os
import json
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
import PyPDF2

# Add parent directory to path for processor imports
sys.path.insert(0, str(Path(__file__).parent))
from processors import get_processor


# Pay Stubs folder ID (shared from corporate account)
PAY_STUBS_FOLDER_ID = "DRIVE_FILE_ID_6"


def run_gwsa_command(args: List[str]) -> dict:
    """Run a gwsa CLI command and return JSON output."""
    cmd = ["gwsa"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gwsa command failed: {result.stderr}")
    return json.loads(result.stdout)


def find_year_folder(year: str) -> Optional[str]:
    """Find the folder ID for a specific year's pay stubs."""
    items = run_gwsa_command(["drive", "list", "--folder-id", PAY_STUBS_FOLDER_ID])

    for item in items.get("items", []):
        if item["type"] == "folder" and item["name"].startswith(year):
            return item["id"]

    # Also check for loose PDFs matching the year in root folder
    return None


def list_pdf_files(folder_id: str) -> List[Dict[str, str]]:
    """List all PDF files in a folder."""
    items = run_gwsa_command(["drive", "list", "--folder-id", folder_id])
    return [
        {"id": item["id"], "name": item["name"]}
        for item in items.get("items", [])
        if item["type"] == "file" and item["name"].lower().endswith(".pdf")
    ]


def download_file(file_id: str, save_path: str) -> dict:
    """Download a file from Drive."""
    return run_gwsa_command(["drive", "download", file_id, save_path])


def split_pdf_pages(pdf_path: str, output_dir: str) -> List[str]:
    """Split a multi-page PDF into individual page files."""
    reader = PyPDF2.PdfReader(pdf_path)
    page_files = []

    base_name = Path(pdf_path).stem

    for i, page in enumerate(reader.pages):
        writer = PyPDF2.PdfWriter()
        writer.add_page(page)

        page_file = os.path.join(output_dir, f"{base_name}_page_{i+1:02d}.pdf")
        with open(page_file, "wb") as f:
            writer.write(f)
        page_files.append(page_file)

    return page_files


def process_single_page(pdf_path: str, employer: str = "Employer A LLC") -> Optional[Dict[str, Any]]:
    """Process a single page PDF and extract pay stub data."""
    processor_class = get_processor("employer_a")

    try:
        stub_data = processor_class.process(pdf_path, employer)
        return stub_data
    except Exception as e:
        # Some pages might be summary pages or non-pay-stub content
        return None


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


def get_sort_key(stub: Dict[str, Any]) -> Tuple:
    """
    Get sort key for a pay stub.

    Sorts by pay date first, then by YTD gross to handle same-date stubs
    (e.g., year-end adjustments should come after regular stubs).
    """
    pay_date = parse_pay_date(stub.get("pay_date", ""))
    ytd_gross = stub.get("pay_summary", {}).get("ytd", {}).get("gross", 0.0)
    return (pay_date, ytd_gross)


def identify_pay_type(stub: Dict[str, Any]) -> str:
    """Identify the type of pay stub (regular, bonus, etc.)."""
    earnings = stub.get("earnings", [])

    # First check for specific bonus/stock types in earnings
    for earning in earnings:
        etype = earning.get("type", "").lower()
        current = earning.get("current_amount", 0)

        if current > 0:
            if "recognition bonus" in etype:
                return "recognition_bonus"
            elif "sales bonus" in etype:
                return "sales_bonus"
            elif "annual bonus" in etype:
                return "annual_bonus"
            elif "special bonus" in etype:
                return "special_bonus"
            elif "stock" in etype or "rsu" in etype:
                return "stock_grant"

    # Check if this is a regular pay stub via earnings
    for earning in earnings:
        if "regular" in earning.get("type", "").lower():
            if earning.get("current_amount", 0) > 0:
                return "regular"

    # Fallback: check pay_summary for regular paycheck pattern
    # If there's significant gross pay (~biweekly salary range) and no bonus detected,
    # it's likely a regular paycheck where earnings extraction was incomplete
    pay_summary = stub.get("pay_summary", {})
    current_gross = pay_summary.get("current", {}).get("gross", 0)

    # Typical biweekly gross is $5k-$15k range for salaried employees
    if 3000 < current_gross < 20000:
        return "regular"

    return "other"


def validate_gaps(stubs: List[Dict[str, Any]], year: str) -> Tuple[List[str], List[str]]:
    """
    Validate for gaps in pay stubs by checking pay date intervals.

    For biweekly pay, expects ~14 days between regular pay stubs.
    YTD continuity isn't used because YTD includes all earnings (bonuses, stock)
    which cause valid jumps between regular pay periods.

    Returns:
        Tuple of (errors, warnings) where errors are fatal issues
        and warnings are informational.
    """
    errors = []
    warnings = []

    # Filter to regular pay stubs only for gap detection
    regular_stubs = [s for s in stubs if s.get("_pay_type") == "regular"]

    if not regular_stubs:
        errors.append("No regular pay stubs found")
        return errors, warnings

    # Check if first stub is actually first of year
    first_stub = regular_stubs[0]
    first_ytd = first_stub.get("pay_summary", {}).get("ytd", {}).get("gross", 0)
    first_current = first_stub.get("pay_summary", {}).get("current", {}).get("gross", 0)
    first_date = first_stub.get("pay_date", "unknown")

    # If YTD equals current, it's likely the first pay period
    if abs(first_ytd - first_current) > 0.01:
        # YTD is higher than current, missing earlier stubs
        errors.append(
            f"First stub ({first_date}) has YTD ${first_ytd:,.2f} but current "
            f"${first_current:,.2f} - missing earlier pay periods"
        )

    # Check if last stub is near end of year
    last_stub = regular_stubs[-1]
    last_date_str = last_stub.get("pay_date", "")
    if last_date_str:
        last_date = parse_pay_date(last_date_str)
        year_end = datetime(int(year), 12, 31)
        days_short = (year_end - last_date).days
        if days_short > 20:  # More than ~2 pay periods short
            warnings.append(
                f"Last stub is {last_date_str}, which is {days_short} days before year end"
            )

    # Check for gaps between consecutive regular stubs using pay date intervals
    # Biweekly pay = ~14 days between stubs; allow up to 21 days before flagging
    MAX_INTERVAL_DAYS = 21
    prev_date = None
    prev_ytd = 0
    employer_segments = []  # Track separate employer segments

    for stub in regular_stubs:
        pay_date_str = stub.get("pay_date", "")
        pay_date = parse_pay_date(pay_date_str)
        ytd_gross = stub.get("pay_summary", {}).get("ytd", {}).get("gross", 0)

        if prev_date and pay_date != datetime.min:
            days_gap = (pay_date - prev_date).days

            # Check for employer change (YTD resets to low value)
            if prev_ytd > 10000 and ytd_gross < prev_ytd * 0.5:
                warnings.append(
                    f"Employer change detected at {pay_date_str}: YTD reset from "
                    f"${prev_ytd:,.2f} to ${ytd_gross:,.2f}"
                )
                # Reset tracking for new employer
                prev_date = pay_date
                prev_ytd = ytd_gross
                continue

            # Check for date gaps (skipped pay periods)
            if days_gap > MAX_INTERVAL_DAYS:
                missed_periods = (days_gap - 7) // 14  # Estimate missed biweekly periods
                errors.append(
                    f"Gap detected: {days_gap} days between {prev_date.strftime('%Y-%m-%d')} "
                    f"and {pay_date_str} (~{missed_periods} missed pay period(s))"
                )

        prev_date = pay_date
        prev_ytd = ytd_gross

    return errors, warnings


def generate_ytd_breakdown(stubs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Generate detailed YTD breakdown from the last stub."""
    if not stubs:
        return {}

    last_stub = stubs[-1]

    # Aggregate earnings by type
    earnings_breakdown = {}
    for earning in last_stub.get("earnings", []):
        etype = earning.get("type", "Unknown")
        ytd = earning.get("ytd_amount", 0)
        if ytd > 0:
            earnings_breakdown[etype] = ytd

    # Aggregate taxes
    taxes_breakdown = {}
    taxes = last_stub.get("taxes", {})
    for tax_name, tax_data in taxes.items():
        ytd_withheld = tax_data.get("ytd_withheld", 0)
        if ytd_withheld > 0:
            # Format tax name nicely
            display_name = tax_name.replace("_", " ").title()
            taxes_breakdown[display_name] = ytd_withheld

    return {
        "earnings": earnings_breakdown,
        "taxes": taxes_breakdown,
        "total_gross": sum(earnings_breakdown.values()),
        "total_taxes": sum(taxes_breakdown.values()),
    }


def generate_summary(stubs: List[Dict[str, Any]], year: str) -> Dict[str, Any]:
    """Generate a summary of the year's pay stubs."""
    if not stubs:
        return {"error": "No pay stubs processed"}

    # Count by type
    type_counts = {}
    for stub in stubs:
        pay_type = stub.get("_pay_type", "unknown")
        type_counts[pay_type] = type_counts.get(pay_type, 0) + 1

    # Get date range
    dates = [parse_pay_date(s.get("pay_date", "")) for s in stubs]
    valid_dates = [d for d in dates if d != datetime.min]

    # Check if first stub appears to be first of year
    # (YTD gross matches current gross within tolerance)
    first_is_first_of_year = False
    regular_stubs = [s for s in stubs if s.get("_pay_type") == "regular"]
    if regular_stubs:
        first_stub = regular_stubs[0]
        first_ytd = first_stub.get("pay_summary", {}).get("ytd", {}).get("gross", 0)
        first_current = first_stub.get("pay_summary", {}).get("current", {}).get("gross", 0)
        if abs(first_ytd - first_current) <= 0.01:
            first_is_first_of_year = True

    # Get final YTD numbers from last stub
    last_stub = stubs[-1]
    final_ytd = last_stub.get("pay_summary", {}).get("ytd", {})

    return {
        "year": year,
        "total_stubs": len(stubs),
        "stubs_by_type": type_counts,
        "first_stub_is_first_of_year": first_is_first_of_year,
        "date_range": {
            "start": min(valid_dates).strftime("%Y-%m-%d") if valid_dates else None,
            "end": max(valid_dates).strftime("%Y-%m-%d") if valid_dates else None,
        },
        "final_ytd": {
            "gross": final_ytd.get("gross", 0),
            "fit_taxable_wages": final_ytd.get("fit_taxable_wages", 0),
            "taxes": final_ytd.get("taxes", 0),
            "net_pay": final_ytd.get("net_pay", 0),
        }
    }


def print_text_report(report: Dict[str, Any]):
    """Print a text format report from the JSON report object."""
    summary = report["summary"]
    errors = report["errors"]
    warnings = report["warnings"]
    ytd_breakdown = report.get("ytd_breakdown")

    print("\n" + "=" * 60)
    print(f"PAY STUB YEAR SUMMARY: {summary['year']}")
    print("=" * 60)

    # Show date range with 1/1 start if first stub is complete
    start_date = summary['date_range']['start']
    end_date = summary['date_range']['end']
    year = summary['year']

    # Check if first stub appears to be first of year (YTD == current)
    first_is_complete = summary.get('first_stub_is_first_of_year', False)
    if first_is_complete:
        display_start = f"{year}-01-01"
        print(f"\nCoverage: {display_start} to {end_date}")
        print(f"  (First stub YTD matches current pay - complete from start of year)")
    else:
        print(f"\nCoverage: {start_date} to {end_date}")
        print(f"  (First stub processed: {start_date})")

    print(f"Total Pay Stubs: {summary['total_stubs']}")

    print("\nBy Type:")
    for pay_type, count in sorted(summary['stubs_by_type'].items()):
        print(f"  {pay_type}: {count}")

    print("\nFinal YTD Totals:")
    ytd = summary['final_ytd']
    print(f"  Gross:              ${ytd['gross']:>12,.2f}")
    print(f"  FIT Taxable Wages:  ${ytd['fit_taxable_wages']:>12,.2f}")
    print(f"  Taxes Withheld:     ${ytd['taxes']:>12,.2f}")
    print(f"  Net Pay:            ${ytd['net_pay']:>12,.2f}")

    # YTD Breakdown (only if no errors / continuity is good)
    if not errors and ytd_breakdown:
        print("\n" + "-" * 60)
        print("YTD EARNINGS BREAKDOWN:")
        earnings = ytd_breakdown.get("earnings", {})
        for etype, amount in sorted(earnings.items(), key=lambda x: -x[1]):
            print(f"  {etype:<25} ${amount:>12,.2f}")
        print(f"  {'─' * 25} {'─' * 13}")
        print(f"  {'Total Gross':<25} ${ytd_breakdown.get('total_gross', 0):>12,.2f}")

        print("\nYTD TAXES WITHHELD:")
        taxes = ytd_breakdown.get("taxes", {})
        for tax_type, amount in sorted(taxes.items(), key=lambda x: -x[1]):
            print(f"  {tax_type:<25} ${amount:>12,.2f}")
        print(f"  {'─' * 25} {'─' * 13}")
        print(f"  {'Total Taxes':<25} ${ytd_breakdown.get('total_taxes', 0):>12,.2f}")

    if errors:
        print("\n" + "-" * 60)
        print("ERRORS (gaps detected):")
        for e in errors:
            print(f"  X {e}")

    if warnings:
        print("\n" + "-" * 60)
        print("WARNINGS:")
        for w in warnings:
            print(f"  ! {w}")

    print("\n" + "-" * 60)
    if errors:
        print("RESULT: GAPS DETECTED in pay stub sequence")
    else:
        print("RESULT: No gaps detected in the date range processed")

    print("=" * 60)


def log(msg: str):
    """Print progress/debug message to stderr."""
    print(msg, file=sys.stderr)


def main():
    if len(sys.argv) < 2:
        log("Usage: python3 process_year.py <year> [--format text|json]")
        log("  year: 4-digit year (e.g., 2025)")
        log("  --format: Output format (default: text)")
        sys.exit(1)

    year = sys.argv[1]
    output_format = "text"

    if "--format" in sys.argv:
        idx = sys.argv.index("--format")
        if idx + 1 < len(sys.argv):
            output_format = sys.argv[idx + 1]

    if not year.isdigit() or len(year) != 4:
        log(f"Error: Invalid year '{year}'. Must be 4 digits.")
        sys.exit(1)

    log(f"Processing pay stubs for {year}...")

    # Find year folder
    year_folder_id = find_year_folder(year)
    if not year_folder_id:
        log(f"Error: No folder found for year {year}")
        sys.exit(1)

    log(f"Found year folder: {year_folder_id}")

    # List PDF files
    pdf_files = list_pdf_files(year_folder_id)
    log(f"Found {len(pdf_files)} PDF files")

    all_stubs = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for pdf_info in pdf_files:
            pdf_name = pdf_info["name"]
            pdf_id = pdf_info["id"]

            log(f"\nProcessing: {pdf_name}")

            # Download PDF
            local_path = os.path.join(tmpdir, pdf_name)
            download_file(pdf_id, local_path)

            # Split into pages
            page_files = split_pdf_pages(local_path, tmpdir)
            log(f"  Split into {len(page_files)} pages")

            # Process each page
            for page_file in page_files:
                stub_data = process_single_page(page_file)
                if stub_data and stub_data.get("pay_date"):
                    stub_data["_pay_type"] = identify_pay_type(stub_data)
                    stub_data["_source_file"] = pdf_name
                    all_stubs.append(stub_data)

            # Clean up downloaded PDF
            os.remove(local_path)

    log(f"\nSuccessfully processed {len(all_stubs)} pay stubs")

    # Sort by date and YTD
    all_stubs.sort(key=get_sort_key)

    # Validate for gaps
    errors, warnings = validate_gaps(all_stubs, year)

    # Build the report object (single source of truth)
    report = {
        "summary": generate_summary(all_stubs, year),
        "errors": errors,
        "warnings": warnings,
        "ytd_breakdown": generate_ytd_breakdown(all_stubs) if not errors else None,
        "stubs": all_stubs
    }

    # Output to stdout
    if output_format == "json":
        print(json.dumps(report, indent=2))
    else:
        print_text_report(report)

    # Save full data to file (always include ytd_breakdown for reference)
    output_file = Path("data") / f"{year}_pay_stubs_full.json"
    output_file.parent.mkdir(exist_ok=True)
    report_with_breakdown = report.copy()
    if report["ytd_breakdown"] is None:
        report_with_breakdown["ytd_breakdown"] = generate_ytd_breakdown(all_stubs)
    with open(output_file, "w") as f:
        json.dump(report_with_breakdown, f, indent=2)
    log(f"\nFull data saved to: {output_file}")

    # Exit with error code if gaps detected
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
