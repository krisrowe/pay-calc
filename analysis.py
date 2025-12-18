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
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
import PyPDF2
import yaml

# Add parent directory to path for processor imports
sys.path.insert(0, str(Path(__file__).parent))
from processors import get_processor
from paycalc.sdk import detect_gaps, check_first_stub_ytd


def sum_deductions(deductions: Any, current: bool = True) -> float:
    """Sum all deduction amounts, handling both list and dict formats.

    Args:
        deductions: Either a list of deduction dicts or a dict of named deductions
        current: If True, sum current period amounts; if False, sum YTD amounts

    Returns:
        Total deduction amount
    """
    total = 0.0

    if isinstance(deductions, dict):
        # Dict format: {"retirement_401k": {"current": 100, "ytd": 500}, ...}
        for ded_vals in deductions.values():
            if isinstance(ded_vals, dict):
                amount_key = 'current' if current else 'ytd'
                amount = ded_vals.get(amount_key) or ded_vals.get(f'{amount_key}_amount') or 0
                total += abs(amount)
            elif isinstance(ded_vals, (int, float)):
                total += abs(ded_vals)
    elif isinstance(deductions, list):
        # List format: [{"type": "401k", "current_amount": 100}, ...]
        for ded in deductions:
            if isinstance(ded, dict):
                amount_key = 'current_amount' if current else 'ytd_amount'
                amount = ded.get(amount_key) or ded.get('amount') or 0
                total += abs(amount)

    return total


def extract_401k_from_deductions(deductions: Any, current: bool = False) -> Dict[str, float]:
    """Extract 401k amounts from deductions, handling both list and dict formats.

    Args:
        deductions: Either a list of deduction dicts or a dict of named deductions
        current: If True, extract current period amounts; if False, extract YTD amounts

    Returns:
        Dict with keys 'employee_pretax', 'employee_aftertax', 'employer_match'
    """
    result = {'employee_pretax': 0.0, 'employee_aftertax': 0.0, 'employer_match': 0.0}

    if isinstance(deductions, dict):
        # Dict format from OCR: {"retirement_401k": {"current": 100, "ytd": 500}, ...}
        for ded_name, ded_vals in deductions.items():
            if not isinstance(ded_vals, dict):
                continue
            ded_name_lower = ded_name.lower()
            if '401k' in ded_name_lower or 'retirement' in ded_name_lower:
                amount_key = 'current' if current else 'ytd'
                amount = ded_vals.get(amount_key) or ded_vals.get(f'{amount_key}_amount') or 0
                # Assume dict-format 401k is pretax unless named otherwise
                if 'after' in ded_name_lower or 'roth' in ded_name_lower:
                    result['employee_aftertax'] += amount
                else:
                    result['employee_pretax'] += amount
                # Check for employer match
                match_key = 'employer_match' if current else 'employer_match_ytd'
                result['employer_match'] += ded_vals.get(match_key, 0)
    elif isinstance(deductions, list):
        # List format: [{"type": "k pretax", "current_amount": 100, "ytd_amount": 500}, ...]
        for ded in deductions:
            if not isinstance(ded, dict):
                continue
            ded_type = ded.get("type", "").lower()
            if "k pretax" in ded_type or "401k" in ded_type:
                amount_key = 'current_amount' if current else 'ytd_amount'
                result['employee_pretax'] += ded.get(amount_key, 0)
                match_key = 'employer_match' if current else 'employer_match_ytd'
                result['employer_match'] += ded.get(match_key, 0)
            elif "k at" in ded_type or "after" in ded_type:
                amount_key = 'current_amount' if current else 'ytd_amount'
                result['employee_aftertax'] += ded.get(amount_key, 0)

    return result


def validate_stub_numbers(stub: Dict[str, Any]) -> List[str]:
    """Validate that pay stub numbers are consistent and add up correctly.

    Returns list of validation errors (empty if valid).
    """
    errors = []

    # Get values from stub structure
    gross = 0.0
    net_pay = stub.get("net_pay", 0) or 0
    federal_tax = 0.0
    ss_tax = 0.0
    medicare_tax = 0.0
    total_deductions = 0.0

    # Extract gross from pay_summary or earnings
    if "pay_summary" in stub and "current" in stub["pay_summary"]:
        gross = stub["pay_summary"]["current"].get("gross", 0) or 0
    elif "earnings" in stub:
        for earning in stub.get("earnings", []):
            if earning.get("type", "").lower() in ["gross pay", "gross"]:
                gross = earning.get("current_amount", 0) or 0
                break

    # Extract taxes
    if "taxes" in stub:
        taxes = stub["taxes"]
        if "federal_income" in taxes:
            federal_tax = taxes["federal_income"].get("current", 0) or 0
        if "social_security" in taxes:
            ss_tax = taxes["social_security"].get("current", 0) or 0
        if "medicare" in taxes:
            medicare_tax = taxes["medicare"].get("current", 0) or 0

    # Sum deductions (handles both list and dict formats)
    total_deductions = sum_deductions(stub.get("deductions", []), current=True)

    # Validate required fields have values
    if not gross or gross <= 0:
        errors.append("Missing or invalid gross pay")
    if not net_pay or net_pay <= 0:
        errors.append("Missing or invalid net pay")

    # Validate net pay roughly equals gross - taxes - deductions
    # Allow for some tolerance due to rounding
    if gross > 0 and net_pay > 0:
        total_taxes = federal_tax + ss_tax + medicare_tax
        expected_net = gross - total_taxes - total_deductions
        tolerance = max(gross * 0.05, 10)  # 5% or $10, whichever is larger

        if abs(net_pay - expected_net) > tolerance:
            errors.append(
                f"Net pay ${net_pay:,.2f} doesn't match expected ${expected_net:,.2f} "
                f"(gross ${gross:,.2f} - taxes ${total_taxes:,.2f} - deductions ${total_deductions:,.2f})"
            )

    # Validate net < gross
    if net_pay > gross and gross > 0:
        errors.append(f"Net pay ${net_pay:,.2f} exceeds gross pay ${gross:,.2f}")

    return errors


def extract_with_gemini_ocr(pdf_path: str) -> Optional[Dict[str, Any]]:
    """Extract pay stub data from image-based PDF using Gemini CLI.

    Falls back to Gemini CLI when PyPDF2 cannot extract text (image-based PDFs).
    Requires gemini CLI to be installed and configured.

    Returns stub data dict or None if extraction fails.
    """
    import shutil

    # Check if gemini CLI is available
    if not shutil.which("gemini"):
        print("  Warning: gemini CLI not found, cannot OCR image-based PDF")
        return None

    # Copy to /tmp to avoid gitignore issues with cache dirs
    tmp_path = f"/tmp/paycalc_ocr_{Path(pdf_path).name}"
    shutil.copy(pdf_path, tmp_path)

    # Prompt asks Gemini to analyze the PDF image and return structured data
    prompt = f'''Analyze the pay stub document at {tmp_path} and extract the financial data.

Return ONLY a valid JSON object (no markdown code blocks, no explanation) with these exact fields:
{{
  "pay_date": "YYYY-MM-DD",
  "period": {{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}},
  "employer": "company name",
  "gross_pay": 0.00,
  "net_pay": 0.00,
  "federal_income_tax_withheld": 0.00,
  "social_security_withheld": 0.00,
  "medicare_withheld": 0.00,
  "state_tax_withheld": 0.00,
  "ytd_gross": 0.00,
  "ytd_federal_withheld": 0.00,
  "ytd_social_security": 0.00,
  "ytd_medicare": 0.00
}}

All number fields must be numeric values (not strings). Dates must be in YYYY-MM-DD format.'''

    try:
        result = subprocess.run(
            ["gemini", "--allowed-mcp-server-names", "none", "--include-directories", "/tmp", "-o", "text", prompt],
            capture_output=True,
            text=True,
            timeout=120
        )

        output = result.stdout.strip()

        # Debug: show raw output if it doesn't look like JSON
        if not output.startswith("{") and not "```" in output:
            print(f"  Debug: Gemini raw output: {output[:200]}...")

        # Extract JSON from response (may have markdown backticks)
        if "```json" in output:
            output = output.split("```json")[1].split("```")[0].strip()
        elif "```" in output:
            output = output.split("```")[1].split("```")[0].strip()

        # Try to find JSON object in output
        if not output.startswith("{"):
            start = output.find("{")
            end = output.rfind("}") + 1
            if start >= 0 and end > start:
                output = output[start:end]

        data = json.loads(output)

        # Convert to internal format expected by analysis
        stub = {
            "file_name": Path(pdf_path).name,
            "employer": data.get("employer", "Unknown"),
            "pay_date": data.get("pay_date"),
            "period": data.get("period", {}),
            "document_id": "ocr",
            "net_pay": data.get("net_pay", 0),
            "earnings": [
                {"type": "Gross Pay", "current_amount": data.get("gross_pay", 0), "ytd_amount": data.get("ytd_gross", 0)}
            ],
            "taxes": {
                "federal_income": {"current": data.get("federal_income_tax_withheld", 0), "ytd": data.get("ytd_federal_withheld", 0)},
                "social_security": {"current": data.get("social_security_withheld", 0), "ytd": data.get("ytd_social_security", 0)},
                "medicare": {"current": data.get("medicare_withheld", 0), "ytd": data.get("ytd_medicare", 0)},
                "state": {"current": data.get("state_tax_withheld", 0), "ytd": 0}
            },
            "deductions": [],
            "pay_summary": {
                "current": {"gross": data.get("gross_pay", 0), "taxes": data.get("federal_income_tax_withheld", 0), "net_pay": data.get("net_pay", 0)},
                "ytd": {"gross": data.get("ytd_gross", 0), "taxes": data.get("ytd_federal_withheld", 0)}
            },
            "_pay_type": "other",
            "_source_file": Path(pdf_path).name,
            "_ocr": True
        }

        # Validate the extracted numbers
        validation_errors = validate_stub_numbers(stub)
        if validation_errors:
            print(f"  Warning: OCR validation issues for {Path(pdf_path).name}:")
            for err in validation_errors:
                print(f"    - {err}")

        # Clean up temp file
        Path(tmp_path).unlink(missing_ok=True)

        return stub

    except subprocess.TimeoutExpired:
        print(f"  Warning: Gemini OCR timed out for {Path(pdf_path).name}")
        return None
    except json.JSONDecodeError as e:
        print(f"  Warning: Could not parse Gemini OCR output as JSON: {e}")
        if 'output' in locals():
            print(f"  Debug: Output was: {output[:500]}...")
        return None
    except Exception as e:
        print(f"  Warning: Gemini OCR failed: {e}")
        return None
    finally:
        # Clean up temp file
        Path(tmp_path).unlink(missing_ok=True)


def load_config() -> dict:
    """Load configuration from profile.yaml via SDK."""
    from paycalc.sdk import load_config as sdk_load_config
    return sdk_load_config(require_exists=True)


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


def get_pay_stubs_folder_id() -> str:
    """Get the Pay Stubs folder ID from config."""
    config = load_config()
    return config.get("drive", {}).get("pay_stubs_folder_id", "")


def run_gwsa_command(args: List[str]) -> dict:
    """Run a gwsa CLI command and return JSON output."""
    cmd = ["gwsa"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gwsa command failed: {result.stderr}")
    return json.loads(result.stdout)


def find_year_folder(year: str) -> Optional[str]:
    """Find the folder ID for a specific year's pay stubs."""
    folder_id = get_pay_stubs_folder_id()
    if not folder_id:
        raise RuntimeError("pay_stubs_folder_id not configured in profile.yaml")
    items = run_gwsa_command(["drive", "list", "--folder-id", folder_id])

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


def get_party_processor_and_employer(party: str, pdf_text: str = "") -> Tuple[str, str]:
    """Get processor name and employer for a party based on config.

    If pdf_text is provided, tries to match keywords to identify specific employer.
    Falls back to first company for the party if no match.
    """
    config = load_config()
    parties = config.get("parties", {})
    party_config = parties.get(party, {})
    companies = party_config.get("companies", [])

    if not companies:
        # Fallback defaults
        return "employer_a", "Employer A LLC"

    # Try to match keywords in PDF text
    if pdf_text:
        normalized_text = pdf_text.lower().replace(" ", "")
        for company in companies:
            keywords = company.get("keywords", [])
            for keyword in keywords:
                if keyword.lower().replace(" ", "") in normalized_text:
                    return company.get("paystub_processor", "generic"), company.get("name", "Unknown")

    # Default to first company for party
    first_company = companies[0]
    return first_company.get("paystub_processor", "generic"), first_company.get("name", "Unknown")


def process_single_page(pdf_path: str, party: str, employer: str = None) -> Optional[Dict[str, Any]]:
    """Process a single page PDF and extract pay stub data."""
    # Read PDF text to identify employer
    pdf_text = ""
    try:
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                pdf_text += page.extract_text() or ""
    except:
        pass

    # Check for image-based PDF (no extractable text)
    if not pdf_text.strip():
        print(f"  Image-based PDF detected, attempting OCR: {Path(pdf_path).name}")
        return extract_with_gemini_ocr(pdf_path)

    processor_name, detected_employer = get_party_processor_and_employer(party, pdf_text)
    if employer is None:
        employer = detected_employer

    processor_class = get_processor(processor_name)

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




def detect_employer_segments(stubs: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """
    Split stubs into segments by employer based on YTD resets.

    Returns list of stub lists, one per employer segment.
    """
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


def validate_segment_totals(segment: List[Dict[str, Any]], segment_name: str) -> Tuple[List[str], List[str], Dict[str, Any]]:
    """Validate totals for a single employer segment."""
    errors = []
    warnings = []

    if not segment:
        return errors, warnings, {}

    # Get date range for this segment
    first_date = segment[0].get("pay_date", "unknown")
    last_date = segment[-1].get("pay_date", "unknown")

    # Initialize accumulators
    sum_gross = 0.0
    sum_fit_taxable = 0.0
    sum_taxes = 0.0
    sum_net = 0.0
    sum_deductions = 0.0
    sum_fed_withheld = 0.0
    sum_ss_withheld = 0.0
    sum_medicare_withheld = 0.0
    sum_401k_employee = 0.0

    for stub in segment:
        pay_summary = stub.get("pay_summary", {}).get("current", {})
        sum_gross += pay_summary.get("gross", 0)
        sum_fit_taxable += pay_summary.get("fit_taxable_wages", 0)
        sum_taxes += pay_summary.get("taxes", 0)
        sum_net += pay_summary.get("net_pay", 0)
        sum_deductions += pay_summary.get("deductions", 0)

        taxes = stub.get("taxes", {})
        sum_fed_withheld += taxes.get("federal_income_tax", {}).get("current_withheld", 0)
        sum_ss_withheld += taxes.get("social_security", {}).get("current_withheld", 0)
        sum_medicare_withheld += taxes.get("medicare", {}).get("current_withheld", 0)

        # Extract 401k from deductions (handles both list and dict formats)
        k401 = extract_401k_from_deductions(stub.get("deductions", []), current=True)
        sum_401k_employee += k401['employee_pretax'] + k401['employee_aftertax']

    # Get final YTD values from last stub in segment
    last_stub = segment[-1]
    final_ytd = last_stub.get("pay_summary", {}).get("ytd", {})
    final_taxes = last_stub.get("taxes", {})

    ytd_gross = final_ytd.get("gross", 0)
    ytd_fit_taxable = final_ytd.get("fit_taxable_wages", 0)
    ytd_taxes = final_ytd.get("taxes", 0)
    ytd_net = final_ytd.get("net_pay", 0)
    ytd_deductions = final_ytd.get("deductions", 0)
    ytd_fed_withheld = final_taxes.get("federal_income_tax", {}).get("ytd_withheld", 0)
    ytd_ss_withheld = final_taxes.get("social_security", {}).get("ytd_withheld", 0)
    ytd_medicare_withheld = final_taxes.get("medicare", {}).get("ytd_withheld", 0)

    # Extract YTD 401k from last stub (handles both list and dict formats)
    ytd_k401 = extract_401k_from_deductions(last_stub.get("deductions", []), current=False)
    ytd_401k_employee = ytd_k401['employee_pretax'] + ytd_k401['employee_aftertax']
    ytd_401k_employer = ytd_k401['employer_match']

    # Build comparison dict with metadata
    totals = {
        "segment": segment_name,
        "stub_count": len(segment),
        "date_range": {"start": first_date, "end": last_date},
        "fields": {
            "gross": {"sum": sum_gross, "ytd": ytd_gross, "diff": sum_gross - ytd_gross},
            "fit_taxable_wages": {"sum": sum_fit_taxable, "ytd": ytd_fit_taxable, "diff": sum_fit_taxable - ytd_fit_taxable},
            "taxes": {"sum": sum_taxes, "ytd": ytd_taxes, "diff": sum_taxes - ytd_taxes},
            "net_pay": {"sum": sum_net, "ytd": ytd_net, "diff": sum_net - ytd_net},
            "deductions": {"sum": sum_deductions, "ytd": ytd_deductions, "diff": sum_deductions - ytd_deductions},
            "federal_withheld": {"sum": sum_fed_withheld, "ytd": ytd_fed_withheld, "diff": sum_fed_withheld - ytd_fed_withheld},
            "social_security": {"sum": sum_ss_withheld, "ytd": ytd_ss_withheld, "diff": sum_ss_withheld - ytd_ss_withheld},
            "medicare": {"sum": sum_medicare_withheld, "ytd": ytd_medicare_withheld, "diff": sum_medicare_withheld - ytd_medicare_withheld},
            "401k_employee": {"sum": sum_401k_employee, "ytd": ytd_401k_employee, "diff": sum_401k_employee - ytd_401k_employee},
        },
        "401k_summary": {
            "employee": ytd_401k_employee,
            "employer_match": ytd_401k_employer,
            "total": ytd_401k_employee + ytd_401k_employer,
        }
    }

    # Check for discrepancies - produce warnings (not errors) since per-stub
    # delta validation is the primary check. Aggregate discrepancies can be
    # symptoms of known-warning fields (Prize/Gift, Tax Gross-Up, etc.)
    TOLERANCE = 1.00
    for field, values in totals["fields"].items():
        diff = abs(values["diff"])
        if diff > TOLERANCE:
            warnings.append(
                f"[{segment_name}] {field}: sum=${values['sum']:,.2f}, "
                f"YTD=${values['ytd']:,.2f}, diff=${values['diff']:+,.2f}"
            )

    return errors, warnings, totals


def normalize_field_name(field: str) -> str:
    """
    Normalize a field name for consistent matching.

    Handles variations like "Prize/ Gift" vs "Prize/Gift" by removing
    spaces around slashes and collapsing multiple spaces.
    """
    import re
    # Remove spaces around slashes
    normalized = re.sub(r'\s*/\s*', '/', field)
    # Collapse multiple spaces
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized.strip().lower()


def get_warning_fields() -> Dict[str, str]:
    """
    Get fields configured to warn (not error) on current vs YTD mismatch.

    Returns dict mapping normalized field name to description message.
    """
    config = load_config()
    warning_fields = {}
    for entry in config.get("validation", {}).get("allow_current_mismatch", []):
        field = normalize_field_name(entry.get("field", ""))
        message = entry.get("message", "")
        if field:
            warning_fields[field] = message
    return warning_fields


def validate_stub_deltas(stubs: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """
    Validate that displayed current values match actual YTD increases.

    For each stub (after the first), compares the displayed "current" amount
    to the actual YTD increase (this_ytd - previous_ytd) for each earnings field.

    Fields in the config's allow_current_mismatch list generate warnings.
    All other fields with mismatches generate errors.

    Skips validation at employer boundaries (YTD resets) since deltas
    don't make sense across different employers.

    Returns:
        Tuple of (errors, warnings)
    """
    errors = []
    warnings = []

    if len(stubs) < 2:
        return errors, warnings

    warning_fields = get_warning_fields()
    TOLERANCE = 0.01

    prev_earnings = {}  # field -> ytd_amount
    prev_ytd_gross = 0.0

    for i, stub in enumerate(stubs):
        pay_date = stub.get("pay_date", "unknown")
        ytd_gross = stub.get("pay_summary", {}).get("ytd", {}).get("gross", 0)

        # Build current earnings lookup
        curr_earnings = {}
        for earning in stub.get("earnings", []):
            field = earning.get("type", "")
            curr_earnings[field] = {
                "current": earning.get("current_amount", 0),
                "ytd": earning.get("ytd_amount", 0)
            }

        # Skip first stub - no previous to compare
        if i == 0:
            prev_earnings = {k: v["ytd"] for k, v in curr_earnings.items()}
            prev_ytd_gross = ytd_gross
            continue

        # Detect employer change (YTD reset) - skip delta validation
        if prev_ytd_gross > 10000 and ytd_gross < prev_ytd_gross * 0.5:
            # Reset for new employer segment
            prev_earnings = {k: v["ytd"] for k, v in curr_earnings.items()}
            prev_ytd_gross = ytd_gross
            continue

        # Compare each field
        for field, values in curr_earnings.items():
            displayed_current = values["current"]
            current_ytd = values["ytd"]
            prev_ytd = prev_earnings.get(field, 0)
            actual_increase = current_ytd - prev_ytd

            diff = abs(displayed_current - actual_increase)

            if diff > TOLERANCE:
                field_normalized = normalize_field_name(field)
                if field_normalized in warning_fields:
                    # This field is configured as warning
                    warnings.append(
                        f"{pay_date} {field} - displayed (current) ${displayed_current:,.2f} "
                        f"vs actual (YTD increase) ${actual_increase:,.2f}"
                    )
                else:
                    # Unconfigured field - this is an error
                    errors.append(
                        f"{pay_date} {field} - displayed (current) ${displayed_current:,.2f} "
                        f"vs actual (YTD increase) ${actual_increase:,.2f}"
                    )

        # Update previous for next iteration
        prev_earnings = {k: v["ytd"] for k, v in curr_earnings.items()}
        prev_ytd_gross = ytd_gross

    return errors, warnings


def validate_year_totals(stubs: List[Dict[str, Any]]) -> Tuple[List[str], List[str], Dict[str, Any]]:
    """
    Validate that sum of current amounts equals final YTD totals.

    Handles multiple employer segments (mid-year employer changes) by
    validating each segment separately.

    Returns:
        Tuple of (errors, warnings, validation_results) where validation_results
        contains segment-by-segment comparisons.
    """
    errors = []
    warnings = []

    if not stubs:
        return errors, warnings, {}

    # Detect employer segments
    segments = detect_employer_segments(stubs)

    validation_results = {
        "total_stubs": len(stubs),
        "employer_segments": len(segments),
        "segments": []
    }

    for i, segment in enumerate(segments):
        segment_name = f"Employer {i + 1}" if len(segments) > 1 else "Full Year"
        seg_errors, seg_warnings, seg_totals = validate_segment_totals(segment, segment_name)
        errors.extend(seg_errors)
        warnings.extend(seg_warnings)
        validation_results["segments"].append(seg_totals)

    return errors, warnings, validation_results


def generate_401k_contributions(stubs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Generate 401k contributions from YTD values of final stubs.

    Uses YTD values from the last stub of each employer segment to get
    accurate totals regardless of how many intermediate stubs are available.

    Tracks:
    - Pre-tax employee contributions (traditional 401k)
    - After-tax employee contributions (mega backdoor Roth)
    - Employer match

    Also tracks monthly breakdown from available stubs (may be incomplete
    if intermediate stubs are missing).

    Combines across all employer segments.
    """
    if not stubs:
        return {}

    from collections import defaultdict

    # Get employer segments and extract YTD from final stub of each
    segments = detect_employer_segments(stubs)

    # Yearly totals from final stub YTD (source of truth)
    yearly_totals = {"pretax": 0.0, "aftertax": 0.0, "employer": 0.0, "total": 0.0}

    for segment in segments:
        if not segment:
            continue
        last_stub = segment[-1]

        # Extract 401k from deductions (handles both list and dict formats)
        k401 = extract_401k_from_deductions(last_stub.get("deductions", []), current=False)
        yearly_totals["pretax"] += k401['employee_pretax']
        yearly_totals["aftertax"] += k401['employee_aftertax']
        yearly_totals["employer"] += k401['employer_match']

    yearly_totals["total"] = (
        yearly_totals["pretax"] +
        yearly_totals["aftertax"] +
        yearly_totals["employer"]
    )

    # Monthly breakdown from available stubs (informational, may be incomplete)
    monthly = defaultdict(lambda: {"pretax": 0.0, "aftertax": 0.0, "employer": 0.0})
    prev_employer_ytd = 0.0

    for stub in stubs:
        pay_date = stub.get("pay_date", "")
        if not pay_date:
            continue

        month = int(pay_date[5:7])

        # Extract current 401k amounts (handles both list and dict formats)
        k401 = extract_401k_from_deductions(stub.get("deductions", []), current=True)
        monthly[month]["pretax"] += k401['employee_pretax']
        monthly[month]["aftertax"] += k401['employee_aftertax']

        # Track employer match delta from YTD changes (list format only has this detail)
        k401_ytd = extract_401k_from_deductions(stub.get("deductions", []), current=False)
        employer_ytd = k401_ytd['employer_match']
        if employer_ytd > prev_employer_ytd:
            delta = employer_ytd - prev_employer_ytd
            monthly[month]["employer"] += delta
            prev_employer_ytd = employer_ytd

    months_data = {}
    for month in range(1, 13):
        m = monthly[month]
        total = m["pretax"] + m["aftertax"] + m["employer"]
        months_data[month] = {
            "pretax": m["pretax"],
            "aftertax": m["aftertax"],
            "employer": m["employer"],
            "total": total
        }

    return {
        "by_month": months_data,
        "yearly_totals": yearly_totals
    }


def generate_imputed_income_summary(stubs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Generate imputed income summary from final YTD values.

    Imputed income includes:
    - Prize/Gift: Non-cash bonus expenses (GPS Club awards, etc.)
    - Ben in Kind Grs: Benefits in kind (meals, gym, transit, etc.)
    - Tax Gross-Up: Covers taxes so employee receives full value

    All amounts are added to gross income for W-2 purposes.
    Combines across all employer segments.
    """
    if not stubs:
        return {}

    # Combine across all employer segments
    segments = detect_employer_segments(stubs)
    prize_gift = 0.0
    ben_in_kind = 0.0
    tax_gross_up = 0.0

    for segment in segments:
        if not segment:
            continue
        last_stub = segment[-1]

        for earning in last_stub.get("earnings", []):
            etype = earning.get("type", "").lower()
            ytd = earning.get("ytd_amount", 0)
            if "prize" in etype and "gift" in etype:
                prize_gift += ytd
            elif "ben in kind" in etype or "benefit" in etype:
                ben_in_kind += ytd
            elif "tax gross" in etype or "gross-up" in etype or "grossup" in etype:
                tax_gross_up += ytd

    if prize_gift == 0 and ben_in_kind == 0 and tax_gross_up == 0:
        return {}

    return {
        "prize_expenses": prize_gift,
        "benefits_in_kind": ben_in_kind,
        "tax_gross_up": tax_gross_up,
        "total_imputed": prize_gift + ben_in_kind + tax_gross_up
    }


def normalize_earnings_type(etype: str) -> str:
    """Normalize earnings type names for consistent aggregation."""
    import re
    # Remove extra spaces around slashes and hyphens
    normalized = re.sub(r'\s*/\s*', '/', etype)
    normalized = re.sub(r'\s*-\s*', '-', normalized)
    # Collapse multiple spaces
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized.strip()


def generate_ytd_breakdown(stubs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Generate detailed YTD breakdown combining all employer segments."""
    if not stubs:
        return {}

    # Get all employer segments (YTD resets at employer changes)
    segments = detect_employer_segments(stubs)

    # Aggregate earnings and taxes across all segments
    # Use normalized keys to combine variants like "Tax Gross- Up" and "Tax Gross-Up"
    earnings_breakdown = {}  # normalized_key -> {"display": original_name, "amount": total}
    taxes_breakdown = {}
    employee_pretax_401k = 0.0
    employee_aftertax_401k = 0.0
    employer_401k_total = 0.0

    for segment in segments:
        if not segment:
            continue
        last_stub = segment[-1]

        # Add earnings from this segment's final YTD
        for earning in last_stub.get("earnings", []):
            etype = earning.get("type", "Unknown")
            ytd = earning.get("ytd_amount", 0)
            if ytd > 0:
                key = normalize_earnings_type(etype).lower()
                if key in earnings_breakdown:
                    earnings_breakdown[key]["amount"] += ytd
                else:
                    earnings_breakdown[key] = {"display": normalize_earnings_type(etype), "amount": ytd}

        # Add all 401k contributions (handles both list and dict formats)
        k401 = extract_401k_from_deductions(last_stub.get("deductions", []), current=False)
        employee_pretax_401k += k401['employee_pretax']
        employee_aftertax_401k += k401['employee_aftertax']
        employer_401k_total += k401['employer_match']

        # Add taxes from this segment's final YTD
        taxes = last_stub.get("taxes", {})
        for tax_name, tax_data in taxes.items():
            ytd_withheld = tax_data.get("ytd_withheld", 0)
            if ytd_withheld > 0:
                display_name = tax_name.replace("_", " ").title()
                taxes_breakdown[display_name] = taxes_breakdown.get(display_name, 0) + ytd_withheld

    # Convert earnings to simple dict for output
    earnings_output = {v["display"]: v["amount"] for v in earnings_breakdown.values()}

    # Add all 401k contributions
    if employee_pretax_401k > 0:
        earnings_output["401k Pre-Tax"] = employee_pretax_401k
    if employee_aftertax_401k > 0:
        earnings_output["401k After-Tax"] = employee_aftertax_401k
    if employer_401k_total > 0:
        earnings_output["401k Employer Match"] = employer_401k_total

    total_compensation = sum(earnings_output.values())

    return {
        "earnings": earnings_output,
        "taxes": taxes_breakdown,
        "total_gross": total_compensation,
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

    # Calculate combined YTD across all employer segments
    # (YTD resets when employer changes, so we sum the final YTD from each segment)
    segments = detect_employer_segments(stubs)
    combined_ytd = {
        "gross": 0.0,
        "fit_taxable_wages": 0.0,
        "taxes": 0.0,
        "net_pay": 0.0,
    }
    for segment in segments:
        if segment:
            last_seg_stub = segment[-1]
            seg_ytd = last_seg_stub.get("pay_summary", {}).get("ytd", {})
            combined_ytd["gross"] += seg_ytd.get("gross", 0)
            combined_ytd["fit_taxable_wages"] += seg_ytd.get("fit_taxable_wages", 0)
            combined_ytd["taxes"] += seg_ytd.get("taxes", 0)
            combined_ytd["net_pay"] += seg_ytd.get("net_pay", 0)

    return {
        "year": year,
        "total_stubs": len(stubs),
        "stubs_by_type": type_counts,
        "first_stub_is_first_of_year": first_is_first_of_year,
        "employer_segments": len(segments),
        "date_range": {
            "start": min(valid_dates).strftime("%Y-%m-%d") if valid_dates else None,
            "end": max(valid_dates).strftime("%Y-%m-%d") if valid_dates else None,
        },
        "final_ytd": combined_ytd
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
    contrib_401k = report.get("contributions_401k", {}).get("yearly_totals", {})
    employee_401k = contrib_401k.get("pretax", 0) + contrib_401k.get("aftertax", 0)
    employer_401k = contrib_401k.get("employer", 0)
    total_401k = contrib_401k.get("total", 0)
    total_comp = ytd['gross'] + total_401k
    print(f"  Gross Pay:          ${ytd['gross']:>12,.2f}")
    print(f"  + Employee 401k:    ${employee_401k:>12,.2f}")
    print(f"  + Employer 401k:    ${employer_401k:>12,.2f}")
    print(f"  {'─' * 18} {'─' * 13}")
    print(f"  Total Compensation: ${total_comp:>12,.2f}")
    print()
    print(f"  FIT Taxable Wages:  ${ytd['fit_taxable_wages']:>12,.2f}")
    print(f"  Taxes Withheld:     ${ytd['taxes']:>12,.2f}")
    print(f"  Net Pay:            ${ytd['net_pay']:>12,.2f}")

    # YTD Breakdown (always show if available - YTD comes from final stubs, not dependent on continuity)
    if ytd_breakdown:
        print("\n" + "-" * 60)
        print("YTD EARNINGS BREAKDOWN:")
        earnings = ytd_breakdown.get("earnings", {})
        for etype, amount in sorted(earnings.items(), key=lambda x: -x[1]):
            print(f"  {etype:<25} ${amount:>12,.2f}")
        print(f"  {'─' * 25} {'─' * 13}")
        print(f"  {'Total Compensation':<25} ${ytd_breakdown.get('total_gross', 0):>12,.2f}")

        print("\nYTD TAXES WITHHELD:")
        taxes = ytd_breakdown.get("taxes", {})
        for tax_type, amount in sorted(taxes.items(), key=lambda x: -x[1]):
            print(f"  {tax_type:<25} ${amount:>12,.2f}")
        print(f"  {'─' * 25} {'─' * 13}")
        print(f"  {'Total Taxes':<25} ${ytd_breakdown.get('total_taxes', 0):>12,.2f}")

    # Totals validation (sum of current vs YTD)
    totals_validation = report.get("totals_validation", {})
    if totals_validation and totals_validation.get("segments"):
        print("\n" + "-" * 60)
        num_segments = totals_validation.get("employer_segments", 1)
        print(f"TOTALS VALIDATION ({totals_validation.get('total_stubs', 0)} stubs, {num_segments} employer segment(s)):")

        for seg in totals_validation["segments"]:
            seg_name = seg.get("segment", "Unknown")
            stub_count = seg.get("stub_count", 0)
            date_range = seg.get("date_range", {})
            start = date_range.get("start", "?")
            end = date_range.get("end", "?")

            print(f"\n  [{seg_name}] {stub_count} stubs from {start} to {end}")
            print(f"  {'Field':<20} {'Sum':>12} {'YTD':>12} {'Diff':>10}")
            print(f"  {'─' * 20} {'─' * 12} {'─' * 12} {'─' * 10}")

            fields = seg.get("fields", {})
            for field, vals in fields.items():
                diff = vals.get("diff", 0)
                diff_str = f"{diff:+,.2f}" if abs(diff) > 0.01 else "OK"
                print(f"  {field:<20} ${vals['sum']:>11,.2f} ${vals['ytd']:>11,.2f} {diff_str:>10}")

    # 401k contributions table
    contrib_401k = report.get("contributions_401k", {})
    if contrib_401k:
        print("\n" + "-" * 60)
        print("401(k) CONTRIBUTIONS BY MONTH:")
        print()
        month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        print(f"  {'Month':<6} {'Emp Pre-Tax':>12} {'Employer':>12} │ {'Tot Pre-Tax':>12} {'After-Tax':>12} │ {'Total':>12}")
        print(f"  {'─' * 6} {'─' * 12} {'─' * 12} ┼ {'─' * 12} {'─' * 12} ┼ {'─' * 12}")

        by_month = contrib_401k.get("by_month", {})
        for m in range(1, 13):
            month_data = by_month.get(m, by_month.get(str(m), {}))
            emp_pretax = month_data.get("pretax", 0)
            aftertax = month_data.get("aftertax", 0)
            employer = month_data.get("employer", 0)
            tot_pretax = emp_pretax + employer
            total = month_data.get("total", 0)
            # Only show months with contributions
            if total > 0:
                print(f"  {month_names[m-1]:<6} ${emp_pretax:>11,.2f} ${employer:>11,.2f} │ ${tot_pretax:>11,.2f} ${aftertax:>11,.2f} │ ${total:>11,.2f}")

        print(f"  {'─' * 6} {'─' * 12} {'─' * 12} ┼ {'─' * 12} {'─' * 12} ┼ {'─' * 12}")
        yearly = contrib_401k.get("yearly_totals", {})
        yearly_tot_pretax = yearly.get('pretax', 0) + yearly.get('employer', 0)
        print(f"  {'TOTAL':<6} ${yearly.get('pretax', 0):>11,.2f} ${yearly.get('employer', 0):>11,.2f} │ ${yearly_tot_pretax:>11,.2f} ${yearly.get('aftertax', 0):>11,.2f} │ ${yearly.get('total', 0):>11,.2f}")

    # Imputed income summary
    imputed = report.get("imputed_income", {})
    if imputed:
        print("\n" + "-" * 60)
        print("IMPUTED INCOME SUMMARY (YTD-based):")
        if imputed.get('prize_expenses', 0) > 0:
            print(f"  Prize/Gift expenses:     ${imputed.get('prize_expenses', 0):>10,.2f}")
        if imputed.get('benefits_in_kind', 0) > 0:
            print(f"  Benefits in Kind:        ${imputed.get('benefits_in_kind', 0):>10,.2f}")
        if imputed.get('tax_gross_up', 0) > 0:
            print(f"  Tax Gross-Up:            ${imputed.get('tax_gross_up', 0):>10,.2f}")
        print(f"  {'─' * 35}")
        print(f"  Total imputed income:    ${imputed.get('total_imputed', 0):>10,.2f}")

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
    if len(sys.argv) < 3:
        log("Usage: python3 analysis.py <year> <party> [--format text|json] [--cache-paystubs] [--through-date YYYY-MM-DD]")
        log("  year: 4-digit year (e.g., 2025)")
        log("  party: 'him' or 'her'")
        log("  --format: Output format (default: text)")
        log("  --cache-paystubs: Cache downloaded PDFs to avoid re-downloading")
        log("  --through-date: Only include pay stubs through this date (YYYY-MM-DD)")
        sys.exit(1)

    year = sys.argv[1]
    party = sys.argv[2]
    output_format = "text"
    cache_paystubs = "--cache-paystubs" in sys.argv
    through_date = None

    if party not in ("him", "her"):
        log(f"Error: Invalid party '{party}'. Must be 'him' or 'her'.")
        sys.exit(1)

    if "--format" in sys.argv:
        idx = sys.argv.index("--format")
        if idx + 1 < len(sys.argv):
            output_format = sys.argv[idx + 1]

    if "--through-date" in sys.argv:
        idx = sys.argv.index("--through-date")
        if idx + 1 < len(sys.argv):
            through_date = sys.argv[idx + 1]
            # Validate format
            try:
                datetime.strptime(through_date, "%Y-%m-%d")
            except ValueError:
                log(f"Error: Invalid date format '{through_date}'. Use YYYY-MM-DD.")
                sys.exit(1)

    if not year.isdigit() or len(year) != 4:
        log(f"Error: Invalid year '{year}'. Must be 4 digits.")
        sys.exit(1)

    log(f"Processing pay stubs for {year}, party: {party}...")

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

    # Determine working directory (cache or temp)
    if cache_paystubs:
        cache_dir = Path("cache") / year / "paystubs"
        cache_dir.mkdir(parents=True, exist_ok=True)
        workdir = str(cache_dir)
        log(f"Using cache directory: {workdir}")
    else:
        # Use a temporary directory that will be cleaned up
        import contextlib
        temp_ctx = tempfile.TemporaryDirectory()
        workdir = temp_ctx.name

    try:
        for pdf_info in pdf_files:
            pdf_name = pdf_info["name"]
            pdf_id = pdf_info["id"]

            log(f"\nProcessing: {pdf_name}")

            # Check if already cached
            local_path = os.path.join(workdir, pdf_name)
            if cache_paystubs and os.path.exists(local_path):
                log(f"  Using cached: {pdf_name}")
            else:
                # Download PDF
                download_file(pdf_id, local_path)
                if cache_paystubs:
                    log(f"  Downloaded and cached: {pdf_name}")

            # Split into pages
            page_files = split_pdf_pages(local_path, workdir)
            log(f"  Split into {len(page_files)} pages")

            # Process each page
            for page_file in page_files:
                stub_data = process_single_page(page_file, party)
                if stub_data and stub_data.get("pay_date"):
                    stub_data["_pay_type"] = identify_pay_type(stub_data)
                    stub_data["_source_file"] = pdf_name
                    all_stubs.append(stub_data)

            # Clean up split page files (but keep original PDFs in cache)
            for page_file in page_files:
                if os.path.exists(page_file):
                    os.remove(page_file)

            # Only clean up original PDF if not caching
            if not cache_paystubs:
                os.remove(local_path)
    finally:
        # Clean up temp directory if not using cache
        if not cache_paystubs:
            temp_ctx.cleanup()

    log(f"\nSuccessfully processed {len(all_stubs)} pay stubs")

    # Sort by date and YTD
    all_stubs.sort(key=get_sort_key)

    # Filter by through_date if specified
    if through_date:
        cutoff = datetime.strptime(through_date, "%Y-%m-%d")
        original_count = len(all_stubs)
        all_stubs = [
            s for s in all_stubs
            if parse_pay_date(s.get("pay_date", "")) <= cutoff
        ]
        filtered_count = original_count - len(all_stubs)
        if filtered_count > 0:
            log(f"Filtered out {filtered_count} stubs after {through_date}")
        log(f"Processing {len(all_stubs)} stubs through {through_date}")

    # Validate for gaps using SDK
    gap_analysis = detect_gaps(all_stubs, year, filter_regular_only=True)
    gap_errors, gap_warnings = gap_analysis.to_errors_warnings()

    # Check if first stub YTD indicates missing earlier stubs
    ytd_error = check_first_stub_ytd(all_stubs)
    if ytd_error:
        gap_errors.insert(0, ytd_error)

    # Validate totals (sum of current vs YTD)
    totals_errors, totals_warnings, totals_comparison = validate_year_totals(all_stubs)

    # Validate per-stub deltas (displayed current vs actual YTD increase)
    delta_errors, delta_warnings = validate_stub_deltas(all_stubs)

    # Combine errors and warnings
    errors = gap_errors + totals_errors + delta_errors
    warnings = gap_warnings + totals_warnings + delta_warnings

    # Build the report object (single source of truth)
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

    # Output to stdout
    if output_format == "json":
        print(json.dumps(report, indent=2))
    else:
        print_text_report(report)

    # Save full data to file (always include ytd_breakdown for reference)
    from paycalc.sdk import get_data_path
    data_dir = get_data_path()
    data_dir.mkdir(parents=True, exist_ok=True)

    output_file = data_dir / f"{year}_{party}_full.json"
    with open(output_file, "w") as f:
        json.dump(report, f, indent=2)
    log(f"\nFull data saved to: {output_file}")

    # Save 401k contributions to separate file for easy reference
    contrib_401k = report.get("contributions_401k", {})
    if contrib_401k:
        contrib_file = data_dir / f"{year}_401k_contributions.json"
        with open(contrib_file, "w") as f:
            json.dump(contrib_401k, f, indent=2)
        log(f"401k contributions saved to: {contrib_file}")

    # Exit with error code if gaps detected
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
