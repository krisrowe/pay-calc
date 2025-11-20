#!/usr/bin/env python3
"""
Calculate YTD totals by employer from pay_stub_data.json

Usage:
    python3 calc_ytd.py [year]

If year is not specified, defaults to current year.

Generates YYYY_ytd.json with:
- YTD totals by employer (using latest stub with largest YTD numbers)
- Grand totals across all employers
- W-2 equivalent numbers
"""

import sys
import json
from pathlib import Path
from datetime import datetime
from collections import defaultdict


def load_pay_stubs(year=None, party=None):
    """Load pay stubs from YYYY_party_pay_stubs.json files in data/ directory."""
    data_dir = Path("data")
    
    if not data_dir.exists():
        print(f"Error: {data_dir} directory not found. Run extract_pay_stub.py first.")
        sys.exit(1)
    
    year_str = str(year) if year else None
    
    if year_str and party:
        # Load specific year and party file
        data_file = data_dir / f"{year_str}_{party}_pay_stubs.json"
        
        if not data_file.exists():
            print(f"Error: No pay stub file found for year {year_str} and party {party} in {data_dir}")
            print(f"  Searched for: {data_file.name}")
            print(f"  Available files: {list(data_dir.glob('*_pay_stubs.json'))}")
            sys.exit(1)
        
        with open(data_file, 'r') as f:
            data = json.load(f)
        
        stubs = data.get("pay_stubs", [])
        print(f"Loaded {len(stubs)} pay stub(s) for year {year_str}, party {party}")
        return stubs
    elif year_str:
        # Load all party files for the year
        all_stubs = []
        for party_name in ["him", "her"]:
            data_file = data_dir / f"{year_str}_{party_name}_pay_stubs.json"
            if data_file.exists():
                with open(data_file, 'r') as f:
                    data = json.load(f)
                    stubs = data.get("pay_stubs", [])
                    all_stubs.extend(stubs)
                    print(f"Loaded {len(stubs)} pay stub(s) from {data_file.name}")
        
        if not all_stubs:
            print(f"Error: No pay stub files found for year {year_str} in {data_dir}")
            print(f"  Available files: {list(data_dir.glob('*_pay_stubs.json'))}")
            sys.exit(1)
        
        print(f"Total: {len(all_stubs)} pay stub(s) loaded for year {year_str}")
        return all_stubs
    else:
        # Load all year and party files
        all_stubs = []
        year_files = sorted(data_dir.glob("*_*_pay_stubs.json"))
        
        # Filter out backup files
        year_files = [f for f in year_files if "backup" not in f.name]
        
        if not year_files:
            print(f"Error: No pay stub files found in {data_dir}")
            sys.exit(1)
        
        for year_file in year_files:
            with open(year_file, 'r') as f:
                data = json.load(f)
                stubs = data.get("pay_stubs", [])
                all_stubs.extend(stubs)
                print(f"Loaded {len(stubs)} pay stub(s) from {year_file.name}")
        
        print(f"Total: {len(all_stubs)} pay stub(s) loaded")
        return all_stubs


def find_latest_stub_by_employer(stubs):
    """
    Find the latest stub for each employer and party combination.
    
    When multiple stubs exist for the same pay date, the one with the
    largest YTD gross is the source of truth (it's inclusive of all payments).
    """
    by_employer_party = defaultdict(list)
    
    # Group by employer and party
    for stub in stubs:
        employer = stub.get("employer", "Unknown")
        party = stub.get("party", "him")
        key = f"{employer}::{party}"
        by_employer_party[key].append(stub)
    
    latest_stubs = {}
    
    for key, employer_stubs in by_employer_party.items():
        # Sort by pay_date, then by YTD gross (descending)
        # This ensures we pick the stub with largest YTD when dates match
        sorted_stubs = sorted(
            employer_stubs,
            key=lambda s: (
                s.get("pay_date", ""),
                -s.get("pay_summary", {}).get("ytd", {}).get("gross", 0.0)
            )
        )
        
        # The latest stub is the one with the latest date
        # If multiple stubs have the same date, pick the one with largest YTD gross
        latest_date = sorted_stubs[-1].get("pay_date", "")
        
        # Among stubs with the latest date, pick the one with largest YTD gross
        latest_date_stubs = [s for s in sorted_stubs if s.get("pay_date", "") == latest_date]
        latest_stub = max(latest_date_stubs, key=lambda s: s.get("pay_summary", {}).get("ytd", {}).get("gross", 0.0))
        
        latest_stubs[key] = latest_stub
    
    return latest_stubs


def aggregate_earnings(stub):
    """Aggregate earnings from a pay stub."""
    earnings_data = {
        "regular_pay": 0.0,
        "bonuses": 0.0,
        "stock_units": 0.0,
        "other": 0.0,
        "total_gross": 0.0
    }
    
    # Get total gross from pay_summary
    earnings_data["total_gross"] = stub.get("pay_summary", {}).get("ytd", {}).get("gross", 0.0)
    
    # Categorize individual earnings
    for earning in stub.get("earnings", []):
        etype = earning.get("type", "").lower()
        ytd = earning.get("ytd_amount", 0.0)
        
        if "regular pay" in etype:
            earnings_data["regular_pay"] += ytd
        elif "bonus" in etype or "recognition bonus" in etype or "sales bonus" in etype or "annual bonus" in etype:
            earnings_data["bonuses"] += ytd
        elif "stock" in etype or "gusu" in etype:
            earnings_data["stock_units"] += ytd
        else:
            earnings_data["other"] += ytd
    
    return earnings_data


def aggregate_taxes(stub):
    """Aggregate tax information from a pay stub."""
    taxes = stub.get("taxes", {})
    
    return {
        "federal_income_tax_withheld": taxes.get("federal_income_tax", {}).get("ytd_withheld", 0.0),
        "social_security_withheld": taxes.get("social_security", {}).get("ytd_withheld", 0.0),
        "medicare_withheld": taxes.get("medicare", {}).get("ytd_withheld", 0.0),
        "total_taxes": (
            taxes.get("federal_income_tax", {}).get("ytd_withheld", 0.0) +
            taxes.get("social_security", {}).get("ytd_withheld", 0.0) +
            taxes.get("medicare", {}).get("ytd_withheld", 0.0)
        )
    }


def aggregate_deductions(stub):
    """Aggregate deductions from a pay stub."""
    deductions_data = {
        "401k_pretax": 0.0,
        "401k_after_tax": 0.0,
        "health_insurance": 0.0,
        "other": 0.0,
        "total_deductions": 0.0
    }
    
    # Get total deductions from pay_summary
    deductions_data["total_deductions"] = stub.get("pay_summary", {}).get("ytd", {}).get("deductions", 0.0)
    
    # Categorize individual deductions
    for deduction in stub.get("deductions", []):
        dtype = deduction.get("type", "").lower()
        ytd = deduction.get("ytd_amount", 0.0)
        
        if "401k pretax" in dtype or "pretax" in dtype:
            deductions_data["401k_pretax"] += ytd
        elif "401k" in dtype and "after tax" in dtype or "bonus 401k" in dtype:
            deductions_data["401k_after_tax"] += ytd
        elif any(term in dtype for term in ["medical", "dental", "vision", "health", "fsa"]):
            deductions_data["health_insurance"] += ytd
        else:
            deductions_data["other"] += ytd
    
    return deductions_data


def calculate_ytd(year=None, party=None):
    """Calculate YTD totals for the specified year and party."""
    if year is None:
        year = datetime.now().year
    
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    
    # Determine which parties to process
    parties_to_process = []
    if party:
        parties_to_process = [party]
    else:
        # Process both him and her
        parties_to_process = ["him", "her"]
    
    all_ytd_data = {}
    
    for party_name in parties_to_process:
        print(f"\n{'='*60}")
        print(f"Calculating YTD totals for {year}, party: {party_name.upper()}")
        print(f"{'='*60}")
        
        # Load pay stubs for the year and party
        stubs = load_pay_stubs(year, party_name)
        
        if not stubs:
            print(f"No pay stubs found for year {year}, party {party_name}")
            continue
        
        # Find latest stub for each employer
        latest_stubs = find_latest_stub_by_employer(stubs)
        
        if not latest_stubs:
            print(f"No employers found for year {year}, party {party_name}")
            continue
        
        print(f"Found {len(latest_stubs)} employer(s)")
        
        # Build YTD data structure
        ytd_data = {
            "year": year,
            "party": party_name,
            "employers": {},
            "totals": {
                "earnings": {"total_gross": 0.0},
                "taxes": {
                    "federal_income_tax_withheld": 0.0,
                    "social_security_withheld": 0.0,
                    "medicare_withheld": 0.0,
                    "total_taxes": 0.0
                },
                "deductions": {"total_deductions": 0.0},
                "fit_taxable_wages": 0.0
            }
        }
        
        # Process each employer
        for key, stub in latest_stubs.items():
            employer = stub.get("employer", "Unknown")
            stub_party = stub.get("party", party_name)
            display_name = f"{employer}"
            
            print(f"\nProcessing {display_name}:")
            print(f"  Source stub: {stub.get('file_name')}")
            print(f"  Pay date: {stub.get('pay_date')}")
            
            earnings = aggregate_earnings(stub)
            taxes = aggregate_taxes(stub)
            deductions = aggregate_deductions(stub)
            fit_taxable = stub.get("pay_summary", {}).get("ytd", {}).get("fit_taxable_wages", 0.0)
            
            ytd_data["employers"][key] = {
                "employer": employer,
                "party": stub_party,
                "earnings": earnings,
                "taxes": taxes,
                "deductions": deductions,
                "fit_taxable_wages": fit_taxable,
                "source_stub": stub.get("file_name"),
                "source_pay_date": stub.get("pay_date")
            }
            
            # Add to totals
            ytd_data["totals"]["earnings"]["total_gross"] += earnings["total_gross"]
            ytd_data["totals"]["taxes"]["federal_income_tax_withheld"] += taxes["federal_income_tax_withheld"]
            ytd_data["totals"]["taxes"]["social_security_withheld"] += taxes["social_security_withheld"]
            ytd_data["totals"]["taxes"]["medicare_withheld"] += taxes["medicare_withheld"]
            ytd_data["totals"]["taxes"]["total_taxes"] += taxes["total_taxes"]
            ytd_data["totals"]["deductions"]["total_deductions"] += deductions["total_deductions"]
            ytd_data["totals"]["fit_taxable_wages"] += fit_taxable
            
            print(f"  YTD Gross: ${earnings['total_gross']:,.2f}")
            print(f"  FIT Taxable: ${fit_taxable:,.2f}")
            print(f"  Federal Tax Withheld: ${taxes['federal_income_tax_withheld']:,.2f}")
        
        # Save to file
        output_file = data_dir / f"{year}_{party_name}_ytd.json"
        with open(output_file, 'w') as f:
            json.dump(ytd_data, f, indent=2)
        
        print(f"\n{'='*60}")
        print(f"YTD totals saved to {output_file}")
        print(f"{'='*60}")
        print(f"\nTotals for {party_name.upper()}:")
        print(f"  Total Gross: ${ytd_data['totals']['earnings']['total_gross']:,.2f}")
        print(f"  FIT Taxable Wages: ${ytd_data['totals']['fit_taxable_wages']:,.2f}")
        print(f"  Federal Tax Withheld: ${ytd_data['totals']['taxes']['federal_income_tax_withheld']:,.2f}")
        print(f"  SS Tax Withheld: ${ytd_data['totals']['taxes']['social_security_withheld']:,.2f}")
        print(f"  Medicare Tax Withheld: ${ytd_data['totals']['taxes']['medicare_withheld']:,.2f}")
        print(f"  Total Taxes: ${ytd_data['totals']['taxes']['total_taxes']:,.2f}")
        print(f"  Total Deductions: ${ytd_data['totals']['deductions']['total_deductions']:,.2f}")
        
        all_ytd_data[party_name] = ytd_data
    
    # If processing both parties, show combined totals
    if len(parties_to_process) > 1 and len(all_ytd_data) > 1:
        print(f"\n{'='*60}")
        print(f"COMBINED TOTALS FOR {year}")
        print(f"{'='*60}")
        
        combined = {
            "total_gross": sum(d["totals"]["earnings"]["total_gross"] for d in all_ytd_data.values()),
            "fit_taxable_wages": sum(d["totals"]["fit_taxable_wages"] for d in all_ytd_data.values()),
            "federal_tax_withheld": sum(d["totals"]["taxes"]["federal_income_tax_withheld"] for d in all_ytd_data.values()),
            "ss_tax_withheld": sum(d["totals"]["taxes"]["social_security_withheld"] for d in all_ytd_data.values()),
            "medicare_tax_withheld": sum(d["totals"]["taxes"]["medicare_withheld"] for d in all_ytd_data.values()),
            "total_taxes": sum(d["totals"]["taxes"]["total_taxes"] for d in all_ytd_data.values()),
            "total_deductions": sum(d["totals"]["deductions"]["total_deductions"] for d in all_ytd_data.values())
        }
        
        print(f"  Total Gross: ${combined['total_gross']:,.2f}")
        print(f"  FIT Taxable Wages: ${combined['fit_taxable_wages']:,.2f}")
        print(f"  Federal Tax Withheld: ${combined['federal_tax_withheld']:,.2f}")
        print(f"  SS Tax Withheld: ${combined['ss_tax_withheld']:,.2f}")
        print(f"  Medicare Tax Withheld: ${combined['medicare_tax_withheld']:,.2f}")
        print(f"  Total Taxes: ${combined['total_taxes']:,.2f}")
        print(f"  Total Deductions: ${combined['total_deductions']:,.2f}")


def main():
    year = None
    party = None
    
    if len(sys.argv) > 1:
        try:
            year = int(sys.argv[1])
        except ValueError:
            print(f"Error: Invalid year: {sys.argv[1]}")
            sys.exit(1)
    
    if len(sys.argv) > 2:
        party = sys.argv[2].lower()
        if party not in ["him", "her"]:
            print(f"Error: Party must be 'him' or 'her', got: {party}")
            sys.exit(1)
    
    calculate_ytd(year, party)


if __name__ == "__main__":
    main()

