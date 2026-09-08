"""The benchmark measures, and cannot write.

Deciding a worker count from first principles does not work: the costs are a
browser per worker thread, pressure on whichever hosts a slice happens to
contain, and a failure rate that belongs to the roster as much as to the
crawler. So the tool exists to measure them — and the property that has to hold
before anyone points it at the real spreadsheet is that it **cannot change
anything**. That is what most of this file is about.

Nothing here runs a crawl. The measurement plumbing is exercised directly and
the safety properties are read off the module, so the suite stays offline.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from crawler.benchmark import (
    Benchmark,
    Sampler,
    chromium_processes,
    render,
    resident_bytes,
)


def a_measurement(**overrides: Any) -> Benchmark:
    """A filled-in measurement, as a real run would produce."""
    values: dict = {
        "workers": 10,
        "browser_budget": 2,
        "host_concurrency": 6,
        "companies": 200,
        "seconds": 640.0,
        "companies_attempted": 200,
        "companies_succeeded": 178,
        "companies_failed": 22,
        "observations": 5312,
        "jobs_persisted": 0,
        "blockers": {"403 forbidden": 9, "network failure": 8, "404 not found": 5},
        "http": {
            "requests": 4820, "domains": 173, "peak_active": 6,
            "waits": 214, "seconds_waiting_for_a_slot": 31.4, "max_concurrent": 6,
        },
        "busiest_hosts": [
            {"host": "jobs.smartrecruiters.com", "requests": 312,
             "peak_active": 6, "waits": 88, "seconds_waiting": 12.5},
        ],
        "peak_rss": 512 * 1024 * 1024,
        "peak_chromium": 34,
        "samples": 1280,
    }
    values.update(overrides)
    return Benchmark(**values)


# ---------------------------------------------------------------------------
# 1. It cannot write
# ---------------------------------------------------------------------------


class TestItCannotWrite(unittest.TestCase):
    """The safety properties, read off the module rather than trusted."""

    def setUp(self) -> None:
        self.source = (
            Path(__file__).resolve().parent.parent / "crawler" / "benchmark.py"
        ).read_text(encoding="utf-8")

    def test_the_run_is_always_a_dry_run(self) -> None:
        """Forced rather than defaulted: a flag could be unset by mistake."""
        self.assertIn("args.dry_run = True", self.source)
        self.assertIn("dry_run=True", self.source)

    def test_no_database_is_opened(self) -> None:
        """A dry run opens none anyway; passing None says so at the call site."""
        self.assertIn("database=None", self.source)

    def test_the_production_checkpoint_is_never_named(self) -> None:
        """It writes to a temporary directory, thrown away afterwards."""
        self.assertIn("TemporaryDirectory", self.source)
        self.assertIn("benchmark-checkpoint.json", self.source)
        self.assertNotIn("DEFAULT_CHECKPOINT_PATH", self.source)

    def test_it_does_not_resume(self) -> None:
        """Resuming would make a benchmark look like progress on the week."""
        self.assertIn("resume=False", self.source)

    def test_it_never_asks_for_a_writable_client(self) -> None:
        """`connect` authenticates read-only when dry_run is set, so Google
        itself refuses a write."""
        self.assertNotIn("read_only=False", self.source)

    def test_it_does_not_import_the_seamless_package(self) -> None:
        self.assertNotIn("seamless", self.source.lower())


# ---------------------------------------------------------------------------
# 2. Sampling
# ---------------------------------------------------------------------------


class TestSampling(unittest.TestCase):
    """Peaks, and a missing number rather than an invented one."""

    def test_resident_memory_is_a_number_or_honestly_absent(self) -> None:
        found = resident_bytes()
        if found is not None:
            self.assertGreater(found, 0)

    def test_the_chromium_count_is_a_number_or_honestly_absent(self) -> None:
        found = chromium_processes()
        if found is not None:
            self.assertGreaterEqual(found, 0)

    def test_the_sampler_records_at_least_once_around_a_block(self) -> None:
        with Sampler(interval=0.05) as sampler:
            pass
        self.assertGreaterEqual(sampler.samples, 2, "no sample either side of the block")

    def test_the_sampler_keeps_the_peak_not_the_last_value(self) -> None:
        sampler = Sampler(interval=10.0)
        with mock.patch("crawler.benchmark.resident_bytes", side_effect=[100, 900, 300]), \
             mock.patch("crawler.benchmark.chromium_processes", side_effect=[1, 12, 3]):
            for _ in range(3):
                sampler._record()

        self.assertEqual(sampler.peak_rss, 900)
        self.assertEqual(sampler.peak_chromium, 12)

    def test_a_measurement_that_fails_does_not_end_the_run(self) -> None:
        sampler = Sampler(interval=10.0)
        with mock.patch("crawler.benchmark.resident_bytes", return_value=None), \
             mock.patch("crawler.benchmark.chromium_processes", return_value=None):
            sampler._record()

        self.assertIsNone(sampler.peak_rss)
        self.assertIsNone(sampler.peak_chromium)
        self.assertEqual(sampler.samples, 1)

    def test_the_sampler_thread_stops(self) -> None:
        with Sampler(interval=0.05) as sampler:
            pass
        self.assertFalse(sampler._thread.is_alive())


# ---------------------------------------------------------------------------
# 3. The measurement
# ---------------------------------------------------------------------------


class TestTheMeasurement(unittest.TestCase):
    """The derived numbers, which are what compare two runs."""

    def test_seconds_per_company_is_the_comparable_number(self) -> None:
        measured = a_measurement(seconds=640.0, companies_attempted=200)
        self.assertAlmostEqual(measured.seconds_per_company, 3.2, places=2)

    def test_seconds_per_company_of_an_empty_run_is_zero_not_an_error(self) -> None:
        self.assertEqual(a_measurement(companies_attempted=0).seconds_per_company, 0.0)

    def test_the_failure_rate_is_a_percentage(self) -> None:
        measured = a_measurement(companies_attempted=200, companies_failed=22)
        self.assertAlmostEqual(measured.failure_rate, 11.0, places=1)

    def test_an_empty_run_has_no_failure_rate_rather_than_a_hundred_percent(self) -> None:
        self.assertEqual(a_measurement(companies_attempted=0).failure_rate, 0.0)

    def test_every_required_metric_is_carried(self) -> None:
        found = a_measurement().as_dict()

        self.assertEqual(found["configuration"]["workers"], 10)
        self.assertEqual(found["configuration"]["browser_budget"], 2)
        self.assertEqual(found["configuration"]["host_concurrency"], 6)
        self.assertEqual(found["throughput"]["seconds"], 640.0)
        self.assertEqual(found["resources"]["peak_rss_mb"], 512.0)
        self.assertEqual(found["resources"]["peak_chromium_processes"], 34)
        self.assertEqual(found["http"]["requests"], 4820)
        self.assertEqual(found["http"]["waits"], 214)
        self.assertEqual(found["outcomes"]["observations"], 5312)
        self.assertEqual(found["outcomes"]["jobs_persisted"], 0)
        self.assertEqual(found["outcomes"]["blockers"]["403 forbidden"], 9)

    def test_the_measurement_survives_a_round_trip_through_json(self) -> None:
        """Two runs are compared by saving one and reading it back."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            path.write_text(json.dumps(a_measurement().as_dict()), encoding="utf-8")
            restored = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(restored["configuration"]["workers"], 10)

    def test_missing_resources_are_null_rather_than_zero(self) -> None:
        found = a_measurement(peak_rss=None, peak_chromium=None).as_dict()
        self.assertIsNone(found["resources"]["peak_rss_mb"])
        self.assertIsNone(found["resources"]["peak_chromium_processes"])


# ---------------------------------------------------------------------------
# 4. The report
# ---------------------------------------------------------------------------


class TestTheReport(unittest.TestCase):
    """What an operator reads after a benchmark."""

    def setUp(self) -> None:
        self.text = render(a_measurement())

    def test_it_says_nothing_was_written(self) -> None:
        self.assertIn("dry run, nothing was written", self.text)

    def test_it_shows_all_three_controls(self) -> None:
        for label in ("Workers", "Browser budget", "Host concurrency"):
            self.assertIn(label, self.text)

    def test_it_points_at_the_number_to_compare(self) -> None:
        self.assertIn("Seconds per company", self.text)
        self.assertIn("compare this between runs", self.text)

    def test_it_explains_why_jobs_persisted_is_zero(self) -> None:
        """Better than a bare zero that looks like a bug."""
        self.assertIn("Jobs persisted", self.text)
        self.assertIn("dry run opens no database", self.text)

    def test_it_calls_out_the_failures_that_mean_pushback(self) -> None:
        self.assertIn("site pushed back", self.text)
        self.assertIn("watch this as workers rise", self.text)

    def test_it_reports_resources_and_the_limiter(self) -> None:
        self.assertIn("Peak resident memory", self.text)
        self.assertIn("Peak Chromium processes", self.text)
        self.assertIn("per-host limiter", self.text)
        self.assertIn("Hosts that waited longest", self.text)
        self.assertIn("jobs.smartrecruiters.com", self.text)

    def test_an_unmeasurable_resource_reads_as_unavailable(self) -> None:
        text = render(a_measurement(peak_rss=None, peak_chromium=None))
        self.assertIn("unavailable", text)

    def test_no_cap_reads_as_unlimited_rather_than_zero(self) -> None:
        text = render(a_measurement(browser_budget=0, host_concurrency=0))
        self.assertIn("unlimited", text)


# ---------------------------------------------------------------------------
# 5. The command line
# ---------------------------------------------------------------------------


class TestTheCommandLine(unittest.TestCase):
    """Defaults that make a benchmark comparable and safe."""

    def parse(self, *argv: str) -> Any:
        from crawler.benchmark import _parse_args

        return _parse_args(list(argv))

    def test_it_defaults_to_productions_worker_count(self) -> None:
        """So the first benchmark is the baseline to compare against."""
        self.assertEqual(self.parse().workers, 6)

    def test_it_defaults_to_one_production_batch(self) -> None:
        self.assertEqual(self.parse().limit, 200)

    def test_the_three_controls_are_configurable(self) -> None:
        parsed = self.parse(
            "--workers", "10", "--browser-budget", "2", "--host-concurrency", "4"
        )
        self.assertEqual(parsed.workers, 10)
        self.assertEqual(parsed.browser_budget, 2)
        self.assertEqual(parsed.host_concurrency, 4)

    def test_a_measurement_can_be_saved_for_comparison(self) -> None:
        self.assertEqual(self.parse("--json", "out.json").json, Path("out.json"))

    def test_there_is_no_flag_that_enables_writing(self) -> None:
        """The safety property is not a default that could be overridden."""
        from crawler.benchmark import _parse_args

        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                _parse_args(["--write"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
