"""
Employer B (EB) pay stub processor.

Handles EB pay stub PDF format.
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


class EBProcessor:
    """Processor for Employer B pay stubs."""
    
    @staticmethod
    def process(pdf_path, employer_name):
        """
        Process a EB pay stub PDF and return standardized JSON structure.
        
        Args:
            pdf_path: Path to PDF file
            employer_name: Name of employer (typically "Employer B")
        
        Returns:
            dict: Standardized pay stub data structure
        """
        text = extract_text_from_pdf(pdf_path)
        
        if not text.strip():
            raise ValueError(f"Could not extract text from {pdf_path}")
        
        pdf_name = Path(pdf_path).name
        
        # Extract basic information
        pay_date = EBProcessor._extract_pay_date(text)
        period = EBProcessor._extract_period_dates(text)
        document_id = EBProcessor._extract_document_id(text)
        net_pay = EBProcessor._extract_net_pay(text)
        
        # Extract detailed sections
        earnings = EBProcessor._extract_earnings(text)
        taxes = EBProcessor._extract_taxes(text)
        deductions = EBProcessor._extract_deductions(text)
        pay_summary = EBProcessor._extract_pay_summary(text)
        
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
        """Extract pay date from text - EB format may differ."""
        # Try common patterns
        patterns = [
            r'Pay\s+Date[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{4})',
            r'Check\s+Date[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{4})',
            r'(\d{1,2}[/-]\d{1,2}[/-]\d{4})',  # Generic date pattern
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return parse_date(match.group(1))
        
        return None
    
    @staticmethod
    def _extract_period_dates(text):
        """Extract period start and end dates - EB format may differ."""
        start_match = re.search(r'Period\s+Start[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{4})', text, re.IGNORECASE)
        end_match = re.search(r'Period\s+End[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{4})', text, re.IGNORECASE)
        
        if not start_match:
            start_match = re.search(r'Pay\s+Period[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{4})', text, re.IGNORECASE)
        
        start = parse_date(start_match.group(1)) if start_match else None
        end = parse_date(end_match.group(1)) if end_match else None
        
        return {"start": start, "end": end}
    
    @staticmethod
    def _extract_document_id(text):
        """Extract document/check number."""
        # Try various patterns
        patterns = [
            r'Check\s+Number[:\s]+(\d+)',
            r'Document\s+Number[:\s]+(\d+)',
            r'Stub\s+Number[:\s]+(\d+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1)
        
        return None
    
    @staticmethod
    def _extract_net_pay(text):
        """Extract net pay amount."""
        patterns = [
            r'Net\s+Pay[:\s]+\$?([\d,]+\.?\d*)',
            r'Net\s+Amount[:\s]+\$?([\d,]+\.?\d*)',
            r'Take\s+Home[:\s]+\$?([\d,]+\.?\d*)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return extract_amount(match.group(1))
        
        return 0.0
    
    @staticmethod
    def _extract_earnings(text):
        """Extract earnings section - EB format may differ."""
        earnings = []
        
        # Look for earnings section with various headers
        earnings_match = re.search(
            r'(Earnings|Current\s+Earnings|Gross\s+Earnings).*?(Deductions|Taxes|Total)',
            text,
            re.DOTALL | re.IGNORECASE
        )
        
        if not earnings_match:
            return earnings
        
        earnings_text = earnings_match.group(0)
        
        # Pattern for earnings: Description, Rate, Hours, Current, YTD
        # This is a placeholder - actual EB format will need to be determined
        pattern = r'([A-Za-z\s/]+?)\s+\$?([\d,]+\.?\d*)\s+(\d+\.?\d*)\s+\$?([\d,]+\.?\d*)\s+\$?([\d,]+\.?\d*)'
        
        for match in re.finditer(pattern, earnings_text):
            etype = match.group(1).strip()
            current = extract_amount(match.group(4))
            ytd = extract_amount(match.group(5))
            
            if current == 0.0 and ytd == 0.0:
                continue
            
            earnings.append({
                "type": etype,
                "current_amount": current,
                "ytd_amount": ytd
            })
        
        return earnings
    
    @staticmethod
    def _extract_taxes(text):
        """Extract tax information - EB format may differ."""
        taxes = {
            "federal_income_tax": {"taxable_wages": 0.0, "current_withheld": 0.0, "ytd_withheld": 0.0},
            "social_security": {"taxable_wages": 0.0, "current_withheld": 0.0, "ytd_withheld": 0.0},
            "medicare": {"taxable_wages": 0.0, "current_withheld": 0.0, "ytd_withheld": 0.0}
        }
        
        taxes_match = re.search(
            r'(Taxes|Withholdings|Deductions).*?(Net|Total|Summary)',
            text,
            re.DOTALL | re.IGNORECASE
        )
        
        if not taxes_match:
            return taxes
        
        taxes_text = taxes_match.group(0)
        
        # Federal Income Tax
        fit_patterns = [
            r'Federal\s+Income\s+Tax[:\s]+\$?([\d,]+\.?\d*)[:\s]+\$?([\d,]+\.?\d*)',
            r'FIT[:\s]+\$?([\d,]+\.?\d*)[:\s]+\$?([\d,]+\.?\d*)',
        ]
        
        for pattern in fit_patterns:
            fit_match = re.search(pattern, taxes_text, re.IGNORECASE)
            if fit_match:
                taxes["federal_income_tax"] = {
                    "taxable_wages": extract_amount(fit_match.group(1)),
                    "current_withheld": extract_amount(fit_match.group(2)),
                    "ytd_withheld": extract_amount(fit_match.group(2))  # May need adjustment
                }
                break
        
        # Social Security
        ss_patterns = [
            r'Social\s+Security[:\s]+\$?([\d,]+\.?\d*)[:\s]+\$?([\d,]+\.?\d*)',
            r'SS[:\s]+\$?([\d,]+\.?\d*)[:\s]+\$?([\d,]+\.?\d*)',
        ]
        
        for pattern in ss_patterns:
            ss_match = re.search(pattern, taxes_text, re.IGNORECASE)
            if ss_match:
                taxes["social_security"] = {
                    "taxable_wages": extract_amount(ss_match.group(1)),
                    "current_withheld": extract_amount(ss_match.group(2)),
                    "ytd_withheld": extract_amount(ss_match.group(2))  # May need adjustment
                }
                break
        
        # Medicare
        med_patterns = [
            r'Medicare[:\s]+\$?([\d,]+\.?\d*)[:\s]+\$?([\d,]+\.?\d*)',
            r'MED[:\s]+\$?([\d,]+\.?\d*)[:\s]+\$?([\d,]+\.?\d*)',
        ]
        
        for pattern in med_patterns:
            med_match = re.search(pattern, taxes_text, re.IGNORECASE)
            if med_match:
                taxes["medicare"] = {
                    "taxable_wages": extract_amount(med_match.group(1)),
                    "current_withheld": extract_amount(med_match.group(2)),
                    "ytd_withheld": extract_amount(med_match.group(2))  # May need adjustment
                }
                break
        
        return taxes
    
    @staticmethod
    def _extract_deductions(text):
        """Extract deductions section - EB format may differ."""
        deductions = []
        
        ded_match = re.search(
            r'(Deductions|Voluntary\s+Deductions).*?(Taxes|Total|Net)',
            text,
            re.DOTALL | re.IGNORECASE
        )
        
        if not ded_match:
            return deductions
        
        ded_text = ded_match.group(0)
        
        # Pattern: Description, Current, YTD
        pattern = r'([A-Za-z\s/]+?)[:\s]+\$?([\d,]+\.?\d*)[:\s]+\$?([\d,]+\.?\d*)'
        
        for match in re.finditer(pattern, ded_text):
            dtype = match.group(1).strip()
            emp_current = extract_amount(match.group(2))
            emp_ytd = extract_amount(match.group(3))
            
            if emp_current == 0.0 and emp_ytd == 0.0:
                continue
            
            deductions.append({
                "type": dtype,
                "current_amount": emp_current,
                "ytd_amount": emp_ytd
            })
        
        return deductions
    
    @staticmethod
    def _extract_pay_summary(text):
        """Extract pay summary section - EB format may differ."""
        summary = {
            "current": {"gross": 0.0, "fit_taxable_wages": 0.0, "taxes": 0.0, "deductions": 0.0, "net_pay": 0.0},
            "ytd": {"gross": 0.0, "fit_taxable_wages": 0.0, "taxes": 0.0, "deductions": 0.0, "net_pay": 0.0}
        }
        
        # Look for summary section
        summary_match = re.search(
            r'(Summary|Totals|Pay\s+Summary).*?(\d+\.?\d*).*?(\d+\.?\d*).*?(\d+\.?\d*)',
            text,
            re.DOTALL | re.IGNORECASE
        )
        
        if summary_match:
            # Extract gross, taxes, deductions, net
            # This is a placeholder - actual EB format will need to be determined
            summary["current"] = {
                "gross": 0.0,  # Will need to extract from actual format
                "fit_taxable_wages": 0.0,
                "taxes": 0.0,
                "deductions": 0.0,
                "net_pay": 0.0
            }
            
            summary["ytd"] = {
                "gross": 0.0,  # Will need to extract from actual format
                "fit_taxable_wages": 0.0,
                "taxes": 0.0,
                "deductions": 0.0,
                "net_pay": 0.0
            }
        
        return summary

