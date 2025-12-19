# Pay Calc MCP Server

Model Context Protocol (MCP) server for pay record access from Claude Code and other agentic tools.

## Installation

```bash
# Install with MCP support
pip install -e ".[mcp]"
```

## Configuration

### Claude Code

Add to `~/.claude.json` or project `.mcp.json`:

```json
{
  "mcpServers": {
    "pay-calc": {
      "command": "pay-calc-mcp",
      "env": {
        "PAY_CALC_DATA": "/path/to/your/data"
      }
    }
  }
}
```

Or if using the repo directly:

```json
{
  "mcpServers": {
    "pay-calc": {
      "command": "python",
      "args": ["-m", "paycalc.mcp.server"],
      "cwd": "/path/to/personal-pay-calc",
      "env": {
        "PAY_CALC_DATA": "/path/to/your/data"
      }
    }
  }
}
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PAY_CALC_DATA` | Data directory containing records | `~/.local/share/pay-calc` |
| `PAY_CALC_CONFIG_PATH` | Profile YAML path | `~/.config/pay-calc/profile.yaml` |

## Available Tools

### `list_records`

List pay records (stubs and W-2s) with optional filters.

**Parameters:**
- `year` (optional): Filter by year (e.g., "2025")
- `party` (optional): Filter by party ("him" or "her")
- `record_type` (optional): Filter by type ("stub" or "w2")
- `employer` (optional): Filter by employer name (substring match)
- `limit` (optional): Max records to return (default 50)

**Example response:**
```json
{
  "records": [
    {
      "id": "bd6ab656",
      "type": "stub",
      "pay_date": "2025-01-03",
      "employer": "Employer A LLC",
      "gross": 5000.00,
      "party": "him",
      "year": "2025"
    }
  ],
  "count": 1,
  "total_available": 84
}
```

### `get_record`

Get full details of a single pay record by ID.

**Parameters:**
- `record_id` (required): The 8-character record ID from `list_records`

**Example response:**
```json
{
  "id": "bd6ab656",
  "summary": {
    "type": "stub",
    "pay_date": "2025-01-03",
    "employer": "Employer A LLC",
    "gross": 5000.00,
    "net_pay": 3500.00,
    "federal_withheld": 1500.00
  },
  "meta": {
    "type": "stub",
    "year": "2025",
    "party": "him",
    "source_filename": "Employer A_Payroll.pdf"
  },
  "data": {
    "employer": "Employer A LLC",
    "pay_date": "2025-01-03",
    "earnings": [...],
    "taxes": {...},
    "deductions": [...],
    "pay_summary": {...}
  }
}
```

## Resources

### `paycalc://records/years`

List available years with record counts.

```json
{
  "years": {
    "2024": 45,
    "2025": 84
  }
}
```

## Testing

Test the MCP server manually:

```bash
# Run server in stdio mode
pay-calc-mcp

# Or via Python module
python -m paycalc.mcp.server
```

## Development

The MCP server is a thin wrapper around the SDK:

```
paycalc/
├── sdk/
│   └── records.py      # Business logic (list_records, get_record)
└── mcp/
    └── server.py       # MCP tool definitions (thin wrappers)
```

To add new tools:

1. Implement SDK function in `paycalc/sdk/`
2. Add `@mcp.tool()` wrapper in `paycalc/mcp/server.py`
3. Document in this file
