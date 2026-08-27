"""Unit tests for the observation adapter, the checkpoint and the weekly runner.

Everything runs offline: the spreadsheet is the in-memory fake, and the crawl
engine is replaced by one that returns canned results, so no test touches the
network or needs credentials.

The classes worth reading first:

* :class:`TestQuotaSafety` — the runner must not read Google Sheets once per
  company. The quota is sixty reads a minute; an N+1 pattern would spend it
  inside the first minute of a five-hour run. The test crawls 60 companies and
  asserts the read count does not grow with them.
* :class:`TestInterruptionAndResume` — a run killed half-way must resume rather
  than restart, and must not crawl a company twice.
* :class:`TestClosureRequiresEvidence` — a company that could not be read has
  none of its postings closed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from crawler.checkpoint import STATUS_DONE, STATUS_FAILED, Checkpoint
from crawler.crawler_engine import CrawlResult
from crawler.observations import (
    observation_from_job,
    observations_from_result,
    observations_from_results,
    workplace_type_of,
)
from crawler.platform_detector import Platform
from crawler.weekly_run import WeeklyRun
from models.job import Job
from sheets.client import SheetsClient
from sheets.companies import CompanyRepository
from sheets.init import initialise
from sheets.jobs import JobRepository
from sheets.runs import RunRepository
from tests._fake_sheets import FakeSheetsService

ACME = "domain:acme.com"


def acme_company(**extra) -> Dict[str, str]:
    """A master-list record as the runner reads it."""
    record = {
        "company": "Acme Corporation",
        "company_key": ACME,
        "website": "https://acme.com",
        "career_url": "https://acme.com/careers",
        "it_link": "",
    }
    record.update(extra)
    return record


def posting(title: str = "Software Engineer", url: str = "https://acme.com/jobs/1", **extra) -> Job:
    """A version 2 job record as an adapter produces it."""
    return Job(
        company_name="Acme Corporation",
        job_title=title,
        job_url=url,
        location=extra.pop("location", "Austin, TX"),
        country=extra.pop("country", "United States"),
        career_page_url="https://acme.com/careers",
        platform=extra.pop("platform", "Greenhouse"),
        **extra,
    )


def result(jobs: Sequence[Job] = (), error: Optional[str] = None, **extra) -> CrawlResult:
    """A version 2 crawl result."""
    return CrawlResult(
        company=extra.pop("company", "Acme Corporation"),
        platform=extra.pop("platform", Platform.GREENHOUSE),
        seed_url=extra.pop("seed_url", "https://acme.com/careers"),
        seed_field=extra.pop("seed_field", "career_url"),
        jobs=list(jobs),
        error=error,
        **extra,
    )


class FakeEngine:
    """A crawl engine that returns canned results and records what it was asked.

    Args:
        by_company: Company key to the result to return.
        fail_after: Raise ``KeyboardInterrupt`` once this many companies have
            been crawled, to simulate an interruption mid-run.
    """

    def __init__(
        self,
        by_company: Optional[Mapping[str, CrawlResult]] = None,
        fail_after: Optional[int] = None,
    ) -> None:
        self.by_company = dict(by_company or {})
        self.fail_after = fail_after
        self.crawled: List[str] = []
        self.batches: List[int] = []

    def crawl_all(self, records: Sequence[Mapping[str, str]], max_workers: int = 0) -> List[CrawlResult]:
        """Return one result per record."""
        self.batches.append(len(records))
        results: List[CrawlResult] = []

        for record in records:
            key = str(record.get("company_key") or "")
            self.crawled.append(key)

            if self.fail_after is not None and len(self.crawled) > self.fail_after:
                raise KeyboardInterrupt("simulated interruption")

            results.append(
                self.by_company.get(key)
                or result(company=str(record.get("company") or ""), jobs=[])
            )

        return results


def fixture() -> tuple:
    """An initialised in-memory spreadsheet and a client over it."""
    service = FakeSheetsService({"Sheet1": []})
    client = SheetsClient(service, "fake", sleep=lambda _seconds: None)
    initialise(client)
    return client, service


class TemporaryCheckpointTest(unittest.TestCase):
    """Base class giving each test its own checkpoint directory."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.checkpoint_path = Path(self._directory.name) / "checkpoint.json"

    def tearDown(self) -> None:
        self._directory.cleanup()


class TestWorkplaceType(unittest.TestCase):
    """Read the working arrangement; never guess it."""

    def test_remote(self) -> None:
        self.assertEqual(workplace_type_of("Remote - US"), "Remote")
        self.assertEqual(workplace_type_of("", "Engineer (Work From Home)"), "Remote")

    def test_hybrid_wins_over_remote(self) -> None:
        """"Hybrid - remote 2 days" is hybrid, and mentions both."""
        self.assertEqual(workplace_type_of("Hybrid - remote 2 days a week"), "Hybrid")

    def test_onsite_when_stated(self) -> None:
        self.assertEqual(workplace_type_of("Austin, TX (On-site)"), "On-site")

    def test_a_plain_city_says_nothing(self) -> None:
        """Most such roles are on site, but "most" is not "this one"."""
        self.assertEqual(workplace_type_of("Austin, TX"), "")

    def test_blank(self) -> None:
        self.assertEqual(workplace_type_of("", ""), "")


class TestObservationAdapter(unittest.TestCase):
    """Converting version 2 records into version 3 observations."""

    def test_every_stored_field_is_carried(self) -> None:
        job = posting(
            department="Engineering",
            employment_type="Full-time",
            posted_date="2026-08-01",
            job_id="R-1234",
        )
        record = observation_from_job(
            job, company_key=ACME, company_name="Acme Corporation",
            website="acme.com", run_id="run-1", industry="SaaS", source="crawl:it_link",
        )

        for field in (
            "job_key", "company_key", "company_name", "job_title", "job_url",
            "url_key", "content_key", "platform", "department", "location",
            "country", "employment_type", "posted_date", "source", "run_id",
            "job_id", "is_tech",
        ):
            self.assertIn(field, record, field)

        self.assertEqual(record["department"], "Engineering")
        self.assertEqual(record["employment_type"], "Full-time")
        self.assertEqual(record["posted_date"], "2026-08-01")
        self.assertEqual(record["job_id"], "R-1234")
        self.assertEqual(record["run_id"], "run-1")
        self.assertEqual(record["industry"], "SaaS")

    def test_identity_comes_from_the_shared_logic(self) -> None:
        from crawler.identity import job_identity

        job = posting(url="https://boards.greenhouse.io/acme/jobs/4012345")
        record = observation_from_job(job, company_key=ACME, website="acme.com")
        expected = job_identity(
            "Acme Corporation", job.job_url, job.job_title,
            location=job.location, platform=job.platform, website="acme.com",
        )
        self.assertEqual(record["job_key"], expected.job_uid)

    def test_tracking_parameters_do_not_change_the_identity(self) -> None:
        plain = observation_from_job(
            posting(url="https://acme.com/jobs/1"), company_key=ACME, website="acme.com"
        )
        decorated = observation_from_job(
            posting(url="https://acme.com/jobs/1?utm_source=news&gh_src=x"),
            company_key=ACME, website="acme.com",
        )
        self.assertEqual(plain["job_key"], decorated["job_key"])

    def test_a_posting_with_no_title_is_dropped(self) -> None:
        self.assertIsNone(observation_from_job(posting(title=""), company_key=ACME))

    def test_a_posting_identifying_no_company_is_dropped(self) -> None:
        job = Job(company_name="", job_title="Engineer", job_url="https://x/1")
        self.assertIsNone(observation_from_job(job))

    def test_the_sheets_company_key_wins_over_the_jobs_name(self) -> None:
        """The master list is authoritative about which company this is."""
        job = posting()
        record = observation_from_job(job, company_key="domain:different.com")
        self.assertEqual(record["company_key"], "domain:different.com")

    def test_workplace_type_is_derived_when_the_board_says(self) -> None:
        record = observation_from_job(
            posting(location="Remote - United States"), company_key=ACME, website="acme.com"
        )
        self.assertEqual(record["workplace_type"], "Remote")

    def test_missing_detail_stays_missing(self) -> None:
        """A board that publishes nothing gets empty cells, not guesses."""
        record = observation_from_job(posting(), company_key=ACME, website="acme.com")
        self.assertEqual(record["posted_date"], "")
        self.assertEqual(record["employment_type"], "")
        self.assertEqual(record["department"], "")

    def test_technology_roles_are_flagged(self) -> None:
        tech = observation_from_job(posting("Senior Software Engineer"), company_key=ACME)
        other = observation_from_job(posting("Maintenance Technician"), company_key=ACME)
        self.assertTrue(tech["is_tech"])
        self.assertFalse(other["is_tech"])

    def test_a_unicode_title_survives(self) -> None:
        record = observation_from_job(
            posting("Ingénieur Logiciel Sénior"), company_key=ACME, website="acme.com"
        )
        self.assertEqual(record["job_title"], "Ingénieur Logiciel Sénior")


class TestObservationDeduplication(unittest.TestCase):
    """One posting yields one observation, however many times it is listed."""

    def test_a_board_listing_a_posting_twice(self) -> None:
        crawl = result(jobs=[posting(url="https://acme.com/jobs/1"),
                             posting(url="https://acme.com/jobs/1")])
        records = observations_from_result(crawl, company=acme_company())
        self.assertEqual(len(records), 1)

    def test_url_variants_of_one_posting_collapse(self) -> None:
        crawl = result(
            jobs=[
                posting(url="https://acme.com/jobs/1"),
                posting(url="https://www.acme.com/jobs/1/?utm_source=x"),
            ]
        )
        records = observations_from_result(crawl, company=acme_company())
        self.assertEqual(len(records), 1)

    def test_deduplication_spans_companies(self) -> None:
        """Two sheet rows can share an ATS tenant."""
        pairs = [
            (acme_company(), result(jobs=[posting(url="https://acme.com/jobs/1")])),
            (acme_company(), result(jobs=[posting(url="https://acme.com/jobs/1")])),
        ]
        self.assertEqual(len(observations_from_results(pairs, run_id="run-1")), 1)

    def test_two_distinct_postings_are_two_observations(self) -> None:
        crawl = result(
            jobs=[
                posting("Engineer", "https://acme.com/jobs/1"),
                posting("Analyst", "https://acme.com/jobs/2"),
            ]
        )
        self.assertEqual(len(observations_from_result(crawl, company=acme_company())), 2)

    def test_a_failed_result_yields_nothing(self) -> None:
        crawl = result(jobs=[], error="AdapterHttpError: HTTP 403")
        self.assertEqual(observations_from_result(crawl, company=acme_company()), [])

    def test_the_crawled_url_beats_the_sheets_one(self) -> None:
        """Discovery may have found a better board than the operator supplied."""
        crawl = result(jobs=[posting()], seed_url="https://boards.greenhouse.io/acme")
        records = observations_from_result(crawl, company=acme_company())
        self.assertEqual(records[0]["career_url"], "https://boards.greenhouse.io/acme")


class TestCheckpoint(TemporaryCheckpointTest):
    """The local resume file."""

    def test_recording_and_remaining(self) -> None:
        checkpoint = Checkpoint.start("run-1", total=3, path=self.checkpoint_path)
        checkpoint.record("domain:a.com", STATUS_DONE)

        self.assertEqual(
            checkpoint.remaining(["domain:a.com", "domain:b.com"]), ["domain:b.com"]
        )

    def test_crawled_keys_exclude_failures(self) -> None:
        """The distinction the closure rule depends on."""
        checkpoint = Checkpoint.start("run-1", path=self.checkpoint_path)
        checkpoint.record("domain:a.com", STATUS_DONE)
        checkpoint.record("domain:b.com", STATUS_FAILED)

        self.assertEqual(checkpoint.crawled_keys, {"domain:a.com"})
        self.assertEqual(checkpoint.failed_keys, {"domain:b.com"})
        self.assertEqual(checkpoint.completed, 2)

    def test_save_and_load_round_trip(self) -> None:
        checkpoint = Checkpoint.start("run-1", total=5, path=self.checkpoint_path)
        checkpoint.record("domain:a.com", STATUS_DONE)
        checkpoint.save()

        loaded = Checkpoint.load(self.checkpoint_path)

        self.assertEqual(loaded.run_id, "run-1")
        self.assertEqual(loaded.total, 5)
        self.assertEqual(loaded.companies, {"domain:a.com": STATUS_DONE})

    def test_it_holds_operational_state_only(self) -> None:
        """No credentials, no company names, no URLs, no job data."""
        checkpoint = Checkpoint.start("run-1", total=2, path=self.checkpoint_path)
        checkpoint.record("domain:acme.com", STATUS_DONE)
        checkpoint.save()

        payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))

        self.assertEqual(
            set(payload),
            {"format", "run_id", "started_at", "updated_at", "week_start", "total",
             "completed", "companies"},
        )
        # The only company data is the key, which names no URL and no secret.
        self.assertEqual(list(payload["companies"]), ["domain:acme.com"])

        text = self.checkpoint_path.read_text(encoding="utf-8").lower()
        for forbidden in ("password", "token", "secret", "credential", "private_key", "http"):
            self.assertNotIn(forbidden, text, forbidden)

    def test_a_corrupt_checkpoint_is_ignored_rather_than_fatal(self) -> None:
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(Checkpoint.load(self.checkpoint_path))

    def test_a_missing_checkpoint_is_not_an_error(self) -> None:
        self.assertIsNone(Checkpoint.load(self.checkpoint_path))

    def test_resume_continues_an_existing_run(self) -> None:
        first = Checkpoint.start("run-1", total=5, path=self.checkpoint_path)
        first.record("domain:a.com", STATUS_DONE)
        first.save()

        resumed = Checkpoint.resume_or_start("run-2", total=5, path=self.checkpoint_path)

        self.assertEqual(resumed.run_id, "run-1")
        self.assertEqual(resumed.completed, 1)

    def test_fresh_ignores_an_existing_checkpoint(self) -> None:
        first = Checkpoint.start("run-1", total=5, path=self.checkpoint_path)
        first.record("domain:a.com", STATUS_DONE)
        first.save()

        fresh = Checkpoint.resume_or_start(
            "run-2", total=5, path=self.checkpoint_path, resume=False
        )

        self.assertEqual(fresh.run_id, "run-2")
        self.assertEqual(fresh.completed, 0)

    def test_a_checkpoint_from_another_week_is_not_resumed(self) -> None:
        """Finishing a fortnight-old crawl would file stale postings as new."""
        stale = Checkpoint.start("run-old", total=5, path=self.checkpoint_path)
        stale.week_start = "2020-01-06"
        stale.record("domain:a.com", STATUS_DONE)
        stale.save()

        resumed = Checkpoint.resume_or_start("run-new", total=5, path=self.checkpoint_path)

        self.assertEqual(resumed.run_id, "run-new")
        self.assertEqual(resumed.completed, 0)

    def test_archive_moves_it_out_of_the_active_path(self) -> None:
        checkpoint = Checkpoint.start("run-1", path=self.checkpoint_path)
        checkpoint.save()

        archived = checkpoint.archive()

        self.assertFalse(self.checkpoint_path.is_file())
        self.assertTrue(archived.is_file())
        self.assertIn("run-1", archived.name)

    def test_a_partial_write_never_replaces_a_good_checkpoint(self) -> None:
        checkpoint = Checkpoint.start("run-1", path=self.checkpoint_path)
        checkpoint.record("domain:a.com", STATUS_DONE)
        checkpoint.save()

        # Whatever happens, the file on disk parses.
        payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["run_id"], "run-1")
        self.assertFalse(
            list(self.checkpoint_path.parent.glob("*.partial.json")),
            "the temporary file should not survive a successful write",
        )


class TestWeeklyRunner(TemporaryCheckpointTest):
    """The runner end to end, against an in-memory spreadsheet."""

    def setUp(self) -> None:
        super().setUp()
        self.client, self.service = fixture()
        CompanyRepository(self.client).import_rows(
            [
                {"company": "Acme Corporation", "website": "acme.com",
                 "career_url": "https://acme.com/careers"},
                {"company": "Other Inc", "website": "other.com",
                 "career_url": "https://other.com/jobs"},
            ]
        )

    def run_with(self, engine: FakeEngine, **kwargs) -> Any:
        """Execute a run against the fixture."""
        runner = WeeklyRun(
            self.client, engine=engine, checkpoint_path=self.checkpoint_path, batch_size=1
        )
        return runner.execute(**kwargs)

    def test_a_run_writes_every_tab(self) -> None:
        engine = FakeEngine(
            {
                ACME: result(jobs=[posting("Software Engineer", "https://acme.com/jobs/1")]),
                "domain:other.com": result(
                    company="Other Inc",
                    jobs=[posting("DevOps Engineer", "https://other.com/jobs/2")],
                ),
            }
        )

        summary = self.run_with(engine, run_id="run-1")

        self.assertEqual(summary.companies_attempted, 2)
        self.assertEqual(summary.observations, 2)
        self.assertEqual(JobRepository(self.client).history.count(), 2)
        self.assertEqual(JobRepository(self.client).current.count(), 2)
        self.assertEqual(len(JobRepository(self.client).weekly.read()), 2)
        self.assertEqual(len(RunRepository(self.client).all()), 1)

    def test_the_run_row_records_its_counts(self) -> None:
        engine = FakeEngine({ACME: result(jobs=[posting()])})
        self.run_with(engine, run_id="run-1")

        stored = RunRepository(self.client).get("run-1")
        self.assertEqual(stored.status, "done")
        self.assertEqual(stored.counts["companies_checked"], 2)
        self.assertEqual(stored.counts["jobs_new"], 1)

    def test_failures_are_recorded_and_classified(self) -> None:
        engine = FakeEngine(
            {
                ACME: result(
                    jobs=[],
                    error="AdapterHttpError: GET https://x returned HTTP 403: 'Just a moment...'",
                ),
            }
        )

        summary = self.run_with(engine, run_id="run-1")

        self.assertEqual(summary.companies_failed, 1)
        self.assertEqual(summary.blockers.get("cloudflare challenge"), 1)

        from sheets.runs import FailureRepository

        stored = FailureRepository(self.client).store.read_index("company_key")
        self.assertEqual(stored[ACME].get("failure_type"), "cloudflare challenge")

    def test_a_failed_company_closes_nothing(self) -> None:
        """The rule that keeps a bad afternoon from reading as a hiring freeze."""
        engine = FakeEngine({ACME: result(jobs=[posting()])})
        self.run_with(engine, run_id="run-1")
        self.assertEqual(JobRepository(self.client).active_count(), 1)

        blocked = FakeEngine({ACME: result(jobs=[], error="HTTP 403 Forbidden")})
        summary = self.run_with(blocked, run_id="run-2", resume=False)

        self.assertEqual(len(summary.changes.closed_jobs), 0)
        self.assertGreaterEqual(summary.changes.skipped_closures, 1)
        self.assertEqual(JobRepository(self.client).active_count(), 1)

    def test_an_empty_board_that_was_read_does_close(self) -> None:
        engine = FakeEngine({ACME: result(jobs=[posting()])})
        self.run_with(engine, run_id="run-1")

        empty = FakeEngine({ACME: result(jobs=[])})
        summary = self.run_with(empty, run_id="run-2", resume=False)

        self.assertEqual(len(summary.changes.closed_jobs), 1)

    def test_dry_run_writes_nothing(self) -> None:
        engine = FakeEngine({ACME: result(jobs=[posting()])})
        self.service.calls.clear()

        summary = self.run_with(engine, run_id="run-1", dry_run=True)

        self.assertEqual(self.service.mutating_calls(), [])
        self.assertEqual(summary.observations, 1)
        self.assertEqual(JobRepository(self.client).history.count(), 0)

    def test_dry_run_writes_no_checkpoint(self) -> None:
        """Otherwise the next real run skips companies never actually crawled."""
        self.run_with(FakeEngine(), run_id="run-1", dry_run=True)

        self.assertFalse(self.checkpoint_path.is_file())

        engine = FakeEngine()
        self.run_with(engine, run_id="run-2")
        self.assertEqual(len(engine.crawled), 2)

    def test_rates_read_n_a_when_nothing_was_attempted(self) -> None:
        """A run that crawled nothing did not fail at everything."""
        summary = self.run_with(FakeEngine(), limit=0, run_id="run-1", dry_run=True)
        summary.companies_attempted = 0

        self.assertIsNone(summary.success_rate)
        self.assertEqual(summary.rate_text, ("n/a", "n/a"))

    def test_rates_are_computed_when_companies_were_attempted(self) -> None:
        engine = FakeEngine({ACME: result(jobs=[], error="HTTP 403")})
        summary = self.run_with(engine, run_id="run-1")

        self.assertEqual(summary.companies_attempted, 2)
        self.assertEqual(summary.companies_failed, 1)
        self.assertEqual(summary.rate_text, ("50.0%", "50.0%"))

    def test_limit_crawls_only_the_first_companies(self) -> None:
        engine = FakeEngine()
        summary = self.run_with(engine, limit=1, run_id="run-1")

        self.assertEqual(len(engine.crawled), 1)
        self.assertEqual(summary.companies_attempted, 1)
        self.assertEqual(summary.companies_total, 2)

    def test_a_limited_run_does_not_close_the_companies_it_skipped(self) -> None:
        engine = FakeEngine(
            {
                ACME: result(jobs=[posting("Engineer", "https://acme.com/jobs/1")]),
                "domain:other.com": result(
                    company="Other Inc",
                    jobs=[posting("Developer", "https://other.com/jobs/2")],
                ),
            }
        )
        self.run_with(engine, run_id="run-1")
        self.assertEqual(JobRepository(self.client).active_count(), 2)

        summary = self.run_with(FakeEngine(), limit=1, run_id="run-2", resume=False)

        self.assertEqual(JobRepository(self.client).active_count(), 1)
        self.assertGreaterEqual(summary.changes.skipped_closures, 1)

    def test_the_checkpoint_is_archived_on_success(self) -> None:
        self.run_with(FakeEngine(), run_id="run-1")

        self.assertFalse(self.checkpoint_path.is_file())
        self.assertTrue((self.checkpoint_path.parent / "completed" / "run-1.json").is_file())

    def test_the_dashboard_is_written(self) -> None:
        from sheets.runs import DashboardRepository

        engine = FakeEngine({ACME: result(jobs=[posting()])})
        self.run_with(engine, run_id="run-1")

        metrics = DashboardRepository(self.client).read_metrics()
        self.assertEqual(metrics["Total companies"], "2")
        self.assertEqual(metrics["Companies checked"], "2")
        self.assertEqual(metrics["New this week"], "1")
        self.assertIn("Success rate", metrics)
        self.assertIn("Duration", metrics)

    def test_dashboard_covers_every_required_metric(self) -> None:
        from sheets.runs import DashboardRepository

        self.run_with(FakeEngine({ACME: result(jobs=[posting()])}), run_id="run-1")
        metrics = DashboardRepository(self.client).read_metrics()

        for required in (
            "Total companies", "Companies checked", "Succeeded", "Failed",
            "With jobs", "No open jobs", "Active jobs", "New this week",
            "Closed this week", "Companies discovered", "Success rate", "Duration",
        ):
            self.assertIn(required, metrics, required)

    def test_non_technology_jobs_stay_out_of_the_current_view(self) -> None:
        engine = FakeEngine(
            {
                ACME: result(
                    jobs=[
                        posting("Software Engineer", "https://acme.com/jobs/1"),
                        posting("Maintenance Technician", "https://acme.com/jobs/2"),
                    ]
                )
            }
        )
        self.run_with(engine, run_id="run-1")

        jobs = JobRepository(self.client)
        self.assertEqual(jobs.history.count(), 2)
        self.assertEqual(jobs.current.count(), 1)
        self.assertEqual(jobs.current.read()[0].get("job_title"), "Software Engineer")


class TestInterruptionAndResume(TemporaryCheckpointTest):
    """A run killed half-way resumes rather than restarting."""

    def setUp(self) -> None:
        super().setUp()
        self.client, self.service = fixture()
        CompanyRepository(self.client).import_rows(
            [
                {"company": f"Company {index}", "website": f"c{index}.com"}
                for index in range(6)
            ]
        )

    def runner(self, engine: FakeEngine) -> WeeklyRun:
        """A runner with a one-company batch, so interruption is precise."""
        return WeeklyRun(
            self.client, engine=engine, checkpoint_path=self.checkpoint_path, batch_size=1
        )

    def test_a_stop_request_finishes_the_batch_and_checkpoints(self) -> None:
        engine = FakeEngine()
        run = self.runner(engine)

        # Stop after the first batch, as a signal handler would.
        original = run._absorb

        def absorb(*args, **kwargs):
            original(*args, **kwargs)
            run.request_stop()

        run._absorb = absorb
        summary = run.execute(run_id="run-1")

        self.assertTrue(summary.interrupted)
        self.assertEqual(len(engine.crawled), 1)
        self.assertTrue(self.checkpoint_path.is_file())

    def test_resuming_does_not_recrawl_completed_companies(self) -> None:
        first = FakeEngine()
        run = self.runner(first)
        original = run._absorb

        def absorb(*args, **kwargs):
            original(*args, **kwargs)
            if len(first.crawled) >= 2:
                run.request_stop()

        run._absorb = absorb
        run.execute(run_id="run-1")

        already = set(first.crawled)
        self.assertEqual(len(already), 2)

        second = FakeEngine()
        resumed = self.runner(second).execute(run_id="run-1", resume=True)

        self.assertEqual(set(second.crawled) & already, set())
        self.assertEqual(len(second.crawled), 4)
        self.assertTrue(resumed.resumed)

    def test_every_company_is_crawled_exactly_once_across_both_halves(self) -> None:
        first = FakeEngine()
        run = self.runner(first)
        original = run._absorb

        def absorb(*args, **kwargs):
            original(*args, **kwargs)
            if len(first.crawled) >= 3:
                run.request_stop()

        run._absorb = absorb
        run.execute(run_id="run-1")

        second = FakeEngine()
        self.runner(second).execute(run_id="run-1", resume=True)

        combined = first.crawled + second.crawled
        self.assertEqual(len(combined), 6)
        self.assertEqual(len(set(combined)), 6)

    def test_the_checkpoint_survives_the_interruption(self) -> None:
        engine = FakeEngine()
        run = self.runner(engine)
        original = run._absorb

        def absorb(*args, **kwargs):
            original(*args, **kwargs)
            run.request_stop()

        run._absorb = absorb
        run.execute(run_id="run-1")

        loaded = Checkpoint.load(self.checkpoint_path)
        self.assertEqual(loaded.run_id, "run-1")
        self.assertEqual(loaded.completed, 1)

    def test_a_completed_resume_archives_the_checkpoint(self) -> None:
        first = FakeEngine()
        run = self.runner(first)
        original = run._absorb

        def absorb(*args, **kwargs):
            original(*args, **kwargs)
            if len(first.crawled) >= 2:
                run.request_stop()

        run._absorb = absorb
        run.execute(run_id="run-1")

        self.runner(FakeEngine()).execute(run_id="run-1", resume=True)

        self.assertFalse(self.checkpoint_path.is_file())
        self.assertTrue((self.checkpoint_path.parent / "completed" / "run-1.json").is_file())


class TestMinimumViableSheetRow(TemporaryCheckpointTest):
    """A row of Company Name plus Website is enough to crawl."""

    def setUp(self) -> None:
        super().setUp()
        self.client, self.service = fixture()

    def seed(self, rows: Sequence[Mapping[str, str]]) -> None:
        """Put rows on the master list exactly as typed."""
        CompanyRepository(self.client).import_rows(list(rows))

    def runner(self, engine: FakeEngine) -> WeeklyRun:
        """A runner whose resolution never reaches the network."""
        return WeeklyRun(
            self.client,
            engine=engine,
            checkpoint_path=self.checkpoint_path,
            batch_size=5,
            session_factory=lambda: None,
        )

    def test_name_and_website_alone_is_crawled(self) -> None:
        self.seed([{"company": "OPKO Health", "website": "https://www.opko.com"}])
        engine = FakeEngine()

        summary = self.runner(engine).execute(run_id="run-1")

        self.assertEqual(len(engine.crawled), 1)
        self.assertEqual(summary.companies_attempted, 1)
        self.assertEqual(summary.companies_unusable, 0)

    def test_blank_optional_fields_do_not_break_anything(self) -> None:
        self.seed(
            [
                {
                    "company": "OPKO Health",
                    "website": "https://www.opko.com",
                    "career_url": "",
                    "it_link": "",
                    "department": "",
                    "country": "",
                    "industry": "",
                }
            ]
        )
        summary = self.runner(FakeEngine()).execute(run_id="run-1")
        self.assertEqual(summary.companies_attempted, 1)

    def test_a_row_with_only_a_name_is_reported_not_crawled(self) -> None:
        """Counting it as crawled would overstate the run."""
        self.seed(
            [
                {"company": "Nameless Co", "website": ""},
                {"company": "OPKO Health", "website": "https://www.opko.com"},
            ]
        )
        engine = FakeEngine()

        summary = self.runner(engine).execute(run_id="run-1")

        self.assertEqual(len(engine.crawled), 1)
        self.assertEqual(summary.companies_unusable, 1)

        from sheets.runs import FailureRepository

        failures = FailureRepository(self.client).store.read()
        reported = [row for row in failures if row.get("company_name") == "Nameless Co"]
        self.assertEqual(len(reported), 1)
        self.assertIn("nothing to crawl", reported[0].get("detail"))

    def test_the_limit_counts_usable_companies(self) -> None:
        """--limit 5 should crawl five companies, not five rows."""
        self.seed(
            [{"company": f"Blank {index}", "website": ""} for index in range(3)]
            + [
                {"company": f"Real {index}", "website": f"https://real{index}.com"}
                for index in range(5)
            ]
        )
        engine = FakeEngine()

        self.runner(engine).execute(limit=2, run_id="run-1")

        self.assertEqual(len(engine.crawled), 2)


class TestMasterCompaniesIsUpdated(TemporaryCheckpointTest):
    """Requirement 7: what a successful crawl writes back."""

    def setUp(self) -> None:
        super().setUp()
        self.client, self.service = fixture()
        CompanyRepository(self.client).import_rows(
            [{"company": "OPKO Health", "website": "https://www.opko.com"}]
        )
        self.key = "domain:opko.com"

    def run_with(self, engine: FakeEngine, **kwargs) -> Any:
        """Execute a run with resolution kept offline."""
        return WeeklyRun(
            self.client,
            engine=engine,
            checkpoint_path=self.checkpoint_path,
            batch_size=5,
            session_factory=lambda: None,
        ).execute(**kwargs)

    def stored(self) -> Any:
        """The company row as the sheet now holds it."""
        return CompanyRepository(self.client).store.read_index("company_key")[self.key]

    def test_the_crawled_board_and_platform_are_recorded(self) -> None:
        engine = FakeEngine(
            {
                self.key: result(
                    company="OPKO Health",
                    platform=Platform.ADP_RM,
                    seed_url="https://myjobs.adp.com/opko/cx/job-listing",
                    seed_field="it_link",
                    jobs=[posting("Software Engineer", "https://myjobs.adp.com/opko/j/1")],
                )
            }
        )
        self.run_with(engine, run_id="run-1")

        row = self.stored()
        self.assertEqual(row.get("platform"), "ADP Recruiting Management")
        self.assertEqual(row.get("career_url"), "https://myjobs.adp.com/opko/cx/job-listing")

    def test_open_jobs_last_checked_and_last_outcome_are_recorded(self) -> None:
        engine = FakeEngine(
            {
                self.key: result(
                    company="OPKO Health",
                    jobs=[
                        posting("Software Engineer", "https://x/1"),
                        posting("Data Engineer", "https://x/2"),
                    ],
                )
            }
        )
        self.run_with(engine, run_id="run-1")

        row = self.stored()
        self.assertEqual(row.get("active_jobs"), "2")
        self.assertTrue(row.get("last_checked"))
        self.assertEqual(row.get("last_outcome"), "jobs")

    def test_location_and_country_come_from_the_postings(self) -> None:
        engine = FakeEngine(
            {
                self.key: result(
                    company="OPKO Health",
                    jobs=[
                        posting("Engineer", "https://x/1", location="Miami, FL"),
                        posting("Developer", "https://x/2", location="Miami, FL"),
                        posting("Analyst", "https://x/3", location="Austin, TX"),
                    ],
                )
            }
        )
        self.run_with(engine, run_id="run-1")

        row = self.stored()
        self.assertEqual(row.get("location"), "Miami, FL")
        self.assertEqual(row.get("country"), "United States")

    def test_a_department_the_boards_publish_is_recorded(self) -> None:
        engine = FakeEngine(
            {
                self.key: result(
                    company="OPKO Health",
                    jobs=[posting("Engineer", "https://x/1", department="Information Technology")],
                )
            }
        )
        self.run_with(engine, run_id="run-1")

        self.assertEqual(self.stored().get("department"), "Information Technology")

    def test_a_failed_crawl_records_the_outcome_without_inventing_urls(self) -> None:
        engine = FakeEngine(
            {self.key: result(company="OPKO Health", jobs=[], error="HTTP 403 Forbidden")}
        )
        self.run_with(engine, run_id="run-1")

        row = self.stored()
        self.assertEqual(row.get("last_outcome"), "technical failure")
        self.assertTrue(row.get("last_checked"))

    def test_a_manually_typed_field_is_not_overwritten(self) -> None:
        CompanyRepository(self.client).upsert(
            [{"company_key": self.key, "industry": "Healthcare Technology"}]
        )
        engine = FakeEngine({self.key: result(company="OPKO Health", jobs=[posting()])})

        self.run_with(engine, run_id="run-1")

        self.assertEqual(self.stored().get("industry"), "Healthcare Technology")

    def test_rerunning_the_same_company_is_idempotent(self) -> None:
        engine = FakeEngine(
            {self.key: result(company="OPKO Health", jobs=[posting("Software Engineer")])}
        )
        self.run_with(engine, run_id="run-1")

        before = {name: [list(row) for row in rows] for name, rows in self.service.tabs.items()}
        self.run_with(engine, run_id="run-1", resume=False)

        self.assertEqual(CompanyRepository(self.client).count(), 1)
        self.assertEqual(JobRepository(self.client).history.count(), 1)
        self.assertEqual(JobRepository(self.client).current.count(), 1)
        # The weekly log must not gain a second copy of the same run's changes.
        self.assertEqual(
            len(before["NEW_LAST_WEEK"]), len(self.service.tabs["NEW_LAST_WEEK"])
        )

    def test_a_dry_run_updates_no_company_row(self) -> None:
        engine = FakeEngine({self.key: result(company="OPKO Health", jobs=[posting()])})
        self.service.calls.clear()

        self.run_with(engine, run_id="run-1", dry_run=True)

        self.assertEqual(self.service.mutating_calls(), [])
        self.assertEqual(self.stored().get("last_checked"), "")
        self.assertEqual(self.stored().get("active_jobs"), "")


class TestResolutionIsSkippedWhenDiscoveryIsOff(TemporaryCheckpointTest):
    """The offline guarantee the whole test suite depends on."""

    def setUp(self) -> None:
        super().setUp()
        self.client, self.service = fixture()
        CompanyRepository(self.client).import_rows(
            [{"company": "Acme", "website": "https://acme.com"}]
        )

    def test_no_session_is_built_when_discovery_is_off(self) -> None:
        """config.settings ships inert, and the suite relies on it."""
        from config.settings import SETTINGS

        self.assertFalse(SETTINGS.discover_careers)

        built = []

        def factory():
            built.append(1)
            return None

        WeeklyRun(
            self.client,
            engine=FakeEngine(),
            checkpoint_path=self.checkpoint_path,
            session_factory=factory,
        ).execute(run_id="run-1")

        self.assertEqual(built, [], "resolution must not open a session with discovery off")

    def test_a_row_naming_a_board_resolves_without_any_request(self) -> None:
        CompanyRepository(self.client).upsert(
            [{"company_key": "domain:acme.com", "it_link": "https://boards.greenhouse.io/acme"}]
        )
        built = []

        WeeklyRun(
            self.client,
            engine=FakeEngine(),
            checkpoint_path=self.checkpoint_path,
            session_factory=lambda: built.append(1),
        ).execute(run_id="run-1")

        self.assertEqual(built, [])


class TestLiveIdempotenceConditions(TemporaryCheckpointTest):
    """Offline regressions for every condition a repeated live run was checked against.

    A real 5-company crawl was run twice against the production spreadsheet and
    28 conditions were verified against snapshots taken before and after. All 28
    held and no production code needed changing — so these tests exist to keep
    it that way, not to fix anything.

    Each test names the live condition it stands in for. The shape of the
    fixture mirrors what the real sheet contained: some companies yielding
    postings, some read successfully but empty, some failing, and — importantly
    — duplicate rows naming one company, because the live sheet has 109 rows
    covering 65 companies and that is what a hand-maintained list looks like.
    """

    def setUp(self) -> None:
        super().setUp()
        self.client, self.service = fixture()

        # 109 rows, 65 companies: the live sheet's shape in miniature.
        CompanyRepository(self.client).import_rows(
            [
                {"company": "Low Cost Interlock", "career_url": "www.lowcostinterlock.com"},
                {"company": "Behavioral Health Services", "career_url": "www.bhs-inc.org"},
                {"company": "Legacy Community Health", "career_url": "www.legacycommunityhealth.org"},
            ]
        )
        # The same company entered again, spelled differently, as the live
        # sheet has it. import_rows folds it; a hand edit would not.
        CompanyRepository(self.client).store.append(
            [
                {
                    "company_name": "Legacy Community Health ",
                    "career_url": "https://www.legacycommunityhealth.org",
                    "status": "active",
                }
            ]
        )

        self.interlock = "domain:lowcostinterlock.com"
        self.engine = FakeEngine(
            {
                self.interlock: result(
                    company="Low Cost Interlock",
                    platform=Platform.ADP,
                    seed_url="https://workforcenow.adp.com/mascsr/default/mdf/recruitment/x",
                    seed_field="it_link",
                    jobs=[
                        posting("Firmware Engineer", "https://workforcenow.adp.com/j/1"),
                        posting("Network Administrator", "https://workforcenow.adp.com/j/2"),
                        posting("Installer", "https://workforcenow.adp.com/j/3"),
                    ],
                ),
                "domain:bhs-inc.org": result(company="Behavioral Health Services", jobs=[]),
                "domain:legacycommunityhealth.org": result(
                    company="Legacy Community Health",
                    jobs=[],
                    error="AdapterHttpError: GET https://x failed: Read timed out",
                ),
            }
        )

    def run_once(self, run_id: str) -> Any:
        """One full run, with resolution kept offline."""
        return WeeklyRun(
            self.client,
            engine=self.engine,
            checkpoint_path=self.checkpoint_path,
            batch_size=5,
            session_factory=lambda: None,
        ).execute(run_id=run_id, resume=False)

    def snapshot(self) -> Dict[str, List[List[str]]]:
        """Every tab's contents, for a before-and-after comparison."""
        return {name: [list(row) for row in rows] for name, rows in self.service.tabs.items()}

    # -- conditions 1, 7, 15 -------------------------------------------------

    def test_a_replay_adds_no_company_rows(self) -> None:
        """Condition 1: MASTER_COMPANIES gains no rows."""
        self.run_once("run-1")
        before = CompanyRepository(self.client).store.count()

        self.run_once("run-2")

        self.assertEqual(CompanyRepository(self.client).store.count(), before)

    def test_company_keys_are_stable_across_runs(self) -> None:
        """Condition 7."""
        self.run_once("run-1")
        first = {r.get("company_key") for r in CompanyRepository(self.client).all()}

        self.run_once("run-2")
        second = {r.get("company_key") for r in CompanyRepository(self.client).all()}

        self.assertEqual(first, second)

    def test_duplicate_rows_naming_one_company_do_not_multiply(self) -> None:
        """Condition 15: a second spelling folds onto the same key."""
        self.run_once("run-1")
        rows = CompanyRepository(self.client).all()

        legacy = [r for r in rows if "legacy" in r.get("company_name", "").lower()]
        self.assertGreaterEqual(len(legacy), 2, "the fixture should hold two Legacy rows")
        self.assertEqual(
            {r.get("company_key") for r in legacy},
            {"domain:legacycommunityhealth.org"},
            "both rows must resolve to one key",
        )

    def test_a_duplicated_company_is_crawled_once(self) -> None:
        """Condition 15: crawling the repeat would double the work for nothing."""
        self.run_once("run-1")
        self.assertEqual(
            self.engine.crawled.count("domain:legacycommunityhealth.org"), 1
        )

    # -- conditions 2, 3, 4, 5, 6 -------------------------------------------

    def test_a_replay_reports_nothing_new_and_nothing_closed(self) -> None:
        """Conditions 4 and 11: the headline result of the live replay."""
        self.run_once("run-1")
        summary = self.run_once("run-2")

        self.assertEqual(len(summary.changes.new_jobs), 0)
        self.assertEqual(len(summary.changes.reopened_jobs), 0)
        self.assertEqual(len(summary.changes.closed_jobs), 0)
        self.assertEqual(len(summary.changes.still_active), 3)

    def test_job_history_gains_no_rows_and_no_duplicate_keys(self) -> None:
        """Conditions 3 and 6."""
        self.run_once("run-1")
        jobs = JobRepository(self.client)
        before = [r.get("job_key") for r in jobs.history.read()]

        self.run_once("run-2")
        after = [r.get("job_key") for r in jobs.history.read()]

        self.assertEqual(len(after), len(before))
        self.assertEqual(len(set(after)), len(after), "no duplicate Job Keys")
        self.assertEqual(set(after), set(before), "the key set is identical")

    def test_current_jobs_gains_no_rows_and_no_duplicate_keys(self) -> None:
        """Condition 2."""
        self.run_once("run-1")
        jobs = JobRepository(self.client)
        before = [r.get("job_key") for r in jobs.current.read()]

        self.run_once("run-2")
        after = [r.get("job_key") for r in jobs.current.read()]

        self.assertEqual(after, before)
        self.assertEqual(len(set(after)), len(after))

    def test_the_weekly_log_gains_nothing_on_a_quiet_replay(self) -> None:
        """Condition 5: no changes means no rows, even under a new run id."""
        self.run_once("run-1")
        jobs = JobRepository(self.client)
        before = len(jobs.weekly.read())

        self.run_once("run-2")

        self.assertEqual(len(jobs.weekly.read()), before)

    def test_a_tracking_parameter_appearing_later_does_not_re_key_a_job(self) -> None:
        """Condition 6, forced: the board decorates its links on the second run."""
        self.run_once("run-1")
        jobs = JobRepository(self.client)
        before = {r.get("job_key") for r in jobs.history.read()}

        self.engine.by_company[self.interlock] = result(
            company="Low Cost Interlock",
            platform=Platform.ADP,
            seed_url="https://workforcenow.adp.com/mascsr/default/mdf/recruitment/x",
            seed_field="it_link",
            jobs=[
                posting("Firmware Engineer", "https://workforcenow.adp.com/j/1?utm_source=news"),
                posting("Network Administrator", "https://www.workforcenow.adp.com/j/2/"),
                posting("Installer", "https://workforcenow.adp.com/j/3?gh_src=x"),
            ],
        )
        summary = self.run_once("run-2")

        self.assertEqual(len(summary.changes.new_jobs), 0, "tracking params must not mint new jobs")
        self.assertEqual(len(summary.changes.closed_jobs), 0)
        self.assertEqual({r.get("job_key") for r in jobs.history.read()}, before)

    # -- conditions 9, 10, 11 -----------------------------------------------

    def test_first_seen_and_first_run_never_move(self) -> None:
        """Condition 9."""
        self.run_once("run-1")
        jobs = JobRepository(self.client)
        before = {
            r.get("job_key"): (r.get("first_seen"), r.get("first_run_id"))
            for r in jobs.history.read()
        }

        self.run_once("run-2")
        after = {
            r.get("job_key"): (r.get("first_seen"), r.get("first_run_id"))
            for r in jobs.history.read()
        }

        self.assertEqual(after, before)

    def test_last_seen_advances_on_a_genuinely_later_run(self) -> None:
        """Condition 10: it must move — the opposite failure to condition 9."""
        self.run_once("run-1")
        jobs = JobRepository(self.client)
        before = {r.get("job_key"): r.get("last_seen") for r in jobs.history.read()}

        self.run_once("run-2")
        after = {r.get("job_key"): r.get("last_seen") for r in jobs.history.read()}

        self.assertEqual(set(after), set(before))
        for key, stamp in after.items():
            self.assertGreater(stamp, before[key], key)

    def test_last_run_points_at_the_newer_run(self) -> None:
        """Condition 10."""
        self.run_once("run-1")
        self.run_once("run-2")

        for record in JobRepository(self.client).history.read():
            self.assertEqual(record.get("last_run_id"), "run-2")
            self.assertEqual(record.get("first_run_id"), "run-1")

    def test_no_active_job_is_given_a_closed_at(self) -> None:
        """Condition 11."""
        self.run_once("run-1")
        self.run_once("run-2")

        for record in JobRepository(self.client).history.read():
            self.assertEqual(record.get("status"), "active")
            self.assertEqual(record.get("closed_at"), "")

    # -- condition 12 --------------------------------------------------------

    def test_a_replay_records_its_own_run_without_rewriting_the_first(self) -> None:
        """Condition 12: two runs, two rows, and history is not revised."""
        self.run_once("run-1")
        runs = RunRepository(self.client)
        first_finished = runs.get("run-1").finished_at

        self.run_once("run-2")

        self.assertEqual(len(runs.all()), 2)
        self.assertEqual(runs.get("run-1").finished_at, first_finished)
        self.assertEqual(runs.get("run-2").counts["jobs_new"], 0)

    def test_repeating_one_run_id_does_not_create_a_second_row(self) -> None:
        """Condition 12: the same logical run stays one row."""
        self.run_once("run-1")
        self.run_once("run-1")

        self.assertEqual(len(RunRepository(self.client).all()), 1)

    # -- conditions 13, 14 ---------------------------------------------------

    def test_dashboard_counts_agree_with_the_tabs_they_describe(self) -> None:
        """Condition 13: the invariant, not just the numbers."""
        from sheets.runs import DashboardRepository

        self.run_once("run-1")
        self.run_once("run-2")

        metrics = DashboardRepository(self.client).read_metrics()
        jobs = JobRepository(self.client)

        self.assertEqual(metrics["Active jobs"], str(jobs.history.count()))
        self.assertEqual(metrics["Technology jobs"], str(jobs.current.count()))
        self.assertEqual(metrics["New this week"], "0")
        self.assertEqual(metrics["Closed this week"], "0")

    def test_a_replay_writes_less_than_the_first_run(self) -> None:
        """Condition 14: an unchanged sheet costs almost nothing to confirm."""
        self.run_once("run-1")
        first = self.client.stats.cells_written

        self.client.stats.cells_written = 0
        self.run_once("run-2")
        second = self.client.stats.cells_written

        self.assertLess(second, first, f"replay wrote {second} cells against {first}")

    def test_the_only_tabs_a_quiet_replay_changes_are_the_expected_ones(self) -> None:
        """Condition 14: nothing is rewritten that did not change."""
        self.run_once("run-1")
        before = self.snapshot()

        self.run_once("run-2")
        after = self.snapshot()

        changed = {name for name in before if before[name] != after[name]}

        # A replay legitimately touches: the run log (a new run), the company
        # rows (Last Checked moves), the history and current view (Last Seen
        # moves), the dashboard (run id and duration), and the failure list
        # (Checked At moves). It must not touch anything else.
        self.assertLessEqual(
            changed,
            {
                "WEEKLY_RUNS", "MASTER_COMPANIES", "JOB_HISTORY",
                "CURRENT_JOBS", "DASHBOARD", "FAILURES",
            },
            f"unexpected tabs changed: {changed}",
        )
        self.assertNotIn("NEW_LAST_WEEK", changed, "a quiet week must add no change-log rows")
        self.assertNotIn("DISCOVERY_CONFIG", changed)
        self.assertNotIn("NEW_COMPANY_DISCOVERY", changed)

    # -- condition 16 --------------------------------------------------------

    def test_each_run_archives_its_own_checkpoint_intact(self) -> None:
        """Condition 16: no corruption, no stale active checkpoint."""
        self.run_once("run-1")
        self.run_once("run-2")

        archive = self.checkpoint_path.parent / "completed"
        archived = sorted(path.name for path in archive.glob("*.json"))

        self.assertEqual(archived, ["run-1.json", "run-2.json"])
        self.assertFalse(self.checkpoint_path.is_file(), "no stale active checkpoint")

        for path in archive.glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["completed"], len(payload["companies"]))
            self.assertTrue(payload["run_id"])

    def test_a_failed_company_stays_failed_in_the_checkpoint(self) -> None:
        """Condition 16: the distinction the closure rule rests on survives."""
        self.run_once("run-1")

        payload = json.loads(
            (self.checkpoint_path.parent / "completed" / "run-1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["companies"]["domain:legacycommunityhealth.org"], "failed")
        self.assertEqual(payload["companies"]["domain:bhs-inc.org"], "done")

    def test_a_failed_company_never_closes_its_jobs_on_replay(self) -> None:
        """Conditions 10 and 11 together, for the company that keeps failing."""
        self.run_once("run-1")
        self.run_once("run-2")
        summary = self.run_once("run-3")

        self.assertEqual(len(summary.changes.closed_jobs), 0)
        self.assertEqual(JobRepository(self.client).active_count(), 3)


class TestQuotaSafety(TemporaryCheckpointTest):
    """Google Sheets must never be read once per company."""

    def setUp(self) -> None:
        super().setUp()
        self.client, self.service = fixture()

    def seed(self, count: int) -> None:
        """Put ``count`` companies on the master list."""
        CompanyRepository(self.client).import_rows(
            [
                {"company": f"Company {index}", "website": f"company{index}.com"}
                for index in range(count)
            ]
        )

    def reads_for(self, count: int) -> int:
        """Run against ``count`` companies and return the Sheets read count."""
        self.seed(count)
        self.client.stats.reads = 0

        WeeklyRun(
            self.client,
            engine=FakeEngine(),
            checkpoint_path=self.checkpoint_path,
            batch_size=10,
        ).execute(run_id=f"run-{count}", resume=False)

        return self.client.stats.reads

    def test_reads_do_not_grow_with_the_company_count(self) -> None:
        """The whole reason the runner batches instead of iterating."""
        few = self.reads_for(5)

        # A second, much larger roster on the same spreadsheet.
        self.seed(60)
        self.client.stats.reads = 0
        WeeklyRun(
            self.client,
            engine=FakeEngine(),
            checkpoint_path=self.checkpoint_path,
            batch_size=10,
        ).execute(run_id="run-many", resume=False)
        many = self.client.stats.reads

        self.assertLessEqual(
            many,
            few + 4,
            f"reads grew from {few} to {many} — an N+1 pattern would exhaust the quota",
        )

    def test_a_whole_run_stays_well_inside_the_per_minute_quota(self) -> None:
        """Sixty reads a minute is the limit; a run must not approach it."""
        self.assertLess(self.reads_for(40), 40)

    def test_the_company_list_is_read_once(self) -> None:
        self.seed(20)
        self.service.calls.clear()

        WeeklyRun(
            self.client,
            engine=FakeEngine(),
            checkpoint_path=self.checkpoint_path,
            batch_size=5,
        ).execute(run_id="run-1", resume=False)

        master_reads = [
            call
            for kind, call in self.service.calls
            if kind in ("values.get", "values.batchGet")
            and "MASTER_COMPANIES" in str(call.get("range", "")) + str(call.get("ranges", ""))
            and "A2" in str(call.get("range", "")) + str(call.get("ranges", ""))
        ]
        self.assertLessEqual(len(master_reads), 2, "the company list should be read in bulk")

    def test_writes_are_batched_not_per_row(self) -> None:
        self.seed(30)
        self.service.calls.clear()

        WeeklyRun(
            self.client,
            engine=FakeEngine({f"domain:company{index}.com": result(
                jobs=[posting("Software Engineer", f"https://company{index}.com/jobs/1")]
            ) for index in range(30)}),
            checkpoint_path=self.checkpoint_path,
            batch_size=10,
        ).execute(run_id="run-1", resume=False)

        writes = self.service.mutating_calls()
        self.assertLess(len(writes), 30, f"{len(writes)} writes for 30 companies is per-row")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestCuratedBoardsSurviveTheWeeklyRun(TemporaryCheckpointTest):
    """A stored ``IT Link`` must outlive every way a crawl can go wrong.

    Forty-two boards in the live sheet were established by
    :mod:`crawler.ats_discovery`, validated through the engine, and written by
    hand. The weekly run refreshes ``MASTER_COMPANIES`` on every pass, so the
    question these guard is narrow and important: can a bad week overwrite a
    good answer?

    Four of those boards are iCIMS tenants behind an AWS WAF that fails every
    single week, so this is not hypothetical.
    """

    BOARD = "https://careers-acme.icims.com/jobs/search?ss=1"

    def setUp(self) -> None:
        super().setUp()
        self.client, self.service = fixture()
        companies = CompanyRepository(self.client)
        companies.import_rows(
            [{"company": "Acme", "website": "https://acme.com",
              "career_url": "https://acme.com"}]
        )
        self.key = "domain:acme.com"
        # Store a curated board, exactly as the enrichment step does.
        rows = companies.store.read_index("company_key")
        companies.store.update_rows(
            {rows[self.key].row: {"it_link": self.BOARD, "platform": "iCIMS"}}
        )

    def run_with(self, engine: FakeEngine, **kwargs) -> Any:
        """Execute a run with resolution kept offline."""
        return WeeklyRun(
            self.client,
            engine=engine,
            checkpoint_path=self.checkpoint_path,
            batch_size=5,
            session_factory=lambda: None,
        ).execute(**kwargs)

    def stored(self) -> Any:
        """The company row as the sheet now holds it."""
        return CompanyRepository(self.client).store.read_index("company_key")[self.key]

    def test_a_blocked_board_does_not_clear_the_link(self) -> None:
        """The live iCIMS case: the crawl fails every week, forever."""
        engine = FakeEngine(
            {self.key: result(
                company="Acme", platform=Platform.ICIMS, seed_url=self.BOARD,
                seed_field="it_link", jobs=[],
                error="AdapterHttpError: served an AWS WAF bot challenge",
            )}
        )
        self.run_with(engine, run_id="run-1")

        self.assertEqual(self.stored().get("it_link"), self.BOARD)

    def test_a_board_with_no_openings_does_not_clear_the_link(self) -> None:
        """Zero jobs is a fact about the company, not a reason to forget it."""
        engine = FakeEngine(
            {self.key: result(
                company="Acme", platform=Platform.ICIMS, seed_url=self.BOARD,
                seed_field="it_link", jobs=[],
            )}
        )
        self.run_with(engine, run_id="run-1")

        self.assertEqual(self.stored().get("it_link"), self.BOARD)

    def test_a_crawl_that_fell_through_to_the_website_does_not_replace_it(self) -> None:
        """The engine may end up somewhere else; that is not a better answer.

        A generic page the crawler happened to reach is not evidence that the
        curated board is wrong, so it must not take its place.
        """
        engine = FakeEngine(
            {self.key: result(
                company="Acme", platform=Platform.GENERIC_HTML,
                seed_url="https://acme.com", seed_field="website",
                jobs=[posting("Engineer", "https://acme.com/jobs/1")],
            )}
        )
        self.run_with(engine, run_id="run-1")

        self.assertEqual(self.stored().get("it_link"), self.BOARD)

    def test_the_careers_page_is_not_replaced_by_the_board(self) -> None:
        """The two columns mean different things and must stay distinct."""
        engine = FakeEngine(
            {self.key: result(
                company="Acme", platform=Platform.ICIMS, seed_url=self.BOARD,
                seed_field="it_link",
                jobs=[posting("Engineer", "https://careers-acme.icims.com/jobs/1")],
            )}
        )
        self.run_with(engine, run_id="run-1")

        self.assertEqual(self.stored().get("career_url"), "https://acme.com")

    def test_the_company_key_is_never_rewritten(self) -> None:
        engine = FakeEngine(
            {self.key: result(
                company="Acme", platform=Platform.ICIMS, seed_url=self.BOARD,
                seed_field="it_link", jobs=[],
            )}
        )
        self.run_with(engine, run_id="run-1")

        self.assertEqual(self.stored().get("company_key"), self.key)

    def test_a_curated_generic_board_survives_too(self) -> None:
        """Three live rows hold a working board whose vendor has no name.

        ``Generic HTML`` is not an ATS, so these take a different path through
        the write-back than a vendor board does. They must survive it.
        """
        companies = CompanyRepository(self.client)
        rows = companies.store.read_index("company_key")
        generic = "https://careers.kenyon.edu/jobs/search"
        companies.store.update_rows(
            {rows[self.key].row: {"it_link": generic, "platform": "Generic HTML"}}
        )

        engine = FakeEngine(
            {self.key: result(
                company="Acme", platform=Platform.GENERIC_HTML, seed_url=generic,
                seed_field="it_link", jobs=[posting("Engineer", f"{generic}/1")],
            )}
        )
        self.run_with(engine, run_id="run-1")

        self.assertEqual(self.stored().get("it_link"), generic)

    def test_a_second_identical_run_changes_nothing_about_the_link(self) -> None:
        """Idempotence, at the column that matters most."""
        engine = FakeEngine(
            {self.key: result(
                company="Acme", platform=Platform.ICIMS, seed_url=self.BOARD,
                seed_field="it_link",
                jobs=[posting("Engineer", "https://careers-acme.icims.com/jobs/1")],
            )}
        )
        self.run_with(engine, run_id="run-1")
        first = self.stored().get("it_link")
        self.run_with(engine, run_id="run-2", resume=False)

        self.assertEqual(self.stored().get("it_link"), first)


class TestDiscoveryIsNotPartOfTheWeeklyRun(unittest.TestCase):
    """Enrichment and crawling are separate commands, deliberately.

    Board discovery costs minutes per company when it has to render, and it
    writes to the same column an operator curates by hand. Folding it into the
    weekly crawl would make a five-hour run unpredictable and would put an
    automated writer behind a human one. It stays a separate entry point with
    its own dry-run and its own approval step.
    """

    def test_the_weekly_run_does_not_import_the_discovery_stage(self) -> None:
        import crawler.weekly_run as weekly

        source = Path(weekly.__file__).read_text(encoding="utf-8")
        self.assertNotIn("ats_discovery", source)

    def test_the_discovery_stage_refuses_to_write_without_apply(self) -> None:
        """Its default mode is the safe one."""
        from crawler.ats_discovery import _parse_args

        args = _parse_args(["--dry-run"])
        self.assertTrue(args.dry_run)
        self.assertFalse(args.apply)

    def test_a_mode_must_be_chosen_explicitly(self) -> None:
        """Neither writing nor reading happens by accident."""
        from crawler.ats_discovery import _parse_args

        with self.assertRaises(SystemExit):
            _parse_args([])

    def test_dry_run_and_apply_are_mutually_exclusive(self) -> None:
        from crawler.ats_discovery import _parse_args

        with self.assertRaises(SystemExit):
            _parse_args(["--dry-run", "--apply"])


class TestResumeDoesNotCloseTheEarlierSegment(TemporaryCheckpointTest):
    """A resumed run must not close what an earlier segment already saw.

    ``execute`` compares this segment's postings against the whole ledger. The
    set of companies it is allowed to close for therefore has to be the set it
    *observed this time* — not every company the checkpoint has ever recorded.
    Passing the cumulative set makes segment one's postings look like postings
    whose company was read and whose jobs have gone, which closes every one of
    them and reopens them the following week.

    The rule these hold to: **a company's jobs may only be closed by a segment
    that actually crawled that company.**
    """

    def setUp(self) -> None:
        super().setUp()
        self.client, self.service = fixture()
        CompanyRepository(self.client).import_rows(
            [
                {"company": "Alpha", "website": "alpha.com"},
                {"company": "Bravo", "website": "bravo.com"},
                {"company": "Charlie", "website": "charlie.com"},
            ]
        )
        self.alpha = "domain:alpha.com"
        self.bravo = "domain:bravo.com"
        self.charlie = "domain:charlie.com"

    def job_for(self, company: str, number: int = 1) -> Job:
        """A posting belonging to one company."""
        return Job(
            company_name=company,
            job_title=f"{company} Engineer {number}",
            job_url=f"https://boards.greenhouse.io/{company.lower()}/{number}",
            career_page_url=f"https://boards.greenhouse.io/{company.lower()}",
            platform="Greenhouse",
        )

    def engine_for(self, **jobs_by_key) -> FakeEngine:
        """An engine returning the given postings per company key."""
        return FakeEngine(
            {
                key: result(
                    company=key,
                    platform=Platform.GREENHOUSE,
                    seed_url=f"https://boards.greenhouse.io/{key}",
                    seed_field="it_link",
                    jobs=list(jobs),
                )
                for key, jobs in jobs_by_key.items()
            }
        )

    def runner(self, engine: FakeEngine) -> WeeklyRun:
        """A runner with one company per batch, so a stop lands precisely."""
        return WeeklyRun(
            self.client,
            engine=engine,
            checkpoint_path=self.checkpoint_path,
            batch_size=1,
            session_factory=lambda: None,
        )

    def run_segment(self, engine: FakeEngine, stop_after: int = 0, **kwargs):
        """Execute one segment, optionally stopping after N companies."""
        runner = self.runner(engine)
        if stop_after:
            original = runner._absorb

            def absorb(*args, **kw):
                """Stop the run once enough companies have been absorbed."""
                original(*args, **kw)
                if len(engine.crawled) >= stop_after:
                    runner.request_stop()

            runner._absorb = absorb
        return runner.execute(**kwargs)

    def history(self):
        """The job ledger, keyed by title."""
        from sheets.jobs import JobRepository

        return {
            record.get("job_title"): record
            for record in JobRepository(self.client).history.read()
            if record.get("job_key")
        }

    # -- 1 and 2: the bug, and the regression that pins it ------------------

    def test_the_first_segments_jobs_are_not_closed_on_resume(self) -> None:
        """The regression test. This is the whole point of the fix."""
        jobs = {
            self.alpha: [self.job_for("Alpha")],
            self.bravo: [self.job_for("Bravo")],
            self.charlie: [self.job_for("Charlie")],
        }

        first = self.run_segment(self.engine_for(**jobs), stop_after=1, run_id="run-1")
        self.assertTrue(first.interrupted)

        second = self.run_segment(self.engine_for(**jobs), run_id="run-1", resume=True)

        self.assertEqual(
            len(second.changes.closed_jobs),
            0,
            "a resumed segment closed jobs belonging to a company it never crawled",
        )
        self.assertEqual(self.history()["Alpha Engineer 1"].get("status"), "active")

    def test_every_posting_survives_the_interruption(self) -> None:
        jobs = {
            self.alpha: [self.job_for("Alpha")],
            self.bravo: [self.job_for("Bravo")],
            self.charlie: [self.job_for("Charlie")],
        }
        self.run_segment(self.engine_for(**jobs), stop_after=1, run_id="run-1")
        self.run_segment(self.engine_for(**jobs), run_id="run-1", resume=True)

        stored = self.history()
        self.assertEqual(len(stored), 3)
        for title in ("Alpha Engineer 1", "Bravo Engineer 1", "Charlie Engineer 1"):
            self.assertEqual(stored[title].get("status"), "active", title)

    # -- 3: an arbitrary batch boundary -------------------------------------

    def test_an_interruption_at_any_boundary_is_safe(self) -> None:
        """Not just after the first company."""
        for stop_after in (1, 2):
            with self.subTest(stop_after=stop_after):
                self.setUp()
                jobs = {
                    self.alpha: [self.job_for("Alpha")],
                    self.bravo: [self.job_for("Bravo")],
                    self.charlie: [self.job_for("Charlie")],
                }
                self.run_segment(
                    self.engine_for(**jobs), stop_after=stop_after, run_id="run-1"
                )
                second = self.run_segment(
                    self.engine_for(**jobs), run_id="run-1", resume=True
                )

                self.assertEqual(len(second.changes.closed_jobs), 0)
                self.assertEqual(len(self.history()), 3)

    # -- 4: resume where the rest succeed -----------------------------------

    def test_the_remaining_companies_are_crawled_and_recorded(self) -> None:
        jobs = {
            self.alpha: [self.job_for("Alpha")],
            self.bravo: [self.job_for("Bravo")],
            self.charlie: [self.job_for("Charlie")],
        }
        self.run_segment(self.engine_for(**jobs), stop_after=1, run_id="run-1")
        second_engine = self.engine_for(**jobs)
        second = self.run_segment(second_engine, run_id="run-1", resume=True)

        self.assertEqual(len(second_engine.crawled), 2)
        self.assertTrue(second.resumed)
        self.assertEqual(len(self.history()), 3)

    # -- 5: resume where the rest fail --------------------------------------

    def test_a_failure_in_the_resumed_segment_closes_nothing(self) -> None:
        jobs = {
            self.alpha: [self.job_for("Alpha")],
            self.bravo: [self.job_for("Bravo")],
            self.charlie: [self.job_for("Charlie")],
        }
        self.run_segment(self.engine_for(**jobs), stop_after=1, run_id="run-1")

        # Bravo and Charlie now fail outright.
        failing = FakeEngine(
            {
                key: result(
                    company=key,
                    platform=Platform.GREENHOUSE,
                    seed_url=f"https://boards.greenhouse.io/{key}",
                    seed_field="it_link",
                    jobs=[],
                    error="AdapterHttpError: 403 forbidden",
                )
                for key in (self.bravo, self.charlie)
            }
        )
        second = self.run_segment(failing, run_id="run-1", resume=True)

        self.assertEqual(len(second.changes.closed_jobs), 0)
        self.assertEqual(self.history()["Alpha Engineer 1"].get("status"), "active")

    # -- 6: incomplete results withhold closures ----------------------------

    def test_closures_are_withheld_for_companies_not_in_this_segment(self) -> None:
        """A stale posting is only closed by the segment that crawled it."""
        jobs = {
            self.alpha: [self.job_for("Alpha")],
            self.bravo: [self.job_for("Bravo")],
            self.charlie: [self.job_for("Charlie")],
        }
        self.run_segment(self.engine_for(**jobs), stop_after=1, run_id="run-1")

        # Alpha's posting is gone, but the resumed segment never reaches Alpha.
        without_alpha = {
            self.bravo: [self.job_for("Bravo")],
            self.charlie: [self.job_for("Charlie")],
        }
        second = self.run_segment(
            self.engine_for(**without_alpha), run_id="run-1", resume=True
        )

        self.assertEqual(len(second.changes.closed_jobs), 0)
        self.assertGreater(second.changes.skipped_closures, 0)
        self.assertEqual(self.history()["Alpha Engineer 1"].get("status"), "active")

    # -- 7: a resumed run equals an uninterrupted one ------------------------

    def test_a_resumed_run_closes_exactly_what_a_clean_run_closes(self) -> None:
        """The equivalence that makes resume trustworthy.

        A stale posting on every company, then a run where each board now
        advertises something different. Interrupted-then-resumed must end in
        the same ledger state as one straight-through run.
        """

        def seed_stale(client) -> None:
            """Put one now-gone posting on each company."""
            stale = {
                self.alpha: [self.job_for("Alpha", 9)],
                self.bravo: [self.job_for("Bravo", 9)],
                self.charlie: [self.job_for("Charlie", 9)],
            }
            WeeklyRun(
                client,
                engine=self.engine_for(**stale),
                checkpoint_path=self.checkpoint_path,
                batch_size=1,
                session_factory=lambda: None,
            ).execute(run_id="run-0")

        current = {
            self.alpha: [self.job_for("Alpha")],
            self.bravo: [self.job_for("Bravo")],
            self.charlie: [self.job_for("Charlie")],
        }

        # (a) straight through
        seed_stale(self.client)
        self.run_segment(self.engine_for(**current), run_id="run-1")
        clean = {title: row.get("status") for title, row in self.history().items()}

        # (b) interrupted, then resumed
        self.setUp()
        seed_stale(self.client)
        self.run_segment(self.engine_for(**current), stop_after=1, run_id="run-2")
        self.run_segment(self.engine_for(**current), run_id="run-2", resume=True)
        resumed = {title: row.get("status") for title, row in self.history().items()}

        self.assertEqual(resumed, clean)
        self.assertEqual(
            {title for title, status in clean.items() if status == "closed"},
            {"Alpha Engineer 9", "Bravo Engineer 9", "Charlie Engineer 9"},
        )

    # -- 8 and 9: the checkpoint file itself --------------------------------

    def test_a_completed_resume_removes_the_checkpoint(self) -> None:
        jobs = {
            self.alpha: [self.job_for("Alpha")],
            self.bravo: [self.job_for("Bravo")],
            self.charlie: [self.job_for("Charlie")],
        }
        self.run_segment(self.engine_for(**jobs), stop_after=1, run_id="run-1")
        self.assertTrue(self.checkpoint_path.is_file())

        self.run_segment(self.engine_for(**jobs), run_id="run-1", resume=True)

        self.assertFalse(self.checkpoint_path.is_file())
        self.assertTrue(
            (self.checkpoint_path.parent / "completed" / "run-1.json").is_file()
        )

    def test_an_interrupted_run_leaves_a_resumable_checkpoint(self) -> None:
        from crawler.checkpoint import Checkpoint

        jobs = {
            self.alpha: [self.job_for("Alpha")],
            self.bravo: [self.job_for("Bravo")],
            self.charlie: [self.job_for("Charlie")],
        }
        self.run_segment(self.engine_for(**jobs), stop_after=1, run_id="run-1")

        loaded = Checkpoint.load(self.checkpoint_path)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.run_id, "run-1")
        self.assertEqual(loaded.completed, 1)
        self.assertEqual(len(loaded.crawled_keys), 1)
