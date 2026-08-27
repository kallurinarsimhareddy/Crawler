"""Unit tests for the platform adapters.

Every case runs offline: HTTP is replaced by a fake session that replays canned
responses and records the requests it was given, so URL parsing, pagination,
payload conversion and error reporting are all exercised without the network.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict, List, Optional, Sequence

from adapters import (
    adp,
    ashby,
    bamboohr,
    dayforce,
    generic,
    greenhouse,
    icims,
    jobvite,
    lever,
    oracle,
    recruitee,
    smartrecruiters,
    successfactors,
    taleo,
    teamtailor,
    ultipro,
    workable,
)
from utils.http import AdapterHttpError, AdapterUrlError


class FakeResponse:
    """Stand-in for :class:`requests.Response`."""

    def __init__(self, body: Any = None, status_code: int = 200, text: Optional[str] = None) -> None:
        self._body = body
        self.status_code = status_code
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")
        self.encoding = "utf-8"

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


class FakeSession:
    """Replays scripted responses and records every request."""

    def __init__(self, responses: Sequence[Any]) -> None:
        self._responses = list(responses)
        self.requests: List[Dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:  # pragma: no cover - never called with an injected session
        pass


def html(body: str) -> FakeResponse:
    """Build an HTML response."""
    return FakeResponse(text=f"<html><body>{body}</body></html>")


class TestGreenhouse(unittest.TestCase):
    def test_parses_board_token_from_every_url_shape(self) -> None:
        cases = {
            "https://boards.greenhouse.io/acme": "acme",
            "https://job-boards.greenhouse.io/acme/jobs/12345": "acme",
            "https://boards.greenhouse.io/embed/job_board?for=acme": "acme",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(greenhouse.parse_board_token(url)[0], expected)

    def test_eu_tenants_use_the_eu_api_host(self) -> None:
        self.assertEqual(
            greenhouse.parse_board_token("https://job-boards.eu.greenhouse.io/acme")[1],
            "boards-api.eu.greenhouse.io",
        )

    def test_rejects_url_without_a_token(self) -> None:
        with self.assertRaises(AdapterUrlError):
            greenhouse.parse_board_token("")

    def test_converts_postings(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "jobs": [
                            {
                                "title": "Engineer",
                                "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                                "location": {"name": "Austin, TX"},
                            },
                            {"title": "No URL"},
                        ]
                    }
                )
            ]
        )

        jobs = greenhouse.fetch_jobs("https://boards.greenhouse.io/acme", "Acme", session=session)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_title, "Engineer")
        self.assertEqual(jobs[0].country, "United States")
        self.assertEqual(jobs[0].platform, "Greenhouse")

    def test_missing_jobs_list_is_an_error(self) -> None:
        session = FakeSession([FakeResponse({"unexpected": True})])
        with self.assertRaises(AdapterHttpError):
            greenhouse.fetch_jobs("https://boards.greenhouse.io/acme", "Acme", session=session)


class TestLever(unittest.TestCase):
    def test_parses_slug(self) -> None:
        self.assertEqual(lever.parse_company_slug("https://jobs.lever.co/acme/123")[0], "acme")

    def test_eu_host(self) -> None:
        self.assertEqual(
            lever.parse_company_slug("https://jobs.eu.lever.co/acme")[1], "api.eu.lever.co"
        )

    def test_converts_postings(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    [
                        {
                            "text": "Engineer",
                            "hostedUrl": "https://jobs.lever.co/acme/1",
                            "categories": {"location": "London, UK"},
                        }
                    ]
                )
            ]
        )

        jobs = lever.fetch_jobs("https://jobs.lever.co/acme", "Acme", session=session)

        self.assertEqual(jobs[0].location, "London, UK")
        self.assertEqual(jobs[0].country, "United Kingdom")

    def test_non_list_payload_is_an_error(self) -> None:
        session = FakeSession([FakeResponse({"jobs": []})])
        with self.assertRaises(AdapterHttpError):
            lever.fetch_jobs("https://jobs.lever.co/acme", "Acme", session=session)


class TestAshby(unittest.TestCase):
    def test_parses_slug(self) -> None:
        self.assertEqual(ashby.parse_board_slug("https://jobs.ashbyhq.com/acme"), "acme")

    def test_joins_secondary_locations(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "jobs": [
                            {
                                "title": "Engineer",
                                "jobUrl": "https://jobs.ashbyhq.com/acme/1",
                                "location": "Austin, TX",
                                "secondaryLocations": [{"location": "Boston, MA"}],
                            }
                        ]
                    }
                )
            ]
        )

        jobs = ashby.fetch_jobs("https://jobs.ashbyhq.com/acme", "Acme", session=session)

        self.assertEqual(jobs[0].location, "Austin, TX | Boston, MA")
        self.assertEqual(jobs[0].country, "United States")


class TestSmartRecruiters(unittest.TestCase):
    def test_parses_company(self) -> None:
        self.assertEqual(
            smartrecruiters.parse_company_id("https://jobs.smartrecruiters.com/Acme"), "Acme"
        )

    def test_walks_every_page(self) -> None:
        page_one = {
            "totalFound": 101,
            "content": [
                {"id": str(i), "name": f"Job {i}", "location": {"city": "Austin", "country": "us"}}
                for i in range(100)
            ],
        }
        page_two = {
            "totalFound": 101,
            "content": [{"id": "100", "name": "Job 100", "location": {"city": "Austin"}}],
        }
        session = FakeSession([FakeResponse(page_one), FakeResponse(page_two)])

        jobs = smartrecruiters.fetch_jobs(
            "https://jobs.smartrecruiters.com/Acme", "Acme", session=session
        )

        self.assertEqual(len(jobs), 101)
        self.assertEqual([r["params"]["offset"] for r in session.requests], [0, 100])
        self.assertEqual(jobs[0].country, "United States")


class TestRecruitee(unittest.TestCase):
    def test_parses_slug_from_subdomain(self) -> None:
        self.assertEqual(recruitee.parse_company_slug("https://acme.recruitee.com/"), "acme")

    def test_converts_offers(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "offers": [
                            {
                                "title": "Engineer",
                                "careers_url": "https://acme.recruitee.com/o/engineer",
                                "location": "Amsterdam, Netherlands",
                            }
                        ]
                    }
                )
            ]
        )

        jobs = recruitee.fetch_jobs("https://acme.recruitee.com/", "Acme", session=session)

        self.assertEqual(jobs[0].country, "Netherlands")


class TestWorkable(unittest.TestCase):
    def test_parses_account(self) -> None:
        self.assertEqual(workable.parse_account_slug("https://apply.workable.com/acme/"), "acme")

    def test_parses_account_from_every_url_shape(self) -> None:
        """The account is in the path on shared hosts and in the host otherwise.

        Reading the path first took the job id out of a tenant-host URL, so
        ``acme.workable.com/jobs/12345`` looked up the account ``12345``.
        """
        cases = {
            "https://apply.workable.com/acme/": "acme",
            "https://apply.workable.com/acme/j/ABC123/": "acme",
            "https://apply.workable.com/acme/jobs/": "acme",
            "https://acme.workable.com/": "acme",
            "https://acme.workable.com/jobs/12345": "acme",
            "https://acme.workable.com/j/ABC123": "acme",
            "acme.workable.com": "acme",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(workable.parse_account_slug(url), expected)

    def test_rejects_a_url_naming_no_account(self) -> None:
        for url in ("", "https://apply.workable.com/", "https://apply.workable.com/jobs/"):
            with self.subTest(url=url):
                with self.assertRaises(AdapterUrlError):
                    workable.parse_account_slug(url)

    def test_rejects_workables_own_job_aggregator(self) -> None:
        """``jobs.workable.com/search`` lists every customer, not one company.

        Reading ``search`` as an account name attributed a stranger's postings
        to whichever company the sheet had pasted that URL against.
        """
        for url in (
            "https://jobs.workable.com/search?location=United+States",
            "https://jobs.workable.com/browse",
            "https://jobs.workable.com/companies",
        ):
            with self.subTest(url=url):
                with self.assertRaises(AdapterUrlError):
                    workable.parse_account_slug(url)

    def test_uses_the_widget_endpoint(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "jobs": [
                            {
                                "title": "Engineer",
                                "url": "https://apply.workable.com/acme/j/ABC",
                                "city": "Austin",
                                "state": "TX",
                                "country": "United States",
                            }
                        ]
                    }
                )
            ]
        )

        jobs = workable.fetch_jobs("https://apply.workable.com/acme/", "Acme", session=session)

        self.assertEqual(jobs[0].country, "United States")

    def test_falls_back_to_v3_when_the_widget_fails(self) -> None:
        session = FakeSession(
            [
                FakeResponse(status_code=404, text="gone"),
                FakeResponse(
                    {
                        "results": [
                            {
                                "title": "Engineer",
                                "shortcode": "ABC",
                                "locations": [{"city": "Austin", "region": "TX"}],
                            }
                        ],
                        "nextPage": None,
                    }
                ),
            ]
        )

        jobs = workable.fetch_jobs("https://apply.workable.com/acme/", "Acme", session=session)

        self.assertEqual(jobs[0].job_url, "https://apply.workable.com/acme/j/ABC")


class TestBambooHR(unittest.TestCase):
    def test_parses_subdomain(self) -> None:
        self.assertEqual(bamboohr.parse_subdomain("https://acme.bamboohr.com/careers"), "acme")

    def test_builds_posting_urls_from_ids(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "result": [
                            {
                                "id": "42",
                                "jobOpeningName": "Engineer",
                                "location": {"city": "Austin", "state": "TX", "country": "United States"},
                            }
                        ]
                    }
                )
            ]
        )

        jobs = bamboohr.fetch_jobs("https://acme.bamboohr.com/careers", "Acme", session=session)

        self.assertEqual(jobs[0].job_url, "https://acme.bamboohr.com/careers/42")
        self.assertEqual(jobs[0].country, "United States")


class TestUltiPro(unittest.TestCase):
    def test_parses_tenant_and_board(self) -> None:
        board = ultipro.parse_board_url(
            "https://recruiting.ultipro.com/ACM1001/JobBoard/abc-123/OpportunityDetail?opportunityId=x"
        )
        self.assertEqual((board.tenant, board.board_id), ("ACM1001", "abc-123"))

    def test_rejects_url_without_a_board(self) -> None:
        with self.assertRaises(AdapterUrlError):
            ultipro.parse_board_url("https://recruiting.ultipro.com/")

    def test_pages_and_converts(self) -> None:
        opportunities = [
            {
                "Id": str(i),
                "Title": f"Job {i}",
                "Locations": [{"LocalizedDescription": "Austin, TX"}],
            }
            for i in range(3)
        ]
        session = FakeSession([FakeResponse({"opportunities": opportunities, "totalCount": 3})])

        jobs = ultipro.fetch_jobs(
            "https://recruiting.ultipro.com/ACM1001/JobBoard/abc-123", "Acme", session=session
        )

        self.assertEqual(len(jobs), 3)
        self.assertIn("OpportunityDetail?opportunityId=0", jobs[0].job_url)
        self.assertEqual(jobs[0].country, "United States")

    def test_multi_site_postings_are_marked(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "opportunities": [
                            {
                                "Id": "1",
                                "Title": "Engineer",
                                "Locations": [
                                    {"LocalizedDescription": "Austin, TX"},
                                    {"LocalizedDescription": "Boston, MA"},
                                ],
                            }
                        ],
                        "totalCount": 1,
                    }
                )
            ]
        )

        jobs = ultipro.fetch_jobs(
            "https://recruiting.ultipro.com/ACM1001/JobBoard/abc", "Acme", session=session
        )

        self.assertEqual(jobs[0].location, "Austin, TX (+1 more)")


class TestADP(unittest.TestCase):
    def test_parses_client_ids(self) -> None:
        cid, cc_id = adp.parse_client_ids(
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
            "?cid=abc&ccId=123&lang=en_US"
        )
        self.assertEqual((cid, cc_id), ("abc", "123"))

    def test_rejects_url_without_cid(self) -> None:
        with self.assertRaises(AdapterUrlError):
            adp.parse_client_ids("https://workforcenow.adp.com/mascsr/default/mdf/x.html")

    def test_rejects_the_unrelated_myjobs_product(self) -> None:
        with self.assertRaises(AdapterUrlError) as ctx:
            adp.parse_client_ids("https://myjobs.adp.com/acme/cx/job-listing?cid=abc")
        self.assertIn("Recruiting Management", str(ctx.exception))

    def test_converts_requisitions(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "jobRequisitions": [
                            {
                                "requisitionTitle": "Engineer",
                                "itemID": "999",
                                "requisitionLocations": [
                                    {
                                        "address": {
                                            "cityName": "Austin",
                                            "countrySubdivisionLevel1": {"codeValue": "TX"},
                                            "countryCode": "US",
                                        }
                                    }
                                ],
                            }
                        ]
                    }
                )
            ]
        )

        jobs = adp.fetch_jobs(
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=abc",
            "Acme",
            session=session,
        )

        self.assertEqual(jobs[0].job_title, "Engineer")
        self.assertIn("jobId=999", jobs[0].job_url)


class TestOracle(unittest.TestCase):
    def test_parses_site_number(self) -> None:
        host, site = oracle.parse_site(
            "https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions"
        )
        self.assertEqual((host, site), ("acme.fa.us2.oraclecloud.com", "CX_1"))

    def test_rejects_url_without_a_site(self) -> None:
        with self.assertRaises(AdapterUrlError):
            oracle.parse_site("https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience")

    def test_unwraps_the_nested_response(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "items": [
                            {
                                "TotalJobsCount": 1,
                                "requisitionList": [
                                    {"Id": "5", "Title": "Engineer", "PrimaryLocation": "Austin, TX"}
                                ],
                            }
                        ]
                    }
                )
            ]
        )

        jobs = oracle.fetch_jobs(
            "https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
            "Acme",
            session=session,
        )

        self.assertEqual(jobs[0].job_url.endswith("/job/5"), True)


class TestTaleo(unittest.TestCase):
    def test_parses_career_section(self) -> None:
        self.assertEqual(
            taleo.parse_career_section("https://acme.taleo.net/careersection/ex/jobsearch.ftl"),
            ("acme.taleo.net", "ex"),
        )

    def test_reads_rest_rows(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "requisitionList": [
                            {"contestNo": "R1", "column": ["Engineer", "Austin, TX"]},
                        ],
                        "pagingData": {"totalCount": 1},
                    }
                )
            ]
        )

        jobs = taleo.fetch_jobs(
            "https://acme.taleo.net/careersection/ex/jobsearch.ftl", "Acme", session=session
        )

        self.assertEqual(jobs[0].job_title, "Engineer")
        self.assertEqual(jobs[0].location, "Austin, TX")
        self.assertIn("job=R1", jobs[0].job_url)


class TestJobvite(unittest.TestCase):
    def test_parses_board(self) -> None:
        self.assertEqual(
            jobvite.parse_board_url("https://jobs.jobvite.com/acme/job/oABC"),
            "https://jobs.jobvite.com/acme",
        )

    def test_reads_listing_rows(self) -> None:
        session = FakeSession(
            [
                html(
                    '<div class="jv-job-list">'
                    '<div><a href="/acme/job/oABC">Engineer</a>'
                    '<span class="jv-job-list-location">Austin, TX</span></div>'
                    "</div>"
                )
            ]
        )

        jobs = jobvite.fetch_jobs("https://jobs.jobvite.com/acme", "Acme", session=session)

        self.assertEqual(jobs[0].job_title, "Engineer")
        self.assertEqual(jobs[0].location, "Austin, TX")


class TestTeamtailor(unittest.TestCase):
    def test_reduces_to_the_jobs_listing(self) -> None:
        self.assertEqual(
            teamtailor.parse_site_url("https://acme.teamtailor.com/jobs/12345-engineer"),
            "https://acme.teamtailor.com/jobs",
        )

    def test_reads_cards_then_stops_when_a_page_repeats(self) -> None:
        page = html(
            '<li><a href="/jobs/1-engineer"><span>Engineer</span></a>'
            '<div class="location">Stockholm, Sweden</div></li>'
        )
        session = FakeSession([page, page])

        jobs = teamtailor.fetch_jobs("https://acme.teamtailor.com/jobs", "Acme", session=session)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].country, "Sweden")


class TestICIMS(unittest.TestCase):
    def test_parses_portal_host(self) -> None:
        self.assertEqual(
            icims.parse_portal_host("https://careers-acme.icims.com/jobs/search?ss=1"),
            "careers-acme.icims.com",
        )

    def test_rejects_non_icims_url(self) -> None:
        with self.assertRaises(AdapterUrlError):
            icims.parse_portal_host("https://boards.greenhouse.io/acme")

    def test_reads_result_rows(self) -> None:
        session = FakeSession(
            [
                html(
                    '<div class="row"><a href="/jobs/1234/engineer/job">Engineer</a>'
                    '<div class="iCIMS_JobHeaderTag">Austin, TX</div></div>'
                ),
                html("<div></div>"),
            ]
        )

        jobs = icims.fetch_jobs("https://careers-acme.icims.com/jobs/search", "Acme", session=session)

        self.assertEqual(jobs[0].job_title, "Engineer")
        self.assertEqual(jobs[0].location, "Austin, TX")

    def test_bot_challenge_is_reported_precisely(self) -> None:
        """Blocked over HTTP, and still blocked when the browser cannot help.

        The browser is stubbed out rather than left to run: iCIMS now falls
        back to it when HTTP meets the challenge, and a unit test must not
        launch Chromium or reach the network to prove what HTTP reported.
        """
        session = FakeSession([html("<title>Human Verification</title><script>gokuProps</script>")])

        original = icims.render_page
        icims.render_page = lambda _url, **_kwargs: None
        try:
            with self.assertRaises(AdapterHttpError) as ctx:
                icims.fetch_jobs(
                    "https://careers-acme.icims.com/jobs/search", "Acme", session=session
                )
        finally:
            icims.render_page = original

        self.assertIn("bot challenge", str(ctx.exception))


class TestDayforce(unittest.TestCase):
    def test_parses_the_classic_portal(self) -> None:
        self.assertEqual(
            dayforce.parse_portal_url(
                "https://acme.dayforcehcm.com/CandidatePortal/en-US/acme/Posting/View/9"
            ),
            "https://acme.dayforcehcm.com/CandidatePortal/en-US/acme",
        )

    def test_unified_portal_is_reported_as_unsupported(self) -> None:
        with self.assertRaises(AdapterUrlError) as ctx:
            dayforce.parse_portal_url("https://jobs.dayforcehcm.com/ecore/CANDIDATEPORTAL")
        self.assertIn("client-side", str(ctx.exception))

    def test_reads_classic_result_rows(self) -> None:
        session = FakeSession(
            [
                html(
                    '<div><a href="/CandidatePortal/en-US/acme/Posting/View/9">Engineer</a>'
                    '<span class="location">Austin, TX</span></div>'
                ),
                html("<div></div>"),
            ]
        )

        jobs = dayforce.fetch_jobs(
            "https://acme.dayforcehcm.com/CandidatePortal/en-US/acme", "Acme", session=session
        )

        self.assertEqual(jobs[0].job_title, "Engineer")


class TestSuccessFactors(unittest.TestCase):
    def test_reduces_to_the_search_page(self) -> None:
        self.assertEqual(
            successfactors.parse_search_url("https://jobs.acme.com/search/?q=engineer"),
            "https://jobs.acme.com/search/",
        )

    def test_legacy_portal_is_reported_as_unsupported(self) -> None:
        with self.assertRaises(AdapterUrlError) as ctx:
            successfactors.parse_search_url("https://career4.successfactors.com/career?company=acme")
        self.assertIn("legacy", str(ctx.exception))

    def test_reads_career_site_builder_rows(self) -> None:
        session = FakeSession(
            [
                html(
                    '<tr class="data-row">'
                    '<td><a class="jobTitle-link" href="/job/1">Engineer</a></td>'
                    '<td><span class="jobLocation">Austin, TX</span></td></tr>'
                ),
                html("<div></div>"),
            ]
        )

        jobs = successfactors.fetch_jobs("https://jobs.acme.com/search/", "Acme", session=session)

        self.assertEqual(jobs[0].job_title, "Engineer")
        self.assertEqual(jobs[0].location, "Austin, TX")


class TestGenericExtraction(unittest.TestCase):
    """The fallback handles structured data, tables, cards and lists."""

    def test_json_ld(self) -> None:
        markup = """
        <script type="application/ld+json">
        {"@type": "JobPosting", "title": "Engineer",
         "url": "https://acme.com/jobs/1",
         "jobLocation": {"address": {"addressLocality": "Austin", "addressRegion": "TX"}}}
        </script>
        """
        jobs = generic.extract_jobs(markup, "https://acme.com/careers", "Acme")

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].location, "Austin, TX")
        self.assertEqual(jobs[0].country, "United States")

    def test_json_ld_graph_wrapper(self) -> None:
        markup = """
        <script type="application/ld+json">
        {"@graph": [{"@type": "JobPosting", "title": "Engineer", "url": "/jobs/1"}]}
        </script>
        """
        jobs = generic.extract_jobs(markup, "https://acme.com/careers", "Acme")
        self.assertEqual(jobs[0].job_url, "https://acme.com/jobs/1")

    def test_microdata(self) -> None:
        markup = """
        <div itemtype="https://schema.org/JobPosting">
          <span itemprop="title">Engineer</span>
          <span itemprop="jobLocation">Austin, TX</span>
          <a href="/jobs/1">Apply</a>
        </div>
        """
        jobs = generic.extract_jobs(markup, "https://acme.com/careers", "Acme")

        self.assertEqual(jobs[0].job_title, "Engineer")
        self.assertEqual(jobs[0].job_url, "https://acme.com/jobs/1")

    def test_table_rows(self) -> None:
        rows = "".join(
            f'<tr><td><a href="/jobs/{i}">Engineer {i}</a></td>'
            f'<td class="location">Austin, TX</td></tr>'
            for i in range(4)
        )
        jobs = generic.extract_jobs(f"<table>{rows}</table>", "https://acme.com/careers", "Acme")

        self.assertEqual(len(jobs), 4)
        self.assertEqual(jobs[0].location, "Austin, TX")

    def test_card_grid(self) -> None:
        cards = "".join(
            f'<div class="card"><a href="/careers/{i}"><h3>Engineer {i}</h3></a>'
            f'<p class="city">Boston, MA</p></div>'
            for i in range(5)
        )
        jobs = generic.extract_jobs(cards, "https://acme.com/careers", "Acme")

        self.assertEqual(len(jobs), 5)
        self.assertEqual(jobs[0].country, "United States")

    def test_plain_list(self) -> None:
        items = "".join(f'<li><a href="/job/{i}">Engineer {i}</a></li>' for i in range(3))
        jobs = generic.extract_jobs(f"<ul>{items}</ul>", "https://acme.com/careers", "Acme")

        self.assertEqual(len(jobs), 3)

    def test_navigation_links_are_not_jobs(self) -> None:
        markup = (
            '<a href="/careers/">Careers</a>'
            '<a href="/jobs/">View all jobs</a>'
            '<a href="/careers/apply">Apply now</a>'
        )
        self.assertEqual(generic.extract_jobs(markup, "https://acme.com/", "Acme"), [])

    def test_page_with_nothing_returns_empty(self) -> None:
        self.assertEqual(generic.extract_jobs("<p>No openings</p>", "https://acme.com/", "Acme"), [])

    def test_malformed_markup_never_raises(self) -> None:
        for markup in ("", "<<<>>", "<div><a href=", "\x00\x01", "<script>{bad json}</script>"):
            with self.subTest(markup=markup[:12]):
                self.assertIsInstance(generic.extract_jobs(markup, "https://acme.com/", "Acme"), list)

    def test_malformed_json_ld_falls_through_to_cards(self) -> None:
        markup = (
            '<script type="application/ld+json">{not json,,}</script>'
            + "".join(f'<li><a href="/job/{i}">Engineer {i}</a></li>' for i in range(3))
        )
        self.assertEqual(len(generic.extract_jobs(markup, "https://acme.com/", "Acme")), 3)

    def test_button_text_is_never_used_as_a_title(self) -> None:
        """Boards label their links "Apply online", not with the job title.

        Taking the link text produced three hundred rows whose Job Title was
        "Apply online" or "View & Apply". The title is in the card's heading.
        """
        for label in (
            "Apply online",
            "Apply for this position",
            "Apply →",
            "Apply Now",
            "View & Apply",
            "View details",
            "Read more",
            "Learn more about this job",
        ):
            with self.subTest(label=label):
                self.assertFalse(generic.looks_like_title(label))

    def test_real_titles_beginning_with_similar_words_survive(self) -> None:
        for title in (
            "Applications Engineer",
            "Application Security Analyst",
            "Viewership Analytics Manager",
            "Reader Services Librarian",
            "Learning & Development Partner",
            "Findings Coordinator",
        ):
            with self.subTest(title=title):
                self.assertTrue(generic.looks_like_title(title))

    def test_a_card_with_an_apply_button_uses_its_heading(self) -> None:
        cards = "".join(
            f'<div class="card"><h3>Machine Operator {i}</h3>'
            f'<span class="location">Austin, TX</span>'
            f'<a href="/jobs/{i}">Apply online</a></div>'
            for i in range(3)
        )
        jobs = generic.extract_jobs(cards, "https://acme.com/careers", "Acme")

        self.assertEqual(len(jobs), 3)
        self.assertEqual(jobs[0].job_title, "Machine Operator 0")
        self.assertEqual(jobs[0].location, "Austin, TX")

    def test_platform_label_is_passed_through(self) -> None:
        markup = "".join(f'<li><a href="/job/{i}">Engineer {i}</a></li>' for i in range(3))
        jobs = generic.extract_jobs(markup, "https://acme.com/", "Acme", platform="iCIMS")
        self.assertEqual(jobs[0].platform, "iCIMS")

    def test_fetch_stops_when_a_page_adds_nothing(self) -> None:
        page = html("".join(f'<li><a href="/job/{i}">Engineer {i}</a></li>' for i in range(3)))
        session = FakeSession([page])

        jobs = generic.fetch_jobs("https://acme.com/careers", "Acme", session=session)

        self.assertEqual(len(jobs), 3)
        self.assertEqual(len(session.requests), 1)


class TestAdapterContract(unittest.TestCase):
    """Every adapter presents the same surface to the engine."""

    MODULES = (
        adp, ashby, bamboohr, dayforce, generic, greenhouse, icims, jobvite, lever,
        oracle, recruitee, smartrecruiters, successfactors, taleo, teamtailor,
        ultipro, workable,
    )

    def test_each_exposes_fetch_jobs_and_a_platform_label(self) -> None:
        for module in self.MODULES:
            with self.subTest(module=module.__name__):
                self.assertTrue(callable(getattr(module, "fetch_jobs", None)))
                self.assertIsInstance(getattr(module, "PLATFORM", None), str)

    def test_platform_labels_match_the_detector(self) -> None:
        from crawler.platform_detector import Platform

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


if __name__ == "__main__":
    unittest.main()
