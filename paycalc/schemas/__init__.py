"""JSON Schema validation for pay-calc records."""

import json
from pathlib import Path
from typing import Dict, Any, List, Tuple

# Try to import jsonschema, fall back gracefully
try:
    import jsonschema
    from jsonschema import Draft202012Validator
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False
    Draft202012Validator = None


def _load_schema(schema_name: str) -> Dict[str, Any]:
    """Load a JSON schema by name."""
    schema_path = Path(__file__).parent / f"{schema_name}.json"
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema not found: {schema_path}")
    with open(schema_path) as f:
        return json.load(f)


def validate_stub(data: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Validate stub data against canonical JSON schema.

    Args:
        data: The stub data dict (not the full record with meta)

    Returns:
        Tuple of (errors, warnings). Errors block import.
    """
    if not HAS_JSONSCHEMA:
        return [], ["jsonschema not installed, skipping schema validation"]

    errors = []
    warnings = []

    try:
        schema = _load_schema("stub")
        validator = Draft202012Validator(schema)

        for error in validator.iter_errors(data):
            # Build a readable path
            path = ".".join(str(p) for p in error.absolute_path) if error.absolute_path else "root"

            # Check for the forbidden field names (these are critical errors)
            if "federal_income" in str(error.message) or "'current'" in str(error.message) or "'ytd'" in str(error.message):
                errors.append(f"SCHEMA VIOLATION at {path}: {error.message}")
            else:
                errors.append(f"Schema error at {path}: {error.message}")

    except Exception as e:
        warnings.append(f"Schema validation failed: {e}")

    return errors, warnings


def validate_record_schema(record_type: str, data: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Validate record data against its JSON schema.

    Args:
        record_type: "stub" or "w2"
        data: The record data dict

    Returns:
        Tuple of (errors, warnings)
    """
    if record_type == "stub":
        return validate_stub(data)
    # TODO: Add W-2 schema validation
    return [], []
