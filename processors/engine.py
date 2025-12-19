"""
YAML-driven document parser engine.

Replaces employer-specific Python processors (employer_a.py, employer_b.py) with
data-driven YAML parser definitions.

The engine:
1. Loads all YAML parser definitions from parsers/ directory
2. Qualifies documents against parser patterns
3. Extracts fields using regex patterns defined in YAML
4. Returns standardized JSON matching current processor output
"""

import re
import yaml
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple
import PyPDF2


class ParserCache:
    """Cache for loaded YAML parser definitions."""

    def __init__(self, parsers_dir: str = "parsers/"):
        self.parsers: List[Dict] = []
        self.qualifiers: List[List[re.Pattern]] = []
        self.parsers_dir = Path(parsers_dir)
        self._loaded = False

    def _get_flags(self, pattern_def: Dict, parser: Dict) -> int:
        """Get regex flags from pattern definition or parser defaults."""
        flags = 0

        # Get flags from pattern def, fall back to parser defaults
        flag_list = pattern_def.get("flags", parser.get("defaults", {}).get("flags", []))

        # Normalize to list
        if isinstance(flag_list, str):
            flag_list = [flag_list]

        flag_map = {
            "IGNORECASE": re.IGNORECASE,
            "MULTILINE": re.MULTILINE,
            "DOTALL": re.DOTALL,
            "I": re.IGNORECASE,
            "M": re.MULTILINE,
            "S": re.DOTALL,
        }

        for flag_name in flag_list:
            if isinstance(flag_name, str):
                flags |= flag_map.get(flag_name.upper(), 0)

        return flags

    def load_all(self) -> None:
        """Load all YAML parser definitions."""
        if self._loaded:
            return

        if not self.parsers_dir.exists():
            self._loaded = True
            return

        for yaml_file in sorted(self.parsers_dir.glob("*.yaml")):
            try:
                parser = yaml.safe_load(yaml_file.read_text())
                if not parser:
                    continue

                # Store the source file for debugging
                parser["_source_file"] = yaml_file.name

                # Pre-compile qualifier patterns
                qualifier = parser.get("qualifier", {})
                patterns = qualifier.get("patterns", [])

                compiled = []
                for p in patterns:
                    if isinstance(p, str):
                        # Simple string pattern
                        compiled.append(re.compile(p, self._get_flags({}, parser)))
                    elif isinstance(p, dict):
                        # Pattern with options
                        regex = p.get("regex", "")
                        if regex:
                            compiled.append(re.compile(regex, self._get_flags(p, parser)))

                self.parsers.append(parser)
                self.qualifiers.append(compiled)

            except Exception as e:
                print(f"Warning: Failed to load parser {yaml_file}: {e}")

        self._loaded = True

    def find_matching_parser(self, text: str) -> Optional[Dict]:
        """Find the best matching parser for the given text.

        Uses a scoring system: parser with most qualifier hits wins.
        Ties broken by min_matches threshold (higher = more specific).
        """
        self.load_all()

        # Normalize text for matching (collapse spaces)
        normalized = re.sub(r'\s+', ' ', text)

        best_parser = None
        best_score = 0
        best_specificity = 0

        for parser, qualifiers in zip(self.parsers, self.qualifiers):
            min_matches = parser.get("qualifier", {}).get("min_matches", 1)
            hits = sum(1 for q in qualifiers if q.search(text) or q.search(normalized))

            if hits >= min_matches:
                # Score by number of hits, then by specificity (min_matches)
                if hits > best_score or (hits == best_score and min_matches > best_specificity):
                    best_parser = parser
                    best_score = hits
                    best_specificity = min_matches

        return best_parser

    def get_all_parsers(self) -> List[Dict]:
        """Get all loaded parsers."""
        self.load_all()
        return self.parsers


# Global parser cache
_parser_cache: Optional[ParserCache] = None


def get_parser_cache(parsers_dir: str = "parsers/") -> ParserCache:
    """Get or create the global parser cache."""
    global _parser_cache
    if _parser_cache is None or str(_parser_cache.parsers_dir) != parsers_dir:
        _parser_cache = ParserCache(parsers_dir)
    return _parser_cache


def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract text from PDF file."""
    text = ""
    with open(pdf_path, 'rb') as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text


def extract_text_per_page(pdf_path: str) -> List[str]:
    """Extract text from PDF file, returning text per page."""
    pages = []
    with open(pdf_path, 'rb') as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            page_text = page.extract_text() or ""
            pages.append(page_text)
    return pages


def parse_date(date_str: str) -> Optional[str]:
    """Parse date string in various formats to YYYY-MM-DD."""
    if not date_str:
        return None

    formats = [
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%m-%d-%Y",
        "%m/%d/%y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    return date_str.strip()


def extract_amount(value_str: str) -> float:
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


def normalize_text(text: str) -> str:
    """Normalize text by collapsing whitespace."""
    return re.sub(r'\s+', ' ', text).strip()


class YAMLParser:
    """Parser engine that executes YAML parser definitions."""

    def __init__(self, parser_def: Dict):
        self.parser = parser_def
        self.defaults = parser_def.get("defaults", {})
        self.debug_info: Dict[str, Any] = {
            "parser_used": parser_def.get("_source_file", "unknown"),
            "pattern_usage": {},
            "extraction_errors": [],
        }

    def _get_flags(self, pattern_def: Dict) -> int:
        """Get regex flags from pattern definition or defaults."""
        flags = 0

        flag_list = pattern_def.get("flags", self.defaults.get("flags", []))
        if isinstance(flag_list, str):
            flag_list = [flag_list]

        flag_map = {
            "IGNORECASE": re.IGNORECASE,
            "MULTILINE": re.MULTILINE,
            "DOTALL": re.DOTALL,
            "I": re.IGNORECASE,
            "M": re.MULTILINE,
            "S": re.DOTALL,
        }

        for flag_name in flag_list:
            if isinstance(flag_name, str):
                flags |= flag_map.get(flag_name.upper(), 0)

        return flags

    def _try_patterns(self, text: str, patterns: List, field_name: str) -> Optional[re.Match]:
        """Try a list of patterns and return the first match."""
        self.debug_info["pattern_usage"][field_name] = {
            "matched": False,
            "pattern_index": None,
            "matched_text": None,
            "patterns_skipped": [],
        }

        for i, pattern_def in enumerate(patterns):
            if isinstance(pattern_def, str):
                regex = pattern_def
                flags = self._get_flags({})
            elif isinstance(pattern_def, dict):
                regex = pattern_def.get("regex", "")
                flags = self._get_flags(pattern_def)
            else:
                continue

            if not regex:
                continue

            try:
                match = re.search(regex, text, flags)
                if match:
                    self.debug_info["pattern_usage"][field_name].update({
                        "matched": True,
                        "pattern_index": i,
                        "matched_text": match.group(0)[:100],  # Truncate for debug
                    })
                    return match
                else:
                    self.debug_info["pattern_usage"][field_name]["patterns_skipped"].append(regex[:50])
            except re.error as e:
                self.debug_info["extraction_errors"].append(f"{field_name}[{i}]: {e}")

        return None

    def _extract_field(self, text: str, field_def: Dict, field_name: str) -> Any:
        """Extract a single field value from text."""
        patterns = field_def.get("patterns", [])
        if not patterns:
            return None

        match = self._try_patterns(text, patterns, field_name)
        if not match:
            return None

        # Get the captured group (default to group 1)
        try:
            value = match.group(1)
        except IndexError:
            value = match.group(0)

        return value

    def _extract_employer(self, text: str) -> Optional[str]:
        """Extract employer name using employer_extraction config."""
        extraction = self.parser.get("employer_extraction", {})
        if not extraction:
            return None

        patterns = extraction.get("patterns", [])
        match = self._try_patterns(text, patterns, "employer_extraction")

        if not match:
            return None

        try:
            employer = match.group(1)
        except IndexError:
            employer = match.group(0)

        # Normalize if configured
        if extraction.get("normalize", False):
            employer = normalize_text(employer)

        return employer.strip() if employer else None

    def _extract_section(self, text: str, section_def: Dict, section_name: str) -> str:
        """Extract a section of text between start and end patterns."""
        start_patterns = section_def.get("start_patterns", [])
        end_patterns = section_def.get("end_patterns", [])

        if not start_patterns:
            return text

        # Find section start
        start_match = self._try_patterns(text, start_patterns, f"{section_name}_start")
        if not start_match:
            return ""

        section_text = text[start_match.start():]

        # Find section end
        if end_patterns:
            end_match = self._try_patterns(section_text, end_patterns, f"{section_name}_end")
            if end_match:
                section_text = section_text[:end_match.start()]

        return section_text

    def _extract_section_items(self, section_text: str, section_def: Dict, section_name: str) -> List[Dict]:
        """Extract items from a section using item patterns."""
        items = []
        item_patterns = section_def.get("item_patterns", [])

        # Normalize section text: collapse whitespace
        normalized = re.sub(r'\s+', ' ', section_text)

        # Remove header patterns defined in YAML (strip_headers)
        strip_headers = section_def.get("strip_headers", [])
        for header_def in strip_headers:
            if isinstance(header_def, str):
                hp = header_def
            elif isinstance(header_def, dict):
                hp = header_def.get("regex", "")
            else:
                continue

            if hp:
                normalized = re.sub(hp, '', normalized, flags=re.IGNORECASE)

        for pattern_def in item_patterns:
            if isinstance(pattern_def, str):
                regex = pattern_def
                groups = []
                flags = self._get_flags({})
            elif isinstance(pattern_def, dict):
                regex = pattern_def.get("regex", "")
                groups = pattern_def.get("groups", [])
                flags = self._get_flags(pattern_def)
            else:
                continue

            if not regex:
                continue

            try:
                for match in re.finditer(regex, normalized, flags):
                    item = {}

                    if groups:
                        # Use named groups from config
                        for group_def in groups:
                            name = group_def.get("name")
                            idx = group_def.get("index", 1)
                            try:
                                item[name] = match.group(idx)
                            except IndexError:
                                pass
                    else:
                        # Use all captured groups
                        item["groups"] = match.groups()

                    if item:
                        items.append(item)

            except re.error as e:
                self.debug_info["extraction_errors"].append(f"{section_name}_items: {e}")

        return items

    def _extract_taxes_structured(self, text: str, taxes_def: Dict) -> Dict:
        """Extract taxes with structured items (federal_income_tax, etc.)."""
        taxes = {
            "federal_income_tax": {"taxable_wages": 0.0, "current_withheld": 0.0, "ytd_withheld": 0.0},
            "social_security": {"taxable_wages": 0.0, "current_withheld": 0.0, "ytd_withheld": 0.0},
            "medicare": {"taxable_wages": 0.0, "current_withheld": 0.0, "ytd_withheld": 0.0},
        }

        # Extract section text first
        section_text = self._extract_section(text, taxes_def, "taxes")
        if not section_text:
            section_text = text  # Fall back to full text

        items = taxes_def.get("items", {})

        for tax_name, tax_def in items.items():
            patterns = tax_def.get("patterns", [])
            match = self._try_patterns(section_text, patterns, f"taxes.{tax_name}")

            if match:
                groups = tax_def.get("patterns", [{}])[0].get("groups", [])
                tax_data = {}

                for group_def in groups:
                    name = group_def.get("name")
                    idx = group_def.get("index", 1)
                    try:
                        tax_data[name] = extract_amount(match.group(idx))
                    except IndexError:
                        pass

                # Map to standard tax structure
                if tax_name in taxes:
                    taxes[tax_name].update(tax_data)

        return taxes

    def _extract_pay_summary(self, text: str, summary_def: Dict) -> Dict:
        """Extract pay summary section."""
        summary = {
            "current": {"gross": 0.0, "fit_taxable_wages": 0.0, "taxes": 0.0, "deductions": 0.0, "net_pay": 0.0},
            "ytd": {"gross": 0.0, "fit_taxable_wages": 0.0, "taxes": 0.0, "deductions": 0.0, "net_pay": 0.0},
        }

        # Extract section text
        section_text = self._extract_section(text, summary_def, "pay_summary")
        if not section_text:
            section_text = text

        patterns = summary_def.get("patterns", {})

        # Extract current values
        current_def = patterns.get("current", {})
        if isinstance(current_def, dict) and "regex" in current_def:
            match = self._try_patterns(section_text, [current_def], "pay_summary.current")
            if match:
                groups = current_def.get("groups", [])
                for group_def in groups:
                    name = group_def.get("name")
                    idx = group_def.get("index", 1)
                    try:
                        summary["current"][name] = extract_amount(match.group(idx))
                    except (IndexError, KeyError):
                        pass

        # Extract YTD values
        ytd_def = patterns.get("ytd", {})
        if isinstance(ytd_def, dict) and "regex" in ytd_def:
            match = self._try_patterns(section_text, [ytd_def], "pay_summary.ytd")
            if match:
                groups = ytd_def.get("groups", [])
                for group_def in groups:
                    name = group_def.get("name")
                    idx = group_def.get("index", 1)
                    try:
                        summary["ytd"][name] = extract_amount(match.group(idx))
                    except (IndexError, KeyError):
                        pass

        return summary

    def process_stub(self, text: str, pdf_name: str, employer_override: Optional[str] = None) -> Dict:
        """Process text as a pay stub and return standardized JSON."""
        fields = self.parser.get("fields", {})
        sections = self.parser.get("sections", {})

        # Extract employer
        employer = employer_override or self._extract_employer(text) or "Unknown Employer"

        # Extract basic fields
        pay_date_raw = self._extract_field(text, fields.get("pay_date", {}), "pay_date")
        pay_date = parse_date(pay_date_raw) if pay_date_raw else None

        period_start_raw = self._extract_field(text, fields.get("period_start", {}), "period_start")
        period_end_raw = self._extract_field(text, fields.get("period_end", {}), "period_end")

        document_id = self._extract_field(text, fields.get("document_id", {}), "document_id")

        net_pay_raw = self._extract_field(text, fields.get("net_pay", {}), "net_pay")
        net_pay = extract_amount(net_pay_raw) if net_pay_raw else 0.0

        # Extract sections
        earnings = []
        earnings_def = sections.get("earnings", {})
        if earnings_def:
            section_text = self._extract_section(text, earnings_def, "earnings")
            items = self._extract_section_items(section_text, earnings_def, "earnings")

            # Convert to standard earnings format
            for item in items:
                earning = {
                    "type": item.get("type", "").strip(),
                    "current_amount": extract_amount(item.get("current", "0")),
                    "ytd_amount": extract_amount(item.get("ytd", "0")),
                }
                if earning["type"] and (earning["current_amount"] != 0 or earning["ytd_amount"] != 0):
                    earnings.append(earning)

        # Extract taxes
        taxes_def = sections.get("taxes", {})
        if taxes_def and "items" in taxes_def:
            taxes = self._extract_taxes_structured(text, taxes_def)
        else:
            taxes = {
                "federal_income_tax": {"taxable_wages": 0.0, "current_withheld": 0.0, "ytd_withheld": 0.0},
                "social_security": {"taxable_wages": 0.0, "current_withheld": 0.0, "ytd_withheld": 0.0},
                "medicare": {"taxable_wages": 0.0, "current_withheld": 0.0, "ytd_withheld": 0.0},
            }

        # Extract deductions
        deductions = []
        deductions_def = sections.get("deductions", {})
        if deductions_def:
            section_text = self._extract_section(text, deductions_def, "deductions")
            items = self._extract_section_items(section_text, deductions_def, "deductions")

            for item in items:
                ded = {
                    "type": item.get("type", "").strip(),
                    "current_amount": extract_amount(item.get("employee_current", item.get("current", "0"))),
                    "ytd_amount": extract_amount(item.get("employee_ytd", item.get("ytd", "0"))),
                }

                employer_match_ytd = item.get("employer_ytd")
                if employer_match_ytd:
                    ded["employer_match_ytd"] = extract_amount(employer_match_ytd)

                if ded["type"] and (ded["current_amount"] != 0 or ded["ytd_amount"] != 0):
                    deductions.append(ded)

        # Extract pay summary
        pay_summary_def = sections.get("pay_summary", {})
        if pay_summary_def:
            pay_summary = self._extract_pay_summary(text, pay_summary_def)
        else:
            pay_summary = {
                "current": {"gross": 0.0, "fit_taxable_wages": 0.0, "taxes": 0.0, "deductions": 0.0, "net_pay": 0.0},
                "ytd": {"gross": 0.0, "fit_taxable_wages": 0.0, "taxes": 0.0, "deductions": 0.0, "net_pay": 0.0},
            }

        # Use pay_summary net_pay if we couldn't extract it separately
        if net_pay == 0.0 and pay_summary["current"]["net_pay"] != 0.0:
            net_pay = pay_summary["current"]["net_pay"]

        return {
            "file_name": pdf_name,
            "employer": employer,
            "pay_date": pay_date,
            "period": {
                "start": parse_date(period_start_raw) if period_start_raw else None,
                "end": parse_date(period_end_raw) if period_end_raw else None,
            },
            "document_id": document_id,
            "net_pay": net_pay,
            "earnings": earnings,
            "taxes": taxes,
            "deductions": deductions,
            "pay_summary": pay_summary,
        }

    def process_w2(self, text: str, pdf_name: str, employer_override: Optional[str] = None) -> Dict:
        """Process text as a W-2 form and return standardized JSON."""
        fields = self.parser.get("fields", {})

        # Extract employer
        employer = employer_override or self._extract_employer(text) or "Unknown Employer"

        # Extract W-2 specific fields
        data = {}

        w2_fields = [
            ("wages_tips_other_comp", "wages_tips_other_comp"),
            ("federal_income_tax_withheld", "federal_income_tax_withheld"),
            ("social_security_wages", "social_security_wages"),
            ("social_security_tax_withheld", "social_security_tax_withheld"),
            ("medicare_wages_and_tips", "medicare_wages_and_tips"),
            ("medicare_tax_withheld", "medicare_tax_withheld"),
        ]

        for field_name, output_name in w2_fields:
            field_def = fields.get(field_name, {})
            if field_def:
                value = self._extract_field(text, field_def, field_name)
                if value:
                    data[output_name] = extract_amount(value)

        return {
            "employer": employer,
            "source_type": "pdf",
            "source_file": pdf_name,
            "data": data,
        }

    def process(self, pdf_path: str, employer_override: Optional[str] = None) -> Dict:
        """Process a PDF file using this parser."""
        text = extract_text_from_pdf(pdf_path)

        if not text.strip():
            raise ValueError(f"Could not extract text from {pdf_path}")

        pdf_name = Path(pdf_path).name
        doc_type = self.parser.get("type", "stub")

        if doc_type == "w2":
            return self.process_w2(text, pdf_name, employer_override)
        else:
            return self.process_stub(text, pdf_name, employer_override)

    def get_debug_info(self) -> Dict:
        """Get debug information about pattern usage."""
        return self.debug_info


class YAMLProcessor:
    """Processor that uses YAML parser definitions."""

    def __init__(self, parsers_dir: str = "parsers/"):
        self.cache = get_parser_cache(parsers_dir)

    @staticmethod
    def process(pdf_path: str, employer_name: str, parsers_dir: str = "parsers/") -> Dict:
        """
        Process a PDF using YAML parser definitions.

        This is the main entry point matching the existing processor interface.

        Args:
            pdf_path: Path to PDF file
            employer_name: Name of employer (for output, not matching)
            parsers_dir: Directory containing YAML parser definitions

        Returns:
            dict: Standardized document data structure
        """
        cache = get_parser_cache(parsers_dir)
        text = extract_text_from_pdf(pdf_path)

        if not text.strip():
            raise ValueError(f"Could not extract text from {pdf_path}")

        # Find matching parser
        parser_def = cache.find_matching_parser(text)

        if not parser_def:
            raise ValueError(f"No parser matched for {pdf_path}")

        # Process using the matched parser
        parser = YAMLParser(parser_def)
        return parser.process(pdf_path, employer_name)

    def find_parser_for_text(self, text: str) -> Optional[Dict]:
        """Find a parser that matches the given text."""
        return self.cache.find_matching_parser(text)

    def process_with_parser(self, pdf_path: str, parser_def: Dict, employer_name: str) -> Dict:
        """Process a PDF using a specific parser definition."""
        parser = YAMLParser(parser_def)
        return parser.process(pdf_path, employer_name)


# Convenience function matching existing processor interface
def process(pdf_path: str, employer_name: str) -> Dict:
    """Process a PDF using YAML parser definitions."""
    return YAMLProcessor.process(pdf_path, employer_name)
