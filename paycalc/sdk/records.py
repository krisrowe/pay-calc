"""
Records management for pay stubs and W-2s.

This module contains all business logic for records storage and validation.
CLI and MCP tools should be thin wrappers that call these functions.

Design Rationale
----------------

Why import cannot filter folders by year/party:
    Year and party are auto-detected FROM file content (pay_date, employer name).
    Filtering a folder import by year/party would require:
    1. Downloading ALL files from Drive
    2. AI/OCR processing potentially ALL of them
    3. Just to determine which ones match the filter

    This would be equivalent in time, processing, and network I/O to a nuclear reset.
    There's no efficiency gain from "filtering" - you'd still process everything.

    Instead, we use a two-tier approach:
    - Folder imports: file-level tracking skips already-processed files (cheap, incremental)
    - Specific file imports: always process fully with stub-level dedup (targeted recovery)

    Recovery workflow:
    - To re-import a specific file: `records import <file-id>` (efficient, targeted)
    - To re-import everything: `reset` then `records import` (expensive, nuclear)

Discard vs Import distinction:
    When importing records from Drive, files that can't be processed are "discarded"
    rather than deleted. A discarded record is stored locally with meta.type="discarded"
    so we know not to re-download it on subsequent imports.

    IMPORTANT: "already imported" and "previously discarded" are different outcomes:
    - "already imported" → record exists, visible in `records list`
    - "previously discarded" → marker exists, hidden from list, use --force to retry

Why we don't distinguish "not a record" from "unsupported format":
    It's impossible to automatically tell whether a PDF is:
    - Genuinely not a pay stub or W-2 (random document in the folder)
    - A pay stub/W-2 from a provider whose format we don't support yet

    Therefore, --force retries ALL discards without trying to be clever about
    which ones are "retriable". If this causes waste (re-processing lots of
    irrelevant files), the user should organize their Drive folder to only
    contain relevant documents.

Discard reasons (meta.discard_reason):
    - "not_recognized": couldn't identify as stub or W-2 (includes unknown formats)
    - "unknown_party": detected type but employer doesn't match config
    - "unreadable": couldn't extract text from PDF
    - "parse_failed": detected type but couldn't extract structured data
"""

import json
import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Literal

from .config import get_data_path

# Configure logging based on LOG_LEVEL environment variable
_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s.%(msecs)03d %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

RecordType = Literal["stub", "w2", "discarded"]


# =============================================================================
# VALIDATION PIPELINE
# =============================================================================

class ValidationError(Exception):
    """Raised when a record fails validation."""
    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__(f"Validation failed: {'; '.join(errors)}")


# Schema definitions: required fields and their types
STUB_SCHEMA = {
    "required": {
        "pay_date": str,           # YYYY-MM-DD
        "gross_pay": (int, float),
        "net_pay": (int, float),
        "federal_tax": (int, float),
    },
    "optional": {
        "state_tax": (int, float),
        "social_security": (int, float),
        "medicare": (int, float),
        "pay_period_start": str,
        "pay_period_end": str,
        "employer": str,
        "ytd_gross": (int, float),
        "ytd_federal_tax": (int, float),
        "ytd_state_tax": (int, float),
        "ytd_social_security": (int, float),
        "ytd_medicare": (int, float),
        "ytd_net": (int, float),
        "hours": (int, float),
        "rate": (int, float),
        "other_deductions": (int, float, list),
    }
}

W2_SCHEMA = {
    "required": {
        "tax_year": (int, str),          # 2024 or "2024"
        "wages": (int, float),           # Box 1
        "federal_tax_withheld": (int, float),  # Box 2
    },
    "optional": {
        "employer_name": str,
        "employer_ein": str,
        "social_security_wages": (int, float),  # Box 3
        "social_security_tax": (int, float),    # Box 4
        "medicare_wages": (int, float),          # Box 5
        "medicare_tax": (int, float),            # Box 6
        "state": str,
        "state_wages": (int, float),             # Box 16
        "state_tax_withheld": (int, float),      # Box 17
        "local_wages": (int, float),
        "local_tax_withheld": (int, float),
    }
}


def normalize_stub_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize stub data from nested to flat format for validation.

    Current format (used by processors and throughout codebase):
        pay_summary.current.gross, taxes.federal_income.current, etc.

    Flat format (used by validation schema):
        gross_pay, federal_tax, etc.

    This converts the nested processor output to flat format for schema validation.
    """
    # If already flat format, return as-is
    if "gross_pay" in data or "pay_summary" not in data:
        return data

    # Convert nested to flat
    normalized = {
        "pay_date": data.get("pay_date"),
        "employer": data.get("employer"),
    }

    # Extract from pay_summary
    pay_summary = data.get("pay_summary", {})
    current = pay_summary.get("current", {})
    ytd = pay_summary.get("ytd", {})

    normalized["gross_pay"] = current.get("gross") or current.get("gross_pay") or 0
    normalized["net_pay"] = data.get("net_pay") or current.get("net_pay") or 0

    # Extract taxes - handle multiple naming conventions
    taxes = data.get("taxes", {})

    # Federal: federal_income_tax.current_withheld or federal_income.current
    fed_tax = taxes.get("federal_income_tax", {}) or taxes.get("federal_income", {}) or taxes.get("federal", {})
    normalized["federal_tax"] = fed_tax.get("current_withheld") or fed_tax.get("current") or 0

    # State
    state_tax = taxes.get("state_income_tax", {}) or taxes.get("state", {})
    normalized["state_tax"] = state_tax.get("current_withheld") or state_tax.get("current") or 0

    # Social Security
    ss_tax = taxes.get("social_security", {})
    normalized["social_security"] = ss_tax.get("current_withheld") or ss_tax.get("current") or 0

    # Medicare
    med_tax = taxes.get("medicare", {})
    normalized["medicare"] = med_tax.get("current_withheld") or med_tax.get("current") or 0

    # YTD values
    normalized["ytd_gross"] = ytd.get("gross") or 0
    normalized["ytd_federal_tax"] = fed_tax.get("ytd_withheld") or fed_tax.get("ytd") or 0
    normalized["ytd_state_tax"] = state_tax.get("ytd_withheld") or state_tax.get("ytd") or 0
    normalized["ytd_social_security"] = ss_tax.get("ytd_withheld") or ss_tax.get("ytd") or 0
    normalized["ytd_medicare"] = med_tax.get("ytd_withheld") or med_tax.get("ytd") or 0

    # Sum other deductions - handle both list and dict formats
    other_deductions = 0
    deductions_data = data.get("deductions", [])
    if isinstance(deductions_data, dict):
        # Dict format from OCR: {"retirement_401k": {"current": 100}, ...}
        for ded_name, ded_vals in deductions_data.items():
            if isinstance(ded_vals, dict):
                amount = ded_vals.get("current") or ded_vals.get("current_amount") or 0
            else:
                amount = ded_vals or 0
            other_deductions += abs(amount)
    elif isinstance(deductions_data, list):
        # List format: [{"name": "401k", "current_amount": 100}, ...]
        for deduction in deductions_data:
            amount = deduction.get("current_amount") or deduction.get("amount") or 0
            other_deductions += abs(amount)
    normalized["other_deductions"] = other_deductions

    # Pay period dates
    period = data.get("period", {})
    if period:
        normalized["pay_period_start"] = period.get("start")
        normalized["pay_period_end"] = period.get("end")

    return normalized


def _validate_schema(data: Dict[str, Any], schema: Dict, record_type: str) -> List[str]:
    """Validate data against a schema.

    Returns list of error messages (empty if valid).
    """
    errors = []

    # Check required fields
    for field, expected_type in schema["required"].items():
        if field not in data:
            errors.append(f"missing required field: {field}")
        elif data[field] is None:
            errors.append(f"required field is null: {field}")
        elif not isinstance(data[field], expected_type):
            errors.append(f"{field} has wrong type: expected {expected_type}, got {type(data[field]).__name__}")

    # Check optional fields (if present, must have correct type)
    for field, expected_type in schema.get("optional", {}).items():
        if field in data and data[field] is not None:
            if not isinstance(data[field], expected_type):
                errors.append(f"{field} has wrong type: expected {expected_type}, got {type(data[field]).__name__}")

    return errors


def _validate_stub_math(data: Dict[str, Any]) -> List[str]:
    """Validate pay stub math: do the numbers add up?

    Returns warnings (not errors) for math mismatches since there are many
    legitimate edge cases: imputed income (Group Term Life), 401k maxing,
    complex deduction structures, etc.

    Checks:
    - gross - deductions ≈ net (within tolerance)
    - YTD values >= current period values
    """
    warnings = []

    gross = data.get("gross_pay", 0) or 0
    net = data.get("net_pay", 0) or 0
    federal = data.get("federal_tax", 0) or 0
    state = data.get("state_tax", 0) or 0
    ss = data.get("social_security", 0) or 0
    medicare = data.get("medicare", 0) or 0
    other = data.get("other_deductions", 0)
    if isinstance(other, list):
        other = sum(d.get("amount", 0) for d in other if isinstance(d, dict))
    other = other or 0

    # gross - all deductions should approximately equal net
    # Use larger tolerance (5% of gross or $100) due to imputed income, etc.
    calculated_net = gross - federal - state - ss - medicare - other
    tolerance = max(gross * 0.05, 100.0) if gross > 0 else 100.0
    if abs(calculated_net - net) > tolerance:
        warnings.append(
            f"math warning: gross({gross:.2f}) - deductions = {calculated_net:.2f}, "
            f"but net_pay = {net:.2f} (diff: ${abs(calculated_net - net):.2f})"
        )

    # YTD validations: ytd should be >= current period (warning only)
    ytd_checks = [
        ("ytd_gross", "gross_pay"),
        ("ytd_federal_tax", "federal_tax"),
    ]

    for ytd_field, period_field in ytd_checks:
        ytd_val = data.get(ytd_field)
        period_val = data.get(period_field)
        if ytd_val is not None and period_val is not None:
            if ytd_val < period_val * 0.99:  # 1% tolerance
                warnings.append(f"YTD warning: {ytd_field}({ytd_val}) < {period_field}({period_val})")

    return warnings


def _validate_w2_math(data: Dict[str, Any]) -> List[str]:
    """Validate W-2 math: do the numbers add up?

    Checks:
    - Social Security tax ≈ 6.2% of SS wages (if both present)
    - Medicare tax ≈ 1.45% of Medicare wages (if both present)
    - State wages <= Federal wages (usually equal or less)
    """
    errors = []

    # Social Security: 6.2% of wages (up to cap)
    ss_wages = data.get("social_security_wages")
    ss_tax = data.get("social_security_tax")
    if ss_wages is not None and ss_tax is not None and ss_wages > 0:
        expected_ss = ss_wages * 0.062
        # Allow 5% tolerance for edge cases
        if abs(ss_tax - expected_ss) > (expected_ss * 0.05 + 1):
            errors.append(
                f"SS tax mismatch: wages({ss_wages}) × 6.2% = {expected_ss:.2f}, "
                f"but social_security_tax = {ss_tax}"
            )

    # Medicare: 1.45% of wages
    med_wages = data.get("medicare_wages")
    med_tax = data.get("medicare_tax")
    if med_wages is not None and med_tax is not None and med_wages > 0:
        expected_med = med_wages * 0.0145
        # Allow 5% tolerance + additional Medicare for high earners
        if med_tax < expected_med * 0.95 - 1:
            errors.append(
                f"Medicare tax too low: wages({med_wages}) × 1.45% = {expected_med:.2f}, "
                f"but medicare_tax = {med_tax}"
            )

    # State wages should not exceed federal wages
    wages = data.get("wages", 0) or 0
    state_wages = data.get("state_wages")
    if state_wages is not None and wages > 0:
        if state_wages > wages * 1.01:  # 1% tolerance
            errors.append(f"state_wages({state_wages}) exceeds federal wages({wages})")

    return errors


def _validate_date_format(date_str: str, field_name: str) -> List[str]:
    """Validate date is in YYYY-MM-DD format."""
    errors = []
    if date_str:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            errors.append(f"{field_name} not in YYYY-MM-DD format: {date_str}")
    return errors


def validate_record(
    record_type: RecordType,
    data: Dict[str, Any],
    meta: Dict[str, Any],
    check_duplicate: bool = True
) -> tuple:
    """Run full validation pipeline on a record.

    Args:
        record_type: "stub" or "w2"
        data: The record data
        meta: The record metadata (for duplicate checking)
        check_duplicate: Whether to check for existing records

    Returns:
        Tuple of (errors, warnings) - both are lists of strings.
        Errors block import; warnings are logged but don't block.
    """
    errors = []
    warnings = []

    if record_type in ("discarded", "unrelated"):
        return errors, warnings  # No validation for tracking records

    if data is None:
        errors.append("data cannot be None for non-tracking records")
        return errors, warnings

    # Schema validation (errors)
    if record_type == "stub":
        # Normalize nested format to flat for validation
        flat_data = normalize_stub_data(data)
        errors.extend(_validate_schema(flat_data, STUB_SCHEMA, "stub"))
        if not errors:  # Only check math if schema is valid
            warnings.extend(_validate_stub_math(flat_data))
        # Date format check (errors)
        if "pay_date" in data:
            errors.extend(_validate_date_format(data["pay_date"], "pay_date"))
        if "pay_period_start" in data:
            errors.extend(_validate_date_format(data["pay_period_start"], "pay_period_start"))
        if "pay_period_end" in data:
            errors.extend(_validate_date_format(data["pay_period_end"], "pay_period_end"))

    elif record_type == "w2":
        errors.extend(_validate_schema(data, W2_SCHEMA, "w2"))
        if not errors:  # Only check math if schema is valid
            warnings.extend(_validate_w2_math(data))
        # Year should be reasonable (error)
        tax_year = data.get("tax_year")
        if tax_year:
            try:
                year_int = int(tax_year)
                if year_int < 2000 or year_int > 2100:
                    errors.append(f"tax_year out of range: {tax_year}")
            except (ValueError, TypeError):
                errors.append(f"tax_year not a valid year: {tax_year}")

    # Duplicate check (error) - content-based for stubs, drive_file_id for W-2s
    if check_duplicate:
        if record_type == "stub":
            # Stub-level duplicate detection by document_id or content signature
            pay_date = data.get("pay_date") if data else None
            employer = data.get("employer") if data else None
            document_id = data.get("document_id") if data else None
            earnings_sig = _get_stub_content_signature(data) if data else None
            year = meta.get("year")
            party = meta.get("party")
            if pay_date and employer and year and party:
                existing = find_duplicate_stub(
                    pay_date, employer, year, party,
                    document_id=document_id, earnings_sig=earnings_sig
                )
                if existing:
                    errors.append(f"duplicate: stub with same identity exists (id={existing['id']})")
        elif record_type == "w2" and meta.get("drive_file_id"):
            # W-2s still use drive_file_id (one W-2 per file)
            existing = find_by_drive_id(meta["drive_file_id"])
            if existing:
                errors.append(f"duplicate: W-2 with drive_file_id already exists (id={existing['id']})")

    return errors, warnings


def validate_and_add_record(
    meta: Dict[str, Any],
    data: Optional[Dict[str, Any]],
    skip_validation: bool = False
) -> tuple:
    """Validate a record and add it if valid.

    This is the primary entry point for importing records.

    Args:
        meta: Record metadata
        data: Record data
        skip_validation: Skip validation (use with caution)

    Returns:
        Tuple of (path, warnings) where path is the saved file path
        and warnings is a list of warning messages (may be empty)

    Raises:
        ValidationError: If validation fails (errors, not warnings)
        ValueError: If required metadata is missing
    """
    record_type = meta.get("type")
    warnings = []

    if not skip_validation and record_type != "discarded":
        errors, warnings = validate_record(record_type, data, meta, check_duplicate=True)
        if errors:
            raise ValidationError(errors)

    # Store warnings in meta for traceability
    if warnings:
        meta["warnings"] = warnings

    path = add_record(meta, data)
    return path, warnings


# =============================================================================
# STORAGE FUNCTIONS
# =============================================================================

def get_records_dir() -> Path:
    """Get the records base directory.

    Returns:
        Path to records directory (~/.local/share/pay-calc/records/)
    """
    records_dir = get_data_path() / "records"
    records_dir.mkdir(parents=True, exist_ok=True)
    return records_dir


def _generate_record_id(meta: Dict[str, Any], data: Optional[Dict[str, Any]] = None) -> str:
    """Generate a record ID from content for filename.

    This ID is used as the JSON filename on disk. It's content-based so that:
    - Same stub imported from different sources gets the same filename
    - Re-importing the same PDF overwrites rather than duplicates

    NOTE: Filenames are NOT guaranteed stable across tool versions. This logic
    may change. The CLI regenerates IDs from data at display time for stability.

    For stubs: hash of "stub|doc:{document_id}|{pay_date}" or "stub|med:{medicare_wages}|{pay_date}"
    For W-2s: hash of "w2|{tax_year}|{employer}|{wages}"
    For tracking records (discarded/unrelated): hash of drive_file_id or source_filename

    IMPORTANT: The CLI's _generate_content_id() uses the SAME logic to regenerate
    IDs at display time rather than reading the filename. This intentional redundancy
    provides data integrity and backward compatibility with older records that used
    different filename logic (e.g., drive_file_id + page).

    WARNING: If you change this logic, you MUST update cli/records_commands.py
    _generate_content_id() to match. The two functions must stay in sync.
    See also: README.md "Record IDs" section.

    Returns first 8 chars of hash for brevity.
    """
    record_type = meta.get("type", "unknown")

    if record_type == "stub" and data:
        # Use document_id if available, else medicare taxable wages
        doc_id = data.get("document_id", "")
        if doc_id and doc_id not in ("", "None", "null", "N/A"):
            identifier = f"doc:{doc_id}"
        else:
            taxes = data.get("taxes", {})
            medicare = taxes.get("medicare", {})
            medicare_wages = medicare.get("taxable_wages", 0.0)
            identifier = f"med:{medicare_wages:.2f}"

        pay_date = data.get("pay_date", "")
        content = f"stub|{identifier}|{pay_date}"

    elif record_type == "w2" and data:
        tax_year = str(data.get("tax_year", ""))
        employer = data.get("employer_name", "")
        wages = data.get("wages", 0.0)
        content = f"w2|{tax_year}|{employer}|{wages:.2f}"

    else:
        # Tracking records: use drive_file_id or source_filename
        if meta.get("drive_file_id"):
            content = meta["drive_file_id"]
        else:
            content = f"{meta.get('source_filename', '')}{meta.get('imported_at', '')}"

    return hashlib.sha256(content.encode()).hexdigest()[:8]


def _extract_year_party_from_data(record_type: RecordType, data: Dict[str, Any]) -> tuple:
    """Extract year and party from record data based on type.

    For stubs: year from pay_date, party must be provided in meta
    For W-2s: year from tax_year, party must be provided in meta

    Returns:
        (year, party) tuple, either may be None if not extractable
    """
    year = None

    if record_type == "stub":
        pay_date = data.get("pay_date", "")
        if pay_date and len(pay_date) >= 4:
            year = pay_date[:4]
    elif record_type == "w2":
        year = data.get("tax_year") or data.get("year")
        if year:
            year = str(year)

    return year, None  # party comes from meta, not data


def list_records(
    year: Optional[str] = None,
    party: Optional[str] = None,
    type_filter: Optional[RecordType] = None,
    include_discarded: bool = False
) -> List[Dict[str, Any]]:
    """List records with optional filters.

    Args:
        year: Filter by year (e.g., "2024")
        party: Filter by party (e.g., "him", "her")
        type_filter: Filter by type ("stub", "w2")
        include_discarded: Include discarded records (default False)

    Returns:
        List of records, each with 'meta', 'data', and 'id' keys
    """
    records_dir = get_records_dir()
    results = []

    # Determine which directories to scan
    if year and party:
        # Specific year/party
        scan_dirs = [records_dir / year / party]
    elif year:
        # All parties for a year
        year_dir = records_dir / year
        scan_dirs = [year_dir / p for p in year_dir.iterdir() if p.is_dir()] if year_dir.exists() else []
    elif party:
        # All years for a party
        scan_dirs = []
        for year_dir in records_dir.iterdir():
            if year_dir.is_dir() and year_dir.name != "_discarded":
                party_dir = year_dir / party
                if party_dir.exists():
                    scan_dirs.append(party_dir)
    else:
        # All records
        scan_dirs = []
        for year_dir in records_dir.iterdir():
            if year_dir.is_dir() and year_dir.name != "_discarded":
                for party_dir in year_dir.iterdir():
                    if party_dir.is_dir():
                        scan_dirs.append(party_dir)

    # Scan directories for JSON files
    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for json_file in scan_dir.glob("*.json"):
            try:
                with open(json_file) as f:
                    record = json.load(f)

                meta = record.get("meta", {})
                record_type = meta.get("type")

                # Apply type filter
                if type_filter and record_type != type_filter:
                    continue

                # Skip tracking records (discarded/unrelated) unless requested
                if record_type in ("discarded", "unrelated") and not include_discarded:
                    continue

                # Add ID and path for convenience
                record["id"] = json_file.stem
                record["_path"] = str(json_file)
                results.append(record)

            except (json.JSONDecodeError, IOError):
                continue  # Skip invalid files

    # Also scan discarded if requested
    if include_discarded:
        discarded_dir = records_dir / "_discarded"
        if discarded_dir.exists():
            for json_file in discarded_dir.glob("*.json"):
                try:
                    with open(json_file) as f:
                        record = json.load(f)
                    record["id"] = json_file.stem
                    record["_path"] = str(json_file)
                    results.append(record)
                except (json.JSONDecodeError, IOError):
                    continue

    # Sort by date (pay_date for stubs, tax_year for W-2s)
    def sort_key(r):
        data = r.get("data") or {}
        # Convert to string for consistent sorting (tax_year is int, pay_date is str)
        return str(data.get("pay_date") or data.get("tax_year") or "")

    results.sort(key=sort_key)
    return results


def add_record(meta: Dict[str, Any], data: Optional[Dict[str, Any]]) -> Path:
    """Save a record to the appropriate location.

    Args:
        meta: Record metadata with at least 'type' key.
              For stubs/W-2s, should include 'year' and 'party'.
              May include: drive_file_id, source_filename, imported_at, discard_reason
        data: Record data (stub or W-2 content), None for discarded records

    Returns:
        Path to the saved JSON file

    Raises:
        ValueError: If required metadata is missing
    """
    record_type = meta.get("type")
    valid_types = ("stub", "w2", "discarded", "unrelated")
    if record_type not in valid_types:
        raise ValueError(f"meta.type must be one of {valid_types}, got: {record_type}")

    # Ensure imported_at is set
    if "imported_at" not in meta:
        meta["imported_at"] = datetime.now().isoformat()

    # Generate record ID from content
    record_id = _generate_record_id(meta, data)

    # Determine storage path
    records_dir = get_records_dir()

    if record_type in ("discarded", "unrelated"):
        target_dir = records_dir / "_tracking"
    else:
        # Need year and party for stubs/W-2s
        year = meta.get("year")
        party = meta.get("party")

        if not year or not party:
            raise ValueError(f"meta.year and meta.party required for type={record_type}")

        target_dir = records_dir / year / party

    target_dir.mkdir(parents=True, exist_ok=True)

    # Build and save record
    record = {"meta": meta, "data": data}
    record_path = target_dir / f"{record_id}.json"

    with open(record_path, "w") as f:
        json.dump(record, f, indent=2)

    return record_path


def find_by_drive_id(drive_file_id: str) -> Optional[Dict[str, Any]]:
    """Find a record by its Drive file ID.

    Scans all records (including discarded) for a matching drive_file_id.

    Args:
        drive_file_id: The Google Drive file ID to search for

    Returns:
        Record dict with 'meta', 'data', 'id', '_path' keys if found, None otherwise
    """
    records_dir = get_records_dir()

    # Scan all JSON files in records directory tree
    for json_file in records_dir.rglob("*.json"):
        try:
            with open(json_file) as f:
                record = json.load(f)

            if record.get("meta", {}).get("drive_file_id") == drive_file_id:
                record["id"] = json_file.stem
                record["_path"] = str(json_file)
                return record

        except (json.JSONDecodeError, IOError):
            continue

    return None


def find_all_by_drive_id(drive_file_id: str) -> List[Dict[str, Any]]:
    """Find all records by their Drive file ID.

    For multi-page PDFs, multiple records may share the same drive_file_id.
    This returns all matching records.

    Args:
        drive_file_id: The Google Drive file ID to search for

    Returns:
        List of record dicts with 'meta', 'data', 'id', '_path' keys
    """
    records_dir = get_records_dir()
    results = []

    # Scan all JSON files in records directory tree
    for json_file in records_dir.rglob("*.json"):
        try:
            with open(json_file) as f:
                record = json.load(f)

            if record.get("meta", {}).get("drive_file_id") == drive_file_id:
                record["id"] = json_file.stem
                record["_path"] = str(json_file)
                results.append(record)

        except (json.JSONDecodeError, IOError):
            continue

    return results


def _get_stub_content_signature(data: Dict[str, Any]) -> str:
    """Generate a signature from stub content for duplicate detection.

    Uses Medicare taxable wages as the primary identifier - it represents
    total compensation for the pay period and is universal across employers
    and pay types. A stub with $0 Medicare taxable likely means all earnings
    are zero (placeholder or year-end adjustment).

    Falls back to first earning current_amount if Medicare data unavailable.
    """
    # Primary: Medicare taxable wages (universal, always present when there's real pay)
    taxes = data.get("taxes", {})
    medicare = taxes.get("medicare", {})
    medicare_taxable = medicare.get("taxable_wages")
    if medicare_taxable is not None:
        return f"medicare_taxable:{medicare_taxable}"

    # Fallback: first earning current amount
    earnings = data.get("earnings", [])
    if earnings:
        e = earnings[0]
        amount = e.get("current_amount", 0)
        return f"first_earning:{amount}"

    return ""


def find_duplicate_stub(
    pay_date: str,
    employer: str,
    year: str,
    party: str,
    document_id: Optional[str] = None,
    earnings_sig: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Find an existing stub that matches the given stub's identity.

    This is the stub-level duplicate detection used to prevent importing
    the same pay stub twice (even from different source files).

    Matching logic:
    1. If document_id is provided: match by document_id (most specific)
    2. Otherwise: match by pay_date + employer + earnings_sig (content-based)

    The earnings_sig captures the first few earnings types and amounts,
    which distinguishes stubs on the same date (e.g., regular pay vs stock grant).

    Args:
        pay_date: The pay date (YYYY-MM-DD format)
        employer: The employer name
        year: The year for scoping the search
        party: The party (him/her) for scoping the search
        document_id: Optional document ID from the pay stub
        earnings_sig: Optional earnings signature for content-based matching

    Returns:
        Matching record dict if found, None otherwise
    """
    records_dir = get_records_dir()
    search_dir = records_dir / year / party

    if not search_dir.exists():
        return None

    for json_file in search_dir.glob("*.json"):
        try:
            with open(json_file) as f:
                record = json.load(f)

            rec_data = record.get("data", {})
            rec_meta = record.get("meta", {})

            # Only check stubs (not W-2s or discarded)
            if rec_meta.get("type") != "stub":
                continue

            # Primary match: document_id (most reliable)
            if document_id and rec_data.get("document_id"):
                if rec_data.get("document_id") == document_id:
                    record["id"] = json_file.stem
                    record["_path"] = str(json_file)
                    return record
                # If both have document_id but they differ, not a match
                continue

            # Fallback match: pay_date + employer + content_sig
            # (for stubs without document_id)
            if not document_id and not rec_data.get("document_id"):
                rec_sig = _get_stub_content_signature(rec_data)
                if (rec_data.get("pay_date") == pay_date and
                    rec_data.get("employer") == employer and
                    rec_sig == earnings_sig):
                    record["id"] = json_file.stem
                    record["_path"] = str(json_file)
                    return record

        except (json.JSONDecodeError, IOError):
            continue

    return None


def remove_record(record_id: str) -> bool:
    """Delete a record by its ID.

    Searches all record locations for the ID and deletes if found.

    Args:
        record_id: The 8-char record ID (filename without .json)

    Returns:
        True if record was found and deleted, False if not found
    """
    records_dir = get_records_dir()

    # Search for the file
    for json_file in records_dir.rglob(f"{record_id}.json"):
        json_file.unlink()
        return True

    return False


def get_record(record_id: str) -> Optional[Dict[str, Any]]:
    """Get a single record by ID.

    Args:
        record_id: The 8-char record ID

    Returns:
        Record dict if found, None otherwise
    """
    records_dir = get_records_dir()

    for json_file in records_dir.rglob(f"{record_id}.json"):
        try:
            with open(json_file) as f:
                record = json.load(f)
            record["id"] = json_file.stem
            record["_path"] = str(json_file)
            return record
        except (json.JSONDecodeError, IOError):
            return None

    return None


def list_discarded() -> List[Dict[str, Any]]:
    """List all discarded records.

    Returns:
        List of discarded records with 'meta', 'data' (null), 'id', '_path' keys
    """
    discarded_dir = get_records_dir() / "_discarded"
    results = []

    if not discarded_dir.exists():
        return results

    for json_file in discarded_dir.glob("*.json"):
        try:
            with open(json_file) as f:
                record = json.load(f)
            record["id"] = json_file.stem
            record["_path"] = str(json_file)
            results.append(record)
        except (json.JSONDecodeError, IOError):
            continue

    # Sort by source filename
    results.sort(key=lambda r: r.get("meta", {}).get("source_filename", ""))
    return results


def clear_all_records() -> int:
    """Delete all records (for reset functionality).

    Returns:
        Number of records deleted
    """
    import shutil
    records_dir = get_records_dir()

    count = 0
    if records_dir.exists():
        # Count files before deletion
        count = sum(1 for _ in records_dir.rglob("*.json"))

        # Remove the entire records directory
        shutil.rmtree(records_dir)

    return count


# =============================================================================
# FOLDER IMPORT FUNCTIONS
# =============================================================================

def is_drive_folder_id(source: str) -> bool:
    """Check if source looks like a Google Drive folder ID.

    Drive folder IDs are typically 33 chars, alphanumeric with - and _.
    """
    if len(source) > 20 and "/" not in source and "\\" not in source:
        return all(c.isalnum() or c in "-_" for c in source)
    return False


# Pay stub extraction prompt for Gemini OCR
# (JSON boilerplate handled by gemini_client)
PAYSTUB_OCR_PROMPT = """Extract pay stub data into this JSON structure:

{
  "pay_date": "YYYY-MM-DD",
  "employer": "company name",
  "net_pay": 0.00,
  "pay_summary": {
    "current": {"gross": 0.00, "taxes": 0.00, "net_pay": 0.00},
    "ytd": {"gross": 0.00, "taxes": 0.00}
  },
  "taxes": {
    "federal_income": {"taxable_wages": 0.00, "current": 0.00, "ytd": 0.00},
    "social_security": {"taxable_wages": 0.00, "current": 0.00, "ytd": 0.00},
    "medicare": {"taxable_wages": 0.00, "current": 0.00, "ytd": 0.00},
    "state": {"taxable_wages": 0.00, "current": 0.00, "ytd": 0.00}
  },
  "deductions": [
    {"type": "description", "current_amount": 0.00, "ytd_amount": 0.00}
  ],
  "earnings": [
    {"type": "Gross Pay", "current_amount": 0.00, "ytd_amount": 0.00}
  ]
}

Include all deductions (401k, health insurance, etc.) and all earnings types found.
Include non-cash taxable fringe benefits (EEGTL, group term life, imputed income) in the earnings array.
For taxable_wages: use value shown on stub, or 0.00 if not shown.
pay_summary.current.taxes should be the sum of all tax withholdings.
"""


def process_pdf_to_json(pdf_path: Path, record_type: RecordType, party: str) -> Optional[Dict[str, Any]]:
    """Extract data from a PDF and return as JSON dict.

    Uses text extraction for text-based PDFs, falls back to OCR for image PDFs.

    Args:
        pdf_path: Path to the PDF file
        record_type: Expected type (stub/w2)
        party: Party for employer matching

    Returns:
        Extracted data dict, or None if extraction failed
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    try:
        import PyPDF2

        # Try to extract text from PDF
        pdf_text = ""
        try:
            with open(pdf_path, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                for page in reader.pages:
                    pdf_text += page.extract_text() or ""
        except Exception:
            pdf_text = ""

        # If no text, need OCR
        if not pdf_text.strip():
            try:
                from gemini_client import process_file
                data = process_file(PAYSTUB_OCR_PROMPT, str(pdf_path))
                data["_source_file"] = pdf_path.name
                data["_ocr"] = True
                return data
            except ImportError:
                return None
            except Exception:
                return None

        # Text-based PDF - use processor
        try:
            from analysis import process_single_page
            return process_single_page(str(pdf_path), party)
        except ImportError:
            return None
        except Exception:
            return None

    except Exception:
        return None


def import_from_folder(
    source: str,
    year: str,
    party: str,
    record_type: RecordType,
    callback: Optional[callable] = None
) -> Dict[str, Any]:
    """Import records from a folder (Drive or local).

    Handles both JSON and PDF files. PDFs are processed to extract data.

    Args:
        source: Drive folder ID or local folder path
        year: Year for imported records
        party: Party (him/her) for imported records
        record_type: Type of records to import (stub/w2)
        callback: Optional function called for progress updates.
                  Signature: callback(event: str, data: dict)
                  Events: "start", "file", "imported", "skipped", "error", "done"

    Returns:
        Dict with import statistics:
        - imported: count of successfully imported records
        - skipped: count of skipped (duplicate) records
        - errors: count of errors
        - warnings: list of all warnings
        - files: list of processed file names
    """
    import tempfile

    stats = {
        "imported": 0,
        "skipped": 0,
        "errors": 0,
        "warnings": [],
        "files": []
    }

    def emit(event: str, data: dict = None):
        if callback:
            callback(event, data or {})

    # Determine source type and get file list
    if is_drive_folder_id(source):
        # Import from Google Drive - requires gwsa
        try:
            import subprocess
            result = subprocess.run(
                ["gwsa", "drive", "list", "--folder-id", source],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                raise RuntimeError(f"gwsa failed: {result.stderr}")

            import json as json_module
            files_info = json_module.loads(result.stdout).get("items", [])
        except FileNotFoundError:
            raise RuntimeError("gwsa not installed - required for Drive imports")
        except Exception as e:
            raise RuntimeError(f"Failed to list Drive folder: {e}")

        # Filter to JSON and PDF files
        processable = [f for f in files_info
                       if f.get("name", "").lower().endswith((".json", ".pdf"))]

        # Download to temp dir and process
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            emit("start", {"source": source, "file_count": len(processable)})

            for file_info in processable:
                name = file_info.get("name", "")
                file_id = file_info.get("id", "")

                emit("file", {"name": name})
                stats["files"].append(name)

                local_path = tmp_path / name
                try:
                    subprocess.run(
                        ["gwsa", "drive", "download", file_id, str(local_path)],
                        capture_output=True, text=True, check=True
                    )
                except subprocess.CalledProcessError as e:
                    emit("error", {"name": name, "error": str(e)})
                    stats["errors"] += 1
                    continue

                # Import the downloaded file
                file_result = _import_single_file(
                    local_path, year, party, record_type,
                    drive_file_id=file_id
                )
                stats["imported"] += file_result["imported"]
                stats["skipped"] += file_result["skipped"]
                stats["errors"] += file_result["errors"]
                stats["warnings"].extend(file_result.get("warnings", []))

                if file_result["imported"]:
                    emit("imported", {"name": name})
                elif file_result["skipped"]:
                    emit("skipped", {"name": name})
                elif file_result["errors"]:
                    emit("error", {"name": name, "error": file_result.get("error_msg", "unknown")})

    else:
        # Local folder
        folder_path = Path(source)
        if not folder_path.is_dir():
            raise ValueError(f"Not a directory: {source}")

        # Get JSON and PDF files
        files = sorted(list(folder_path.glob("*.json")) + list(folder_path.glob("*.pdf")))
        emit("start", {"source": source, "file_count": len(files)})

        for file_path in files:
            emit("file", {"name": file_path.name})
            stats["files"].append(file_path.name)

            file_result = _import_single_file(file_path, year, party, record_type)
            stats["imported"] += file_result["imported"]
            stats["skipped"] += file_result["skipped"]
            stats["errors"] += file_result["errors"]
            stats["warnings"].extend(file_result.get("warnings", []))

            if file_result["imported"]:
                emit("imported", {"name": file_path.name})
            elif file_result["skipped"]:
                emit("skipped", {"name": file_path.name})
            elif file_result["errors"]:
                emit("error", {"name": file_path.name, "error": file_result.get("error_msg", "unknown")})

    emit("done", stats)
    return stats


# =============================================================================
# AUTO-DETECTION IMPORT FUNCTIONS
# =============================================================================

def detect_party_from_employer(employer_name: str, profile: Optional[Dict] = None) -> Optional[str]:
    """Match employer name to a party using profile config keywords.

    Args:
        employer_name: Employer name from parsed document
        profile: Profile dict (loaded from load_profile() if not provided)

    Returns:
        Party name ("him", "her", etc.) if matched, None otherwise
    """
    if not employer_name:
        return None

    if profile is None:
        from .config import load_profile
        profile = load_profile()

    normalized = employer_name.lower().replace(" ", "")

    parties = profile.get("parties", {})
    for party_name, party_data in parties.items():
        if not isinstance(party_data, dict):
            continue
        for company in party_data.get("companies", []):
            keywords = company.get("keywords", [])
            # Also match on company name itself
            company_name = company.get("name", "")
            if company_name:
                keywords = keywords + [company_name]

            for keyword in keywords:
                if keyword.lower().replace(" ", "") in normalized:
                    return party_name

    return None


def detect_record_type_from_data(data: Dict[str, Any]) -> Optional[RecordType]:
    """Detect record type (stub vs w2) from parsed data structure.

    Args:
        data: Parsed record data

    Returns:
        "stub", "w2", or None if can't detect
    """
    if not data:
        return None

    # Check both top-level and nested "data" field (some formats nest the actual data)
    check_data = data
    if "data" in data and isinstance(data["data"], dict):
        check_data = data["data"]

    # W-2 indicators
    w2_fields = ["tax_year", "wages", "federal_tax_withheld",
                 "wages_tips_other_comp", "federal_income_tax_withheld",
                 "social_security_wages", "medicare_wages"]

    w2_matches = sum(1 for f in w2_fields if f in check_data or f in data)
    if w2_matches >= 2:
        return "w2"

    # Stub indicators: has pay_date and pay-related fields
    if "pay_date" in data or "pay_date" in check_data:
        return "stub"
    if "pay_summary" in data or "pay_summary" in check_data:
        return "stub"
    if ("gross_pay" in check_data and "net_pay" in check_data) or \
       ("gross_pay" in data and "net_pay" in data):
        return "stub"

    return None


def extract_year_from_data(data: Dict[str, Any], record_type: RecordType,
                           filename: Optional[str] = None) -> Optional[str]:
    """Extract year from record data or filename.

    Args:
        data: Parsed record data
        record_type: "stub" or "w2"
        filename: Optional filename to extract year from if not in data

    Returns:
        Year string (e.g., "2025") or None
    """
    import re

    if not data:
        return None

    # Check nested data field too
    check_data = data.get("data", {}) if isinstance(data.get("data"), dict) else {}

    if record_type == "stub":
        pay_date = data.get("pay_date") or check_data.get("pay_date", "")
        if pay_date and len(pay_date) >= 4:
            return pay_date[:4]
    elif record_type == "w2":
        tax_year = data.get("tax_year") or check_data.get("tax_year")
        if tax_year:
            return str(tax_year)

    # Try to extract year from filename (e.g., "2024_manual-w2_1.json" or "2024 W-2...")
    if filename:
        # Match 4-digit year at start or after common separators
        match = re.search(r'(?:^|[_\-\s])(\d{4})(?:[_\-\s]|$)', filename)
        if match:
            year = match.group(1)
            if 2000 <= int(year) <= 2100:
                return year

    return None


def extract_employer_from_data(data: Dict[str, Any], record_type: RecordType) -> Optional[str]:
    """Extract employer name from record data.

    Args:
        data: Parsed record data
        record_type: "stub" or "w2"

    Returns:
        Employer name or None
    """
    if not data:
        return None

    # Try common field names
    for field in ["employer", "employer_name", "company", "company_name"]:
        if field in data and data[field]:
            return data[field]

    return None


def import_file_auto(
    file_path: Path,
    force: bool = False,
    drive_file_id: Optional[str] = None
) -> Dict[str, Any]:
    """Import a single file with auto-detection of type, year, and party.

    Parses the file, detects:
    - Record type (stub vs W-2) from content structure
    - Year from pay_date (stubs) or tax_year (W-2s)
    - Party by matching employer to profile config keywords

    Args:
        file_path: Path to PDF or JSON file
        force: If True, re-process previously discarded files
        drive_file_id: Optional Drive file ID for tracking

    Returns:
        Dict with:
        - status: "imported", "skipped", "discarded"
        - type: "stub", "w2", or None
        - year: Detected year or None
        - party: Detected party or None
        - employer: Detected employer name or None
        - reason: Reason if skipped/discarded
        - path: Saved file path if imported
    """
    result = {
        "status": None,
        "type": None,
        "year": None,
        "party": None,
        "employer": None,
        "reason": None,
        "path": None,
    }

    # Check if already processed
    if drive_file_id:
        existing = find_by_drive_id(drive_file_id)
        if existing:
            existing_type = existing.get("meta", {}).get("type")
            if existing_type in ("discarded", "unrelated"):
                if not force:
                    result["status"] = "skipped"
                    result["reason"] = "previously skipped"
                    return result
                # With --force, continue to re-process
            else:
                result["status"] = "skipped"
                result["reason"] = "already imported"
                result["type"] = existing_type
                return result

    # Parse the file
    suffix = file_path.suffix.lower()
    data = None

    if suffix == ".json":
        try:
            with open(file_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            result["status"] = "discarded"
            result["reason"] = f"parse_failed: {e}"
            _save_tracking(file_path.name, drive_file_id, result["reason"])
            return result

    elif suffix == ".pdf":
        # First try text extraction
        data = _extract_pdf_auto(file_path)
        if data is None:
            result["status"] = "discarded"
            result["reason"] = "unreadable"
            _save_tracking(file_path.name, drive_file_id, result["reason"])
            return result

    else:
        result["status"] = "discarded"
        result["reason"] = "unsupported format"
        return result

    # Detect record type
    record_type = detect_record_type_from_data(data)
    if not record_type:
        result["status"] = "discarded"
        result["reason"] = "not_recognized"
        _save_tracking(file_path.name, drive_file_id, result["reason"])
        return result

    result["type"] = record_type

    # Extract year
    year = extract_year_from_data(data, record_type)
    if not year:
        result["status"] = "discarded"
        result["reason"] = "no_year_detected"
        _save_tracking(file_path.name, drive_file_id, result["reason"])
        return result

    result["year"] = year

    # Extract employer and detect party
    employer = extract_employer_from_data(data, record_type)
    result["employer"] = employer

    party = detect_party_from_employer(employer)
    if not party:
        result["status"] = "discarded"
        result["reason"] = f"unknown_party (employer: {employer or 'not found'})"
        _save_tracking(file_path.name, drive_file_id, result["reason"])
        return result

    result["party"] = party

    # Build metadata
    meta = {
        "type": record_type,
        "year": year,
        "party": party,
        "source_filename": file_path.name,
    }
    if drive_file_id:
        meta["drive_file_id"] = drive_file_id
    if data.get("_extraction_method"):
        meta["extraction_method"] = data.pop("_extraction_method")

    # Validate and save
    try:
        path, warnings = validate_and_add_record(meta=meta, data=data)
        result["status"] = "imported"
        result["path"] = str(path)
        if warnings:
            result["warnings"] = warnings
            for w in warnings:
                logger.warning(f"{file_path.name}: {w}")
        return result

    except ValidationError as e:
        # Check if it's a duplicate
        if any("duplicate" in err.lower() for err in e.errors):
            result["status"] = "skipped"
            result["reason"] = "duplicate"
        else:
            result["status"] = "discarded"
            result["reason"] = f"validation_failed: {'; '.join(e.errors)}"
            _save_tracking(file_path.name, drive_file_id, result["reason"])
        return result


def import_file_auto_all(
    file_path: Path,
    force: bool = False,
    drive_file_id: Optional[str] = None,
    targeted: bool = False
) -> List[Dict[str, Any]]:
    """Import a file with multi-page PDF support.

    For multi-page PDFs (common for quarterly pay stub bundles), splits the PDF
    and processes each page as a separate record. For JSON files and single-page
    PDFs, behaves like import_file_auto.

    Args:
        file_path: Path to PDF or JSON file
        force: If True, re-process previously discarded files
        drive_file_id: Optional Drive file ID for tracking
        targeted: If True, bypass file-level "already imported" check and use
                  stub-level duplicate detection instead. Use this for targeted
                  re-import of specific files (recovery workflow).

    Returns:
        List of result dicts, one per imported record. Each has:
        - status: "imported", "skipped", "discarded"
        - type: "stub", "w2", or None
        - year, party, employer, reason, path as in import_file_auto
        - page: (multi-page PDFs only) page number within source file
    """
    import tempfile

    suffix = file_path.suffix.lower()

    # JSON files - single record
    if suffix == ".json":
        return [import_file_auto(file_path, force=force, drive_file_id=drive_file_id)]

    # Not a PDF - delegate to single import
    if suffix != ".pdf":
        return [import_file_auto(file_path, force=force, drive_file_id=drive_file_id)]

    # PDF - check if already processed (by drive_file_id)
    # Skip this check for targeted imports - rely on stub-level dedup instead
    if drive_file_id and not targeted:
        existing_records = find_all_by_drive_id(drive_file_id)
        if existing_records:
            # Already processed - return skipped results
            results = []
            for rec in existing_records:
                rec_type = rec.get("meta", {}).get("type")
                if rec_type in ("discarded", "unrelated"):
                    if not force:
                        results.append({
                            "status": "skipped",
                            "reason": "previously skipped",
                            "type": None,
                            "year": None,
                            "party": None,
                            "employer": None,
                            "path": None,
                        })
                    # With --force, continue to re-process
                    else:
                        break  # Will process below
                else:
                    results.append({
                        "status": "skipped",
                        "reason": "already imported",
                        "type": rec_type,
                        "year": rec.get("meta", {}).get("year"),
                        "party": rec.get("meta", {}).get("party"),
                        "employer": rec.get("data", {}).get("employer"),
                        "path": None,
                    })
            if results and not (force and any(r.get("reason") == "previously skipped" for r in results)):
                return results

    # Check page count
    page_count = _get_pdf_page_count(file_path)
    if page_count <= 1:
        # Single page - use standard import
        result = import_file_auto(file_path, force=force, drive_file_id=drive_file_id)
        # Track single-page duplicates so we don't re-download
        if (drive_file_id and
            result.get("status") == "skipped" and
            result.get("reason") == "duplicate"):
            _save_tracking(file_path.name, drive_file_id, "all_duplicates")
        return [result]

    # Multi-page PDF - split and process each page
    logger.debug(f"Multi-page PDF detected: {file_path.name} has {page_count} pages")
    results = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        page_files = _split_pdf_pages(file_path, tmp_path)

        if not page_files:
            # Split failed - fall back to single import
            logger.warning(f"Failed to split PDF: {file_path.name}")
            return [import_file_auto(file_path, force=force, drive_file_id=drive_file_id)]

        for page_num, page_file in enumerate(page_files, start=1):
            # Process this page
            result = _import_single_page(
                page_file,
                source_filename=file_path.name,
                page_number=page_num,
                drive_file_id=drive_file_id,
                force=force
            )
            result["page"] = page_num
            results.append(result)

    # If all stubs were duplicates, save a tracking record so we don't re-download
    if drive_file_id and results:
        all_duplicates = all(
            r.get("status") == "skipped" and r.get("reason") == "duplicate"
            for r in results
        )
        if all_duplicates:
            _save_tracking(file_path.name, drive_file_id, "all_duplicates")

    return results


def _import_single_page(
    page_file: Path,
    source_filename: str,
    page_number: int,
    drive_file_id: Optional[str] = None,
    force: bool = False
) -> Dict[str, Any]:
    """Import a single page from a multi-page PDF.

    Args:
        page_file: Path to the single-page PDF (temporary file)
        source_filename: Original filename (for metadata)
        page_number: Page number within the source file
        drive_file_id: Optional Drive file ID
        force: If True, re-process discarded files

    Returns:
        Result dict like import_file_auto
    """
    result = {
        "status": None,
        "type": None,
        "year": None,
        "party": None,
        "employer": None,
        "reason": None,
        "path": None,
    }

    # Extract data from this page
    data = _extract_pdf_auto(page_file)
    if data is None:
        result["status"] = "discarded"
        result["reason"] = f"unreadable (page {page_number})"
        return result

    # Detect record type
    record_type = detect_record_type_from_data(data)
    if not record_type:
        result["status"] = "discarded"
        result["reason"] = f"not_recognized (page {page_number})"
        return result

    result["type"] = record_type

    # Extract year
    year = extract_year_from_data(data, record_type)
    if not year:
        result["status"] = "discarded"
        result["reason"] = f"no_year_detected (page {page_number})"
        return result

    result["year"] = year

    # Extract employer and detect party
    employer = extract_employer_from_data(data, record_type)
    result["employer"] = employer

    party = detect_party_from_employer(employer)
    if not party:
        result["status"] = "discarded"
        result["reason"] = f"unknown_party (page {page_number}, employer: {employer or 'not found'})"
        return result

    result["party"] = party

    # Build metadata with source file and page number
    meta = {
        "type": record_type,
        "year": year,
        "party": party,
        "source_filename": source_filename,
        "source_page": page_number,
    }
    if drive_file_id:
        meta["drive_file_id"] = drive_file_id
    if data.get("_extraction_method"):
        meta["extraction_method"] = data.pop("_extraction_method")

    # Validate and save
    try:
        path, warnings = validate_and_add_record(meta=meta, data=data)
        result["status"] = "imported"
        result["path"] = str(path)
        if warnings:
            result["warnings"] = warnings
            for w in warnings:
                logger.warning(f"{source_filename} (page {page_number}): {w}")
        return result

    except ValidationError as e:
        if any("duplicate" in err.lower() for err in e.errors):
            result["status"] = "skipped"
            result["reason"] = "duplicate"
        else:
            result["status"] = "discarded"
            result["reason"] = f"validation_failed (page {page_number}): {'; '.join(e.errors)}"
        return result


def _compute_taxable_wages(data: Dict[str, Any]) -> float:
    """Compute FICA taxable wages from earnings minus Section 125 deductions.

    FICA taxable wages = sum(earnings) - sum(section_125_deductions)

    Section 125 deductions (reduce FICA wages):
    - Health insurance (medical, dental, vision)
    - FSA (Flexible Spending Account)
    - HSA (Health Savings Account)

    NOT Section 125 (do NOT reduce FICA wages):
    - 401k contributions (only reduce federal income tax)
    - After-tax deductions

    Returns:
        Computed FICA taxable wages, or 0.0 if no earnings found
    """
    # Sum all earnings (includes non-cash fringe like EEGTL)
    earnings = data.get("earnings", [])
    earnings_total = 0.0
    if isinstance(earnings, list):
        for item in earnings:
            amount = item.get("current_amount") or item.get("current") or 0.0
            if isinstance(amount, (int, float)):
                earnings_total += amount

    # Fallback: use gross from pay_summary if no earnings
    if earnings_total == 0.0:
        pay_summary = data.get("pay_summary", {})
        current = pay_summary.get("current", {})
        earnings_total = current.get("gross", 0.0) or 0.0

    if earnings_total == 0.0:
        return 0.0

    # Sum Section 125 deductions
    section_125_total = 0.0
    deductions = data.get("deductions", [])

    # Section 125 keywords (case-insensitive matching)
    # Note: 401k is pre-federal-income-tax but NOT pre-FICA, so we exclude "pretax"
    section_125_keywords = [
        "medical", "dental", "vision", "fsa", "hsa",
        "flex", "cafeteria", "section 125"
    ]

    if isinstance(deductions, list):
        for item in deductions:
            ded_type = (item.get("type") or item.get("name") or "").lower()
            amount = item.get("current_amount") or item.get("current") or 0.0

            # Check if this is a Section 125 deduction
            is_section_125 = any(kw in ded_type for kw in section_125_keywords)

            # Explicitly NOT Section 125
            if "401k" in ded_type or "401(k)" in ded_type or "retirement" in ded_type:
                is_section_125 = False

            if is_section_125 and isinstance(amount, (int, float)):
                section_125_total += abs(amount)

    elif isinstance(deductions, dict):
        # OCR format: {"health_insurance": {"current": 100}, ...}
        for ded_name, ded_vals in deductions.items():
            ded_type = ded_name.lower()
            if isinstance(ded_vals, dict):
                amount = ded_vals.get("current") or ded_vals.get("current_amount") or 0.0
            else:
                amount = ded_vals or 0.0

            is_section_125 = any(kw in ded_type for kw in section_125_keywords)
            if "401k" in ded_type or "retirement" in ded_type:
                is_section_125 = False

            if is_section_125 and isinstance(amount, (int, float)):
                section_125_total += abs(amount)

    fica_taxable = earnings_total - section_125_total
    logger.debug(f"FICA taxable: {earnings_total:.2f} earnings - {section_125_total:.2f} sec125 = {fica_taxable:.2f}")
    return round(fica_taxable, 2)


def _validate_taxable_wages(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate or populate taxable_wages for each tax type.

    Computes expected taxable_wages from earnings sum, then:
    - If taxable_wages missing/zero: populate with computed value (debug log)
    - If taxable_wages present: validate against computed (debug if match, warning if not)

    Args:
        data: Extracted stub data dict

    Returns:
        Modified data dict with taxable_wages populated/validated
    """
    # Only process pay stubs, not W-2s
    if "pay_date" not in data:
        return data

    computed = _compute_taxable_wages(data)
    if computed == 0.0:
        logger.debug("taxable_wages: no earnings to compute from")
        return data

    taxes = data.get("taxes", {})
    if not taxes:
        return data

    # Tax types to check (Medicare is primary, others should match)
    tax_types = ["medicare", "social_security", "federal_income", "federal_income_tax"]

    for tax_type in tax_types:
        tax_data = taxes.get(tax_type, {})
        if not tax_data:
            continue

        existing = tax_data.get("taxable_wages", 0.0) or 0.0

        if existing == 0.0:
            # Populate with computed value
            tax_data["taxable_wages"] = computed
            logger.debug(f"taxable_wages: populated {tax_type} with computed {computed}")
        else:
            # Validate against computed
            tolerance = 1.0  # Allow $1 rounding difference
            if abs(existing - computed) <= tolerance:
                logger.debug(f"taxable_wages: {tax_type} validated ({existing} == {computed})")
            else:
                logger.warning(
                    f"taxable_wages: {tax_type} mismatch - stub has {existing}, "
                    f"computed {computed} (keeping stub value)"
                )

    return data


def _extract_pdf_auto(pdf_path: Path) -> Optional[Dict[str, Any]]:
    """Extract data from PDF with auto-detection.

    Tries text extraction first, falls back to OCR if needed.

    Returns:
        Parsed data dict or None if extraction failed
    """
    logger.debug(f"_extract_pdf_auto: {pdf_path.name}")

    try:
        import PyPDF2

        # Try text extraction
        pdf_text = ""
        try:
            with open(pdf_path, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                for page in reader.pages:
                    pdf_text += page.extract_text() or ""
        except Exception as e:
            logger.debug(f"PyPDF2 extract failed: {e}")
            pdf_text = ""

        logger.debug(f"extracted {len(pdf_text)} chars of text")

        if pdf_text.strip():
            # Text-based PDF - detect type and parse
            logger.debug("calling _parse_text_pdf_auto")
            result = _parse_text_pdf_auto(pdf_text, pdf_path)
            if result:
                result["_extraction_method"] = "text"
                return _validate_taxable_wages(result)
            # Text parsing failed - fall back to OCR
            logger.debug("text parsing returned None, falling back to OCR")

        # Image-based PDF or text parsing failed - use OCR
        logger.debug("calling _extract_pdf_ocr")
        ocr_result = _extract_pdf_ocr(pdf_path)
        if ocr_result:
            return _validate_taxable_wages(ocr_result)
        return None

    except ImportError:
        return None
    except Exception:
        return None


def _get_pdf_page_count(pdf_path: Path) -> int:
    """Get the number of pages in a PDF file.

    Returns:
        Number of pages, or 0 if file can't be read
    """
    try:
        import PyPDF2
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            return len(reader.pages)
    except Exception:
        return 0


def _split_pdf_pages(pdf_path: Path, output_dir: Path) -> List[Path]:
    """Split a multi-page PDF into individual page files.

    Args:
        pdf_path: Path to the multi-page PDF
        output_dir: Directory to write individual page files

    Returns:
        List of paths to individual page PDFs
    """
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(pdf_path)
        page_files = []
        base_name = pdf_path.stem

        for i, page in enumerate(reader.pages):
            writer = PyPDF2.PdfWriter()
            writer.add_page(page)

            page_file = output_dir / f"{base_name}_page_{i+1:02d}.pdf"
            with open(page_file, "wb") as f:
                writer.write(f)
            page_files.append(page_file)

        return page_files
    except Exception as e:
        logger.debug(f"Failed to split PDF: {e}")
        return []


def _parse_text_pdf_auto(pdf_text: str, pdf_path: Path) -> Optional[Dict[str, Any]]:
    """Parse text-based PDF with auto type detection.

    Args:
        pdf_text: Extracted text from PDF
        pdf_path: Path to PDF file

    Returns:
        Parsed data dict or None
    """
    filename = pdf_path.name

    # Detect type from text
    text_lower = pdf_text.lower()

    is_w2 = any(indicator in text_lower for indicator in [
        "wage and tax statement", "form w-2", "w-2", "box 1", "box 2",
        "employer identification", "employee's social security"
    ])

    is_stub = any(indicator in text_lower for indicator in [
        "pay period", "pay date", "gross pay", "net pay", "ytd",
        "earnings statement", "pay stub", "direct deposit"
    ])

    # Filename hints can help break ties
    fname_lower = filename.lower()
    if "w2" in fname_lower or "w-2" in fname_lower:
        is_w2 = True
    if "stub" in fname_lower or "pay" in fname_lower:
        is_stub = True

    logger.debug(f"type detection: is_w2={is_w2}, is_stub={is_stub}")

    if is_w2 and not is_stub:
        # Parse as W-2 - no text parser yet, fall back to OCR
        logger.debug("W-2 detected, will fall back to OCR")
        return None

    elif is_stub and not is_w2:
        # Parse as stub - detect processor from config-driven keywords
        logger.debug("attempting stub text parse")
        try:
            from processors import get_processor
            from .config import load_config

            # Load config and search for matching employer keywords
            processor_name = "generic"
            employer = "Unknown"
            detected_party = None

            try:
                config = load_config(require_exists=False)
                parties = config.get("parties", {})

                # Normalize text once for keyword matching
                normalized_text = text_lower.replace(" ", "")

                # Search all parties/companies for keyword match
                for party_name, party_config in parties.items():
                    companies = party_config.get("companies", [])
                    for company in companies:
                        keywords = company.get("keywords", [])
                        for keyword in keywords:
                            if keyword.lower().replace(" ", "") in normalized_text:
                                processor_name = company.get("paystub_processor", "generic")
                                employer = company.get("name", "Unknown")
                                detected_party = party_name
                                logger.debug(f"keyword match '{keyword}' → {employer} ({party_name})")
                                break
                        if detected_party:
                            break
                    if detected_party:
                        break
            except Exception as e:
                logger.debug(f"config load failed, using defaults: {e}")

            logger.debug(f"detected processor={processor_name}, employer={employer}, party={detected_party}")
            processor_class = get_processor(processor_name)
            result = processor_class.process(str(pdf_path), employer)
            if result and result.get("pay_date"):
                logger.debug(f"text parse successful: {result.get('pay_date')}")
                return result
        except Exception as e:
            logger.debug(f"text parse failed: {e}")
        # Text parsing failed - return None to trigger OCR fallback
        logger.debug("stub text parse failed, will fall back to OCR")
        return None

    # Can't determine type from text - return None to trigger OCR
    logger.debug("can't determine type from text, will fall back to OCR")
    return None


def _extract_pdf_ocr(pdf_path: Path) -> Optional[Dict[str, Any]]:
    """Extract data from image-based PDF using Gemini OCR.

    Uses unified prompt that detects type AND extracts data in one call.

    Returns:
        Parsed data dict with type indicator, or None
    """
    logger.debug(f"_extract_pdf_ocr starting for {pdf_path.name}")

    try:
        from gemini_client import process_file
        logger.debug("calling gemini_client.process_file...")

        # Unified prompt for type detection + extraction
        prompt = """Analyze this document and extract data.

First, identify the document type:
- PAY STUB: Contains pay period, pay date, gross/net pay, YTD totals
- W-2 (Wage and Tax Statement): Contains tax year, boxes 1-17, employer EIN

Then extract the relevant data into JSON format:

For PAY STUB:
{
  "pay_date": "YYYY-MM-DD",
  "employer": "company name",
  "net_pay": 0.00,
  "pay_summary": {
    "current": {"gross": 0.00},
    "ytd": {"gross": 0.00}
  },
  "earnings": [
    {"type": "description", "current_amount": 0.00, "ytd_amount": 0.00}
  ],
  "taxes": {
    "federal_income": {"current": 0.00, "ytd": 0.00},
    "social_security": {"current": 0.00, "ytd": 0.00},
    "medicare": {"current": 0.00, "ytd": 0.00},
    "state": {"current": 0.00, "ytd": 0.00}
  },
  "deductions": {
    "retirement_401k": {"current": 0.00, "ytd": 0.00},
    "health_insurance": {"current": 0.00, "ytd": 0.00},
    "dental_vision": {"current": 0.00, "ytd": 0.00},
    "other_pretax": {"current": 0.00, "ytd": 0.00}
  }
}

IMPORTANT:
- Extract ALL earnings including "Non-Cash Fringe" items (EEGTL, Group Term Life, imputed income)
- Extract ALL deductions (401k, health, dental, FSA, HSA, etc)
- The math should work: gross - taxes - deductions ≈ net_pay

For W-2:
{
  "tax_year": 2024,
  "employer_name": "company name",
  "employer_ein": "XX-XXXXXXX",
  "wages": 0.00,
  "federal_tax_withheld": 0.00,
  "social_security_wages": 0.00,
  "social_security_tax": 0.00,
  "medicare_wages": 0.00,
  "medicare_tax": 0.00,
  "state": "XX",
  "state_wages": 0.00,
  "state_tax_withheld": 0.00
}

If unable to identify as either type, return: {"_unrecognized": true}

Return ONLY the JSON object, no explanation."""

        data = process_file(prompt, str(pdf_path))
        if data.get("_unrecognized"):
            return None
        data["_extraction_method"] = "ocr"
        return data

    except ImportError:
        return None
    except Exception:
        return None


def _save_tracking(
    source_filename: str,
    drive_file_id: Optional[str],
    reason: str
) -> Path:
    """Save a tracking marker for a file that wasn't imported.

    Args:
        source_filename: Original filename
        drive_file_id: Drive file ID if from Drive
        reason: Why file wasn't imported

    Returns:
        Path to saved tracking marker

    Tracking types:
        - "unrelated": File is not a pay stub or W-2 (normal, expected)
        - "discarded": File IS a pay stub but has actionable issues (unknown_party, parse_failed)
    """
    # Determine tracking type based on reason
    if reason in ("not_recognized", "all_duplicates"):
        tracking_type = "unrelated"
    else:
        tracking_type = "discarded"

    meta = {
        "type": tracking_type,
        "source_filename": source_filename,
        "skip_reason": reason,
    }
    if drive_file_id:
        meta["drive_file_id"] = drive_file_id

    return add_record(meta, None)


def import_from_folder_auto(
    source: str,
    callback: Optional[callable] = None,
    force: bool = False,
    debug: bool = False
) -> Dict[str, Any]:
    """Import records from a folder with full auto-detection.

    Processes all JSON and PDF files, auto-detecting type, year, and party.

    Args:
        source: Drive folder ID or local folder path
        callback: Progress callback(event, data)
        force: Re-process previously discarded files
        debug: Include detailed debug info

    Returns:
        Stats dict with: imported, skipped, discarded, errors, stubs, w2s
    """
    import tempfile

    stats = {
        "imported": 0,
        "skipped": 0,
        "discarded": 0,
        "errors": 0,
        "stubs": 0,
        "w2s": 0,
    }

    def emit(event: str, data: dict = None):
        if callback:
            callback(event, data or {})

    if is_drive_folder_id(source):
        # Import from Google Drive
        try:
            import subprocess
            result = subprocess.run(
                ["gwsa", "drive", "list", "--folder-id", source],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                raise RuntimeError(f"gwsa failed: {result.stderr}")

            files_info = json.loads(result.stdout).get("items", [])
        except FileNotFoundError:
            raise RuntimeError("gwsa not installed - required for Drive imports")
        except Exception as e:
            raise RuntimeError(f"Failed to list Drive folder: {e}")

        # Filter to processable files
        processable = [f for f in files_info
                       if f.get("name", "").lower().endswith((".json", ".pdf"))]

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            emit("start", {"source": source, "file_count": len(processable)})

            for file_info in processable:
                name = file_info.get("name", "")
                file_id = file_info.get("id", "")

                # Check if already processed BEFORE downloading (efficiency)
                # For folder imports: skip ANY file we've seen before (imported, discarded, or unrelated)
                # Only targeted imports (by file ID) should re-process
                if not force:
                    existing_records = find_all_by_drive_id(file_id)
                    if existing_records:
                        # Categorize existing records by type
                        tracking_types = ("discarded", "unrelated")
                        discarded = [r for r in existing_records
                                     if r.get("meta", {}).get("type") == "discarded"]
                        unrelated = [r for r in existing_records
                                     if r.get("meta", {}).get("type") == "unrelated"]
                        imported = [r for r in existing_records
                                    if r.get("meta", {}).get("type") not in tracking_types]

                        if imported:
                            logger.debug(f"skipping {name} (already imported, {len(imported)} records)")
                            stats["skipped"] += len(imported)
                            emit("skipped", {"name": name, "reason": "already imported"})
                        elif unrelated:
                            # Non-pay file - just info, no warning
                            logger.debug(f"skipping {name} (unrelated file)")
                            stats["skipped"] += 1
                            emit("skipped", {"name": name, "reason": "unrelated"})
                        elif discarded:
                            # Pay stub that couldn't be processed - check if actionable
                            skip_reason = discarded[0].get("meta", {}).get("skip_reason", "")
                            logger.debug(f"skipping {name} (previously discarded: {skip_reason})")
                            stats["skipped"] += 1
                            emit("skipped", {"name": name, "reason": "previously discarded"})

                            # Emit warning for actionable discard reasons
                            if skip_reason.startswith("unknown_party"):
                                emit("warning", {
                                    "name": name,
                                    "file_id": file_id,
                                    "message": f"Unknown employer - update profile.yaml with employer keywords, then run: records import file {file_id}"
                                })
                        continue

                logger.debug(f"downloading {name}...")

                local_path = tmp_path / name
                try:
                    subprocess.run(
                        ["gwsa", "drive", "download", file_id, str(local_path)],
                        capture_output=True, text=True, check=True
                    )
                except subprocess.CalledProcessError as e:
                    emit("error", {"name": name, "error": str(e)})
                    stats["errors"] += 1
                    continue

                logger.debug(f"downloaded {name}, processing...")

                # Import with auto-detection (handles multi-page PDFs)
                results = import_file_auto_all(local_path, force=force, drive_file_id=file_id)
                for result in results:
                    page_suffix = f" (page {result['page']})" if result.get('page') else ""
                    _accumulate_file_result(stats, result, f"{name}{page_suffix}", emit)

                logger.debug(f"{name} result: {len(results)} record(s)")

    else:
        # Local folder
        folder_path = Path(source)
        if not folder_path.is_dir():
            raise ValueError(f"Not a directory: {source}")

        files = sorted(list(folder_path.glob("*.json")) + list(folder_path.glob("*.pdf")))
        emit("start", {"source": source, "file_count": len(files)})

        for file_path in files:
            # Import with auto-detection (handles multi-page PDFs)
            results = import_file_auto_all(file_path, force=force)
            for result in results:
                page_suffix = f" (page {result['page']})" if result.get('page') else ""
                _accumulate_file_result(stats, result, f"{file_path.name}{page_suffix}", emit)

    emit("done", stats)
    return stats


def _accumulate_file_result(stats: dict, result: dict, name: str, emit: callable):
    """Accumulate import result into stats and emit events."""
    status = result.get("status")
    rec_type = result.get("type")

    if status == "imported":
        stats["imported"] += 1
        if rec_type == "stub":
            stats["stubs"] += 1
        elif rec_type == "w2":
            stats["w2s"] += 1
        emit("imported", {
            "name": name,
            "type": rec_type,
            "year": result.get("year"),
            "party": result.get("party"),
            "employer": result.get("employer"),
        })

    elif status == "skipped":
        stats["skipped"] += 1
        emit("skipped", {"name": name, "reason": result.get("reason")})

    elif status == "discarded":
        stats["discarded"] += 1
        emit("discarded", {"name": name, "reason": result.get("reason")})


# =============================================================================
# TARGETED FILE IMPORT (for recovery workflows)
# =============================================================================

def import_from_drive_file(
    file_id: str,
    force: bool = False,
    callback: Optional[callable] = None
) -> Dict[str, Any]:
    """Import a specific Drive file by ID, bypassing file-level dedup.

    This is the recovery workflow for re-importing a specific file. Unlike folder
    imports which skip files that have any existing records, this function:
    - Always downloads and processes the file
    - Uses stub-level duplicate detection to avoid creating duplicate records
    - Imports only genuinely new stubs from the file

    Use cases:
    - File was updated on Drive with new stubs (e.g., quarterly PDF with new month)
    - Previous import had issues that are now fixed
    - Re-processing after config changes (e.g., new employer keywords)

    Args:
        file_id: Google Drive file ID to import
        force: If True, also re-process previously discarded files
        callback: Progress callback(event, data)

    Returns:
        Stats dict with: imported, skipped, discarded, errors, stubs, w2s
    """
    import subprocess
    import tempfile

    stats = {
        "imported": 0,
        "skipped": 0,
        "discarded": 0,
        "errors": 0,
        "stubs": 0,
        "w2s": 0,
    }

    def emit(event: str, data: dict = None):
        if callback:
            callback(event, data or {})

    # Get file metadata from Drive
    try:
        # Use gwsa to get file info (we need the filename)
        result = subprocess.run(
            ["gwsa", "drive", "get", file_id],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            # Fallback: just use the file_id as name
            filename = f"{file_id}.pdf"
        else:
            file_info = json.loads(result.stdout)
            filename = file_info.get("name", f"{file_id}.pdf")
    except (FileNotFoundError, json.JSONDecodeError):
        filename = f"{file_id}.pdf"

    emit("start", {"source": file_id, "file_count": 1})
    emit("file", {"name": filename})

    # Download to temp directory
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        local_path = tmp_path / filename

        try:
            subprocess.run(
                ["gwsa", "drive", "download", file_id, str(local_path)],
                capture_output=True, text=True, check=True
            )
        except subprocess.CalledProcessError as e:
            emit("error", {"name": filename, "error": str(e)})
            stats["errors"] += 1
            emit("done", stats)
            return stats
        except FileNotFoundError:
            raise RuntimeError("gwsa not installed - required for Drive imports")

        # Import with targeted=True to bypass file-level dedup
        # This ensures stub-level dedup is used for each stub in the file
        results = import_file_auto_all(
            local_path,
            force=force,
            drive_file_id=file_id,
            targeted=True
        )

        for result in results:
            page_suffix = f" (page {result['page']})" if result.get('page') else ""
            _accumulate_file_result(stats, result, f"{filename}{page_suffix}", emit)

    emit("done", stats)
    return stats


# =============================================================================
# LEGACY IMPORT FUNCTIONS (with explicit year/party/type)
# =============================================================================

def _import_single_file(
    file_path: Path,
    year: str,
    party: str,
    record_type: RecordType,
    drive_file_id: Optional[str] = None
) -> Dict[str, Any]:
    """Import a single file (JSON or PDF) with explicit metadata.

    LEGACY: Use import_file_auto() for new code.

    For PDFs, extracts data using text extraction or Gemini OCR.
    For JSON, loads directly.

    Returns:
        Dict with: imported (0/1), skipped (0/1), errors (0/1),
                   warnings (list), error_msg (str if error)
    """
    result = {"imported": 0, "skipped": 0, "errors": 0, "warnings": [], "error_msg": None}

    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        # Extract data from PDF
        data = process_pdf_to_json(file_path, record_type, party)
        if data is None:
            result["errors"] = 1
            result["error_msg"] = "Failed to extract data from PDF"
            return result
    elif suffix == ".json":
        try:
            with open(file_path) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            result["errors"] = 1
            result["error_msg"] = f"Invalid JSON: {e}"
            return result
        except IOError as e:
            result["errors"] = 1
            result["error_msg"] = f"Read error: {e}"
            return result
    else:
        result["errors"] = 1
        result["error_msg"] = f"Unsupported file type: {suffix}"
        return result

    meta = {
        "type": record_type,
        "year": year,
        "party": party,
        "source_filename": file_path.name,
    }
    if drive_file_id:
        meta["drive_file_id"] = drive_file_id

    try:
        path, warnings = validate_and_add_record(meta=meta, data=data)
        result["imported"] = 1
        result["warnings"] = warnings
    except ValidationError as e:
        # Check if it's a duplicate error (skip) vs other error
        if any("duplicate" in err.lower() for err in e.errors):
            result["skipped"] = 1
        else:
            result["errors"] = 1
            result["error_msg"] = "; ".join(e.errors)
    except ValueError as e:
        result["errors"] = 1
        result["error_msg"] = str(e)

    return result
