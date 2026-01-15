"""Validate pay stub model against real stubs from the records database.

SDK layer - pure logic, returns comparison dicts. No CLI or presentation.

Usage:
    from paycalc.sdk.modeling.validate import validate_stub

    result = validate_stub("abc12345", iterative=True)
    if result["match"]:
        print("Model matches actual stub!")
    else:
        print(f"Discrepancies: {result['discrepancies']}")
"""

from typing import Any, Dict, List, Optional, Tuple

from ..records import get_record, list_records
from ..stub_model import model_stub, model_stubs_in_sequence
from ..config import normalize_deduction_type


def is_supplemental_stub(data: Dict[str, Any]) -> bool:
    """Determine if a stub is supplemental pay (bonus, RSU, etc.) vs regular.

    A stub is considered supplemental if it has non-zero current amounts for
    earnings types like Bonus, RSU, Spot Bonus, Peer Bonus, etc.
    """
    SUPPLEMENTAL_KEYWORDS = (
        "bonus", "rsu", "stock", "gsu", "peer", "spot", "supplemental",
        "award", "equity", "vesting", "grant",
    )

    earnings = data.get("earnings", [])
    if isinstance(earnings, dict):
        earnings = [{"type": k, **v} if isinstance(v, dict) else {"type": k, "current_amount": v}
                    for k, v in earnings.items()]

    for earn in earnings:
        raw_type = (earn.get("type") or earn.get("name") or "").lower()
        amount = earn.get("current_amount") or earn.get("amount") or earn.get("current") or 0

        if amount > 0:
            for keyword in SUPPLEMENTAL_KEYWORDS:
                if keyword in raw_type:
                    return True

    return False


def build_history_from_prior_stubs(
    party: str,
    year: str,
    target_pay_date: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Query prior stubs and build supplementals + special_deductions + comp_plan_history.

    Args:
        party: Party identifier
        year: Tax year
        target_pay_date: Target pay date (YYYY-MM-DD) - excludes this and later

    Returns:
        Tuple of (supplementals, special_deductions, comp_plan_history) lists
    """
    from ..comp.salary_changes import identify_salary_changes

    all_stubs = list_records(year=year, party=party, type_filter="stub")

    supplementals = []
    special_deductions = []

    for record in all_stubs:
        data = record.get("data", {})
        stub_date = data.get("pay_date", "")

        if not stub_date or stub_date >= target_pay_date:
            continue

        inputs = extract_inputs_from_stub(data)

        if is_supplemental_stub(data):
            supp_entry = {
                "date": stub_date,
                "gross": inputs["gross"],
            }
            if inputs["pretax_401k"] > 0:
                supp_entry["401k"] = inputs["pretax_401k"]
            supplementals.append(supp_entry)
        else:
            if inputs["pretax_401k"] > 0:
                special_deductions.append({
                    "date": stub_date,
                    "401k": inputs["pretax_401k"],
                })

    supplementals.sort(key=lambda x: x["date"])
    special_deductions.sort(key=lambda x: x["date"])

    comp_plan_history = identify_salary_changes(
        f"{year}-01-01",
        f"{year}-12-31",
        party=party,
    )

    return supplementals, special_deductions, comp_plan_history


def extract_inputs_from_stub(data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract model inputs from a pay stub's data.

    Returns:
        Dict with gross, pay_frequency, pretax_401k, and benefits dict
    """
    pay_summary = data.get("pay_summary", {})
    current = pay_summary.get("current", {})

    gross = current.get("gross", 0)
    pay_frequency = "biweekly"  # Default - most common

    deductions = data.get("deductions", [])
    pretax_401k = 0
    benefits = {}

    if isinstance(deductions, dict):
        deductions = [
            {"type": k, **v} if isinstance(v, dict) else {"type": k, "current_amount": v}
            for k, v in deductions.items()
        ]

    for ded in deductions:
        raw_type = ded.get("type") or ded.get("name") or ""
        amount = ded.get("current_amount") or ded.get("amount") or ded.get("current") or 0

        if amount <= 0:
            continue

        canonical = normalize_deduction_type(raw_type)

        if canonical == "401k":
            pretax_401k = amount
        elif canonical == "health":
            benefits["pretax_health"] = amount
        elif canonical == "dental":
            benefits["pretax_dental"] = amount
        elif canonical == "vision":
            benefits["pretax_vision"] = amount
        elif canonical == "fsa":
            benefits["pretax_fsa"] = amount
        elif canonical == "hsa":
            benefits["pretax_hsa"] = amount
        elif canonical == "life":
            benefits["pretax_life"] = amount
        elif canonical == "disability":
            benefits["pretax_disability"] = amount

    return {
        "gross": gross,
        "pay_frequency": pay_frequency,
        "pretax_401k": pretax_401k,
        "benefits": benefits,
    }


def extract_actuals_from_stub(data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract actual values from a pay stub for comparison.

    Returns:
        Dict with current and ytd sub-dicts of actual values
    """
    pay_summary = data.get("pay_summary", {})
    current_pay = pay_summary.get("current", {})
    ytd_pay = pay_summary.get("ytd", {})
    taxes = data.get("taxes", {})

    fit = taxes.get("federal_income_tax", {})
    ss = taxes.get("social_security", {})
    med = taxes.get("medicare", {})

    pretax_total = 0
    deductions = data.get("deductions", [])
    if isinstance(deductions, dict):
        deductions = [
            {"type": k, **v} if isinstance(v, dict) else {"type": k, "current_amount": v}
            for k, v in deductions.items()
        ]
    for ded in deductions:
        amount = ded.get("current_amount") or ded.get("amount") or ded.get("current") or 0
        pretax_total += amount

    fit_taxable = ytd_pay.get("fit_taxable_wages", 0)
    current_fit_taxable = current_pay.get("gross", 0) - pretax_total

    return {
        "current": {
            "gross": current_pay.get("gross", 0),
            "fit_taxable": current_fit_taxable,
            "fit_withheld": fit.get("current_withheld", 0),
            "ss_withheld": ss.get("current_withheld", 0),
            "medicare_withheld": med.get("current_withheld", 0),
            "net_pay": data.get("net_pay", 0),
        },
        "ytd": {
            "gross": ytd_pay.get("gross", 0),
            "fit_taxable": fit_taxable,
            "fit_withheld": fit.get("ytd_withheld", 0),
            "ss_wages": ss.get("ytd_wages", ytd_pay.get("gross", 0)),
            "ss_withheld": ss.get("ytd_withheld", 0),
            "medicare_wages": med.get("ytd_wages", ytd_pay.get("gross", 0)),
            "medicare_withheld": med.get("ytd_withheld", 0),
        },
    }


def compare_values(
    modeled: Dict[str, Any],
    actual: Dict[str, Any],
    prefix: str = "",
) -> List[Dict[str, Any]]:
    """Compare modeled vs actual values.

    Returns:
        List of dicts with field, modeled, actual, diff for mismatches
    """
    diffs = []

    for key, actual_val in actual.items():
        if isinstance(actual_val, dict):
            modeled_sub = modeled.get(key, {})
            sub_prefix = f"{prefix}{key}." if prefix else f"{key}."
            diffs.extend(compare_values(modeled_sub, actual_val, sub_prefix))
        else:
            modeled_val = modeled.get(key, 0)
            diff = round(modeled_val - actual_val, 2)
            if abs(diff) >= 0.01:
                field_name = f"{prefix}{key}" if prefix else key
                diffs.append({
                    "field": field_name,
                    "modeled": modeled_val,
                    "actual": actual_val,
                    "diff": diff,
                })

    return diffs


def validate_stub(
    record_id: str,
    iterative: bool = True,
    auto_history: bool = True,
    supplementals: Optional[List[Dict[str, Any]]] = None,
    special_deductions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Validate a pay stub model against an actual stub record.

    Args:
        record_id: 8-character record ID
        iterative: Use model_stubs_in_sequence for accurate YTD (default True)
        auto_history: Auto-build history from prior stubs (default True)
        supplementals: Optional list of supplemental pay events
        special_deductions: Optional list of per-date 401k overrides

    Returns:
        Dict with:
            - record_id: The validated record ID
            - party: Party identifier
            - pay_date: Pay date from stub
            - model: Model function used
            - periods_modeled: Number of periods (if iterative)
            - inputs: Extracted inputs from stub
            - modeled: Model output values
            - actual: Actual stub values
            - discrepancies: List of field differences
            - match: True if no discrepancies
            - error: Error message if validation failed
    """
    # Load the record
    record = get_record(record_id)
    if not record:
        return {"error": f"Record '{record_id}' not found", "match": False}

    meta = record.get("meta", {})
    if meta.get("type") != "stub":
        return {"error": f"Record '{record_id}' is not a stub (type={meta.get('type')})", "match": False}

    data = record.get("data", {})
    pay_date = data.get("pay_date", "unknown")
    party = meta.get("party", "unknown")

    # Extract inputs from stub
    inputs = extract_inputs_from_stub(data)

    # Build overrides for model
    comp_plan_override = {
        "gross_per_period": inputs["gross"],
        "pay_frequency": inputs["pay_frequency"],
    }
    benefits_override = inputs["benefits"] if inputs["benefits"] else None

    # Run model
    if iterative:
        comp_plan_history = None
        if auto_history and supplementals is None and special_deductions is None:
            year = pay_date[:4]
            auto_supplementals, auto_special_deductions, comp_plan_history = build_history_from_prior_stubs(
                party, year, pay_date
            )
            if supplementals is None:
                supplementals = auto_supplementals
            if special_deductions is None:
                special_deductions = auto_special_deductions

        year_int = int(pay_date[:4])
        result = model_stubs_in_sequence(
            year_int,
            party,
            comp_plan_override=comp_plan_override,
            comp_plan_history=comp_plan_history,
            benefits_override=benefits_override,
            pretax_401k=inputs["pretax_401k"],
            supplementals=supplementals,
            special_deductions=special_deductions,
        )

        # Find the stub matching the target pay_date
        if result.get("stubs"):
            for stub in result["stubs"]:
                if stub.get("date") == pay_date:
                    result["current"] = stub
                    break
            else:
                result["current"] = result["stubs"][-1]
        else:
            result["current"] = {}
        model_name = "model_stubs_in_sequence"
    else:
        result = model_stub(
            pay_date,
            party,
            comp_plan_override=comp_plan_override,
            benefits_override=benefits_override,
            pretax_401k=inputs["pretax_401k"],
        )
        model_name = "model_stub"

    if "error" in result:
        return {"error": f"Model error: {result['error']}", "match": False}

    # Extract actuals from stub
    actuals = extract_actuals_from_stub(data)

    # Build modeled values in same structure
    current = result.get("current", {})
    ytd = result.get("ytd", {})

    modeled = {
        "current": {
            "gross": current.get("gross", 0),
            "fit_taxable": current.get("fit_taxable", 0),
            "fit_withheld": current.get("fit_withheld", 0),
            "ss_withheld": current.get("ss_withheld", 0),
            "medicare_withheld": current.get("medicare_withheld", 0),
            "net_pay": current.get("net_pay", 0),
        },
        "ytd": {
            "gross": ytd.get("gross", 0),
            "fit_taxable": ytd.get("fit_taxable", 0),
            "fit_withheld": ytd.get("fit_withheld", 0),
            "ss_wages": ytd.get("ss_wages", 0),
            "ss_withheld": ytd.get("ss_withheld", 0),
            "medicare_wages": ytd.get("medicare_wages", 0),
            "medicare_withheld": ytd.get("medicare_withheld", 0),
        },
    }

    # Compare
    discrepancies = compare_values(modeled, actuals)

    return {
        "record_id": record_id,
        "party": party,
        "pay_date": pay_date,
        "model": model_name,
        "periods_modeled": result.get("periods_modeled"),
        "inputs": inputs,
        "modeled": modeled,
        "actual": actuals,
        "discrepancies": discrepancies,
        "match": len(discrepancies) == 0,
    }
