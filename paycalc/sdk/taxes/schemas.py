"""Pydantic schemas for tax rules validation.

These schemas validate the tax-rules/*.yaml files and provide typed access
to tax parameters like SS wage cap, 401k limits, and tax brackets.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class TaxBracket(BaseModel):
    """Single tax bracket entry."""
    model_config = ConfigDict(extra="forbid")

    up_to: Optional[float] = Field(default=None, description="Upper bound (None if 'over' bracket)")
    over: Optional[float] = Field(default=None, description="Lower bound for top bracket")
    rate: float = Field(..., ge=0, le=1, description="Tax rate as decimal")


class FilingStatusRules(BaseModel):
    """Tax rules for a filing status (MFJ, single, etc.)."""
    model_config = ConfigDict(extra="forbid")

    standard_deduction: float = Field(..., ge=0)
    tax_brackets: list[TaxBracket]
    capital_gains_brackets: Optional[list[TaxBracket]] = None


class SocialSecurityRules(BaseModel):
    """Social Security tax rules."""
    model_config = ConfigDict(extra="forbid")

    wage_cap: float = Field(..., gt=0, description="SS wage base (max taxable)")
    tax_rate: float = Field(..., ge=0, le=1, description="SS tax rate (employee portion)")


class Retirement401kRules(BaseModel):
    """401(k) contribution limits."""
    model_config = ConfigDict(extra="forbid")

    employee_elective_limit: float = Field(..., ge=0, description="Pre-tax + Roth employee limit")
    total_annual_limit: float = Field(..., ge=0, description="Total including employer match")


class ChildCareRules(BaseModel):
    """Child and dependent care credit rules."""
    model_config = ConfigDict(extra="forbid")

    credit_rate: float = Field(..., ge=0, le=1)
    max_expenses_one_child: float = Field(..., ge=0)
    max_expenses_two_plus: float = Field(..., ge=0)


class TaxRules(BaseModel):
    """Complete tax rules for a year."""
    model_config = ConfigDict(extra="ignore")  # Allow unknown fields for forward compat

    mfj: FilingStatusRules
    social_security: SocialSecurityRules
    additional_medicare_tax_threshold: float = Field(..., ge=0)
    additional_medicare_withholding_threshold: float = Field(..., ge=0)
    retirement_401k: Retirement401kRules = Field(..., alias="401k")
    # Optional sections
    single: Optional[FilingStatusRules] = None
    hoh: Optional[FilingStatusRules] = None
    child_care: Optional[ChildCareRules] = None
