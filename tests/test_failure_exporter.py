"""Unit tests for :mod:`exporters.failure_exporter` and the run report helpers."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import main
from crawler.crawler_engine import CrawlResult
from crawler.platform_detector import Platform
from exporters.failure_exporter import FAILURE_COLUMNS, NO_JOBS_REASON, export_failures
from models.job import Job


def _record(name: str = "Acme") -> dict:
    """Build one input-sheet record."""
    return {
        "company": name,
        "website": "https://acme.com",
        "career_url": "https://acme.com/careers",
        "it_link": "https://boards.greenhouse.io/acme",
    }


def _job() -> Job:
    """Build one posting."""
    return Job(company_name="Acme", job_title="Engineer", job_url="https://acme.com/jobs/1")


class TestExportFailures(unittest.TestCase):
    """Every company without jobs is written, with a distinguishable reason."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "failed_companies.csv"

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _rows(self, pairs) -> list:
        export_failures(pairs, self.path)
        with self.path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.reader(handle))

    def test_headers_are_the_agreed_columns(self) -> None:
        rows = self._rows([])
        self.assertEqual(rows[0], list(FAILURE_COLUMNS))
        self.assertEqual(
            rows[0],
            ["Company", "Website", "Career URL", "IT LINK", "Platform", "Failure Reason"],
        )

    def test_companies_with_jobs_are_omitted(self) -> None:
        pairs = [(_record(), CrawlResult("Acme", Platform.GREENHOUSE, jobs=[_job()]))]
        self.assertEqual(len(self._rows(pairs)), 1)  # header only

    def test_failed_company_keeps_its_error(self) -> None:
        result = CrawlResult("Acme", Platform.ICIMS, error="AdapterHttpError: bot challenge")
        rows = self._rows([(_record(), result)])

        self.assertEqual(rows[1][0], "Acme")
        self.assertEqual(rows[1][4], "iCIMS")
        self.assertEqual(rows[1][5], "AdapterHttpError: bot challenge")

    def test_empty_board_is_labelled_distinctly(self) -> None:
        rows = self._rows([(_record(), CrawlResult("Acme", Platform.GREENHOUSE))])
        self.assertEqual(rows[1][5], NO_JOBS_REASON)

    def test_original_sheet_urls_are_carried_through(self) -> None:
        rows = self._rows([(_record(), CrawlResult("Acme", Platform.GREENHOUSE))])
        self.assertEqual(rows[1][1:4], [
            "https://acme.com",
            "https://acme.com/careers",
            "https://boards.greenhouse.io/acme",
        ])

    def test_missing_record_keys_become_empty_cells(self) -> None:
        rows = self._rows([({"company": "Acme"}, CrawlResult("Acme", Platform.UNKNOWN))])
        self.assertEqual(rows[1][1:4], ["", "", ""])

    def test_creates_missing_directories(self) -> None:
        nested = Path(self._directory.name) / "deep" / "failed.csv"
        export_failures([(_record(), CrawlResult("Acme", Platform.UNKNOWN))], nested)
        self.assertTrue(nested.is_file())

    def test_no_partial_file_is_left_behind(self) -> None:
        export_failures([(_record(), CrawlResult("Acme", Platform.UNKNOWN))], self.path)
        self.assertEqual(list(self.path.parent.glob("*.partial.csv")), [])


class TestReasonFamily(unittest.TestCase):
    """Error messages collapse to groupable families, not one bucket each."""

    def test_urls_and_ids_are_stripped_so_messages_group(self) -> None:
        first = main._reason_family(
            "AdapterHttpError: GET https://a.icims.com/jobs/search returned HTTP 405: '...'"
        )
        second = main._reason_family(
            "AdapterHttpError: GET https://b.icims.com/jobs/search returned HTTP 405: '...'"
        )
        self.assertEqual(first, second)

    def test_known_causes_get_their_own_family(self) -> None:
        cases = {
            "AdapterHttpError: https://x served an AWS WAF bot challenge instead": "anti-bot interstitial",
            "AdapterUrlError: 'https://x' is a unified Dayforce portal, rendered client-side": "needs a browser",
            "AdapterUrlError: 'https://x' is a legacy SuccessFactors career portal": "legacy portal",
        }
        for error, expected in cases.items():
            with self.subTest(error=error[:40]):
                self.assertIn(expected, main._reason_family(error))

    def test_unsupported_platform_is_kept_verbatim(self) -> None:
        self.assertEqual(main._reason_family("no adapter for Phenom"), "no adapter for Phenom")


class TestElapsed(unittest.TestCase):
    """Durations read as durations."""

    def test_formats(self) -> None:
        self.assertEqual(main._elapsed(9), "9s")
        self.assertEqual(main._elapsed(69), "1m 09s")
        self.assertEqual(main._elapsed(3849), "1h 04m 09s")


if __name__ == "__main__":
    unittest.main()
