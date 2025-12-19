"""
Pay stub processors using YAML-driven parser definitions.

Parsers auto-detect document format from content patterns.
No employer-specific code needed.
"""

from .engine import YAMLProcessor


def get_processor(processor_name=None):
    """
    Get processor for PDF extraction.

    Args:
        processor_name: Ignored. Kept for backward compatibility.
                       Parser selection is automatic based on content.

    Returns:
        YAMLProcessor class
    """
    return YAMLProcessor
