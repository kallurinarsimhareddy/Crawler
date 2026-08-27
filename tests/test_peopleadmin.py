"""Extraction from PeopleAdmin boards, against both shapes in our data.

PeopleAdmin is a higher-education applicant tracking system. Two companies in
``MASTER_COMPANIES`` run it — Montana State University on ``jobs.montana.edu``
and Pacific Lutheran University on ``employment.plu.edu`` — and neither uses a
``peopleadmin.com`` hostname, which is why host-based detection never found
them.

The fixtures below are trimmed from those two live boards, and they differ in
the way that matters: **each tenant configures its own result columns**, in its
own order, and the row cells carry no labels at all. Montana State publishes
Posting Number, Division, Department, Position Type and Job Close Date; Pacific
Lutheran publishes Job Open Date, Position Type and Department. Reading either
one by column position would put a date in the department field of the other,
so the header is parsed and the columns are matched by name.

Nothing here reaches the network.
"""

from __future__ import annotations

import unittest

from adapters import peopleadmin
from tests._fakes import FakeSession, html
from utils.http import AdapterHttpError, AdapterUrlError

# ---------------------------------------------------------------------------
# Fixtures, trimmed from the live boards
# ---------------------------------------------------------------------------

MONTANA_HEADER = """
<div id='job_list_header_responsive' class="row hidden-xs hidden-sm">
  <div class='job-title col-md-4'><a href="/postings/search?sort=435+asc">Job Title</a></div>
  <div class='col-md-8'>
    <div class='col-md-2'></div>
    <div class='col-md-2 col-md-push-0'><a href="/x?sort=225">Posting Number</a></div>
    <div class='col-md-2 col-md-push-0'><a href="/x?sort=437">Division</a></div>
    <div class='col-md-2 col-md-push-0'><a href="/x?sort=226">Department</a></div>
    <div class='col-md-2 col-md-push-0'><a href="/x?sort=434">Position Type</a></div>
    <div class='col-md-2 col-md-push-0'><a href="/x?sort=227">Job Close Date</a></div>
  </div>
</div>
"""

PLU_HEADER = """
<div id='job_list_header_responsive' class="row hidden-xs hidden-sm">
  <div class='job-title col-md-4'>&nbsp;</div>
  <div class='col-md-8'>
    <div class='col-md-2'></div>
    <div class='col-md-2 col-md-push-4'><a href="/x?sort=396">Job Open Date</a></div>
    <div class='col-md-2 col-md-push-4'><a href="/x?sort=395">Position Type</a></div>
    <div class='col-md-2 col-md-push-4'><a href="/x?sort=393">Department</a></div>
  </div>
</div>
"""


def montana_row(posting_id: int, title: str) -> str:
    """One Montana State result row: number, division, department, type, close date."""
    return f"""
    <div class='job-item job-item-posting' data-posting-title="{title}">
      <div class='container-fluid'><div class='row'>
        <div class='col-md-4 col-xs-12 job-title job-title-text-wrap'>
          <h3><a href="/postings/{posting_id}">{title}</a></h3>
        </div>
        <div class='col-md-8 col-xs-12 '>
          <div class='col-md-2 col-xs-12'></div>
          <div class='col-md-2 col-xs-12 job-title job-title-text-wrap col-md-push-0'>
            STAFF - VA - {posting_id}
          </div>
          <div class='col-md-2 col-xs-12 job-title job-title-text-wrap col-md-push-0'>
            College of Letters &amp; Science
          </div>
          <div class='col-md-2 col-xs-12 job-title job-title-text-wrap col-md-push-0'>
            Chemistry
          </div>
          <div class='col-md-2 col-xs-12 job-title job-title-text-wrap col-md-push-0'>
            Staff
          </div>
          <div class='col-md-2 col-xs-12 job-title job-title-text-wrap col-md-push-0'>
          </div>
        </div>
      </div></div>
    </div>
    """


def plu_row(posting_id: int, title: str) -> str:
    """One Pacific Lutheran result row: open date, position type, department."""
    return f"""
    <div class='job-item job-item-posting' data-posting-title="{title}">
      <div class='container-fluid'><div class='row'>
        <div class='col-md-4 col-xs-12 job-title job-title-text-wrap'>
          <h3><a href="/postings/{posting_id}">{title}</a></h3>
        </div>
        <div class='col-md-8 col-xs-12 '>
          <div class='col-md-2 col-xs-12'></div>
          <div class='col-md-2 col-xs-12 job-title job-title-text-wrap col-md-push-4'>
            August 21, 2026
          </div>
          <div class='col-md-2 col-xs-12 job-title job-title-text-wrap col-md-push-4'>
            Staff
          </div>
          <div class='col-md-2 col-xs-12 job-title job-title-text-wrap col-md-push-4'>
            College of Professional Studies
          </div>
        </div>
      </div></div>
    </div>
    """


def board(header: str, *rows: str, pages: int = 1) -> str:
    """A complete PeopleAdmin results page."""
    pagination = "".join(
        f'<a href="/postings/search?page={n}">{n}</a>' for n in range(2, pages + 1)
    )
    return f"""
    <html><head><title>Job Search</title>
    <script src="https://pa-hrsuite-production.s3.amazonaws.com/x.js"></script></head>
    <body>
      <h2 id='search-results'>View Results <span class='smaller muted'>({len(rows)})</span></h2>
      {header}
      <div id="search_results">{''.join(rows)}</div>
      <nav>{pagination}</nav>
    </body></html>
    """


MONTANA_BOARD = board(MONTANA_HEADER, montana_row(52714, "Postdoctoral Research Associate"),
                      montana_row(52573, "Campus Planner"))
PLU_BOARD = board(PLU_HEADER, plu_row(8894, "Budget &amp; Administrative Manager"),
                  plu_row(8899, "Head Coach"))
EMPTY_BOARD = board(MONTANA_HEADER)

#: A results page with a header but no ``#search_results`` container at all —
#: what an error page or an unrelated site looks like.
NOT_A_BOARD = "<html><body><h1>Page not found</h1></body></html>"

MSU = "https://jobs.montana.edu/postings/search"
PLU = "https://employment.plu.edu/postings/search"


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------


class TestBoardUrl(unittest.TestCase):
    """PeopleAdmin runs on vanity domains, so hosts prove nothing."""

    def test_a_search_url_is_kept_as_is(self) -> None:
        self.assertEqual(peopleadmin.parse_board_url(MSU), MSU)

    def test_a_vanity_root_gains_the_search_path(self) -> None:
        """Both boards link to /postings/search from their own navigation."""
        self.assertEqual(
            peopleadmin.parse_board_url("https://jobs.montana.edu"),
            "https://jobs.montana.edu/postings/search",
        )

    def test_a_peopleadmin_host_works_too(self) -> None:
        self.assertEqual(
            peopleadmin.parse_board_url("https://acme.peopleadmin.com"),
            "https://acme.peopleadmin.com/postings/search",
        )

    def test_a_posting_url_is_reduced_to_the_board(self) -> None:
        self.assertEqual(
            peopleadmin.parse_board_url("https://employment.plu.edu/postings/8894"),
            PLU,
        )

    def test_an_unusable_url_is_rejected(self) -> None:
        with self.assertRaises(AdapterUrlError):
            peopleadmin.parse_board_url("not a url")


# ---------------------------------------------------------------------------
# Montana State: six configured columns
# ---------------------------------------------------------------------------


class TestMontanaStateShape(unittest.TestCase):
    """Posting Number, Division, Department, Position Type, Job Close Date."""

    def jobs(self):
        session = FakeSession([html(MONTANA_BOARD)])
        return peopleadmin.fetch_jobs(MSU, "Montana State University", session=session)

    def test_every_posting_is_found(self) -> None:
        self.assertEqual(len(self.jobs()), 2)

    def test_titles_come_from_the_link(self) -> None:
        titles = {job.job_title for job in self.jobs()}
        self.assertEqual(titles, {"Postdoctoral Research Associate", "Campus Planner"})

    def test_urls_are_absolute(self) -> None:
        urls = {job.job_url for job in self.jobs()}
        self.assertIn("https://jobs.montana.edu/postings/52714", urls)

    def test_the_department_column_is_read_by_name(self) -> None:
        """Not by position: Division sits before Department on this board."""
        job = self.jobs()[0]
        self.assertEqual(job.department, "Chemistry")

    def test_the_position_type_becomes_employment_type(self) -> None:
        self.assertEqual(self.jobs()[0].employment_type, "Staff")

    def test_the_posting_number_becomes_the_job_id(self) -> None:
        self.assertEqual(self.jobs()[0].job_id, "STAFF - VA - 52714")

    def test_no_location_column_leaves_location_empty(self) -> None:
        """This board publishes none, and a guess would be worse than blank."""
        self.assertEqual(self.jobs()[0].location, "")

    def test_the_platform_is_labelled(self) -> None:
        self.assertEqual(self.jobs()[0].platform, "PeopleAdmin")


# ---------------------------------------------------------------------------
# Pacific Lutheran: a different set, in a different order
# ---------------------------------------------------------------------------


class TestPacificLutheranShape(unittest.TestCase):
    """Job Open Date, Position Type, Department — no posting number."""

    def jobs(self):
        session = FakeSession([html(PLU_BOARD)])
        return peopleadmin.fetch_jobs(PLU, "Pacific Lutheran University", session=session)

    def test_every_posting_is_found(self) -> None:
        self.assertEqual(len(self.jobs()), 2)

    def test_the_ampersand_in_a_title_is_decoded(self) -> None:
        titles = {job.job_title for job in self.jobs()}
        self.assertIn("Budget & Administrative Manager", titles)

    def test_the_department_column_is_read_by_name(self) -> None:
        """Department is last here and third on the Montana board."""
        self.assertEqual(self.jobs()[0].department, "College of Professional Studies")

    def test_the_open_date_becomes_the_posted_date(self) -> None:
        self.assertEqual(self.jobs()[0].posted_date, "August 21, 2026")

    def test_no_posting_number_column_leaves_the_job_id_empty(self) -> None:
        self.assertEqual(self.jobs()[0].job_id, "")

    def test_position_type_is_still_found(self) -> None:
        self.assertEqual(self.jobs()[0].employment_type, "Staff")


# ---------------------------------------------------------------------------
# Empty, blocked and malformed are three different things
# ---------------------------------------------------------------------------


class TestEmptyIsNotBlocked(unittest.TestCase):
    """A board with no openings is a fact, not a failure."""

    def test_an_empty_board_returns_no_jobs(self) -> None:
        session = FakeSession([html(EMPTY_BOARD)])
        self.assertEqual(peopleadmin.fetch_jobs(MSU, "Montana State", session=session), [])

    def test_an_empty_board_does_not_raise(self) -> None:
        session = FakeSession([html(EMPTY_BOARD)])
        try:
            peopleadmin.fetch_jobs(MSU, "Montana State", session=session)
        except Exception as exc:  # noqa: BLE001 - the assertion is that this does not happen
            self.fail(f"an empty board must not raise, but raised {exc!r}")

    def test_a_page_that_is_not_a_results_page_raises(self) -> None:
        session = FakeSession([html(NOT_A_BOARD)])
        with self.assertRaises(AdapterHttpError):
            peopleadmin.fetch_jobs(MSU, "Montana State", session=session)

    def test_an_http_failure_propagates(self) -> None:
        session = FakeSession([AdapterHttpError("GET ... returned HTTP 503")])
        with self.assertRaises(AdapterHttpError):
            peopleadmin.fetch_jobs(MSU, "Montana State", session=session)

    def test_malformed_markup_is_not_mistaken_for_an_empty_board(self) -> None:
        session = FakeSession([html("<html><body><div>truncated")])
        with self.assertRaises(AdapterHttpError):
            peopleadmin.fetch_jobs(MSU, "Montana State", session=session)


# ---------------------------------------------------------------------------
# Paging and identity
# ---------------------------------------------------------------------------


class TestPaging(unittest.TestCase):
    """Montana State pages; Pacific Lutheran does not."""

    def test_further_pages_are_followed(self) -> None:
        page_one = board(MONTANA_HEADER, montana_row(1, "One"), pages=2)
        page_two = board(MONTANA_HEADER, montana_row(2, "Two"))
        session = FakeSession([html(page_one), html(page_two), html(EMPTY_BOARD)])

        jobs = peopleadmin.fetch_jobs(MSU, "Montana State", session=session)

        self.assertEqual({job.job_title for job in jobs}, {"One", "Two"})

    def test_a_board_without_pagination_costs_one_request(self) -> None:
        session = FakeSession([html(PLU_BOARD)])
        peopleadmin.fetch_jobs(PLU, "Pacific Lutheran", session=session)
        self.assertEqual(len(session.requests), 1)

    def test_repeated_postings_across_pages_are_deduplicated(self) -> None:
        page_one = board(MONTANA_HEADER, montana_row(1, "One"), pages=2)
        repeat = board(MONTANA_HEADER, montana_row(1, "One"))
        session = FakeSession([html(page_one), html(repeat), html(EMPTY_BOARD)])

        jobs = peopleadmin.fetch_jobs(MSU, "Montana State", session=session)

        self.assertEqual(len(jobs), 1)

    def test_paging_is_bounded(self) -> None:
        """A board that always advertises another page cannot hang a run."""
        endless = board(MONTANA_HEADER, montana_row(1, "One"), pages=99)
        session = FakeSession([html(endless) for _ in range(peopleadmin.MAX_PAGES + 5)])

        peopleadmin.fetch_jobs(MSU, "Montana State", session=session)

        self.assertLessEqual(len(session.requests), peopleadmin.MAX_PAGES)


class TestIdentityAndDeduplication(unittest.TestCase):
    """Postings must flow through the shared identity path unchanged."""

    def test_a_posting_yields_a_stable_identity(self) -> None:
        from crawler.identity import job_identity

        session = FakeSession([html(MONTANA_BOARD)])
        jobs = peopleadmin.fetch_jobs(MSU, "Montana State", session=session)

        def identity(job):
            """The identity the crawler derives for a posting."""
            return job_identity(
                job.company_name, job.job_url, job.job_title,
                location=job.location, platform=job.platform, job_id=job.job_id,
            )

        first = identity(jobs[0])
        session = FakeSession([html(MONTANA_BOARD)])
        again = peopleadmin.fetch_jobs(MSU, "Montana State", session=session)

        self.assertEqual(first.job_uid, identity(again[0]).job_uid)

    def test_distinct_postings_have_distinct_identities(self) -> None:
        from crawler.identity import job_identity

        session = FakeSession([html(MONTANA_BOARD)])
        jobs = peopleadmin.fetch_jobs(MSU, "Montana State", session=session)

        keys = {
            job_identity(
                job.company_name, job.job_url, job.job_title,
                location=job.location, platform=job.platform, job_id=job.job_id,
            ).job_uid
            for job in jobs
        }
        self.assertEqual(len(keys), 2)

    def test_a_posting_without_a_title_is_dropped(self) -> None:
        """build_job rejects a half-row rather than exporting one."""
        broken = board(MONTANA_HEADER, """
        <div class='job-item job-item-posting'>
          <div class='col-md-4 job-title'><h3><a href="/postings/9"></a></h3></div>
        </div>
        """)
        session = FakeSession([html(broken)])
        self.assertEqual(peopleadmin.fetch_jobs(MSU, "Montana State", session=session), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# ---------------------------------------------------------------------------
# Production integration
# ---------------------------------------------------------------------------


class TestPeopleAdminDetection(unittest.TestCase):
    """PeopleAdmin is identified by path, because its hosts are the customer's.

    Every other vendor in the registry is found by hostname. PeopleAdmin cannot
    be: Montana State runs it on ``jobs.montana.edu`` and Pacific Lutheran on
    ``employment.plu.edu``, neither of which says PeopleAdmin anywhere. The
    board's search path is the only thing in the URL that does.
    """

    def test_the_two_live_boards_are_detected(self) -> None:
        from crawler.platform_detector import Platform, detect_platform

        for url in (
            "https://jobs.montana.edu/postings/search",
            "https://employment.plu.edu/postings/search",
        ):
            with self.subTest(url=url):
                self.assertEqual(detect_platform(url), Platform.PEOPLEADMIN)

    def test_a_vendor_hosted_tenant_is_detected_by_host(self) -> None:
        from crawler.platform_detector import Platform, detect_platform

        self.assertEqual(
            detect_platform("https://acme.peopleadmin.com/postings/search"),
            Platform.PEOPLEADMIN,
        )

    def test_the_search_path_is_detected_with_a_query(self) -> None:
        from crawler.platform_detector import Platform, detect_platform

        self.assertEqual(
            detect_platform("https://jobs.montana.edu/postings/search?page=2"),
            Platform.PEOPLEADMIN,
        )

    def test_a_bare_posting_url_is_not_claimed(self) -> None:
        """`/postings/<id>` alone is far too generic to claim for a vendor.

        The cost is small: the stored ``IT Link`` is always the search view,
        which is what the discovery stage records and what the adapter reduces
        any URL to.
        """
        from crawler.platform_detector import Platform, detect_platform

        self.assertNotEqual(
            detect_platform("https://jobs.montana.edu/postings/52714"),
            Platform.PEOPLEADMIN,
        )

    def test_an_unrelated_site_using_that_path_is_claimed_too(self) -> None:
        """The known limit of a path rule, pinned so nobody is surprised.

        Detection never fetches, so the URL is all there is. A site that
        happens to serve ``/postings/search`` is classified PeopleAdmin — the
        same trade Taleo's ``/careersection/`` rule already makes. The adapter
        is where this is caught: it checks the markup and fails with a clear
        message rather than inventing postings.
        """
        from crawler.platform_detector import Platform, detect_platform

        self.assertEqual(
            detect_platform("https://forum.example.com/postings/search"),
            Platform.PEOPLEADMIN,
        )

    def test_a_false_positive_fails_loudly_in_the_adapter(self) -> None:
        """The safety net for the trade made above."""
        from adapters import peopleadmin

        session = FakeSession([html("<html><body><h1>A forum</h1></body></html>")])
        with self.assertRaises(AdapterHttpError):
            peopleadmin.fetch_jobs(
                "https://forum.example.com/postings/search", "Not A University",
                session=session,
            )


class TestExistingDetectionIsUnchanged(unittest.TestCase):
    """Adding a path rule must not disturb any vendor already detected."""

    def test_hostnames_still_win(self) -> None:
        from crawler.platform_detector import Platform, detect_platform

        cases = (
            ("https://boards.greenhouse.io/acme", Platform.GREENHOUSE),
            ("https://jobs.lever.co/acme", Platform.LEVER),
            ("https://acme.wd1.myworkdayjobs.com/External", Platform.WORKDAY),
            ("https://careers-acme.icims.com/jobs/search", Platform.ICIMS),
            ("https://acme.taleo.net/careersection/ex/joblist.ftl", Platform.TALEO),
        )
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(detect_platform(url), expected)

    def test_an_ordinary_careers_page_is_still_generic(self) -> None:
        from crawler.platform_detector import Platform, detect_platform

        self.assertEqual(
            detect_platform("https://acme.com/careers"), Platform.GENERIC_HTML
        )


class TestEngineRegistry(unittest.TestCase):
    """The engine has to be able to load the adapter it is told to use."""

    def test_peopleadmin_is_registered(self) -> None:
        from crawler.crawler_engine import ADAPTER_MODULES
        from crawler.platform_detector import Platform

        self.assertEqual(ADAPTER_MODULES[Platform.PEOPLEADMIN], "adapters.peopleadmin")

    def test_the_registry_resolves_it_to_a_callable(self) -> None:
        from crawler.crawler_engine import build_registry
        from crawler.platform_detector import Platform

        registry = build_registry()

        self.assertIn(Platform.PEOPLEADMIN, registry)
        self.assertTrue(callable(registry[Platform.PEOPLEADMIN]))

    def test_the_engine_selects_it_for_a_board_url(self) -> None:
        from crawler.crawler_engine import CrawlerEngine
        from crawler.platform_detector import Platform

        engine = CrawlerEngine()
        _url, _source, platform = engine.select_seed(
            {"company": "Montana State University", "website": "",
             "career_url": "", "it_link": "https://jobs.montana.edu/postings/search"}
        )

        self.assertEqual(platform, Platform.PEOPLEADMIN)
