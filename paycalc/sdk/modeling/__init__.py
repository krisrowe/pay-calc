"""modeling - Pay stub modeling and validation.

Scope:
- Model hypothetical pay stubs from comp plan, benefits, W-4 (stub_modeler.py)
- Validate modeled values against real stubs (validate.py)
- Sequence modeling with FICA rounding balance tracking

Validation Philosophy:
    Validation compares modeled values against actual stub values. Discrepancies
    may indicate model errors, extraction errors, or payroll quirks. The validation
    is intentionally neutral about which side is "correct" - it reports differences
    for investigation. Early in the project, the stub is typically treated as the
    control; later, validation might verify extraction logic or payroll accuracy.

Constraints:
- Uses employee/ for party-specific config resolution
- Uses taxes/ for withholding calculations
- Validation uses employee.records for typed stub access

Usage:
    from paycalc.sdk.modeling import validate_stub, validate_stub_in_sequence
    from paycalc.sdk.schemas import FicaRoundingBalance

    # Non-iterative (single stub, self-contained)
    result = validate_stub("abc12345", FicaRoundingBalance.none())

    # Iterative (models sequence, more accurate)
    result = validate_stub_in_sequence("abc12345")
"""

from .validate import (
    validate_stub,
    validate_stub_in_sequence,
    extract_inputs_from_stub,
    is_supplemental_stub,
)

from .stub_modeler import (
    model_stub,
    model_stubs_in_sequence,
    get_first_regular_pay_date,
    model_regular_401k_contribs,
    model_401k_max_frontload,
    model_401k_max_spread_evenly,
)

from .schemas import ModelResult

__all__ = [
    # Schemas
    "ModelResult",
    # Validation
    "validate_stub",
    "validate_stub_in_sequence",
    "extract_inputs_from_stub",
    "is_supplemental_stub",
    # Modeling
    "model_stub",
    "model_stubs_in_sequence",
    "get_first_regular_pay_date",
    "model_regular_401k_contribs",
    "model_401k_max_frontload",
    "model_401k_max_spread_evenly",
]

