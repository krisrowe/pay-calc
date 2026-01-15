"""Pydantic schemas for strict validation of override inputs.

All schemas use extra='forbid' to reject unknown fields, ensuring
typos in config files cause clear errors rather than silent ignoring.
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CompPlanOverride(BaseModel):
    """Compensation plan override schema.

    Used for --comp-plan JSON file validation.
    """

    model_config = ConfigDict(extra="forbid")

    gross_per_period: float = Field(..., gt=0, description="Gross pay per period")
    pay_frequency: Literal["weekly", "biweekly", "semimonthly", "monthly"] = Field(
        default="biweekly", description="Pay frequency"
    )
    target_401k_pct: Optional[float] = Field(
        default=None, ge=0, le=1, description="Target 401k as percentage of gross (0-1)"
    )
    target_401k_amount: Optional[float] = Field(
        default=None, ge=0, description="Target 401k as fixed dollar amount per period"
    )


class BenefitsOverride(BaseModel):
    """Benefits/deductions override schema.

    Used for --benefits JSON file validation.
    All fields are optional since benefits vary by employer.
    Field names must start with 'pretax_' prefix.
    """

    model_config = ConfigDict(extra="forbid")

    pretax_health: Optional[float] = Field(default=None, ge=0, description="Health insurance")
    pretax_medical: Optional[float] = Field(default=None, ge=0, description="Medical insurance (alias for health)")
    pretax_dental: Optional[float] = Field(default=None, ge=0, description="Dental insurance")
    pretax_vision: Optional[float] = Field(default=None, ge=0, description="Vision insurance")
    pretax_fsa: Optional[float] = Field(default=None, ge=0, description="FSA contribution")
    pretax_hsa: Optional[float] = Field(default=None, ge=0, description="HSA contribution")
    pretax_life: Optional[float] = Field(default=None, ge=0, description="Life insurance")
    pretax_disability: Optional[float] = Field(default=None, ge=0, description="Disability insurance")
    pretax_imputed: Optional[float] = Field(default=None, ge=0, description="Imputed income offset (e.g., GTL)")
    pretax_other: Optional[float] = Field(default=None, ge=0, description="Other pretax deductions")


class W4Override(BaseModel):
    """W-4 settings override schema.

    Used for --w4 JSON file validation.
    Based on 2020+ W-4 form fields.
    """

    model_config = ConfigDict(extra="forbid")

    filing_status: Literal["single", "mfj", "hoh"] = Field(
        default="single", description="Filing status: single, mfj (married filing jointly), hoh (head of household)"
    )
    step2c_multiple_jobs: bool = Field(
        default=False, description="Step 2(c): Check if multiple jobs or spouse works"
    )
    step3_dependents: float = Field(
        default=0, ge=0, description="Step 3: Total dependent credits (dollar amount)"
    )
    step4a_other_income: float = Field(
        default=0, ge=0, description="Step 4(a): Other income not from jobs"
    )
    step4b_deductions: float = Field(
        default=0, ge=0, description="Step 4(b): Deductions beyond standard deduction"
    )
    step4c_extra_withholding: float = Field(
        default=0, ge=0, description="Step 4(c): Extra withholding per period"
    )


def validate_comp_plan_override(data: dict) -> dict:
    """Validate comp plan override data.

    Args:
        data: Raw dict from JSON file

    Returns:
        Validated dict

    Raises:
        pydantic.ValidationError: If validation fails (unknown fields, invalid values)
    """
    validated = CompPlanOverride.model_validate(data)
    return validated.model_dump(exclude_none=True)


def validate_benefits_override(data: dict) -> dict:
    """Validate benefits override data.

    Args:
        data: Raw dict from JSON file

    Returns:
        Validated dict

    Raises:
        pydantic.ValidationError: If validation fails
    """
    validated = BenefitsOverride.model_validate(data)
    return validated.model_dump(exclude_none=True)


def validate_w4_override(data: dict) -> dict:
    """Validate W-4 override data.

    Args:
        data: Raw dict from JSON file

    Returns:
        Validated dict

    Raises:
        pydantic.ValidationError: If validation fails
    """
    validated = W4Override.model_validate(data)
    return validated.model_dump(exclude_none=True)
