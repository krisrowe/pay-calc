"""Employee records access with canonical data transformation.

Schema-on-read layer that retrieves pay stubs and W-2s from the generic
records database and transforms them to canonical form.

All downstream code (modeling, validation, analysis) should use these
functions rather than the generic get_record() to ensure consistent,
canonical data with no surprises.
"""

from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from ..records import get_record
from ..schemas import PayStub, PaySummary, PayLineItem, TaxAmounts, DeductionTotals
from ..taxes import get_ss_wage_cap


# =============================================================================
# Deduction type canonicalization
# =============================================================================

_DEDUCTION_MAPPINGS: Optional[Dict[str, str]] = None
_SORTED_PATTERNS: Optional[list] = None


def _load_deduction_mappings() -> Dict[str, str]:
    """Load deduction type mappings from mappings.yaml."""
    global _DEDUCTION_MAPPINGS, _SORTED_PATTERNS

    if _DEDUCTION_MAPPINGS is not None:
        return _DEDUCTION_MAPPINGS

    mappings_path = Path(__file__).parent.parent.parent / "config" / "mappings.yaml"
    if mappings_path.exists():
        with open(mappings_path) as f:
            data = yaml.safe_load(f) or {}
            _DEDUCTION_MAPPINGS = data.get("deduction_types", {})
    else:
        _DEDUCTION_MAPPINGS = {}

    # Sort patterns by length (longest first) so more specific patterns
    # match before shorter ones. E.g., "fsa health" matches before "health".
    _SORTED_PATTERNS = sorted(
        _DEDUCTION_MAPPINGS.items(),
        key=lambda x: len(x[0]),
        reverse=True,
    )

    return _DEDUCTION_MAPPINGS


def canonicalize_deduction_type(raw_type: str) -> str:
    """Canonicalize a deduction type name.

    Uses mappings.yaml to convert employer-specific deduction names
    (like "FSA Health", "Medical Insurance") to canonical types
    (like "fsa", "health").

    Matching is case-insensitive substring matching, with longer patterns
    checked first to ensure specificity (e.g., "fsa health" before "health").

    Args:
        raw_type: Raw deduction type from pay stub

    Returns:
        Canonical deduction type, or the original (lowercased) if no match
    """
    if not raw_type:
        return ""

    _load_deduction_mappings()

    raw_lower = raw_type.lower()

    # Check each pattern for substring match (longest first)
    for pattern, canonical in _SORTED_PATTERNS:
        if pattern.lower() in raw_lower:
            return canonical

    # No match - return original lowercased
    return raw_lower


# Deduction classification by tax treatment
FULLY_PRETAX_TYPES = {"health", "dental", "vision", "fsa", "hsa"}  # Section 125
RETIREMENT_TYPES = {"401k"}  # Pre-FIT only
POST_TAX_TYPES = {"vol_life", "401k_aftertax", "roth_401k", "life", "disability"}


def _classify_deduction(canonical_type: str) -> str:
    """Classify a deduction type by tax treatment."""
    if canonical_type in FULLY_PRETAX_TYPES:
        return "fully_pretax"
    elif canonical_type in RETIREMENT_TYPES:
        return "retirement"
    elif canonical_type in POST_TAX_TYPES:
        return "post_tax"
    return "post_tax"  # Unknown → conservative default


# =============================================================================
# Pay stub retrieval
# =============================================================================


def get_pay_stub(record_id: str) -> PayStub:
    """Get a validated pay stub by record ID.

    Transforms raw record data to canonical PayStub with:
    - Canonicalized deduction types
    - Classified deduction totals (fully_pretax, retirement, post_tax)
    - Proper PaySummary objects for current and ytd

    Args:
        record_id: The 8-char record ID

    Returns:
        Validated PayStub

    Raises:
        ValueError: If record not found or not a stub
        pydantic.ValidationError: If data doesn't conform to schema
    """
    record = get_record(record_id)
    if not record:
        raise ValueError(f"Record '{record_id}' not found")

    if record.get("meta", {}).get("type") != "stub":
        raise ValueError(f"Record '{record_id}' is not a stub")

    data = record["data"]

    # Transform earnings
    earnings = [
        PayLineItem(
            type=e.get("type", ""),
            current=e.get("current_amount", 0),
            ytd=e.get("ytd_amount", 0),
        )
        for e in data.get("earnings", [])
    ]

    # Transform and classify deductions
    deductions = []
    current_ded = {"fully_pretax": 0, "retirement": 0, "post_tax": 0}
    ytd_ded = {"fully_pretax": 0, "retirement": 0, "post_tax": 0}

    for d in data.get("deductions", []):
        raw_type = d.get("type") or d.get("category") or ""
        canonical = canonicalize_deduction_type(raw_type)
        curr = d.get("current_amount", 0)
        ytd = d.get("ytd_amount", 0)

        deductions.append(PayLineItem(type=canonical, current=curr, ytd=ytd))

        classification = _classify_deduction(canonical)
        current_ded[classification] += curr
        ytd_ded[classification] += ytd

    # Extract tax info
    taxes = data.get("taxes", {})
    fit = taxes.get("federal_income_tax", {})
    ss = taxes.get("social_security", {})
    med = taxes.get("medicare", {})

    pay_summary = data.get("pay_summary", {})
    current_pay = pay_summary.get("current", {})
    ytd_pay = pay_summary.get("ytd", {})

    current_gross = current_pay.get("gross", 0)
    ytd_gross = ytd_pay.get("gross", 0)

    # Extract year from pay_date for SS wage cap lookup
    pay_date = data.get("pay_date", "")
    year = pay_date[:4] if pay_date else "2025"
    ss_wage_cap = get_ss_wage_cap(year)

    # Compute expected taxable wages when not explicit in source
    # FICA taxable = gross - fully_pretax (Section 125 deductions)
    # FIT taxable = gross - fully_pretax - retirement
    # SS taxable is capped at wage base; Medicare has no cap
    current_fica_taxable = current_gross - current_ded["fully_pretax"]
    current_fit_taxable = current_fica_taxable - current_ded["retirement"]
    ytd_fica_taxable = ytd_gross - ytd_ded["fully_pretax"]
    ytd_fit_taxable = ytd_fica_taxable - ytd_ded["retirement"]
    ytd_ss_taxable = min(ytd_fica_taxable, ss_wage_cap)

    # Compute net_pay when not available (YTD net_pay isn't meaningful in stubs)
    current_withheld_total = (
        fit.get("current_withheld", 0) +
        ss.get("current_withheld", 0) +
        med.get("current_withheld", 0)
    )
    ytd_withheld_total = (
        fit.get("ytd_withheld", 0) +
        ss.get("ytd_withheld", 0) +
        med.get("ytd_withheld", 0)
    )
    computed_ytd_net = round(
        ytd_gross - ytd_ded["fully_pretax"] - ytd_ded["retirement"] - ytd_ded["post_tax"] - ytd_withheld_total,
        2,
    )

    # Build PaySummary objects
    current = PaySummary(
        gross=current_gross,
        deductions=DeductionTotals(**current_ded),
        taxable=TaxAmounts(
            fit=current_pay.get("fit_taxable_wages") or fit.get("taxable_wages") or current_fit_taxable,
            ss=ss.get("taxable_wages") or current_fica_taxable,
            medicare=med.get("taxable_wages") or current_fica_taxable,
        ),
        withheld=TaxAmounts(
            fit=fit.get("current_withheld", 0),
            ss=ss.get("current_withheld", 0),
            medicare=med.get("current_withheld", 0),
        ),
        net_pay=data.get("net_pay", 0),
    )

    ytd_summary = PaySummary(
        gross=ytd_gross,
        deductions=DeductionTotals(**ytd_ded),
        taxable=TaxAmounts(
            fit=ytd_pay.get("fit_taxable_wages") or ytd_fit_taxable,
            ss=ss.get("ytd_wages") or ytd_ss_taxable,
            medicare=med.get("ytd_wages") or ytd_fica_taxable,
        ),
        withheld=TaxAmounts(
            fit=fit.get("ytd_withheld", 0),
            ss=ss.get("ytd_withheld", 0),
            medicare=med.get("ytd_withheld", 0),
        ),
        net_pay=ytd_pay.get("net_pay") or computed_ytd_net,
    )

    period = data.get("period", {})
    party = record.get("meta", {}).get("party", "")

    return PayStub(
        party=party,
        employer=data.get("employer", ""),
        pay_date=data.get("pay_date", ""),
        period_start=period.get("start", ""),
        period_end=period.get("end", ""),
        earnings=earnings,
        deductions=deductions,
        current=current,
        ytd=ytd_summary,
    )


# =============================================================================
# W-2 retrieval (future)
# =============================================================================


def get_w2(record_id: str):
    """Get a validated W-2 by record ID.

    Future home for canonical W-2 retrieval from records.

    Args:
        record_id: The 8-char record ID

    Returns:
        Validated W2 model (schema TBD)

    Raises:
        NotImplementedError: Not yet implemented
    """
    raise NotImplementedError(
        "W-2 retrieval not yet implemented. "
        "This will be the canonical entry point for W-2 data, similar to get_pay_stub()."
    )
