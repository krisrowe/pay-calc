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
pay-calc pay-projection 2025 --cache  # Process pay stubs
pay-calc config path                  # Show config location
pay-calc config show                  # Show current config
```

## Configuration

Configuration is loaded from (in order):

1. `PAY_CALC_CONFIG_PATH` environment variable (if set)
2. `./pay-calc/config.yaml` in current working directory (if exists)
3. `~/.config/pay-calc/config.yaml` (XDG default)

Initialize a new config:
```bash
pay-calc config init          # Creates ~/.config/pay-calc/config.yaml
pay-calc config init --local  # Creates ./pay-calc/config.yaml
```

### Data Paths (XDG Base Directory Spec)

| Type | Path | Purpose |
|------|------|---------|
| Config | `~/.config/pay-calc/` | Settings, Drive folder IDs, party config |
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

The `process_year.py` script downloads and processes all pay stubs for a given year from Google Drive, validates them, and generates a comprehensive report.

**Usage:**
```bash
python3 process_year.py <year> [--format text|json] [--projection]
```

Example:
```bash
python3 process_year.py 2025
python3 process_year.py 2025 --format json
python3 process_year.py 2025 --projection
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

**Projection (--projection flag):**
When the `--projection` flag is passed, the script analyzes pay patterns and projects year-end totals:

- **Regular Pay**: Detects pay frequency (biweekly) and projects remaining pay periods
- **Stock Grants**: Detects vesting pattern by month and projects remaining vests
- **Taxes**: Estimates additional withholding using effective tax rate from actuals

The projection table shows:
| Column | Description |
|--------|-------------|
| Actual | Current YTD totals from last pay stub |
| Projected Add | Estimated additional income before year-end |
| Est. Total | Projected year-end totals (Actual + Projected) |

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

1.  **W-2 PDFs**: Text-based PDF files of W-2s. The script identifies the correct PDFs based on keywords in the filename (e.g., 'W-2', the year, and employer names defined in `config.yaml`).
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

