"""Write the companies that produced no jobs to ``output/failed_companies.csv``.

The workbook produced by :mod:`exporters.excel_exporter` says what was found.
This says what was *not*, and why, so a run can be triaged and the input sheet
corrected without re-reading logs::

    Company | Website | Career URL | IT LINK | Platform | Failure Reason

A company lands here for one of three reasons, and the ``Failure Reason`` column
distinguishes them:

* the adapter raised — a dead link, a blocked board, an unreadable response;
* the platform was detected but has no adapter yet;
* the crawl succeeded and the board genuinely advertises nothing.

The third is not a defect, but it belongs in the same file: from the outside all
three look identical — a company with no rows in ``jobs.xlsx``.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Final, Iterable, List, Mapping, Sequence, Tuple

from loguru import logger

from crawler.crawler_engine import CrawlResult
from exporters._atomic import write_atomically

__all__ = ["DEFAULT_FAILURE_PATH", "FAILURE_COLUMNS", "NO_JOBS_REASON", "export_failures"]

#: Default destination, relative to the project root.
DEFAULT_FAILURE_PATH: Final[Path] = Path("output") / "failed_companies.csv"

#: Column headers, in order.
FAILURE_COLUMNS: Final[Tuple[str, ...]] = (
    "Company",
    "Website",
    "Career URL",
    "IT LINK",
    "Platform",
    "Failure Reason",
)

#: Reason recorded when the crawl worked but the board was empty.
NO_JOBS_REASON: Final[str] = "crawled successfully; board advertises no jobs"


def export_failures(
    pairs: Iterable[Tuple[Mapping[str, str], CrawlResult]],
    output_path: Path | str = DEFAULT_FAILURE_PATH,
    fallback_when_locked: bool = True,
) -> Path:
    """Write every company that yielded no jobs, with the reason.

    As with the workbook, a destination locked by another program is written
    beside rather than lost.

    Args:
        pairs: ``(input record, crawl result)`` for every company attempted, in
            input order. The record supplies the sheet's original URLs, which
            the result does not carry.
        output_path: Destination CSV. Parent directories are created.
        fallback_when_locked: Whether to write beside a locked destination.

    Returns:
        The path actually written.

    Raises:
        OSError: If the file cannot be written at all.
    """
    rows: List[Sequence[str]] = []

    for record, result in pairs:
        if result.jobs:
            continue

        rows.append(
            (
                result.company or str(record.get("company") or ""),
                str(record.get("website") or ""),
                str(record.get("career_url") or ""),
                str(record.get("it_link") or ""),
                result.platform.value,
                result.error or NO_JOBS_REASON,
            )
        )

    def write(temporary: Path) -> None:
        """Write the CSV to a temporary file.

        Args:
            temporary: Where to write, before it is swapped into place.
        """
        # utf-8-sig so Excel opens accented company names correctly on Windows.
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(FAILURE_COLUMNS)
            writer.writerows(rows)

    written = write_atomically(Path(output_path), write, fallback_when_locked)

    logger.success("Wrote {} company(ies) with no jobs to {}", len(rows), written)
    return written
