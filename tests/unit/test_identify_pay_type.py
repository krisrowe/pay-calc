"""Unit tests for identify_pay_type function.

Tests use synthetic stub data - no external dependencies.
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from paycalc.sdk.analysis import identify_pay_type


def make_stub(earnings: list[dict]) -> dict:
    """Create minimal stub structure for testing."""
    return {"earnings": earnings}


def make_earning(etype: str, current: float = 100.0) -> dict:
    """Create earning entry."""
    return {"type": etype, "current_amount": current}


class TestIdentifyPayType:
    """Test identify_pay_type classification logic."""

    def test_regular_pay(self):
        stub = make_stub([make_earning("Regular Pay")])
        assert identify_pay_type(stub) == "regular"

    def test_stock_grant(self):
        stub = make_stub([make_earning("Stock Units")])
        assert identify_pay_type(stub) == "stock_grant"

    def test_rsu_grant(self):
        stub = make_stub([make_earning("RSU Vesting")])
        assert identify_pay_type(stub) == "stock_grant"

    def test_annual_bonus(self):
        stub = make_stub([make_earning("Annual Bonus")])
        assert identify_pay_type(stub) == "annual_bonus"

    def test_quarterly_bonus(self):
        stub = make_stub([make_earning("Quarterly Bonus")])
        assert identify_pay_type(stub) == "quarterly_bonus"

    def test_performance_bonus(self):
        stub = make_stub([make_earning("Performance Bonus")])
        assert identify_pay_type(stub) == "performance_bonus"

    def test_generic_bonus(self):
        """Bonus without prefix returns 'bonus'."""
        stub = make_stub([make_earning("Bonus")])
        assert identify_pay_type(stub) == "bonus"

    def test_zero_amount_ignored(self):
        """Earnings with zero amount are skipped."""
        stub = make_stub([
            make_earning("Annual Bonus", current=0),
            make_earning("Regular Pay", current=100),
        ])
        assert identify_pay_type(stub) == "regular"

    def test_first_nonzero_wins(self):
        """First non-zero earning determines type."""
        stub = make_stub([
            make_earning("Stock Units", current=500),
            make_earning("Regular Pay", current=100),
        ])
        assert identify_pay_type(stub) == "stock_grant"

    def test_empty_earnings_returns_other(self):
        """Empty earnings list defaults to other."""
        stub = make_stub([])
        assert identify_pay_type(stub) == "other"

    def test_no_earnings_key_returns_other(self):
        """Missing earnings key defaults to other."""
        stub = {}
        assert identify_pay_type(stub) == "other"

    def test_case_insensitive(self):
        """Matching is case-insensitive."""
        stub = make_stub([make_earning("ANNUAL BONUS")])
        assert identify_pay_type(stub) == "annual_bonus"

    def test_multi_word_prefix(self):
        """Multi-word prefix before bonus."""
        stub = make_stub([make_earning("Year End Bonus")])
        assert identify_pay_type(stub) == "year_end_bonus"
