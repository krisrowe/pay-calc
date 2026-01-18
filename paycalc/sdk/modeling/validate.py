"""Validate pay stub model against real stubs from the records database.

SDK layer - pure logic, returns comparison dicts. No CLI or presentation.

Validation compares modeled values against stub values. Discrepancies may indicate:
- Model logic errors (early in project lifecycle, most common)
- Extraction errors from original stub documents
- Payroll system quirks or errors (less common with large employers)

The validation is intentionally neutral about which side is "correct" - it simply
reports differences for investigation.

Two public validation methods:

1. validate_stub(record_id, fica_rounding_balance) - Non-iterative validation
   - Validates a single stub using only data from that stub
   - Requires explicit FicaRoundingBalance (use .none() if unknown)
   - Self-contained: computes prior_ytd by subtracting current from YTD

2. validate_stub_in_sequence(record_id, ...) - Iterative validation
   - Models all stubs from year start through target date
   - Computes FICA rounding balance from prior stub outputs
   - More accurate but requires prior stub data in database

Usage:
    from paycalc.sdk.modeling.validate import validate_stub, validate_stub_in_sequence
    from paycalc.sdk.schemas import FicaRoundingBalance

    # Non-iterative (single stub, self-contained)
    result = validate_stub("abc12345", FicaRoundingBalance.none())

    # Iterative (models sequence, more accurate)
    result = validate_stub_in_sequence("abc12345")

    if result["match"]:
        print("Model matches stub!")
    else:
        print(f"Discrepancies: {result['discrepancies']}")
"""

from typing import Any, Dict, List, Optional, Tuple, Union

from ..records import get_record, list_records
from ..employee.records import get_pay_stub
from ..schemas import FicaRoundingBalance, PayStub, PaySummary, DeductionTotals, TaxAmounts
from .schemas import Discrepancy, PeriodComparison, ValidateStubResult, StubSequenceResult
from .stub_modeler import model_stub, model_stubs_in_sequence


def is_supplemental_stub(stub: PayStub) -> bool:
    """Determine if a stub is supplemental pay (bonus, RSU, etc.) vs regular.

    A stub is considered supplemental if it has non-zero current amounts for
    earnings types like Bonus, RSU, Spot Bonus, Peer Bonus, etc.
    """
    SUPPLEMENTAL_KEYWORDS = (
        "bonus", "rsu", "stock", "gsu", "peer", "spot", "supplemental",
        "award", "equity", "vesting", "grant",
    )

    for earning in stub.earnings:
        if earning.current > 0:
            type_lower = earning.type.lower()
            for keyword in SUPPLEMENTAL_KEYWORDS:
                if keyword in type_lower:
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

    all_records = list_records(year=year, party=party, type_filter="stub")

    supplementals = []
    special_deductions = []

    for record in all_records:
        record_id = record.get("id", "")
        if not record_id:
            continue

        try:
            stub = get_pay_stub(record_id)
        except (ValueError, Exception):
            continue  # Skip invalid records

        if not stub.pay_date or stub.pay_date >= target_pay_date:
            continue

        inputs = extract_inputs_from_stub(stub)

        if is_supplemental_stub(stub):
            supp_entry = {
                "date": stub.pay_date,
                "gross": inputs["gross"],
            }
            if inputs["pretax_401k"] > 0:
                supp_entry["401k"] = inputs["pretax_401k"]
            supplementals.append(supp_entry)
        else:
            if inputs["pretax_401k"] > 0:
                special_deductions.append({
                    "date": stub.pay_date,
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


def extract_inputs_from_stub(stub: PayStub) -> Dict[str, Any]:
    """Extract model inputs from a pay stub.

    Returns:
        Dict with gross, pay_frequency, pretax_401k, benefits dict,
        and unclassified list of (type, amount) for unknown deductions
    """
    gross = stub.current.gross
    pay_frequency = "biweekly"  # Default - most common

    pretax_401k = 0
    benefits = {}
    unclassified = []

    # Known canonical types and their tax treatment
    # fully_pretax: reduces FIT and FICA (Section 125)
    # retirement: reduces FIT but not FICA (401k, 403b, 457b)
    # post_tax: reduces neither (Roth, after-tax, vol life)
    FULLY_PRETAX = {"health", "dental", "vision", "fsa", "hsa"}
    RETIREMENT = {"401k"}
    POST_TAX = {"vol_life", "401k_aftertax", "roth_401k", "life", "disability"}

    for ded in stub.deductions:
        if ded.current <= 0:
            continue

        canonical = ded.type  # Already canonicalized by get_pay_stub

        if canonical in RETIREMENT:
            pretax_401k = ded.current
        elif canonical in FULLY_PRETAX:
            benefits[f"pretax_{canonical}"] = ded.current
        elif canonical in POST_TAX:
            # Post-tax: tracked but doesn't affect tax calculations
            pass
        else:
            # Unknown - track for permutation inference
            unclassified.append((canonical, ded.current))

    return {
        "gross": gross,
        "pay_frequency": pay_frequency,
        "pretax_401k": pretax_401k,
        "benefits": benefits,
        "unclassified": unclassified,
    }


def infer_deduction_classifications(
    gross: float,
    known_pre_fit: float,
    known_pre_fica: float,
    unclassified: List[Tuple[str, str, float]],
    actual_fit_taxable: float,
    actual_fica_wages: float,
    tolerance: float = 0.50,
) -> Optional[List[Tuple[str, str, str]]]:
    """Try all permutations of tax classifications to find matching values.

    Pure stdlib, in-memory, no I/O. Fast for small N (3^N combinations).

    Args:
        gross: Gross pay
        known_pre_fit: Sum of known deductions that reduce FIT
        known_pre_fica: Sum of known deductions that reduce FICA
        unclassified: List of (raw_type, canonical, amount) for unknown deductions
        actual_fit_taxable: Actual FIT taxable wages from stub
        actual_fica_wages: Actual FICA wages from stub (or gross if not recorded)
        tolerance: Match tolerance in dollars (default 0.50)

    Returns:
        List of (raw_type, canonical, classification) if exactly one combo matches,
        None if zero or multiple matches
    """
    if not unclassified:
        return []

    n = len(unclassified)
    if n > 10:
        # 3^10 = 59049 - cap to avoid runaway
        import logging
        logging.getLogger(__name__).warning(
            f"Too many unclassified deductions ({n}) for permutation inference"
        )
        return None

    # Classifications: 0=post_tax, 1=pre_fit_only, 2=fully_pretax
    CLASSIFICATIONS = ("post_tax", "pre_fit_only", "fully_pretax")

    matches = []
    amounts = [u[2] for u in unclassified]

    # Try all 3^N combinations
    for combo in range(3 ** n):
        extra_pre_fit = 0.0
        extra_pre_fica = 0.0

        temp = combo
        for i in range(n):
            cls = temp % 3
            temp //= 3
            if cls >= 1:  # pre_fit_only or fully_pretax
                extra_pre_fit += amounts[i]
            if cls == 2:  # fully_pretax
                extra_pre_fica += amounts[i]

        # Compute what taxable wages would be with this classification
        computed_fit_taxable = gross - known_pre_fit - extra_pre_fit
        computed_fica_wages = gross - known_pre_fica - extra_pre_fica

        # Check if it matches actuals within tolerance
        fit_diff = abs(computed_fit_taxable - actual_fit_taxable)
        fica_diff = abs(computed_fica_wages - actual_fica_wages)

        if fit_diff <= tolerance and fica_diff <= tolerance:
            # Decode this combo
            result = []
            temp = combo
            for i in range(n):
                cls = temp % 3
                temp //= 3
                result.append((unclassified[i][0], unclassified[i][1], CLASSIFICATIONS[cls]))
            matches.append(result)

    if len(matches) == 1:
        return matches[0]
    elif len(matches) > 1:
        import logging
        logging.getLogger(__name__).debug(
            f"Multiple classification combos match ({len(matches)}), cannot infer"
        )
    return None


def extract_ytd_401k(stub: PayStub) -> Tuple[float, float]:
    """Extract current and YTD 401k amounts from a pay stub.

    Returns:
        Tuple of (current_401k, ytd_401k)
    """
    for ded in stub.deductions:
        if ded.type == "401k":
            return (ded.current, ded.ytd)
    return (0, 0)


def compare_pay_summaries(
    modeled: PaySummary,
    actual: PaySummary,
    prefix: str = "",
) -> List[Discrepancy]:
    """Compare modeled vs actual PaySummary values.

    Returns:
        List of Discrepancy objects for mismatches
    """
    diffs: List[Discrepancy] = []

    def add_diff(name: str, mod_val: float, act_val: float):
        diff = round(mod_val - act_val, 2)
        if abs(diff) >= 0.01:
            field_name = f"{prefix}{name}" if prefix else name
            diffs.append(Discrepancy(
                field=field_name,
                modeled=mod_val,
                actual=act_val,
                diff=diff,
            ))

    # Compare top-level fields
    add_diff("gross", modeled.gross, actual.gross)
    add_diff("net_pay", modeled.net_pay, actual.net_pay)

    # Compare deductions
    add_diff("deductions.fully_pretax", modeled.deductions.fully_pretax, actual.deductions.fully_pretax)
    add_diff("deductions.retirement", modeled.deductions.retirement, actual.deductions.retirement)
    add_diff("deductions.post_tax", modeled.deductions.post_tax, actual.deductions.post_tax)

    # Compare taxable wages
    add_diff("taxable.fit", modeled.taxable.fit, actual.taxable.fit)
    add_diff("taxable.ss", modeled.taxable.ss, actual.taxable.ss)
    add_diff("taxable.medicare", modeled.taxable.medicare, actual.taxable.medicare)

    # Compare withholding
    add_diff("withheld.fit", modeled.withheld.fit, actual.withheld.fit)
    add_diff("withheld.ss", modeled.withheld.ss, actual.withheld.ss)
    add_diff("withheld.medicare", modeled.withheld.medicare, actual.withheld.medicare)

    return diffs


def validate_stub(
    record_id: str,
    fica_rounding_balance: FicaRoundingBalance,
) -> Union[ValidateStubResult, Dict[str, Any]]:
    """Validate a pay stub model against an actual stub record (non-iterative).

    Self-contained validation that uses only data from the stub itself.
    Computes prior_ytd by subtracting current values from YTD values.

    Args:
        record_id: 8-character record ID
        fica_rounding_balance: FICA rounding remainder from prior periods.
            Use FicaRoundingBalance.none() if unknown.

    Returns:
        ValidateStubResult on success, or dict with "error" and "match": False on failure.
    """
    # Load the stub
    try:
        stub = get_pay_stub(record_id)
    except ValueError as e:
        return {"error": str(e), "match": False}

    # Compute prior YTD by subtracting current from YTD
    prior_ytd = PaySummary(
        gross=stub.ytd.gross - stub.current.gross,
        deductions=DeductionTotals(
            fully_pretax=stub.ytd.deductions.fully_pretax - stub.current.deductions.fully_pretax,
            retirement=stub.ytd.deductions.retirement - stub.current.deductions.retirement,
            post_tax=stub.ytd.deductions.post_tax - stub.current.deductions.post_tax,
        ),
        taxable=TaxAmounts(
            fit=stub.ytd.taxable.fit - stub.current.taxable.fit,
            ss=stub.ytd.taxable.ss - stub.current.taxable.ss,
            medicare=stub.ytd.taxable.medicare - stub.current.taxable.medicare,
        ),
        withheld=TaxAmounts(
            fit=stub.ytd.withheld.fit - stub.current.withheld.fit,
            ss=stub.ytd.withheld.ss - stub.current.withheld.ss,
            medicare=stub.ytd.withheld.medicare - stub.current.withheld.medicare,
        ),
        net_pay=stub.ytd.net_pay - stub.current.net_pay,
    )

    # Run model with stub's input facts (gross, deductions)
    # Model calculates outputs (taxable, withheld, net_pay)
    result = model_stub(
        stub.pay_date,
        stub.party,
        prior_ytd=prior_ytd,
        current_deductions=stub.current.deductions,
        comp_plan_override={
            "gross_per_period": stub.current.gross,
            # TODO: Infer pay_frequency from stub.period_end - stub.period_start
            # (13-14 days = biweekly, ~30 = monthly, 6-7 = weekly)
            # PayStub schema requires these dates, so always available.
            "pay_frequency": "biweekly",
        },
        fica_balance=fica_rounding_balance,
    )

    # model_stub returns dict with "error" on failure, ModelResult on success
    if isinstance(result, dict) and "error" in result:
        return {"error": f"Model error: {result['error']}", "match": False}

    # Compare model's calculated outputs against actual stub values
    current_diffs = compare_pay_summaries(result.current, stub.current)
    ytd_diffs = compare_pay_summaries(result.ytd, stub.ytd)

    return ValidateStubResult(
        record_id=record_id,
        party=stub.party,
        pay_date=stub.pay_date,
        model="model_stub",
        inputs={"gross": stub.current.gross, "deductions": stub.current.deductions.model_dump()},
        current=PeriodComparison(
            modeled=result.current,
            actual=stub.current,
            discrepancies=current_diffs,
        ),
        ytd=PeriodComparison(
            modeled=result.ytd,
            actual=stub.ytd,
            discrepancies=ytd_diffs,
        ),
    )


def validate_stub_in_sequence(
    record_id: str,
    auto_history: bool = True,
    supplementals: Optional[List[Dict[str, Any]]] = None,
    special_deductions: Optional[List[Dict[str, Any]]] = None,
) -> Union[ValidateStubResult, Dict[str, Any]]:
    """Validate a pay stub by modeling all stubs in sequence (iterative).

    Models all pay periods from year start through the target date, computing
    accurate YTD values and FICA rounding balances from prior stub outputs.

    Args:
        record_id: 8-character record ID
        auto_history: Auto-build history from prior stubs in database (default True)
        supplementals: Optional list of supplemental pay events
        special_deductions: Optional list of per-date 401k overrides

    Returns:
        ValidateStubResult on success, or dict with "error" and "match": False on failure.
    """
    # Load the stub
    try:
        stub = get_pay_stub(record_id)
    except ValueError as e:
        return {"error": str(e), "match": False}

    # Extract inputs from stub
    inputs = extract_inputs_from_stub(stub)

    # Build overrides for model
    comp_plan_override = {
        "gross_per_period": inputs["gross"],
        "pay_frequency": inputs["pay_frequency"],
    }
    benefits_override = inputs["benefits"] if inputs["benefits"] else None

    # Auto-build history from prior stubs if requested
    comp_plan_history = None
    if auto_history and supplementals is None and special_deductions is None:
        year = stub.pay_date[:4]
        auto_supplementals, auto_special_deductions, comp_plan_history = build_history_from_prior_stubs(
            stub.party, year, stub.pay_date
        )
        if supplementals is None:
            supplementals = auto_supplementals
        if special_deductions is None:
            special_deductions = auto_special_deductions

    # Run sequence model
    year_int = int(stub.pay_date[:4])
    result = model_stubs_in_sequence(
        year_int,
        stub.party,
        comp_plan_override=comp_plan_override,
        comp_plan_history=comp_plan_history,
        benefits_override=benefits_override,
        supplementals=supplementals,
    )

    # model_stubs_in_sequence returns StubSequenceResult on success, or dict with "error" on failure
    if isinstance(result, dict) and "error" in result:
        return {"error": f"Model error: {result['error']}", "match": False}

    # Find the modeled stub matching the target pay_date
    target_stub = None
    for modeled_stub in result.stubs:
        if modeled_stub.pay_date == stub.pay_date:
            target_stub = modeled_stub
            break

    if target_stub is None:
        # Target date not in modeled sequence - use last stub as fallback
        if result.stubs:
            target_stub = result.stubs[-1]
        else:
            return {"error": "No stubs modeled in sequence", "match": False}

    # StubResult has .current (PaySummary), StubSequenceResult has .ytd (PaySummary)
    modeled_current = target_stub.current
    modeled_ytd = result.ytd

    # Compare PaySummary objects directly
    current_diffs = compare_pay_summaries(modeled_current, stub.current)
    ytd_diffs = compare_pay_summaries(modeled_ytd, stub.ytd)

    return ValidateStubResult(
        record_id=record_id,
        party=stub.party,
        pay_date=stub.pay_date,
        model="model_stubs_in_sequence",
        inputs=inputs,
        current=PeriodComparison(
            modeled=modeled_current,
            actual=stub.current,
            discrepancies=current_diffs,
        ),
        ytd=PeriodComparison(
            modeled=modeled_ytd,
            actual=stub.ytd,
            discrepancies=ytd_diffs,
        ),
        periods_modeled=result.periods_modeled,
    )
