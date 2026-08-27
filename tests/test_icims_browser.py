"""The browser fallback for iCIMS portals behind an AWS WAF challenge.

iCIMS fronts many tenants with an AWS WAF interstitial. It is not a wall — it
is a script that computes a token, sets a cookie and expects the visitor to
come back — so plain HTTP can never satisfy it, and four of the boards stored
in ``MASTER_COMPANIES`` return nothing over HTTP for exactly that reason.

Every test here runs against a fake session and a fake browser. Nothing reaches
the network, no browser is launched, and no spreadsheet is touched.

The rule these guard is that the browser is a **fallback**, never a
replacement: a tenant that answers over HTTP must cost no browser visit at all.
"""

from __future__ import annotations

import unittest
from typing import Dict, List, Optional

from adapters import icims
from tests._fakes import FakeSession, html
from utils.http import AdapterHttpError

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

WAF_CHALLENGE = """
<html><head><title>Human Verification</title>
<script src="https://de5282c3ca0c.eu-west-1.captcha.awswaf.com/x.js"></script>
<script>window.gokuProps = {"key":"x"}; var awsWafCookieDomainList = [".icims.com"];</script>
</head><body>Checking your browser…</body></html>
"""

def board_page(*ids: int) -> str:
    """An iCIMS search results page listing the given posting ids."""
    rows = "".join(
        f"""
        <div class="row job">
          <a href="/jobs/{job_id}/software-engineer/job">Software Engineer {job_id}</a>
          <span class="iCIMS_JobHeaderTag">Austin, TX</span>
        </div>
        """
        for job_id in ids
    )
    return f"<html><body><div class='iCIMS_JobsTable'>{rows}</div></body></html>"


EMPTY_BOARD = "<html><body><div class='iCIMS_JobsTable'></div></body></html>"


class FakeRendered:
    """Stands in for :class:`utils.browser.RenderedPage`."""

    def __init__(self, html: str, url: str = "", error: Optional[str] = None) -> None:
        self.html = html
        self.url = url
        self.status = 200
        self.headers: Dict[str, str] = {}
        self.payloads: List[object] = []
        self.requests: List[str] = []
        self.title = ""
        self.error = error

    @property
    def ok(self) -> bool:
        """Match the real page's contract."""
        return self.error is None and bool(self.html)


class BrowserPatch:
    """Replace the adapter's browser with canned pages, counting visits."""

    def __init__(self, pages: Optional[Dict[str, FakeRendered]] = None) -> None:
        self.pages = dict(pages or {})
        self.visited: List[str] = []
        self._original = None

    def __enter__(self) -> "BrowserPatch":
        def fake_render(url, **_kwargs):
            """Serve a canned rendered page."""
            self.visited.append(url)
            return self.pages.get(url)

        self._original = icims.render_page
        icims.render_page = fake_render
        return self

    def __exit__(self, *_exc) -> None:
        icims.render_page = self._original


def blocked_session(pages: int = 4) -> FakeSession:
    """A session that answers every request with the WAF challenge.

    iCIMS serves the interstitial with a 403, and ``fetch_jobs`` deliberately
    lets that body through so the challenge can be recognised rather than
    reported as a bare HTTP error.
    """
    return FakeSession([html(WAF_CHALLENGE) for _ in range(pages)])


def readable_session(*bodies: str) -> FakeSession:
    """A session that serves real board pages, then an empty one."""
    return FakeSession([html(body) for body in bodies] + [html(EMPTY_BOARD)])


def search_url(page: int, host: str = "careers-acme.icims.com") -> str:
    """The URL the adapter uses for one results page."""
    return f"https://{host}/jobs/search?ss=1&in_iframe=1&pr={page}"


PORTAL = "https://careers-acme.icims.com/jobs/intro"


# --------------------------------------------------------------------------
# The challenge is recognised, not parsed
# --------------------------------------------------------------------------


class TestChallengeIsRecognised(unittest.TestCase):
    """A challenge page must never be mistaken for an empty board."""

    def test_the_challenge_is_detected(self) -> None:
        self.assertTrue(icims.looks_like_a_challenge(WAF_CHALLENGE))

    def test_a_real_board_is_not_a_challenge(self) -> None:
        self.assertFalse(icims.looks_like_a_challenge(board_page(1, 2)))

    def test_an_empty_board_is_not_a_challenge(self) -> None:
        """No jobs is a fact about the company, not a blocker."""
        self.assertFalse(icims.looks_like_a_challenge(EMPTY_BOARD))

    def test_extraction_refuses_a_challenge_page(self) -> None:
        with self.assertRaises(AdapterHttpError):
            icims._extract_rows(WAF_CHALLENGE, search_url(0), "Acme", PORTAL)


# --------------------------------------------------------------------------
# The browser is a fallback, never a replacement
# --------------------------------------------------------------------------


class TestBrowserIsOnlyAFallback(unittest.TestCase):
    """A tenant that answers over HTTP must cost nothing extra."""

    def test_a_readable_portal_never_opens_a_browser(self) -> None:
        session = readable_session(board_page(1, 2))
        with BrowserPatch() as browser:
            jobs = icims.fetch_jobs(PORTAL, "Acme", session=session)

        self.assertEqual(len(jobs), 2)
        self.assertEqual(browser.visited, [], "HTTP succeeded; the browser must stay shut")

    def test_the_http_path_is_tried_first(self) -> None:
        session = readable_session(board_page(7))
        with BrowserPatch():
            icims.fetch_jobs(PORTAL, "Acme", session=session)
        self.assertTrue(session.requests, "the HTTP path must still run")

    def test_without_a_browser_a_blocked_portal_still_raises(self) -> None:
        """The existing contract for callers that cannot render."""
        session = blocked_session()
        with BrowserPatch({}):
            with self.assertRaises(AdapterHttpError):
                icims.fetch_jobs(PORTAL, "Acme", session=session)


# --------------------------------------------------------------------------
# The fallback itself
# --------------------------------------------------------------------------


class TestBrowserFallback(unittest.TestCase):
    """What the browser recovers that HTTP cannot."""

    def test_a_blocked_portal_is_read_in_the_browser(self) -> None:
        session = blocked_session()
        rendered = {
            search_url(0): FakeRendered(board_page(1, 2, 3), url=search_url(0)),
            search_url(1): FakeRendered(EMPTY_BOARD, url=search_url(1)),
        }
        with BrowserPatch(rendered) as browser:
            jobs = icims.fetch_jobs(PORTAL, "Acme", session=session)

        self.assertEqual(len(jobs), 3)
        self.assertTrue(browser.visited)

    def test_jobs_carry_the_portal_and_platform(self) -> None:
        session = blocked_session()
        rendered = {
            search_url(0): FakeRendered(board_page(5), url=search_url(0)),
            search_url(1): FakeRendered(EMPTY_BOARD, url=search_url(1)),
        }
        with BrowserPatch(rendered):
            jobs = icims.fetch_jobs(PORTAL, "Acme", session=session)

        self.assertEqual(jobs[0].platform, "iCIMS")
        self.assertEqual(jobs[0].company_name, "Acme")
        self.assertIn("/jobs/5/", jobs[0].job_url)

    def test_the_rendered_url_resolves_relative_links(self) -> None:
        """A redirect must not strand every posting URL on the wrong host."""
        session = blocked_session()
        landed = "https://careers-acme.icims.com/jobs/search?ss=1&in_iframe=1&pr=0&redirected=1"
        rendered = {
            search_url(0): FakeRendered(board_page(9), url=landed),
            search_url(1): FakeRendered(EMPTY_BOARD, url=search_url(1)),
        }
        with BrowserPatch(rendered):
            jobs = icims.fetch_jobs(PORTAL, "Acme", session=session)

        self.assertTrue(jobs[0].job_url.startswith("https://careers-acme.icims.com/jobs/9/"))

    def test_pages_are_walked_until_one_yields_nothing_new(self) -> None:
        session = blocked_session()
        rendered = {
            search_url(0): FakeRendered(board_page(1, 2), url=search_url(0)),
            search_url(1): FakeRendered(board_page(3, 4), url=search_url(1)),
            search_url(2): FakeRendered(EMPTY_BOARD, url=search_url(2)),
        }
        with BrowserPatch(rendered) as browser:
            jobs = icims.fetch_jobs(PORTAL, "Acme", session=session)

        self.assertEqual(len(jobs), 4)
        self.assertEqual(len(browser.visited), 3)

    def test_repeated_postings_across_pages_are_deduplicated(self) -> None:
        """A board that loops must not multiply its postings."""
        session = blocked_session()
        rendered = {
            search_url(0): FakeRendered(board_page(1, 2), url=search_url(0)),
            search_url(1): FakeRendered(board_page(2, 1), url=search_url(1)),
            search_url(2): FakeRendered(EMPTY_BOARD, url=search_url(2)),
        }
        with BrowserPatch(rendered):
            jobs = icims.fetch_jobs(PORTAL, "Acme", session=session)

        self.assertEqual(len(jobs), 2)
        self.assertEqual(len({job.job_url for job in jobs}), 2)


# --------------------------------------------------------------------------
# Bounded, and honest when it fails
# --------------------------------------------------------------------------


class TestFallbackIsBounded(unittest.TestCase):
    """A browser visit costs seconds; a board that pages forever cannot hang."""

    def test_visits_are_capped_per_company(self) -> None:
        session = blocked_session()
        # Every page yields new postings, so only the cap can stop it.
        rendered = {
            search_url(page): FakeRendered(
                board_page(*range(page * 10, page * 10 + 5)), url=search_url(page)
            )
            for page in range(icims.MAX_RENDER_PAGES + 10)
        }
        with BrowserPatch(rendered) as browser:
            icims.fetch_jobs(PORTAL, "Acme", session=session)

        self.assertLessEqual(len(browser.visited), icims.MAX_RENDER_PAGES)


class TestFallbackReportsRatherThanGuesses(unittest.TestCase):
    """The safety rule: no board is invented when the browser cannot read one."""

    def test_a_challenge_that_survives_the_browser_still_raises(self) -> None:
        session = blocked_session()
        rendered = {search_url(0): FakeRendered(WAF_CHALLENGE, url=search_url(0))}
        with BrowserPatch(rendered):
            with self.assertRaises(AdapterHttpError) as caught:
                icims.fetch_jobs(PORTAL, "Acme", session=session)

        self.assertIn("bot challenge", str(caught.exception).lower())

    def test_a_browser_that_returns_nothing_still_raises(self) -> None:
        session = blocked_session()
        with BrowserPatch({search_url(0): FakeRendered("", error="no browser")}):
            with self.assertRaises(AdapterHttpError):
                icims.fetch_jobs(PORTAL, "Acme", session=session)

    def test_a_browser_that_raises_does_not_escape(self) -> None:
        """A browser failure must surface as the original blocker, not a crash."""
        session = blocked_session()

        def explode(*_args, **_kwargs):
            raise RuntimeError("playwright is not installed")

        original = icims.render_page
        icims.render_page = explode
        try:
            with self.assertRaises(AdapterHttpError):
                icims.fetch_jobs(PORTAL, "Acme", session=session)
        finally:
            icims.render_page = original

    def test_an_empty_board_read_in_the_browser_is_not_an_error(self) -> None:
        """Zero jobs is a fact, and different from being blocked."""
        session = blocked_session()
        with BrowserPatch({search_url(0): FakeRendered(EMPTY_BOARD, url=search_url(0))}):
            jobs = icims.fetch_jobs(PORTAL, "Acme", session=session)

        self.assertEqual(jobs, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
