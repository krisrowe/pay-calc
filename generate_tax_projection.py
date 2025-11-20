#!/usr/bin/env python3
"""
Generate tax projection CSV file from YTD data

Usage:
    python3 generate_tax_projection.py [year]

Generates a CSV file similar to 2025_tax_projection.csv with combined
tax projections for both parties (him and her).
"""

import sys
import json
import csv
from pathlib import Path
from datetime import datetime


# 2025 Tax brackets for MFJ (Married Filing Jointly)
TAX_BRACKETS_2025 = [
    (0, 0.10),
    (23200, 0.12),
    (94300, 0.22),
    (201050, 0.24),
    (383900, 0.32),
    (487450, 0.35),
    (731200, 0.37)
]

STANDARD_DEDUCTION_2025 = 31500.0
SS_WAGE_BASE_2025 = 168600.0
SS_TAX_RATE = 0.062


def calculate_tax_brackets(taxable_income):
    """Calculate tax using 2025 MFJ brackets."""
    tax = 0.0
    prev_bracket = 0
    
    for bracket, rate in TAX_BRACKETS_2025:
        if taxable_income > bracket:
            taxable_in_bracket = min(taxable_income, bracket) - prev_bracket
            tax += taxable_in_bracket * rate
            prev_bracket = bracket
        else:
            break
    
    if taxable_income > prev_bracket:
        taxable_in_bracket = taxable_income - prev_bracket
        tax += taxable_in_bracket * TAX_BRACKETS_2025[-1][1]
    
    return tax


def load_ytd_data(year, party):
    """Load YTD data for a specific year and party."""
    data_dir = Path("data")
    ytd_file = data_dir / f"{year}_{party}_ytd.json"
    
    if not ytd_file.exists():
        return None
    
    with open(ytd_file, 'r') as f:
        return json.load(f)


def aggregate_by_party(ytd_data):
    """Aggregate totals from all employers for a party."""
    totals = ytd_data.get("totals", {})
    
    # Get earnings breakdown
    earnings = totals.get("earnings", {})
    taxes = totals.get("taxes", {})
    deductions = totals.get("deductions", {})
    
    return {
        "gross_pay": earnings.get("total_gross", 0.0),
        "fit_taxable_wages": totals.get("fit_taxable_wages", 0.0),
        "federal_tax_withheld": taxes.get("federal_income_tax_withheld", 0.0),
        "ss_tax_withheld": taxes.get("social_security_withheld", 0.0),
        "medicare_tax_withheld": taxes.get("medicare_withheld", 0.0),
        "total_deductions": deductions.get("total_deductions", 0.0),
        "ss_wages": min(totals.get("fit_taxable_wages", 0.0), SS_WAGE_BASE_2025),
        "medicare_wages": totals.get("fit_taxable_wages", 0.0)
    }


def calculate_ss_overpayment(him_data):
    """Calculate Social Security overpayment if applicable."""
    ss_tax_paid = him_data.get("ss_tax_withheld", 0.0)
    ss_tax_max = SS_WAGE_BASE_2025 * SS_TAX_RATE
    
    if ss_tax_paid > ss_tax_max:
        return ss_tax_paid - ss_tax_max
    return 0.0


def generate_tax_projection(year=None):
    """Generate tax projection CSV for the specified year."""
    if year is None:
        year = datetime.now().year
    
    year_str = str(year)
    
    print(f"Generating tax projection for {year_str}...")
    
    # Load YTD data for both parties
    him_ytd = load_ytd_data(year_str, "him")
    her_ytd = load_ytd_data(year_str, "her")
    
    if not him_ytd:
        print(f"Error: No YTD data found for {year_str} (him)")
        print(f"  Run: python3 calc_ytd.py {year_str} him")
        sys.exit(1)
    
    if not her_ytd:
        print(f"Warning: No YTD data found for {year_str} (her)")
        print(f"  Run: python3 calc_ytd.py {year_str} her")
        print(f"  Continuing with him data only...")
        her_ytd = {
            "totals": {
                "earnings": {"total_gross": 0.0},
                "taxes": {
                    "federal_income_tax_withheld": 0.0,
                    "social_security_withheld": 0.0,
                    "medicare_withheld": 0.0
                },
                "deductions": {"total_deductions": 0.0},
                "fit_taxable_wages": 0.0
            }
        }
    
    # Aggregate data by party
    him_data = aggregate_by_party(him_ytd)
    her_data = aggregate_by_party(her_ytd)
    
    # Calculate combined taxable income
    combined_taxable_wages = him_data["fit_taxable_wages"] + her_data["fit_taxable_wages"]
    taxable_income = combined_taxable_wages - STANDARD_DEDUCTION_2025
    
    # Calculate federal income tax
    federal_tax_assessed = calculate_tax_brackets(taxable_income)
    
    # Calculate SS overpayment
    ss_overpayment = calculate_ss_overpayment(him_data)
    
    # Calculate refund/owed
    total_withheld = him_data["federal_tax_withheld"] + her_data["federal_tax_withheld"]
    refund = total_withheld - federal_tax_assessed + ss_overpayment
    
    # Generate CSV
    output_file = Path(f"{year_str}_tax_projection.csv")
    
    with open(output_file, 'w', newline='') as f:
        writer = csv.writer(f)
        
        # Header rows
        writer.writerow(['', '', '', '', '', '', ''])
        writer.writerow(['', '', 'INCOME TAX BRACKETS (MFJ)', '', '', 'HIM', ''])
        writer.writerow(['', '', 'Applied to income of', f'${taxable_income:,.2f}', '', '', ''])
        writer.writerow(['', '', '', '', '', '', ''])
        writer.writerow(['', 'Earnings Above', 'Rate / Bracket', 'Tax Assessed', '', '', ''])
        
        # Tax brackets
        prev_bracket = 0
        for bracket, rate in TAX_BRACKETS_2025:
            if taxable_income > prev_bracket:
                bracket_tax = calculate_tax_brackets(min(taxable_income, bracket)) - calculate_tax_brackets(prev_bracket)
                writer.writerow(['', f'${bracket:,}', f'{int(rate*100)}%', f'${bracket_tax:,.2f}', '', '', ''])
                prev_bracket = bracket
        
        if taxable_income > prev_bracket:
            bracket_tax = calculate_tax_brackets(taxable_income) - calculate_tax_brackets(prev_bracket)
            writer.writerow(['', f'${prev_bracket:,}', f'{int(TAX_BRACKETS_2025[-1][1]*100)}%', f'${bracket_tax:,.2f}', '', '', ''])
        
        writer.writerow(['', '', 'Total Assessed', f'${federal_tax_assessed:,.2f}', '', '', ''])
        writer.writerow(['', '', '', '', '', '', ''])
        writer.writerow(['', '', '', '', '', f'${him_data["gross_pay"]:,.2f}', 'Gross pay'])
        writer.writerow(['', '', '', '', '', '', ''])
        writer.writerow(['', '', '', '', '', f'${him_data["fit_taxable_wages"]:,.2f}', 'Wages'])
        writer.writerow(['', '', '', '', '', f'${him_data["medicare_wages"]:,.2f}', 'Medicare wages'])
        writer.writerow(['', '', '', '', '', f'${him_data["ss_wages"]:,.2f}', 'SS wages'])
        writer.writerow(['', '', '', '', '', '', ''])
        writer.writerow(['', '', '', '', '', 'HER', ''])
        writer.writerow(['', '', '', '', '', f'${her_data["gross_pay"]:,.2f}', 'Gross pay YTD'])
        writer.writerow(['', '', '', '', '', f'${her_data["fit_taxable_wages"]:,.2f}', 'Wages (should match W-2 to the penny)'])
        writer.writerow(['', '', '', '', '', f'${her_data["ss_wages"]:,.2f}', 'SS wages'])
        writer.writerow(['', '', '', '', '', f'${her_data["ss_tax_withheld"]:,.2f}', 'SS taxes'])
        writer.writerow(['', '', '', '', '', f'${her_data["medicare_wages"]:,.2f}', 'Medicare wages'])
        writer.writerow(['', '', '', '', '', f'${her_data["medicare_tax_withheld"]:,.2f}', 'Medicare tax withheld'])
        writer.writerow(['', '', '', '', '', '', ''])
        writer.writerow(['', '', '', '', '', 'TAXABLE INCOME', ''])
        writer.writerow(['', '', '', '', '', f'${him_data["fit_taxable_wages"]:,.2f}', 'His wages per W-2'])
        writer.writerow(['', '', '', '', '', f'${her_data["fit_taxable_wages"]:,.2f}', 'Her wages per W-2'])
        writer.writerow(['', '', '', '', '', f'${combined_taxable_wages:,.2f}', 'Combined gross income'])
        writer.writerow(['', '', '', '', '', f'-${STANDARD_DEDUCTION_2025:,.2f}', 'Standard deduction'])
        writer.writerow(['', '', '', '', '', f'${taxable_income:,.2f}', 'Taxable income'])
        writer.writerow(['', '', '', '', '', '', ''])
        writer.writerow(['', '', '', '', '', 'TAX RETURN / REFUND PROJECTION', ''])
        writer.writerow(['', '', '', '', '', f'-${federal_tax_assessed:,.2f}', 'Federal Income Tax (Taxable income applied to tax table)'])
        writer.writerow(['', '', '', '', '', f'${him_data["federal_tax_withheld"]:,.2f}', 'His income tax withheld'])
        writer.writerow(['', '', '', '', '', f'${her_data["federal_tax_withheld"]:,.2f}', 'Her income tax withheld'])
        if ss_overpayment > 0:
            writer.writerow(['', '', '', '', '', f'${ss_overpayment:,.2f}', 'Overpayment of SS from job switch'])
        writer.writerow(['', '', '', '', '', f'${refund:,.2f}', 'Refund (or owed, if negative)'])
    
    print(f"\n{'='*60}")
    print(f"Tax projection saved to {output_file}")
    print(f"{'='*60}")
    print(f"\nSummary:")
    print(f"  Combined Taxable Wages: ${combined_taxable_wages:,.2f}")
    print(f"  Taxable Income: ${taxable_income:,.2f}")
    print(f"  Federal Tax Assessed: ${federal_tax_assessed:,.2f}")
    print(f"  Total Withheld: ${total_withheld:,.2f}")
    if ss_overpayment > 0:
        print(f"  SS Overpayment: ${ss_overpayment:,.2f}")
    print(f"  Refund/Owed: ${refund:,.2f}")


def main():
    year = None
    if len(sys.argv) > 1:
        try:
            year = int(sys.argv[1])
        except ValueError:
            print(f"Error: Invalid year: {sys.argv[1]}")
            sys.exit(1)
    
    generate_tax_projection(year)


if __name__ == "__main__":
    main()

