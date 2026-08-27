"""Unit tests for :mod:`crawler.weekly_diff`.

The comparison has one catastrophic failure mode and one merely annoying one.

Catastrophic: closing every job at a company the run could not read. On the
reference sheet 389 of 8,275 companies fail on a given Friday, so the naive
"anything I did not see is gone" rule would report roughly eleven thousand
closures on a quiet week — and then report the same jobs as new again the next
week when the board came back. :class:`TestFailedCompaniesAreNotClosures` is
that rule.

Annoying: a board rewrites its URLs and every posting reads as one closure plus
one opening. :class:`TestRelinking` is that one.
"""

from __future__ import annotations

import unittest
from typing import List

from crawler.weekly_diff import (
    STATUS_ACTIVE,
    STATUS_CLOSED,
    KnownJob,
    ObservedJob,
    compare,
)

ACME = "domain:acme.com"
OTHER = "domain:other.com"


def known(
    uid: str,
    company: str = ACME,
    url: str = "",
    content: str = "",
    status: str = STATUS_ACTIVE,
    title: str = "Engineer",
) -> KnownJob:
    """Build a stored job."""
    return KnownJob(
        job_uid=uid,
        company_key=company,
        url_key=url,
        content_key=content,
        status=status,
        title=title,
    )


def seen(
    uid: str,
    company: str = ACME,
    url: str = "",
    content: str = "",
    title: str = "Engineer",
) -> ObservedJob:
    """Build an observed job."""
    return ObservedJob(
        job_uid=uid, company_key=company, url_key=url, content_key=content, title=title
    )


def uids(jobs: List) -> set:
    """Collect the primary keys off a list of jobs."""
    return {job.job_uid for job in jobs}


class TestNewAndUnchanged(unittest.TestCase):
    """The ordinary week."""

    def test_a_first_run_reports_everything_as_new(self) -> None:
        changes = compare(previous=[], observed=[seen("a"), seen("b")], crawled={ACME})

        self.assertEqual(uids(changes.new_jobs), {"a", "b"})
        self.assertEqual(changes.closed_jobs, [])

    def test_a_job_seen_again_is_still_active(self) -> None:
        changes = compare(previous=[known("a")], observed=[seen("a")], crawled={ACME})

        self.assertEqual(changes.new_jobs, [])
        self.assertEqual(uids(changes.still_active), {"a"})
        self.assertEqual(changes.closed_jobs, [])

    def test_a_mix(self) -> None:
        changes = compare(
            previous=[known("a"), known("b")],
            observed=[seen("a"), seen("c")],
            crawled={ACME},
        )

        self.assertEqual(uids(changes.new_jobs), {"c"})
        self.assertEqual(uids(changes.still_active), {"a"})
        self.assertEqual(uids(changes.closed_jobs), {"b"})

    def test_an_empty_board_closes_its_jobs(self) -> None:
        """A board read successfully that advertises nothing really is empty."""
        changes = compare(previous=[known("a")], observed=[], crawled={ACME})

        self.assertEqual(uids(changes.closed_jobs), {"a"})
        self.assertEqual(changes.skipped_closures, 0)

    def test_a_closed_job_seen_again_is_reopened(self) -> None:
        changes = compare(
            previous=[known("a", status=STATUS_CLOSED)], observed=[seen("a")], crawled={ACME}
        )

        self.assertEqual(uids(changes.reopened_jobs), {"a"})
        self.assertEqual(changes.new_jobs, [])
        self.assertEqual(changes.closed_jobs, [])

    def test_an_already_closed_job_is_not_closed_twice(self) -> None:
        changes = compare(
            previous=[known("a", status=STATUS_CLOSED)], observed=[], crawled={ACME}
        )
        self.assertEqual(changes.closed_jobs, [])

    def test_a_duplicate_observation_is_counted_once(self) -> None:
        changes = compare(previous=[], observed=[seen("a"), seen("a")], crawled={ACME})
        self.assertEqual(len(changes.new_jobs), 1)


class TestFailedCompaniesAreNotClosures(unittest.TestCase):
    """The rule that keeps a bad afternoon from reading as a hiring freeze."""

    def test_a_company_that_was_not_read_keeps_its_jobs(self) -> None:
        changes = compare(previous=[known("a"), known("b")], observed=[], crawled=set())

        self.assertEqual(changes.closed_jobs, [])
        self.assertEqual(changes.skipped_closures, 2)
        self.assertEqual(changes.untouched_companies, {ACME})

    def test_one_company_failing_does_not_affect_another(self) -> None:
        changes = compare(
            previous=[known("a", company=ACME), known("b", company=OTHER)],
            observed=[],
            # Only OTHER was read this run.
            crawled={OTHER},
        )

        self.assertEqual(uids(changes.closed_jobs), {"b"})
        self.assertEqual(changes.skipped_closures, 1)
        self.assertEqual(changes.untouched_companies, {ACME})

    def test_the_withheld_count_is_reported(self) -> None:
        """The operator has to be able to see why closures look low."""
        changes = compare(
            previous=[known(f"j{index}") for index in range(11)], observed=[], crawled=set()
        )
        self.assertEqual(changes.summary()["closures_withheld"], 11)
        self.assertEqual(changes.summary()["jobs_closed"], 0)

    def test_a_company_limited_out_of_the_run_is_untouched(self) -> None:
        """``--limit 50`` must not close every job at company 51 onwards."""
        changes = compare(
            previous=[known("a", company=ACME), known("b", company=OTHER)],
            observed=[seen("a", company=ACME)],
            crawled={ACME},
        )
        self.assertEqual(changes.closed_jobs, [])
        self.assertEqual(changes.untouched_companies, {OTHER})


class TestRelinking(unittest.TestCase):
    """A posting whose identity moved is the posting it already was."""

    def test_a_rewritten_url_is_recognised_by_content(self) -> None:
        changes = compare(
            previous=[known("old", url="https://acme.com/a", content="fingerprint")],
            observed=[seen("new", url="https://acme.com/b", content="fingerprint")],
            crawled={ACME},
        )

        self.assertEqual(changes.new_jobs, [])
        self.assertEqual(changes.closed_jobs, [])
        self.assertEqual(len(changes.relinked), 1)
        self.assertEqual(changes.relinked[0].previous_uid, "old")
        self.assertEqual(changes.relinked[0].matched_on, "content")

    def test_a_retitled_job_is_recognised_by_url(self) -> None:
        changes = compare(
            previous=[known("old", url="https://acme.com/a", content="before")],
            observed=[seen("new", url="https://acme.com/a", content="after")],
            crawled={ACME},
        )

        self.assertEqual(len(changes.relinked), 1)
        self.assertEqual(changes.relinked[0].matched_on, "url")
        self.assertEqual(changes.closed_jobs, [])

    def test_url_is_preferred_over_content(self) -> None:
        changes = compare(
            previous=[
                known("by-url", url="https://acme.com/a", content="x"),
                known("by-content", url="https://acme.com/z", content="fingerprint"),
            ],
            observed=[seen("new", url="https://acme.com/a", content="fingerprint")],
            crawled={ACME},
        )
        self.assertEqual(changes.relinked[0].previous_uid, "by-url")

    def test_a_relinked_job_counts_as_still_active(self) -> None:
        changes = compare(
            previous=[known("old", url="https://acme.com/a")],
            observed=[seen("new", url="https://acme.com/a")],
            crawled={ACME},
        )
        self.assertEqual(uids(changes.still_active), {"new"})

    def test_two_jobs_cannot_relink_onto_one_stored_job(self) -> None:
        """Otherwise one stored job absorbs several postings and the rest vanish."""
        changes = compare(
            previous=[known("old", url="https://acme.com/shared")],
            observed=[
                seen("new-1", url="https://acme.com/shared"),
                seen("new-2", url="https://acme.com/shared"),
            ],
            crawled={ACME},
        )

        self.assertEqual(len(changes.relinked), 1)
        self.assertEqual(uids(changes.new_jobs), {"new-2"})

    def test_relinking_never_crosses_companies(self) -> None:
        """Two companies can link the same aggregator URL."""
        changes = compare(
            previous=[known("acme-job", company=ACME, url="https://aggregator/1")],
            observed=[seen("other-job", company=OTHER, url="https://aggregator/1")],
            crawled={ACME, OTHER},
        )

        self.assertEqual(changes.relinked, [])
        self.assertEqual(uids(changes.new_jobs), {"other-job"})
        self.assertEqual(uids(changes.closed_jobs), {"acme-job"})

    def test_empty_fallback_keys_never_match(self) -> None:
        """Two jobs with no URL and no fingerprint are not the same job."""
        changes = compare(
            previous=[known("old", url="", content="")],
            observed=[seen("new", url="", content="")],
            crawled={ACME},
        )
        self.assertEqual(changes.relinked, [])
        self.assertEqual(uids(changes.new_jobs), {"new"})


class TestSummary(unittest.TestCase):
    """The numbers the dashboard reads."""

    def test_counts_line_up(self) -> None:
        changes = compare(
            previous=[known("a"), known("b"), known("c")],
            observed=[seen("a"), seen("d")],
            crawled={ACME},
        )
        summary = changes.summary()

        self.assertEqual(summary["jobs_new"], 1)
        self.assertEqual(summary["jobs_still_active"], 1)
        self.assertEqual(summary["jobs_closed"], 2)
        self.assertEqual(summary["jobs_observed"], 2)
        self.assertEqual(changes.total_observed, 2)

    def test_a_quiet_week(self) -> None:
        changes = compare(previous=[known("a")], observed=[seen("a")], crawled={ACME})
        summary = changes.summary()
        self.assertEqual(summary["jobs_new"], 0)
        self.assertEqual(summary["jobs_closed"], 0)


class TestThreeConsecutiveRuns(unittest.TestCase):
    """The behaviour that actually matters: stability across weeks."""

    def test_a_stable_board_reports_nothing_after_week_one(self) -> None:
        week_one = compare(previous=[], observed=[seen("a"), seen("b")], crawled={ACME})
        self.assertEqual(len(week_one.new_jobs), 2)

        history = [known("a"), known("b")]

        week_two = compare(previous=history, observed=[seen("a"), seen("b")], crawled={ACME})
        self.assertEqual(week_two.new_jobs, [])
        self.assertEqual(week_two.closed_jobs, [])

        week_three = compare(previous=history, observed=[seen("a"), seen("b")], crawled={ACME})
        self.assertEqual(week_three.new_jobs, [])
        self.assertEqual(week_three.closed_jobs, [])

    def test_a_blocked_week_does_not_churn_the_history(self) -> None:
        """Week 2 is blocked; week 3 recovers. Nothing should have moved."""
        history = [known("a"), known("b")]

        blocked = compare(previous=history, observed=[], crawled=set())
        self.assertEqual(blocked.new_jobs, [])
        self.assertEqual(blocked.closed_jobs, [])
        self.assertEqual(blocked.skipped_closures, 2)

        recovered = compare(previous=history, observed=[seen("a"), seen("b")], crawled={ACME})
        self.assertEqual(recovered.new_jobs, [])
        self.assertEqual(recovered.closed_jobs, [])
        self.assertEqual(len(recovered.still_active), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
