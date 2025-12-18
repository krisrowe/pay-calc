"""Pay stub gap detection utilities.

Shared logic for detecting gaps in pay stub sequences.
Used by both the `stubs list` command and `analysis` command.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple


# Biweekly pay = ~14 days. Allow up to 20 days before flagging a gap.
MAX_INTERVAL_DAYS = 20


@dataclass
class Gap:
    """Represents a gap in pay stub sequence."""

    gap_type: str  # "start", "middle", "end"
    days: int
    after_date: Optional[str]  # Date before gap (None for start gap)
    before_date: Optional[str]  # Date after gap (None for end gap)
    message: str

    @property
    def estimated_date(self) -> str:
        """Generate estimated display date for the gap."""
        if self.gap_type == "start" and self.before_date:
            try:
                dt = datetime.strptime(self.before_date, "%Y-%m-%d")
                return f"{dt.year}-01-??"
            except ValueError:
                return "????-01-??"
        elif self.gap_type == "end" and self.after_date:
            try:
                dt = datetime.strptime(self.after_date, "%Y-%m-%d")
                # Use current month or December
                today = datetime.now()
                year_end = datetime(dt.year, 12, 31)
                ref = min(today, year_end)
                return f"{dt.year}-{ref.month:02d}-??"
            except ValueError:
                return "????-??-??"
        elif self.after_date and self.before_date:
            try:
                prev = datetime.strptime(self.after_date, "%Y-%m-%d")
                next_ = datetime.strptime(self.before_date, "%Y-%m-%d")
                mid = prev + (next_ - prev) / 2
                return f"{mid.year}-{mid.month:02d}-??"
            except ValueError:
                return "????-??-??"
        return "????-??-??"


@dataclass
class GapAnalysis:
    """Result of analyzing gaps in a pay stub sequence."""

    gaps: List[Gap]
    total_stubs: int
    first_date: Optional[str]
    last_date: Optional[str]

    @property
    def has_gaps(self) -> bool:
        return len(self.gaps) > 0

    @property
    def gap_count(self) -> int:
        return len(self.gaps)

    def to_errors_warnings(self) -> Tuple[List[str], List[str]]:
        """Convert gaps to error/warning string lists for analysis command."""
        errors = []
        warnings = []

        for gap in self.gaps:
            if gap.gap_type == "end":
                # End gaps are warnings (year may not be over yet)
                warnings.append(gap.message)
            else:
                # Start and middle gaps are errors
                errors.append(gap.message)

        return errors, warnings


def detect_gaps(
    stubs: List[Dict[str, Any]],
    year: str,
    filter_regular_only: bool = False,
    reference_date: Optional[datetime] = None
) -> GapAnalysis:
    """Detect gaps in a sequence of pay stubs.

    Args:
        stubs: List of stub dictionaries with at least 'pay_date' field.
        year: The year being analyzed (for start/end gap detection).
        filter_regular_only: If True, only consider stubs with _pay_type == "regular".
        reference_date: Date to use for end gap detection. Defaults to now.

    Returns:
        GapAnalysis with detected gaps and metadata.
    """
    if reference_date is None:
        reference_date = datetime.now()

    # Optionally filter to regular pay stubs only
    if filter_regular_only:
        working_stubs = [s for s in stubs if s.get("_pay_type") == "regular"]
    else:
        working_stubs = list(stubs)

    if not working_stubs:
        return GapAnalysis(gaps=[], total_stubs=0, first_date=None, last_date=None)

    # Sort by pay_date
    working_stubs.sort(key=lambda s: s.get("pay_date", ""))

    gaps: List[Gap] = []

    # Get first and last dates
    first_stub = working_stubs[0]
    last_stub = working_stubs[-1]
    first_date_str = first_stub.get("pay_date", "")
    last_date_str = last_stub.get("pay_date", "")

    # Check for gap at start of year
    if first_date_str:
        try:
            first_date = datetime.strptime(first_date_str, "%Y-%m-%d")
            year_start = datetime(int(year), 1, 1)
            days_from_start = (first_date - year_start).days

            if days_from_start > MAX_INTERVAL_DAYS:
                gaps.append(Gap(
                    gap_type="start",
                    days=days_from_start,
                    after_date=None,
                    before_date=first_date_str,
                    message=f"Gap at start: {days_from_start} days from Jan 1 to first stub ({first_date_str})"
                ))
        except ValueError:
            pass

    # Check for gaps between consecutive stubs
    prev_date: Optional[datetime] = None
    prev_date_str: Optional[str] = None
    prev_ytd: float = 0

    for stub in working_stubs:
        pay_date_str = stub.get("pay_date", "")
        if not pay_date_str:
            continue

        try:
            pay_date = datetime.strptime(pay_date_str, "%Y-%m-%d")
        except ValueError:
            continue

        ytd_gross = stub.get("pay_summary", {}).get("ytd", {}).get("gross", 0)

        if prev_date:
            days_gap = (pay_date - prev_date).days

            # Check for employer change (YTD resets to low value)
            # When employer changes, don't flag the gap
            is_employer_change = prev_ytd > 10000 and ytd_gross < prev_ytd * 0.5

            if days_gap > MAX_INTERVAL_DAYS and not is_employer_change:
                missed_periods = max(1, (days_gap - 7) // 14)
                gaps.append(Gap(
                    gap_type="middle",
                    days=days_gap,
                    after_date=prev_date_str,
                    before_date=pay_date_str,
                    message=f"Gap: {days_gap} days between {prev_date_str} and {pay_date_str} (~{missed_periods} missed pay period(s))"
                ))

        prev_date = pay_date
        prev_date_str = pay_date_str
        prev_ytd = ytd_gross

    # Check for gap at end
    if last_date_str:
        try:
            last_date = datetime.strptime(last_date_str, "%Y-%m-%d")
            year_end = datetime(int(year), 12, 31)
            # Use earlier of reference_date or year end
            end_ref = min(reference_date, year_end)
            days_to_end = (end_ref - last_date).days

            if days_to_end > MAX_INTERVAL_DAYS:
                gaps.append(Gap(
                    gap_type="end",
                    days=days_to_end,
                    after_date=last_date_str,
                    before_date=None,
                    message=f"Gap at end: {days_to_end} days from last stub ({last_date_str}) to {end_ref.strftime('%Y-%m-%d')}"
                ))
        except ValueError:
            pass

    return GapAnalysis(
        gaps=gaps,
        total_stubs=len(working_stubs),
        first_date=first_date_str,
        last_date=last_date_str
    )


def check_first_stub_ytd(stubs: List[Dict[str, Any]]) -> Optional[str]:
    """Check if first stub's YTD matches its current (indicating no missing earlier stubs).

    This is a separate check from gap detection because it uses YTD/current comparison
    rather than date intervals.

    Args:
        stubs: List of stub dictionaries.

    Returns:
        Error message if first stub appears to be missing earlier periods, None otherwise.
    """
    if not stubs:
        return None

    # Filter to regular stubs
    regular_stubs = [s for s in stubs if s.get("_pay_type") == "regular"]
    if not regular_stubs:
        return None

    # Sort by pay_date
    regular_stubs.sort(key=lambda s: s.get("pay_date", ""))
    first_stub = regular_stubs[0]

    first_ytd = first_stub.get("pay_summary", {}).get("ytd", {}).get("gross", 0)
    first_current = first_stub.get("pay_summary", {}).get("current", {}).get("gross", 0)
    first_date = first_stub.get("pay_date", "unknown")

    # If YTD equals current (within tolerance), it's the first pay period
    if abs(first_ytd - first_current) > 0.01:
        return (
            f"First stub ({first_date}) has YTD ${first_ytd:,.2f} but current "
            f"${first_current:,.2f} - missing earlier pay periods"
        )

    return None
