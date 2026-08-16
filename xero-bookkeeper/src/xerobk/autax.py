"""Australian tax periods.

The Australian financial year runs 1 July to 30 June. BAS quarters are keyed
to that year, not the calendar year:

    Q1  Jul - Sep    due 28 Oct
    Q2  Oct - Dec    due 28 Feb   (extended over the Christmas shutdown)
    Q3  Jan - Mar    due 28 Apr
    Q4  Apr - Jun    due 28 Jul

The dates above are the ATO's standard self-lodgement due dates. Lodging
through a registered BAS or tax agent generally earns a later concessional
date, so these should be read as "the earliest date this could be due", and
the app labels them that way rather than asserting a hard deadline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# (quarter number, start month, end month, due month, due-month offset in years
# from the quarter's start year)
_QUARTERS: tuple[tuple[int, int, int, int, int], ...] = (
    (1, 7, 9, 10, 0),
    (2, 10, 12, 2, 1),
    (3, 1, 3, 4, 0),
    (4, 4, 6, 7, 0),
)


@dataclass(frozen=True, slots=True)
class Period:
    """A reporting period with a label and, where relevant, a due date."""

    label: str
    start: date
    end: date
    due: date | None = None

    def contains(self, when: date) -> bool:
        return self.start <= when <= self.end

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def financial_year(when: date) -> Period:
    """The Australian FY containing ``when`` (1 Jul - 30 Jun)."""
    start_year = when.year if when.month >= 7 else when.year - 1
    start = date(start_year, 7, 1)
    end = date(start_year + 1, 6, 30)
    return Period(label=f"FY{str(end.year)[2:]}", start=start, end=end)


def month_period(when: date) -> Period:
    """The calendar month containing ``when``."""
    start = when.replace(day=1)
    end = next_month(start) - timedelta(days=1)
    return Period(label=start.strftime("%B %Y"), start=start, end=end)


def next_month(when: date) -> date:
    """First day of the month after the one containing ``when``."""
    if when.month == 12:
        return date(when.year + 1, 1, 1)
    return date(when.year, when.month + 1, 1)


def last_day_of_month(when: date) -> date:
    return next_month(when.replace(day=1)) - timedelta(days=1)


def bas_quarter(when: date) -> Period:
    """The BAS quarter containing ``when``."""
    for number, start_month, end_month, due_month, due_offset in _QUARTERS:
        # Q2 spans a calendar-year boundary only in its due date, not its range.
        if start_month <= when.month <= end_month:
            start = date(when.year, start_month, 1)
            end = last_day_of_month(date(when.year, end_month, 1))
            due = date(when.year + due_offset, due_month, 28)
            fy = financial_year(start)
            return Period(label=f"{fy.label} Q{number}", start=start, end=end, due=due)
    # Unreachable: the four quarters cover all twelve months.
    raise ValueError(f"no BAS quarter for {when}")


def previous_bas_quarter(when: date) -> Period:
    """The quarter before the one containing ``when`` — the one being lodged."""
    current = bas_quarter(when)
    return bas_quarter(current.start - timedelta(days=1))


def previous_month(when: date) -> Period:
    """The month before the one containing ``when`` — the one being closed."""
    first = when.replace(day=1)
    return month_period(first - timedelta(days=1))


def close_period(when: date, frequency: str = "quarterly") -> Period:
    """The period a close run should be reporting on."""
    if frequency == "monthly":
        return previous_month(when)
    if frequency == "annual":
        fy = financial_year(when)
        # Before 1 July the FY in progress is not closable; report the last one.
        return financial_year(fy.start - timedelta(days=1))
    return previous_bas_quarter(when)
