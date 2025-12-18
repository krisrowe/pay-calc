"""
Records management for pay stubs and W-2s.

Design Rationale
----------------

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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Literal

from .config import get_data_path


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

    # Extract taxes
    taxes = data.get("taxes", {})
    normalized["federal_tax"] = (taxes.get("federal_income", {}).get("current")
                                  or taxes.get("federal", {}).get("current") or 0)
    normalized["state_tax"] = taxes.get("state", {}).get("current") or 0
    normalized["social_security"] = taxes.get("social_security", {}).get("current") or 0
    normalized["medicare"] = taxes.get("medicare", {}).get("current") or 0

    # YTD values
    normalized["ytd_gross"] = ytd.get("gross") or 0
    normalized["ytd_federal_tax"] = taxes.get("federal_income", {}).get("ytd") or 0
    normalized["ytd_state_tax"] = taxes.get("state", {}).get("ytd") or 0
    normalized["ytd_social_security"] = taxes.get("social_security", {}).get("ytd") or 0
    normalized["ytd_medicare"] = taxes.get("medicare", {}).get("ytd") or 0

    # Sum other deductions
    other_deductions = 0
    for deduction in data.get("deductions", []):
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

    Checks:
    - gross - deductions ≈ net (within $1 tolerance for rounding)
    - YTD values >= current period values
    """
    errors = []

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

    # gross - all deductions should equal net (allow $1 tolerance)
    calculated_net = gross - federal - state - ss - medicare - other
    if abs(calculated_net - net) > 1.0:
        errors.append(
            f"math mismatch: gross({gross}) - deductions({federal}+{state}+{ss}+{medicare}+{other}) "
            f"= {calculated_net:.2f}, but net_pay = {net}"
        )

    # YTD validations: ytd should be >= current period
    ytd_checks = [
        ("ytd_gross", "gross_pay"),
        ("ytd_federal_tax", "federal_tax"),
        ("ytd_state_tax", "state_tax"),
        ("ytd_social_security", "social_security"),
        ("ytd_medicare", "medicare"),
    ]

    for ytd_field, period_field in ytd_checks:
        ytd_val = data.get(ytd_field)
        period_val = data.get(period_field)
        if ytd_val is not None and period_val is not None:
            if ytd_val < period_val:
                errors.append(f"YTD value {ytd_field}({ytd_val}) < period value {period_field}({period_val})")

    return errors


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
) -> List[str]:
    """Run full validation pipeline on a record.

    Args:
        record_type: "stub" or "w2"
        data: The record data
        meta: The record metadata (for duplicate checking)
        check_duplicate: Whether to check for existing records

    Returns:
        List of error messages (empty if valid)
    """
    errors = []

    if record_type == "discarded":
        return errors  # No validation for discarded records

    if data is None:
        errors.append("data cannot be None for non-discarded records")
        return errors

    # Schema validation
    if record_type == "stub":
        # Normalize nested format to flat for validation
        flat_data = normalize_stub_data(data)
        errors.extend(_validate_schema(flat_data, STUB_SCHEMA, "stub"))
        if not errors:  # Only check math if schema is valid
            errors.extend(_validate_stub_math(flat_data))
        # Date format check
        if "pay_date" in data:
            errors.extend(_validate_date_format(data["pay_date"], "pay_date"))
        if "pay_period_start" in data:
            errors.extend(_validate_date_format(data["pay_period_start"], "pay_period_start"))
        if "pay_period_end" in data:
            errors.extend(_validate_date_format(data["pay_period_end"], "pay_period_end"))

    elif record_type == "w2":
        errors.extend(_validate_schema(data, W2_SCHEMA, "w2"))
        if not errors:  # Only check math if schema is valid
            errors.extend(_validate_w2_math(data))
        # Year should be reasonable
        tax_year = data.get("tax_year")
        if tax_year:
            try:
                year_int = int(tax_year)
                if year_int < 2000 or year_int > 2100:
                    errors.append(f"tax_year out of range: {tax_year}")
            except (ValueError, TypeError):
                errors.append(f"tax_year not a valid year: {tax_year}")

    # Duplicate check (by drive_file_id)
    if check_duplicate and meta.get("drive_file_id"):
        existing = find_by_drive_id(meta["drive_file_id"])
        if existing:
            errors.append(f"duplicate: record with drive_file_id already exists (id={existing['id']})")

    return errors


def validate_and_add_record(
    meta: Dict[str, Any],
    data: Optional[Dict[str, Any]],
    skip_validation: bool = False
) -> Path:
    """Validate a record and add it if valid.

    This is the primary entry point for importing records.

    Args:
        meta: Record metadata
        data: Record data
        skip_validation: Skip validation (use with caution)

    Returns:
        Path to the saved record

    Raises:
        ValidationError: If validation fails
        ValueError: If required metadata is missing
    """
    record_type = meta.get("type")

    if not skip_validation and record_type != "discarded":
        errors = validate_record(record_type, data, meta, check_duplicate=True)
        if errors:
            raise ValidationError(errors)

    return add_record(meta, data)


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


def _generate_record_id(meta: Dict[str, Any]) -> str:
    """Generate a unique record ID from metadata.

    Uses drive_file_id if available, otherwise hashes source_filename + imported_at.
    Returns first 8 chars of hash for brevity.
    """
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

                # Skip discarded unless requested
                if record_type == "discarded" and not include_discarded:
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
        return data.get("pay_date") or data.get("tax_year") or ""

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
    if record_type not in ("stub", "w2", "discarded"):
        raise ValueError(f"meta.type must be 'stub', 'w2', or 'discarded', got: {record_type}")

    # Ensure imported_at is set
    if "imported_at" not in meta:
        meta["imported_at"] = datetime.now().isoformat()

    # Generate record ID
    record_id = _generate_record_id(meta)

    # Determine storage path
    records_dir = get_records_dir()

    if record_type == "discarded":
        target_dir = records_dir / "_discarded"
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
