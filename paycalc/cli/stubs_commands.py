"""Stubs command group for managing pay stub JSON files."""

import hashlib
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

import click

from paycalc.sdk import get_data_path, detect_gaps


def get_stubs_dir(year: str, party: str) -> Path:
    """Get the stubs directory for a year/party."""
    return get_data_path() / "stubs" / year / party


def load_stub(stub_path: Path) -> Dict[str, Any]:
    """Load a stub JSON file."""
    with open(stub_path) as f:
        return json.load(f)


def save_stub(stub: Dict[str, Any], stubs_dir: Path) -> Path:
    """Save a stub to the stubs directory, named by pay date."""
    pay_date = stub.get("pay_date", "unknown")
    stub_path = stubs_dir / f"{pay_date}.json"
    with open(stub_path, "w") as f:
        json.dump(stub, f, indent=2)
    return stub_path


def get_stub_key(stub: Dict[str, Any]) -> Tuple[str, str, float]:
    """Get a unique key for duplicate detection: (pay_date, employer, gross)."""
    pay_date = stub.get("pay_date", "")
    employer = stub.get("employer", "")

    # Try to get gross from different locations in stub structure
    gross = 0.0
    if "pay_summary" in stub and "current" in stub["pay_summary"]:
        gross = stub["pay_summary"]["current"].get("gross", 0.0)
    elif "earnings" in stub:
        for earning in stub.get("earnings", []):
            if earning.get("type", "").lower() in ["gross pay", "gross"]:
                gross = earning.get("current_amount", 0.0)
                break

    return (pay_date, employer, gross)


def is_duplicate(stub: Dict[str, Any], existing_stubs: List[Dict[str, Any]]) -> bool:
    """Check if stub is a duplicate of any existing stub."""
    new_key = get_stub_key(stub)
    for existing in existing_stubs:
        if get_stub_key(existing) == new_key:
            return True
    return False


def load_existing_stubs(stubs_dir: Path) -> List[Dict[str, Any]]:
    """Load all existing stubs from the stubs directory."""
    stubs = []
    if stubs_dir.exists():
        for stub_path in stubs_dir.glob("*.json"):
            try:
                stubs.append(load_stub(stub_path))
            except (json.JSONDecodeError, IOError):
                pass
    return stubs


def validate_stub_schema(stub: Dict[str, Any]) -> List[str]:
    """Validate stub has required fields. Returns list of errors."""
    errors = []
    required = ["pay_date", "employer"]
    for field in required:
        if field not in stub or not stub[field]:
            errors.append(f"Missing required field: {field}")

    # Validate pay_date format
    if "pay_date" in stub:
        try:
            datetime.strptime(stub["pay_date"], "%Y-%m-%d")
        except ValueError:
            errors.append(f"Invalid pay_date format: {stub['pay_date']} (expected YYYY-MM-DD)")

    return errors


# Pay stub extraction prompt (JSON boilerplate handled by gemini_client)
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
    "federal_income": {"current": 0.00, "ytd": 0.00},
    "social_security": {"current": 0.00, "ytd": 0.00},
    "medicare": {"current": 0.00, "ytd": 0.00},
    "state": {"current": 0.00, "ytd": 0.00}
  },
  "deductions": [
    {"type": "description", "current_amount": 0.00, "ytd_amount": 0.00}
  ],
  "earnings": [
    {"type": "Gross Pay", "current_amount": 0.00, "ytd_amount": 0.00}
  ]
}

Include all deductions (401k, health insurance, etc.) and all earnings types found.
Use 0.00 for any tax fields not present on the stub.
pay_summary.current.taxes should be the sum of all tax withholdings.
"""


def process_pdf_file(pdf_path: Path, party: str) -> Optional[Dict[str, Any]]:
    """Process a PDF file and extract stub data.

    First attempts text extraction using PyPDF2 and processors.
    Falls back to Gemini OCR for image-based PDFs.
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
            pass

        # If no text, use Gemini OCR
        if not pdf_text.strip():
            click.echo(f"  Image-based PDF, using Gemini OCR...")
            try:
                from gemini_client import process_file
                stub = process_file(PAYSTUB_OCR_PROMPT, str(pdf_path))
                stub["_source_file"] = pdf_path.name
                stub["_ocr"] = True
                return stub
            except Exception as e:
                click.echo(f"  Gemini OCR failed: {e}", err=True)
                return None

        # Text-based PDF - use processor
        from analysis import process_single_page
        return process_single_page(str(pdf_path), party)

    except Exception as e:
        click.echo(f"  Error processing PDF {pdf_path.name}: {e}", err=True)
        return None


def validate_stub_numbers(stub: Dict[str, Any]) -> List[str]:
    """Validate that pay stub numbers are consistent and add up correctly.

    Returns list of validation errors (empty if valid).
    """
    # Import the validation function from analysis.py
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    try:
        from analysis import validate_stub_numbers as analysis_validate
        return analysis_validate(stub)
    except ImportError:
        # Fallback basic validation if import fails
        errors = []
        gross = 0.0
        net_pay = stub.get("net_pay", 0) or 0

        if "pay_summary" in stub and "current" in stub["pay_summary"]:
            gross = stub["pay_summary"]["current"].get("gross", 0) or 0
        elif "earnings" in stub:
            for earning in stub.get("earnings", []):
                if earning.get("type", "").lower() in ["gross pay", "gross"]:
                    gross = earning.get("current_amount", 0) or 0
                    break

        if not gross or gross <= 0:
            errors.append("Missing or invalid gross pay")
        if not net_pay or net_pay <= 0:
            errors.append("Missing or invalid net pay")
        if net_pay > gross and gross > 0:
            errors.append(f"Net pay ${net_pay:,.2f} exceeds gross pay ${gross:,.2f}")

        return errors


def is_drive_folder_id(source: str) -> bool:
    """Check if source looks like a Drive folder ID."""
    # Drive folder IDs are typically 33 chars, alphanumeric with - and _
    if len(source) > 20 and "/" not in source and "\\" not in source:
        return all(c.isalnum() or c in "-_" for c in source)
    return False


@click.group()
def stubs():
    """Manage pay stub JSON files.

    Import stubs from PDFs or JSON files, list available stubs,
    and export for backup.
    """
    pass


@stubs.command("import")
@click.argument("year")
@click.argument("party", type=click.Choice(["him", "her"]))
@click.argument("source")
@click.option("--force", is_flag=True, help="Re-import even if duplicate detected")
def stubs_import(year: str, party: str, source: str, force: bool):
    """Import pay stubs from a source.

    SOURCE can be:

    \b
    - Drive folder ID: Downloads and processes all PDF/JSON files
    - Local folder path: Processes all PDF/JSON files in folder
    - Local JSON file: Imports single stub
    - Local PDF file: Extracts single stub

    Duplicates (same date + employer + gross) are skipped with a message.
    """
    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    stubs_dir = get_stubs_dir(year, party)
    stubs_dir.mkdir(parents=True, exist_ok=True)

    existing_stubs = load_existing_stubs(stubs_dir)
    imported = 0
    skipped = 0
    errors = 0

    source_path = Path(source)

    # Determine source type
    if is_drive_folder_id(source):
        # Drive folder
        click.echo(f"Importing from Drive folder: {source}")
        try:
            from drive_sync import list_drive_folder, download_drive_file
            import tempfile

            files = list_drive_folder(source)
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                for file_info in files:
                    name = file_info["name"]
                    if name.endswith(".json") or name.endswith(".pdf"):
                        local_file = tmp_path / name
                        download_drive_file(file_info["id"], str(local_file))

                        # Process the downloaded file
                        result = _process_single_file(local_file, party, existing_stubs, stubs_dir, force)
                        imported += result["imported"]
                        skipped += result["skipped"]
                        errors += result["errors"]
                        if result["stub"]:
                            existing_stubs.append(result["stub"])
        except Exception as e:
            raise click.ClickException(f"Failed to access Drive folder: {e}")

    elif source_path.is_dir():
        # Local folder
        click.echo(f"Importing from local folder: {source}")
        for file_path in sorted(source_path.iterdir()):
            if file_path.suffix.lower() in [".json", ".pdf"]:
                result = _process_single_file(file_path, party, existing_stubs, stubs_dir, force)
                imported += result["imported"]
                skipped += result["skipped"]
                errors += result["errors"]
                if result["stub"]:
                    existing_stubs.append(result["stub"])

    elif source_path.is_file():
        # Single file
        result = _process_single_file(source_path, party, existing_stubs, stubs_dir, force)
        imported += result["imported"]
        skipped += result["skipped"]
        errors += result["errors"]

    else:
        raise click.ClickException(f"Source not found: {source}")

    # Summary
    click.echo("")
    click.echo(f"Import complete: {imported} imported, {skipped} skipped, {errors} errors")


def import_stub(
    stub: Dict[str, Any],
    stubs_dir: Path,
    existing_stubs: List[Dict[str, Any]],
    force: bool = False,
    source_name: str = "unknown"
) -> Dict[str, Any]:
    """Validate and import a stub dict. Single entry point for all sources.

    Args:
        stub: The stub dict to import (from JSON file, PDF extraction, OCR, etc.)
        stubs_dir: Target directory for saving
        existing_stubs: List of already-imported stubs for duplicate detection
        force: If True, import even if duplicate detected
        source_name: Description of source for error messages

    Returns:
        Dict with keys: imported (0/1), skipped (0/1), errors (0/1), stub (if imported)
    """
    result = {"imported": 0, "skipped": 0, "errors": 0, "stub": None}

    # Check for extraction error objects (from Gemini, etc.)
    if stub.get("error"):
        click.echo(f"  Extraction error from {source_name}: {stub.get('message', 'Unknown')}", err=True)
        if stub.get("details"):
            click.echo(f"    Details: {stub['details']}", err=True)
        result["errors"] = 1
        return result

    # Validate schema (required fields, date format)
    schema_errors = validate_stub_schema(stub)
    if schema_errors:
        for err in schema_errors:
            click.echo(f"  Schema error in {source_name}: {err}", err=True)
        result["errors"] = 1
        return result

    # Validate numbers add up
    number_errors = validate_stub_numbers(stub)
    if number_errors:
        click.echo(f"  Warning: Number validation issues in {source_name}:")
        for err in number_errors:
            click.echo(f"    - {err}")
        # Continue - warnings don't block import

    # Check for duplicates
    if not force and is_duplicate(stub, existing_stubs):
        key = get_stub_key(stub)
        click.echo(f"  Skipping duplicate: {key[0]} {key[1]} ${key[2]:,.2f}")
        result["skipped"] = 1
        return result

    # Save to disk
    stub_path = save_stub(stub, stubs_dir)
    click.echo(f"  Imported: {stub_path.name}")
    result["imported"] = 1
    result["stub"] = stub
    return result


def _extract_stub_from_file(file_path: Path, party: str) -> Optional[Dict[str, Any]]:
    """Extract stub dict from a file. Returns None if extraction fails."""
    if file_path.suffix.lower() == ".json":
        return load_stub(file_path)

    elif file_path.suffix.lower() == ".pdf":
        click.echo(f"  Processing PDF: {file_path.name}")
        return process_pdf_file(file_path, party)

    return None


def _process_single_file(
    file_path: Path,
    party: str,
    existing_stubs: List[Dict[str, Any]],
    stubs_dir: Path,
    force: bool
) -> Dict[str, Any]:
    """Extract from file and import. Convenience wrapper for file-based sources."""
    try:
        stub = _extract_stub_from_file(file_path, party)
        if stub is None:
            if file_path.suffix.lower() in [".json", ".pdf"]:
                click.echo(f"  Could not extract from: {file_path.name}", err=True)
                return {"imported": 0, "skipped": 0, "errors": 1, "stub": None}
            return {"imported": 0, "skipped": 0, "errors": 0, "stub": None}

        return import_stub(stub, stubs_dir, existing_stubs, force, file_path.name)

    except Exception as e:
        click.echo(f"  Error processing {file_path.name}: {e}", err=True)
        return {"imported": 0, "skipped": 0, "errors": 1, "stub": None}


def parse_year_party_filters(filters: Tuple[str, ...]) -> Tuple[Optional[str], Optional[str]]:
    """Parse flexible year/party filters.

    Args:
        filters: 0-2 arguments that can be year (4 digits) or party (him/her)

    Returns:
        (year, party) tuple - either can be None if not specified
    """
    year = None
    party = None
    valid_parties = ["him", "her"]

    for f in filters:
        if f.isdigit() and len(f) == 4:
            if year is not None:
                raise click.BadParameter(f"Multiple years specified: {year} and {f}")
            year = f
        elif f.lower() in valid_parties:
            if party is not None:
                raise click.BadParameter(f"Multiple parties specified: {party} and {f}")
            party = f.lower()
        else:
            raise click.BadParameter(
                f"Invalid filter '{f}'. Expected 4-digit year or party (him/her)."
            )

    return year, party


def get_stubs_dirs_for_filters(
    year: Optional[str],
    party: Optional[str]
) -> List[Tuple[str, str, Path]]:
    """Get list of (year, party, path) tuples for given filters."""
    base_dir = get_data_path() / "stubs"
    results = []

    if year and party:
        # Specific year and party
        path = base_dir / year / party
        if path.exists():
            results.append((year, party, path))
    elif year:
        # Specific year, all parties
        year_dir = base_dir / year
        if year_dir.exists():
            for party_dir in sorted(year_dir.iterdir()):
                if party_dir.is_dir() and party_dir.name in ["him", "her"]:
                    results.append((year, party_dir.name, party_dir))
    elif party:
        # All years, specific party
        if base_dir.exists():
            for year_dir in sorted(base_dir.iterdir()):
                if year_dir.is_dir() and year_dir.name.isdigit():
                    party_dir = year_dir / party
                    if party_dir.exists():
                        results.append((year_dir.name, party, party_dir))
    else:
        # All years, all parties
        if base_dir.exists():
            for year_dir in sorted(base_dir.iterdir()):
                if year_dir.is_dir() and year_dir.name.isdigit():
                    for party_dir in sorted(year_dir.iterdir()):
                        if party_dir.is_dir() and party_dir.name in ["him", "her"]:
                            results.append((year_dir.name, party_dir.name, party_dir))

    return results


def get_stub_gross(stub: Dict[str, Any]) -> float:
    """Extract gross pay from stub."""
    if "pay_summary" in stub and "current" in stub["pay_summary"]:
        return stub["pay_summary"]["current"].get("gross", 0.0)
    elif "earnings" in stub:
        for earning in stub.get("earnings", []):
            if earning.get("type", "").lower() in ["gross pay", "gross"]:
                return earning.get("current_amount", 0.0)
    return 0.0


def get_stub_id(stub_path: Path) -> str:
    """Generate a short ID for a stub based on its path."""
    # Use first 6 chars of MD5 hash of relative path from stubs dir
    rel_path = str(stub_path)
    hash_input = rel_path.encode()
    return hashlib.md5(hash_input).hexdigest()[:6]


@stubs.command("list")
@click.argument("filters", nargs=-1)
def stubs_list(filters: Tuple[str, ...]):
    """List available pay stubs.

    FILTERS can be year (4 digits) and/or party (him/her) in any order.

    \b
    Examples:
      pay-calc stubs list              # All stubs
      pay-calc stubs list 2025         # All parties for 2025
      pay-calc stubs list her          # All years for her
      pay-calc stubs list 2025 her     # Just 2025/her
      pay-calc stubs list her 2025     # Same as above

    Lists are grouped by party with stubs in date order.
    Gaps in pay periods are shown inline as MISSING rows.
    """
    year, party = parse_year_party_filters(filters)
    dirs_to_scan = get_stubs_dirs_for_filters(year, party)

    if not dirs_to_scan:
        filter_desc = []
        if year:
            filter_desc.append(year)
        if party:
            filter_desc.append(party)
        desc = "/".join(filter_desc) if filter_desc else "any year/party"
        click.echo(f"No stubs found for {desc}")
        click.echo(f"\nRun 'pay-calc stubs import <year> <party> <source>' to import stubs.")
        return

    # Collect all stubs grouped by (year, party)
    by_year_party: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for scan_year, scan_party, stubs_dir in dirs_to_scan:
        key = (scan_year, scan_party)
        by_year_party.setdefault(key, [])
        for stub_path in stubs_dir.glob("*.json"):
            try:
                stub = load_stub(stub_path)
                stub["_path"] = stub_path
                by_year_party[key].append(stub)
            except Exception as e:
                # Store error stub for display
                by_year_party[key].append({
                    "_error": str(e),
                    "_path": stub_path,
                    "pay_date": stub_path.stem  # filename as date
                })

    # Sort each group by pay_date
    for key in by_year_party:
        by_year_party[key].sort(key=lambda s: s.get("pay_date", ""))

    # Display
    total_stubs = 0
    total_missing = 0
    show_year = year is None

    for (grp_year, grp_party), stubs_list in sorted(by_year_party.items()):
        if not stubs_list:
            continue

        # Header for this party group
        header_label = f"{grp_year}/{grp_party}" if show_year else grp_party
        click.echo(f"\n{header_label}")
        click.echo("-" * 75)
        click.echo(f"{'ID':<8} {'DATE':<12} {'STATUS':<10} {'EMPLOYER':<23} {'GROSS':>12}")

        # Use shared gap detection
        gap_analysis = detect_gaps(stubs_list, grp_year)
        group_stubs = 0

        # Build map of gap before_date -> gap for middle gaps
        # and track start/end gaps separately
        start_gap = None
        end_gap = None
        middle_gaps = {}  # before_date -> Gap

        for gap in gap_analysis.gaps:
            if gap.gap_type == "start":
                start_gap = gap
            elif gap.gap_type == "end":
                end_gap = gap
            else:  # middle
                if gap.before_date:
                    middle_gaps[gap.before_date] = gap

        # Show start gap if any
        if start_gap:
            click.echo(
                f"{'--':<8} {start_gap.estimated_date:<12} "
                f"{click.style('MISSING', fg='red'):<19} "
                f"{'(gap at start: ' + str(start_gap.days) + ' days)':<23} {'':>12}"
            )

        # Display stubs, inserting middle gaps before the stub they precede
        for stub in stubs_list:
            pay_date_str = stub.get("pay_date", "unknown")

            # Check if there's a gap before this stub
            if pay_date_str in middle_gaps:
                gap = middle_gaps[pay_date_str]
                click.echo(
                    f"{'--':<8} {gap.estimated_date:<12} "
                    f"{click.style('MISSING', fg='red'):<19} "
                    f"{'(gap: ' + str(gap.days) + ' days)':<23} {'':>12}"
                )

            # Get stub ID
            stub_path = stub.get("_path")
            stub_id = get_stub_id(stub_path) if stub_path else "------"

            # Display this stub
            if "_error" in stub:
                click.echo(
                    f"{stub_id:<8} {pay_date_str:<12} "
                    f"{click.style('ERROR', fg='yellow'):<19} "
                    f"{stub['_error'][:21]:<23} {'':>12}"
                )
            else:
                employer = stub.get("employer", "unknown")[:21]
                gross = get_stub_gross(stub)
                errors = validate_stub_schema(stub)
                status = click.style("OK", fg='green') if not errors else click.style("⚠️", fg='yellow')

                click.echo(
                    f"{stub_id:<8} {pay_date_str:<12} {status:<19} "
                    f"{employer:<23} ${gross:>11,.2f}"
                )
                group_stubs += 1

        # Show end gap if any
        if end_gap:
            click.echo(
                f"{'--':<8} {end_gap.estimated_date:<12} "
                f"{click.style('MISSING', fg='red'):<19} "
                f"{'(gap at end: ' + str(end_gap.days) + ' days)':<23} {'':>12}"
            )

        click.echo("-" * 75)
        summary = f"{group_stubs} stubs"
        if gap_analysis.gap_count:
            summary += click.style(f", {gap_analysis.gap_count} missing", fg='red')
        click.echo(summary)

        total_stubs += group_stubs
        total_missing += gap_analysis.gap_count

    # Grand total if multiple groups
    if len(by_year_party) > 1:
        click.echo(f"\nTotal: {total_stubs} stubs")
        if total_missing:
            click.echo(f"Total missing: {total_missing}")


@stubs.command("export")
@click.argument("year")
@click.argument("party", type=click.Choice(["him", "her"]))
@click.argument("target")
def stubs_export(year: str, party: str, target: str):
    """Export pay stubs to a zip file.

    TARGET can be:

    \b
    - Local file path: Creates zip at that location
    - Drive folder ID: Uploads zip to Drive folder
    """
    if not year.isdigit() or len(year) != 4:
        raise click.BadParameter(f"Invalid year '{year}'. Must be 4 digits.")

    stubs_dir = get_stubs_dir(year, party)

    if not stubs_dir.exists():
        raise click.ClickException(f"No stubs found for {year}/{party}")

    stub_files = list(stubs_dir.glob("*.json"))
    if not stub_files:
        raise click.ClickException(f"No stubs found for {year}/{party}")

    # Create zip file
    zip_name = f"{year}_{party}_stubs.zip"

    if is_drive_folder_id(target):
        # Export to Drive
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            zip_path = Path(tmp_dir) / zip_name
            _create_stubs_zip(stub_files, zip_path)

            # Upload to Drive
            try:
                from drive_sync import upload_to_drive
                upload_to_drive(str(zip_path), target, zip_name)
                click.echo(f"Exported {len(stub_files)} stubs to Drive folder: {target}")
            except Exception as e:
                raise click.ClickException(f"Failed to upload to Drive: {e}")
    else:
        # Export to local path
        target_path = Path(target)
        if target_path.is_dir():
            target_path = target_path / zip_name

        _create_stubs_zip(stub_files, target_path)
        click.echo(f"Exported {len(stub_files)} stubs to: {target_path}")


def _create_stubs_zip(stub_files: List[Path], zip_path: Path):
    """Create a zip file from stub files."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for stub_file in stub_files:
            zf.write(stub_file, stub_file.name)
