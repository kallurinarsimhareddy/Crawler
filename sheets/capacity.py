"""The spreadsheet's cell budget, measured rather than assumed.

A Google spreadsheet holds ten million cells across every tab, and the limit is
counted on the *grid* — rows times columns — not on the cells an operator has
typed into. A tab grown to 400,000 rows spends its whole width whether or not
the rows are full.

That ceiling stops being theoretical at this roster's size. Twelve thousand
companies produce a few hundred thousand postings, and a ledger row is
seventeen columns wide, so writing every posting into a tab exhausts the
spreadsheet before the crawl is halfway through. The failure is also the worst
possible shape: it lands on the *write*, after the crawling is paid for.

So the budget is measured before a run and tracked during it. When a tab cannot
take another batch the guard closes *that tab* and says so, and the crawl keeps
going — the postings are in SQLite either way, and
:class:`~store.repositories.JobRepository` is the ledger this module exists to
avoid duplicating. A closed tab is a reduced report, not a lost crawl.

Nothing here deletes or prunes. Reclaiming space is an operator's decision
about their own data, and a guard that quietly made room would be the most
destructive component in the crawler.

    >>> report = measure(client)
    >>> report.available
    9195814
    >>> guard = CapacityGuard(report)
    >>> guard.allow_rows("NEW_LAST_WEEK", rows=5000, columns=15)
    5000
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Final, List, Mapping, Optional, Sequence

from loguru import logger

__all__ = [
    "CELL_BUDGET",
    "DEFAULT_RESERVE",
    "CapacityGuard",
    "CapacityReport",
    "Projection",
    "measure",
    "project",
]

#: Cells one Google spreadsheet may hold, across every tab.
CELL_BUDGET: Final[int] = 10_000_000

#: Cells held back from the budget. The guard refuses to spend into this, so a
#: run that fills the sheet still leaves room for the operator's own columns,
#: for WEEKLY_RUNS to record what happened, and for the next run to report that
#: it could not write. A guard that spent the last cell would leave no way to
#: say so.
DEFAULT_RESERVE: Final[int] = 250_000


@dataclass
class CapacityReport:
    """What the spreadsheet is currently spending.

    Attributes:
        used: Cells the grid occupies now, across every tab.
        budget: The ceiling, normally :data:`CELL_BUDGET`.
        reserve: Cells held back from use.
        per_tab: Tab title to the cells its grid occupies.
    """

    used: int = 0
    budget: int = CELL_BUDGET
    reserve: int = DEFAULT_RESERVE
    per_tab: Dict[str, int] = field(default_factory=dict)

    @property
    def available(self) -> int:
        """Cells a run may still spend, after the reserve.

        Returns:
            The spendable remainder, never negative.
        """
        return max(0, self.budget - self.reserve - self.used)

    @property
    def spent_fraction(self) -> float:
        """How much of the budget is gone, as ``0.0`` to ``1.0``.

        Returns:
            The fraction.
        """
        return (self.used / self.budget) if self.budget else 1.0

    def describe(self) -> str:
        """One line an operator can read.

        Returns:
            The summary.
        """
        return (
            f"{self.used:,} of {self.budget:,} cells used "
            f"({self.spent_fraction * 100:.1f}%), "
            f"{self.available:,} available after a {self.reserve:,}-cell reserve"
        )


@dataclass
class Projection:
    """What a run of a given size is expected to cost.

    Attributes:
        companies: Companies the run intends to crawl.
        jobs_per_company: Postings each is expected to yield.
        postings: The resulting posting count.
        cells_per_posting: Cells one posting occupies across the tabs written.
        cells: Total projected cells.
        available: Cells the sheet can actually give.
        fits: Whether the projection is within the budget.
        companies_that_fit: How many companies the budget would cover.
    """

    companies: int = 0
    jobs_per_company: float = 0.0
    postings: int = 0
    cells_per_posting: float = 0.0
    cells: int = 0
    available: int = 0
    fits: bool = True
    companies_that_fit: int = 0

    def describe(self) -> str:
        """One line an operator can read.

        Returns:
            The summary.
        """
        verdict = "fits" if self.fits else "EXCEEDS the budget"
        return (
            f"{self.companies:,} company(ies) at {self.jobs_per_company:.1f} posting(s) each "
            f"projects to {self.postings:,} posting(s) and {self.cells:,} cell(s); "
            f"{self.available:,} available — {verdict}"
            + ("" if self.fits else f", {self.companies_that_fit:,} would fit")
        )


def measure(client: Any, budget: int = CELL_BUDGET, reserve: int = DEFAULT_RESERVE) -> CapacityReport:
    """Read the spreadsheet's current grid usage.

    Args:
        client: The Sheets client.
        budget: The ceiling to measure against.
        reserve: Cells to hold back.

    Returns:
        What is used and what is left.
    """
    report = CapacityReport(budget=int(budget), reserve=max(0, int(reserve)))

    metadata = client.metadata()
    for sheet in metadata.get("sheets", []) or []:
        properties = sheet.get("properties", {}) or {}
        title = str(properties.get("title") or "")
        grid = properties.get("gridProperties", {}) or {}
        cells = int(grid.get("rowCount", 0) or 0) * int(grid.get("columnCount", 0) or 0)
        if title:
            report.per_tab[title] = cells
        report.used += cells

    return report


def project(
    companies: int,
    jobs_per_company: float,
    cells_per_posting: float,
    available: int,
) -> Projection:
    """Work out whether a run of this size can be written.

    Args:
        companies: Companies to crawl.
        jobs_per_company: Postings each is expected to produce.
        cells_per_posting: Cells one posting costs across the tabs written.
        available: Cells the sheet can give.

    Returns:
        The projection.
    """
    postings = int(round(max(0, companies) * max(0.0, jobs_per_company)))
    cells = int(round(postings * max(0.0, cells_per_posting)))

    per_company = max(0.0, jobs_per_company) * max(0.0, cells_per_posting)
    fits_count = int(available // per_company) if per_company > 0 else companies

    return Projection(
        companies=int(companies),
        jobs_per_company=float(jobs_per_company),
        postings=postings,
        cells_per_posting=float(cells_per_posting),
        cells=cells,
        available=int(available),
        fits=cells <= available,
        companies_that_fit=min(int(companies), fits_count),
    )


class CapacityGuard:
    """Hands out cells until the budget is gone, then says no.

    The guard is deliberately per-tab. A run that fills ``NEW_LAST_WEEK`` should
    keep updating ``CURRENT_JOBS`` and ``WEEKLY_RUNS`` — those are small, bounded
    and the ones an operator actually watches — rather than stopping every
    output because the largest one ran out.

    Args:
        report: What the sheet is already spending.
    """

    def __init__(self, report: Optional[CapacityReport] = None) -> None:
        self.report = report or CapacityReport()
        self.spent = 0
        #: Tab title to the reason it stopped accepting rows.
        self.blocked: Dict[str, str] = {}

    @property
    def remaining(self) -> int:
        """Cells still available to this run.

        Returns:
            The remainder, never negative.
        """
        return max(0, self.report.available - self.spent)

    def is_blocked(self, tab: str) -> bool:
        """Whether a tab has already run out.

        Args:
            tab: Its title.

        Returns:
            ``True`` when it is closed to further writes.
        """
        return tab in self.blocked

    def allow_rows(self, tab: str, rows: int, columns: int) -> int:
        """How many of these rows may be written.

        Args:
            tab: The tab being written, for the message and the block.
            rows: Rows the caller wants to write.
            columns: The tab's width, in cells per row.

        Returns:
            How many rows fit — ``rows`` when everything fits, fewer when the
            budget runs out part-way, ``0`` when the tab is closed. The
            returned cells are counted as spent.
        """
        rows = max(0, int(rows))
        columns = max(1, int(columns))

        if rows == 0:
            return 0

        if self.is_blocked(tab):
            return 0

        wanted = rows * columns
        if wanted <= self.remaining:
            self.spent += wanted
            return rows

        allowed = self.remaining // columns
        self.spent += allowed * columns

        reason = (
            f"the spreadsheet's {self.report.budget:,}-cell budget is spent "
            f"({allowed:,} of {rows:,} row(s) written)"
        )
        self.blocked[tab] = reason
        logger.warning(
            "{}: capacity reached — {}. Every posting is still in SQLite; "
            "the crawl continues and this tab stops here.",
            tab,
            reason,
        )
        return allowed

    def note(self, tab: str, reason: str) -> None:
        """Record that a tab was closed for a reason other than the budget.

        Args:
            tab: Its title.
            reason: Why it stopped.
        """
        if tab not in self.blocked:
            self.blocked[tab] = reason
            logger.warning("{}: output stopped — {}", tab, reason)

    def summary(self) -> str:
        """What to record on the run.

        Returns:
            A short description, or ``""`` when nothing was blocked.
        """
        if not self.blocked:
            return ""
        return "; ".join(f"{tab}: {reason}" for tab, reason in sorted(self.blocked.items()))
