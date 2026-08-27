"""Work out what changed between last Friday's crawl and this one.

This is the module the whole product is for. Everything else gathers evidence;
this decides what to tell the operator::

    >>> from crawler.weekly_diff import compare
    >>> changes = compare(previous=known, observed=this_week, crawled={"domain:acme.com"})
    >>> len(changes.new_jobs), len(changes.closed_jobs)
    (14, 3)

Three rules carry it, and each exists because the obvious implementation gets
something badly wrong.

**A job is closed only if its company was successfully read this run.** The
naive diff — "anything I did not see is gone" — turns every 403, every timeout,
every Cloudflare interstitial into a mass closure. On the reference sheet 389 of
8,275 companies fail on a given run; treating those as closures would report
roughly eleven thousand jobs shut on a week when nothing shut at all, and then
report them all as new again the following Friday when the board came back.
:func:`compare` therefore takes the set of companies actually observed, and a
company outside that set has its jobs left exactly as they were.

**A job whose identity moved is re-linked, not replaced.** A board that rewrites
its URLs would otherwise produce a closure and an opening for every posting on
it. Each job carries a URL key and a content key alongside its primary one, so a
posting whose requisition id survived a move — or whose URL survived a retitle —
is recognised as the job it already was. The re-link is reported separately so a
run can say how much of its comparison rested on the fallbacks.

**An empty crawl of a company is evidence, and a failed one is not.** A board
read successfully that advertises nothing really has closed everything on it.
That is why the caller passes the companies it *read*, rather than the companies
whose jobs it *found*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Final, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from loguru import logger

__all__ = [
    "KnownJob",
    "ObservedJob",
    "WeeklyChanges",
    "compare",
]

#: Status values a stored job can hold.
STATUS_ACTIVE: Final[str] = "active"
STATUS_CLOSED: Final[str] = "closed"


@dataclass(frozen=True)
class KnownJob:
    """A job as the history remembers it.

    Attributes:
        job_uid: Its stored primary key.
        company_key: The company it belongs to.
        url_key: Canonical URL, for re-linking when the primary key moves.
        content_key: Company-title-location hash, the last-resort re-link.
        status: ``"active"`` or ``"closed"``.
        title: Its title, for reporting.
        first_seen: When it was first observed.
    """

    job_uid: str
    company_key: str
    url_key: str = ""
    content_key: str = ""
    status: str = STATUS_ACTIVE
    title: str = ""
    first_seen: str = ""


@dataclass(frozen=True)
class ObservedJob:
    """A job this run actually saw.

    Attributes:
        job_uid: Identity derived by :mod:`crawler.identity`.
        company_key: The company it was found under.
        url_key: Canonical URL.
        content_key: Company-title-location hash.
        title: Its title, for reporting.
    """

    job_uid: str
    company_key: str
    url_key: str = ""
    content_key: str = ""
    title: str = ""


@dataclass(frozen=True)
class Relink:
    """A job recognised under a new primary key.

    Attributes:
        previous_uid: The key the history had.
        observed: The job as seen this run, carrying the new key.
        matched_on: ``"url"`` or ``"content"`` — which fallback recognised it.
    """

    previous_uid: str
    observed: ObservedJob
    matched_on: str


@dataclass
class WeeklyChanges:
    """Everything that changed, and everything deliberately left alone.

    Attributes:
        new_jobs: Postings never seen before.
        reopened_jobs: Postings seen again after having been closed.
        still_active: Postings seen this run that were already known.
        closed_jobs: Postings that were active, whose company was read, and
            which the board no longer advertises.
        relinked: Postings recognised under a new primary key.
        untouched_companies: Companies whose jobs were left alone because the
            run did not successfully read them. Reported so the dashboard can
            say why a closure count is lower than it looks.
        skipped_closures: How many active jobs were spared closure by that
            rule. This is the number that would have been wrong.
    """

    new_jobs: List[ObservedJob] = field(default_factory=list)
    reopened_jobs: List[ObservedJob] = field(default_factory=list)
    still_active: List[ObservedJob] = field(default_factory=list)
    closed_jobs: List[KnownJob] = field(default_factory=list)
    relinked: List[Relink] = field(default_factory=list)
    untouched_companies: Set[str] = field(default_factory=set)
    skipped_closures: int = 0

    @property
    def total_observed(self) -> int:
        """How many postings this run saw, after deduplication.

        Returns:
            New plus reopened plus still-active.
        """
        return len(self.new_jobs) + len(self.reopened_jobs) + len(self.still_active)

    def summary(self) -> Dict[str, int]:
        """Render the comparison as counts, for the dashboard and the log.

        Returns:
            Metric name to value.
        """
        return {
            "jobs_observed": self.total_observed,
            "jobs_new": len(self.new_jobs),
            "jobs_reopened": len(self.reopened_jobs),
            "jobs_still_active": len(self.still_active),
            "jobs_closed": len(self.closed_jobs),
            "jobs_relinked": len(self.relinked),
            "companies_not_read": len(self.untouched_companies),
            "closures_withheld": self.skipped_closures,
        }


def _index(previous: Iterable[KnownJob]) -> Tuple[
    Dict[str, KnownJob], Dict[Tuple[str, str], KnownJob], Dict[Tuple[str, str], KnownJob]
]:
    """Build the three lookups a comparison needs.

    Args:
        previous: Every job the history holds.

    Returns:
        ``(by_uid, by_url, by_content)``. The fallback lookups are keyed by
        ``(company_key, key)`` so a URL shared across two companies — which
        happens on aggregator links — cannot re-link one company's job onto
        another's.
    """
    by_uid: Dict[str, KnownJob] = {}
    by_url: Dict[Tuple[str, str], KnownJob] = {}
    by_content: Dict[Tuple[str, str], KnownJob] = {}

    for job in previous:
        by_uid[job.job_uid] = job
        if job.url_key:
            # First writer wins: if two stored jobs somehow share a URL key,
            # re-linking onto either is a guess, so the older one is kept.
            by_url.setdefault((job.company_key, job.url_key), job)
        if job.content_key:
            by_content.setdefault((job.company_key, job.content_key), job)

    return by_uid, by_url, by_content


def _relink(
    observed: ObservedJob,
    by_url: Mapping[Tuple[str, str], KnownJob],
    by_content: Mapping[Tuple[str, str], KnownJob],
    claimed: Set[str],
) -> Optional[Relink]:
    """Try to recognise an unfamiliar job as one already known.

    Args:
        observed: The job this run saw, whose primary key is not in the history.
        by_url: Known jobs indexed by ``(company_key, url_key)``.
        by_content: Known jobs indexed by ``(company_key, content_key)``.
        claimed: Primary keys already matched this run, so two observed jobs
            cannot both re-link onto one stored job.

    Returns:
        The re-link, or ``None`` when nothing recognises it.
    """
    if observed.url_key:
        candidate = by_url.get((observed.company_key, observed.url_key))
        if candidate is not None and candidate.job_uid not in claimed:
            return Relink(previous_uid=candidate.job_uid, observed=observed, matched_on="url")

    if observed.content_key:
        candidate = by_content.get((observed.company_key, observed.content_key))
        if candidate is not None and candidate.job_uid not in claimed:
            return Relink(previous_uid=candidate.job_uid, observed=observed, matched_on="content")

    return None


def compare(
    previous: Iterable[KnownJob],
    observed: Sequence[ObservedJob],
    crawled: Set[str],
) -> WeeklyChanges:
    """Compare this run's postings against the history.

    Args:
        previous: Every job the history holds, active and closed.
        observed: Every posting this run saw, already deduplicated.
        crawled: ``company_key`` for every company this run *successfully read*
            — including those whose board turned out to be empty, and excluding
            every company that failed, was blocked, or was not attempted. Jobs
            belonging to a company outside this set are never closed.

    Returns:
        The changes.
    """
    by_uid, by_url, by_content = _index(previous)

    changes = WeeklyChanges()
    claimed: Set[str] = set()
    seen_this_run: Set[str] = set()

    for job in observed:
        # A board can list one posting twice; the crawler dedupes, but a caller
        # assembling from several sources may not.
        if job.job_uid in seen_this_run:
            continue
        seen_this_run.add(job.job_uid)

        known = by_uid.get(job.job_uid)

        if known is None:
            relink = _relink(job, by_url, by_content, claimed)
            if relink is not None:
                claimed.add(relink.previous_uid)
                changes.relinked.append(relink)
                known = by_uid[relink.previous_uid]
            else:
                changes.new_jobs.append(job)
                continue
        else:
            claimed.add(known.job_uid)

        if known.status == STATUS_CLOSED:
            changes.reopened_jobs.append(job)
        else:
            changes.still_active.append(job)

    # Anything active that this run did not see -- but only where the company
    # was actually read. This is the rule that keeps a bad afternoon on the
    # network from being reported as a hiring freeze.
    for job in by_uid.values():
        if job.status != STATUS_ACTIVE:
            continue
        if job.job_uid in claimed:
            continue

        if job.company_key not in crawled:
            changes.untouched_companies.add(job.company_key)
            changes.skipped_closures += 1
            continue

        changes.closed_jobs.append(job)

    if changes.skipped_closures:
        logger.info(
            "Withheld {} closure(s) across {} company(ies) the run could not read",
            changes.skipped_closures,
            len(changes.untouched_companies),
        )

    logger.success(
        "Weekly comparison: {} new, {} reopened, {} still active, {} closed, {} re-linked",
        len(changes.new_jobs),
        len(changes.reopened_jobs),
        len(changes.still_active),
        len(changes.closed_jobs),
        len(changes.relinked),
    )
    return changes
