"""Tests for the version 2 reports.

The point of splitting ``failed_companies.csv`` into four files is that each
one names a different job of work. These tests hold that split exact: a company
must appear in one file and no other, and a file must be written even when it
is empty — a report that disappears when it has nothing to say is
indistinguishable from one that failed to be written.
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

from config.settings import configure
from crawler.crawler_engine import CrawlResult, Outcome
from crawler.platform_detector import Platform
from exporters.report_exporter import (
    OUTCOME_FILES,
    REPORT_COLUMNS,
    coverage_by_platform,
    export_outcome_reports,
    export_summary,
    summarise,
)
from models.job import Job

configure(browser_fallback=False, max_workers=1)


def _job(company: str = "Acme", index: int = 0, country: str = "United States") -> Job:
    """Build one posting.

    Args:
        company: Company name.
        index: Makes the URL unique.
        country: Country to record.

    Returns:
        The job.
    """
    return Job(
        company_name=company,
        job_title=f"Engineer {index}",
        job_url=f"https://acme.com/jobs/{index}",
        location="Austin, TX",
        country=country,
    )


def _record(name: str) -> Dict[str, str]:
    """Build one input record.

    Args:
        name: Company name.

    Returns:
        The record, with the four keys the reader guarantees.
    """
    return {
        "company": name,
        "website": f"https://{name.lower()}.com",
        "career_url": f"https://{name.lower()}.com/careers",
        "it_link": "",
    }


#: One company per outcome, which is what the split has to get right.
PAIRS: List[Tuple[Dict[str, str], CrawlResult]] = [
    (
        _record("Producer"),
        CrawlResult("Producer", Platform.WORKDAY, seed_url="https://w/", jobs=[_job(index=1)], seconds=2.0),
    ),
    (_record("Empty"), CrawlResult("Empty", Platform.LEVER, seed_url="https://l/", seconds=1.0)),
    (
        _record("Unadapted"),
        CrawlResult("Unadapted", Platform.GEM, seed_url="https://g/", error="no adapter for Gem"),
    ),
    (
        _record("Blocked"),
        CrawlResult("Blocked", Platform.ICIMS, seed_url="https://i/", error="AdapterHttpError: HTTP 403"),
    ),
    (
        _record("Nameless"),
        CrawlResult("Nameless", Platform.UNKNOWN, error="no usable URL in the input sheet"),
    ),
]


class TestOutcomeReports(unittest.TestCase):
    """One company, one file."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _rows(self, filename: str) -> List[List[str]]:
        """Read one report back.

        Args:
            filename: Report to read.

        Returns:
            Every row including the header.
        """
        with (self.path / filename).open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.reader(handle))

    def test_every_file_is_written_even_when_empty(self) -> None:
        export_outcome_reports([], self.path)

        for filename in OUTCOME_FILES.values():
            with self.subTest(filename=filename):
                self.assertTrue((self.path / filename).is_file())
                self.assertEqual(self._rows(filename), [list(REPORT_COLUMNS)])

    def test_each_company_lands_in_exactly_one_report(self) -> None:
        export_outcome_reports(PAIRS, self.path)

        placements: Dict[str, List[str]] = {}
        for filename in OUTCOME_FILES.values():
            for row in self._rows(filename)[1:]:
                placements.setdefault(row[0], []).append(filename)

        self.assertEqual(placements["Empty"], ["no_open_jobs.csv"])
        self.assertEqual(placements["Unadapted"], ["unsupported_platforms.csv"])
        self.assertEqual(placements["Blocked"], ["technical_failures.csv"])
        self.assertEqual(placements["Nameless"], ["unknown_platforms.csv"])

    def test_a_company_that_produced_jobs_appears_nowhere(self) -> None:
        export_outcome_reports(PAIRS, self.path)

        for filename in OUTCOME_FILES.values():
            with self.subTest(filename=filename):
                self.assertNotIn("Producer", [row[0] for row in self._rows(filename)[1:]])

    def test_rows_carry_the_sheets_own_urls_and_the_crawled_one(self) -> None:
        export_outcome_reports(PAIRS, self.path)

        row = self._rows("technical_failures.csv")[1]
        self.assertEqual(row[0], "Blocked")
        self.assertEqual(row[1], "https://blocked.com")
        self.assertEqual(row[4], "https://i/")
        self.assertEqual(row[7], Outcome.TECHNICAL.value)
        self.assertEqual(row[8], "AdapterHttpError: HTTP 403")

    def test_an_empty_board_records_a_reason_rather_than_a_blank(self) -> None:
        export_outcome_reports(PAIRS, self.path)

        self.assertIn("advertises no jobs", self._rows("no_open_jobs.csv")[1][8])

    def test_the_directory_is_created(self) -> None:
        target = self.path / "nested" / "deeper"
        export_outcome_reports(PAIRS, target)
        self.assertTrue((target / "no_open_jobs.csv").is_file())


class TestCoverage(unittest.TestCase):
    """The per-platform table behind the coverage report."""

    def test_counts_are_split_by_outcome(self) -> None:
        coverage = {item.platform: item for item in coverage_by_platform(r for _, r in PAIRS)}

        self.assertEqual(coverage["Workday"].produced, 1)
        self.assertEqual(coverage["Workday"].jobs, 1)
        self.assertEqual(coverage["Lever"].no_jobs, 1)
        self.assertEqual(coverage["Gem"].unsupported, 1)
        self.assertEqual(coverage["iCIMS"].technical, 1)

    def test_success_rate_is_a_percentage_of_companies(self) -> None:
        coverage = {item.platform: item for item in coverage_by_platform(r for _, r in PAIRS)}

        self.assertEqual(coverage["Workday"].success_rate, 100.0)
        self.assertEqual(coverage["Lever"].success_rate, 0.0)

    def test_failures_exclude_boards_that_are_merely_empty(self) -> None:
        coverage = {item.platform: item for item in coverage_by_platform(r for _, r in PAIRS)}

        self.assertEqual(coverage["Lever"].failures, 0)
        self.assertEqual(coverage["iCIMS"].failures, 1)

    def test_ordering_is_most_productive_first(self) -> None:
        order = [item.platform for item in coverage_by_platform(r for _, r in PAIRS)]
        self.assertEqual(order[0], "Workday")

    def test_an_empty_run_yields_an_empty_table(self) -> None:
        self.assertEqual(coverage_by_platform([]), [])


class TestSummary(unittest.TestCase):
    """summary.json is meant to be diffed between runs."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "summary.json"

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_totals(self) -> None:
        payload = summarise(PAIRS, [_job(index=1)], 12.5)

        self.assertEqual(payload["totals"]["companies_processed"], 5)
        self.assertEqual(payload["totals"]["jobs_extracted"], 1)
        self.assertEqual(payload["totals"]["companies_producing_jobs"], 1)
        self.assertEqual(payload["totals"]["success_rate"], 20.0)
        self.assertEqual(payload["totals"]["crawl_seconds"], 12.5)

    def test_every_outcome_is_present_even_at_zero(self) -> None:
        payload = summarise(PAIRS[:1], [_job()], 1.0)

        self.assertEqual(set(payload["outcomes"]), {outcome.value for outcome in Outcome})
        self.assertEqual(payload["outcomes"][Outcome.TECHNICAL.value], 0)

    def test_platforms_detected_without_an_adapter_are_named(self) -> None:
        payload = summarise(PAIRS, [], 1.0, supported=[Platform.WORKDAY, Platform.LEVER])

        self.assertIn("Gem", payload["platforms_detected_without_an_adapter"])
        self.assertNotIn("Workday", payload["platforms_detected_without_an_adapter"])

    def test_countries_are_counted(self) -> None:
        jobs = [_job(index=1), _job(index=2, country="India"), _job(index=3, country="")]
        payload = summarise(PAIRS, jobs, 1.0)

        self.assertEqual(payload["jobs_by_country"]["United States"], 1)
        self.assertEqual(payload["jobs_by_country"]["India"], 1)
        self.assertEqual(payload["jobs_by_country"]["(unstated)"], 1)
        self.assertEqual(payload["totals"]["countries_identified"], 2)

    def test_an_empty_run_does_not_divide_by_zero(self) -> None:
        payload = summarise([], [], 0.0)

        self.assertEqual(payload["totals"]["success_rate"], 0.0)
        self.assertEqual(payload["totals"]["seconds_per_company"], 0.0)

    def test_the_file_round_trips_as_json(self) -> None:
        export_summary(PAIRS, [_job()], 3.0, self.path, supported=[Platform.WORKDAY])

        payload = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertEqual(payload["totals"]["companies_processed"], 5)
        self.assertTrue(payload["generated_at"])
        self.assertTrue(payload["platforms"])


if __name__ == "__main__":
    unittest.main()
