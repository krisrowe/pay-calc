#!/usr/bin/env python3
"""
Generate a tax projection based on W-2 or YTD data and output to CSV.
"""

import sys
import json
import yaml
import csv
from pathlib import Path
import traceback
from collections import defaultdict

def load_tax_rules(year):
    """Load tax rules for a specific year from tax-rules/YYYY.yaml."""
    config_file = Path(f"tax-rules/{year}.yaml")
    if not config_file.exists():
        raise FileNotFoundError(f"Tax rules file not found for year {year}: {config_file}")
    
    with open(config_file, 'r') as f:
        return yaml.safe_load(f)

def calculate_federal_income_tax(taxable_income, tax_brackets):
    """Calculate federal income tax based on taxable income and tax brackets."""
    tax_owed = 0.0
    previous_bracket_max = 0.0
    
    sorted_brackets = sorted(tax_brackets, key=lambda b: b.get("up_to", float('inf')))
    
    for bracket in sorted_brackets:
        rate = bracket['rate']
        
        if 'up_to' in bracket:
            current_bracket_max = bracket['up_to']
            if taxable_income > previous_bracket_max:
                income_in_this_bracket = min(taxable_income, current_bracket_max) - previous_bracket_max
                tax_owed += income_in_this_bracket * rate
            previous_bracket_max = current_bracket_max
            
        elif 'over' in bracket:
            if taxable_income > bracket['over']:
                income_in_this_bracket = taxable_income - bracket['over']
                tax_owed += income_in_this_bracket * rate
                
    return tax_owed

def calculate_additional_medicare_tax_amount(total_medicare_wages, threshold):
    """Calculate the Additional Medicare Tax amount (0.9% of excess wages)."""
    excess_medicare_wages = max(0, total_medicare_wages - threshold)
    return excess_medicare_wages * 0.009

def load_party_data(year, party):
    """
    Load and aggregate W-2 data for a party from the corresponding _w2_forms.json file.
    Falls back to YTD pay stub data if W-2 data is not available.
    """
    data_dir = Path("data")
    w2_file = data_dir / f"{year}_{party}_w2_forms.json"
    ytd_file = data_dir / f"{year}_{party}_ytd.json"

    if w2_file.exists():
        print(f"Loading W-2 data for {party} from {w2_file.name}...")
        with open(w2_file, 'r') as f:
            w2_data = json.load(f)
        
        aggregated_data = defaultdict(float)
        for form in w2_data.get("forms", []):
            for key, value in form.get("data", {}).items():
                aggregated_data[key] += value
        
        return dict(aggregated_data)

    elif ytd_file.exists():
        print(f"Warning: W-2 data not found for {party}. Falling back to YTD pay stub data from {ytd_file.name}.")
        with open(ytd_file, 'r') as f:
            ytd_data = json.load(f)
            return {
                "wages_tips_other_comp": ytd_data["totals"]["fit_taxable_wages"],
                "federal_income_tax_withheld": ytd_data["totals"]["taxes"]["federal_income_tax_withheld"],
                "social_security_wages": ytd_data["totals"].get("social_security_wages", 0.0),
                "social_security_tax_withheld": ytd_data["totals"]["taxes"]["social_security_withheld"],
                "medicare_wages_and_tips": ytd_data["totals"].get("medicare_wages_and_tips", 0.0),
                "medicare_tax_withheld": ytd_data["totals"]["taxes"]["medicare_withheld"],
            }
    else:
        raise FileNotFoundError(f"No data file found for {year} {party} in {data_dir}")

def generate_csv_output(year, projection_data):
    """Generates the CSV output string."""
    output_filename = Path("data") / f"{year}_tax_projection.csv"
    
    with open(output_filename, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        
        writer.writerow(['', '', 'INCOME TAX BRACKETS (MFJ)', '', '', 'HIM', ''])
        writer.writerow(['', '', 'Applied to income of', f'${projection_data["final_taxable_income"]:,.2f}', '', 'Wages:', f'${projection_data["him_wages"]:,.2f}'])
        writer.writerow(['', 'Earnings Above', 'Rate / Bracket', 'Tax Assessed', '', 'Fed Tax Withheld:', f'${projection_data["him_fed_withheld"]:,.2f}'])
        
        previous_bracket_max = 0
        for bracket in projection_data["tax_brackets"]:
            rate = bracket['rate']
            row_to_write = ['', '', '', '']
            
            if 'up_to' in bracket:
                row_to_write[1] = f'${previous_bracket_max:,.2f}'
                current_bracket_max = bracket['up_to']
                income_in_bracket = min(projection_data["final_taxable_income"], current_bracket_max) - previous_bracket_max
                if income_in_bracket < 0: income_in_bracket = 0
                tax_assessed = income_in_bracket * rate
                row_to_write[3] = f'${tax_assessed:,.2f}'
                previous_bracket_max = current_bracket_max
            elif 'over' in bracket:
                row_to_write[1] = f'${bracket["over"]:,.2f}'
                if projection_data["final_taxable_income"] > bracket['over']:
                    income_in_bracket = projection_data["final_taxable_income"] - bracket['over']
                    tax_assessed = income_in_bracket * rate
                else: tax_assessed = 0
                row_to_write[3] = f'${tax_assessed:,.2f}'

            row_to_write[2] = f'{rate:.0%}'
            writer.writerow(row_to_write)

        writer.writerow(['', '', 'Total Assessed', f'${projection_data["federal_income_tax_assessed"]:,.2f}', '', '', ''])
        writer.writerow([])
        
        writer.writerow(['', '', '', '', '', 'HER', ''])
        writer.writerow(['', '', '', '', '', 'Wages:', f'${projection_data["her_wages"]:,.2f}'])
        writer.writerow(['', '', '', '', '', 'Fed Tax Withheld:', f'${projection_data["her_fed_withheld"]:,.2f}'])
        writer.writerow([])
        
        writer.writerow(['', '', '', '', '', 'TAXABLE INCOME', ''])
        writer.writerow(['', '', '', '', '', 'His wages per W-2', f'${projection_data["him_wages"]:,.2f}'])
        writer.writerow(['', '', '', '', '', 'Her wages per W-2', f'${projection_data["her_wages"]:,.2f}'])
        writer.writerow(['', '', '', '', '', 'Combined gross income', f'${projection_data["combined_wages"]:,.2f}'])
        writer.writerow(['', '', '', '', '', 'Standard deduction', f'-${projection_data["standard_deduction"]:,.2f}'])
        writer.writerow(['', '', '', '', '', 'Taxable income', f'${projection_data["final_taxable_income"]:,.2f}'])
        writer.writerow([])
        
        writer.writerow(['', '', '', '', '', 'MEDICARE TAXES OVER OR UNDERPAID', ''])
        writer.writerow(['', '', '', '', '', 'Total medicare wages (his and hers)', f'${projection_data["combined_medicare_wages"]:,.2f}'])
        writer.writerow(['', '', '', '', '', 'Total medicare taxes withheld', f'${projection_data["combined_medicare_withheld"]:,.2f}'])
        writer.writerow(['', '', '', '', '', 'Total medicare taxes assessed', f'-${projection_data["total_medicare_taxes_assessed"]:,.2f}'])
        writer.writerow(['', '', '', '', '', 'Refund on medicare taxes withheld (or amount owed if negative)', f'${projection_data["medicare_refund"]:,.2f}'])
        writer.writerow([])
        
        writer.writerow(['', '', '', '', '', 'TAX RETURN / REFUND PROJECTION', ''])
        writer.writerow(['', '', '', '', '', 'Federal Income Tax', f'-${projection_data["federal_income_tax_assessed"]:,.2f}'])
        writer.writerow(['', '', '', '', '', 'Additional Medicare Tax', f'${projection_data["medicare_refund"]:,.2f}'])
        writer.writerow(['', '', '', '', '', 'Tentative tax per tax return', f'-${projection_data["tentative_tax_per_return"]:,.2f}'])
        writer.writerow(['', '', '', '', '', 'His income tax withheld', f'${projection_data["him_fed_withheld"]:,.2f}'])
        writer.writerow(['', '', '', '', '', 'Her income tax withheld', f'${projection_data["her_fed_withheld"]:,.2f}'])
        writer.writerow(['', '', '', '', '', 'Refund (or owed, if negative)', f'${projection_data["final_refund"]:,.2f}'])

    print(f"\nSuccessfully generated tax projection CSV: {output_filename}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 generate_tax_projection.py <year>")
        sys.exit(1)
    
    year = int(sys.argv[1])
    
    try:
        tax_rules = load_tax_rules(year)
        him_data = load_party_data(year, "him")
        her_data = load_party_data(year, "her")
        
        him_wages = him_data["wages_tips_other_comp"]
        her_wages = her_data["wages_tips_other_comp"]
        combined_wages = him_wages + her_wages
        
        him_fed_withheld = him_data["federal_income_tax_withheld"]
        her_fed_withheld = her_data["federal_income_tax_withheld"]
        combined_fed_withheld = him_fed_withheld + her_fed_withheld
        
        him_medicare_wages = him_data.get("medicare_wages_and_tips", 0.0)
        her_medicare_wages = her_data.get("medicare_wages_and_tips", 0.0)
        combined_medicare_wages = him_medicare_wages + her_medicare_wages

        him_medicare_withheld = him_data["medicare_tax_withheld"]
        her_medicare_withheld = her_data["medicare_tax_withheld"]
        combined_medicare_withheld = him_medicare_withheld + her_medicare_withheld
        
        mfj_rules = tax_rules["mfj"]
        standard_deduction = mfj_rules["standard_deduction"]
        tax_brackets = mfj_rules["tax_brackets"]
        
        additional_medicare_tax_threshold = tax_rules["additional_medicare_tax_threshold"]
        
        final_taxable_income = combined_wages - standard_deduction
        
        federal_income_tax_assessed = calculate_federal_income_tax(final_taxable_income, tax_brackets)
        
        additional_medicare_tax_amount = calculate_additional_medicare_tax_amount(combined_medicare_wages, additional_medicare_tax_threshold)

        total_medicare_taxes_assessed = (combined_medicare_wages * 0.0145) + additional_medicare_tax_amount
        medicare_refund = combined_medicare_withheld - total_medicare_taxes_assessed
        
        tentative_tax_per_return = federal_income_tax_assessed - medicare_refund

        final_refund = combined_fed_withheld + medicare_refund - federal_income_tax_assessed
        
        projection_data = {
            "year": year, 
            "him_wages": him_wages, "her_wages": her_wages, "combined_wages": combined_wages,
            "him_fed_withheld": him_fed_withheld, "her_fed_withheld": her_fed_withheld,
            "combined_fed_withheld": combined_fed_withheld,
            "combined_medicare_wages": combined_medicare_wages,
            "combined_medicare_withheld": combined_medicare_withheld,
            "standard_deduction": standard_deduction, 
            "final_taxable_income": final_taxable_income,
            "federal_income_tax_assessed": federal_income_tax_assessed, 
            "tax_brackets": tax_brackets,
            "additional_medicare_tax": additional_medicare_tax_amount,
            "total_medicare_taxes_assessed": total_medicare_taxes_assessed,
            "medicare_refund": medicare_refund,
            "tentative_tax_per_return": tentative_tax_per_return,
            "final_refund": final_refund
        }

        generate_csv_output(year, projection_data)
        
    except FileNotFoundError as e:
        print(f"Error: {e}")
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()