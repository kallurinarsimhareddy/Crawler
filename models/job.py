"""The single job posting record passed between crawler stages.

Every adapter returns these, the engine collects them, and
:mod:`exporters.excel_exporter` writes them out. The fields mirror the columns
of ``output/jobs.xlsx`` one-for-one:

    ==================  ==========================================================
    Field               Meaning
    ==================  ==========================================================
    company_name        Company as named in ``input/companies.csv``.
    job_title           Posting title, as advertised.
    location            Location string as published, e.g. "Austin, TX".
    country             Country derived from ``location``; empty if undetermined.
    job_url             Absolute link to the individual posting.
    career_page_url     Careers page the posting was crawled from.
    platform            ATS the posting came from, or "Generic HTML".
    ==================  ==========================================================

Missing values are represented as empty strings rather than ``None`` so the
exported sheet never shows "None" in a cell. Records are frozen: once an adapter
has produced a posting, later stages describe it rather than edit it.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Dict, Final, Tuple

__all__ = ["EXPORT_COLUMNS", "Job"]

#: Field name -> spreadsheet header, in the exact order of ``output/jobs.xlsx``.
EXPORT_COLUMNS: Final[Tuple[Tuple[str, str], ...]] = (
    ("company_name", "Company Name"),
    ("job_title", "Job Title"),
    ("location", "Location"),
    ("country", "Country"),
    ("job_url", "Job URL"),
    ("career_page_url", "Career Page URL"),
    ("platform", "Platform"),
)


def _clean(value: object) -> str:
    """Coerce a field to a stripped string, treating ``None`` as empty.

    Args:
        value: Whatever an adapter put in the field.

    Returns:
        The stripped string form of ``value``, or ``""`` if it is ``None``.
    """
    if value is None:
        return ""
    return str(value).strip()


@dataclass(frozen=True)
class Job:
    """One job posting, normalised for export.

    All fields are stripped strings; adapters may pass ``None`` for anything
    they could not determine and it becomes ``""``.

    Attributes:
        company_name: Company as named in the input sheet.
        job_title: Posting title.
        location: Location exactly as the platform published it.
        country: Country derived from ``location``, or ``""``.
        job_url: Absolute URL of the individual posting.
        career_page_url: Careers page this posting was found from.
        platform: Label of the ATS it came from.
    """

    company_name: str
    job_title: str
    job_url: str
    location: str = ""
    country: str = ""
    career_page_url: str = ""
    platform: str = ""

    # --- Added in version 3 -------------------------------------------------
    # Detail the version 3 sheet has columns for. Every one defaults to empty
    # and none appears in EXPORT_COLUMNS, so ``output/jobs.xlsx`` keeps exactly
    # the seven columns it has always had and no adapter has to change. An
    # adapter that already parses one of these — several parse a department or
    # a requisition id and currently discard it — can now pass it through.
    #
    # Nothing here is ever inferred. A board that does not publish a posted
    # date leaves ``posted_date`` empty rather than being given a guess, which
    # is why version 3 dates a posting by when it first observed it instead.
    department: str = ""
    employment_type: str = ""
    workplace_type: str = ""
    posted_date: str = ""
    job_id: str = ""

    def __post_init__(self) -> None:
        """Normalise every field to a stripped string in place."""
        for item in fields(self):
            # object.__setattr__ because the dataclass is frozen.
            object.__setattr__(self, item.name, _clean(getattr(self, item.name)))

    @property
    def key(self) -> Tuple[str, str]:
        """Identity used to drop duplicate postings.

        Boards routinely list the same posting under several categories or
        locations. The job URL identifies a posting; the company scopes it so
        two firms sharing an ATS tenant can never collide.

        Returns:
            ``(company_name, job_url)``, both lowercased.
        """
        return self.company_name.lower(), self.job_url.lower()

    def to_row(self) -> Dict[str, str]:
        """Project the record onto the spreadsheet's columns.

        Returns:
            Mapping of spreadsheet header to value, in :data:`EXPORT_COLUMNS`
            order.
        """
        return {header: getattr(self, name) for name, header in EXPORT_COLUMNS}
