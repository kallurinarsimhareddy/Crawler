"""One definition of "now" and one definition of "this week".

Scattering :func:`datetime.datetime.now` through a codebase produces three
subtle problems at once: local time, which changes twice a year and differs
between the operator's laptop and the scheduled task; naive timestamps, which
compare wrongly against aware ones; and untestable code, because a function that
reads the clock itself cannot be asked what it would do on a different Friday.

So time enters the system here::

    >>> from utils.clock import utc_now, week_of
    >>> stamp = utc_now()
    >>> week_of(stamp)
    ('2026-08-24', '2026-08-30')

Everything is UTC and ISO-8601. A weekly run that starts at 18:00 on a Friday in
one timezone and 02:00 on a Saturday in another must still file its results
under one week, and UTC is what makes that true.

**A week runs Monday to Sunday**, matching ISO 8601. A Friday run therefore
files under the Monday just gone, and a run that slips to Saturday morning —
because the machine was asleep, and Task Scheduler started it late — files under
the same week rather than appearing to be a second week's worth of results.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Final, Optional, Tuple

__all__ = [
    "DATE_FORMAT",
    "as_date",
    "iso",
    "parse",
    "run_id_for",
    "utc_now",
    "week_of",
]

#: How a calendar date is written wherever one is stored.
DATE_FORMAT: Final[str] = "%Y-%m-%d"

#: How a run identifier is written: sortable, filename-safe, and readable
#: enough that an operator can tell which run a log belongs to at a glance.
_RUN_STAMP_FORMAT: Final[str] = "%Y%m%dT%H%M%SZ"


def utc_now() -> datetime:
    """The current time, in UTC, timezone-aware.

    Returns:
        The current moment.
    """
    return datetime.now(timezone.utc)


def iso(moment: Optional[datetime] = None) -> str:
    """Render a moment as an ISO-8601 string.

    Args:
        moment: The moment. Defaults to now.

    Returns:
        The timestamp, e.g. ``"2026-08-24T09:30:00+00:00"``. A naive input is
        assumed to be UTC rather than rejected, so a value read back from a
        database written by an older version still compares correctly.
    """
    value = moment if moment is not None else utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def parse(text: str) -> Optional[datetime]:
    """Read a timestamp previously written by :func:`iso`.

    Args:
        text: An ISO-8601 timestamp, or ``""``.

    Returns:
        The moment in UTC, or ``None`` when the text is blank or unparsable.
        Never raises: a corrupt timestamp in one row must not stop a run.
    """
    candidate = (text or "").strip()
    if not candidate:
        return None

    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def as_date(moment: Optional[datetime] = None) -> date:
    """The calendar date of a moment, in UTC.

    Args:
        moment: The moment. Defaults to now.

    Returns:
        The date.
    """
    value = moment if moment is not None else utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).date()


def week_of(moment: Optional[datetime] = None) -> Tuple[str, str]:
    """The ISO week a moment falls in.

    Args:
        moment: The moment. Defaults to now.

    Returns:
        ``(week_start, week_end)`` as ``YYYY-MM-DD``, Monday and the Sunday
        after it. A Friday run and a run that slipped to the following Saturday
        morning return the same pair, so one week's results stay one week's
        results however late the machine woke up.
    """
    today = as_date(moment)
    monday = today - timedelta(days=today.weekday())
    return monday.strftime(DATE_FORMAT), (monday + timedelta(days=6)).strftime(DATE_FORMAT)


def run_id_for(moment: Optional[datetime] = None) -> str:
    """Build the identifier for a run starting at a given moment.

    Args:
        moment: When the run started. Defaults to now.

    Returns:
        An identifier such as ``"2026-W35-20260828T060000Z"`` — the ISO week it
        belongs to, then the exact start, so runs sort chronologically and a
        second run in one week does not collide with the first.
    """
    value = moment if moment is not None else utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)

    iso_year, iso_week, _ = value.isocalendar()
    return f"{iso_year}-W{iso_week:02d}-{value.strftime(_RUN_STAMP_FORMAT)}"
