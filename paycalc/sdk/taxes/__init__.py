"""taxes - Tax calculation and withholding logic.

Scope:
- Federal tax rules and brackets (IRS Pub 15-T)
- Per-period withholding calculations (FIT, SS, Medicare)
- FICA rounding compensation (IRS Form 941 line 7)
- Tax projections and W-2 generation

Constraints:
- Pure calculation - no party-specific config (that's in employee/)
- No records access - receives data, returns results
- Year-specific rules loaded from config/tax_rules/{year}.yaml

Modules:
- withholding: Per-period FICA and FIT calculations
- other: Mixed tax utilities - needs modularization. Contains:
    - Tax rules loading (load_tax_rules, get_tax_rule)
    - Federal/Medicare tax calculations
    - Tax projections (generate_projection, CSV output)
    - W-2 data loading and generation

Usage:
    from paycalc.sdk.taxes import calc_period_taxes, load_tax_rules

    taxes = calc_period_taxes(gross=5000, prior_ytd={...}, year="2025", w4={...})
    rules = load_tax_rules("2025")
"""

# Per-period withholding calculations
from .withholding import (
    calc_period_taxes,
    calc_ss_withholding,
    calc_medicare_withholding,
    truncate_cents,
    round_with_compensation,
)

# Tax rules schemas
from .schemas import TaxRules

# Tax rules loading and calculations
from .other import (
    load_tax_rules,
    get_ss_wage_cap,
    calculate_federal_income_tax,
    calculate_additional_medicare_tax,
)

__all__ = [
    # Withholding
    "calc_period_taxes",
    "calc_ss_withholding",
    "calc_medicare_withholding",
    "truncate_cents",
    "round_with_compensation",
    # Rules
    "TaxRules",
    "load_tax_rules",
    "get_ss_wage_cap",
    "calculate_federal_income_tax",
    "calculate_additional_medicare_tax",
]
