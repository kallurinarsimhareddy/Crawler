"""Tests for turning a company website into a job board URL.

The behaviour that matters most is the ranking. A site's footer links to a
dozen plausible-sounding pages, and picking the wrong one costs the company its
listings — so the tests are mostly about which candidate wins, not merely that
one was found.
"""

from __future__ import annotations

import unittest

from config.settings import configure
from crawler.career_finder import find_careers_url, score_candidate

try:  # see tests/_fakes.py for why both spellings are needed
    from tests._fakes import FakeResponse, FakeSession, html
except ImportError:  # pragma: no cover - depends on how unittest was invoked
    from _fakes import FakeResponse, FakeSession, html

configure(browser_fallback=False, max_workers=1)


class TestScoreCandidate(unittest.TestCase):
    """A link's text, its URL and its host all count."""

    BASE = "https://acme.com/"

    def test_a_link_to_a_real_ats_outranks_everything(self) -> None:
        ats = score_candidate("Openings", "https://boards.greenhouse.io/acme", self.BASE)
        own = score_candidate("Careers", "https://acme.com/careers", self.BASE)
        self.assertGreater(ats, own)

    def test_an_exact_word_beats_a_partial_match(self) -> None:
        exact = score_candidate("Careers", "https://acme.com/careers", self.BASE)
        partial = score_candidate("Our careers philosophy", "https://acme.com/careers", self.BASE)
        self.assertGreater(exact, partial)

    def test_policy_and_news_pages_are_rejected(self) -> None:
        for text in ("Careers privacy policy", "Careers blog", "Recruitment fraud warning"):
            with self.subTest(text=text):
                self.assertEqual(score_candidate(text, "https://acme.com/careers", self.BASE), 0)

    def test_an_offsite_link_that_is_not_an_ats_is_penalised(self) -> None:
        offsite = score_candidate("Careers", "https://facebook.com/acme/careers", self.BASE)
        onsite = score_candidate("Careers", "https://acme.com/careers", self.BASE)
        self.assertGreater(onsite, offsite)

    def test_an_unrelated_link_does_not_score(self) -> None:
        self.assertEqual(score_candidate("Our products", "https://acme.com/products", self.BASE), 0)

    def test_an_empty_url_does_not_score(self) -> None:
        self.assertEqual(score_candidate("Careers", "", self.BASE), 0)


class TestFindCareersUrl(unittest.TestCase):
    """End to end, against scripted pages."""

    def test_a_homepage_link_straight_to_an_ats_is_taken_at_once(self) -> None:
        session = FakeSession(
            [
                html(
                    '<a href="/about">About</a>'
                    '<a href="https://boards.greenhouse.io/acme">Careers</a>'
                )
            ]
        )

        found = find_careers_url("https://acme.com", session)

        self.assertEqual(found, "https://boards.greenhouse.io/acme")
        self.assertEqual(len(session.requests), 1)

    def test_it_follows_an_own_site_careers_page_to_the_board_behind_it(self) -> None:
        session = FakeSession(
            [
                html('<a href="/careers">Careers</a>'),
                html('<a href="https://jobs.lever.co/acme">See our open roles</a>'),
            ]
        )

        found = find_careers_url("https://acme.com", session)

        self.assertEqual(found, "https://jobs.lever.co/acme")

    def test_it_settles_for_the_companys_own_careers_page(self) -> None:
        session = FakeSession(
            [
                html('<a href="/careers">Careers</a>'),
                html("<p>Email us your CV.</p>"),
            ]
        )

        self.assertEqual(find_careers_url("https://acme.com", session), "https://acme.com/careers")

    def test_it_probes_conventional_paths_when_nothing_links_anywhere(self) -> None:
        session = FakeSession(
            [
                html("<p>Welcome to Acme.</p>"),
                html("<h1>Open positions</h1><p>Apply now for a full-time role.</p>"),
            ]
        )

        found = find_careers_url("https://acme.com", session)

        self.assertEqual(found, "https://acme.com/careers")

    def test_a_probe_that_finds_no_listings_is_not_accepted(self) -> None:
        session = FakeSession(
            [html("<p>Welcome.</p>")] + [html("<p>Page not found.</p>") for _ in range(8)]
        )

        self.assertEqual(find_careers_url("https://acme.com", session), "")

    def test_an_unreachable_site_yields_nothing_rather_than_raising(self) -> None:
        session = FakeSession([FakeResponse(text="down", status_code=500)] * 9)

        self.assertEqual(find_careers_url("https://acme.com", session), "")

    def test_a_blank_or_unusable_website_costs_no_requests(self) -> None:
        for website in ("", "   ", "N/A", "TBD"):
            with self.subTest(website=website):
                session = FakeSession()
                self.assertEqual(find_careers_url(website, session), "")
                self.assertEqual(session.requests, [])

    def test_discovery_is_skipped_when_the_run_forbids_it(self) -> None:
        """The engine, not the finder, owns that switch — so this checks the engine."""
        from crawler.crawler_engine import CrawlerEngine

        configure(discover_careers=False)
        engine = CrawlerEngine(registry={})
        session = FakeSession()

        self.assertEqual(
            engine._discover_seed({"website": "https://acme.com"}, session)[0], ""
        )
        self.assertEqual(session.requests, [])


if __name__ == "__main__":
    unittest.main()
