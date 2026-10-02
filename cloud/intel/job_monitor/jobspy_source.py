"""JobSpy as a job source: the open-source ``python-jobspy`` library behind the monitor contract.

A JobSpy monitor stores its search in ``filters``::

    {"boards": ["indeed"], "search_terms": ["ERP", "SAP"], "location": "United States",
     "hours_old": 24, "results_wanted": 25, "country": "USA"}

Each (board, search term) pair is one "page" of a run, so progress, checkpoints and
resume work exactly like listing pages. Results are search windows ("posted in the last
24 hours"), not a complete listing, so a JobSpy monitor never closes jobs.

**Access policy.** JobSpy reaches boards in ways SANA's safe fetcher cannot vouch for
(Indeed: Indeed's private mobile GraphQL API with the Indeed app's embedded key and an
app User-Agent). Every board is therefore OFF until the deployment lists it in
``SANA_JOBSPY_BOARDS`` (or ``platform.config.extra["jobspy_boards_enabled"]``) after the
access has been authorized. A disabled board is reported on the run, never silently
skipped, and the library is not even imported.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

from cloud.intel.core.context import ValidationError

__all__ = ["JOBSPY_BOARDS", "BOARD_LABELS", "BOARD_URLS", "JobSpyStrategy", "enabled_boards",
           "validate_jobspy_filters", "record_from_jobspy"]

JOBSPY_BOARDS = ("indeed", "linkedin", "zip_recruiter", "glassdoor", "google")
BOARD_LABELS = {"indeed": "Indeed", "linkedin": "LinkedIn", "zip_recruiter": "ZipRecruiter",
                "glassdoor": "Glassdoor", "google": "Google Jobs"}
BOARD_URLS = {"indeed": "https://www.indeed.com/", "linkedin": "https://www.linkedin.com/jobs/",
              "zip_recruiter": "https://www.ziprecruiter.com/", "glassdoor": "https://www.glassdoor.com/",
              "google": "https://www.google.com/search?q=jobs"}
DISABLED_REASON = ("JobSpy board {label} is disabled: it needs authorized access (Indeed: an authorized or partner "
                   "feed). An admin enables it with SANA_JOBSPY_BOARDS once access is approved.")
MAX_TERMS = 25
MAX_RESULTS = 200


def enabled_boards(platform: Any) -> Set[str]:
    configured = platform.config.extra.get("jobspy_boards_enabled")
    if configured is None:
        configured = [b.strip() for b in os.environ.get("SANA_JOBSPY_BOARDS", "").split(",") if b.strip()]
    return {b for b in configured if b in JOBSPY_BOARDS}


def validate_jobspy_filters(raw: Mapping[str, Any]) -> Dict[str, Any]:
    boards = [str(b).strip().lower() for b in (raw.get("boards") or ["indeed"])]
    unknown = [b for b in boards if b not in JOBSPY_BOARDS]
    if unknown:
        raise ValidationError(f"unknown JobSpy board(s): {', '.join(unknown)}")
    terms = raw.get("search_terms") or []
    if isinstance(terms, str):
        terms = [t for t in (p.strip() for p in terms.replace("\n", ",").split(",")) if t]
    terms = list(dict.fromkeys(str(t).strip()[:120] for t in terms if str(t).strip()))
    if not terms:
        raise ValidationError("add at least one search keyword")
    if len(terms) > MAX_TERMS:
        raise ValidationError(f"at most {MAX_TERMS} search keywords per monitor")
    try:
        hours_old = int(24 if raw.get("hours_old") in (None, "") else raw["hours_old"])
        results = int(25 if raw.get("results_wanted") in (None, "") else raw["results_wanted"])
    except (TypeError, ValueError) as error:
        raise ValidationError("hours_old and results_wanted must be numbers") from error
    if not 1 <= hours_old <= 720:
        raise ValidationError("freshness (hours_old) must be between 1 and 720 hours")
    if not 1 <= results <= MAX_RESULTS:
        raise ValidationError(f"results_wanted must be between 1 and {MAX_RESULTS} per search")
    return {"boards": list(dict.fromkeys(boards)), "search_terms": terms,
            "location": str(raw.get("location") or "United States")[:200], "hours_old": hours_old,
            "results_wanted": results, "country": str(raw.get("country") or "USA")[:40]}


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # NaN from pandas


def _salary(row: Mapping[str, Any]) -> Optional[str]:
    low, high = _num(row.get("min_amount")), _num(row.get("max_amount"))
    if low is None and high is None:
        return None
    currency = str(row.get("currency") or "").upper()
    symbol = "$" if currency in ("", "USD") else f"{currency} "
    interval = str(row.get("interval") or "").strip()

    def fmt(v: float) -> str:
        return f"{symbol}{v / 1000:.0f}k" if v >= 1000 else f"{symbol}{v:g}"

    text = fmt(low) if high is None else (fmt(high) if low is None else f"{fmt(low)}–{fmt(high)}")
    return f"{text}/{interval}" if interval and interval.lower() != "nan" else text


def _clean(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and value != value):
        return None
    text = str(value).strip()
    return text or None


def record_from_jobspy(row: Mapping[str, Any], *, board: str, search_term: str) -> Dict[str, Any]:
    """One JobSpy result row -> a monitor record. Only what JobSpy returned is used."""
    return {
        "job_url": _clean(row.get("job_url")),
        "title": _clean(row.get("title")),
        "company_name": _clean(row.get("company")),
        "location": _clean(row.get("location")),
        "experience_level": _clean(row.get("job_level")),
        "salary_budget": _salary(row),
        "keywords": [],                      # filled from the job's own text by the relevance engine
        "remote": "Remote" if row.get("is_remote") is True else None,
        "description": _clean(row.get("description")),
        "date_posted": row.get("date_posted"),
        "search_term": search_term,
        "source_board": BOARD_LABELS.get(board, board),
    }


def _default_scrape(**kwargs: Any) -> List[Dict[str, Any]]:  # pragma: no cover - needs the optional library
    from jobspy import scrape_jobs  # cloud/intel/requirements-jobspy.txt; not installed by default

    frame = scrape_jobs(**kwargs)
    return frame.to_dict("records") if frame is not None else []


class JobSpyStrategy:
    """Pages are ``jobspy:<n>`` tokens over the (board, search term) pairs."""

    name = "jobspy"
    newest_first = False
    supports_close = False
    source_name = "JobSpy"

    def __init__(self, monitor: Mapping[str, Any], *, enabled: Set[str],
                 scrape: Optional[Callable[..., Sequence[Mapping[str, Any]]]] = None) -> None:
        self.params = validate_jobspy_filters(monitor.get("filters") or {})
        self.pairs = [(b, t) for b in self.params["boards"] for t in self.params["search_terms"]]
        self.enabled = enabled
        self.scrape = scrape or _default_scrape

    def first_url(self, monitor: Mapping[str, Any]) -> str:
        return "jobspy:0"

    def read(self, url: str) -> Any:
        from cloud.intel.job_monitor.strategies import PageResult

        index = int(url.split(":", 1)[1])
        board, term = self.pairs[index]
        next_url = f"jobspy:{index + 1}" if index + 1 < len(self.pairs) else None
        label = BOARD_LABELS.get(board, board)
        if board not in self.enabled:
            return PageResult(url, "DISABLED", reason=DISABLED_REASON.format(label=label))
        try:
            rows = self.scrape(site_name=[board], search_term=term, location=self.params["location"],
                               hours_old=self.params["hours_old"], results_wanted=self.params["results_wanted"],
                               country_indeed=self.params["country"], description_format="markdown")
        except Exception as error:  # noqa: BLE001 - a board failure is reported on the run
            return PageResult(url, "FAILED", reason=f"{label} search {term!r} failed: {type(error).__name__}: "
                                                    f"{str(error)[:200]}")
        records = [record_from_jobspy(r, board=board, search_term=term) for r in rows]
        return PageResult(url, "OK", records=records[: self.params["results_wanted"]], next_url=next_url,
                          cards=len(records))
