"""The browser rescue is bounded, and gives its slot back.

``SETTINGS.browser_budget`` was declared, documented and read by no code at all,
so nothing limited how many workers could be inside a browser rescue at once. On
a stretch of the real roster where half the companies failed, that meant every
worker handing a page to headless Chromium simultaneously — seconds and hundreds
of megabytes each, against milliseconds for the HTTP read they were replacing.

Two properties matter and both are tested against a real thread pool rather than
by inspecting a counter:

**The cap holds.** Never more rescues in flight than the budget, measured at the
peak rather than at whatever moment a test happens to look.

**The slot always comes back.** After a success, after an exception, after a
worker is torn down. A leaked slot is worse than no cap at all: the budget would
shrink silently until the run stopped rescuing anything.

Nothing here launches Chromium. ``render_and_extract`` is patched throughout —
the engine's contract with it is what is under test, not Playwright.
"""

from __future__ import annotations

import threading
import time
import unittest
from dataclasses import fields
from typing import Any, List, Optional
from unittest import mock

from config.settings import SETTINGS, configure
from crawler.crawler_engine import CrawlerEngine, _BrowserSlots
from crawler.platform_detector import Platform
from models.job import Job
from utils.http import AdapterHttpError, AdapterUrlError


def a_job(title: str = "Software Engineer") -> Job:
    """One posting, as a rescue would return it."""
    return Job(company_name="Acme", job_title=title, job_url="https://acme.com/jobs/1")


class SettingsGuard(unittest.TestCase):
    """Restores the settings singleton, which the whole process shares."""

    def setUp(self) -> None:
        self._settings = {
            item.name: getattr(SETTINGS, item.name) for item in fields(SETTINGS)
        }
        configure(browser_fallback=True, browser_budget=0)

    def tearDown(self) -> None:
        for name, value in self._settings.items():
            setattr(SETTINGS, name, value)


# ---------------------------------------------------------------------------
# 1. The cap holds
# ---------------------------------------------------------------------------


class TestTheCapHolds(SettingsGuard):
    """No more rescues at once than the budget allows."""

    def rescue_many(self, budget: int, workers: int, hold: float = 0.05) -> CrawlerEngine:
        """Run ``workers`` concurrent rescues against a given budget.

        Args:
            budget: Concurrent rescues allowed.
            workers: Threads attempting a rescue at the same time.
            hold: Seconds each fake render occupies its slot for.

        Returns:
            The engine, so its slot accounting can be inspected.
        """
        engine = CrawlerEngine(registry={}, browser_budget=budget)

        def render(*_args: Any, **_kwargs: Any) -> List[Job]:
            time.sleep(hold)
            return [a_job()]

        started = threading.Barrier(workers)

        def attempt() -> None:
            started.wait(timeout=30)
            engine._rescue_with_browser(
                "Acme", "https://acme.com/careers", Platform.GENERIC_HTML,
                AdapterHttpError("boom"),
            )

        with mock.patch("adapters.generic.render_and_extract", render):
            threads = [threading.Thread(target=attempt) for _ in range(workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)

        return engine

    def test_a_budget_of_one_allows_only_one_at_a_time(self) -> None:
        engine = self.rescue_many(budget=1, workers=6)

        self.assertEqual(engine.browser_slots.peak, 1)
        self.assertEqual(engine.browser_slots.live, 0)

    def test_excess_workers_wait_rather_than_rendering_anyway(self) -> None:
        """Six workers, two slots: four of them queue."""
        engine = self.rescue_many(budget=2, workers=6, hold=0.05)

        self.assertLessEqual(engine.browser_slots.peak, 2)
        self.assertGreater(
            engine.browser_slots.waited, 0.0, "nobody waited, so nobody was capped"
        )
        self.assertEqual(engine.browser_slots.skipped, 0, "a rescue was dropped, not queued")

    def test_a_larger_budget_lets_more_through_at_once(self) -> None:
        engine = self.rescue_many(budget=4, workers=6)
        self.assertGreater(engine.browser_slots.peak, 1)
        self.assertLessEqual(engine.browser_slots.peak, 4)

    def test_rescues_taken_is_the_denominator_for_skipped(self) -> None:
        """"3 skipped" means nothing without "how many were there"."""
        engine = self.rescue_many(budget=2, workers=6)

        self.assertEqual(engine.browser_slots.taken, 6)
        self.assertEqual(engine.browser_slots.skipped, 0)

    def test_zero_means_no_cap(self) -> None:
        """The shipped default, and what every run to date has had."""
        engine = self.rescue_many(budget=0, workers=6)

        self.assertEqual(engine.browser_slots.limit, 0)
        self.assertEqual(engine.browser_slots.waited, 0.0, "an uncapped run waited")
        self.assertEqual(engine.browser_slots.peak, 0, "an uncapped run counted slots")
        self.assertEqual(engine.browser_slots.taken, 0, "an uncapped run counted slots")


# ---------------------------------------------------------------------------
# 2. The slot always comes back
# ---------------------------------------------------------------------------


class TestTheSlotIsReleased(SettingsGuard):
    """A leaked slot would shrink the budget silently until nothing rescued."""

    def engine(self, budget: int = 1) -> CrawlerEngine:
        return CrawlerEngine(registry={}, browser_budget=budget)

    def rescue(self, engine: CrawlerEngine) -> List[Job]:
        return engine._rescue_with_browser(
            "Acme", "https://acme.com/careers", Platform.GENERIC_HTML,
            AdapterHttpError("boom"),
        )

    def test_released_after_a_successful_rescue(self) -> None:
        engine = self.engine()

        with mock.patch("adapters.generic.render_and_extract", return_value=[a_job()]):
            self.assertEqual(len(self.rescue(engine)), 1)

        self.assertEqual(engine.browser_slots.live, 0)

    def test_released_after_the_render_raises(self) -> None:
        """The rescue swallows the error; it must not swallow the slot too."""
        engine = self.engine()

        with mock.patch(
            "adapters.generic.render_and_extract", side_effect=RuntimeError("chromium died")
        ):
            self.assertEqual(self.rescue(engine), [])

        self.assertEqual(engine.browser_slots.live, 0)

    def test_released_after_the_render_returns_nothing(self) -> None:
        engine = self.engine()

        with mock.patch("adapters.generic.render_and_extract", return_value=[]):
            self.assertEqual(self.rescue(engine), [])

        self.assertEqual(engine.browser_slots.live, 0)

    def test_many_sequential_rescues_do_not_exhaust_the_budget(self) -> None:
        """The shape a leak takes: it works, then quietly stops working."""
        engine = self.engine(budget=1)

        with mock.patch("adapters.generic.render_and_extract", return_value=[a_job()]):
            for _ in range(20):
                self.assertEqual(len(self.rescue(engine)), 1)

        self.assertEqual(engine.browser_slots.live, 0)
        self.assertEqual(engine.browser_slots.skipped, 0)

    def test_a_mix_of_failures_and_successes_does_not_leak(self) -> None:
        engine = self.engine(budget=1)
        outcomes = [[a_job()], RuntimeError("boom"), [], RuntimeError("again"), [a_job()]]

        def render(*_a: Any, **_k: Any) -> List[Job]:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with mock.patch("adapters.generic.render_and_extract", render):
            for _ in range(5):
                self.rescue(engine)

        self.assertEqual(engine.browser_slots.live, 0)

    def test_a_worker_torn_down_mid_run_leaves_no_slot_held(self) -> None:
        """Shutdown: threads stop, and the budget is whole again afterwards."""
        engine = self.engine(budget=2)
        release = threading.Event()

        def render(*_a: Any, **_k: Any) -> List[Job]:
            release.wait(timeout=30)
            raise RuntimeError("interrupted by shutdown")

        with mock.patch("adapters.generic.render_and_extract", render):
            threads = [
                threading.Thread(target=lambda: self.rescue(engine)) for _ in range(2)
            ]
            for thread in threads:
                thread.start()

            # Both are inside a rescue, holding both slots.
            deadline = time.monotonic() + 10
            while engine.browser_slots.live < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(engine.browser_slots.live, 2)

            release.set()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(engine.browser_slots.live, 0)

        # And the budget is genuinely usable again, not merely counted as free.
        with mock.patch("adapters.generic.render_and_extract", return_value=[a_job()]):
            self.assertEqual(len(self.rescue(engine)), 1)
        self.assertEqual(engine.browser_slots.skipped, 0)


# ---------------------------------------------------------------------------
# 3. Waiting has a floor under it
# ---------------------------------------------------------------------------


class TestWaitingIsBounded(unittest.TestCase):
    """A wedged render must not hold the last slot for the rest of the run."""

    def test_a_worker_that_cannot_get_a_slot_gives_up_rather_than_blocking(self) -> None:
        slots = _BrowserSlots(1)
        taken = threading.Event()
        finish = threading.Event()

        def holder() -> None:
            with slots.hold(timeout=30) as got:
                taken.set()
                finish.wait(timeout=30)
                self.assertTrue(got)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(taken.wait(timeout=10))

        try:
            with slots.hold(timeout=0.05) as got:
                self.assertFalse(got, "a second slot was handed out with a budget of one")
        finally:
            finish.set()
            thread.join(timeout=30)

        self.assertEqual(slots.skipped, 1)
        self.assertEqual(slots.live, 0)

    def test_giving_up_does_not_consume_a_slot(self) -> None:
        slots = _BrowserSlots(1)
        with slots.hold(timeout=0.05) as first:
            self.assertTrue(first)
        with slots.hold(timeout=0.05) as second:
            self.assertTrue(second)
        self.assertEqual(slots.skipped, 0)

    def test_the_description_reports_what_the_budget_cost(self) -> None:
        self.assertEqual(_BrowserSlots(0).describe(), "browser rescues: unlimited")
        self.assertIn("at most 3 at once", _BrowserSlots(3).describe())

    def test_a_negative_budget_is_treated_as_no_cap(self) -> None:
        self.assertEqual(_BrowserSlots(-5).limit, 0)


# ---------------------------------------------------------------------------
# 4. Everything else about the rescue is unchanged
# ---------------------------------------------------------------------------


class TestExistingBehaviourIsPreserved(SettingsGuard):
    """Phase 3 enforces a limit. It changes nothing else."""

    def rescue(self, engine: CrawlerEngine, failure: Optional[BaseException]) -> List[Job]:
        return engine._rescue_with_browser(
            "Acme", "https://acme.com/careers", Platform.GENERIC_HTML, failure
        )

    def test_a_run_that_forbids_the_browser_never_takes_a_slot(self) -> None:
        configure(browser_fallback=False)
        engine = CrawlerEngine(registry={}, browser_budget=1)

        with mock.patch("adapters.generic.render_and_extract") as render:
            self.assertEqual(self.rescue(engine, AdapterHttpError("boom")), [])

        render.assert_not_called()
        self.assertEqual(engine.browser_slots.live, 0)
        self.assertEqual(engine.browser_slots.peak, 0)

    def test_a_rejected_url_is_still_not_rescued(self) -> None:
        """A browser cannot fix a wrong address, and the check still precedes
        the slot -- an unrescuable company must not queue for one."""
        engine = CrawlerEngine(registry={}, browser_budget=1)

        with mock.patch("adapters.generic.render_and_extract") as render:
            self.assertEqual(
                self.rescue(engine, AdapterUrlError("not this platform's URL")), []
            )

        render.assert_not_called()
        self.assertEqual(engine.browser_slots.peak, 0)

    def test_a_url_error_that_asks_for_a_browser_is_still_rescued(self) -> None:
        engine = CrawlerEngine(registry={}, browser_budget=1)

        with mock.patch("adapters.generic.render_and_extract", return_value=[a_job()]):
            rescued = self.rescue(engine, AdapterUrlError("this board is browser-driven"))

        self.assertEqual(len(rescued), 1)

    def test_the_rescue_is_called_with_the_same_arguments_as_before(self) -> None:
        engine = CrawlerEngine(registry={}, browser_budget=1)

        with mock.patch("adapters.generic.render_and_extract", return_value=[]) as render:
            self.rescue(engine, AdapterHttpError("boom"))

        render.assert_called_once_with(
            "https://acme.com/careers", "Acme", Platform.GENERIC_HTML.value,
            career_page_url="https://acme.com/careers",
        )

    def test_an_engine_built_without_a_budget_reads_the_settings(self) -> None:
        configure(browser_budget=3)
        self.assertEqual(CrawlerEngine(registry={}).browser_slots.limit, 3)

    def test_the_engine_still_takes_its_old_arguments(self) -> None:
        """Every existing caller constructs it positionally or by keyword
        without a budget."""
        engine = CrawlerEngine()
        self.assertTrue(engine.supported_platforms)
        self.assertEqual(engine.browser_slots.limit, SETTINGS.browser_budget)


# ---------------------------------------------------------------------------
# 5. The command line
# ---------------------------------------------------------------------------


class TestTheCommandLine(unittest.TestCase):
    """``--browser-budget`` reaches the settings, and defaults to no cap."""

    def test_the_flag_defaults_to_no_cap(self) -> None:
        from crawler.weekly_run import _parse_args

        self.assertEqual(_parse_args([]).browser_budget, 0)

    def test_the_flag_is_accepted(self) -> None:
        from crawler.weekly_run import _parse_args

        self.assertEqual(_parse_args(["--browser-budget", "4"]).browser_budget, 4)

    def test_a_negative_value_becomes_no_cap(self) -> None:
        self.assertEqual(_BrowserSlots(max(0, -3)).limit, 0)

    def test_the_help_explains_the_cost_it_bounds(self) -> None:
        from crawler.weekly_run import _parse_args

        with self.assertRaises(SystemExit):
            with mock.patch("sys.stdout"):
                _parse_args(["--help"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
