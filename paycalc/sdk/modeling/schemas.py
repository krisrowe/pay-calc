"""Modeling-specific schemas.

These schemas are used by the modeling layer and depend on core schemas
from paycalc.sdk.schemas.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..schemas import FicaRoundingBalance, PaySummary


class RetirementElectionItem(BaseModel):
    """A single retirement contribution election (e.g., pretax 401k)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["pretax", "roth", "after_tax"] = Field(
        ..., description="Type of contribution"
    )
    amount: float = Field(..., ge=0, description="Contribution amount or percentage")
    amount_type: Literal["percentage", "absolute"] = Field(
        ..., description="Whether amount is percentage of gross or absolute dollars"
    )

    @field_validator("amount")
    @classmethod
    def amount_max_two_decimals(cls, v: float) -> float:
        """Ensure amount has at most 2 decimal places."""
        rounded = round(v, 2)
        if abs(v - rounded) > 1e-9:
            raise ValueError(f"amount must have at most 2 decimal places, got {v}")
        return rounded


class RetirementElectionChange(BaseModel):
    """A change in retirement elections effective on a date."""

    model_config = ConfigDict(extra="forbid")

    date: str = Field(..., description="Effective date (YYYY-MM-DD)")
    regular: Optional[RetirementElectionItem] = Field(
        None, description="Election for regular pay periods"
    )
    bonus: Optional[RetirementElectionItem] = Field(
        None, description="Election for bonus/supplemental pay"
    )


class RetirementElectionHistory(BaseModel):
    """History of retirement election changes."""

    model_config = ConfigDict(extra="forbid")

    changes: List[RetirementElectionChange] = Field(
        default_factory=list, description="Election changes in chronological order"
    )

    @classmethod
    def regular_for_year(
        cls,
        year: int,
        amount: float,
        amount_type: Literal["percentage", "absolute"],
        contrib_type: Literal["pretax", "roth", "after_tax"] = "pretax",
    ) -> "RetirementElectionHistory":
        """Create elections with a single regular-pay election for the full year.

        Args:
            year: Tax year (e.g., 2025)
            amount: Contribution amount or percentage
            amount_type: "percentage" (0.10 = 10%) or "absolute" ($500)
            contrib_type: Type of contribution (default: pretax)

        Returns:
            RetirementElectionHistory with one change effective Jan 1
        """
        return cls(
            changes=[
                RetirementElectionChange(
                    date=f"{year}-01-01",
                    regular=RetirementElectionItem(
                        type=contrib_type,
                        amount=amount,
                        amount_type=amount_type,
                    ),
                )
            ]
        )


class ModelResult(BaseModel):
    """Output of model_stub.

    Contains computed pay summaries for current period and YTD,
    plus modeling state (fica_balance) and diagnostic info (warnings).

    The current and ytd fields use PaySummary, the same schema used by
    PayStub records, enabling direct comparison during validation.
    """

    model_config = ConfigDict(extra="forbid")

    current: PaySummary = Field(..., description="Current period computed amounts")
    ytd: PaySummary = Field(..., description="Year-to-date computed amounts")
    fica_balance: FicaRoundingBalance = Field(
        ..., description="FICA rounding balance to pass to next period"
    )
    warnings: List[str] = Field(
        default_factory=list, description="Diagnostic warnings (caps reached, etc.)"
    )
