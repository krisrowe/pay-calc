# Personal Tax Projection

Tools for processing pay stubs, extracting W-2 data, and generating federal tax projections.

## Installation

```bash
pip install -e .
```

This installs the `pay-calc` CLI command.

## CLI Usage

```bash
pay-calc --help

# Records management (unified workflow)
pay-calc records import                   # Import from all configured Drive folders
pay-calc records import <folder-id>       # Import from specific Drive folder
pay-calc records import ./local/folder    # Import from local folder
pay-calc records import ./file.pdf        # Import single file
pay-calc records list                     # List all imported records
pay-calc records list 2025 him            # Filter by year/party
pay-calc records show <id>                # Show record details
pay-calc records remove <id>              # Remove a record

# Analysis and projection
pay-calc analysis 2025 him            # Validate stubs, extract YTD totals
pay-calc projection 2025 him          # Project year-end (requires analysis first)
pay-calc taxes 2024                   # Calculate tax liability from W-2s or analysis

# Profile management
pay-calc profile show                 # Show profile location, validation, feature readiness
pay-calc profile use <path>           # Point to profile in your config repo
pay-calc profile records list         # List configured Drive folders for records
pay-calc profile records add <id>     # Add a Drive folder for pay records
```

## Workflow Overview

The core workflow has three stages:

```
1. IMPORT: PDF → JSON
   Google Drive (PDFs) → records import → ~/.local/share/pay-calc/records/<year>/<party>/*.json

2. ANALYZE: JSON → Summary
   Local records → pay-analysis → data/YYYY_party_full.json

3. PROJECT: Summary → Tax Estimate
   Analysis output → tax-projection → data/YYYY_tax_projection.csv
```

### Stage 1: Import Records

The `records import` command downloads PDFs, extracts data (using text parsing or Gemini OCR for image-based PDFs), validates the extracted data, and stores the results as JSON files locally.

**Key design:** No year/party/type arguments. These values are auto-detected from file content:
- **Type** (stub vs W-2): detected from document structure and keywords
- **Year**: extracted from pay_date (stubs) or tax year (W-2s)
- **Party**: matched by employer name to `parties.*.companies[].keywords` in config

```bash
# Import from all configured Drive folders (drive.pay_records[] in profile.yaml)
pay-calc records import

# Import from a specific Drive folder
pay-calc records import <drive-folder-id>

# Import from local folder or file
pay-calc records import /path/to/folder
pay-calc records import /path/to/file.pdf
```

Records are stored in: `~/.local/share/pay-calc/records/<year>/<party>/<hash>.json`

### Stage 2: Analyze Pay Stubs

The `analysis` command reads the imported JSON records (not PDFs directly), validates continuity, and produces an aggregated summary.

**Goal:** Extract accurate year-end totals from pay stub YTD values while validating data completeness.

**How it works:**
- **Year totals come from YTD values of the most recent stub** - the final stub's YTD is the source of truth for gross pay, taxes withheld, FIT taxable wages, etc.
- **Earlier stubs validate completeness** - they ensure an unbroken pay history with no missing periods
- **Intermediate stubs track timing details** - when 401k contributions were made (for IRS limit tracking), when RSUs vested, when bonuses paid
- **Gap detection** warns if stubs are missing or if you don't have the latest/final stub

```bash
pay-calc analysis 2025 him
```

### Stage 3: Tax Projection

The `tax-projection` command uses W-2 data or analysis output to calculate tax liability:

```bash
pay-calc tax-projection 2024
```

## Command Inventory

| Command | Primary Function | Input | Output |
|---------|------------------|-------|--------|
| `records import` | Convert PDFs to JSON | Drive folder or local folder | Local JSON records |
| `records list` | View imported records | Local JSON records | Table display |
| `analysis` | Validate stubs, extract YTD totals | Local JSON records | `YYYY_party_pay_all.json` |
| `projection` | Project year-end totals | Analysis output | Projection report |
| `taxes` | Calculate tax liability | W-2 JSON or analysis output | `YYYY_tax_projection.csv` |

### Command Details

**`records import`** - Import pay records from PDF sources

Downloads PDFs from Google Drive or local folders, extracts structured data, validates, and stores as JSON:
- Text-based PDFs: Uses PyPDF2 and pattern matching to extract data
- Image-based PDFs: Uses Gemini OCR for extraction
- Validates extracted data (schema, math checks)
- Stores in local records directory for use by analysis commands

**`analysis`** - Pay stub analysis (single party)

**Goal:** Extract accurate year totals from the most recent stub's YTD values, while validating data completeness.

- **Year totals from final stub YTD** - gross, taxes, FIT taxable wages all come from YTD of most recent stub
- **Completeness validation** - detects gaps in pay history (missing pay periods)
- **Consistency checks** - validates current amounts vs YTD increases
- **Continuity tracking** - detects employer changes and YTD resets
- **Timing details** - tracks when 401k contributions, RSU vests, and bonuses occurred

Reads from local records (imported via `records import`) and produces aggregated output.

**`projection`** - Year-end projection from partial year data

Reads the JSON output from `analysis` and projects year-end totals:
- Analyzes regular pay cadence to project remaining pay periods
- Detects stock vesting pattern to project remaining vests
- Projects 401k contributions to annual limit
- Estimates tax withholding based on effective rate

**`taxes`** - Federal tax calculation
- Loads W-2 data for both parties (him + her)
- Falls back to analysis output if W-2s not available (mid-year projections)
- Applies tax brackets, calculates liability, determines refund/owed

## Configuration

### profile.yaml (Your personal data)

Contains your private configuration:
- `drive`: Google Drive folder IDs for W-2s and pay stubs
- `parties`: him/her definitions with employer keywords

This data is consequential - store it in a config repo you control.

### settings.json (Machine-specific, auto-managed)

Located in `~/.config/pay-calc/settings.json`. Automatically managed by CLI:
- `profile`: Path to your profile.yaml (set via `pay-calc profile use`)

You typically don't edit this file directly.

### Profile Resolution Order

1. `PAY_CALC_CONFIG_PATH` environment variable (if set)
2. `settings.json` → `profile` key (if set via CLI)
3. `~/.config/pay-calc/profile.yaml` (XDG default)

### Setup for Config Repo Pattern

If you keep your profile in a separate config repo:

```bash
# Create profile manually or copy from example
cp config.yaml.example ~/repos/my-config/pay-calc/profile.yaml
# Edit with your Drive folder IDs, employer config, etc.

# Point pay-calc to use it
pay-calc profile use ~/repos/my-config/pay-calc/profile.yaml

# Verify
pay-calc profile show
```

This writes the path to `~/.config/pay-calc/settings.json`, so it works
from any directory without environment variables.

To copy an existing profile to a config repo:
```bash
pay-calc profile export ~/repos/my-config/pay-calc/profile.yaml --set-path
```

### Data Paths (XDG Base Directory Spec)

| Type | Path | Purpose |
|------|------|---------|
| Config | `~/.config/pay-calc/` | settings.json, default profile.yaml location |
| Cache | `~/.cache/pay-calc/` | Downloaded PDFs (regeneratable) |
| Data | `~/.local/share/pay-calc/` | Imported records and output files |
| Records | `~/.local/share/pay-calc/records/` | Imported pay stubs and W-2s as JSON |

## Project Structure

- `paycalc/` - Python package (SDK and CLI)
  - `sdk/` - Core logic (config, records, tax calculations)
  - `cli/` - Click-based CLI commands
- `tax-rules/` - YAML files with tax brackets by year
- `docs/` - Additional documentation
  - [paystubs.md](docs/paystubs.md) - Pay stub quirks and validation

## Dependencies

- Python 3.x
- PyPDF2 - For PDF text extraction
- PyYAML - For reading configuration files
- gwsa - For Google Drive integration (optional, for Drive imports)
- gemini-client - For OCR of image-based PDFs (optional)

Install dependencies:
```bash
pip install PyPDF2 PyYAML
```
