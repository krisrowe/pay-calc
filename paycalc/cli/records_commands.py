"""Records command group for unified pay stub and W-2 management."""

import hashlib
import json
from pathlib import Path
from typing import Optional, Tuple

import click

from paycalc.sdk import records


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


def _get_medicare_taxable(data: dict) -> float:
    """Extract Medicare taxable wages from stub data."""
    taxes = data.get("taxes", {})
    medicare = taxes.get("medicare", {})
    return medicare.get("taxable_wages", 0.0)


def _generate_content_id(record: dict) -> str:
    """Generate a deterministic ID from record content for display.

    IMPORTANT: We regenerate this ID from the JSON data rather than using
    the filename for two reasons:

    1. DATA INTEGRITY: If a file gets corrupted or renamed, the displayed ID
       still reflects the actual content. The ID is a function of the data,
       not the storage location.

    2. BACKWARD COMPATIBILITY: Older records may have filenames generated with
       different logic (e.g., based on drive_file_id + page instead of content).
       By computing from data at display time, we can compare baselines from
       before this change against new imports - same content = same ID regardless
       of when or how the file was created.

    WARNING: Do NOT "optimize" this by using the filename instead. The explicit
    recomputation from data is intentional and required for the above guarantees.
    See also: sdk/records.py _generate_record_id() and README.md "Record IDs".

    Hash is based on:
    - record type (stub/w2)
    - document_id (or medicare taxable wages if doc_id unavailable)
    - pay_date (stubs) or tax_year (w2)

    Returns first 8 chars of hash for brevity.
    """
    meta = record.get("meta", {})
    data = record.get("data", {})
    record_type = meta.get("type", "unknown")

    parts = [record_type]

    if record_type == "stub":
        # Use document_id if available, else medicare taxable wages
        doc_id = data.get("document_id", "")
        if doc_id and doc_id not in ("", "null", "None", "N/A"):
            parts.append(f"doc:{doc_id}")
        else:
            medicare = _get_medicare_taxable(data)
            parts.append(f"med:{medicare:.2f}")

        parts.append(data.get("pay_date", ""))

    elif record_type == "w2":
        parts.append(str(data.get("tax_year", "")))
        # W-2s use employer + wages as identifier
        parts.append(data.get("employer_name", ""))
        parts.append(f"{data.get('wages', 0):.2f}")

    else:
        # Fallback for discarded/unknown
        parts.append(meta.get("source_filename", "unknown"))

    content = "|".join(parts)
    return hashlib.sha256(content.encode()).hexdigest()[:8]


def format_record_row(record: dict, record_type: str, verbose: bool = False) -> str:
    """Format a record as a table row."""
    # Use content-based ID for deterministic diffing between importers
    rec_id = _generate_content_id(record)
    data = record.get("data") or {}
    meta = record.get("meta") or {}

    # Warning indicator
    warnings = meta.get("warnings", [])
    warn_str = f" ⚠{len(warnings)}" if warnings else ""

    if record_type == "stub":
        pay_date = data.get("pay_date") or "unknown"
        employer = (data.get("employer") or "unknown")[:21]
        doc_id = data.get("document_id") or ""

        # Get gross from various locations
        gross = 0.0
        if "pay_summary" in data and "current" in data["pay_summary"]:
            gross = data["pay_summary"]["current"].get("gross") or 0.0
        elif "gross_pay" in data:
            gross = data["gross_pay"] or 0.0

        if verbose:
            # Show content ID + dedup fields: doc_id, pay_date, medicare taxable
            medicare = _get_medicare_taxable(data)
            return f"{rec_id:<10} {pay_date:<12} {doc_id:<8} {medicare:>12,.2f} ${gross:>11,.2f}{warn_str}"
        else:
            return f"{rec_id:<10} {pay_date:<12} {'stub':<8} {employer:<21} ${gross:>11,.2f}{warn_str}"

    elif record_type == "w2":
        tax_year = str(data.get("tax_year", "unknown"))
        employer = data.get("employer_name", "unknown")[:21]
        wages = data.get("wages", 0.0)
        fed_tax = data.get("federal_tax_withheld", 0.0)

        if verbose:
            return f"{tax_year:<12} {'w2':<8} ${wages:>12,.2f} ${fed_tax:>11,.2f}{warn_str}"
        else:
            return f"{rec_id:<10} {tax_year:<12} {'w2':<8} {employer:<21} ${wages:>11,.2f}{warn_str}"

    elif record_type == "form_1040":
        tax_year = str(data.get("tax_year", "unknown"))
        refund_owed = data.get("refund_or_owed", {})
        refund = refund_owed.get("line_35a_refund_amount", 0.0)
        owed = refund_owed.get("line_37_amount_owed", 0.0)
        
        if refund > 0:
            amount_str = f"Refund ${refund:>8,.2f}"
        elif owed > 0:
            amount_str = f"Owed ${owed:>10,.2f}"
        else:
            amount_str = "Even"
        
        return f"{rec_id:<10} {tax_year:<12} {'1040':<8} {'IRS Form 1040':<21} {amount_str}"

    elif record_type == "discarded":
        filename = meta.get("source_filename", "unknown")[:30]
        reason = meta.get("discard_reason", "unknown")

        return f"{rec_id:<10} {'--':<12} {'discard':<8} {filename:<21} {reason}"

    return f"{rec_id:<10} {'?':<12} {record_type:<8} {'?':<21} {'?':>12}"


@click.group()
def records_cli():
    """Manage pay records (stubs and W-2s).

    Unified storage for pay stubs and W-2 documents with validation.
    Records are stored in ~/.local/share/pay-calc/records/<year>/<party>/.

    \b
    Examples:
      pay-calc records list                    # All records
      pay-calc records list 2025               # All 2025 records
      pay-calc records list 2025 him           # 2025/him only
      pay-calc records list --type stub        # Only stubs
      pay-calc records import 2025 him stub ./pay.json
      pay-calc records remove abc123
    """
    pass


@records_cli.command("list")
@click.argument("filters", nargs=-1)
@click.option("--type", "type_filter", type=click.Choice(["stub", "w2"]),
              help="Filter by record type.")
@click.option("--employer", help="Filter by employer (case-insensitive substring match).")
@click.option("--show-discarded", is_flag=True,
              help="Also show discarded records.")
@click.option("--format", "output_format", type=click.Choice(["text", "json"]),
              default="text", help="Output format.")
@click.option("--verbose", "-v", is_flag=True,
              help="Show dedup fields (doc_id, medicare) for diffing between importers.")
def records_list(filters: Tuple[str, ...], type_filter: Optional[str], employer: Optional[str],
                 show_discarded: bool, output_format: str, verbose: bool):
    """List pay records.

    FILTERS can be year (4 digits) and/or party (him/her) in any order.

    \b
    Examples:
      pay-calc records list              # All records
      pay-calc records list 2025         # All parties for 2025
      pay-calc records list her          # All years for her
      pay-calc records list 2025 her     # Just 2025/her
      pay-calc records list --type stub  # Only stubs
      pay-calc records list --employer "Employer A LLC"  # Filter by employer
      pay-calc records list -v           # Verbose with dedup fields
    """
    year, party = parse_year_party_filters(filters)

    all_records = records.list_records(
        year=year,
        party=party,
        type_filter=type_filter,
        include_discarded=show_discarded
    )

    # Filter by employer if specified
    if employer:
        employer_lower = employer.lower()
        all_records = [
            r for r in all_records
            if employer_lower in (r.get("data", {}).get("employer") or "").lower()
            or employer_lower in (r.get("data", {}).get("employer_name") or "").lower()
        ]

    if output_format == "json":
        # Strip internal _path field
        output = []
        for r in all_records:
            r_copy = {"id": r.get("id"), "meta": r.get("meta"), "data": r.get("data")}
            output.append(r_copy)
        click.echo(json.dumps(output, indent=2))
        return

    if not all_records:
        filter_desc = []
        if year:
            filter_desc.append(year)
        if party:
            filter_desc.append(party)
        if type_filter:
            filter_desc.append(f"type={type_filter}")
        if employer:
            filter_desc.append(f"employer={employer}")
        desc = "/".join(filter_desc) if filter_desc else "any filters"
        click.echo(f"No records found for {desc}")
        click.echo(f"\nRun 'pay-calc records import' to import records.")
        return

    # Group by year/party
    by_group: dict = {}
    for rec in all_records:
        meta = rec.get("meta", {})
        rec_type = meta.get("type", "unknown")

        if rec_type == "discarded":
            grp_key = ("_discarded", "")
        else:
            data = rec.get("data", {})
            if rec_type == "stub":
                rec_year = data.get("pay_date", "")[:4] or "unknown"
            elif rec_type in ("w2", "form_1040"):
                rec_year = str(data.get("tax_year", "unknown"))
            else:
                rec_year = "unknown"
            rec_party = meta.get("party", "unknown")
            grp_key = (rec_year, rec_party)

        by_group.setdefault(grp_key, []).append((rec, rec_type))

    # Display
    total_count = 0
    show_year_in_header = year is None

    for (grp_year, grp_party), grp_records in sorted(by_group.items()):
        if grp_year == "_discarded":
            header = "Discarded"
        elif show_year_in_header:
            header = f"{grp_year}/{grp_party}"
        else:
            header = grp_party

        click.echo(f"\n{header}")
        if verbose:
            click.echo("-" * 67)
            click.echo(f"{'ID':<10} {'PAY_DATE':<12} {'DOC_ID':<8} {'MED_WAGES':>12} {'GROSS':>12}")
        else:
            click.echo("-" * 75)
            click.echo(f"{'ID':<10} {'DATE/YEAR':<12} {'TYPE':<8} {'EMPLOYER/FILE':<21} {'AMOUNT':>12}")

        for rec, rec_type in grp_records:
            click.echo(format_record_row(rec, rec_type, verbose=verbose))
            total_count += 1

    click.echo("-" * (67 if verbose else 75))
    click.echo(f"Total: {total_count} record(s)")


def _make_progress_callback(debug: bool):
    """Create a progress callback for import operations."""
    def progress_callback(event: str, data: dict):
        if debug:
            if event == "start":
                click.echo(f"Importing from: {data.get('source')}")
                click.echo(f"Found {data.get('file_count', 0)} files")
            elif event == "imported":
                rec_type = data.get("type", "record")
                year = data.get("year", "?")
                party = data.get("party", "?")
                employer = data.get("employer", "")
                click.echo(f"  ✓ {data.get('name')} → {rec_type}, {year}/{party}, {employer}")
            elif event == "skipped":
                reason = data.get("reason", "duplicate")
                click.echo(f"  - {data.get('name')} (skipped: {reason})")
            elif event == "discarded":
                reason = data.get("reason", "unknown")
                click.echo(f"  ✗ {data.get('name')} (discarded: {reason})")
            elif event == "error":
                click.echo(click.style(f"  ✗ {data.get('name')}: {data.get('error')}", fg="red"))
    return progress_callback


def _print_import_summary(stats: dict):
    """Print import summary."""
    click.echo("")
    click.echo(f"Import complete: {stats['imported']} imported "
               f"({stats['stubs']} stubs, {stats['w2s']} W-2s), "
               f"{stats['skipped']} skipped, "
               f"{stats['discarded']} discarded, "
               f"{stats['errors']} errors")


@records_cli.group("import")
def import_cli():
    """Import records from PDF/JSON sources.

    Use 'import file' for single files or 'import folder' for directories.
    """
    pass


@import_cli.command("file")
@click.argument("source")
@click.option("--debug", is_flag=True, help="Show detailed import decisions.")
def import_file(source: str, debug: bool):
    """Import a single file (always reprocesses, bypasses file-level dedup).

    SOURCE can be:
    - A local PDF or JSON file path
    - A Google Drive file ID

    \b
    Examples:
      pay-calc records import file ./paystub.pdf
      pay-calc records import file 1tKho1iaEeFQpC...
    """
    source_path = Path(source)

    # Check if it's a local file
    if source_path.exists() and source_path.is_file():
        result = _import_single_file_auto(source_path, debug)
        _print_import_summary(result)
        return

    # Assume it's a Drive file ID
    if records.is_drive_folder_id(source):  # Works for file IDs too (same format)
        try:
            stats = records.import_from_drive_file(
                file_id=source,
                callback=_make_progress_callback(debug)
            )
            _print_import_summary(stats)
        except (RuntimeError, ValueError) as e:
            click.echo(click.style(f"Error importing file {source}: {e}", fg="red"))
    else:
        raise click.ClickException(f"File not found: {source}")


@import_cli.command("folder")
@click.argument("source")
@click.option("--debug", is_flag=True, help="Show detailed import decisions.")
@click.option("--force", is_flag=True, help="Re-process previously discarded files.")
def import_folder(source: str, debug: bool, force: bool):
    """Import records from a folder.

    SOURCE can be a local folder path or Google Drive folder ID.

    \b
    Examples:
      pay-calc records import folder ./stubs_folder/  # Local folder
      pay-calc records import folder 1tKho1iaEeFQpC   # Drive folder ID
    """
    stats = _import_from_sources([source], debug, force)
    _print_import_summary(stats)


@import_cli.command("sources")
@click.option("--debug", is_flag=True, help="Show detailed import decisions.")
@click.option("--force", is_flag=True, help="Re-process previously discarded files.")
def import_sources(debug: bool, force: bool):
    """Import from all configured drive.pay_records[] folders.

    \b
    Example:
      pay-calc records import sources
    """
    from paycalc.sdk.config import load_profile
    profile = load_profile()
    drive_config = profile.get("drive", {})
    pay_records = drive_config.get("pay_records", [])
    if not pay_records:
        raise click.ClickException(
            "No drive.pay_records[] configured.\n"
            "Configure with: pay-calc profile records add <folder-id>"
        )
    # Extract folder IDs from config objects (format: {"id": "...", "comment": "..."})
    sources = []
    for rec in pay_records:
        if isinstance(rec, dict):
            sources.append(rec.get("id", ""))
        else:
            sources.append(str(rec))
    sources = [s for s in sources if s]  # Filter empty

    stats = _import_from_sources(sources, debug, force)
    _print_import_summary(stats)


def _import_from_sources(sources: list, debug: bool, force: bool) -> dict:
    """Import from multiple folder sources."""
    total_stats = {
        "imported": 0,
        "skipped": 0,
        "discarded": 0,
        "errors": 0,
        "stubs": 0,
        "w2s": 0,
    }

    for src in sources:
        try:
            stats = records.import_from_folder_auto(
                source=src,
                callback=_make_progress_callback(debug),
                force=force,
                debug=debug
            )
            _accumulate_stats(total_stats, stats)
        except (RuntimeError, ValueError) as e:
            click.echo(click.style(f"Error importing from {src}: {e}", fg="red"))
            total_stats["errors"] += 1

    return total_stats


def _accumulate_stats(total: dict, result: dict):
    """Accumulate import statistics."""
    total["imported"] += result.get("imported", 0)
    total["skipped"] += result.get("skipped", 0)
    total["discarded"] += result.get("discarded", 0)
    total["errors"] += result.get("errors", 0)
    total["stubs"] += result.get("stubs", 0)
    total["w2s"] += result.get("w2s", 0)


def _import_single_file_auto(file_path: Path, debug: bool) -> dict:
    """Import a single file with auto-detection and multi-page support.

    If a record with the same file ID exists, it is overwritten.

    Returns stats dict with: imported, skipped, discarded, errors, stubs, w2s
    """
    stats = {"imported": 0, "skipped": 0, "discarded": 0, "errors": 0, "stubs": 0, "w2s": 0}

    suffix = file_path.suffix.lower()
    if suffix not in (".json", ".pdf"):
        if debug:
            click.echo(f"  - {file_path.name} (skipped: unsupported format)")
        return stats

    if debug:
        click.echo(f"Processing: {file_path.name}")

    try:
        # Import file (overwrites existing record if same file ID)
        import_results = records.import_file_auto_all(file_path)

        for import_result in import_results:
            status = import_result.get("status")

            if status == "imported":
                stats["imported"] += 1
                rec_type = import_result.get("type", "unknown")
                if rec_type == "stub":
                    stats["stubs"] += 1
                elif rec_type == "w2":
                    stats["w2s"] += 1

                if debug:
                    year = import_result.get("year", "?")
                    party = import_result.get("party", "?")
                    employer = import_result.get("employer", "")
                    page = import_result.get("page", "")
                    page_info = f" (page {page})" if page else ""
                    click.echo(f"  ✓ {file_path.name}{page_info} → {rec_type}, {year}/{party}, {employer}")

            elif status == "skipped":
                stats["skipped"] += 1
                if debug:
                    reason = import_result.get("reason", "duplicate")
                    page = import_result.get("page", "")
                    page_info = f" (page {page})" if page else ""
                    click.echo(f"  - {file_path.name}{page_info} (skipped: {reason})")

            elif status == "discarded":
                stats["discarded"] += 1
                if debug:
                    reason = import_result.get("reason", "unknown")
                    click.echo(f"  ✗ {file_path.name} (discarded: {reason})")

    except Exception as e:
        stats["errors"] += 1
        click.echo(click.style(f"  ✗ {file_path.name}: {e}", fg="red"))

    return stats


@records_cli.command("show")
@click.argument("record_id")
@click.option("--format", "output_format", type=click.Choice(["text", "json"]),
              default="text", help="Output format.")
def records_show(record_id: str, output_format: str):
    """Show details of a single record.

    \b
    Arguments:
      RECORD_ID    The 8-character record ID (from 'records list')
    """
    record = records.get_record(record_id)

    if not record:
        raise click.ClickException(f"Record not found: {record_id}")

    if output_format == "json":
        output = {"id": record.get("id"), "meta": record.get("meta"), "data": record.get("data")}
        click.echo(json.dumps(output, indent=2))
        return

    meta = record.get("meta", {})
    data = record.get("data", {})

    click.echo(f"Record: {record_id}")
    click.echo("-" * 40)
    click.echo(f"Type: {meta.get('type', 'unknown')}")
    click.echo(f"Year: {meta.get('year', 'unknown')}")
    click.echo(f"Party: {meta.get('party', 'unknown')}")
    click.echo(f"Source: {meta.get('source_filename', 'unknown')}")
    click.echo(f"Imported: {meta.get('imported_at', 'unknown')}")

    if meta.get("drive_file_id"):
        click.echo(f"Drive ID: {meta['drive_file_id']}")

    click.echo("\nData:")
    click.echo(json.dumps(data, indent=2))


@records_cli.command("remove")
@click.argument("filters", nargs=-1)
@click.option("--force", is_flag=True, help="Skip confirmation prompt.")
@click.option("--include-discarded", is_flag=True, help="Also remove discarded markers.")
def records_remove(filters: Tuple[str, ...], force: bool, include_discarded: bool):
    """Remove records by ID or by year/party filters.

    FILTERS can be:
    - A single record ID (8-char hex from 'records list')
    - Year (4 digits) and/or party (him/her) to bulk remove

    \b
    Examples:
      pay-calc records remove abc12345           # Single record by ID
      pay-calc records remove 2025 him           # All 2025/him records
      pay-calc records remove 2025               # All 2025 records (both parties)
      pay-calc records remove him                # All records for him (all years)
      pay-calc records remove 2025 him --include-discarded  # Also clear discarded markers
    """
    if not filters:
        raise click.ClickException(
            "No filter specified. Provide a record ID or year/party filters.\n"
            "Examples:\n"
            "  pay-calc records remove abc12345    # Single record\n"
            "  pay-calc records remove 2025 him    # Bulk remove"
        )

    # Check if first filter looks like a record ID (8+ hex chars, not a year)
    first = filters[0]
    is_record_id = (
        len(first) >= 8 and
        all(c in "0123456789abcdef" for c in first.lower()) and
        not (first.isdigit() and len(first) == 4)
    )

    if is_record_id and len(filters) == 1:
        # Single record removal (existing behavior)
        record_id = first
        record = records.get_record(record_id)
        if not record:
            raise click.ClickException(f"Record not found: {record_id}")

        meta = record.get("meta", {})
        data = record.get("data", {})

        # Show what will be deleted
        rec_type = meta.get("type", "unknown")
        if rec_type == "stub":
            desc = f"stub {data.get('pay_date', 'unknown')} {data.get('employer', 'unknown')}"
        elif rec_type == "w2":
            desc = f"W-2 {data.get('tax_year', 'unknown')} {data.get('employer_name', 'unknown')}"
        else:
            desc = f"discarded record {meta.get('source_filename', 'unknown')}"

        click.echo(f"Will remove: {desc}")

        if not force:
            click.confirm("Proceed?", abort=True)

        if records.remove_record(record_id):
            click.echo(click.style(f"Removed record {record_id}", fg="green"))
        else:
            raise click.ClickException(f"Failed to remove record {record_id}")

    else:
        # Bulk removal by year/party filters
        year, party = parse_year_party_filters(filters)

        if not year and not party:
            raise click.ClickException(
                "Bulk remove requires at least year or party filter.\n"
                "Use 'pay-calc reset' to remove all data."
            )

        # Get matching records (not including discarded - those are handled separately)
        matching = records.list_records(
            year=year,
            party=party,
            include_discarded=False
        )

        # Get ALL discarded markers if flag is set (they don't have year/party)
        discarded_to_remove = []
        if include_discarded:
            discarded_to_remove = records.list_discarded()

        if not matching and not discarded_to_remove:
            filter_desc = "/".join(f for f in [year, party] if f)
            click.echo(f"No records found for {filter_desc}")
            return

        # Group by type for summary
        stubs = [r for r in matching if r.get("meta", {}).get("type") == "stub"]
        w2s = [r for r in matching if r.get("meta", {}).get("type") == "w2"]

        filter_desc = "/".join(f for f in [year, party] if f)
        total_count = len(matching) + len(discarded_to_remove)
        click.echo(f"\nWill remove {total_count} item(s):")
        if stubs:
            click.echo(f"  - {len(stubs)} pay stubs ({filter_desc})")
        if w2s:
            click.echo(f"  - {len(w2s)} W-2s ({filter_desc})")
        if discarded_to_remove:
            click.echo(f"  - {len(discarded_to_remove)} discarded markers (ALL - these are not filtered by year/party)")

        if not force:
            click.confirm("\nProceed?", abort=True)

        # Remove all matching records
        removed = 0
        for rec in matching:
            rec_id = rec.get("id")
            if rec_id and records.remove_record(rec_id):
                removed += 1

        click.echo(click.style(f"\nRemoved {removed} record(s)", fg="green"))
        if removed < len(matching):
            click.echo(click.style(
                f"Warning: {len(matching) - removed} record(s) could not be removed",
                fg="yellow"
            ))


@records_cli.command("validate")
@click.argument("source", type=click.Path(exists=True))
@click.option("--type", "record_type", type=click.Choice(["stub", "w2"]),
              required=True, help="Record type to validate as.")
def records_validate(source: str, record_type: str):
    """Validate a JSON file without importing.

    Runs the validation pipeline and reports any errors or warnings.

    \b
    Arguments:
      SOURCE       Path to JSON file to validate
    """
    source_path = Path(source)

    try:
        with open(source_path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise click.ClickException(f"Invalid JSON: {e}")

    # Build minimal meta for validation (no duplicate check)
    meta = {"type": record_type}

    errors, warnings = records.validate_record(
        record_type=record_type,
        data=data,
        meta=meta,
        check_duplicate=False
    )

    if errors:
        click.echo(click.style("Validation failed:", fg="red"))
        for error in errors:
            click.echo(f"  - {error}")
        raise SystemExit(1)

    if warnings:
        click.echo(click.style("Validation passed with warnings:", fg="yellow"))
        for warning in warnings:
            click.echo(f"  - {warning}")
    else:
        click.echo(click.style("Validation passed!", fg="green"))
