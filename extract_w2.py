#!/usr/bin/env python3
"""
Extracts and aggregates W-2 data from multiple sources for all parties.

This script processes W-2 data for a given year. It finds all W-2 PDFs in the
`source-data` directory and uses keywords from `config.yaml` to identify which
company and party each PDF belongs to. It checks both filenames and file content.

It also processes manually created W-2 JSON files as a secondary source,
checking for conflicts with processed PDFs.

Finally, it aggregates all W-2s for each party ('him' and 'her') into two
separate output files: `data/<year>_him_w2_forms.json` and
`data/<year>her_w2_forms.json`.

Usage:
    python3 extract_w2.py <year>
"""

import sys
import json
import re
from pathlib import Path
import PyPDF2
import yaml
from collections import defaultdict

def load_config():
    """Load configuration from config.yaml."""
    config_file = Path("config.yaml")
    if not config_file.exists():
        raise FileNotFoundError("config.yaml not found.")
    with open(config_file, 'r') as f:
        return yaml.safe_load(f)

def extract_text_from_pdf(pdf_path):
    """Extract text from PDF file."""
    text = ""
    with open(pdf_path, 'rb') as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            text += page.extract_text() or ""
    return text

def find_company_and_party_from_keywords(text_to_search, config):
    """Find the company and party by searching text for keywords from the config."""
    for party, party_config in config.get("parties", {}).items():
        for company in party_config.get("companies", []):
            for keyword in company.get("keywords", []):
                if keyword.lower() in text_to_search.lower():
                    return company, party
    return None, None

def parse_w2_text(text):
    """Parse the text of a W-2 to extract key financial data."""
    w2_data = {}
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    
    for i, line in enumerate(lines):
        if "1" in line and "Wages" in line and "2" in line and "Federal" in line:
            if i + 1 < len(lines):
                values = re.findall(r'[\d,]+\.\d{2}', lines[i+1])
                if len(values) >= 2:
                    w2_data['wages_tips_other_comp'] = float(values[0].replace(',', ''))
                    w2_data['federal_income_tax_withheld'] = float(values[1].replace(',', ''))
        elif "3" in line and "Social security wages" in line:
            for j in range(i + 1, min(i + 5, len(lines))):
                values = re.findall(r'[\d,]+\.\d{2}', lines[j])
                if len(values) >= 2:
                    w2_data['social_security_wages'] = float(values[0].replace(',', ''))
                    w2_data['social_security_tax_withheld'] = float(values[1].replace(',', ''))
                    break
        elif "5" in line and "Medicare wages" in line:
            if i + 1 < len(lines):
                values = re.findall(r'[\d,]+\.\d{2}', lines[i+1])
                if len(values) >= 2:
                    w2_data['medicare_wages_and_tips'] = float(values[0].replace(',', ''))
                    w2_data['medicare_tax_withheld'] = float(values[1].replace(',', ''))
    return w2_data

def main():
    if len(sys.argv) != 2:
        print("Usage: python3 extract_w2.py <year>")
        sys.exit(1)
    
    year = sys.argv[1]
    source_dir = Path("source-data")
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)

    try:
        config = load_config()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    processed_sources = defaultdict(list)

    # --- 1. Process all PDFs ---
    pdf_files = list(source_dir.glob(f"*{year}*W-2*.pdf"))
    unidentified_pdfs = []

    print(f"Found {len(pdf_files)} W-2 PDF(s) for {year}...")
    for pdf_path in pdf_files:
        company, party = find_company_and_party_from_keywords(pdf_path.name, config)
        
        pdf_text = ""
        if not company:
            # If not found in filename, extract text and try to identify from content
            pdf_text = extract_text_from_pdf(pdf_path)
            company, party = find_company_and_party_from_keywords(pdf_text, config)

        if not company:
            unidentified_pdfs.append(pdf_path.name)
            continue
        
        print(f"  Identified '{pdf_path.name}' as '{company['name']}' ({party})")
        if not pdf_text: pdf_text = extract_text_from_pdf(pdf_path)
        
        w2_data = parse_w2_text(pdf_text)
        if not w2_data:
            print(f"    Warning: Could not parse financial data from {pdf_path.name}. Skipping.")
            continue
            
        w2_form = {
            "employer": company['name'],
            "source_type": "pdf",
            "source_file": pdf_path.name,
            "data": w2_data
        }
        processed_sources[party].append(w2_form)

    if unidentified_pdfs:
        print("\nError: Could not identify an employer for the following PDF(s):")
        for pdf_name in unidentified_pdfs:
            print(f"  - {pdf_name}")
        sys.exit(1)

    # --- 2. Process all Manual Files ---
    manual_files = list(source_dir.glob(f"{year}_manual-w2_*.json"))
    print(f"\nFound {len(manual_files)} manual W-2 file(s)...")

    for manual_path in manual_files:
        with open(manual_path, 'r') as f:
            manual_data = json.load(f)
        
        employer_name = manual_data.get("employer")
        if not employer_name:
            print(f"Warning: Manual file {manual_path.name} is missing 'employer' key. Skipping.")
            continue

        # Use the employer slug from the manual file to find company and party from config
        company_info, party = find_company_and_party_from_keywords(employer_name, config)
        
        if not company_info:
            print(f"Warning: Could not find company config for employer slug '{employer_slug}' in {manual_path.name}. Skipping.")
            continue
        employer_name_from_config = company_info['name'] # Get the official name from config

        # Check for conflicts - an employer shouldn't have a PDF and a manual W-2
        if any(src.get('employer') == employer_name_from_config for src in processed_sources[party]):
            print(f"Error: Conflict for employer '{employer_name_from_config}'. A PDF was already processed for {party}. "
                  f"Please remove the conflicting manual file: {manual_path.name}")
            sys.exit(1)
        
        print(f"  Processing manual file '{manual_path.name}' for {party} ({employer_name_from_config})...")
        w2_form = {
            "employer": employer_name_from_config,
            "source_type": "manual",
            "source_file": manual_path.name,
            "data": manual_data["data"]
        }
        processed_sources[party].append(w2_form)

    # --- 3. Write Output Files ---
    print("\nWriting final output files...")
    for party, forms in processed_sources.items():
        if not forms: continue

        final_output = {"year": year, "party": party, "forms": forms}
        output_file = data_dir / f"{year}_{party}_w2_forms.json"
        
        with open(output_file, 'w') as f:
            json.dump(final_output, f, indent=2)
        
        print(f"  Successfully aggregated {len(forms)} W-2 form(s) to {output_file}")
        print(json.dumps(final_output, indent=2))
        
    print("\nExtraction complete.")

if __name__ == "__main__":
    main()
