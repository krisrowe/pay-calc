"""Pydantic schemas for pay-calc data validation.

All schemas use extra='forbid' to reject unknown fields, ensuring
typos in config files cause clear errors rather than silent ignoring.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# =============================================================================
# Core Pay Schemas - Shared by records and modeling
# =============================================================================


class TaxAmounts(BaseModel):
    """Amounts per tax type (taxable wages or withholding)."""

    model_config = ConfigDict(extra="forbid")

    fit: float = Field(..., description="Federal income tax amount")
    ss: float = Field(..., description="Social Security amount")
    medicare: float = Field(..., description="Medicare amount")


class DeductionTotals(BaseModel):
    """Deduction totals by tax treatment."""

    model_config = ConfigDict(extra="forbid")

    fully_pretax: float = Field(
        default=0, ge=0,
        description=(
            "Section 125 cafeteria plan deductions (health, dental, vision, FSA, HSA). "
            "Not subject to FIT or FICA - reduces both federal income tax and "
            "Social Security/Medicare taxable wages."
        ),
    )
    retirement: float = Field(
        default=0, ge=0,
        description=(
            "Traditional retirement contributions (401k, 403b, 457b). "
            "Pre-FIT: deferred from federal income tax until withdrawal. "
            "NOT pre-FICA: Social Security and Medicare taxes are paid when earned."
        ),
    )
    post_tax: float = Field(
        default=0, ge=0,
        description=(
            "After-tax deductions that reduce neither FIT nor FICA taxable wages. "
            "Examples: Roth 401k, after-tax 401k (mega backdoor Roth), "
            "voluntary life insurance, charitable contributions."
        ),
    )

    @property
    def total(self) -> float:
        """Total of all deductions."""
        return self.fully_pretax + self.retirement + self.post_tax


class PaySummary(BaseModel):
    """Tax-relevant pay totals. Internally coherent.

    Used for both current period and YTD. Shared by PayStub (records)
    and ModelResult (modeling) for direct comparison.
    """

    model_config = ConfigDict(extra="forbid")

    gross: float = Field(..., ge=0, description="Gross wages")
    deductions: DeductionTotals = Field(..., description="Deductions by tax treatment")
    taxable: TaxAmounts = Field(..., description="Taxable wage bases")
    withheld: TaxAmounts = Field(..., description="Tax withholding amounts")
    net_pay: float = Field(default=0, ge=0, description="Net pay")

    @model_validator(mode="after")
    def check_coherence(self) -> "PaySummary":
        """Validate internal consistency of amounts."""
        errors = []
        tolerance = 1.00  # Allow $1 tolerance for rounding

        # FIT taxable = gross - fully_pretax - retirement
        expected_fit = self.gross - self.deductions.fully_pretax - self.deductions.retirement
        if abs(self.taxable.fit - expected_fit) > tolerance:
            errors.append(
                f"taxable.fit ({self.taxable.fit:.2f}) != "
                f"gross - fully_pretax - retirement ({expected_fit:.2f})"
            )

        # Medicare taxable = gross - fully_pretax (no SS cap)
        expected_medicare = self.gross - self.deductions.fully_pretax
        if abs(self.taxable.medicare - expected_medicare) > tolerance:
            errors.append(
                f"taxable.medicare ({self.taxable.medicare:.2f}) != "
                f"gross - fully_pretax ({expected_medicare:.2f})"
            )

        # SS taxable = gross - fully_pretax (but capped, so only check upper bound)
        if self.taxable.ss > expected_medicare + tolerance:
            errors.append(
                f"taxable.ss ({self.taxable.ss:.2f}) > "
                f"gross - fully_pretax ({expected_medicare:.2f})"
            )

        # Net pay = gross - all deductions - all withholding
        total_withheld = self.withheld.fit + self.withheld.ss + self.withheld.medicare
        expected_net = self.gross - self.deductions.total - total_withheld
        if abs(self.net_pay - expected_net) > tolerance:
            errors.append(
                f"net_pay ({self.net_pay:.2f}) != "
                f"gross - deductions - withholding ({expected_net:.2f})"
            )

        if errors:
            raise ValueError("; ".join(errors))

        return self

    @classmethod
    def zero(cls) -> "PaySummary":
        """Create a zero PaySummary (for period 1 or initialization)."""
        return cls(
            gross=0,
            deductions=DeductionTotals(fully_pretax=0, retirement=0, post_tax=0),
            taxable=TaxAmounts(fit=0, ss=0, medicare=0),
            withheld=TaxAmounts(fit=0, ss=0, medicare=0),
            net_pay=0,
        )


class PayLineItem(BaseModel):
    """Single earning or deduction line item."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., description="Line item type (e.g., 'Regular Pay', '401k')")
    current: float = Field(..., ge=0, description="Current period amount")
    ytd: float = Field(default=0, ge=0, description="Year-to-date amount")


class PayStub(BaseModel):
    """Full pay stub record.

    Contains source line items (earnings, deductions) and derived summaries
    (current, ytd). The summaries use PaySummary which is also used by
    modeling, enabling direct comparison.
    """

    model_config = ConfigDict(extra="forbid")

    party: str = Field(..., description="Party identifier (e.g., 'him', 'her')")
    employer: str = Field(..., description="Employer name")
    pay_date: str = Field(..., description="Pay date (YYYY-MM-DD)")
    period_start: str = Field(..., description="Period start date (YYYY-MM-DD)")
    period_end: str = Field(..., description="Period end date (YYYY-MM-DD)")
    earnings: List[PayLineItem] = Field(..., description="Earnings line items")
    deductions: List[PayLineItem] = Field(default_factory=list, description="Deduction line items")
    current: PaySummary = Field(..., description="Current period summary")
    ytd: PaySummary = Field(..., description="Year-to-date summary")


# =============================================================================
# FICA Rounding Balance - For stub modeling
# =============================================================================


class FicaRoundingBalance(BaseModel):
    """FICA rounding remainder for stub modeling.

    Payroll systems track fractional cents across pay periods to minimize
    cumulative rounding error. Without knowing the prior remainder, modeled
    FICA values may differ from actual by up to 1 cent.

    This is a known IRS phenomenon - employers report the cumulative
    fractions-of-cents adjustment on Form 941 (quarterly) line 7 or
    Form 944 (annually) line 6.

    Note: FICA balance does NOT carry over year-to-year. Each calendar
    year starts fresh with zero balance.

    Use classmethods to construct:
        FicaRoundingBalance.none()  - explicit zeros, no auto-adjust
        FicaRoundingBalance.auto()  - zeros, try combinations to find match
    """

    model_config = ConfigDict(extra="forbid")

    ss: float = Field(gt=-0.005, lt=0.005, description="SS remainder from prior periods")
    medicare: float = Field(gt=-0.005, lt=0.005, description="Medicare remainder from prior periods")
    auto_adjust: bool = Field(
        default=False,
        description="Allow auto-adjustment to find matching rounding balance",
    )

    @classmethod
    def none(cls) -> "FicaRoundingBalance":
        """Zero remainder, no auto-adjust (explicit mode)."""
        return cls(ss=0.0, medicare=0.0, auto_adjust=False)

    @classmethod
    def auto(cls) -> "FicaRoundingBalance":
        """Zero remainder, auto-adjust to find match."""
        return cls(ss=0.0, medicare=0.0, auto_adjust=True)


# =============================================================================
# Override Schemas - For CLI and config file validation
# =============================================================================


class CompPlanOverride(BaseModel):
    """Compensation plan override schema."""

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


class W4Override(BaseModel):
    """W-4 settings override schema (2020+ form fields)."""

    model_config = ConfigDict(extra="forbid")

    filing_status: Literal["single", "mfj", "hoh"] = Field(
        default="single", description="Filing status"
    )
    step2c_multiple_jobs: bool = Field(
        default=False, description="Step 2(c): Multiple jobs checkbox"
    )
    step3_dependents: float = Field(
        default=0, ge=0, description="Step 3: Dependent credits"
    )
    step4a_other_income: float = Field(
        default=0, ge=0, description="Step 4(a): Other income"
    )
    step4b_deductions: float = Field(
        default=0, ge=0, description="Step 4(b): Itemized deductions"
    )
    step4c_extra_withholding: float = Field(
        default=0, ge=0, description="Step 4(c): Extra withholding per period"
    )


# =============================================================================
# Legacy Schemas - For backward compatibility during migration
# =============================================================================


class BenefitsOverride(BaseModel):
    """Benefits override schema (legacy - individual benefit fields)."""

    model_config = ConfigDict(extra="forbid")

    pretax_health: Optional[float] = Field(default=None, ge=0)
    pretax_medical: Optional[float] = Field(default=None, ge=0)
    pretax_dental: Optional[float] = Field(default=None, ge=0)
    pretax_vision: Optional[float] = Field(default=None, ge=0)
    pretax_fsa: Optional[float] = Field(default=None, ge=0)
    pretax_hsa: Optional[float] = Field(default=None, ge=0)
    pretax_life: Optional[float] = Field(default=None, ge=0)
    pretax_disability: Optional[float] = Field(default=None, ge=0)
    pretax_imputed: Optional[float] = Field(default=None, ge=0)
    pretax_other: Optional[float] = Field(default=None, ge=0)


class Benefits(BaseModel):
    """Benefits schema (legacy - required input for model_stub)."""

    model_config = ConfigDict(extra="forbid")

    pretax_health: Optional[float] = Field(default=None, ge=0)
    pretax_medical: Optional[float] = Field(default=None, ge=0)
    pretax_dental: Optional[float] = Field(default=None, ge=0)
    pretax_vision: Optional[float] = Field(default=None, ge=0)
    pretax_fsa: Optional[float] = Field(default=None, ge=0)
    pretax_hsa: Optional[float] = Field(default=None, ge=0)
    pretax_life: Optional[float] = Field(default=None, ge=0)
    pretax_disability: Optional[float] = Field(default=None, ge=0)
    pretax_imputed: Optional[float] = Field(default=None, ge=0)
    pretax_other: Optional[float] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def at_least_one_benefit(self) -> "Benefits":
        """Ensure at least one benefit field is explicitly set."""
        values = self.model_dump(exclude_none=True)
        if not values:
            raise ValueError(
                "At least one benefit field must be provided (can be zero). "
                "Use pretax_health=0 if no benefits."
            )
        return self


class PriorYtd(BaseModel):
    """Prior YTD values schema (legacy - for model_stub input)."""

    model_config = ConfigDict(extra="forbid")

    gross: float = Field(..., ge=0)
    fit_taxable: float = Field(..., ge=0)
    fit_withheld: float = Field(..., ge=0)
    ss_wages: float = Field(..., ge=0)
    ss_withheld: float = Field(..., ge=0)
    medicare_wages: float = Field(..., ge=0)
    medicare_withheld: float = Field(..., ge=0)
    pretax_401k: float = Field(default=0, ge=0)


# =============================================================================
# Validation Functions
# =============================================================================


def validate_comp_plan_override(data: dict) -> dict:
    """Validate comp plan override data."""
    validated = CompPlanOverride.model_validate(data)
    return validated.model_dump(exclude_none=True)


def validate_benefits_override(data: dict) -> dict:
    """Validate benefits override data."""
    validated = BenefitsOverride.model_validate(data)
    return validated.model_dump(exclude_none=True)


def validate_w4_override(data: dict) -> dict:
    """Validate W-4 override data."""
    validated = W4Override.model_validate(data)
    return validated.model_dump(exclude_none=True)


def validate_prior_ytd(data: dict) -> dict:
    """Validate prior YTD data."""
    validated = PriorYtd.model_validate(data)
    return validated.model_dump()
