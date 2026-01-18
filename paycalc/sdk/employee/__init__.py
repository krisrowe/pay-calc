"""employee - Party-specific data access and configuration.

Scope:
- Pay stub retrieval with canonical deduction types (records.py)
- W-2 retrieval (records.py)
- Compensation plan resolution by effective date (comp_plan.py)
- Benefits/deductions resolution (benefits.py)
- W-4 configuration resolution (w4.py)
- Party profile configuration (config.py)

Constraints:
- All functions take a 'party' identifier ('him' or 'her')
- Returns canonical data ready for modeling - no downstream translation needed
- Accesses records database and profile.yaml for party-specific data

Usage:
    from paycalc.sdk.employee import get_pay_stub, resolve_comp_plan

    stub = get_pay_stub("abc12345")  # Returns PayStub with canonical types
    comp = resolve_comp_plan("him", date(2025, 3, 14))
"""

# Records access - canonical pay stubs and W-2s
from .records import get_pay_stub, get_w2

# Configuration resolution
from .comp_plan import resolve_comp_plan, calc_period_number, calc_401k_for_period
from .benefits import resolve_benefits
from .w4 import resolve_w4
from .config import derive_w4_from_stub

__all__ = [
    # Records
    "get_pay_stub",
    "get_w2",
    # Comp plan
    "resolve_comp_plan",
    "calc_period_number",
    "calc_401k_for_period",
    # Benefits
    "resolve_benefits",
    # W-4
    "resolve_w4",
    # Config
    "derive_w4_from_stub",
]
