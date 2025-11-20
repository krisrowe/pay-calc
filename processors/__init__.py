"""
Pay stub processors for different employer formats.

Each processor handles a specific pay stub format and returns
a standardized JSON structure.
"""

from .employer_a import Employer AProcessor
from .employer_b import EBProcessor

# Registry of processors by name
PROCESSORS = {
    "employer_a": Employer AProcessor,
    "google_llc": Employer AProcessor,  # Legacy alias
    "google_public_sector": Employer AProcessor,  # Legacy alias
    "employer_b": EBProcessor,
    "generic": Employer AProcessor,  # Default to Employer A format
}


def get_processor(processor_name):
    """Get processor class by name."""
    return PROCESSORS.get(processor_name, PROCESSORS["generic"])

