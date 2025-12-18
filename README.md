# Personal Tax Projection

Tools for processing pay stubs, extracting W-2 data, and generating federal tax projections.

## Why This Project

This project serves multiple purposes:

**1. Tax Planning and Liability Projection:** The primary, practical goal is to generate a precise federal tax projection. By processing pay stubs throughout the year, we can project year-end tax liability, optimize 401k contributions, and plan estimated tax payments with confidence—rather than waiting until next year to discover what is owed.

**2. Practical AI Experimentation:** This project is an experiment in applying AI to personal productivity workflows. We've invested significant effort on what might seem like a narrow use case (pay stub analysis) precisely because it's a real-world test bed for AI-assisted tooling. The goal is to understand where AI helps (OCR for image PDFs, pattern detection) and where traditional approaches work better (text extraction, validation).

**3. Detailed Investment Tracking:** The detailed pay stub data enables tracking of investment contributions (401k, RSU vests, ESPP purchases) with exact dates and amounts. This is valuable for entering transactions into portfolio tools (Monarch, Yahoo Finance) and calculating accurate returns on investment portfolios. While aggregators like Monarch can theoretically track contributions, historically financial aggregators have struggled to do much more than track balance history for accounts—they often miss the detailed holding-level transaction data needed for accurate return calculations.

**4. A Template for Resilient, Portable Tooling:** A primary goal is to prove out a design pattern for building CLI tools that are both powerful and portable. Many useful, custom tools end up as "shelfware" because they are too dependent on a specific local machine's setup. They become brittle, difficult to share, and a nightmare to resurrect on a new workstation. This project directly confronts that problem by establishing a clear, repeatable configuration pattern that separates the public code (this repo) from the user's private data and configuration (a separate, private repo).

   **The problem this solves:** Most developers use their personal GitHub accounts on work laptops, collaborating on company projects and open source with a public identity. This creates real risk: it's far too easy to accidentally commit sensitive information, or to have something private appear on screen while browsing repos during a demo or screen share. The traditional alternatives are unsatisfying—either keep everything private (losing the benefits of public collaboration and open source contribution), or accept the constant vigilance required to keep personal data out of public repos.

   This pattern offers a third way: keep the *tool* public while keeping the *data* private. You get the benefits of cloud-backed storage for your personal tooling (sync across machines, backup, version history) without building something no one else can use. The public repo contains reusable code; the private repo contains your configuration and data references.

   The core philosophy is that your tools should not make your workstation a precious, irreplaceable artifact. You should be able to clone this public repository on any machine, point it to your private configuration file with a single command, and have it work instantly, reliably, and identically. This approach offers several key advantages:
    - **Resilience:** Your private data (like pay stubs on Google Drive) and configuration are managed independently, so the public code remains stateless and easily replaceable.
    - **Portability:** Setting up the tool on a new machine is trivial, eliminating the high cognitive load and lack of confidence often associated with rebuilding a complex local environment.
    - **Security:** It provides a safe way to use public code with private data, without ever committing sensitive information to a public repository. No more worrying about accidental exposure during demos or screen shares.
    - **A Workable Pattern:** If successful, this project serves as a template for other similar tools, demonstrating a practical way to build and maintain personal software that is robust, secure, and built to last.

Because of the AI component, we prioritize a **practical and repeatable configuration pattern**. The tool should be easy to set up on a new machine with minimal configuration steps, and the deployment strategy should be straightforward (local CLI, no server required). The Gemini CLI approach exemplifies this: users set up `gemini` CLI once, and the tool leverages it without additional API key management.

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
- **Multi-page PDFs:** Each page is processed separately, creating one JSON record per logical pay stub
  - A quarterly payroll PDF with 12 pages creates 12 individual record files
  - This enables accurate pattern detection for projections (RSU vesting frequency, pay cadence)
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
