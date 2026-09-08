"""Requests to one host are bounded; requests to different hosts are not.

Workers are concurrent *companies*, not concurrent requests to one site — but
companies share hosts, and the ones that do are the busiest in the ledger.
Raising the worker count without this raises the pressure on those hosts
one-for-one, which is how a vendor starts answering 403 instead of JSON.

Every concurrency test here **observes real threads holding real slots**, not a
counter. A test that asserts ``stats()["requests"] == 8`` proves the limiter can
count; only threads that overlap in time prove it can stop them.

The other half is the shape of the limit. Hostname, not vendor: of 5,396
distinct hostnames the crawler has seen, 608 are Workday tenants carrying 45% of
all postings, and one gate for those would be worse than no gate at all.
:class:`TestTheGateIsAHostname` is what holds that decision in place.
"""

from __future__ import annotations

import threading
import time
import unittest
from dataclasses import fields
from typing import Any, Callable, Dict, List, Optional
from unittest import mock

import requests

import utils.http as http
from config.settings import SETTINGS, configure
from crawler.ratelimit import DomainLimiter, RateLimitConfig
from utils.http import AdapterHttpError, request


class _Response:
    """The parts of a response ``request()`` touches."""

    def __init__(self, status: int = 200, body: bytes = b"{}") -> None:
        self.ok = 200 <= status < 400
        self.status_code = status
        self.content = body
        self.text = body.decode("utf-8", "replace")
        self.encoding = "utf-8"

    def json(self) -> Any:
        import json

        return json.loads(self.text)


class Overlap:
    """Records how many callers were inside a block at the same time.

    Peak rather than a sample: "never more than N at once" is a claim about the
    worst moment, and a test that looks at one moment can miss it entirely.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.live = 0
        self.peak = 0
        self.calls = 0

    def enter(self) -> None:
        with self.lock:
            self.live += 1
            self.calls += 1
            self.peak = max(self.peak, self.live)

    def leave(self) -> None:
        with self.lock:
            self.live -= 1


class LimiterTest(unittest.TestCase):
    """Restores the settings singleton and the process-wide limiter."""

    def setUp(self) -> None:
        self._settings = {
            item.name: getattr(SETTINGS, item.name) for item in fields(SETTINGS)
        }
        http.reset_host_limiter()

    def tearDown(self) -> None:
        for name, value in self._settings.items():
            setattr(SETTINGS, name, value)
        http.reset_host_limiter()

    # -- helpers -------------------------------------------------------------

    def session_that(self, behaviour: Callable[..., Any]) -> Any:
        """A session whose ``request`` runs ``behaviour``."""
        session = mock.Mock()
        session.request = behaviour
        return session

    def hammer(
        self,
        urls: List[str],
        concurrency: int,
        hold: float = 0.05,
        session: Optional[Any] = None,
    ) -> Overlap:
        """Fire one request per URL, all at once, and record the overlap.

        Args:
            urls: One URL per thread.
            concurrency: ``host_concurrency`` to configure.
            hold: Seconds each fake response takes.
            session: A session to use, or ``None`` to build a recording one.

        Returns:
            What overlapped.
        """
        configure(host_concurrency=concurrency)
        http.reset_host_limiter()
        overlap = Overlap()

        def respond(*_args: Any, **_kwargs: Any) -> _Response:
            overlap.enter()
            time.sleep(hold)
            overlap.leave()
            return _Response()

        used = session if session is not None else self.session_that(respond)
        ready = threading.Barrier(len(urls))

        def call(url: str) -> None:
            ready.wait(timeout=30)
            try:
                request(used, "GET", url)
            except AdapterHttpError:
                pass

        threads = [threading.Thread(target=call, args=(url,)) for url in urls]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        return overlap


# ---------------------------------------------------------------------------
# 1. One host is capped
# ---------------------------------------------------------------------------


class TestOneHostIsCapped(LimiterTest):
    """Concurrency to a single hostname is bounded, observably."""

    def test_eight_callers_on_one_host_never_exceed_the_limit(self) -> None:
        overlap = self.hammer(["https://one.example/x"] * 8, concurrency=2)

        self.assertEqual(overlap.calls, 8, "a request was lost")
        self.assertLessEqual(overlap.peak, 2, "more requests overlapped than allowed")
        self.assertGreater(overlap.peak, 0)

    def test_a_limit_of_one_serialises_that_host(self) -> None:
        overlap = self.hammer(["https://one.example/x"] * 5, concurrency=1)
        self.assertEqual(overlap.peak, 1)
        self.assertEqual(overlap.calls, 5)

    def test_a_larger_limit_lets_more_overlap(self) -> None:
        overlap = self.hammer(["https://one.example/x"] * 8, concurrency=4)
        self.assertGreater(overlap.peak, 2)
        self.assertLessEqual(overlap.peak, 4)

    def test_zero_disables_the_limiter_entirely(self) -> None:
        """The behaviour that existed before this was wired in."""
        overlap = self.hammer(["https://one.example/x"] * 6, concurrency=0)

        self.assertIsNone(http.host_limiter())
        self.assertGreater(overlap.peak, 1, "an unlimited run was still serialised")

    def test_waiting_is_recorded(self) -> None:
        self.hammer(["https://one.example/x"] * 6, concurrency=2)
        found = http.limiter_stats()

        self.assertEqual(found["requests"], 6)
        self.assertLessEqual(found["peak_active"], 2)
        self.assertGreater(found["waits"], 0, "nobody waited, so nothing was capped")
        self.assertGreater(found["seconds_waiting_for_a_slot"], 0.0)


# ---------------------------------------------------------------------------
# 2. Different hosts are not capped against each other
# ---------------------------------------------------------------------------


class TestDifferentHostsRunConcurrently(LimiterTest):
    """A cap that serialised the whole crawl would be worse than none."""

    def test_six_hosts_overlap_despite_a_limit_of_one_each(self) -> None:
        overlap = self.hammer(
            [f"https://host{index}.example/x" for index in range(6)], concurrency=1
        )
        self.assertGreater(overlap.peak, 1, "unrelated hosts were serialised")

    def test_one_busy_host_does_not_hold_up_another(self) -> None:
        """The property that matters: a slow vendor must not stall the rest."""
        configure(host_concurrency=1)
        http.reset_host_limiter()

        slow_started = threading.Event()
        release = threading.Event()
        fast_done = threading.Event()

        def respond(_method: str, url: str, **_kwargs: Any) -> _Response:
            if "slow" in url:
                slow_started.set()
                release.wait(timeout=30)
            return _Response()

        session = self.session_that(respond)

        def slow() -> None:
            request(session, "GET", "https://slow.example/x")

        def fast() -> None:
            self.assertTrue(slow_started.wait(timeout=10))
            request(session, "GET", "https://fast.example/x")
            fast_done.set()

        threads = [threading.Thread(target=slow), threading.Thread(target=fast)]
        for thread in threads:
            thread.start()

        try:
            self.assertTrue(
                fast_done.wait(timeout=10),
                "a fast host waited behind a slow one",
            )
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=30)

    def test_a_company_is_not_serialised_by_its_own_pagination(self) -> None:
        """The limit is per request, not per company: an adapter paging through
        a board takes and returns a slot each time rather than holding one."""
        configure(host_concurrency=2)
        http.reset_host_limiter()

        session = self.session_that(lambda *a, **k: _Response())
        for _ in range(10):
            request(session, "GET", "https://board.example/jobs?page=1")

        found = http.limiter_stats()
        self.assertEqual(found["requests"], 10)
        self.assertEqual(found["active"], 0, "a slot was still held after the calls")


# ---------------------------------------------------------------------------
# 3. The gate is a hostname
# ---------------------------------------------------------------------------


class TestTheGateIsAHostname(LimiterTest):
    """608 Workday tenants are 608 gates, not one."""

    def limiter(self, **config: Any) -> DomainLimiter:
        return DomainLimiter(RateLimitConfig(min_delay=0.0, **config))

    def test_workday_tenants_do_not_share_a_gate(self) -> None:
        limiter = self.limiter(max_concurrent=1)
        self.assertNotEqual(
            limiter.key_for("https://marmon.wd501.myworkdayjobs.com/x"),
            limiter.key_for("https://flir.wd1.myworkdayjobs.com/x"),
        )

    def test_a_genuinely_shared_host_does_share_a_gate(self) -> None:
        """SmartRecruiters really is one host for many companies, so it gets
        one gate -- without any vendor table saying so."""
        limiter = self.limiter(max_concurrent=1)
        self.assertEqual(
            limiter.key_for("https://jobs.smartrecruiters.com/AcmeCorp"),
            limiter.key_for("https://jobs.smartrecruiters.com/OtherCorp"),
        )

    def test_www_is_not_a_different_host(self) -> None:
        limiter = self.limiter(max_concurrent=1)
        self.assertEqual(
            limiter.key_for("https://www.acme.com/careers"),
            limiter.key_for("https://acme.com/careers"),
        )

    def test_vendor_grouping_is_available_but_off(self) -> None:
        self.assertFalse(RateLimitConfig().group_shared_vendors)

        grouped = self.limiter(max_concurrent=1, group_shared_vendors=True)
        self.assertEqual(
            grouped.key_for("https://marmon.wd501.myworkdayjobs.com/x"),
            grouped.key_for("https://flir.wd1.myworkdayjobs.com/x"),
        )

    def test_the_wired_limiter_does_not_group_vendors(self) -> None:
        configure(host_concurrency=4)
        http.reset_host_limiter()
        limiter = http.host_limiter()

        self.assertIsNotNone(limiter)
        self.assertFalse(limiter.config.group_shared_vendors)
        self.assertEqual(limiter.config.min_delay, 0.0, "the limiter also paces")

    def test_an_unparseable_url_is_not_gated(self) -> None:
        configure(host_concurrency=1)
        http.reset_host_limiter()
        session = self.session_that(lambda *a, **k: _Response())
        request(session, "GET", "")


# ---------------------------------------------------------------------------
# 4. Slots come back
# ---------------------------------------------------------------------------


class TestSlotsAreReleased(LimiterTest):
    """A leaked slot shrinks the limit silently until the crawl stops."""

    def setUp(self) -> None:
        super().setUp()
        configure(host_concurrency=1)
        http.reset_host_limiter()

    def test_released_after_a_successful_request(self) -> None:
        session = self.session_that(lambda *a, **k: _Response())
        for _ in range(5):
            request(session, "GET", "https://one.example/x")
        self.assertEqual(http.limiter_stats()["active"], 0)

    def test_released_after_a_transport_error(self) -> None:
        def explode(*_a: Any, **_k: Any) -> None:
            raise requests.ConnectionError("network down")

        session = self.session_that(explode)
        for _ in range(5):
            with self.assertRaises(AdapterHttpError):
                request(session, "GET", "https://one.example/x")

        self.assertEqual(http.limiter_stats()["active"], 0)

    def test_released_after_a_bad_status(self) -> None:
        session = self.session_that(lambda *a, **k: _Response(status=403, body=b"nope"))
        for _ in range(5):
            with self.assertRaises(AdapterHttpError):
                request(session, "GET", "https://one.example/x")

        self.assertEqual(http.limiter_stats()["active"], 0)

    def test_released_after_an_unexpected_exception(self) -> None:
        """Anything the session raises, not only the ones request() converts."""
        def explode(*_a: Any, **_k: Any) -> None:
            raise ZeroDivisionError("something else entirely")

        session = self.session_that(explode)
        for _ in range(3):
            with self.assertRaises(ZeroDivisionError):
                request(session, "GET", "https://one.example/x")

        self.assertEqual(http.limiter_stats()["active"], 0)

    def test_the_limit_still_works_after_many_failures(self) -> None:
        """The shape a leak takes: it works, then quietly stops working."""
        def explode(*_a: Any, **_k: Any) -> None:
            raise requests.Timeout("slow")

        session = self.session_that(explode)
        for _ in range(20):
            with self.assertRaises(AdapterHttpError):
                request(session, "GET", "https://one.example/x")

        overlap = self.hammer(["https://one.example/x"] * 4, concurrency=1)
        self.assertEqual(overlap.calls, 4, "the limiter stopped letting requests through")


# ---------------------------------------------------------------------------
# 5. No deadlock
# ---------------------------------------------------------------------------


class TestNoDeadlock(LimiterTest):
    """Twenty threads on one host must finish, not wedge."""

    def test_many_threads_on_one_host_all_complete(self) -> None:
        overlap = self.hammer(["https://one.example/x"] * 20, concurrency=3, hold=0.01)
        self.assertEqual(overlap.calls, 20)
        self.assertLessEqual(overlap.peak, 3)
        self.assertEqual(http.limiter_stats()["active"], 0)

    def test_a_mix_of_hosts_and_failures_all_complete(self) -> None:
        configure(host_concurrency=2)
        http.reset_host_limiter()

        done: List[str] = []
        lock = threading.Lock()

        def respond(_method: str, url: str, **_kwargs: Any) -> _Response:
            if "bad" in url:
                raise requests.ConnectionError("down")
            return _Response()

        session = self.session_that(respond)
        urls = [
            f"https://{'bad' if index % 3 == 0 else 'good'}{index % 4}.example/x"
            for index in range(24)
        ]

        def call(url: str) -> None:
            try:
                request(session, "GET", url)
            except AdapterHttpError:
                pass
            with lock:
                done.append(url)

        threads = [threading.Thread(target=call, args=(url,)) for url in urls]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(len(done), 24)
        self.assertEqual(http.limiter_stats()["active"], 0)


# ---------------------------------------------------------------------------
# 6. Everything else about request() is unchanged
# ---------------------------------------------------------------------------


class TestRequestBehaviourIsPreserved(LimiterTest):
    """The limiter wraps the call. It changes nothing about it."""

    def setUp(self) -> None:
        super().setUp()
        configure(host_concurrency=4)
        http.reset_host_limiter()

    def test_the_session_is_called_with_the_same_arguments(self) -> None:
        session = mock.Mock()
        session.request.return_value = _Response()

        request(
            session, "POST", "https://one.example/x",
            json_body={"a": 1}, params={"b": 2}, headers={"X": "y"},
            timeout=(1.0, 2.0),
        )

        session.request.assert_called_once_with(
            "POST", "https://one.example/x",
            json={"a": 1}, params={"b": 2}, headers={"X": "y"}, timeout=(1.0, 2.0),
        )

    def test_a_transport_error_still_becomes_an_adapter_error(self) -> None:
        session = self.session_that(
            lambda *a, **k: (_ for _ in ()).throw(requests.Timeout("slow"))
        )
        with self.assertRaises(AdapterHttpError) as caught:
            request(session, "GET", "https://one.example/x")
        self.assertIn("one.example", str(caught.exception))

    def test_an_allowed_status_is_still_returned_rather_than_raised(self) -> None:
        session = self.session_that(lambda *a, **k: _Response(status=404, body=b"gone"))
        found = request(
            session, "GET", "https://one.example/x", allow_statuses=(404,)
        )
        self.assertEqual(found.status_code, 404)

    def test_retry_policy_is_untouched(self) -> None:
        """The retries live in the session; the limiter never sees them."""
        session = http.build_session(retries=4)
        policy = session.get_adapter("https://one.example/").max_retries

        self.assertIsInstance(policy, http.BoundedRetry)
        self.assertEqual(policy.total, 3)
        self.assertEqual(set(policy.status_forcelist), {429, 500, 502, 503, 504})
        self.assertEqual(policy.max_retry_after, http.MAX_RETRY_AFTER)

    def test_the_timeout_is_passed_through_unchanged(self) -> None:
        session = mock.Mock()
        session.request.return_value = _Response()
        request(session, "GET", "https://one.example/x")
        self.assertEqual(
            session.request.call_args.kwargs["timeout"], http.DEFAULT_TIMEOUT
        )

    def test_get_text_and_get_json_still_work_through_the_limiter(self) -> None:
        session = self.session_that(lambda *a, **k: _Response(body=b'{"n": 1}'))
        self.assertEqual(http.get_json(session, "https://one.example/x"), {"n": 1})
        self.assertEqual(http.get_text(session, "https://one.example/x"), '{"n": 1}')


# ---------------------------------------------------------------------------
# 7. The three controls stay separate
# ---------------------------------------------------------------------------


class TestTheControlsAreIndependent(LimiterTest):
    """Workers, browser budget and host concurrency are three knobs."""

    def test_each_setting_is_its_own(self) -> None:
        configure(max_workers=10, browser_budget=2, host_concurrency=4)

        self.assertEqual(SETTINGS.max_workers, 10)
        self.assertEqual(SETTINGS.browser_budget, 2)
        self.assertEqual(SETTINGS.host_concurrency, 4)

    def test_the_browser_budget_does_not_touch_the_http_limiter(self) -> None:
        from crawler.crawler_engine import CrawlerEngine

        configure(browser_budget=3, host_concurrency=5)
        http.reset_host_limiter()

        self.assertEqual(CrawlerEngine(registry={}).browser_slots.limit, 3)
        self.assertEqual(http.host_limiter().config.max_concurrent, 5)

    def test_the_http_limiter_does_not_touch_the_browser_budget(self) -> None:
        from crawler.crawler_engine import CrawlerEngine

        configure(browser_budget=0, host_concurrency=1)
        http.reset_host_limiter()

        self.assertEqual(CrawlerEngine(registry={}).browser_slots.limit, 0)
        self.assertIsNotNone(http.host_limiter())

    def test_the_per_host_delay_is_still_the_engines_and_not_the_limiters(self) -> None:
        """Two mechanisms, deliberately: the engine spaces out companies, the
        limiter bounds concurrent requests. Stacking a delay here would slow
        every paginated adapter twice over."""
        configure(host_concurrency=4, per_host_delay=0.35)
        http.reset_host_limiter()

        self.assertEqual(http.host_limiter().config.min_delay, 0.0)
        self.assertEqual(SETTINGS.per_host_delay, 0.35)


# ---------------------------------------------------------------------------
# 8. The command line
# ---------------------------------------------------------------------------


class TestTheCommandLine(unittest.TestCase):
    """``--host-concurrency`` and ``--workers`` are separate and configurable."""

    def test_host_concurrency_defaults_to_six(self) -> None:
        from crawler.weekly_run import _parse_args

        self.assertEqual(_parse_args([]).host_concurrency, 6)

    def test_host_concurrency_is_accepted(self) -> None:
        from crawler.weekly_run import _parse_args

        self.assertEqual(_parse_args(["--host-concurrency", "3"]).host_concurrency, 3)
        self.assertEqual(_parse_args(["--host-concurrency", "0"]).host_concurrency, 0)

    def test_workers_is_accepted(self) -> None:
        from crawler.weekly_run import _parse_args

        self.assertEqual(_parse_args(["--workers", "10"]).workers, 10)

    def test_the_three_controls_are_three_flags(self) -> None:
        from crawler.weekly_run import _parse_args

        parsed = _parse_args(
            ["--workers", "10", "--browser-budget", "2", "--host-concurrency", "4"]
        )
        self.assertEqual(parsed.workers, 10)
        self.assertEqual(parsed.browser_budget, 2)
        self.assertEqual(parsed.host_concurrency, 4)

    def test_production_still_runs_six_workers(self) -> None:
        """The deployment is what sets production's value, and Phase 4 does not
        change it."""
        from pathlib import Path

        env = (Path(__file__).resolve().parent.parent
               / "deploy" / "careercrawler.env.example").read_text(encoding="utf-8")
        self.assertIn("CAREERCRAWLER_WORKERS=6", env)

        for unit in ("careercrawler.service", "careercrawler-resume.service"):
            text = (Path(__file__).resolve().parent.parent / "deploy" / unit).read_text(
                encoding="utf-8"
            )
            self.assertIn("Environment=CAREERCRAWLER_WORKERS=6", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
