"""Identify base salary changes from pay stub history.

SDK layer - pure logic, returns salary history. No CLI or presentation.

Analyzes regular pay stubs to find comp plan transitions (raises, employer changes).
Filters out noise (<1% changes) and anomalies (>25% changes like partial periods).
"""

from typing import Any, Dict, List

from ..records import list_records


def get_regular_pay_amount(data: Dict[str, Any]) -> float:
    """Extract the Regular Pay earning amount from stub data."""
    earnings = data.get("earnings", [])
    if isinstance(earnings, dict):
        earnings = [{"type": k, **v} if isinstance(v, dict) else {"type": k, "current_amount": v}
                    for k, v in earnings.items()]

    for e in earnings:
        etype = (e.get("type") or e.get("name") or "").lower()
        eamt = e.get("current_amount") or e.get("amount") or e.get("current") or 0
        if "regular" in etype and eamt > 0:
            return eamt
    return 0


def identify_salary_changes(
    start_date: str,
    end_date: str,
    party: str = "him",
    min_change_pct: float = 0.01,
    max_change_pct: float = 0.25,
) -> List[Dict[str, Any]]:
    """Identify base salary changes within a date range.

    Args:
        start_date: Start date (YYYY-MM-DD), inclusive
        end_date: End date (YYYY-MM-DD), inclusive
        party: Party identifier
        min_change_pct: Minimum change to consider (default 1%)
        max_change_pct: Maximum change to consider (default 25%)

    Returns:
        List of salary entries including initial and changes:
        [
            {
                "effective_date": "2025-01-03",
                "gross_per_period": 7800.00,
                "regular_pay": 7800.00,
                "employer": "Acme Corp",
                "change_pct": None,  # First entry
                "source_record": "abc12345",
            },
            ...
        ]
    """
    start_year = start_date[:4]
    end_year = end_date[:4]

    # Query stubs for relevant years
    all_stubs = []
    for year in range(int(start_year), int(end_year) + 1):
        stubs = list_records(year=str(year), party=party, type_filter="stub")
        all_stubs.extend(stubs)

    # Extract regular pay stubs within date range
    regular_stubs = []
    for stub in all_stubs:
        data = stub.get("data", {})
        pay_date = data.get("pay_date", "")

        if not pay_date or pay_date < start_date or pay_date > end_date:
            continue

        regular_pay = get_regular_pay_amount(data)
        gross = data.get("pay_summary", {}).get("current", {}).get("gross", 0)

        if regular_pay > 0:
            regular_stubs.append({
                "date": pay_date,
                "gross": gross,
                "regular_pay": regular_pay,
                "employer": data.get("employer", ""),
                "record_id": stub.get("id", ""),
            })

    if not regular_stubs:
        return []

    # Sort by date
    regular_stubs.sort(key=lambda x: x["date"])

    # Build salary history: include first entry + significant changes
    salary_history = []
    prev_entry = None

    for stub in regular_stubs:
        if prev_entry is None:
            # Always include the first entry
            salary_history.append({
                "effective_date": stub["date"],
                "gross_per_period": round(stub["gross"], 2),
                "regular_pay": round(stub["regular_pay"], 2),
                "employer": stub["employer"] or None,
                "change_pct": None,
                "source_record": stub["record_id"],
            })
            prev_entry = stub
            continue

        # Check for employer change
        employer_changed = (
            stub["employer"] and prev_entry["employer"] and
            stub["employer"] != prev_entry["employer"]
        )

        # Calculate percentage change (signed: positive = increase, negative = decrease)
        change_pct = (stub["regular_pay"] - prev_entry["regular_pay"]) / prev_entry["regular_pay"]
        abs_change_pct = abs(change_pct)

        # Include if employer changed OR salary change is significant but not anomalous
        if employer_changed or (min_change_pct <= abs_change_pct <= max_change_pct):
            salary_history.append({
                "effective_date": stub["date"],
                "gross_per_period": round(stub["gross"], 2),
                "regular_pay": round(stub["regular_pay"], 2),
                "employer": stub["employer"] or None,
                "change_pct": round(change_pct * 100, 2),
                "source_record": stub["record_id"],
            })
            prev_entry = stub

    return salary_history
