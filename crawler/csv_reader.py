"""Read and validate the company input sheet for the career page crawler.

The crawler is seeded from a CSV that lists, for every target company, its
marketing website and its careers / jobs page. This module is the single entry
point for turning that sheet into clean, in-memory records:

    >>> from crawler.csv_reader import read_companies
    >>> companies = read_companies()
    >>> companies[0]
    {'company': 'AgReliant Genetics',
     'website': 'www.agreliantgenetics.com',
     'career_url': 'https://agreliantgenetics.com/careers/',
     'it_link': 'https://recruiting.ultipro.com/AGR1003ARGI/JobBoard/...'}

Everything downstream may assume the returned records are non-empty, stripped
strings keyed by ``company``, ``website``, ``career_url`` and ``it_link``.
``it_link`` is ``""`` when the sheet has no such column.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Final, List, Sequence

import pandas as pd
from loguru import logger

__all__ = [
    "DEFAULT_CSV_PATH",
    "OPTIONAL_COLUMNS",
    "REQUIRED_COLUMNS",
    "CompanyRecord",
    "read_companies",
]

#: A single validated row of the input sheet.
CompanyRecord = Dict[str, str]

#: Default location of the input sheet, relative to the project root.
DEFAULT_CSV_PATH: Final[Path] = Path("input") / "companies.csv"

#: Output key -> canonical column header expected in the CSV.
REQUIRED_COLUMNS: Final[Dict[str, str]] = {
    "company": "Company Name",
    "website": "Website",
    "career_url": "Career Page URL",
}

#: Output key -> canonical header for columns that are used when present but
#: never demanded. ``it_link`` is the sheet's direct link to the applicant
#: tracking system; where it exists it is a far better crawl seed than the
#: careers page, which is usually a marketing landing page.
OPTIONAL_COLUMNS: Final[Dict[str, str]] = {
    "it_link": "IT LINK",
}

#: Output key -> accepted header spellings, normalised via :func:`_normalise`.
#: Real-world exports vary ("Careers / Jobs URL", "Company Website", ...), so
#: headers are matched on a normalised form rather than byte-for-byte.
_COLUMN_ALIASES: Final[Dict[str, Sequence[str]]] = {
    "company": (
        "Company Name",
        "Company",
        "Name",
    ),
    "website": (
        "Website",
        "Company Website",
        "Web Site",
        "URL",
    ),
    "career_url": (
        "Career Page URL",
        "Careers Page URL",
        "Careers / Jobs URL",
        "Careers URL",
        "Career URL",
        "Careers Page",
        "Jobs URL",
        "Job URL",
    ),
    "it_link": (
        "IT LINK",
        "IT Link",
        "ATS Link",
        "ATS URL",
        "Job Board URL",
    ),
}

_NON_ALNUM: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


def _normalise(header: str) -> str:
    """Reduce a column header to a comparable key.

    Case, punctuation, and surrounding or repeated whitespace are discarded so
    that ``"Careers / Jobs URL"``, ``"careers/jobs url"`` and
    ``"Careers  -  Jobs  URL"`` all collapse to ``"careersjobsurl"``.

    Args:
        header: Raw column header as it appears in the CSV.

    Returns:
        The normalised comparison key.
    """
    return _NON_ALNUM.sub("", str(header).strip().lower())


def _resolve_columns(columns: Sequence[str]) -> Dict[str, str]:
    """Map each required output key to the actual column header present.

    Args:
        columns: Column headers as read from the CSV, in file order.

    Returns:
        Mapping of output key (``company``, ``website``, ``career_url``) to the
        matching header in ``columns``.

    Raises:
        ValueError: If any required column has no match. The message names every
            missing column and lists the headers that were actually found, so a
            mis-exported sheet is diagnosable from the log alone.
    """
    available: Dict[str, str] = {}
    for column in columns:
        # First occurrence wins, so a duplicated header cannot shadow the original.
        available.setdefault(_normalise(column), column)

    resolved: Dict[str, str] = {}
    missing: List[str] = []

    for key, canonical in {**REQUIRED_COLUMNS, **OPTIONAL_COLUMNS}.items():
        for alias in _COLUMN_ALIASES[key]:
            match = available.get(_normalise(alias))
            if match is not None:
                resolved[key] = match
                if match != canonical:
                    logger.debug(
                        "Column {!r} accepted as {!r} (alias of {!r})",
                        match,
                        key,
                        canonical,
                    )
                break
        else:
            if key in REQUIRED_COLUMNS:
                missing.append(canonical)
            else:
                logger.debug("Optional column {!r} not present in the sheet", canonical)

    if missing:
        raise ValueError(
            "Missing required column(s) in the input CSV: "
            f"{', '.join(missing)}. Columns found: {', '.join(map(str, columns)) or '<none>'}."
        )

    return resolved


def _clean(value: object) -> str:
    """Coerce a cell to a stripped string, treating nulls as empty.

    ``pandas`` yields ``NaN`` for empty cells and may parse unquoted values as
    numbers; both are normalised here so callers only ever see ``str``.

    Args:
        value: Raw cell value produced by pandas.

    Returns:
        The stripped string form of ``value``, or ``""`` if it is null.
    """
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def read_companies(csv_path: Path | str = DEFAULT_CSV_PATH) -> List[CompanyRecord]:
    """Read, validate and normalise the company input sheet.

    Blank rows are ignored, every text value is stripped of surrounding
    whitespace, and rows without a company name are skipped (they cannot be
    attributed to a target and would poison the crawl output).

    Args:
        csv_path: Path to the input CSV. Defaults to :data:`DEFAULT_CSV_PATH`.

    Returns:
        One dictionary per usable row, in file order::

            [{"company": "...", "website": "...", "career_url": "..."}]

    Raises:
        FileNotFoundError: If ``csv_path`` does not exist.
        ValueError: If the file is not parsable as CSV, or if any of the
            required columns (:data:`REQUIRED_COLUMNS`) is missing.
    """
    path = Path(csv_path)
    logger.info("Reading company input sheet: {}", path)

    if not path.is_file():
        raise FileNotFoundError(f"Input CSV not found: {path.resolve()}")

    try:
        # dtype=str keeps values verbatim (no numeric coercion of, say, "3E");
        # utf-8-sig transparently drops the BOM that Excel exports prepend.
        frame = pd.read_csv(
            path,
            dtype=str,
            keep_default_na=False,
            na_values=[""],
            encoding="utf-8-sig",
        )
    except pd.errors.EmptyDataError as exc:
        raise ValueError(f"Input CSV is empty: {path}") from exc
    except pd.errors.ParserError as exc:
        raise ValueError(f"Input CSV is malformed: {path} ({exc})") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"Input CSV is not valid UTF-8: {path} ({exc})") from exc

    logger.debug("Loaded {} raw row(s) with columns: {}", len(frame), list(frame.columns))

    columns = _resolve_columns(list(frame.columns))

    records: List[CompanyRecord] = []
    blank_rows = 0
    nameless_rows = 0

    for position, (_, row) in enumerate(frame.iterrows(), start=2):  # 1 = header
        record: CompanyRecord = {key: "" for key in OPTIONAL_COLUMNS}
        record.update({key: _clean(row[column]) for key, column in columns.items()})

        if not any(record.values()):
            blank_rows += 1
            continue

        if not record["company"]:
            nameless_rows += 1
            logger.warning("Row {}: skipped, no company name (website={!r})", position, record["website"])
            continue

        records.append(record)

    if blank_rows:
        logger.debug("Ignored {} blank row(s)", blank_rows)
    if nameless_rows:
        logger.warning("Skipped {} row(s) without a company name", nameless_rows)

    logger.success("Loaded {} company record(s) from {}", len(records), path)
    return records
