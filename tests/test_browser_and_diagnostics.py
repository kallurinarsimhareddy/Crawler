"""Tests for the browser layer and the unknown-platform diagnostics.

Neither is exercised by launching Chromium — that would make the suite slow and
put it on the network. What is tested instead is everything around the browser:
that a machine without Playwright degrades quietly rather than crashing, that a
CAPTCHA is distinguished from a self-clearing challenge, that a rendered page is
mined for its XHR before its DOM, and that the diagnostics honour their run cap
and never raise.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

import utils.browser
from config.settings import SETTINGS, configure
from crawler import diagnostics
from utils.browser import (
    RenderedPage,
    _capture,
    _clear_challenge,
    _is_captcha,
    _is_challenge,
    close_current_thread,
    render,
)

configure(browser_fallback=False, max_workers=1)


class FakePage:
    """The handful of Playwright page methods the challenge logic touches.

    Args:
        titles: Title to report on each successive call, so a test can model a
            page that clears after a reload.
        content: Markup to report, likewise.
    """

    def __init__(self, titles: List[str], content: List[str]) -> None:
        self._titles = list(titles)
        self._content = list(content)
        self.url = "https://acme.com/careers"
        self.reloads = 0
        self.waited = 0

    def title(self) -> str:
        """Return the next scripted title, repeating the last one."""
        return self._titles[0] if len(self._titles) == 1 else self._titles.pop(0)

    def content(self) -> str:
        """Return the next scripted markup, repeating the last one."""
        return self._content[0] if len(self._content) == 1 else self._content.pop(0)

    def wait_for_timeout(self, milliseconds: int) -> None:
        """Record a wait without actually sleeping."""
        self.waited += milliseconds

    def reload(self, **kwargs: Any) -> None:
        """Record a reload."""
        self.reloads += 1

    def wait_for_load_state(self, *args: Any, **kwargs: Any) -> None:
        """Do nothing; the fake page is always settled."""


class FakeHttpSession:
    """A minimal ``requests``-shaped session returning one page of markup.

    Args:
        body: Markup to place inside ``<body>``.
    """

    def __init__(self, body: str) -> None:
        self._body = f"<html><body>{body}</body></html>"
        self.requests: List[str] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Return the scripted page, whatever is asked for.

        Args:
            method: HTTP method, ignored.
            url: Requested URL, recorded.
            **kwargs: Ignored.

        Returns:
            A response-shaped object.
        """
        self.requests.append(url)
        body = self._body
        session = self

        class _Response:
            status_code = 200
            encoding = "utf-8"
            headers: Dict[str, str] = {"content-type": "text/html"}
            text = body

            @property
            def ok(self) -> bool:
                return True

            @property
            def content(self) -> bytes:
                return body.encode("utf-8")

            def json(self) -> Any:
                raise ValueError("no JSON")

        # Only the first page has links; a second request ends the pagination.
        if len(session.requests) > 1:
            _Response.text = "<html><body><p>No more</p></body></html>"
        return _Response()

    def close(self) -> None:
        """Match the session interface."""


class FakeResponseObject:
    """The parts of a Playwright response that :func:`_capture` reads."""

    def __init__(self, url: str, headers: Dict[str, str], body: bytes) -> None:
        self.url = url
        self.headers = headers
        self._body = body

    def body(self) -> bytes:
        """Return the response body."""
        return self._body


class TestChallengeDetection(unittest.TestCase):
    """A self-clearing challenge and a human CAPTCHA need different handling."""

    def test_a_waf_title_is_a_challenge(self) -> None:
        page = FakePage(["Human Verification"], ["<html></html>"])
        self.assertTrue(_is_challenge(page))

    def test_a_cloudflare_title_is_a_challenge(self) -> None:
        page = FakePage(["Just a moment..."], ["<html></html>"])
        self.assertTrue(_is_challenge(page))

    def test_challenge_markup_counts_even_without_the_title(self) -> None:
        page = FakePage(["Careers"], ['<script src="https://x.awswaf.com/challenge.js">'])
        self.assertTrue(_is_challenge(page))

    def test_an_ordinary_board_is_not_a_challenge(self) -> None:
        page = FakePage(["Careers at Acme"], ["<html><body>Engineer</body></html>"])
        self.assertFalse(_is_challenge(page))

    def test_a_captcha_is_recognised(self) -> None:
        for markup in (
            '<script src="https://x.captcha.awswaf.com/captcha.js">',
            '<div id="captcha"></div>',
            '<div class="g-recaptcha"></div>',
        ):
            with self.subTest(markup=markup[:30]):
                self.assertTrue(_is_captcha(FakePage(["x"], [markup])))

    def test_a_challenge_page_is_not_automatically_a_captcha(self) -> None:
        page = FakePage(["Just a moment..."], ['<script src="/cdn-cgi/challenge-platform/x.js">'])
        self.assertFalse(_is_captcha(page))


class TestClearChallenge(unittest.TestCase):
    """Wait out what can clear; abandon what cannot."""

    def test_a_page_that_is_already_fine_costs_nothing(self) -> None:
        page = FakePage(["Careers"], ["<html>ok</html>"])

        self.assertTrue(_clear_challenge(page))
        self.assertEqual(page.reloads, 0)
        self.assertEqual(page.waited, 0)

    def test_a_challenge_that_clears_after_a_reload_is_reported_cleared(self) -> None:
        page = FakePage(
            ["Human Verification", "Careers at Acme"],
            ['<script src="https://x.awswaf.com/challenge.js">', "<html>jobs</html>"],
        )

        self.assertTrue(_clear_challenge(page, wait_ms=1))
        self.assertEqual(page.reloads, 1)

    def test_a_captcha_is_abandoned_without_a_single_reload(self) -> None:
        page = FakePage(
            ["Human Verification"],
            ['<script src="https://x.captcha.awswaf.com/captcha.js"></script><div id="captcha">'],
        )

        self.assertFalse(_clear_challenge(page, wait_ms=1))
        self.assertEqual(page.reloads, 0)

    def test_a_challenge_that_never_clears_gives_up_after_its_attempts(self) -> None:
        page = FakePage(["Just a moment..."], ["<html>cf-browser-verification</html>"])

        self.assertFalse(_clear_challenge(page, attempts=2, wait_ms=1))
        self.assertEqual(page.reloads, 2)


class TestCapture(unittest.TestCase):
    """The network log keeps JSON and nothing else."""

    def test_a_json_response_is_decoded_and_kept(self) -> None:
        payloads: List[Any] = []
        requests: List[str] = []

        _capture(
            FakeResponseObject(
                "https://acme.com/api/jobs",
                {"content-type": "application/json"},
                json.dumps({"jobs": [1]}).encode(),
            ),
            payloads,
            requests,
        )

        self.assertEqual(payloads, [{"jobs": [1]}])
        self.assertEqual(requests, ["https://acme.com/api/jobs"])

    def test_html_is_logged_but_not_decoded(self) -> None:
        payloads: List[Any] = []
        requests: List[str] = []

        _capture(
            FakeResponseObject("https://acme.com/careers", {"content-type": "text/html"}, b"<html>"),
            payloads,
            requests,
        )

        self.assertEqual(payloads, [])
        self.assertEqual(requests, ["https://acme.com/careers"])

    def test_a_json_body_that_will_not_decode_is_skipped(self) -> None:
        payloads: List[Any] = []
        _capture(
            FakeResponseObject("https://acme.com/api", {"content-type": "application/json"}, b"{bad"),
            payloads,
            [],
        )
        self.assertEqual(payloads, [])


class TestRenderedPageExtraction(unittest.TestCase):
    """A rendered page's own XHR beats the DOM it produced."""

    def test_captured_json_is_preferred_over_the_dom(self) -> None:
        from adapters.generic import jobs_from_rendered_page

        page = RenderedPage(
            url="https://acme.com/careers",
            html='<li><a href="/job/9">From The Dom</a></li>' * 3,
            payloads=[
                {
                    "jobs": [
                        {"title": "From The Api", "url": "/j/1", "location": "Austin, TX",
                         "department": "IT"},
                        {"title": "Also From The Api", "url": "/j/2", "location": "Austin, TX",
                         "department": "IT"},
                    ]
                }
            ],
        )

        jobs = jobs_from_rendered_page(page, "Acme", "Generic HTML")

        self.assertEqual([job.job_title for job in jobs], ["From The Api", "Also From The Api"])

    def test_the_dom_is_used_when_no_xhr_carried_jobs(self) -> None:
        from adapters.generic import jobs_from_rendered_page

        page = RenderedPage(
            url="https://acme.com/careers",
            html="".join(f'<li><a href="/job/{i}">Engineer {i}</a></li>' for i in range(3)),
            payloads=[{"unrelated": {"telemetry": True}}],
        )

        jobs = jobs_from_rendered_page(page, "Acme", "Generic HTML")

        self.assertEqual(len(jobs), 3)

    def test_a_failed_render_yields_nothing(self) -> None:
        from adapters.generic import jobs_from_rendered_page

        self.assertEqual(jobs_from_rendered_page(None, "Acme"), [])
        self.assertEqual(
            jobs_from_rendered_page(RenderedPage(error="TimeoutError: gone"), "Acme"), []
        )


class _BlockPlaywright:
    """An import hook that makes ``import playwright`` fail.

    Used to prove the crawler still runs on a machine where the optional
    browser dependency was never installed — which is the documented promise
    and would otherwise only ever be tested by someone hitting it in the wild.
    """

    def find_module(self, name: str, path: Any = None) -> Any:  # pragma: no cover - legacy hook
        return self.find_spec(name, path)

    def find_spec(self, name: str, path: Any = None, target: Any = None) -> Any:
        if name == "playwright" or name.startswith("playwright."):
            raise ImportError(f"No module named {name!r} (blocked by the test)")
        return None


class TestWithoutPlaywright(unittest.TestCase):
    """The optional dependency really is optional."""

    def setUp(self) -> None:
        self._hook = _BlockPlaywright()
        self._saved = utils.browser.BROWSER_AVAILABLE
        sys.meta_path.insert(0, self._hook)
        # The availability probe caches its answer, and each thread remembers
        # a failed launch; both have to be cleared for the probe to run again.
        utils.browser.BROWSER_AVAILABLE = None
        utils.browser._LOCAL.__dict__.clear()
        for module in [name for name in sys.modules if name.startswith("playwright")]:
            del sys.modules[module]

    def tearDown(self) -> None:
        sys.meta_path.remove(self._hook)
        utils.browser.BROWSER_AVAILABLE = self._saved
        utils.browser._LOCAL.__dict__.clear()

    def test_availability_reports_false(self) -> None:
        self.assertFalse(utils.browser.browser_available())

    def test_render_returns_none_rather_than_raising(self) -> None:
        configure(browser_fallback=True)
        try:
            self.assertIsNone(render("https://acme.com/careers"))
        finally:
            configure(browser_fallback=False)

    def test_the_generic_adapter_still_extracts_over_plain_http(self) -> None:
        from adapters.generic import fetch_jobs

        configure(browser_fallback=True)
        try:
            session = FakeHttpSession(
                "".join(f'<li><a href="/job/{i}">Engineer {i}</a></li>' for i in range(3))
            )
            jobs = fetch_jobs("https://acme.com/careers", "Acme", session=session)
        finally:
            configure(browser_fallback=False)

        self.assertEqual(len(jobs), 3)

    def test_a_board_that_needs_a_browser_reports_no_jobs_not_a_crash(self) -> None:
        from adapters.generic import render_and_extract

        configure(browser_fallback=True)
        try:
            self.assertEqual(render_and_extract("https://acme.com/careers", "Acme"), [])
        finally:
            configure(browser_fallback=False)

    def test_the_engines_browser_rescue_is_a_no_op(self) -> None:
        from crawler.crawler_engine import CrawlerEngine
        from crawler.platform_detector import Platform

        def always_fails(career_url: str, company_name: str, session: Any = None) -> List[Any]:
            raise RuntimeError("blocked")

        configure(browser_fallback=True)
        try:
            engine = CrawlerEngine(registry={Platform.GENERIC_HTML: always_fails})
            result = engine.crawl_company({"company": "Acme", "career_url": "https://acme.com/x"})
        finally:
            configure(browser_fallback=False)

        self.assertIn("blocked", result.error or "")
        self.assertEqual(result.jobs, [])


class TestBrowserDegradation(unittest.TestCase):
    """A machine without Playwright must simply do less, not fail."""

    def test_render_declines_an_empty_url_without_launching_anything(self) -> None:
        self.assertIsNone(render(""))
        self.assertIsNone(render("   "))

    def test_closing_a_thread_that_never_opened_a_browser_is_safe(self) -> None:
        close_current_thread()
        close_current_thread()

    def test_render_and_extract_returns_nothing_when_the_run_forbids_it(self) -> None:
        from adapters.generic import render_and_extract

        configure(browser_fallback=False)
        self.assertEqual(render_and_extract("https://acme.com/careers", "Acme"), [])

    def test_a_rendered_page_reports_whether_it_is_usable(self) -> None:
        self.assertFalse(RenderedPage().ok)
        self.assertFalse(RenderedPage(html="<html/>", error="boom").ok)
        self.assertTrue(RenderedPage(html="<html/>").ok)


class TestDiagnostics(unittest.TestCase):
    """Evidence dumps are bounded, and never break a run."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name)
        configure(diagnostics=True, diagnostics_dir=self.path, diagnostics_limit=3, browser_fallback=False)
        diagnostics.reset()

    def tearDown(self) -> None:
        configure(diagnostics=False, diagnostics_dir=SETTINGS.diagnostics_dir)
        self._directory.cleanup()

    def test_it_writes_a_report_and_the_served_markup(self) -> None:
        record = diagnostics.record_unknown(
            company="Acme Corp",
            url="https://acme.com/careers",
            platform="Generic HTML",
            error="no postings found",
            markup='<html><script src="/static/app.js"></script></html>',
        )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertIn("report.md", record.files)
        self.assertIn("page.html", record.files)
        self.assertTrue((record.directory / "report.md").is_file())
        self.assertIn("Acme Corp", (record.directory / "report.md").read_text(encoding="utf-8"))

    def test_the_directory_name_is_derived_from_the_company(self) -> None:
        record = diagnostics.record_unknown("Acme Corp & Sons, Inc.", "https://acme.com/")
        assert record is not None
        self.assertEqual(record.directory.name, "acme-corp-sons-inc")

    def test_it_indexes_every_company_it_records(self) -> None:
        diagnostics.record_unknown("First", "https://one.com/")
        diagnostics.record_unknown("Second", "https://two.com/")

        index = (self.path / "index.csv").read_text(encoding="utf-8-sig")

        self.assertIn("First", index)
        self.assertIn("Second", index)

    def test_the_run_cap_is_honoured(self) -> None:
        written = [
            diagnostics.record_unknown(f"Company {index}", f"https://{index}.com/")
            for index in range(6)
        ]

        self.assertEqual(sum(1 for record in written if record is not None), 3)

    def test_reset_restores_the_cap(self) -> None:
        for index in range(3):
            diagnostics.record_unknown(f"Company {index}", f"https://{index}.com/")
        self.assertIsNone(diagnostics.record_unknown("Extra", "https://x.com/"))

        diagnostics.reset()

        self.assertIsNotNone(diagnostics.record_unknown("After Reset", "https://y.com/"))

    def test_nothing_is_written_when_diagnostics_are_off(self) -> None:
        configure(diagnostics=False)
        self.assertIsNone(diagnostics.record_unknown("Acme", "https://acme.com/"))

    def test_endpoints_found_in_the_markup_are_recorded(self) -> None:
        record = diagnostics.record_unknown(
            "Acme",
            "https://acme.com/careers",
            markup='<script>fetch("/api/v1/openings")</script>',
        )
        assert record is not None

        payload = json.loads((record.directory / "endpoints.json").read_text(encoding="utf-8"))

        self.assertIn("/api/v1/openings", payload["paths"])


if __name__ == "__main__":
    unittest.main()
