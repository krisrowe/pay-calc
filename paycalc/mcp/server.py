"""Pay Calc MCP Server - FastMCP implementation for pay record tools."""

import json
import logging
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from paycalc.sdk import records as sdk_records
from paycalc.sdk.employee.w4 import resolve_w4, merge_w4_with_defaults
from paycalc.sdk.employee.config import derive_w4_from_stub as sdk_derive_w4

logger = logging.getLogger(__name__)

# Initialize FastMCP server
mcp = FastMCP("pay-calc")


# --- Tools ---

@mcp.tool()
async def list_records(
    year: str | None = Field(default=None, description="Filter by year (e.g., '2025')"),
    party: str | None = Field(default=None, description="Filter by party ('him' or 'her')"),
    record_type: str | None = Field(default=None, description="Filter by type ('stub' or 'w2')"),
    employer: str | None = Field(default=None, description="Filter by employer name (case-insensitive substring match)"),
    data_filter: str | None = Field(default=None, description="JSONPath expression to filter records by data content (jsonpath-ng.ext syntax). Evaluated against each record's 'data' object. Syntax: '&' for AND, '=~' for regex. Examples: '$.earnings[?type==\"Bonus\" & current_amount>0]', '$.deductions[?type=~\".*401.*\"]'. Use get_record to see data structure."),
    limit: int = Field(default=50, description="Maximum number of records to return (default 50)"),
) -> dict[str, Any]:
    """List pay records (stubs and W-2s) with optional filters. Returns record IDs, dates, employers, and amounts."""
    try:
        # Get records from SDK
        all_records = sdk_records.list_records(
            year=year,
            party=party,
            type_filter=record_type,
            include_discarded=False,
            data_filter=data_filter,
        )

        # Apply employer filter if specified
        if employer:
            employer_lower = employer.lower()
            all_records = [
                r for r in all_records
                if employer_lower in (r.get("data", {}).get("employer") or "").lower()
                or employer_lower in (r.get("data", {}).get("employer_name") or "").lower()
            ]

        # Apply limit
        total_count = len(all_records)
        limited_records = all_records[:limit]

        # Format records for output
        formatted = []
        for rec in limited_records:
            meta = rec.get("meta", {})
            data = rec.get("data", {})
            rec_type = meta.get("type", "unknown")

            if rec_type == "stub":
                formatted.append({
                    "id": rec.get("id"),
                    "type": "stub",
                    "pay_date": data.get("pay_date"),
                    "employer": data.get("employer"),
                    "gross": data.get("pay_summary", {}).get("current", {}).get("gross", 0),
                    "party": meta.get("party"),
                    "year": data.get("pay_date", "")[:4] if data.get("pay_date") else None,
                })
            elif rec_type == "w2":
                formatted.append({
                    "id": rec.get("id"),
                    "type": "w2",
                    "tax_year": data.get("tax_year"),
                    "employer": data.get("employer_name"),
                    "wages": data.get("wages"),
                    "federal_withheld": data.get("federal_tax_withheld"),
                    "party": meta.get("party"),
                })

        return {
            "records": formatted,
            "count": len(formatted),
            "total_available": total_count,
            "filters_applied": {
                "year": year,
                "party": party,
                "type": record_type,
                "employer": employer,
                "data_filter": data_filter,
            },
        }

    except Exception as e:
        logger.error(f"Error listing records: {e}")
        return {"error": str(e), "records": [], "count": 0}


@mcp.tool()
async def get_record(
    record_id: str = Field(description="The 8-character record ID (from list_records)"),
) -> dict[str, Any]:
    """Get full details of a single pay record by ID. Returns complete stub or W-2 data."""
    try:
        record = sdk_records.get_record(record_id)

        if record is None:
            return {
                "error": f"Record not found: {record_id}",
                "record": None,
            }

        # Remove internal path field
        record.pop("_path", None)

        meta = record.get("meta", {})
        data = record.get("data", {})
        rec_type = meta.get("type", "unknown")

        # Add computed summary based on type
        if rec_type == "stub":
            summary = {
                "type": "stub",
                "pay_date": data.get("pay_date"),
                "employer": data.get("employer"),
                "gross": data.get("pay_summary", {}).get("current", {}).get("gross", 0),
                "net_pay": data.get("pay_summary", {}).get("current", {}).get("net_pay", 0),
                "federal_withheld": data.get("taxes", {}).get("federal_income_tax", {}).get("current_withheld", 0),
            }
        elif rec_type == "w2":
            summary = {
                "type": "w2",
                "tax_year": data.get("tax_year"),
                "employer": data.get("employer_name"),
                "wages": data.get("wages"),
                "federal_withheld": data.get("federal_tax_withheld"),
            }
        else:
            summary = {"type": rec_type}

        return {
            "id": record.get("id"),
            "summary": summary,
            "meta": meta,
            "data": data,
        }

    except Exception as e:
        logger.error(f"Error getting record {record_id}: {e}")
        return {"error": str(e), "record": None}


@mcp.tool()
async def get_stock_quote(
    ticker: str = Field(description="Stock ticker symbol (e.g., 'SBUX', 'JPM')"),
) -> dict[str, Any]:
    """Get the last closing price for a stock ticker.

    Use this to get the current stock price for RSU valuation in income projections.
    The returned 'last_close' value can be passed to generate_income_projection()
    or generate_w2() as the 'stock_price' parameter.

    Example workflow:
    1. Call get_stock_quote(ticker='SBUX') to get current price
    2. Pass result['last_close'] to generate_income_projection(stock_price=...)
    """
    try:
        from paycalc.gemini_client import get_stock_quote as fetch_quote

        price = fetch_quote(ticker)
        return {
            "ticker": ticker.upper(),
            "last_close": price,
            "formatted": f"${price:.2f}",
        }

    except ValueError as e:
        return {"error": str(e), "ticker": ticker.upper(), "last_close": None}
    except RuntimeError as e:
        return {"error": f"Failed to get quote: {e}", "ticker": ticker.upper(), "last_close": None}


@mcp.tool()
async def generate_income_projection(
    year: str = Field(description="Tax year (4 digits, e.g., '2025')"),
    party: str = Field(description="Party identifier ('him' or 'her')"),
    stock_price: float | None = Field(default=None, description="Stock price for RSU valuation. Use get_stock_quote() to fetch current price."),
) -> dict[str, Any]:
    """Generate year-end income projection from pay stub analysis.

    Projects total income to year-end based on:
    - Regular pay pattern (biweekly/monthly cadence)
    - RSU vesting schedule (if configured for party)

    For RSU projections with dollar values, first call get_stock_quote()
    and pass the result's 'last_close' as stock_price.

    Requires analysis data to exist (run 'pay-calc analysis YEAR PARTY' first).
    """
    try:
        from paycalc.sdk.income_projection import generate_income_projection as sdk_projection
        from paycalc.sdk import get_data_path
        import json

        data_path = get_data_path()
        analysis_file = data_path / f"{year}_{party}_pay_all.json"

        if not analysis_file.exists():
            return {
                "error": f"Analysis data not found for {year}/{party}. Run 'pay-calc analysis {year} {party}' first.",
                "projection": None,
            }

        with open(analysis_file) as f:
            analysis_data = json.load(f)

        stubs = analysis_data.get("stubs", [])
        if not stubs:
            return {"error": "No pay stub data in analysis file", "projection": None}

        from paycalc.sdk.income_projection import generate_projection
        proj = generate_projection(stubs, year, party=party, stock_price=stock_price)

        if not proj:
            return {"message": "No projection needed - year appears complete", "projection": None}

        # Extract total from stub property
        stub = proj.get("stub", {})
        stub_gross = stub.get("pay_summary", {}).get("ytd", {}).get("gross", 0)

        return {
            "projection": proj,
            "summary": {
                "as_of_date": proj.get("as_of_date"),
                "days_remaining": proj.get("days_remaining"),
                "actual_gross": proj.get("actual", {}).get("gross"),
                "projected_additional": proj.get("projected_additional", {}).get("total_gross"),
                "projected_total_gross": stub_gross,
                "stock_price_used": stock_price,
            },
        }

    except Exception as e:
        logger.error(f"Error generating projection: {e}")
        return {"error": str(e), "projection": None}


@mcp.tool()
async def generate_w2(
    year: str = Field(description="Tax year (4 digits, e.g., '2025')"),
    party: str = Field(description="Party identifier ('him' or 'her')"),
    include_projection: bool = Field(default=False, description="Include projected income to year-end"),
    stock_price: float | None = Field(default=None, description="Stock price for RSU projection. Use get_stock_quote() to fetch current price."),
) -> dict[str, Any]:
    """Generate W-2 data from pay stub analysis.

    Creates W-2 box values (wages, withholding, SS/Medicare) from YTD totals.

    With include_projection=True, returns 3 W-2 format objects:
    - ytd_w2: W-2 from current YTD data
    - projected_additional_w2: W-2 for projected additional income
    - projected_w2: Total W-2 (ytd + additional)

    All three have identical structure. The third is the sum of the first two.
    SS wages cap ($176,100) carries forward - additional shows only remaining taxable.

    For RSU projections with dollar values, first call get_stock_quote()
    and pass the result's 'last_close' as stock_price.

    Requires analysis data to exist (run 'pay-calc analysis YEAR PARTY' first).
    """
    try:
        from paycalc.sdk.w2 import generate_w2_with_projection

        return generate_w2_with_projection(
            year=year,
            party=party,
            include_projection=include_projection,
            stock_price=stock_price,
        )

    except FileNotFoundError as e:
        return {"error": str(e), "ytd_w2": None}
    except Exception as e:
        logger.error(f"Error generating W-2: {e}")
        return {"error": str(e), "ytd_w2": None}


@mcp.tool()
async def generate_tax_projection(
    year: str = Field(description="Tax year (4 digits, e.g., '2025')"),
    output_format: str = Field(default="json", description="Output format: 'json' (default) or 'csv'"),
    ytd_final_party: str | None = Field(default=None, description="Use final YTD without projection: null (project both), 'all' (final for both), 'him'/'her' (final for one, project the other)"),
    stock_price: float | None = Field(default=None, description="Stock price for RSU valuation. Use get_stock_quote() to fetch current price."),
) -> dict[str, Any] | str:
    """Calculate federal tax liability and refund/owed amount.

    Loads W-2 data for both parties (him + her), applies MFJ tax brackets,
    and calculates federal income tax, medicare taxes, and projected
    refund or amount owed.

    Data sources (in priority order):
    1. Official W-2 records (from records import)
    2. Latest stub → projected to year-end (default)
    3. Latest stub as-is (if ytd_final_party specified)

    By default, projects income to year-end based on:
    - Regular pay pattern (biweekly/monthly cadence)
    - RSU vesting schedule (if configured and stock_price provided)

    The ytd_final_party parameter controls per-party projection:
    - null/None: project both parties to year-end (default)
    - "all": use final YTD for both parties (no projection)
    - "him": use final YTD for him, project her
    - "her": use final YTD for her, project him

    Returns structured JSON with data_sources metadata showing where each
    party's W-2 data came from. CSV format available for spreadsheet import.

    Requires W-2 or analysis data to exist for both parties.
    """
    try:
        from paycalc.sdk.taxes.other import generate_tax_projection as sdk_tax_projection

        result = sdk_tax_projection(
            year,
            output_format=output_format,
            ytd_final_party=ytd_final_party,
            stock_price=stock_price,
        )

        if output_format == "csv":
            return {"csv": result}

        return result

    except FileNotFoundError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"Error generating tax projection: {e}")
        return {"error": str(e)}


@mcp.tool()
async def get_first_regular_pay_date(
    party: str = Field(description="Party identifier ('him' or 'her')"),
    year: int | None = Field(default=None, description="Target year (defaults to current year)"),
) -> dict[str, Any]:
    """Get the first regular pay date of the year for a party.

    Calculates based on the latest comp plan for the party and most recent
    regular pay stub matching that employer, then works backwards to find
    the first pay date of the year.

    Returns success with date, frequency, employer, and reference date used.
    On failure, returns error code and message.
    """
    try:
        from paycalc.sdk.modeling import get_first_regular_pay_date as sdk_get_first

        return sdk_get_first(party=party, year=year)

    except Exception as e:
        logger.error(f"Error getting first pay date: {e}")
        return {
            "success": False,
            "error": {
                "code": "other",
                "message": str(e),
            },
        }


@mcp.tool()
async def model_regular_401k_contribs(
    year: int = Field(description="Tax year to model (e.g., 2025)"),
    party: str = Field(description="Party identifier ('him' or 'her')"),
    contribution_amount: float | None = Field(default=None, description="401k contribution amount per period"),
    contribution_type: str = Field(default="absolute", description="'absolute' (fixed dollar) or 'percentage' (of gross, e.g. 0.10 for 10%)"),
    starting_date: str | None = Field(default=None, description="Date to start 401k contributions (YYYY-MM-DD). Defaults to first pay date of year."),
) -> dict[str, Any]:
    """Model regular pay stubs with configurable 401k contributions.

    Projects pay stubs for the full calendar year, calculating FIT, FICA, and
    net pay for each period with the specified 401k contributions.

    Pay schedule is auto-detected from the most recent stub in the prior year.

    Returns stubs array with each period's breakdown plus YTD totals.
    """
    try:
        from paycalc.sdk.modeling import (
            model_regular_401k_contribs as sdk_model_401k,
            get_first_regular_pay_date,
        )

        # Resolve starting_date for 401k contributions if needed
        effective_start = starting_date
        if contribution_amount is not None and not effective_start:
            first_pay_result = get_first_regular_pay_date(party, year)
            if first_pay_result.get("success"):
                effective_start = first_pay_result["date"]

        # Build regular_401k_contribs config
        regular_401k_contribs = None
        if contribution_amount is not None:
            regular_401k_contribs = {
                "starting_date": effective_start or f"{year}-01-01",
                "amount": contribution_amount,
                "amount_type": contribution_type,
            }

        return sdk_model_401k(
            year,
            party,
            regular_401k_contribs=regular_401k_contribs,
        )

    except Exception as e:
        logger.error(f"Error modeling 401k contributions: {e}")
        return {"error": str(e)}


@mcp.tool()
async def max_regular_401k_contribs(
    year: int = Field(description="Tax year to model (e.g., 2025)"),
    party: str = Field(description="Party identifier ('him' or 'her')"),
    starting_date: str | None = Field(default=None, description="Date to start 401k contributions (YYYY-MM-DD). Defaults to first pay date of year."),
) -> dict[str, Any]:
    """Model regular pay stubs with max 401k contributions (100% of gross).

    Projects pay stubs for the full calendar year with 401k contributions
    set to maximum (capped at IRS limit). Useful for comparing net pay
    between contributing $0 vs maxing out 401k.

    Pay schedule is auto-detected from the most recent stub in the prior year.

    Returns stubs array with each period's breakdown plus YTD totals.
    """
    try:
        from paycalc.sdk.modeling import model_401k_max_frontload as sdk_max_401k

        return sdk_max_401k(
            year,
            party,
            starting_date=starting_date,
        )

    except Exception as e:
        logger.error(f"Error modeling max 401k contributions: {e}")
        return {"error": str(e)}


@mcp.tool()
async def get_configured_w4(
    party: str = Field(description="Party identifier ('him' or 'her')"),
    date: str = Field(description="Date to check effective W4 for (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Get the effective W4 settings configured in this app for a party on a given date.

    Resolves from registered W4 history in profile.yaml, falling back to
    defaults if no W4 is registered for the party.

    DIAGNOSTIC USE: When modeled pay stub outputs don't match actual stub values,
    compare this tool's output with derive_w4_from_stub() results:
    - This tool shows what W4 pay-calc THINKS is in effect (from config)
    - derive_w4_from_stub shows what W4 the payroll system ACTUALLY used
    - Differences indicate W4 config needs updating or a W4 change occurred

    Returns W4 settings including filing_status, step2_checkbox, step3_dependents,
    step4a_other_income, step4b_deductions, step4c_extra_withholding.
    """
    try:
        from datetime import datetime

        target_date = datetime.strptime(date, "%Y-%m-%d").date()
        result = resolve_w4(party, target_date)

        # Merge with defaults to get complete W4 structure
        settings = merge_w4_with_defaults(result["settings"])

        return {
            "party": party,
            "date": date,
            "settings": settings,
            "source": result["source"],
        }

    except ValueError as e:
        return {"error": f"Invalid date format: {e}", "party": party, "date": date}
    except Exception as e:
        logger.error(f"Error getting effective W4: {e}")
        return {"error": str(e), "party": party, "date": date}


@mcp.tool()
async def derive_w4_from_stub(
    record_id: str = Field(description="Pay stub record ID (from list_records)"),
    max_dependents: int = Field(default=8, description="Maximum number of dependents to try in search (default 8)"),
) -> dict[str, Any]:
    """Derive W4 settings from a pay stub's actual withholding.

    Reverse-engineers the W4 configuration that would produce the stub's
    actual FIT withholding by trying all combinations of filing status,
    step2 checkbox, and dependent credits.

    DIAGNOSTIC USE: When modeled pay stub outputs don't match actual stub values,
    compare this tool's output with get_configured_w4() results:
    - This tool shows what W4 the payroll system ACTUALLY used (derived from stub)
    - get_configured_w4 shows what W4 pay-calc THINKS is in effect (from config)
    - Differences indicate W4 config needs updating or a W4 change occurred

    Use cases:
    - Diagnosing FIT withholding discrepancies between model and actual
    - Detecting when employer processed a W4 change
    - Setting up accurate W4 history in profile.yaml for modeling

    Returns derived W4 settings and all matching configurations.
    """
    try:
        result = sdk_derive_w4(record_id, max_dependents=max_dependents)
        return result

    except ValueError as e:
        return {"error": str(e), "record_id": record_id}
    except Exception as e:
        logger.error(f"Error deriving W4: {e}")
        return {"error": str(e), "record_id": record_id}


# --- Resources (optional, for browsing) ---

@mcp.resource("paycalc://records/years")
async def list_years_resource() -> str:
    """List available years with record counts."""
    try:
        records_dir = sdk_records.get_records_dir()
        years = {}

        if records_dir.exists():
            for year_dir in sorted(records_dir.iterdir()):
                if year_dir.is_dir() and year_dir.name.isdigit():
                    count = sum(1 for _ in year_dir.rglob("*.json"))
                    years[year_dir.name] = count

        return json.dumps({"years": years}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# --- Server Entry Point ---

def run_server():
    """Run the MCP server in stdio mode."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_server()
