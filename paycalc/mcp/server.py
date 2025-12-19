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
    """Get the last closing price for a stock ticker. Uses AI to look up the most recent closing price."""
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
