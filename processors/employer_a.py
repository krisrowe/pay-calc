"""
Employer A LLC and Employer A LLC pay stub processor.

Handles both Employer A LLC and Employer A LLC pay stub formats.
"""

import re
from pathlib import Path
from datetime import datetime
import PyPDF2


def extract_text_from_pdf(pdf_path):
    """Extract text from PDF file."""
    text = ""
    with open(pdf_path, 'rb') as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            text += page.extract_text() + "\n"
    return text


def parse_date(date_str):
    """Parse date string in various formats to YYYY-MM-DD."""
    formats = [
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%m-%d-%Y",
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    
    return date_str.strip()


def extract_amount(value_str):
    """Extract numeric amount from string, handling currency formatting."""
    if not value_str:
        return 0.0
    
    cleaned = re.sub(r'[$,]\s*', '', str(value_str).strip())
    is_negative = cleaned.startswith('-') or cleaned.startswith('(')
    cleaned = cleaned.replace('(', '').replace(')', '').replace('-', '')
    
    try:
        amount = float(cleaned)
        return -amount if is_negative else amount
    except ValueError:
        return 0.0


class Employer AProcessor:
    """Processor for Employer A LLC and Employer A LLC pay stubs."""
    
    @staticmethod
    def process(pdf_path, employer_name):
        """
        Process a Employer A pay stub PDF and return standardized JSON structure.
        
        Args:
            pdf_path: Path to PDF file
            employer_name: Name of employer (Employer A LLC or Employer A LLC)
        
        Returns:
            dict: Standardized pay stub data structure
        """
        text = extract_text_from_pdf(pdf_path)
        
        if not text.strip():
            raise ValueError(f"Could not extract text from {pdf_path}")
        
        pdf_name = Path(pdf_path).name
        
        # Extract basic information
        pay_date = Employer AProcessor._extract_pay_date(text)
        period = Employer AProcessor._extract_period_dates(text)
        document_id = Employer AProcessor._extract_document_id(text)
        net_pay = Employer AProcessor._extract_net_pay(text)
        
        # Extract detailed sections
        earnings = Employer AProcessor._extract_earnings(text)
        taxes = Employer AProcessor._extract_taxes(text)
        deductions = Employer AProcessor._extract_deductions(text)
        pay_summary = Employer AProcessor._extract_pay_summary(text)
        
        # Use pay_summary net_pay if we couldn't extract it separately
        if net_pay == 0.0 and pay_summary["current"]["net_pay"] != 0.0:
            net_pay = pay_summary["current"]["net_pay"]
        
        return {
            "file_name": pdf_name,
            "employer": employer_name,
            "pay_date": pay_date,
            "period": period,
            "document_id": document_id,
            "net_pay": net_pay,
            "earnings": earnings,
            "taxes": taxes,
            "deductions": deductions,
            "pay_summary": pay_summary
        }
    
    @staticmethod
    def _extract_pay_date(text):
        """Extract pay date from text."""
        # Normalize text first to handle space variations
        # Pattern allows for variable spacing in "Pay Date"
        match = re.search(r'Pay\s*Date\s+(\d{1,2}[/-]\d{1,2}[/-]\d{4})', text, re.IGNORECASE)
        if match:
            return parse_date(match.group(1))
        
        match = re.search(r'Pay\s*Date[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{4})', text, re.IGNORECASE)
        if match:
            return parse_date(match.group(1))
        
        return None
    
    @staticmethod
    def _extract_period_dates(text):
        """Extract period start and end dates."""
        # Handle space variations like "Period Star t Date" vs "Period Start Date"
        start_match = re.search(r'Period\s+Star\s*t\s+Date\s+(\d{1,2}[/-]\d{1,2}[/-]\d{4})', text, re.IGNORECASE)
        if not start_match:
            # Try without the space issue
            start_match = re.search(r'Period\s+Start\s+Date\s+(\d{1,2}[/-]\d{1,2}[/-]\d{4})', text, re.IGNORECASE)
        
        end_match = re.search(r'Period\s+End\s+Date\s+(\d{1,2}[/-]\d{1,2}[/-]\d{4})', text, re.IGNORECASE)
        
        start = parse_date(start_match.group(1)) if start_match else None
        end = parse_date(end_match.group(1)) if end_match else None
        
        return {"start": start, "end": end}
    
    @staticmethod
    def _extract_document_id(text):
        """Extract document ID."""
        match = re.search(r'Document\s+(\d+)', text, re.IGNORECASE)
        return match.group(1) if match else None
    
    @staticmethod
    def _extract_net_pay(text):
        """Extract net pay amount."""
        match = re.search(r'Net\s+P\s*ay\s+\$?([\d,]+\.?\d*)', text, re.IGNORECASE)
        return extract_amount(match.group(1)) if match else 0.0
    
    @staticmethod
    def _extract_earnings(text):
        """Extract earnings section."""
        earnings = []

        # Handle space variations in "Pay T ype" vs "Pay Type"
        earnings_match = re.search(r'Earnings\s+Pay\s+T\s*ype.*?Total\s+Hours', text, re.DOTALL | re.IGNORECASE)
        if not earnings_match:
            # Try without space issue
            earnings_match = re.search(r'Earnings\s+Pay\s+Type.*?Total\s+Hours', text, re.DOTALL | re.IGNORECASE)

        if not earnings_match:
            return earnings

        earnings_text = earnings_match.group(0)

        # Normalize text: replace newlines with spaces, collapse multiple spaces
        # This handles cases where type names span lines (e.g., "Regular\nPay")
        normalized = re.sub(r'\s+', ' ', earnings_text)

        # Remove header text
        normalized = re.sub(r'Earnings\s+Pay\s+T\s*ype\s+Hours\s*Pay\s*Rate\s+Current\s+YTD\s*', '', normalized)
        normalized = re.sub(r'Total\s+Hours\s+W\s*orked.*', '', normalized)

        # Pattern 1: Type with Hours/Rate and YTD
        # e.g., "Regular Pay 80.000000 $75.00 $5,000.00 $10,000.00"
        # e.g., "Sales Bonus Q1 0.000000 $0.0000 $0.00 $15,000.00"
        pattern_with_ytd = re.compile(
            r'([A-Za-z][A-Za-z0-9\s/\-]*?)\s*'  # Type name (starts with letter, can include digits like Q1)
            r'(\d+\.\d+)\s+'                    # Hours
            r'\$?([\d,]+\.?\d*)\s+'             # Rate
            r'\$?([\d,]+\.?\d*)\s+'             # Current
            r'\$?([\d,]+\.?\d*)',               # YTD
            re.IGNORECASE
        )

        # Pattern 2: Type with Hours/Rate but NO YTD (continuation lines)
        # e.g., "Regular Pay 40.000000 $85.5769 $3,400.00"
        # These are additional lines for same type where YTD appears only on last line
        pattern_no_ytd = re.compile(
            r'([A-Za-z][A-Za-z0-9\s/\-]*?)\s+'  # Type name (can include digits)
            r'(\d+\.\d+)\s+'                    # Hours
            r'\$?([\d,]+\.?\d*)\s+'             # Rate
            r'\$?([\d,]+\.?\d*)(?:\s|$)',       # Current (followed by space or end, not another $)
            re.IGNORECASE
        )

        # Pattern 3: Type without Hours/Rate (just Current and YTD)
        # e.g., "Prize/ Gift $321.67 $2,000.00"
        pattern_simple = re.compile(
            r'([A-Za-z][A-Za-z0-9\s/\-]*?)\s+'  # Type name (can include digits)
            r'\$([\d,]+\.?\d*)\s+'              # Current
            r'\$([\d,]+\.?\d*)',                # YTD
            re.IGNORECASE
        )

        # Collect all earnings, aggregating same-type entries
        # Key: normalized type name -> {"current": sum, "ytd": last seen}
        earnings_map = {}

        # First pass: lines with YTD (these have the authoritative YTD)
        for match in pattern_with_ytd.finditer(normalized):
            etype = re.sub(r'\s+', ' ', match.group(1).strip()).strip()
            current = extract_amount(match.group(4))
            ytd = extract_amount(match.group(5))

            key = etype.lower()
            if key in earnings_map:
                earnings_map[key]["current"] += current
                earnings_map[key]["ytd"] = ytd  # Last YTD wins
            else:
                earnings_map[key] = {"type": etype, "current": current, "ytd": ytd}

        # Second pass: lines without YTD (add to current, don't overwrite YTD)
        for match in pattern_no_ytd.finditer(normalized):
            etype = re.sub(r'\s+', ' ', match.group(1).strip()).strip()
            current = extract_amount(match.group(4))

            # Check this isn't a false match from pattern_with_ytd
            # (pattern_no_ytd might match beginning of a line that has YTD)
            match_end = match.end()
            if match_end < len(normalized) and normalized[match_end:match_end+1] == '$':
                continue  # This line has YTD, skip (already captured above)

            key = etype.lower()
            if key in earnings_map:
                earnings_map[key]["current"] += current
            else:
                # No YTD seen yet for this type
                earnings_map[key] = {"type": etype, "current": current, "ytd": 0.0}

        # Third pass: simple patterns (no hours/rate)
        for match in pattern_simple.finditer(normalized):
            etype = re.sub(r'\s+', ' ', match.group(1).strip()).strip()
            current = extract_amount(match.group(2))
            ytd = extract_amount(match.group(3))

            # Skip obvious non-earnings
            if etype.lower() in ['current', 'ytd', 'total']:
                continue

            key = etype.lower()
            if key not in earnings_map:
                earnings_map[key] = {"type": etype, "current": current, "ytd": ytd}

        # Convert to list, filtering zero-value entries
        for key, data in earnings_map.items():
            if data["current"] == 0.0 and data["ytd"] == 0.0:
                if key not in ['regular pay', 'recognition bonus', 'sales bonus']:
                    continue
            earnings.append({
                "type": data["type"],
                "current_amount": data["current"],
                "ytd_amount": data["ytd"]
            })

        return earnings
    
    @staticmethod
    def _extract_taxes(text):
        """Extract tax information."""
        taxes = {
            "federal_income_tax": {"taxable_wages": 0.0, "current_withheld": 0.0, "ytd_withheld": 0.0},
            "social_security": {"taxable_wages": 0.0, "current_withheld": 0.0, "ytd_withheld": 0.0},
            "medicare": {"taxable_wages": 0.0, "current_withheld": 0.0, "ytd_withheld": 0.0}
        }
        
        taxes_match = re.search(r'Taxes\s+Tax\s+Based\s+On.*?Paid\s+Time\s+Off', text, re.DOTALL | re.IGNORECASE)
        if not taxes_match:
            return taxes
        
        taxes_text = taxes_match.group(0)
        
        # Federal Income Tax
        fit_match = re.search(r'Federal\s+Income\s+Tax\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)', taxes_text, re.IGNORECASE)
        if fit_match:
            taxes["federal_income_tax"] = {
                "taxable_wages": extract_amount(fit_match.group(1)),
                "current_withheld": extract_amount(fit_match.group(2)),
                "ytd_withheld": extract_amount(fit_match.group(3))
            }
        
        # Social Security
        ss_match = re.search(r'Social\s+Security\s+Employee\s+Tax\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)', taxes_text, re.IGNORECASE)
        if ss_match:
            taxes["social_security"] = {
                "taxable_wages": extract_amount(ss_match.group(1)),
                "current_withheld": extract_amount(ss_match.group(2)),
                "ytd_withheld": extract_amount(ss_match.group(3))
            }
        
        # Medicare
        med_match = re.search(r'Employee\s+Medicare\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)', taxes_text, re.IGNORECASE)
        if med_match:
            taxes["medicare"] = {
                "taxable_wages": extract_amount(med_match.group(1)),
                "current_withheld": extract_amount(med_match.group(2)),
                "ytd_withheld": extract_amount(med_match.group(3))
            }
        
        return taxes
    
    @staticmethod
    def _extract_deductions(text):
        """Extract deductions section."""
        deductions = []
        
        ded_match = re.search(r'Deductions\s+Deduction.*?Taxes', text, re.DOTALL | re.IGNORECASE)
        if not ded_match:
            return deductions
        
        ded_text = ded_match.group(0)
        
        # Pattern: Type, Employee Current, Employee YTD, Employer Current, Employer YTD
        pattern = r'([A-Za-z\s/]+?)\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)'
        
        for match in re.finditer(pattern, ded_text):
            dtype = match.group(1).strip()
            emp_current = extract_amount(match.group(2))
            emp_ytd = extract_amount(match.group(3))
            emp_match_ytd = extract_amount(match.group(5))
            
            if emp_current == 0.0 and emp_ytd == 0.0:
                continue
            
            ded = {
                "type": dtype,
                "current_amount": emp_current,
                "ytd_amount": emp_ytd
            }
            
            if emp_match_ytd != 0.0:
                ded["employer_match_ytd"] = emp_match_ytd
            
            deductions.append(ded)
        
        return deductions
    
    @staticmethod
    def _extract_pay_summary(text):
        """Extract pay summary section."""
        summary = {
            "current": {"gross": 0.0, "fit_taxable_wages": 0.0, "taxes": 0.0, "deductions": 0.0, "net_pay": 0.0},
            "ytd": {"gross": 0.0, "fit_taxable_wages": 0.0, "taxes": 0.0, "deductions": 0.0, "net_pay": 0.0}
        }
        
        # Handle space variations in "Pay Summar y" vs "Pay Summary"
        summary_match = re.search(r'Pay\s+Summar\s*y\s+Gross.*?YTD\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)', text, re.DOTALL | re.IGNORECASE)
        if not summary_match:
            # Try without space issue
            summary_match = re.search(r'Pay\s+Summary\s+Gross.*?YTD\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)', text, re.DOTALL | re.IGNORECASE)
        if summary_match:
            current_match = re.search(r'Current\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)', text, re.IGNORECASE)
            if current_match:
                summary["current"] = {
                    "gross": extract_amount(current_match.group(1)),
                    "fit_taxable_wages": extract_amount(current_match.group(2)),
                    "taxes": extract_amount(current_match.group(3)),
                    "deductions": extract_amount(current_match.group(4)),
                    "net_pay": extract_amount(current_match.group(5))
                }
            
            summary["ytd"] = {
                "gross": extract_amount(summary_match.group(1)),
                "fit_taxable_wages": extract_amount(summary_match.group(2)),
                "taxes": extract_amount(summary_match.group(3)),
                "deductions": extract_amount(summary_match.group(4)),
                "net_pay": extract_amount(summary_match.group(5))
            }
        
        return summary

