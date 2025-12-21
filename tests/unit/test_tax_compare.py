"""Unit tests for tax compare summary logic.

Tests the 7 variance scenarios for the compare_1040 summary output:
1. No variance (match)
2. Greater refund (favorable)
3. Lesser refund (unfavorable)
4. Refund becomes owing (unfavorable)
5. Owing becomes refund (favorable)
6. Owing becomes lesser (favorable)
7. Owing becomes greater (unfavorable)
"""

import pytest


def build_summary(actual_refund: int, actual_owed: int, calc_refund: int, calc_owed: int) -> dict:
    """Build a summary dict using the same logic as compare_1040.

    This mirrors the logic in paycalc/sdk/tax.py compare_1040() function.
    """
    calc_net = calc_refund - calc_owed
    actual_net = actual_refund - actual_owed
    variance_amount = actual_net - calc_net  # positive = favorable

    # Determine captions based on sign (always show positive amounts)
    if actual_net >= 0:
        actual_caption = "Actual refund"
        actual_display = actual_net
    else:
        actual_caption = "Actual owed"
        actual_display = abs(actual_net)

    if calc_net >= 0:
        calc_caption = "Calculated refund"
        calc_display = calc_net
    else:
        calc_caption = "Calculated owed"
        calc_display = abs(calc_net)

    # Variance favorable: positive = good, negative = bad, zero = neutral (None)
    if variance_amount > 0:
        favorable = True
    elif variance_amount < 0:
        favorable = False
    else:
        favorable = None

    return {
        "status": "match" if abs(variance_amount) <= 1 else "gap",
        "amounts": [
            {"caption": actual_caption, "value": actual_display, "subtract": False},
            {"caption": calc_caption, "value": calc_display, "subtract": True},
        ],
        "variance": {
            "amount": abs(variance_amount),
            "favorable": favorable,
        },
    }


class TestVarianceScenarios:
    """Test all 7 variance scenarios."""

    def test_no_variance_match(self):
        """1. No variance - refund $3594 = refund $3594"""
        summary = build_summary(
            actual_refund=3594, actual_owed=0,
            calc_refund=3594, calc_owed=0
        )

        assert summary["status"] == "match"
        assert summary["amounts"][0] == {"caption": "Actual refund", "value": 3594, "subtract": False}
        assert summary["amounts"][1] == {"caption": "Calculated refund", "value": 3594, "subtract": True}
        assert summary["variance"] == {"amount": 0, "favorable": None}

    def test_greater_refund_favorable(self):
        """2. Greater refund - actual $4000 > calc $3594 (favorable)"""
        summary = build_summary(
            actual_refund=4000, actual_owed=0,
            calc_refund=3594, calc_owed=0
        )

        assert summary["status"] == "gap"
        assert summary["amounts"][0] == {"caption": "Actual refund", "value": 4000, "subtract": False}
        assert summary["amounts"][1] == {"caption": "Calculated refund", "value": 3594, "subtract": True}
        assert summary["variance"] == {"amount": 406, "favorable": True}

    def test_lesser_refund_unfavorable(self):
        """3. Lesser refund - actual $3000 < calc $3594 (unfavorable)"""
        summary = build_summary(
            actual_refund=3000, actual_owed=0,
            calc_refund=3594, calc_owed=0
        )

        assert summary["status"] == "gap"
        assert summary["amounts"][0] == {"caption": "Actual refund", "value": 3000, "subtract": False}
        assert summary["amounts"][1] == {"caption": "Calculated refund", "value": 3594, "subtract": True}
        assert summary["variance"] == {"amount": 594, "favorable": False}

    def test_refund_becomes_owing_unfavorable(self):
        """4. Refund becomes owing - actual owed $500, calc refund $3594 (unfavorable)"""
        summary = build_summary(
            actual_refund=0, actual_owed=500,
            calc_refund=3594, calc_owed=0
        )

        assert summary["status"] == "gap"
        assert summary["amounts"][0] == {"caption": "Actual owed", "value": 500, "subtract": False}
        assert summary["amounts"][1] == {"caption": "Calculated refund", "value": 3594, "subtract": True}
        assert summary["variance"] == {"amount": 4094, "favorable": False}

    def test_owing_becomes_refund_favorable(self):
        """5. Owing becomes refund - actual refund $500, calc owed $500 (favorable)"""
        summary = build_summary(
            actual_refund=500, actual_owed=0,
            calc_refund=0, calc_owed=500
        )

        assert summary["status"] == "gap"
        assert summary["amounts"][0] == {"caption": "Actual refund", "value": 500, "subtract": False}
        assert summary["amounts"][1] == {"caption": "Calculated owed", "value": 500, "subtract": True}
        assert summary["variance"] == {"amount": 1000, "favorable": True}

    def test_owing_becomes_lesser_favorable(self):
        """6. Owing becomes lesser - actual owed $300 < calc owed $500 (favorable)"""
        summary = build_summary(
            actual_refund=0, actual_owed=300,
            calc_refund=0, calc_owed=500
        )

        assert summary["status"] == "gap"
        assert summary["amounts"][0] == {"caption": "Actual owed", "value": 300, "subtract": False}
        assert summary["amounts"][1] == {"caption": "Calculated owed", "value": 500, "subtract": True}
        assert summary["variance"] == {"amount": 200, "favorable": True}

    def test_owing_becomes_greater_unfavorable(self):
        """7. Owing becomes greater - actual owed $700 > calc owed $500 (unfavorable)"""
        summary = build_summary(
            actual_refund=0, actual_owed=700,
            calc_refund=0, calc_owed=500
        )

        assert summary["status"] == "gap"
        assert summary["amounts"][0] == {"caption": "Actual owed", "value": 700, "subtract": False}
        assert summary["amounts"][1] == {"caption": "Calculated owed", "value": 500, "subtract": True}
        assert summary["variance"] == {"amount": 200, "favorable": False}


class TestAmountValues:
    """Test that amounts are always positive with correct subtract flag."""

    def test_amounts_always_positive(self):
        """All amount values should be positive integers."""
        # Test with owing (negative net)
        summary = build_summary(
            actual_refund=0, actual_owed=500,
            calc_refund=0, calc_owed=300
        )

        for amt in summary["amounts"]:
            assert amt["value"] >= 0, f"Amount value should be positive: {amt}"

    def test_subtract_flag_on_calculated(self):
        """Calculated line should always have subtract=True for display math."""
        summary = build_summary(
            actual_refund=1000, actual_owed=0,
            calc_refund=1000, calc_owed=0
        )

        assert summary["amounts"][0]["subtract"] is False  # Actual
        assert summary["amounts"][1]["subtract"] is True   # Calculated
