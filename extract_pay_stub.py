#!/usr/bin/env python3
"""
Extract pay stub data from PDF and add/update pay_stub_data.json

Usage:
    python3 extract_pay_stub.py "path/to/pay_stub.pdf"

The script will:
1. Extract data from the PDF
2. Check if a stub with identical YTD numbers already exists
3. Add the stub if new, or skip if duplicate
"""

import sys
import json
import yaml
from pathlib import Path
from datetime import datetime
import PyPDF2
from processors import get_processor


def extract_text_from_pdf(pdf_path):
    """Extract text from PDF file for initial identification."""
    text = ""
    with open(pdf_path, 'rb') as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            text += page.extract_text() + "\n"
    return text


def load_config():
    """Load configuration from config.yaml."""
    config_file = Path("config.yaml")
    if not config_file.exists():
        raise FileNotFoundError("config.yaml not found. Please create it with employer definitions.")
    
    with open(config_file, 'r') as f:
        return yaml.safe_load(f)


def normalize_text(text):
    """Normalize text by removing spaces for comparison."""
    if not text:
        return ""
    return text.replace(" ", "").replace("\t", "").lower()


def identify_employer_and_party(text, filename, config):
    """
    Identify employer and party (him/her) based on config patterns.
    Returns (employer_name, party, processor_name)
    
    Text normalization removes spaces to handle PDF extraction variations
    like "Employer A LL C" vs "Employer A LLC" or "US-D AL-ADD" vs "US-DAL-ADD".
    """
    employers = config.get("employers", [])
    
    # Normalize text and filename for comparison
    normalized_text = normalize_text(text)
    normalized_filename = normalize_text(filename)
    
    # First try file name patterns
    for employer in employers:
        file_patterns = employer.get("file_patterns", [])
        for pattern in file_patterns:
            normalized_pattern = normalize_text(pattern)
            if normalized_pattern in normalized_filename:
                return (
                    employer["name"],
                    employer["party"],
                    employer.get("processor", "generic")
                )
    
    # Then try content patterns
    for employer in employers:
        content_patterns = employer.get("content_patterns", [])
        for pattern in content_patterns:
            normalized_pattern = normalize_text(pattern)
            if normalized_pattern in normalized_text:
                return (
                    employer["name"],
                    employer["party"],
                    employer.get("processor", "generic")
                )
    
    # Fallback: try to extract from text line by line
    lines = text.split('\n')
    for line in lines[:10]:
        if line.strip() and not line.strip().startswith('1600'):
            normalized_line = normalize_text(line)
            # Check if this line matches any employer pattern
            for employer in employers:
                content_patterns = employer.get("content_patterns", [])
                for pattern in content_patterns:
                    normalized_pattern = normalize_text(pattern)
                    if normalized_pattern in normalized_line:
                        return (
                            employer["name"],
                            employer["party"],
                            employer.get("processor", "generic")
                        )
    
    # Default fallback
    return ("Unknown Employer", "him", "generic")




def extract_pay_stub_data(pdf_path, config):
    """Extract all pay stub data from PDF using appropriate processor."""
    # First, identify employer and party to determine processor
    text = extract_text_from_pdf(pdf_path)
    
    if not text.strip():
        raise ValueError(f"Could not extract text from {pdf_path}")
    
    pdf_name = Path(pdf_path).name
    employer, party, processor_name = identify_employer_and_party(text, pdf_name, config)
    
    # Get the appropriate processor
    processor_class = get_processor(processor_name)
    
    # Process the PDF using the processor
    stub_data = processor_class.process(pdf_path, employer)
    
    # Add party and processor metadata
    stub_data["party"] = party
    stub_data["processor"] = processor_name
    
    return stub_data


def is_duplicate(new_stub, existing_stubs):
    """Check if a stub with identical YTD numbers already exists."""
    new_ytd_gross = new_stub.get("pay_summary", {}).get("ytd", {}).get("gross", 0.0)
    new_ytd_fit = new_stub.get("pay_summary", {}).get("ytd", {}).get("fit_taxable_wages", 0.0)
    new_ytd_taxes = new_stub.get("pay_summary", {}).get("ytd", {}).get("taxes", 0.0)
    
    for existing in existing_stubs:
        # Match on employer and party
        if (existing.get("employer") != new_stub.get("employer") or
            existing.get("party") != new_stub.get("party")):
            continue
        
        existing_ytd_gross = existing.get("pay_summary", {}).get("ytd", {}).get("gross", 0.0)
        existing_ytd_fit = existing.get("pay_summary", {}).get("ytd", {}).get("fit_taxable_wages", 0.0)
        existing_ytd_taxes = existing.get("pay_summary", {}).get("ytd", {}).get("taxes", 0.0)
        
        # Check if YTD numbers match (within small tolerance for floating point)
        if (abs(new_ytd_gross - existing_ytd_gross) < 0.01 and
            abs(new_ytd_fit - existing_ytd_fit) < 0.01 and
            abs(new_ytd_taxes - existing_ytd_taxes) < 0.01):
            return True
    
    return False


def process_pdf_file(pdf_path, config, data_dir):
    """Process a single PDF file and add to appropriate year's pay_stubs.json."""
    if not pdf_path.exists():
        print(f"Error: File not found: {pdf_path}")
        return False
    
    try:
        print(f"\nExtracting data from {pdf_path.name}...")
        new_stub = extract_pay_stub_data(pdf_path, config)
        
        # Determine year from pay_date
        pay_date = new_stub.get("pay_date", "")
        if not pay_date:
            raise ValueError("Could not determine pay date from PDF")
        
        year = pay_date.split("-")[0] if "-" in pay_date else pay_date.split("/")[2] if "/" in pay_date else str(datetime.now().year)
        party = new_stub.get("party", "him")
        
        # Load existing pay stub data for this year and party
        data_file = data_dir / f"{year}_{party}_pay_stubs.json"
        if data_file.exists():
            with open(data_file, 'r') as f:
                data = json.load(f)
        else:
            data = {"pay_stubs": []}
        
        # Check for duplicates
        if is_duplicate(new_stub, data["pay_stubs"]):
            print(f"  Stub with identical YTD numbers already exists. Skipping import.")
            print(f"    Employer: {new_stub['employer']}")
            print(f"    Party: {new_stub['party'].upper()}")
            print(f"    Pay Date: {new_stub['pay_date']}")
            print(f"    YTD Gross: ${new_stub['pay_summary']['ytd']['gross']:,.2f}")
            return False
        
        # Add new stub
        data["pay_stubs"].append(new_stub)
        
        # Save updated data
        with open(data_file, 'w') as f:
            json.dump(data, f, indent=2)
        
        # Also save backup
        backup_file = data_dir / f"{year}_{party}_pay_stubs_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(backup_file, 'w') as f:
            json.dump(data, f, indent=2)
        
        print(f"  ✓ Successfully added pay stub to {data_file}")
        print(f"    Employer: {new_stub['employer']}")
        print(f"    Party: {new_stub['party'].upper()}")
        print(f"    Processor: {new_stub['processor']}")
        print(f"    Pay Date: {new_stub['pay_date']}")
        print(f"    YTD Gross: ${new_stub['pay_summary']['ytd']['gross']:,.2f}")
        return True
        
    except Exception as e:
        print(f"  ✗ Error extracting pay stub data: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 extract_pay_stub.py <pdf_file_or_directory> [year]")
        print("  If year is specified, only processes PDFs matching that year")
        print("  If directory is provided, processes all PDFs in that directory")
        sys.exit(1)
    
    # Load config
    try:
        config = load_config()
    except Exception as e:
        print(f"Error loading config.yaml: {e}")
        sys.exit(1)
    
    # Setup directories
    stubs_dir = Path("stubs")
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    
    # Determine what to process
    input_path = Path(sys.argv[1])
    filter_year = sys.argv[2] if len(sys.argv) > 2 else None
    
    pdf_files = []
    
    if input_path.is_file():
        # Single file
        if input_path.suffix.lower() == '.pdf':
            pdf_files = [input_path]
        else:
            print(f"Error: {input_path} is not a PDF file")
            sys.exit(1)
    elif input_path.is_dir():
        # Directory - find all PDFs
        if filter_year:
            # Filter by year in filename
            pattern = f"*{filter_year}*.pdf"
            pdf_files = list(input_path.glob(pattern))
        else:
            pdf_files = list(input_path.glob("*.pdf"))
    else:
        # Try stubs directory
        if filter_year:
            pattern = f"*{filter_year}*.pdf"
            pdf_files = list(stubs_dir.glob(pattern))
        else:
            pdf_files = list(stubs_dir.glob("*.pdf"))
    
    if not pdf_files:
        print(f"No PDF files found to process.")
        if filter_year:
            print(f"  Searched for files matching year: {filter_year}")
        sys.exit(1)
    
    print(f"Found {len(pdf_files)} PDF file(s) to process")
    
    # Process each PDF
    success_count = 0
    for pdf_path in sorted(pdf_files):
        if process_pdf_file(pdf_path, config, data_dir):
            success_count += 1
    
    print(f"\n{'='*60}")
    print(f"Processing complete: {success_count}/{len(pdf_files)} files processed successfully")


if __name__ == "__main__":
    main()

