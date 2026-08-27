"""Unit tests for board resolution and the minimum viable master-sheet row.

The premise these guard is that the operator supplies two cells::

    Company Name | Website
    OPKO Health  | https://www.opko.com

and the crawler works out the rest: the careers page, the applicant tracking
system, and the tenant's actual job-board URL. Every test here runs against a
fake HTTP session, so nothing reaches the network.

:class:`TestOpkoShape` walks the exact chain the brief names — website, to
careers page, to an ADP board — because that is the case the whole stage exists
for.
"""

from __future__ import annotations

import unittest
from typing import Dict, List, Optional

from crawler.platform_detector import Platform
from crawler.resolve import Resolution, ats_link_on, is_ats, resolve_company
from utils.http import AdapterHttpError


class FakeSession:
    """Serves canned markup and records what was fetched.

    Args:
        pages: URL to the markup served for it. A URL not present raises, as an
            unreachable page would.
    """

    def __init__(self, pages: Optional[Dict[str, str]] = None) -> None:
        self.pages = dict(pages or {})
        self.fetched: List[str] = []

    def get(self, url, **kwargs):  # pragma: no cover - career_finder uses get_text
        raise NotImplementedError

    def close(self) -> None:
        """Match the session interface the runner closes."""


def markup_session(pages: Dict[str, str]) -> FakeSession:
    """A session whose pages are served through a patched ``get_text``."""
    return FakeSession(pages)


class GetTextPatch:
    """Patch ``get_text`` in both modules that resolution reaches through.

    ``crawler.resolve`` imports it directly and ``crawler.career_finder`` has
    its own reference, so a test that patches one and not the other reaches the
    network through the other.
    """

    def __init__(self, pages: Dict[str, str]) -> None:
        self.pages = pages
        self._originals = []

    def __enter__(self):
        import crawler.career_finder as finder
        import crawler.resolve as resolve

        def fake_get_text(session, url, **kwargs):
            """Serve canned markup, or fail as an unreachable page would."""
            if url in self.pages:
                return self.pages[url]
            # Also match without a trailing slash, as real sites redirect.
            trimmed = url.rstrip("/")
            if trimmed in self.pages:
                return self.pages[trimmed]
            raise AdapterHttpError(f"GET {url} returned HTTP 404")

        for module in (resolve, finder):
            self._originals.append((module, module.get_text))
            module.get_text = fake_get_text

        return self

    def __exit__(self, *_exc):
        for module, original in self._originals:
            module.get_text = original


class TestIsAts(unittest.TestCase):
    """Which platforms belong in the ``IT Link`` column."""

    def test_a_named_vendor_is_an_ats(self) -> None:
        self.assertTrue(is_ats(Platform.ADP))
        self.assertTrue(is_ats(Platform.GREENHOUSE))

    def test_generic_html_is_not(self) -> None:
        """Crawlable, but it names no vendor, so it is not a board."""
        self.assertFalse(is_ats(Platform.GENERIC_HTML))

    def test_unknown_is_not(self) -> None:
        self.assertFalse(is_ats(Platform.UNKNOWN))


class TestMinimumViableRow(unittest.TestCase):
    """Company Name plus Website has to be enough."""

    def test_a_website_only_row_resolves(self) -> None:
        pages = {
            "https://acme.com": '<a href="/careers">Careers</a>',
            "https://acme.com/careers": '<a href="https://boards.greenhouse.io/acme">Openings</a>',
        }
        with GetTextPatch(pages):
            resolution = resolve_company(
                {"company": "Acme", "website": "https://acme.com"}, session=FakeSession()
            )

        self.assertTrue(resolution.resolved)
        self.assertTrue(resolution.discovered)
        self.assertEqual(resolution.it_link, "https://boards.greenhouse.io/acme")
        self.assertIs(resolution.platform, Platform.GREENHOUSE)

    def test_every_optional_field_may_be_blank(self) -> None:
        record = {
            "company": "Acme",
            "website": "https://acme.com",
            "career_url": "",
            "it_link": "",
            "company_key": "domain:acme.com",
        }
        pages = {"https://acme.com": '<a href="https://jobs.lever.co/acme">Jobs</a>'}

        with GetTextPatch(pages):
            resolution = resolve_company(record, session=FakeSession())

        self.assertIs(resolution.platform, Platform.LEVER)
        self.assertEqual(resolution.company_key, "domain:acme.com")

    def test_a_row_with_no_website_at_all_is_unresolved(self) -> None:
        resolution = resolve_company({"company": "Acme"}, session=FakeSession())

        self.assertFalse(resolution.resolved)
        self.assertEqual(resolution.source, "unresolved")
        self.assertIn("MASTER_COMPANIES", resolution.detail)

    def test_a_website_that_names_no_careers_page_is_unresolved(self) -> None:
        with GetTextPatch({"https://acme.com": "<p>About us</p>"}):
            resolution = resolve_company(
                {"company": "Acme", "website": "https://acme.com"}, session=FakeSession()
            )

        self.assertFalse(resolution.resolved)
        self.assertIn("no careers page", resolution.detail)


class TestOpkoShape(unittest.TestCase):
    """The exact chain from the brief: website, careers page, ADP board."""

    def setUp(self) -> None:
        self.pages = {
            "https://www.opko.com": (
                '<html><footer>'
                '<a href="/about">About</a>'
                '<a href="/careers">Careers</a>'
                '</footer></html>'
            ),
            "https://www.opko.com/careers": (
                '<html><body><h1>Careers at OPKO</h1>'
                '<p>See our current openings.</p>'
                '<a href="https://myjobs.adp.com/opko/cx/job-listing">View job listings</a>'
                '</body></html>'
            ),
        }

    def test_the_board_is_discovered_from_the_website_alone(self) -> None:
        with GetTextPatch(self.pages):
            resolution = resolve_company(
                {"company": "OPKO Health", "website": "https://www.opko.com"},
                session=FakeSession(),
            )

        self.assertEqual(resolution.it_link, "https://myjobs.adp.com/opko/cx/job-listing")
        self.assertTrue(resolution.discovered)

        # ADP sells two recruiting products on two hosts, and version 2's
        # detector tells them apart: myjobs.adp.com is Recruitment Management,
        # workforcenow.adp.com is WorkforceNow. They have separate adapters, so
        # resolving to the specific product is what routes the crawl correctly.
        self.assertIs(resolution.platform, Platform.ADP_RM)
        self.assertTrue(is_ats(resolution.platform))

    def test_the_resolved_board_is_handed_to_the_engine_as_the_best_seed(self) -> None:
        """``it_link`` is the engine's first choice, so the adapter gets it."""
        with GetTextPatch(self.pages):
            resolution = resolve_company(
                {"company": "OPKO Health", "website": "https://www.opko.com"},
                session=FakeSession(),
            )

        record = resolution.to_record(
            {"company": "OPKO Health", "website": "https://www.opko.com"}
        )

        from crawler.crawler_engine import CrawlerEngine

        seed_url, seed_field, platform = CrawlerEngine.seed_candidates(record)[0]
        self.assertEqual(seed_field, "it_link")
        self.assertEqual(seed_url, "https://myjobs.adp.com/opko/cx/job-listing")
        self.assertIs(platform, Platform.ADP_RM)

        # ...and that platform has an adapter registered, which is the whole
        # point of resolving to the board rather than to the careers page.
        from crawler.crawler_engine import ADAPTER_MODULES

        self.assertEqual(ADAPTER_MODULES[platform], "adapters.adp_rm")

    def test_the_sheet_updates_name_both_urls_and_the_platform(self) -> None:
        with GetTextPatch(self.pages):
            resolution = resolve_company(
                {"company": "OPKO Health", "website": "https://www.opko.com"},
                session=FakeSession(),
            )

        updates = resolution.sheet_updates()
        self.assertEqual(updates["it_link"], "https://myjobs.adp.com/opko/cx/job-listing")
        self.assertEqual(updates["platform"], "ADP Recruiting Management")
        self.assertIn("career_url", updates)


class TestManualEntriesAreRespected(unittest.TestCase):
    """A value the operator typed is used, never second-guessed."""

    def test_an_it_link_in_the_sheet_short_circuits_discovery(self) -> None:
        session = FakeSession()
        resolution = resolve_company(
            {
                "company": "Acme",
                "website": "https://acme.com",
                "it_link": "https://boards.greenhouse.io/acme",
            },
            session=session,
        )

        self.assertEqual(resolution.source, "sheet")
        self.assertEqual(resolution.it_link, "https://boards.greenhouse.io/acme")
        self.assertEqual(session.fetched, [], "no request should be made")

    def test_a_career_url_naming_an_ats_is_used_directly(self) -> None:
        resolution = resolve_company(
            {"company": "Acme", "career_url": "https://jobs.lever.co/acme"},
            session=FakeSession(),
        )

        self.assertEqual(resolution.source, "sheet")
        self.assertIs(resolution.platform, Platform.LEVER)
        self.assertEqual(resolution.it_link, "https://jobs.lever.co/acme")

    def test_a_career_url_on_the_companys_own_site_is_kept_and_followed(self) -> None:
        """The operator's careers page stands; the board it links to is added."""
        pages = {
            "https://acme.com/careers": '<a href="https://acme.wd1.myworkdayjobs.com/External">Search</a>'
        }
        with GetTextPatch(pages):
            resolution = resolve_company(
                {"company": "Acme", "career_url": "https://acme.com/careers"},
                session=FakeSession(),
            )

        self.assertEqual(resolution.career_url, "https://acme.com/careers")
        self.assertEqual(resolution.it_link, "https://acme.wd1.myworkdayjobs.com/External")
        self.assertIs(resolution.platform, Platform.WORKDAY)

    def test_a_careers_page_linking_to_no_board_is_crawled_directly(self) -> None:
        with GetTextPatch({"https://acme.com/careers": "<p>Email us your CV</p>"}):
            resolution = resolve_company(
                {"company": "Acme", "career_url": "https://acme.com/careers"},
                session=FakeSession(),
            )

        self.assertEqual(resolution.career_url, "https://acme.com/careers")
        self.assertEqual(resolution.it_link, "")
        self.assertIs(resolution.platform, Platform.GENERIC_HTML)
        self.assertTrue(resolution.resolved)

    def test_sheet_updates_never_blank_a_field(self) -> None:
        """Blank values are omitted, so nothing overwrites a stored cell."""
        resolution = Resolution(career_url="", it_link="", platform=Platform.UNKNOWN)
        self.assertEqual(resolution.sheet_updates(), {})


class TestAtsLinkOn(unittest.TestCase):
    """Finding a board link on a page."""

    def test_it_finds_a_vendor_link(self) -> None:
        pages = {
            "https://acme.com/careers": (
                '<a href="/about">About</a>'
                '<a href="https://acme.rec.pro.ukg.net/ACM1001ACME/JobBoard/x">Openings</a>'
            )
        }
        with GetTextPatch(pages):
            found = ats_link_on("https://acme.com/careers", FakeSession())

        self.assertIn("ukg.net", found)

    def test_it_ignores_links_that_are_not_boards(self) -> None:
        pages = {"https://acme.com/careers": '<a href="/about">About</a><a href="/team">Team</a>'}
        with GetTextPatch(pages):
            self.assertEqual(ats_link_on("https://acme.com/careers", FakeSession()), "")

    def test_a_relative_link_is_resolved_against_the_page(self) -> None:
        pages = {"https://acme.com/careers": '<a href="/careersection/2/joblist.ftl">Jobs</a>'}
        with GetTextPatch(pages):
            found = ats_link_on("https://acme.com/careers", FakeSession())

        self.assertEqual(found, "https://acme.com/careersection/2/joblist.ftl")

    def test_an_unreachable_page_yields_nothing(self) -> None:
        with GetTextPatch({}):
            self.assertEqual(ats_link_on("https://acme.com/careers", FakeSession()), "")

    def test_no_session_means_no_request(self) -> None:
        self.assertEqual(ats_link_on("https://acme.com/careers", None), "")

    def test_a_blank_url_yields_nothing(self) -> None:
        self.assertEqual(ats_link_on("", FakeSession()), "")


class TestDiscoveryCanBeTurnedOff(unittest.TestCase):
    """With discovery off, only what the row already says is used."""

    def test_no_request_is_made(self) -> None:
        session = FakeSession()
        resolution = resolve_company(
            {"company": "Acme", "website": "https://acme.com"},
            session=session,
            discover=False,
        )

        self.assertEqual(session.fetched, [])
        self.assertIn("discovery is off", resolution.detail)

    def test_the_homepage_is_not_written_into_the_career_page_column(self) -> None:
        """A marketing URL in Career Page URL is a wrong answer that sticks.

        The engine already tries ``website`` as its own last-resort seed, so
        leaving the cell blank costs nothing and keeps the sheet honest.
        """
        resolution = resolve_company(
            {"company": "Acme", "website": "https://acme.com"},
            session=FakeSession(),
            discover=False,
        )

        self.assertEqual(resolution.career_url, "")
        self.assertEqual(resolution.sheet_updates(), {})

    def test_without_a_session_nothing_is_fetched(self) -> None:
        resolution = resolve_company(
            {"company": "Acme", "website": "https://acme.com"}, session=None
        )
        self.assertIn("discovery is off", resolution.detail)


class TestResolutionIsDeterministic(unittest.TestCase):
    """The same row resolves the same way every time."""

    def test_repeating_a_resolution_gives_the_same_answer(self) -> None:
        pages = {
            "https://acme.com": '<a href="/careers">Careers</a>',
            "https://acme.com/careers": '<a href="https://boards.greenhouse.io/acme">Jobs</a>',
        }
        record = {"company": "Acme", "website": "https://acme.com"}

        with GetTextPatch(pages):
            first = resolve_company(record, session=FakeSession())
            second = resolve_company(record, session=FakeSession())

        self.assertEqual(first.it_link, second.it_link)
        self.assertEqual(first.platform, second.platform)

    def test_a_resolved_row_needs_no_discovery_next_time(self) -> None:
        """Writing the board back to the sheet is what makes week two cheap."""
        session = FakeSession()
        enriched = {
            "company": "Acme",
            "website": "https://acme.com",
            "career_url": "https://acme.com/careers",
            "it_link": "https://boards.greenhouse.io/acme",
        }

        resolution = resolve_company(enriched, session=session)

        self.assertEqual(resolution.source, "sheet")
        self.assertEqual(session.fetched, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
