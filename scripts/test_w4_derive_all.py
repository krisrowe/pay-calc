#!/usr/bin/env python3
"""Test W-4 derivation on all regular pay stubs.

Analyzes how many stubs match standard W-4 configurations vs requiring
custom credit amounts or extra withholding.
"""

import json
from collections import defaultdict

from paycalc.sdk.records import list_records
from paycalc.sdk.employee.config import derive_w4_from_stub


def is_regular_pay_stub(data: dict) -> bool:
    """Check if stub has Regular Pay earnings > 0."""
    earnings = data.get("earnings", [])
    for earn in earnings:
        raw_type = (earn.get("type") or earn.get("name") or "").lower()
        amount = earn.get("current_amount") or earn.get("amount") or 0
        if "regular pay" in raw_type and amount > 0:
            return True
    return False


def analyze_stubs_for_party(party: str, year: str = "2025") -> dict:
    """Analyze all regular pay stubs for a party."""

    # Get all stubs for party
    records = list_records(year=year, party=party, type_filter="stub")

    results = {
        "party": party,
        "year": year,
        "total_stubs": 0,
        "regular_pay_stubs": 0,
        "analyzed": 0,
        "skipped_low_fit": 0,
        "skipped_not_regular": 0,
        "categories": defaultdict(int),
        "details": [],
    }

    for record in records:
        results["total_stubs"] += 1
        record_id = record["id"]

        # Only analyze regular pay stubs (not RSU, bonus, adjustments)
        if not is_regular_pay_stub(record.get("data", {})):
            results["skipped_not_regular"] += 1
            continue

        results["regular_pay_stubs"] += 1

        try:
            result = derive_w4_from_stub(record_id, max_dependents=8)
        except ValueError as e:
            # Skip stubs with no FIT taxable (e.g., adjustment stubs)
            results["skipped_low_fit"] += 1
            continue

        # Skip very low FIT taxable (likely 401k maxing, not useful for deriving W-4)
        if result["fit_taxable"] < 1000:
            results["skipped_low_fit"] += 1
            continue

        results["analyzed"] += 1

        derived = result["derived"]
        category = derived["match_category"]
        results["categories"][category] += 1

        results["details"].append({
            "record_id": record_id,
            "pay_date": result["pay_date"],
            "fit_taxable": result["fit_taxable"],
            "fit_withheld": result["fit_withheld"],
            "effective_rate_pct": result["effective_rate_pct"],
            "match_category": category,
            "description": derived["description"],
            "step3_credits": derived["step3_credits"],
            "step4c_extra": derived["step4c_extra_withholding"],
            "matches_found": result["analysis"]["matches_found"],
        })

    return results


def print_summary(results: dict):
    """Print summary for a party."""
    party = results["party"]
    print(f"\n{'='*60}")
    print(f"W-4 Derivation Summary for {party.upper()}")
    print(f"{'='*60}")
    print(f"Total stubs: {results['total_stubs']}")
    print(f"  Non-regular (RSU, bonus, etc): {results['skipped_not_regular']}")
    print(f"  Regular pay stubs: {results['regular_pay_stubs']}")
    print(f"    Analyzed (FIT taxable >= $1000): {results['analyzed']}")
    print(f"    Skipped (low/no FIT): {results['skipped_low_fit']}")
    print()
    print("Match Categories:")
    for cat, count in sorted(results["categories"].items()):
        pct = (count / results["analyzed"] * 100) if results["analyzed"] > 0 else 0
        desc = {
            "standard": "Standard W-4 (whole # dependents)",
            "custom_credits": "Custom credits (non-$2000 multiple)",
            "extra_withholding": "Required extra withholding",
            "no_match": "No matching configuration",
        }.get(cat, cat)
        print(f"  {desc}: {count} ({pct:.0f}%)")

    # Show sample of each category
    print()
    print("Sample details:")
    by_cat = defaultdict(list)
    for d in results["details"]:
        by_cat[d["match_category"]].append(d)

    for cat in ["standard", "custom_credits", "extra_withholding", "no_match"]:
        samples = by_cat.get(cat, [])[:3]
        if samples:
            print(f"\n  [{cat}]")
            for s in samples:
                print(f"    {s['record_id']} ({s['pay_date']}): "
                      f"${s['fit_withheld']:.2f} withheld, "
                      f"{s['description']}, credits=${s['step3_credits']}"
                      + (f", extra=${s['step4c_extra']}/period" if s['step4c_extra'] else ""))


def main():
    print("Analyzing W-4 derivation matches for all regular pay stubs...")

    all_results = {}

    for party in ["him", "her"]:
        results = analyze_stubs_for_party(party, "2025")
        all_results[party] = results
        print_summary(results)

    # Overall summary
    print(f"\n{'='*60}")
    print("OVERALL SUMMARY")
    print(f"{'='*60}")

    total_analyzed = sum(r["analyzed"] for r in all_results.values())
    total_standard = sum(r["categories"]["standard"] for r in all_results.values())
    total_custom = sum(r["categories"]["custom_credits"] for r in all_results.values())
    total_extra = sum(r["categories"]["extra_withholding"] for r in all_results.values())
    total_no_match = sum(r["categories"]["no_match"] for r in all_results.values())

    print(f"Total stubs analyzed: {total_analyzed}")
    print(f"  Standard W-4 match: {total_standard} ({total_standard/total_analyzed*100:.0f}%)" if total_analyzed else "")
    print(f"  Custom credits: {total_custom} ({total_custom/total_analyzed*100:.0f}%)" if total_analyzed else "")
    print(f"  Extra withholding: {total_extra} ({total_extra/total_analyzed*100:.0f}%)" if total_analyzed else "")
    print(f"  No match: {total_no_match} ({total_no_match/total_analyzed*100:.0f}%)" if total_analyzed else "")


if __name__ == "__main__":
    main()
