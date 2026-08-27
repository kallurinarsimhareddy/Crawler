"""Unit tests for filling in the ``IT Link`` column.

Every test here runs against canned markup through a patched ``get_text``, so
nothing reaches the network and nothing reaches Google.

The rules this stage has to obey are narrow and easy to break, so each one gets
its own class:

* :class:`TestExistingLinksAreAuthoritative` — a stored board is never
  second-guessed.
* :class:`TestNeverGuesses` — a board that cannot be named is left alone, with
  a reason.
* :class:`TestSheetUpdatesStayInTheirColumn` — an ATS URL never lands in
  ``Website`` or ``Career Page URL``.
* :class:`TestVendors` — the eight vendors the brief names, plus the generic
  fallback.
"""

from __future__ import annotations

import unittest
from typing import Dict, List, Optional

from crawler.ats_discovery import (
    STATUS_BLOCKED,
    STATUS_DISCOVERED,
    STATUS_KEPT,
    STATUS_REJECTED,
    STATUS_REPLACED,
    STATUS_UNRESOLVED,
    DiscoveryReport,
    Enrichment,
    _Budget,
    apply_discoveries,
    discover_board,
    discover_missing_boards,
    needs_a_board,
)
from crawler.platform_detector import Platform
from tests._fake_sheets import FakeSheetsService
from utils.http import AdapterHttpError


class FakeSession:
    """Stands in for a requests session. Never used directly by these tests."""

    def close(self) -> None:
        """Match the interface the runner closes."""


class GetTextPatch:
    """Serve canned markup everywhere resolution reaches for the network.

    ``crawler.resolve`` and ``crawler.career_finder`` each hold their own
    reference to ``get_text``, and ``crawler.ats_discovery`` holds a third, so
    patching one and not the others would let a test reach the network.
    """

    def __init__(self, pages: Dict[str, str]) -> None:
        self.pages = pages
        self.fetched: List[str] = []
        self._originals: List[tuple] = []

    def __enter__(self) -> "GetTextPatch":
        import crawler.ats_discovery as discovery
        import crawler.career_finder as finder
        import crawler.resolve as resolve

        def fake_get_text(session, url, **kwargs):
            """Serve canned markup, or fail as an unreachable page would."""
            self.fetched.append(url)
            if url in self.pages:
                return self.pages[url]
            trimmed = url.rstrip("/")
            if trimmed in self.pages:
                return self.pages[trimmed]
            raise AdapterHttpError(f"GET {url} returned HTTP 404")

        for module in (resolve, finder, discovery):
            self._originals.append((module, module.get_text))
            module.get_text = fake_get_text
        return self

    def __exit__(self, *_exc) -> None:
        for module, original in self._originals:
            module.get_text = original


def company(**overrides) -> Dict[str, str]:
    """A master-sheet record, with sensible blanks."""
    record = {
        "company": "Acme",
        "company_key": "acme",
        "website": "https://acme.com",
        "career_url": "",
        "it_link": "",
        "row": 2,
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# Which rows are even looked at
# ---------------------------------------------------------------------------


class TestNeedsABoard(unittest.TestCase):
    """Only rows with no usable stored board are examined."""

    def test_a_blank_it_link_needs_one(self) -> None:
        self.assertTrue(needs_a_board(company()))

    def test_a_whitespace_only_it_link_needs_one(self) -> None:
        self.assertTrue(needs_a_board(company(it_link="   ")))

    def test_a_stored_ats_board_does_not(self) -> None:
        self.assertFalse(needs_a_board(company(it_link="https://boards.greenhouse.io/acme")))

    def test_filler_text_needs_one(self) -> None:
        """Sheets carry 'N/A' and 'TBD' where a URL was never found."""
        self.assertTrue(needs_a_board(company(it_link="N/A")))
        self.assertTrue(needs_a_board(company(it_link="none found")))


# ---------------------------------------------------------------------------
# Rule 5: existing links are authoritative
# ---------------------------------------------------------------------------


class TestExistingLinksAreAuthoritative(unittest.TestCase):
    """A stored board is kept, and no network call is made to check it."""

    def test_a_stored_board_is_kept_untouched(self) -> None:
        record = company(it_link="https://boards.greenhouse.io/acme")
        with GetTextPatch({}) as patch:
            result = discover_board(record, FakeSession())

        self.assertEqual(result.status, STATUS_KEPT)
        self.assertEqual(result.it_link, "https://boards.greenhouse.io/acme")
        self.assertEqual(result.platform, Platform.GREENHOUSE)
        self.assertEqual(patch.fetched, [], "a kept row must cost no requests")

    def test_a_stored_board_is_kept_even_when_the_site_names_another(self) -> None:
        """Discovery does not get to overrule the operator."""
        pages = {
            "https://acme.com": '<a href="https://jobs.lever.co/acme">Careers</a>',
        }
        record = company(it_link="https://boards.greenhouse.io/acme")
        with GetTextPatch(pages):
            result = discover_board(record, FakeSession())

        self.assertEqual(result.status, STATUS_KEPT)
        self.assertEqual(result.it_link, "https://boards.greenhouse.io/acme")

    def test_a_stored_board_produces_no_sheet_update(self) -> None:
        record = company(it_link="https://boards.greenhouse.io/acme")
        with GetTextPatch({}):
            result = discover_board(record, FakeSession())

        self.assertEqual(result.sheet_updates(), {})

    def test_an_unparseable_stored_value_is_demonstrably_invalid(self) -> None:
        """'N/A' is not a URL, so replacing it is not second-guessing anyone."""
        pages = {
            "https://acme.com": '<a href="https://boards.greenhouse.io/acme">Careers</a>',
        }
        record = company(it_link="N/A")
        with GetTextPatch(pages):
            result = discover_board(record, FakeSession())

        self.assertEqual(result.status, STATUS_REPLACED)
        self.assertEqual(result.it_link, "https://boards.greenhouse.io/acme")
        self.assertEqual(result.previous_it_link, "N/A")
        self.assertIn("not a usable URL", result.reason)

    def test_an_unparseable_stored_value_survives_when_nothing_is_found(self) -> None:
        """A replacement only happens when there is something to replace it with.

        The site here reads fine but names no board, so there is no candidate
        and the filler stays exactly where it was.
        """
        pages = {
            "https://acme.com": '<a href="/careers">Careers</a>',
            "https://acme.com/careers": "<h1>No openings</h1>",
        }
        record = company(it_link="N/A")
        with GetTextPatch(pages):
            result = discover_board(record, FakeSession())

        self.assertEqual(result.status, STATUS_UNRESOLVED)
        self.assertEqual(result.it_link, "")
        self.assertEqual(result.previous_it_link, "N/A")
        self.assertEqual(result.sheet_updates(), {}, "nothing found means nothing written")


# ---------------------------------------------------------------------------
# Rule 3: the discovery chain
# ---------------------------------------------------------------------------


class TestDiscoveryChain(unittest.TestCase):
    """website -> careers page -> ATS board."""

    def test_a_website_leading_straight_to_a_board(self) -> None:
        pages = {
            "https://acme.com": '<a href="https://boards.greenhouse.io/acme">Careers</a>',
        }
        with GetTextPatch(pages):
            result = discover_board(company(), FakeSession())

        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.it_link, "https://boards.greenhouse.io/acme")
        self.assertEqual(result.platform, Platform.GREENHOUSE)

    def test_a_website_then_a_careers_page_then_a_board(self) -> None:
        """The three-hop chain the brief describes."""
        pages = {
            "https://acme.com": '<a href="/careers">Careers</a>',
            "https://acme.com/careers": (
                '<a href="https://acme.wd1.myworkdayjobs.com/External">See openings</a>'
            ),
        }
        with GetTextPatch(pages):
            result = discover_board(company(), FakeSession())

        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.WORKDAY)
        self.assertEqual(result.it_link, "https://acme.wd1.myworkdayjobs.com/External")

    def test_a_stored_careers_page_is_read_for_its_board(self) -> None:
        """The common shape in this sheet: a careers page, no board."""
        pages = {
            "https://acme.com/careers": (
                '<a href="https://jobs.lever.co/acme">Open roles</a>'
            ),
        }
        record = company(career_url="https://acme.com/careers")
        with GetTextPatch(pages):
            result = discover_board(record, FakeSession())

        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.LEVER)
        self.assertEqual(result.it_link, "https://jobs.lever.co/acme")

    def test_a_homepage_in_the_career_url_column_still_reaches_the_board(self) -> None:
        """The shape MASTER_COMPANIES is actually in.

        ``Website`` is blank and ``Career Page URL`` holds the company's home
        page, not a careers page. Plain resolution scans that one page and
        stops; the board is one hop further, behind the header's Careers link.
        """
        pages = {
            "https://acme.com": '<a href="/careers">Careers</a><a href="/about">About</a>',
            "https://acme.com/careers": (
                "<h1>Join Acme</h1>"
                '<a href="https://acme.wd1.myworkdayjobs.com/External">View openings</a>'
            ),
        }
        record = company(website="", career_url="https://acme.com")
        with GetTextPatch(pages):
            result = discover_board(record, FakeSession())

        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.WORKDAY)
        self.assertEqual(result.it_link, "https://acme.wd1.myworkdayjobs.com/External")

    def test_a_homepage_whose_careers_page_names_no_vendor_is_unresolved(self) -> None:
        """The extra hop must not turn into a guess."""
        pages = {
            "https://acme.com": '<a href="/careers">Careers</a>',
            "https://acme.com/careers": "<h1>Join Acme</h1><p>Email us your CV.</p>",
        }
        record = company(website="", career_url="https://acme.com")
        with GetTextPatch(pages):
            result = discover_board(record, FakeSession())

        self.assertEqual(result.status, STATUS_UNRESOLVED)
        self.assertEqual(result.it_link, "")
        self.assertEqual(result.sheet_updates(), {})

    def test_a_discovered_careers_page_is_followed_one_further_hop(self) -> None:
        """Discovery landing on a careers page is not the end of the search.

        ``resolve_company`` stops here when the page it discovered is on the
        company's own domain. This stage carries on, because a careers page is
        not a board and the ``IT Link`` column wants a board.
        """
        pages = {
            "https://acme.com": '<a href="/work-with-us">Work with us</a>',
            "https://acme.com/work-with-us": (
                "<h1>Life at Acme</h1>"
                '<a href="https://careers.icims.com/jobs/search?hashed=-1">Search jobs</a>'
            ),
        }
        with GetTextPatch(pages):
            result = discover_board(company(), FakeSession())

        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.ICIMS)


# ---------------------------------------------------------------------------
# Rule 7: never guess
# ---------------------------------------------------------------------------


class TestNeverGuesses(unittest.TestCase):
    """No board is invented, and every failure explains itself."""

    def test_a_careers_page_with_no_board_link_is_unresolved(self) -> None:
        pages = {
            "https://acme.com": '<a href="/careers">Careers</a>',
            "https://acme.com/careers": "<h1>We have no openings</h1>",
        }
        with GetTextPatch(pages):
            result = discover_board(company(), FakeSession())

        self.assertEqual(result.status, STATUS_UNRESOLVED)
        self.assertEqual(result.it_link, "")
        self.assertTrue(result.reason, "an unresolved company must say why")

    def test_a_generic_page_is_never_written_as_a_board(self) -> None:
        """A crawlable page that names no vendor is not an ATS."""
        pages = {
            "https://acme.com": '<a href="/careers">Careers</a>',
            "https://acme.com/careers": '<a href="/careers/openings">Openings</a>',
            "https://acme.com/careers/openings": "<h1>Openings</h1>",
        }
        with GetTextPatch(pages):
            result = discover_board(company(), FakeSession())

        self.assertEqual(result.status, STATUS_UNRESOLVED)
        self.assertEqual(result.it_link, "")
        self.assertEqual(result.sheet_updates(), {})

    def test_an_aggregator_is_refused(self) -> None:
        """Indeed lists the jobs without being the company's own ATS."""
        pages = {
            "https://acme.com": '<a href="https://www.indeed.com/cmp/Acme/jobs">Careers</a>',
        }
        with GetTextPatch(pages):
            result = discover_board(company(), FakeSession())

        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertFalse(result.storable)
        self.assertEqual(result.sheet_updates(), {})
        self.assertIn("not the company's own ATS", result.reason)

    def test_a_single_posting_deep_link_is_refused(self) -> None:
        """The Providence/Avature case.

        A careers page linking to one open role yields a URL that names the
        vendor correctly and is not a board: pointed at it, the Avature adapter
        derives a search URL that 404s.
        """
        deep = (
            "https://providence.avature.net/providencetalentnetwork"
            "?jobId=12086&source=Providence.jobs&tags=2020-06-12086"
        )
        pages = {
            "https://acme.com": '<a href="/careers">Careers</a>',
            "https://acme.com/careers": f'<a href="{deep}">Registered Nurse</a>',
        }
        with GetTextPatch(pages):
            result = discover_board(company(), FakeSession())

        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertFalse(result.storable)
        self.assertEqual(result.sheet_updates(), {})
        self.assertIn("one posting", result.reason)

    def test_a_board_carrying_a_uuid_in_its_path_is_not_mistaken_for_a_posting(self) -> None:
        """UltiPro boards carry a UUID. That is a board id, not a job id."""
        board = (
            "https://recruiting2.ultipro.com/VIV1002VIVHE/JobBoard/"
            "47c9bc3a-8c84-4a15-a882-998936cd7f40/?q=&o=postedDateDesc"
        )
        pages = {
            "https://acme.com": f'<a href="{board}">Careers</a>',
        }
        with GetTextPatch(pages):
            result = discover_board(company(row=7), FakeSession())

        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertTrue(result.storable)
        self.assertEqual(result.platform, Platform.ULTIPRO)

    def test_a_vendors_sign_in_app_is_not_stored_as_a_board(self) -> None:
        """Detecting the vendor is not the same as finding its board.

        ``www.myworkday.com/<tenant>/d/task/...`` is the authenticated Workday
        application. It carries a Workday hostname, so the detector names it
        Workday, and the Workday adapter cannot read it — it parses the tenant
        as ``www``. Storing it would put an uncrawlable URL in the column.
        """
        app = "https://www.myworkday.com/austincc/d/task/2998$46522.htmld"
        pages = {
            "https://acme.com": '<a href="/careers">Careers</a>',
            "https://acme.com/careers": f'<a href="{app}">Search openings</a>',
        }
        with GetTextPatch(pages):
            result = discover_board(company(), FakeSession())

        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertFalse(result.storable)
        self.assertEqual(result.sheet_updates(), {})
        self.assertIn("sign-in app", result.reason)

    def test_a_real_workday_board_is_still_accepted(self) -> None:
        """The guard must not reject the boards it is meant to let through."""
        for board in (
            "https://acme.wd1.myworkdayjobs.com/External",
            "https://acme.wd3.myworkdaysite.com/recruiting/acme/External",
        ):
            with self.subTest(board=board):
                pages = {
                    "https://acme.com": '<a href="/careers">Careers</a>',
                    "https://acme.com/careers": f'<a href="{board}">Openings</a>',
                }
                with GetTextPatch(pages):
                    result = discover_board(company(), FakeSession())

                self.assertEqual(result.status, STATUS_DISCOVERED)
                self.assertEqual(result.platform, Platform.WORKDAY)

    def test_an_unreadable_site_is_reported_as_blocked(self) -> None:
        with GetTextPatch({}):
            result = discover_board(company(), FakeSession())

        self.assertIn(result.status, (STATUS_BLOCKED, STATUS_UNRESOLVED))
        self.assertEqual(result.it_link, "")
        self.assertTrue(result.reason)

    def test_a_row_with_nothing_to_go_on_is_unresolved(self) -> None:
        record = company(website="", career_url="", it_link="")
        with GetTextPatch({}) as patch:
            result = discover_board(record, FakeSession())

        self.assertEqual(result.status, STATUS_UNRESOLVED)
        self.assertEqual(patch.fetched, [], "nothing to go on means nothing to fetch")


# ---------------------------------------------------------------------------
# Rule 4: an ATS URL belongs in exactly one column
# ---------------------------------------------------------------------------


class TestSheetUpdatesStayInTheirColumn(unittest.TestCase):
    """``IT Link`` is for the board. Nothing else is touched."""

    def test_only_it_link_and_platform_are_ever_written(self) -> None:
        pages = {
            "https://acme.com": '<a href="https://boards.greenhouse.io/acme">Careers</a>',
        }
        with GetTextPatch(pages):
            result = discover_board(company(), FakeSession())

        self.assertEqual(set(result.sheet_updates()), {"it_link", "platform"})

    def test_the_website_column_is_never_updated(self) -> None:
        pages = {
            "https://acme.com": '<a href="https://boards.greenhouse.io/acme">Careers</a>',
        }
        with GetTextPatch(pages):
            result = discover_board(company(), FakeSession())

        self.assertNotIn("website", result.sheet_updates())

    def test_the_career_page_column_is_never_updated(self) -> None:
        """Even though discovery learned a careers page on the way."""
        pages = {
            "https://acme.com": '<a href="/careers">Careers</a>',
            "https://acme.com/careers": '<a href="https://jobs.lever.co/acme">Roles</a>',
        }
        with GetTextPatch(pages):
            result = discover_board(company(), FakeSession())

        self.assertEqual(result.it_link, "https://jobs.lever.co/acme")
        self.assertNotIn("career_url", result.sheet_updates())
        self.assertNotIn("careers_url", result.sheet_updates())

    def test_an_unresolved_company_updates_nothing(self) -> None:
        with GetTextPatch({}):
            result = discover_board(company(), FakeSession())

        self.assertEqual(result.sheet_updates(), {})


# ---------------------------------------------------------------------------
# Rule 6: the named vendors
# ---------------------------------------------------------------------------


class TestVendors(unittest.TestCase):
    """Each vendor the brief names is recognised from a careers-page link."""

    CASES = (
        ("https://acme.wd1.myworkdayjobs.com/External", Platform.WORKDAY),
        ("https://boards.greenhouse.io/acme", Platform.GREENHOUSE),
        ("https://jobs.lever.co/acme", Platform.LEVER),
        ("https://recruiting.ultipro.com/ACM1000/JobBoard/abc/", Platform.ULTIPRO),
        ("https://careers.icims.com/jobs/search?hashed=-1", Platform.ICIMS),
        ("https://acme.taleo.net/careersection/ex/joblist.ftl", Platform.TALEO),
        ("https://acme.eightfold.ai/careers", Platform.EIGHTFOLD),
        (
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
            "recruitment.html?cid=abc",
            Platform.ADP,
        ),
        ("https://myjobs.adp.com/acme/cx/job-listing", Platform.ADP_RM),
    )

    def test_every_named_vendor_is_discovered(self) -> None:
        for board, expected in self.CASES:
            with self.subTest(platform=expected.value):
                pages = {
                    "https://acme.com": '<a href="/careers">Careers</a>',
                    "https://acme.com/careers": f'<a href="{board}">Apply</a>',
                }
                with GetTextPatch(pages):
                    result = discover_board(company(), FakeSession())

                self.assertEqual(result.status, STATUS_DISCOVERED)
                self.assertEqual(result.platform, expected)
                self.assertEqual(result.it_link, board)

    def test_ukg_pro_is_recognised(self) -> None:
        pages = {
            "https://acme.com": '<a href="https://acme.ukgpro.com/careers">Careers</a>',
        }
        with GetTextPatch(pages):
            result = discover_board(company(), FakeSession())

        self.assertEqual(result.platform, Platform.UKG)
        self.assertEqual(result.status, STATUS_DISCOVERED)


# ---------------------------------------------------------------------------
# The batch driver
# ---------------------------------------------------------------------------


class TestDiscoverMissingBoards(unittest.TestCase):
    """The driver examines only what it should, and counts what it did."""

    def test_only_rows_missing_a_board_are_examined(self) -> None:
        records = [
            company(company_key="a", it_link="https://boards.greenhouse.io/a"),
            company(company_key="b", website="https://b.com"),
        ]
        pages = {"https://b.com": '<a href="https://jobs.lever.co/b">Careers</a>'}

        with GetTextPatch(pages):
            report = discover_missing_boards(
                records, session_factory=lambda: FakeSession(), workers=1
            )

        self.assertEqual(report.examined, 1)
        self.assertEqual(report.skipped_have_board, 1)
        self.assertEqual(report.discovered, 1)

    def test_the_report_counts_platforms(self) -> None:
        records = [
            company(company_key="a", website="https://a.com"),
            company(company_key="b", website="https://b.com"),
        ]
        pages = {
            "https://a.com": '<a href="https://jobs.lever.co/a">Careers</a>',
            "https://b.com": '<a href="https://jobs.lever.co/b">Careers</a>',
        }
        with GetTextPatch(pages):
            report = discover_missing_boards(
                records, session_factory=lambda: FakeSession(), workers=1
            )

        self.assertEqual(report.platforms.get("Lever"), 2)

    def test_unresolved_companies_are_listed_with_reasons(self) -> None:
        """A readable site that simply names no board."""
        records = [company(company_key="a", website="https://a.com")]
        pages = {
            "https://a.com": '<a href="/careers">Careers</a>',
            "https://a.com/careers": "<h1>No openings right now</h1>",
        }
        with GetTextPatch(pages):
            report = discover_missing_boards(
                records, session_factory=lambda: FakeSession(), workers=1
            )

        self.assertEqual(report.discovered, 0)
        self.assertEqual(len(report.unresolved), 1)
        self.assertTrue(report.unresolved[0].reason)

    def test_unreadable_sites_are_counted_as_blocked_not_unresolved(self) -> None:
        """The two are different outcomes and the report keeps them apart."""
        records = [company(company_key="a", website="https://a.com")]
        with GetTextPatch({}):
            report = discover_missing_boards(
                records, session_factory=lambda: FakeSession(), workers=1
            )

        self.assertEqual(report.discovered, 0)
        self.assertEqual(len(report.failed), 1)
        self.assertTrue(report.failed[0].reason)
        self.assertEqual(report.planned_updates(), [])

    def test_the_limit_is_honoured(self) -> None:
        records = [company(company_key=str(n), website=f"https://{n}.com") for n in range(5)]
        with GetTextPatch({}):
            report = discover_missing_boards(
                records, session_factory=lambda: FakeSession(), workers=1, limit=2
            )

        self.assertEqual(report.examined, 2)

    def test_one_company_raising_does_not_end_the_run(self) -> None:
        records = [
            company(company_key="a", website="https://a.com"),
            company(company_key="b", website="https://b.com"),
        ]
        pages = {"https://b.com": '<a href="https://jobs.lever.co/b">Careers</a>'}

        def explode(*_args, **_kwargs):
            raise RuntimeError("boom")

        with GetTextPatch(pages):
            import crawler.ats_discovery as discovery

            original = discovery.resolve_company
            calls = {"n": 0}

            def flaky(record, session=None, discover=True):
                calls["n"] += 1
                if record.get("company_key") == "a":
                    explode()
                return original(record, session=session, discover=discover)

            discovery.resolve_company = flaky
            try:
                report = discover_missing_boards(
                    records, session_factory=lambda: FakeSession(), workers=1
                )
            finally:
                discovery.resolve_company = original

        self.assertEqual(report.examined, 2)
        self.assertEqual(report.discovered, 1)
        self.assertEqual(len(report.failed), 1)

    def test_an_aggregator_is_not_counted_as_discovered(self) -> None:
        records = [company(company_key="a", website="https://a.com")]
        pages = {
            "https://a.com": '<a href="https://www.indeed.com/cmp/Acme/jobs">Careers</a>',
        }
        with GetTextPatch(pages):
            report = discover_missing_boards(
                records, session_factory=lambda: FakeSession(), workers=1
            )

        self.assertEqual(report.discovered, 0)
        self.assertEqual(len(report.rejected), 1)
        self.assertEqual(report.planned_updates(), [])
        self.assertIn("REFUSED", report.render(dry_run=True))

    def test_the_report_renders_without_a_spreadsheet(self) -> None:
        records = [company(company_key="a", website="https://a.com")]
        pages = {"https://a.com": '<a href="https://jobs.lever.co/a">Careers</a>'}
        with GetTextPatch(pages):
            report = discover_missing_boards(
                records, session_factory=lambda: FakeSession(), workers=1
            )

        text = report.render(dry_run=True)
        self.assertIn("DRY RUN", text)
        self.assertIn("Lever", text)


# ---------------------------------------------------------------------------
# The dry run writes nothing
# ---------------------------------------------------------------------------


class TestDryRunWritesNothing(unittest.TestCase):
    """The whole point of the stage's first outing."""

    def test_planned_updates_are_produced_but_not_applied(self) -> None:
        records = [company(company_key="a", website="https://a.com")]
        pages = {"https://a.com": '<a href="https://jobs.lever.co/a">Careers</a>'}

        with GetTextPatch(pages):
            report = discover_missing_boards(
                records, session_factory=lambda: FakeSession(), workers=1
            )

        planned = report.planned_updates()
        self.assertEqual(len(planned), 1)
        self.assertEqual(
            set(planned[0]), {"row", "company", "company_key", "it_link", "platform"}
        )
        # Only two of those are cells; the rest address the row for the report.
        self.assertEqual(planned[0]["it_link"], "https://jobs.lever.co/a")
        self.assertEqual(planned[0]["platform"], "Lever")

    def test_planned_updates_never_include_a_kept_row(self) -> None:
        records = [company(company_key="a", it_link="https://boards.greenhouse.io/a")]
        with GetTextPatch({}):
            report = discover_missing_boards(
                records, session_factory=lambda: FakeSession(), workers=1
            )

        self.assertEqual(report.planned_updates(), [])


class TestApplyWritesOnlyTheTwoColumns(unittest.TestCase):
    """``--apply`` against a fake spreadsheet: what lands, and what does not."""

    HEADERS = [
        "Company Name", "Website", "Career Page URL", "IT Link", "ATS / Platform",
        "Department", "Country", "Location", "Industry", "Status", "Source",
        "Open Jobs", "First Seen", "Last Checked", "Last Outcome", "Company Key",
    ]

    def _sheet(self):
        """A MASTER_COMPANIES tab: one blank row, one already holding a board."""
        from sheets.client import SheetsClient
        from sheets.companies import CompanyRepository

        rows = [
            self.HEADERS,
            ["Acme", "", "https://acme.com", "", "", "Eng", "US", "Austin",
             "Tech", "active", "csv", "3", "2026-01-01", "2026-08-01", "jobs", "acme"],
            ["Stored", "", "https://stored.com", "https://boards.greenhouse.io/stored",
             "Greenhouse", "Ops", "US", "Reno", "Retail", "active", "csv", "9",
             "2026-01-01", "2026-08-01", "jobs", "stored"],
        ]
        service = FakeSheetsService({"MASTER_COMPANIES": rows})
        client = SheetsClient(service, "fake", sleep=lambda _s: None)
        return service, CompanyRepository(client)

    def _run(self, pages, dry_run=False):
        """Discover against canned markup, then apply."""
        from crawler.ats_discovery import roster_with_rows

        service, repository = self._sheet()
        with GetTextPatch(pages):
            records = roster_with_rows(repository)
            report = discover_missing_boards(
                records, session_factory=lambda: FakeSession(), workers=1
            )
            result = apply_discoveries(repository, report, dry_run=dry_run)
        return service, repository, report, result

    PAGES = {
        "https://acme.com": '<a href="https://jobs.lever.co/acme">Careers</a>',
    }

    def test_only_it_link_and_platform_change(self) -> None:
        service, repository, report, result = self._run(self.PAGES)

        after = {r.get("company_name"): r for r in repository.store.read()}
        acme = after["Acme"]

        self.assertEqual(acme.get("it_link"), "https://jobs.lever.co/acme")
        self.assertEqual(acme.get("platform"), "Lever")
        # Everything else in that row is exactly as it was.
        self.assertEqual(acme.get("company_name"), "Acme")
        self.assertEqual(acme.get("website"), "")
        self.assertEqual(acme.get("career_url"), "https://acme.com")
        self.assertEqual(acme.get("company_key"), "acme")
        self.assertEqual(acme.get("industry"), "Tech")
        self.assertEqual(acme.get("first_seen"), "2026-01-01")
        self.assertEqual(acme.get("active_jobs"), "3")
        self.assertEqual(result.updated, 1)

    def test_an_existing_board_row_is_untouched(self) -> None:
        service, repository, report, result = self._run(self.PAGES)

        after = {r.get("company_name"): r for r in repository.store.read()}
        stored = after["Stored"]

        self.assertEqual(stored.get("it_link"), "https://boards.greenhouse.io/stored")
        self.assertEqual(stored.get("platform"), "Greenhouse")
        self.assertEqual(report.skipped_have_board, 1)

    def test_the_header_row_is_not_rewritten(self) -> None:
        service, _repository, _report, _result = self._run(self.PAGES)
        self.assertEqual(service.headers_of("MASTER_COMPANIES"), self.HEADERS)

    def test_no_destructive_request_is_generated(self) -> None:
        service, _repository, _report, _result = self._run(self.PAGES)
        self.assertEqual(service.destructive_requests(), [])

    def test_no_row_is_appended_so_no_duplicate_key_appears(self) -> None:
        _service, repository, _report, _result = self._run(self.PAGES)

        rows = repository.store.read()
        keys = [r.get("company_key") for r in rows]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(keys), len(set(keys)))

    def test_a_dry_run_writes_nothing(self) -> None:
        service, repository, _report, result = self._run(self.PAGES, dry_run=True)

        after = {r.get("company_name"): r for r in repository.store.read()}
        self.assertEqual(after["Acme"].get("it_link"), "")
        self.assertEqual(service.mutating_calls(), [])
        self.assertTrue(result.dry_run)

    def test_an_unresolved_company_leaves_its_row_alone(self) -> None:
        pages = {"https://acme.com": "<h1>No careers link here</h1>"}
        service, repository, report, _result = self._run(pages)

        after = {r.get("company_name"): r for r in repository.store.read()}
        self.assertEqual(after["Acme"].get("it_link"), "")
        self.assertEqual(after["Acme"].get("platform"), "")
        self.assertEqual(service.mutating_calls(), [])
        self.assertEqual(report.discovered, 0)

    def test_an_aggregator_is_never_written(self) -> None:
        pages = {
            "https://acme.com": '<a href="https://www.indeed.com/cmp/Acme/jobs">Jobs</a>',
        }
        service, repository, _report, _result = self._run(pages)

        after = {r.get("company_name"): r for r in repository.store.read()}
        self.assertEqual(after["Acme"].get("it_link"), "")
        self.assertEqual(service.mutating_calls(), [])

    def test_a_single_posting_deep_link_is_never_written(self) -> None:
        deep = "https://providence.avature.net/talentnetwork?jobId=12086"
        pages = {"https://acme.com": f'<a href="{deep}">Registered Nurse</a>'}
        service, repository, _report, _result = self._run(pages)

        after = {r.get("company_name"): r for r in repository.store.read()}
        self.assertEqual(after["Acme"].get("it_link"), "")
        self.assertEqual(service.mutating_calls(), [])

    def test_a_workday_sign_in_app_is_never_written(self) -> None:
        app = "https://www.myworkday.com/acme/d/task/2998$46522.htmld"
        pages = {"https://acme.com": f'<a href="{app}">Careers</a>'}
        service, repository, _report, _result = self._run(pages)

        after = {r.get("company_name"): r for r in repository.store.read()}
        self.assertEqual(after["Acme"].get("it_link"), "")
        self.assertEqual(service.mutating_calls(), [])

    def test_applying_twice_changes_nothing_the_second_time(self) -> None:
        """The second run finds the row already answered and skips it."""
        from crawler.ats_discovery import roster_with_rows

        service, repository = self._sheet()
        with GetTextPatch(self.PAGES):
            first = discover_missing_boards(
                roster_with_rows(repository),
                session_factory=lambda: FakeSession(),
                workers=1,
            )
            apply_discoveries(repository, first, dry_run=False)

            second = discover_missing_boards(
                roster_with_rows(repository),
                session_factory=lambda: FakeSession(),
                workers=1,
            )
            result = apply_discoveries(repository, second, dry_run=False)

        self.assertEqual(second.examined, 0)
        self.assertEqual(second.skipped_have_board, 2)
        self.assertEqual(result.updated, 0)


class TestRosterWithRows(unittest.TestCase):
    """Reading the roster must keep the row and never write a key back."""

    def test_every_record_carries_its_sheet_row(self) -> None:
        from sheets.client import SheetsClient
        from sheets.companies import CompanyRepository
        from crawler.ats_discovery import roster_with_rows

        rows = [
            TestApplyWritesOnlyTheTwoColumns.HEADERS,
            ["Acme", "", "https://acme.com", "", "", "", "", "", "", "active",
             "", "", "", "", "", "acme"],
        ]
        service = FakeSheetsService({"MASTER_COMPANIES": rows})
        repository = CompanyRepository(SheetsClient(service, "fake", sleep=lambda _s: None))

        records = roster_with_rows(repository)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["row"], 2)
        self.assertEqual(service.mutating_calls(), [], "reading must write nothing")

    def test_duplicate_rows_collapse_to_one_record(self) -> None:
        """One company is examined once, as the weekly run examines it once."""
        from sheets.client import SheetsClient
        from sheets.companies import CompanyRepository
        from crawler.ats_discovery import roster_with_rows

        rows = [
            TestApplyWritesOnlyTheTwoColumns.HEADERS,
            ["Acme", "", "https://acme.com", "", "", "", "", "", "", "active",
             "", "", "", "", "", "acme"],
            ["Acme", "", "https://acme.com", "", "", "", "", "", "", "active",
             "", "", "", "", "", "acme"],
        ]
        service = FakeSheetsService({"MASTER_COMPANIES": rows})
        repository = CompanyRepository(SheetsClient(service, "fake", sleep=lambda _s: None))

        records = roster_with_rows(repository)

        self.assertEqual([r["row"] for r in records], [2])


class TestEnrichmentRecord(unittest.TestCase):
    """The record each company produces."""

    def test_an_enrichment_defaults_to_unresolved(self) -> None:
        entry = Enrichment(company_key="a", company="Acme")
        self.assertEqual(entry.status, STATUS_UNRESOLVED)
        self.assertEqual(entry.sheet_updates(), {})

    def test_a_report_starts_empty(self) -> None:
        report = DiscoveryReport()
        self.assertEqual(report.examined, 0)
        self.assertEqual(report.discovered, 0)
        self.assertEqual(report.planned_updates(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestDuplicateRowsPreferTheAnsweredOne(unittest.TestCase):
    """A company appearing twice must not have its stored board rediscovered."""

    def test_the_row_holding_a_board_is_the_one_kept(self) -> None:
        from sheets.client import SheetsClient
        from sheets.companies import CompanyRepository
        from crawler.ats_discovery import roster_with_rows

        board = "https://boards.greenhouse.io/acme"
        rows = [
            TestApplyWritesOnlyTheTwoColumns.HEADERS,
            # The blank duplicate comes first, so order alone would pick it.
            ["Acme", "", "https://acme.com", "", "", "", "", "", "", "active",
             "", "", "", "", "", "acme"],
            ["Acme", "", "https://acme.com", board, "Greenhouse", "", "", "", "",
             "active", "", "", "", "", "", "acme"],
        ]
        service = FakeSheetsService({"MASTER_COMPANIES": rows})
        repository = CompanyRepository(SheetsClient(service, "fake", sleep=lambda _s: None))

        records = roster_with_rows(repository)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["row"], 3)
        self.assertEqual(records[0]["it_link"], board)

    def test_such_a_company_costs_no_discovery_request(self) -> None:
        from sheets.client import SheetsClient
        from sheets.companies import CompanyRepository
        from crawler.ats_discovery import roster_with_rows

        board = "https://boards.greenhouse.io/acme"
        rows = [
            TestApplyWritesOnlyTheTwoColumns.HEADERS,
            ["Acme", "", "https://acme.com", "", "", "", "", "", "", "active",
             "", "", "", "", "", "acme"],
            ["Acme", "", "https://acme.com", board, "Greenhouse", "", "", "", "",
             "active", "", "", "", "", "", "acme"],
        ]
        service = FakeSheetsService({"MASTER_COMPANIES": rows})
        repository = CompanyRepository(SheetsClient(service, "fake", sleep=lambda _s: None))

        pages = {"https://acme.com": '<a href="https://jobs.lever.co/acme">Careers</a>'}
        with GetTextPatch(pages) as patch:
            report = discover_missing_boards(
                roster_with_rows(repository),
                session_factory=lambda: FakeSession(),
                workers=1,
            )

        self.assertEqual(report.examined, 0)
        self.assertEqual(report.skipped_have_board, 1)
        self.assertEqual(patch.fetched, [])
        self.assertEqual(report.planned_updates(), [])


# ---------------------------------------------------------------------------
# Reach: a board is not always an anchor
# ---------------------------------------------------------------------------


class TestEmbeddedBoardsAreFound(unittest.TestCase):
    """Most boards on this sheet are embedded, not linked.

    ``crawler.resolve.ats_link_on`` reads ``<a href>``. A board booted by a
    ``<script>``, framed by an ``<iframe>``, or named only inside inline
    JavaScript is invisible to it however plainly it renders in a browser. On
    the live sheet that accounted for more unresolved companies than every
    other cause combined.
    """

    def _discover(self, careers_markup: str):
        pages = {
            "https://acme.com": '<a href="/careers">Careers</a>',
            "https://acme.com/careers": careers_markup,
        }
        with GetTextPatch(pages):
            return discover_board(company(), FakeSession())

    def test_a_script_embedded_greenhouse_board_is_found(self) -> None:
        """Beam Therapeutics' shape."""
        result = self._discover(
            '<script src="https://boards.greenhouse.io/embed/job_board/js'
            '?for=beamtherapeutics"></script>'
        )
        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.GREENHOUSE)
        self.assertEqual(result.it_link, "https://job-boards.greenhouse.io/beamtherapeutics")

    def test_an_iframed_paycom_board_is_found(self) -> None:
        """Behavioral Health Services' shape."""
        board = ("https://www.paycomonline.net/v4/ats/web.php/jobs"
                 "?clientkey=F2434A55DD64FE0B647BCAB93DA4CA46")
        result = self._discover('<iframe src="' + board + '"></iframe>')
        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.PAYCOM)
        self.assertEqual(result.it_link, board)

    def test_a_board_named_only_in_inline_javascript_is_found(self) -> None:
        """Pet Supplies Plus' shape."""
        result = self._discover(
            '<script>window.careers = {url: '
            '"https://careers-petsuppliesplus.icims.com/jobs/intro"};</script>'
        )
        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.ICIMS)

    def test_a_bamboohr_embed_script_yields_the_tenant_board(self) -> None:
        """Ireland Home Based Services' shape: the host carries the tenant."""
        result = self._discover('<script src="https://ihbs.bamboohr.com/js/embed.js"></script>')
        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.BAMBOOHR)
        self.assertEqual(result.it_link, "https://ihbs.bamboohr.com/careers")

    def test_a_paycor_iframe_action_becomes_the_career_home(self) -> None:
        """Zumbiel Packaging's shape."""
        client = "8a7883c6708df1d40170bfe4e14717fe"
        result = self._discover(
            '<script src="https://recruitingbypaycor.com/career/iframe.action'
            '?clientId=' + client + '"></script>'
        )
        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.PAYCOR)
        self.assertIn("CareerHome.action", result.it_link)
        self.assertIn(client, result.it_link)

    def test_an_anchor_still_wins_over_an_embed(self) -> None:
        """A real link is better evidence than a widget asset."""
        result = self._discover(
            '<a href="https://jobs.lever.co/acme">Open roles</a>'
            '<script src="https://static.smartrecruiters.com/job-widget/x.js"></script>'
        )
        self.assertEqual(result.platform, Platform.LEVER)
        self.assertEqual(result.it_link, "https://jobs.lever.co/acme")


class TestVendorAssetsAreNotBoards(unittest.TestCase):
    """A CDN asset names the vendor without being a board.

    ``static.smartrecruiters.com/job-widget/...css`` and
    ``jobs.jobvite.com/__assets__/...iframe.js`` identify the vendor and carry
    no tenant at all -- the tenant arrives at runtime. Storing one would put a
    stylesheet in the ``IT Link`` column.
    """

    def _discover(self, careers_markup: str):
        pages = {
            "https://acme.com": '<a href="/careers">Careers</a>',
            "https://acme.com/careers": careers_markup,
        }
        with GetTextPatch(pages):
            return discover_board(company(), FakeSession())

    def test_a_smartrecruiters_widget_asset_is_not_stored(self) -> None:
        result = self._discover(
            '<script src="https://static.smartrecruiters.com/job-widget/1.5.5/'
            'script/smart_widget.js"></script>'
        )
        self.assertNotEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.sheet_updates(), {})

    def test_a_jobvite_asset_bundle_is_not_stored(self) -> None:
        result = self._discover(
            '<script src="https://jobs.jobvite.com/__assets__/scripts/'
            'careersite/public/iframe.js"></script>'
        )
        self.assertNotEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.sheet_updates(), {})

    def test_a_stylesheet_on_a_vendor_host_is_not_stored(self) -> None:
        result = self._discover(
            '<link rel="stylesheet" href="https://static.smartrecruiters.com/a.css">'
        )
        self.assertNotEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.sheet_updates(), {})

    def test_a_real_jobvite_board_is_still_accepted(self) -> None:
        """The asset guard must not reject the thing it is protecting."""
        result = self._discover('<a href="https://jobs.jobvite.com/cushingterrell">Jobs</a>')
        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.JOBVITE)


class TestConventionalPathsAreProbed(unittest.TestCase):
    """When nothing links to a board, try where boards conventionally live."""

    def test_the_careers_path_is_probed_when_the_home_page_links_nowhere(self) -> None:
        """Quick Quack's shape: a home page that never mentions careers."""
        pages = {
            "https://acme.com": "<h1>We wash cars</h1>",
            "https://acme.com/careers": (
                '<a href="https://acme.rec.pro.ukg.net/ACM1500ACM/JobBoard/abc/">Openings</a>'
            ),
        }
        record = company(website="", career_url="https://acme.com")
        with GetTextPatch(pages):
            result = discover_board(record, FakeSession())

        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.UKG)

    def test_a_jobs_subdomain_is_probed(self) -> None:
        """Georgetown's and SDSU's shape: the board lives on jobs.<domain>."""
        pages = {
            "https://acme.com": "<h1>Acme</h1>",
            "https://jobs.acme.com": (
                '<a href="https://acme.wd1.myworkdayjobs.com/Careers">Search jobs</a>'
            ),
        }
        record = company(website="", career_url="https://acme.com")
        with GetTextPatch(pages):
            result = discover_board(record, FakeSession())

        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.WORKDAY)

    def test_probing_stops_at_the_first_board_found(self) -> None:
        """Cost control: this runs against 7,570 companies eventually.

        Exercised against the prober directly. Going through
        ``discover_board`` would not isolate it, because
        ``find_careers_url`` does its own probing first and fetches several of
        the same paths on its way to finding nothing.
        """
        from crawler.ats_discovery import _probe_for_board

        pages = {
            "https://acme.com/careers": '<a href="https://jobs.lever.co/acme">Roles</a>',
            "https://acme.com/jobs": '<a href="https://boards.greenhouse.io/acme">Roles</a>',
        }
        with GetTextPatch(pages) as patch:
            board, _readable = _probe_for_board("https://acme.com", FakeSession())

        self.assertEqual(board, "https://jobs.lever.co/acme")
        self.assertNotIn("https://acme.com/jobs", patch.fetched)

    def test_probing_is_bounded(self) -> None:
        """A company with no board anywhere costs a fixed handful of requests."""
        from crawler.ats_discovery import MAX_PROBES, _probe_for_board

        with GetTextPatch({}) as patch:
            board, _readable = _probe_for_board("https://acme.com", FakeSession())

        self.assertEqual(board, "")
        self.assertLessEqual(len(patch.fetched), MAX_PROBES)

    def test_probing_gives_up_quietly_when_nothing_answers(self) -> None:
        pages = {"https://acme.com": "<h1>Acme</h1>"}
        record = company(website="", career_url="https://acme.com")
        with GetTextPatch(pages):
            result = discover_board(record, FakeSession())

        self.assertIn(result.status, (STATUS_UNRESOLVED, STATUS_BLOCKED))
        self.assertEqual(result.sheet_updates(), {})

    def test_a_row_that_already_has_a_board_is_never_probed(self) -> None:
        """Rule 5 still holds: no requests at all for an answered row."""
        record = company(it_link="https://boards.greenhouse.io/acme")
        with GetTextPatch({}) as patch:
            discover_board(record, FakeSession())
        self.assertEqual(patch.fetched, [])


# ---------------------------------------------------------------------------
# The browser fallback
# ---------------------------------------------------------------------------


class FakeRenderedPage:
    """Stands in for :class:`utils.browser.RenderedPage`."""

    def __init__(self, url="", html="", requests=None, error=None) -> None:
        self.url = url
        self.html = html
        self.requests = list(requests or [])
        self.payloads: List[object] = []
        self.error = error

    @property
    def ok(self) -> bool:
        """Match the real page's contract."""
        return self.error is None and bool(self.html)


class RenderPatch:
    """Replace the browser with canned rendered pages, and count the visits."""

    def __init__(self, pages: Optional[Dict[str, FakeRenderedPage]] = None) -> None:
        self.pages = dict(pages or {})
        self.visited: List[str] = []
        self._original = None

    def __enter__(self) -> "RenderPatch":
        import crawler.ats_discovery as discovery

        def fake_render(url, **_kwargs):
            """Serve a canned rendered page, or nothing at all."""
            self.visited.append(url)
            return self.pages.get(url) or self.pages.get(url.rstrip("/"))

        self._original = discovery.render_page
        discovery.render_page = fake_render
        return self

    def __exit__(self, *_exc) -> None:
        import crawler.ats_discovery as discovery

        discovery.render_page = self._original


def rendered(html="", requests=None, url="https://acme.com/careers"):
    """A rendered page carrying the given DOM and network traffic."""
    return FakeRenderedPage(url=url, html=html, requests=requests)


class TestRenderBudget(unittest.TestCase):
    """Browser use is capped, and the cap is the whole point.

    A render costs seconds where a fetch costs milliseconds. Against 7,570
    companies an uncapped fallback would turn a five-hour run into a week, so
    the budget is claimed rather than checked -- the same reasoning
    :class:`crawler.weekly_run.WeeklyRun` applies to filter rendering.
    """

    def test_no_rendering_happens_without_a_budget(self) -> None:
        pages = {"https://acme.com": "<h1>Acme</h1>"}
        with GetTextPatch(pages), RenderPatch() as browser:
            discover_board(company(), FakeSession())
        self.assertEqual(browser.visited, [])

    def test_the_budget_caps_total_visits_across_companies(self) -> None:
        records = [company(company_key=str(n), website=f"https://c{n}.com") for n in range(6)]
        with GetTextPatch({}), RenderPatch() as browser:
            discover_missing_boards(
                records, session_factory=lambda: FakeSession(), workers=1, render_budget=2
            )
        self.assertLessEqual(len(browser.visited), 2)

    def test_a_zero_budget_renders_nothing(self) -> None:
        records = [company(company_key="a", website="https://a.com")]
        with GetTextPatch({}), RenderPatch() as browser:
            discover_missing_boards(
                records, session_factory=lambda: FakeSession(), workers=1, render_budget=0
            )
        self.assertEqual(browser.visited, [])

    def test_the_budget_is_not_spent_when_static_discovery_succeeds(self) -> None:
        """A company answered by a cheap fetch must not cost a browser visit."""
        pages = {"https://acme.com": '<a href="https://jobs.lever.co/acme">Careers</a>'}
        records = [company(company_key="a")]
        with GetTextPatch(pages), RenderPatch() as browser:
            report = discover_missing_boards(
                records, session_factory=lambda: FakeSession(), workers=1, render_budget=5
            )
        self.assertEqual(report.discovered, 1)
        self.assertEqual(browser.visited, [])

    def test_a_row_that_already_has_a_board_is_never_rendered(self) -> None:
        records = [company(company_key="a", it_link="https://boards.greenhouse.io/a")]
        with GetTextPatch({}), RenderPatch() as browser:
            discover_missing_boards(
                records, session_factory=lambda: FakeSession(), workers=1, render_budget=5
            )
        self.assertEqual(browser.visited, [])


class TestRenderedBoardsAreFound(unittest.TestCase):
    """What the browser sees that a fetch cannot."""

    def _discover(self, page, budget=3):
        pages = {"https://acme.com": '<a href="/careers">Careers</a>',
                 "https://acme.com/careers": "<h1>Join us</h1>"}
        with GetTextPatch(pages), RenderPatch({"https://acme.com/careers": page}):
            return discover_board(
                company(), FakeSession(), render_budget=_Budget(budget)
            )

    def test_a_board_only_in_the_rendered_dom_is_found(self) -> None:
        """PACE Supply's shape."""
        page = rendered(
            html='<a href="https://alljobs-pacesupply.icims.com/jobs/search">Jobs</a>'
        )
        result = self._discover(page)
        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.ICIMS)

    def test_a_board_only_in_network_traffic_is_found(self) -> None:
        """Hammerspace's shape: the board is fetched, never written into the DOM."""
        page = rendered(
            html="<div id='board'></div>",
            requests=["https://ats.rippling.com/embed/hammerspace/jobs"],
        )
        result = self._discover(page)
        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.RIPPLING)

    def test_a_smartrecruiters_posting_yields_the_tenant_board(self) -> None:
        """KIPP's shape: the rendered DOM carries job links, not a board link."""
        page = rendered(
            html='<a href="https://www.smartrecruiters.com/KIPP/'
                 '744000145308553-high-school-math-teacher">Apply</a>'
        )
        result = self._discover(page)
        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.SMARTRECRUITERS)
        self.assertEqual(result.it_link, "https://careers.smartrecruiters.com/KIPP")

    def test_a_jobvite_tenant_path_yields_the_tenant_board(self) -> None:
        """Cushing Terrell's shape."""
        page = rendered(
            requests=["https://jobs.jobvite.com/cushingterrell/?nl=1&fr=true"],
            html="<div></div>",
        )
        result = self._discover(page)
        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.JOBVITE)
        self.assertEqual(result.it_link, "https://jobs.jobvite.com/cushingterrell")

    def test_a_failed_render_is_not_an_error(self) -> None:
        with GetTextPatch({"https://acme.com": '<a href="/careers">Careers</a>',
                           "https://acme.com/careers": "<h1>Join us</h1>"}), RenderPatch({}):
            result = discover_board(company(), FakeSession(), render_budget=_Budget(3))
        self.assertNotEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.sheet_updates(), {})


class TestGuardsHoldInTheRenderedPath(unittest.TestCase):
    """Everything refused statically stays refused when a browser found it."""

    def _discover(self, page):
        pages = {"https://acme.com": '<a href="/careers">Careers</a>',
                 "https://acme.com/careers": "<h1>Join us</h1>"}
        with GetTextPatch(pages), RenderPatch({"https://acme.com/careers": page}):
            return discover_board(company(), FakeSession(), render_budget=_Budget(3))

    def test_a_vendor_cdn_asset_is_still_refused(self) -> None:
        page = rendered(requests=[
            "https://static.smartrecruiters.com/job-widget/1.5.5/script/smart_widget.js"
        ], html="<div></div>")
        result = self._discover(page)
        self.assertNotEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.sheet_updates(), {})

    def test_an_aggregator_is_still_refused(self) -> None:
        page = rendered(html='<a href="https://www.indeed.com/cmp/Acme/jobs">Jobs</a>')
        result = self._discover(page)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.sheet_updates(), {})

    def test_a_single_posting_deep_link_is_still_refused(self) -> None:
        page = rendered(
            html='<a href="https://providence.avature.net/tn?jobId=12086">Nurse</a>'
        )
        result = self._discover(page)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.sheet_updates(), {})

    def test_a_workday_sign_in_app_is_still_refused(self) -> None:
        page = rendered(
            html='<a href="https://www.myworkday.com/austincc/d/task/2998$4.htmld">Jobs</a>'
        )
        result = self._discover(page)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.sheet_updates(), {})

    def test_the_rendered_path_still_writes_only_two_columns(self) -> None:
        page = rendered(html='<a href="https://jobs.lever.co/acme">Roles</a>')
        result = self._discover(page)
        self.assertEqual(set(result.sheet_updates()), {"it_link", "platform"})


class TestRenderFollowsOneHop(unittest.TestCase):
    """The board is often a level below the page a careers search lands on.

    Cushing Terrell's Jobvite board is on ``/joinus/job-listings/`` and KIPP's
    SmartRecruiters widget on ``/careers/apply-now/``. Rendering the careers
    page alone finds neither; the link to the deeper page exists only once the
    page has rendered.
    """

    def test_a_board_one_hop_below_the_careers_page_is_found(self) -> None:
        static = {
            "https://acme.com": '<a href="/careers">Careers</a>',
            "https://acme.com/careers": "<h1>Join us</h1>",
        }
        renders = {
            "https://acme.com/careers": rendered(
                html='<a href="/careers/open-roles">See our open roles</a>',
                url="https://acme.com/careers",
            ),
            "https://acme.com/careers/open-roles": rendered(
                html='<a href="https://jobs.jobvite.com/acme">Apply</a>',
                url="https://acme.com/careers/open-roles",
            ),
        }
        with GetTextPatch(static), RenderPatch(renders) as browser:
            result = discover_board(company(), FakeSession(), render_budget=_Budget(4))

        self.assertEqual(result.status, STATUS_DISCOVERED)
        self.assertEqual(result.platform, Platform.JOBVITE)
        self.assertEqual(len(browser.visited), 2)

    def test_the_hop_still_respects_the_per_company_ceiling(self) -> None:
        """A chain of careers links must not walk a site forever."""
        from crawler.ats_discovery import _RENDER_TARGETS

        static = {
            "https://acme.com": '<a href="/careers">Careers</a>',
            "https://acme.com/careers": "<h1>Join us</h1>",
        }
        # Every rendered page offers another careers link and never a board.
        renders = {
            f"https://acme.com/careers{'/more' * n}": rendered(
                html=f'<a href="/careers{"/more" * (n + 1)}">More careers</a>',
                url=f"https://acme.com/careers{'/more' * n}",
            )
            for n in range(6)
        }
        with GetTextPatch(static), RenderPatch(renders) as browser:
            result = discover_board(company(), FakeSession(), render_budget=_Budget(50))

        self.assertNotEqual(result.status, STATUS_DISCOVERED)
        self.assertLessEqual(len(browser.visited), _RENDER_TARGETS)


class TestProbeReportsReadablePages(unittest.TestCase):
    """A page that answers and names no board is where a render is worth spending."""

    def test_pages_that_answered_are_reported(self) -> None:
        from crawler.ats_discovery import _probe_for_board

        pages = {"https://acme.com/careers": "<h1>Careers</h1><p>Nothing here.</p>"}
        with GetTextPatch(pages):
            board, readable = _probe_for_board("https://acme.com", FakeSession())

        self.assertEqual(board, "")
        self.assertIn("https://acme.com/careers", readable)

    def test_pages_that_did_not_answer_are_not_reported(self) -> None:
        from crawler.ats_discovery import _probe_for_board

        with GetTextPatch({}):
            board, readable = _probe_for_board("https://acme.com", FakeSession())

        self.assertEqual(board, "")
        self.assertEqual(readable, [])


class TestTenantDerivationIsNotAGuess(unittest.TestCase):
    """A vendor host is not enough; the tenant has to be where a tenant lives."""

    def test_a_smartrecruiters_api_route_yields_no_tenant(self) -> None:
        """`api.smartrecruiters.com/job-api/...` is a route, not a customer.

        Found live: KIPP's rendered page fetches this, and the first path
        segment produced a board called `careers.smartrecruiters.com/job-api`.
        """
        from crawler.ats_discovery import _canonical_board

        board = _canonical_board(
            "https://api.smartrecruiters.com/job-api/v1/companies/KIPP/postings",
            Platform.SMARTRECRUITERS,
        )
        self.assertEqual(board, "")

    def test_a_smartrecruiters_posting_still_yields_its_tenant(self) -> None:
        from crawler.ats_discovery import _canonical_board

        board = _canonical_board(
            "https://www.smartrecruiters.com/KIPP/744000145308553-math-teacher",
            Platform.SMARTRECRUITERS,
        )
        self.assertEqual(board, "https://careers.smartrecruiters.com/KIPP")

    def test_a_bare_smartrecruiters_host_yields_nothing(self) -> None:
        from crawler.ats_discovery import _canonical_board

        self.assertEqual(
            _canonical_board("https://www.smartrecruiters.com", Platform.SMARTRECRUITERS), ""
        )
