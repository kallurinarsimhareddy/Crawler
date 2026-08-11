"""Tests for the applicant tracking systems added in version 2.

Two shapes of test, because there are two shapes of adapter.

The API adapters each get their own class, built around a payload in the shape
the vendor actually returns — that is the part worth pinning down, since it is
what breaks when a vendor changes.

The hosted-HTML adapters are table-driven. They differ only in the hosts they
accept and the shape of their posting URLs, so testing each with its own class
would be twenty near-identical copies asserting the same three things. The
table states each vendor's board URL, a realistic posting URL and its label,
and every adapter is then held to the same contract: it accepts its own URLs,
rejects other vendors', and turns a board page into postings.
"""

from __future__ import annotations

import unittest
from typing import Any, Tuple

from config.settings import configure
from crawler.platform_detector import Platform
from utils.http import AdapterHttpError, AdapterUrlError

try:  # see tests/_fakes.py for why both spellings are needed
    from tests._fakes import FakeResponse, FakeSession, html
except ImportError:  # pragma: no cover - depends on how unittest was invoked
    from _fakes import FakeResponse, FakeSession, html

from adapters import (
    adp_rm,
    applicantpro,
    applicantstack,
    asure,
    avature,
    breezyhr,
    bullhorn,
    careerplug,
    clearcompany,
    comeet,
    cornerstone,
    eightfold,
    fountain,
    gem,
    gohire,
    hireology,
    homerun,
    hrmdirect,
    indeed,
    isolved,
    jazzhr,
    jobscore,
    join,
    manatal,
    neogov,
    oleeo,
    paycom,
    paycor,
    paylocity,
    personio,
    phenom,
    pinpoint,
    radancy,
    recruiterbox,
    rippling,
    silkroad,
    talentreef,
    ukg_ready,
    zoho_recruit,
)

# The suite must never reach the network. These are the shipped defaults; the
# call is here so the module is safe when run on its own.
configure(browser_fallback=False, discover_careers=False, diagnostics=False, max_workers=1)


class TestBreezyHr(unittest.TestCase):
    """Breezy returns the whole board as a bare JSON list."""

    PAYLOAD = [
        {
            "id": "abc123",
            "name": "Systems Engineer",
            "url": "https://acme.breezy.hr/p/abc123-systems-engineer",
            "location": {
                "name": "Austin, Texas, United States",
                "city": "Austin",
                "country": {"id": "US", "name": "United States"},
            },
        },
        {"id": "def456", "name": "No URL Role"},
    ]

    def test_reads_the_tenant_from_every_url_shape(self) -> None:
        for url in (
            "https://acme.breezy.hr/",
            "https://acme.breezy.hr/p/abc123-systems-engineer",
            "acme.breezy.hr",
        ):
            with self.subTest(url=url):
                self.assertEqual(breezyhr.parse_tenant(url), "acme")

    def test_rejects_a_non_breezy_url(self) -> None:
        with self.assertRaises(AdapterUrlError):
            breezyhr.parse_tenant("https://boards.greenhouse.io/acme")

    def test_converts_postings_and_trusts_the_stated_country(self) -> None:
        session = FakeSession([FakeResponse(self.PAYLOAD)])

        jobs = breezyhr.fetch_jobs("https://acme.breezy.hr/", "Acme", session=session)

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].job_title, "Systems Engineer")
        self.assertEqual(jobs[0].country, "United States")
        self.assertEqual(jobs[0].platform, "BreezyHR")

    def test_builds_a_url_for_a_posting_that_states_none(self) -> None:
        session = FakeSession([FakeResponse(self.PAYLOAD)])

        jobs = breezyhr.fetch_jobs("https://acme.breezy.hr/", "Acme", session=session)

        self.assertEqual(jobs[1].job_url, "https://acme.breezy.hr/p/def456")

    def test_calls_the_documented_endpoint(self) -> None:
        session = FakeSession([FakeResponse([])])

        breezyhr.fetch_jobs("https://acme.breezy.hr/", "Acme", session=session)

        self.assertEqual(session.requests[0]["url"], "https://acme.breezy.hr/json")

    def test_a_non_list_body_is_an_error(self) -> None:
        session = FakeSession([FakeResponse({"error": "gone"})])
        with self.assertRaises(AdapterHttpError):
            breezyhr.fetch_jobs("https://acme.breezy.hr/", "Acme", session=session)


class TestRippling(unittest.TestCase):
    """Rippling serves every board from one API keyed by slug."""

    def test_reads_the_slug_past_the_plumbing_segments(self) -> None:
        for url in (
            "https://ats.rippling.com/acme/jobs",
            "https://ats.rippling.com/acme/jobs/1234",
            "https://app.rippling.com/jobs/acme",
        ):
            with self.subTest(url=url):
                self.assertEqual(rippling.parse_board_slug(url), "acme")

    def test_rejects_a_url_with_no_slug(self) -> None:
        with self.assertRaises(AdapterUrlError):
            rippling.parse_board_slug("https://ats.rippling.com/jobs")

    def test_converts_postings(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    [
                        {
                            "uuid": "u-1",
                            "name": "Network Engineer",
                            "url": "https://ats.rippling.com/acme/jobs/u-1",
                            "workLocation": {"label": "Boston, MA"},
                        }
                    ]
                )
            ]
        )

        jobs = rippling.fetch_jobs("https://ats.rippling.com/acme/jobs", "Acme", session=session)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].location, "Boston, MA")
        self.assertEqual(jobs[0].country, "United States")
        self.assertEqual(
            session.requests[0]["url"],
            "https://api.rippling.com/platform/api/ats/v1/board/acme/jobs",
        )


class TestPinpoint(unittest.TestCase):
    """Pinpoint wraps its postings in a ``data`` envelope."""

    def test_reads_the_tenant(self) -> None:
        self.assertEqual(parse := pinpoint.parse_tenant("https://holtgrp.pinpointhq.com/"), "holtgrp")
        self.assertTrue(parse)

    def test_rejects_a_foreign_host(self) -> None:
        with self.assertRaises(AdapterUrlError):
            pinpoint.parse_tenant("https://acme.breezy.hr/")

    def test_converts_postings_from_the_data_envelope(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "data": [
                            {
                                "title": "IT Manager",
                                "url": "https://holtgrp.pinpointhq.com/postings/1",
                                "location": {"name": "Leeds, United Kingdom", "country": "United Kingdom"},
                            }
                        ]
                    }
                )
            ]
        )

        jobs = pinpoint.fetch_jobs("https://holtgrp.pinpointhq.com/", "Holt", session=session)

        self.assertEqual(jobs[0].job_title, "IT Manager")
        self.assertEqual(jobs[0].country, "United Kingdom")
        self.assertEqual(session.requests[0]["url"], "https://holtgrp.pinpointhq.com/postings.json")


class TestPersonio(unittest.TestCase):
    """Personio publishes an XML feed, not JSON."""

    FEED = """<?xml version="1.0" encoding="utf-8"?>
    <workzag-jobs>
      <position>
        <id>1234567</id>
        <office>Munich</office>
        <department>Engineering</department>
        <name>Backend Engineer</name>
        <employmentType>permanent</employmentType>
      </position>
      <position>
        <id>7654321</id>
        <office>London</office>
        <name>Data Analyst</name>
      </position>
    </workzag-jobs>
    """

    def test_keeps_the_boards_own_domain(self) -> None:
        self.assertEqual(
            personio.parse_board("https://acme.jobs.personio.com/job/1"),
            "https://acme.jobs.personio.com",
        )

    def test_rejects_a_non_personio_url(self) -> None:
        with self.assertRaises(AdapterUrlError):
            personio.parse_board("https://acme.breezy.hr/")

    def test_converts_positions_and_builds_their_urls(self) -> None:
        session = FakeSession([FakeResponse(text=self.FEED)])

        jobs = personio.fetch_jobs("https://acme.jobs.personio.de/", "Acme", session=session)

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].job_title, "Backend Engineer")
        self.assertEqual(jobs[0].location, "Munich")
        self.assertEqual(jobs[0].job_url, "https://acme.jobs.personio.de/job/1234567")
        self.assertEqual(session.requests[0]["url"], "https://acme.jobs.personio.de/xml")

    def test_a_non_xml_body_is_an_error(self) -> None:
        # A real HTML error page, whose unclosed <meta> is what makes it
        # unparsable as XML — the actual failure mode when a tenant moves.
        error_page = "<html><head><meta charset='utf-8'></head><body>Not found</body></html>"
        session = FakeSession([FakeResponse(text=error_page)])

        with self.assertRaises(AdapterHttpError):
            personio.fetch_jobs("https://acme.jobs.personio.de/", "Acme", session=session)

    def test_a_board_with_no_positions_is_not_an_error(self) -> None:
        session = FakeSession([FakeResponse(text="<workzag-jobs></workzag-jobs>")])

        self.assertEqual(
            personio.fetch_jobs("https://acme.jobs.personio.de/", "Acme", session=session), []
        )


class TestManatal(unittest.TestCase):
    """Manatal pages with a ``next`` cursor."""

    def test_reads_the_slug_from_both_url_shapes(self) -> None:
        self.assertEqual(manatal.parse_slug("https://acme.manatal.com/"), "acme")
        self.assertEqual(manatal.parse_slug("https://careers.manatal.com/acme"), "acme")

    def test_follows_the_cursor_to_the_end(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "results": [{"id": 1, "position_name": "Recruiter", "location": "Bangkok"}],
                        "next": "https://api.manatal.com/open/v3/career-page/acme/jobs/?page=2",
                    }
                ),
                FakeResponse(
                    {"results": [{"id": 2, "position_name": "Analyst", "location": "Bangkok"}], "next": None}
                ),
            ]
        )

        jobs = manatal.fetch_jobs("https://acme.manatal.com/", "Acme", session=session)

        self.assertEqual([job.job_title for job in jobs], ["Recruiter", "Analyst"])
        self.assertEqual(jobs[0].job_url, "https://acme.manatal.com/job/1")
        self.assertEqual(len(session.requests), 2)

    def test_a_missing_results_list_is_an_error(self) -> None:
        session = FakeSession([FakeResponse({"detail": "not found"})])
        with self.assertRaises(AdapterHttpError):
            manatal.fetch_jobs("https://acme.manatal.com/", "Acme", session=session)


class TestComeet(unittest.TestCase):
    """Comeet needs a company uid and falls back to using it as the token."""

    def test_reads_the_uid_from_a_hosted_board_url(self) -> None:
        uid, token = comeet.parse_company("https://www.comeet.co/jobs/acme/93.00A")
        self.assertEqual(uid, "93.00A")
        self.assertEqual(token, "93.00A")

    def test_an_explicit_token_wins_over_the_uid(self) -> None:
        _, token = comeet.parse_company("https://www.comeet.co/jobs/acme/93.00A?token=SECRET")
        self.assertEqual(token, "SECRET")

    def test_rejects_a_url_with_no_uid(self) -> None:
        with self.assertRaises(AdapterUrlError):
            comeet.parse_company("https://www.comeet.co/careers")

    def test_converts_positions(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    [
                        {
                            "uid": "AA.00B",
                            "name": "QA Engineer",
                            "url_active_page": "https://acme.com/careers/qa",
                            "location": {"name": "Tel Aviv, Israel", "country": "Israel"},
                        }
                    ]
                )
            ]
        )

        jobs = comeet.fetch_jobs("https://www.comeet.co/jobs/acme/93.00A", "Acme", session=session)

        self.assertEqual(jobs[0].job_title, "QA Engineer")
        self.assertEqual(jobs[0].country, "Israel")
        self.assertEqual(session.requests[0]["params"], {"token": "93.00A"})


class TestEightfold(unittest.TestCase):
    """Eightfold is keyed by a tenant domain, not by the board host."""

    def test_takes_the_domain_from_the_url_when_it_carries_one(self) -> None:
        origin, domain = eightfold.parse_site("https://acme.eightfold.ai/careers?domain=acme.com")
        self.assertEqual(origin, "https://acme.eightfold.ai")
        self.assertEqual(domain, "acme.com")

    def test_rejects_an_empty_url(self) -> None:
        with self.assertRaises(AdapterUrlError):
            eightfold.parse_site("")

    def test_converts_positions_without_a_second_request(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "count": 1,
                        "positions": [
                            {
                                "id": 55,
                                "name": "Cloud Architect",
                                "location": "Seattle, WA",
                                "canonicalPositionUrl": "https://acme.eightfold.ai/careers?pid=55",
                            }
                        ],
                    }
                )
            ]
        )

        jobs = eightfold.fetch_jobs(
            "https://acme.eightfold.ai/careers?domain=acme.com", "Acme", session=session
        )

        self.assertEqual(jobs[0].job_title, "Cloud Architect")
        self.assertEqual(jobs[0].country, "United States")
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(session.requests[0]["params"]["domain"], "acme.com")

    def test_reads_the_domain_from_the_board_page_when_the_url_omits_it(self) -> None:
        session = FakeSession(
            [
                FakeResponse(text='<script>window.config={"domain":"acme.co.uk"};</script>'),
                FakeResponse({"count": 0, "positions": []}),
            ]
        )

        eightfold.fetch_jobs("https://acme.eightfold.ai/careers", "Acme", session=session)

        self.assertEqual(session.requests[1]["params"]["domain"], "acme.co.uk")


class TestCornerstone(unittest.TestCase):
    """Cornerstone is keyed by the career-site id in the board URL."""

    def test_reads_the_site_id(self) -> None:
        origin, site_id = cornerstone.parse_site(
            "https://acme.csod.com/ux/ats/careersite/4/home?c=acme"
        )
        self.assertEqual(origin, "https://acme.csod.com")
        self.assertEqual(site_id, "4")

    def test_converts_requisitions_from_the_search_api(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "data": {
                            "requisitions": [
                                {
                                    "requisitionId": 987,
                                    "displayJobTitle": "Plant Manager",
                                    "locations": ["Dayton, OH"],
                                }
                            ]
                        }
                    }
                )
            ]
        )

        jobs = cornerstone.fetch_jobs(
            "https://acme.csod.com/ux/ats/careersite/4/home", "Acme", session=session
        )

        self.assertEqual(jobs[0].job_title, "Plant Manager")
        self.assertEqual(jobs[0].job_url, "https://acme.csod.com/ux/ats/careersite/4/job/987")

    def test_falls_back_to_the_rendered_board_when_the_api_is_closed(self) -> None:
        session = FakeSession(
            [
                FakeResponse({"error": "denied"}, status_code=403),
                FakeResponse({"error": "denied"}, status_code=403),
                html('<a href="/ux/ats/careersite/4/job/12">Maintenance Technician</a>'),
            ]
        )

        jobs = cornerstone.fetch_jobs(
            "https://acme.csod.com/ux/ats/careersite/4/home", "Acme", session=session
        )

        self.assertEqual(jobs[0].job_title, "Maintenance Technician")


class TestPhenom(unittest.TestCase):
    """Phenom's widget nests its postings under ``refineSearch``."""

    def test_reads_the_origin(self) -> None:
        self.assertEqual(
            phenom.parse_origin("https://careers.acme.com/us/en/search-results"),
            "https://careers.acme.com",
        )

    def test_converts_postings_from_the_widget(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "refineSearch": {
                            "data": {
                                "jobs": [
                                    {
                                        "jobId": "R1",
                                        "title": "Line Supervisor",
                                        "cityStateCountry": "Tulsa, OK, United States",
                                        "jobSeoUrl": "/job/tulsa/line-supervisor/1/9",
                                    }
                                ]
                            }
                        }
                    }
                )
            ]
        )

        jobs = phenom.fetch_jobs("https://careers.acme.com/us/en/search", "Acme", session=session)

        self.assertEqual(jobs[0].job_title, "Line Supervisor")
        self.assertEqual(jobs[0].job_url, "https://careers.acme.com/job/tulsa/line-supervisor/1/9")
        self.assertEqual(jobs[0].country, "United States")

    def test_falls_back_to_the_board_when_the_widget_is_absent(self) -> None:
        session = FakeSession(
            [
                FakeResponse({"error": "no widget"}, status_code=404),
                html('<a href="/job/tulsa/line-supervisor/1/9">Line Supervisor</a>'),
            ]
        )

        jobs = phenom.fetch_jobs("https://careers.acme.com/us/en/search", "Acme", session=session)

        self.assertEqual(jobs[0].job_title, "Line Supervisor")


class TestTaRecruitment(unittest.TestCase):
    """UKG Ready and Asure run one application, read through its REST API.

    Scraping their rendered boards returns nothing — every anchor is ``#`` —
    so these adapters must call the endpoint the board itself calls, and a
    regression to HTML scraping would silently return zero for every company.
    """

    PAYLOAD = {
        "job_requisitions": [
            {
                "id": 1006959249,
                "job_title": "Intern, System Administrator",
                "location": {"city": "Neenah", "state": "WI", "country": "USA"},
                "job_categories": ["Quality"],
            },
            {
                "id": 1006959250,
                "job_title": "Maintenance Technician",
                "location": {"city": "Neenah", "state": "WI", "country": "USA"},
            },
        ]
    }

    CASES = (
        (ukg_ready, "https://secure4.saashr.com/ta/6095384.careers?CareersSearch=", "UKG Ready"),
        (asure, "https://secure3.entertimeonline.com/ta/6142834.careers?lang=en-US", "Asure"),
    )

    def test_each_reads_its_own_board_url(self) -> None:
        for module, board_url, _ in self.CASES:
            with self.subTest(adapter=module.__name__):
                self.assertTrue(module.parse_board_url(board_url).startswith("https://"))

    def test_each_rejects_another_vendors_url(self) -> None:
        for module, _, _ in self.CASES:
            with self.subTest(adapter=module.__name__):
                with self.assertRaises(AdapterUrlError):
                    module.parse_board_url(_FOREIGN_URL)

    def test_a_url_with_no_company_id_is_rejected(self) -> None:
        with self.assertRaises(AdapterUrlError):
            ukg_ready.parse_board_url("https://secure4.saashr.com/ta/careers")

    def test_each_converts_requisitions(self) -> None:
        for module, board_url, label in self.CASES:
            with self.subTest(adapter=module.__name__):
                session = FakeSession([FakeResponse(self.PAYLOAD)])

                jobs = module.fetch_jobs(board_url, "Acme", session=session)

                self.assertEqual(len(jobs), 2)
                self.assertEqual(jobs[0].job_title, "Intern, System Administrator")
                self.assertEqual(jobs[0].location, "Neenah, WI, USA")
                self.assertEqual(jobs[0].country, "United States")
                self.assertEqual(jobs[0].platform, label)

    def test_it_calls_the_rest_endpoint_not_the_board(self) -> None:
        session = FakeSession([FakeResponse({"job_requisitions": []})])

        ukg_ready.fetch_jobs("https://secure4.saashr.com/ta/6095384.careers", "Acme", session=session)

        self.assertEqual(
            session.requests[0]["url"],
            "https://secure4.saashr.com/ta/rest/ui/recruitment/companies/%7C6095384/job-requisitions",
        )
        # The API counts requisitions from one, not zero.
        self.assertEqual(session.requests[0]["params"]["offset"], 1)

    def test_postings_link_back_to_the_board(self) -> None:
        session = FakeSession([FakeResponse(self.PAYLOAD)])

        jobs = ukg_ready.fetch_jobs(
            "https://secure4.saashr.com/ta/6095384.careers", "Acme", session=session
        )

        self.assertEqual(
            jobs[0].job_url, "https://secure4.saashr.com/ta/6095384.careers?ApplyToJob=1006959249"
        )

    def test_an_empty_board_is_not_an_error(self) -> None:
        session = FakeSession([FakeResponse({"job_requisitions": []})])

        self.assertEqual(
            ukg_ready.fetch_jobs(
                "https://secure4.saashr.com/ta/6095384.careers", "Acme", session=session
            ),
            [],
        )


class TestAdpRecruitingManagement(unittest.TestCase):
    """The board is browser-only, and its payload carries no posting URLs."""

    def test_it_accepts_only_the_recruiting_management_host(self) -> None:
        self.assertTrue(adp_rm.parse_board_url("https://myjobs.adp.com/acme/cx/job-listing"))

        # A WorkforceNow board belongs to the other ADP adapter entirely.
        with self.assertRaises(AdapterUrlError):
            adp_rm.parse_board_url(
                "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=1"
            )

    def test_it_returns_nothing_without_reaching_the_network_when_browserless(self) -> None:
        configure(browser_fallback=False)
        session = FakeSession()

        jobs = adp_rm.fetch_jobs("https://myjobs.adp.com/acme/cx/job-listing", "Acme", session=session)

        self.assertEqual(jobs, [])
        self.assertEqual(session.requests, [])

    def test_it_rebuilds_a_posting_url_from_the_requisition_id(self) -> None:
        posting = {"reqid": "R-4471", "jobtitle": "Systems Engineer"}

        self.assertEqual(
            adp_rm._posting_url(posting, "https://myjobs.adp.com/acme/cx/job-listing",
                                "https://myjobs.adp.com", "acme"),
            "https://myjobs.adp.com/acme/cx/job-details?reqId=R-4471",
        )

    def test_a_posting_with_no_identifier_gets_no_url(self) -> None:
        self.assertEqual(
            adp_rm._posting_url({"jobtitle": "Systems Engineer"}, "https://x/", "https://x", "acme"),
            "",
        )

    def test_the_slug_is_read_from_the_board_path(self) -> None:
        self.assertEqual(adp_rm._board_slug("https://myjobs.adp.com/kearfott/cx/job-listing"), "kearfott")
        self.assertEqual(adp_rm._board_slug("https://myjobs.adp.com/"), "")


#: One row per hosted-HTML adapter: the module, a board URL it must accept, a
#: posting URL in that vendor's real layout, and its ``Platform`` label.
_HOSTED: Tuple[Tuple[Any, str, str, str], ...] = (
    (paylocity, "https://recruiting.paylocity.com/recruiting/jobs/All/abc/Acme",
     "/recruiting/jobs/Details/4180929/abc/IT-Support-Specialist", "Paylocity"),
    (paycom, "https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey=A1",
     "/v4/ats/web.php/jobs/ViewJobDetails?job=55&clientkey=A1", "Paycom"),
    (paycor, "https://recruiting.paycor.com/career/CareerHome.action?clientId=8",
     "/career/JobIntroduction.action?id=99&clientId=8", "Paycor"),
    (jazzhr, "https://acme.applytojob.com/apply/",
     "/apply/aBcD1234/systems-administrator", "JazzHR"),
    (applicantpro, "https://acme.applicantpro.com/jobs/",
     "/jobs/3312345-warehouse-lead.html", "ApplicantPro"),
    (isolved, "https://acme.isolvedhire.com/jobs/",
     "/jobs/998877.html", "isolved"),
    (hrmdirect, "https://careers-acme.hrmdirect.com/employment/job-openings.php",
     "/employment/job-opening.php?req=3210", "HRMDirect"),
    (silkroad, "https://acme.silkroad.com/epostings/index.cfm?fuseaction=app.jobsearch",
     "/epostings/index.cfm?fuseaction=app.jobinfo&jobid=44", "SilkRoad"),
    (clearcompany, "https://acme.clearcompany.com/careers/jobs",
     "/careers/jobs/1a2b3c4d-1111-2222-3333-444455556666/apply", "ClearCompany"),
    (careerplug, "https://acme.careerplug.com/jobs",
     "/jobs/2233445/apps/new", "CareerPlug"),
    (hireology, "https://acme.hireology.com/",
     "/careers/1234567/service-advisor", "Hireology"),
    (applicantstack, "https://acme.applicantstack.com/x/openings",
     "/x/detail/a2abcdef", "ApplicantStack"),
    (recruiterbox, "https://acme.recruiterbox.com/jobs",
     "/jobs/fk0abcd/quality-engineer", "Trakstar Hire"),
    (oleeo, "https://acme.tal.net/vx/candidate/jobboard/vacancy/1/adv/",
     "/vx/candidate/postings/9911", "Oleeo"),
    (jobscore, "https://careers.jobscore.com/careers/acme",
     "/careers/acme/jobs/staff-accountant-abcd1234", "JobScore"),
    (gohire, "https://acme.gohire.io/",
     "/j/kd93jd", "GoHire"),
    (fountain, "https://acme.fountain.com/",
     "/positions/9f8e7d6c", "Fountain"),
    (zoho_recruit, "https://acme.zohorecruit.com/jobs/Careers",
     "/jobs/Careers/551234/Field-Technician", "Zoho Recruit"),
    (join, "https://join.com/companies/acme",
     "/companies/acme/9911223-frontend-developer", "Join.com"),
    (radancy, "https://acme.talentbrew.com/search-jobs",
     "/job/554433/regional-manager", "Radancy"),
    (indeed, "https://www.indeed.com/cmp/Acme/jobs",
     "/viewjob?jk=abc123", "Indeed"),
    (avature, "https://acme.avature.net/careers/SearchJobs",
     "/careers/JobDetail/network-engineer/8812", "Avature"),
    (neogov, "https://www.governmentjobs.com/careers/acme",
     "/careers/acme/jobs/4455667/civil-engineer", "NeoGov"),
    # These vendors address postings by opaque id with no distinguishing path,
    # so their adapters supply no pattern and rely on the generic routes. A
    # plain job-card layout is what those routes read.
    (bullhorn, "https://acme.bullhornstaffing.com/careers/",
     "/jobs/12345", "Bullhorn"),
    (talentreef, "https://jobs.talentreef.com/acme",
     "/jobs/12345", "TalentReef"),
    (homerun, "https://acme.homerun.co/",
     "/jobs/12345", "Homerun"),
    (gem, "https://jobs.gem.com/acme",
     "/jobs/12345", "Gem"),
)

#: Boards for two different vendors, used to check that each adapter rejects
#: URLs that are not its own.
_FOREIGN_URL: str = "https://boards.greenhouse.io/someoneelse"


def _board_markup(job_path: str) -> str:
    """Build a board page listing three postings at a vendor's URL layout.

    Three, because the generic card detector needs a repeating group before it
    will call something a job list.

    Args:
        job_path: A posting path in the vendor's layout.

    Returns:
        The markup.
    """
    rows = "".join(
        f'<li class="job"><a href="{job_path}{"" if index == 0 else f"?v={index}"}">'
        f"Systems Engineer {index}</a>"
        f'<span class="location">Austin, TX</span></li>'
        for index in range(3)
    )
    return f'<ul class="jobs">{rows}</ul>'


class TestHostedBoardAdapters(unittest.TestCase):
    """Every hosted-HTML adapter honours the same contract."""

    def test_each_accepts_its_own_board_url(self) -> None:
        for module, board_url, _, _ in _HOSTED:
            with self.subTest(adapter=module.__name__):
                self.assertTrue(module.parse_board_url(board_url))

    def test_each_rejects_another_vendors_url(self) -> None:
        for module, _, _, _ in _HOSTED:
            with self.subTest(adapter=module.__name__):
                with self.assertRaises(AdapterUrlError):
                    module.parse_board_url(_FOREIGN_URL)

    def test_each_rejects_an_empty_url(self) -> None:
        for module, _, _, _ in _HOSTED:
            with self.subTest(adapter=module.__name__):
                with self.assertRaises(AdapterUrlError):
                    module.parse_board_url("")

    def test_each_extracts_postings_from_its_board(self) -> None:
        for module, board_url, job_path, label in _HOSTED:
            with self.subTest(adapter=module.__name__):
                # Two pages of responses: some adapters page by building each
                # page's URL, so they ask for a second one before stopping.
                session = FakeSession([html(_board_markup(job_path)), html("<p>No more</p>")])

                jobs = module.fetch_jobs(board_url, "Acme", session=session)

                self.assertEqual(len(jobs), 3, f"{module.__name__} found {len(jobs)}")
                self.assertEqual(jobs[0].job_title, "Systems Engineer 0")
                self.assertEqual(jobs[0].platform, label)
                self.assertEqual(jobs[0].location, "Austin, TX")
                self.assertEqual(jobs[0].country, "United States")
                self.assertTrue(jobs[0].job_url.startswith("http"))

    def test_each_reports_an_empty_board_as_no_jobs_rather_than_an_error(self) -> None:
        for module, board_url, _, _ in _HOSTED:
            with self.subTest(adapter=module.__name__):
                session = FakeSession([html("<p>We have no openings right now.</p>")])

                self.assertEqual(module.fetch_jobs(board_url, "Acme", session=session), [])

    def test_a_link_back_to_the_board_is_never_a_posting(self) -> None:
        """Language switchers and pagers link to the board's own URL.

        Oleeo's board path is ``/vacancy/<n>/adv/``, and every language link on
        it points back there. A pattern loose enough to match that reported
        sixteen hundred "jobs" with titles like "German" across two companies.
        """
        board = "https://acme.tal.net/vx/candidate/jobboard/vacancy/3/adv/"
        markup = (
            f'<a href="{board}">German</a>'
            f'<a href="{board}?page=2">French</a>'
            '<a href="/vx/candidate/postings/9911">Maintenance Engineer</a>'
        )
        session = FakeSession([html(markup), html("<p>No more</p>")])

        jobs = oleeo.fetch_jobs(board, "Acme", session=session)

        self.assertEqual([job.job_title for job in jobs], ["Maintenance Engineer"])

    def test_each_surfaces_a_dead_board_as_an_error(self) -> None:
        for module, board_url, _, _ in _HOSTED:
            with self.subTest(adapter=module.__name__):
                session = FakeSession([FakeResponse(text="gone", status_code=404)])

                with self.assertRaises(AdapterHttpError):
                    module.fetch_jobs(board_url, "Acme", session=session)


class TestVersionTwoAdapterContract(unittest.TestCase):
    """The new adapters present the same surface to the engine as the old."""

    MODULES = tuple(module for module, _, _, _ in _HOSTED) + (
        adp_rm, asure, breezyhr, comeet, cornerstone, eightfold, manatal, personio, phenom,
        pinpoint, rippling, ukg_ready,
    )

    def test_each_exposes_fetch_jobs_and_a_platform_label(self) -> None:
        for module in self.MODULES:
            with self.subTest(module=module.__name__):
                self.assertTrue(callable(getattr(module, "fetch_jobs", None)))
                self.assertIsInstance(getattr(module, "PLATFORM", None), str)

    def test_platform_labels_match_the_detector(self) -> None:
        labels = {platform.value for platform in Platform}
        for module in self.MODULES:
            with self.subTest(module=module.__name__):
                self.assertIn(module.PLATFORM, labels)

    def test_fetch_jobs_accepts_url_company_and_session(self) -> None:
        import inspect

        for module in self.MODULES:
            with self.subTest(module=module.__name__):
                parameters = list(inspect.signature(module.fetch_jobs).parameters)
                self.assertEqual(parameters[:3], ["career_url", "company_name", "session"])

    def test_every_platform_but_unknown_has_an_adapter(self) -> None:
        """The whole point of version 2: nothing detected is left unhandled."""
        from crawler.crawler_engine import ADAPTER_MODULES

        expected = set(Platform) - {Platform.UNKNOWN}
        self.assertEqual(set(ADAPTER_MODULES), expected)

    def test_every_mapped_adapter_actually_imports(self) -> None:
        from crawler.crawler_engine import ADAPTER_MODULES, build_registry

        self.assertEqual(len(build_registry()), len(ADAPTER_MODULES))


if __name__ == "__main__":
    unittest.main()
