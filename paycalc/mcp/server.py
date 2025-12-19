"""Pay Calc MCP Server - FastMCP implementation for pay record tools."""

import json
import logging
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from paycalc.sdk import records as sdk_records

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
    ticker: str = Field(description="Stock ticker symbol (e.g., 'GOOG', 'AAPL')"),
) -> dict[str, Any]:
    """Get the last closing price for a stock ticker.

    Use this to get the current stock price for RSU valuation in income projections.
    The returned 'last_close' value can be passed to generate_income_projection()
    or generate_w2() as the 'stock_price' parameter.

    Example workflow:
    1. Call get_stock_quote(ticker='GOOG') to get current price
    2. Pass result['last_close'] to generate_income_projection(stock_price=...)
    """
    try:
        from gemini_client import get_stock_quote as fetch_quote

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
    stock_price: float | None = Field(default=None, description="Stock price for RSU valuation. Use get_stock_quote() to fetch current price for GOOG."),
) -> dict[str, Any]:
    """Generate year-end income projection from pay stub analysis.

    Projects total income to year-end based on:
    - Regular pay pattern (biweekly/monthly cadence)
    - RSU vesting schedule (if configured for party)

    For RSU projections with dollar values, first call get_stock_quote('GOOG')
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

        return {
            "projection": proj,
            "summary": {
                "as_of_date": proj.get("as_of_date"),
                "days_remaining": proj.get("days_remaining"),
                "actual_gross": proj.get("actual", {}).get("gross"),
                "projected_additional": proj.get("projected_additional", {}).get("total_gross"),
                "projected_total_gross": proj.get("projected_total", {}).get("gross"),
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
    stock_price: float | None = Field(default=None, description="Stock price for RSU projection. Use get_stock_quote() to fetch current price for GOOG."),
) -> dict[str, Any]:
    """Generate W-2 data from pay stub analysis.

    Creates W-2 box values (wages, withholding, SS/Medicare) from YTD totals.

    With include_projection=True, also returns:
    - Projected additional income by type
    - Combined W-2 (YTD + projected)

    For RSU projections with dollar values, first call get_stock_quote('GOOG')
    and pass the result's 'last_close' as stock_price.

    Requires analysis data to exist (run 'pay-calc analysis YEAR PARTY' first).
    """
    try:
        from paycalc.sdk import generate_w2_from_analysis, get_data_path
        import json

        w2_data = generate_w2_from_analysis(year=year, party=party)

        result = {
            "year": year,
            "party": party,
            "w2": w2_data["forms"][0]["data"],
            "date_range": w2_data.get("analysis_date_range"),
        }

        if include_projection:
            data_path = get_data_path()
            analysis_file = data_path / f"{year}_{party}_pay_all.json"

            if analysis_file.exists():
                with open(analysis_file) as f:
                    analysis_data = json.load(f)

                stubs = analysis_data.get("stubs", [])
                if stubs:
                    from paycalc.sdk.income_projection import generate_projection
                    proj = generate_projection(stubs, year, party=party, stock_price=stock_price)

                    if proj and proj.get("days_remaining", 0) > 0:
                        result["projection"] = {
                            "additional": proj.get("projected_additional"),
                            "combined_w2": {
                                "wages_tips_other_comp": (
                                    result["w2"]["wages_tips_other_comp"] +
                                    proj.get("projected_additional", {}).get("total_gross", 0)
                                ),
                                "federal_income_tax_withheld": (
                                    result["w2"]["federal_income_tax_withheld"] +
                                    proj.get("projected_additional", {}).get("taxes", 0)
                                ),
                            },
                            "stock_price_used": stock_price,
                        }

        return result

    except FileNotFoundError as e:
        return {"error": str(e), "w2": None}
    except Exception as e:
        logger.error(f"Error generating W-2: {e}")
        return {"error": str(e), "w2": None}


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
