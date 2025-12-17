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
pay-calc w2-extract 2024 --cache      # Extract W-2 data from Google Drive
pay-calc tax-projection 2024          # Generate tax projection
pay-calc pay-analysis 2025 --cache    # Process pay stubs (single party)
pay-calc pay-projection 2025          # Project year-end (requires pay-analysis first)
pay-calc household-ytd 2025           # Aggregate YTD across all parties

# Configuration management
pay-calc config path                  # Show config paths and active profile
pay-calc config set-profile <path>    # Point to profile in your config repo
pay-calc config show                  # Show machine settings (settings.json)

# Profile management (your personal data)
pay-calc profile show                 # Show active profile (profile.yaml)
pay-calc profile init                 # Create new profile
pay-calc profile set <key> <value>    # Set profile value (e.g., Drive folder IDs)
```

## Command Inventory

| Command | Primary Function | Input | Output | Parties |
|---------|------------------|-------|--------|---------|
| `w2-extract` | Parse W-2 PDFs from Drive | W-2 PDFs, manual JSON | `YYYY_party_w2_forms.json` | Single |
| `tax-projection` | Calculate tax liability | W-2 JSON (or YTD JSON fallback) | `YYYY_tax_projection.csv` | Both (combined) |
| `pay-analysis` | Validate pay stubs, report YTD | Pay stub PDFs from Drive | `YYYY_pay_stubs_full.json` | Single |
| `pay-projection` | Project year-end totals | `YYYY_pay_stubs_full.json` | Projection report | Single |
| `household-ytd` | Aggregate YTD across parties | `YYYY_party_pay_stubs.json` | `YYYY_party_ytd.json` | Both (per-party + combined) |

### Command Details

**`w2-extract`** - Year-end W-2 processing
- Downloads W-2 PDFs from configured Google Drive folder
- Parses text-based PDFs; supports manual JSON for image-based W-2s
- Outputs structured JSON for use by `tax-projection`

**`tax-projection`** - Federal tax calculation
- Loads W-2 data for both parties (him + her)
- Falls back to YTD JSON if W-2s not available (mid-year projections)
- Applies tax brackets, calculates liability, determines refund/owed

**`pay-analysis`** - Pay stub analysis (single party)

Multi-level validation:
- **Completeness**: Detects gaps in pay history (missing pay periods)
- **Consistency**: Validates current amounts vs YTD increases
- **Continuity**: Detects employer changes and YTD resets

Financial event timeline extraction:
- 401k contributions with dates (for month-to-month accounting)
- Pay breakdown by type (regular, bonus, stock grants)
- Future: Stock vesting timeline for investment tracking

**Why this exists**: Personal accounting software like Monarch tracks balance changes over time, but those include market fluctuations. To accurately track:
- Income/expense by month (not just balance changes)
- Timing of investments for true ROI calculation
- 401k contributions separate from employer match

We need source-of-truth data from pay stubs showing when money was earned and invested, not just when account balances changed.

**Why single-party**: This is personal finance tracking, not joint tax calculation. Each person has their own pay stubs, 401k accounts, stock grants, and investment timing. The `household-ytd` and `tax-projection` commands handle the joint/combined view for tax purposes.

Outputs `YYYY_pay_stubs_full.json` which serves as input for `pay-projection`.

**`pay-projection`** - Year-end projection from partial year data

**Prerequisite**: Requires `pay-analysis` output - run `pay-analysis` first.

Reads the JSON output from `pay-analysis` and projects year-end totals:
- Analyzes regular pay cadence to project remaining pay periods
- Detects stock vesting pattern to project remaining vests
- Projects 401k contributions to annual limit
- Estimates tax withholding based on effective rate

Use for mid-year tax planning when W-2 data is not yet available.

```bash
# Step 1: Run pay-analysis to generate the JSON
pay-calc pay-analysis 2025 --cache

# Step 2: Run pay-projection using that output
pay-calc pay-projection 2025
```

**`household-ytd`** - Multi-party YTD aggregation
- Reads local pay stub JSON files for all parties
- Extracts YTD from latest stub per employer
- Outputs `_ytd.json` files that `tax-projection` can use as fallback

## Data Flow

```
YEAR-END (W-2 available):
  W-2 PDFs ──► w2-extract ──► YYYY_party_w2_forms.json ──► tax-projection ──► CSV

MID-YEAR (no W-2 yet):
  Pay stub PDFs ──► extract_pay_stub.py ──► YYYY_party_pay_stubs.json
                                                    │
                                                    ▼
                                            household-ytd
                                                    │
                                                    ▼
                                          YYYY_party_ytd.json ──► tax-projection ──► CSV
                                                                    (fallback input)
```

## Configuration

Configuration is split into two files:

### settings.json (Machine-specific)

Located in `~/.config/pay-calc/settings.json`. Contains:
- `profile`: Path to your profile.yaml (if not in default location)
- Tool preferences (output format, etc.)

These are ephemeral settings that can be recreated easily.

### profile.yaml (Your personal data)

Contains your private configuration:
- `drive`: Google Drive folder IDs for W-2s and pay stubs
- `parties`: him/her definitions with employer keywords

This data is consequential - store it in a config repo you control.

### Profile Resolution Order

1. `PAY_CALC_CONFIG_PATH` environment variable (if set)
2. `settings.json` → `profile` key (if set via CLI)
3. `~/.config/pay-calc/profile.yaml` (XDG default)

### Setup for Config Repo Pattern

If you keep your profile in a separate config repo:

```bash
# Create profile in your config repo
pay-calc profile init --path ~/repos/my-config/pay-calc/profile.yaml

# Point pay-calc to use it
pay-calc config set-profile ~/repos/my-config/pay-calc/profile.yaml

# Verify
pay-calc config path
```

This writes the path to `~/.config/pay-calc/settings.json`, so it works
from any directory without environment variables.

### Data Paths (XDG Base Directory Spec)

| Type | Path | Purpose |
|------|------|---------|
| Config | `~/.config/pay-calc/` | settings.json, default profile.yaml location |
| Cache | `~/.cache/pay-calc/` | Downloaded PDFs (regeneratable) |
| Data | `~/.local/share/pay-calc/` | Output files (W-2 JSON, projections) |

## Overview

The core workflow:
1. **Extract W-2 Data**: Download from Google Drive, parse PDFs, output structured JSON
2. **Generate Projection**: Calculate tax liability from W-2 data using year's tax rules

Handles multiple W-2s per individual (party) in a household.

## Project Structure

- `paycalc/` - Python package (SDK and CLI)
  - `sdk/` - Core logic (config, tax calculations)
  - `cli/` - Click-based CLI commands
- `tax-rules/` - YAML files with tax brackets by year
- `docs/` - Additional documentation
  - [paystubs.md](docs/paystubs.md) - Pay stub quirks and validation

---

## The Workflow

### Full Year Pay Stub Processing

The `analysis.py` script (called via `pay-calc pay-analysis`) downloads and processes all pay stubs for a given year from Google Drive, validates them, and generates a comprehensive report.

**Usage:**
```bash
pay-calc pay-analysis <year> [--cache] [--through-date YYYY-MM-DD]
# Or directly:
python3 analysis.py <year> [--format text|json] [--cache-paystubs]
```

**What it does:**
1. Downloads multi-period PDF files from Drive
2. Splits them into individual pay stubs
3. Extracts earnings, taxes, deductions from each stub
4. Validates for gaps (missing pay periods)
5. Validates current vs YTD consistency per field
6. Detects employer changes (mid-year job switches)
7. Generates year-end summary with totals

**Validation:**
- Compares displayed "Current" values to actual YTD increases
- Flags discrepancies as warnings or errors (see [paystubs.md](docs/paystubs.md))
- Exits with code 1 if any errors detected

**Output:**
- Text report to stdout (default)
- JSON report with `--format json`
- Full data saved to `data/YYYY_pay_stubs_full.json`

### Year-End Projection

Use `pay-calc pay-projection` to project year-end totals from partial year data:

```bash
# First run analysis
pay-calc pay-analysis 2025 --cache

# Then run projection
pay-calc pay-projection 2025
```

The projection analyzes pay patterns:
- **Regular Pay**: Detects pay frequency (biweekly) and projects remaining pay periods
- **Stock Grants**: Detects vesting pattern by month and projects remaining vests
- **401k**: Projects contributions to annual limit
- **Taxes**: Estimates additional withholding using effective tax rate

This is useful for mid-year tax planning and estimating quarterly payments.

---

### Step 1: Extract W-2 Data

The `extract_w2.py` script is responsible for finding all W-2 data for a person (a "party", e.g., 'him' or 'her') for a specific year and consolidating it into a single file.

**Usage:**
```bash
python3 extract_w2.py <year> <party>
```
Example:
```bash
# Process all W-2 sources for 'her' for 2024
python3 extract_w2.py 2024 her
```

#### Data Sources

The script looks for two types of data sources in the `source-data/` directory:

1.  **W-2 PDFs**: Text-based PDF files of W-2s. The script identifies the correct PDFs based on keywords in the filename (e.g., 'W-2', the year, and employer names defined in `profile.yaml`).
2.  **Manual W-2 JSONs**: For W-2s that are image-based or cannot be parsed, you can create a manual JSON file. These files **must** follow a specific naming convention:
    `YYYY_manual-w2_{party}_{employer-slug}.json`

    -   `YYYY`: The four-digit year.
    -   `{party}`: The party identifier (e.g., 'him', 'her').
    -   `{employer-slug}`: A short, lowercase name for the employer (e.g., 'employer_b', 'employer_a').

    Example: `source-data/2024_manual-w2_her_employer_b.json`

#### Conflict Resolution

The script is designed to prevent data duplication. It will raise an error if it finds both a PDF and a manual JSON file for the same employer, forcing you to remove one to ensure there is only one source of truth per W-2.

#### Output: `data/YYYY_{party}_w2_forms.json`

The script generates a single JSON file per party and year. This file contains a `forms` array, where each object in the array represents a single W-2.

**Example `data/2024_her_w2_forms.json`:**
```json
{
  "year": 2024,
  "party": "her",
  "forms": [
    {
      "employer": "employer_b",
      "source_type": "manual",
      "source_file": "2024_manual-w2_her_employer_b.json",
      "data": {
        "wages_tips_other_comp": 70000.00,
        "federal_income_tax_withheld": 5000.00,
        // ... other financial data
      }
    },
    {
      "employer": "employer_c",
      "source_type": "manual",
      "source_file": "2024_manual-w2_her_employer_c.json",
      "data": {
        "wages_tips_other_comp": 9500.00,
        "federal_income_tax_withheld": 446.14,
        // ... other financial data
      }
    }
  ]
}
```

---

### Step 2: Generate Tax Projection

Once the W-2 data has been extracted, the `generate_tax_projection.py` script calculates the tax projection.

**Usage:**
```bash
python3 generate_tax_projection.py <year>
```
Example:
```bash
python3 generate_tax_projection.py 2024
```

#### Process

1.  **Load Data**: For the given year, it loads the `..._w2_forms.json` file for each party ('him' and 'her'). It aggregates the financial data from all forms within each file to get the total wages, taxes withheld, etc., for each person.
2.  **Load Tax Rules**: It loads the corresponding `tax-rules/YYYY.yaml` file to get the tax brackets, standard deduction, and other constants.
3.  **Calculate Taxes**: It performs the tax calculations:
    -   Combines income for both parties.
    -   Subtracts the standard deduction to determine taxable income.
    -   Calculates federal income tax based on the progressive tax brackets.
    -   Calculates Medicare taxes (including the additional tax for high earners).
    -   Determines the final estimated refund or amount owed.
4.  **Generate CSV Output**: It creates a detailed projection in `data/YYYY_tax_projection.csv`. This CSV file provides a breakdown similar to a 1040 tax form, showing how the final numbers were calculated.

---

## Dependencies

-   Python 3.x
-   PyPDF2 - For PDF text extraction
-   PyYAML - For reading configuration files

Install dependencies:
```bash
pip install PyPDF2 PyYAML
```

---

## Future Considerations

### Workflow Integration

**`pay-analysis` output is disconnected:**
- Currently outputs `_pay_stubs_full.json` (single consolidated file)
- This format is not consumed by `household-ytd` or `tax-projection`
- `household-ytd` expects `_party_pay_stubs.json` (per-party files)

**Options to consider:**
1. Have `pay-analysis` output per-party `_pay_stubs.json` files
2. Enhance `household-ytd` to also accept `_pay_stubs_full.json` (would require party breakdown in that format)
3. Keep workflows separate (Drive-based vs local-file-based)

### Standalone Scripts

Several standalone Python scripts exist alongside CLI commands:
- `extract_pay_stub.py` - Single PDF processor (outputs `_party_pay_stubs.json`)
- `calc_ytd.py` - YTD aggregation (now wrapped by `household-ytd` command)
- `analysis.py` - Full year pay stub processor (called by `pay-analysis`)
- `projection.py` - Year-end projection (called by `pay-projection`)
- `generate_tax_projection.py` - Tax calculation (called by `tax-projection`)

These may eventually be fully integrated into the CLI/SDK structure.

### Party Awareness and Drive Folder Consolidation

Currently, Drive folders are party-specific (one folder per party's pay stubs). Consolidating into a single folder requires all commands to be party-aware:

- Pay stubs must be identified by party (filename convention or metadata)
- Commands processing Drive folders must filter by party
- Cannot accidentally mix parties' pay records

Until this is implemented, Drive folders must remain separate per party.

A future `pay-projection` command (actual projection, separate from `pay-analysis`) could:
- Consume `pay-analysis` and/or `household-ytd` outputs
- Work for single party or combined household
- Be party-aware without requiring party-specific Drive folders

