"""Unit tests for :mod:`adapters.workday`.

No network is used: the CXS endpoint is stood in for by a fake session that
records the request bodies it was given, so pagination and the loop guards can
be driven deterministically.
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, Sequence

import requests

from adapters.workday import (
    PLATFORM,
    WorkdayApiError,
    WorkdayBoard,
    WorkdayUrlError,
    build_session,
    fetch_jobs,
    parse_board_url,
)


class FakeResponse:
    """Stand-in for :class:`requests.Response` with a canned body."""

    def __init__(self, body: Any = None, status_code: int = 200, text: str = "") -> None:
        self._body = body
        self.status_code = status_code
        self.text = text or (str(body) if body is not None else "")

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("No JSON object could be decoded")
        return self._body


class FakeSession:
    """Session that replays a scripted list of responses and records requests."""

    def __init__(self, responses: Sequence[Any]) -> None:
        self._responses = list(responses)
        self.requests: List[Dict[str, Any]] = []
        self.closed = False

    def post(self, url: str, json: Dict[str, Any], timeout: Any) -> FakeResponse:
        self.requests.append({"url": url, "json": json})

        if not self._responses:
            raise AssertionError(f"Unexpected extra request to {url} with {json}")

        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


def _posting(title: str, path: str, location: str = "Austin, TX") -> Dict[str, Any]:
    """Build one raw ``jobPostings`` entry."""
    return {"title": title, "externalPath": path, "locationsText": location}


def _page(postings: Sequence[Dict[str, Any]], total: int) -> FakeResponse:
    """Build one CXS response page."""
    return FakeResponse({"total": total, "jobPostings": list(postings)})


class TestParseBoardUrl(unittest.TestCase):
    """Every shape a Workday board URL arrives in resolves to tenant + site."""

    def test_language_and_site(self) -> None:
        board = parse_board_url("https://acme.wd1.myworkdayjobs.com/en-US/External")
        self.assertEqual(board.tenant, "acme")
        self.assertEqual(board.site, "External")
        self.assertEqual(board.language, "en-US")

    def test_site_without_language(self) -> None:
        board = parse_board_url("https://acme.wd1.myworkdayjobs.com/External")
        self.assertEqual((board.tenant, board.site, board.language), ("acme", "External", "en-US"))

    def test_myworkdaysite_recruiting_form(self) -> None:
        board = parse_board_url(
            "https://acme.wd5.myworkdaysite.com/en-US/recruiting/acme_tenant/Careers"
        )
        self.assertEqual(board.tenant, "acme_tenant")
        self.assertEqual(board.site, "Careers")

    def test_api_url_form(self) -> None:
        board = parse_board_url("https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs")
        self.assertEqual((board.tenant, board.site), ("acme", "External"))

    def test_posting_url_is_reduced_to_its_board(self) -> None:
        board = parse_board_url(
            "https://acme.wd1.myworkdayjobs.com/en-US/External/job/Austin/Engineer_R-1"
        )
        self.assertEqual((board.tenant, board.site), ("acme", "External"))

    def test_scheme_less_and_padded_input(self) -> None:
        board = parse_board_url("  acme.wd1.myworkdayjobs.com/en-US/External  ")
        self.assertEqual((board.tenant, board.site), ("acme", "External"))

    def test_trailing_slash_and_query(self) -> None:
        board = parse_board_url("https://acme.wd1.myworkdayjobs.com/en-US/External/?q=engineer")
        self.assertEqual((board.tenant, board.site), ("acme", "External"))

    def test_two_letter_site_is_not_read_as_a_locale(self) -> None:
        """Real boards use short uppercase site names like /VR and /WG."""
        board = parse_board_url(
            "https://veradigm.wd12.myworkdayjobs.com/VR?jobFamilyGroup=f419edd004"
        )
        self.assertEqual((board.tenant, board.site), ("veradigm", "VR"))

    def test_locale_is_still_stripped_before_a_site(self) -> None:
        board = parse_board_url("https://acme.wd1.myworkdayjobs.com/fr/Carrieres")
        self.assertEqual((board.language, board.site), ("fr", "Carrieres"))

    def test_rejects_non_workday_host(self) -> None:
        with self.assertRaises(WorkdayUrlError) as ctx:
            parse_board_url("https://boards.greenhouse.io/acme")
        self.assertIn("not a workday url", str(ctx.exception).lower())

    def test_rejects_board_without_site(self) -> None:
        with self.assertRaises(WorkdayUrlError):
            parse_board_url("https://acme.wd1.myworkdayjobs.com/")

    def test_rejects_empty_input(self) -> None:
        for value in ("", "   ", None):
            with self.subTest(value=value):
                with self.assertRaises(WorkdayUrlError):
                    parse_board_url(value)


class TestWorkdayBoard(unittest.TestCase):
    """The board knows how to address its API and its postings."""

    BOARD = WorkdayBoard(host="acme.wd1.myworkdayjobs.com", tenant="acme", site="External")

    def test_api_url(self) -> None:
        self.assertEqual(
            self.BOARD.api_url,
            "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs",
        )

    def test_board_url(self) -> None:
        self.assertEqual(
            self.BOARD.board_url, "https://acme.wd1.myworkdayjobs.com/en-US/External"
        )

    def test_job_url_joins_external_path(self) -> None:
        self.assertEqual(
            self.BOARD.job_url("/job/Austin/Engineer_R-1"),
            "https://acme.wd1.myworkdayjobs.com/en-US/External/job/Austin/Engineer_R-1",
        )

    def test_job_url_of_empty_path_is_empty(self) -> None:
        self.assertEqual(self.BOARD.job_url(""), "")


class TestFetchJobs(unittest.TestCase):
    """Pagination walks the whole board and normalises what it finds."""

    URL = "https://acme.wd1.myworkdayjobs.com/en-US/External"

    def test_single_page(self) -> None:
        session = FakeSession([_page([_posting("Engineer", "/job/Austin/Engineer_R-1")], total=1)])

        jobs = fetch_jobs(self.URL, "Acme", session=session)

        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.company_name, "Acme")
        self.assertEqual(job.job_title, "Engineer")
        self.assertEqual(job.location, "Austin, TX")
        self.assertEqual(job.country, "United States")
        self.assertEqual(
            job.job_url,
            "https://acme.wd1.myworkdayjobs.com/en-US/External/job/Austin/Engineer_R-1",
        )
        self.assertEqual(job.career_page_url, self.URL)
        self.assertEqual(job.platform, PLATFORM)

    def test_walks_every_page(self) -> None:
        pages = [
            _page([_posting(f"Job {i}", f"/job/Austin/J{i}") for i in range(20)], total=45),
            _page([_posting(f"Job {i}", f"/job/Austin/J{i}") for i in range(20, 40)], total=45),
            _page([_posting(f"Job {i}", f"/job/Austin/J{i}") for i in range(40, 45)], total=45),
        ]
        session = FakeSession(pages)

        jobs = fetch_jobs(self.URL, "Acme", session=session)

        self.assertEqual(len(jobs), 45)
        self.assertEqual([r["json"]["offset"] for r in session.requests], [0, 20, 40])
        self.assertEqual({r["json"]["limit"] for r in session.requests}, {20})

    def test_does_not_stop_at_any_result_cap(self) -> None:
        """A large board is returned whole, not truncated."""
        pages = [
            _page(
                [_posting(f"Job {i}", f"/job/Austin/J{i}") for i in range(start, start + 20)],
                total=500,
            )
            for start in range(0, 500, 20)
        ]
        session = FakeSession(pages)

        jobs = fetch_jobs(self.URL, "Acme", session=session)

        self.assertEqual(len(jobs), 500)

    def test_stops_when_a_page_comes_back_empty(self) -> None:
        """A board that overstates its total still terminates."""
        session = FakeSession(
            [
                _page([_posting("Engineer", "/job/Austin/E1")], total=99),
                _page([], total=99),
            ]
        )

        jobs = fetch_jobs(self.URL, "Acme", session=session)

        self.assertEqual(len(jobs), 1)

    def test_paginates_when_total_is_missing(self) -> None:
        session = FakeSession(
            [
                FakeResponse({"jobPostings": [_posting("A", "/job/Austin/A")]}),
                FakeResponse({"jobPostings": []}),
            ]
        )

        jobs = fetch_jobs(self.URL, "Acme", session=session, page_size=1)

        self.assertEqual(len(jobs), 1)

    def test_deduplicates_repeated_postings(self) -> None:
        session = FakeSession(
            [
                _page(
                    [
                        _posting("Engineer", "/job/Austin/E1"),
                        _posting("Engineer", "/job/Austin/E1"),
                        _posting("Analyst", "/job/Austin/A1"),
                    ],
                    total=3,
                )
            ]
        )

        jobs = fetch_jobs(self.URL, "Acme", session=session)

        self.assertEqual(len(jobs), 2)

    def test_skips_postings_without_title_or_path(self) -> None:
        session = FakeSession(
            [
                _page(
                    [
                        {"title": "", "externalPath": "/job/Austin/E1"},
                        {"title": "Analyst", "externalPath": ""},
                        {"externalPath": "/job/Austin/E2"},
                        _posting("Engineer", "/job/Austin/E3"),
                    ],
                    total=4,
                )
            ]
        )

        jobs = fetch_jobs(self.URL, "Acme", session=session)

        self.assertEqual([job.job_title for job in jobs], ["Engineer"])

    def test_multi_location_posting_has_no_country(self) -> None:
        session = FakeSession(
            [_page([_posting("Engineer", "/job/Various/E1", location="3 Locations")], total=1)]
        )

        jobs = fetch_jobs(self.URL, "Acme", session=session)

        self.assertEqual(jobs[0].location, "3 Locations")
        self.assertEqual(jobs[0].country, "")

    def test_empty_board_returns_empty_list(self) -> None:
        session = FakeSession([_page([], total=0)])

        self.assertEqual(fetch_jobs(self.URL, "Acme", session=session), [])

    def test_page_size_is_clamped_to_workday_limit(self) -> None:
        session = FakeSession([_page([], total=0)])

        fetch_jobs(self.URL, "Acme", session=session, page_size=500)

        self.assertEqual(session.requests[0]["json"]["limit"], 20)

    def test_caller_session_is_not_closed(self) -> None:
        session = FakeSession([_page([], total=0)])

        fetch_jobs(self.URL, "Acme", session=session)

        self.assertFalse(session.closed)

    def test_posts_to_the_cxs_endpoint(self) -> None:
        session = FakeSession([_page([], total=0)])

        fetch_jobs(self.URL, "Acme", session=session)

        self.assertEqual(
            session.requests[0]["url"],
            "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs",
        )


class TestLoopGuards(unittest.TestCase):
    """A tenant that mishandles paging must not spin forever."""

    URL = "https://acme.wd1.myworkdayjobs.com/en-US/External"

    def test_stops_when_offset_is_ignored(self) -> None:
        repeated = _posting("Engineer", "/job/Austin/E1")
        session = FakeSession([_page([repeated], total=50) for _ in range(10)])

        jobs = fetch_jobs(self.URL, "Acme", session=session)

        self.assertEqual(len(jobs), 1)
        # Page one is accepted, page two repeats it and ends the crawl.
        self.assertEqual(len(session.requests), 2)


class TestErrorHandling(unittest.TestCase):
    """Failures surface as WorkdayApiError with the board in the message."""

    URL = "https://acme.wd1.myworkdayjobs.com/en-US/External"

    def test_http_error_status(self) -> None:
        session = FakeSession([FakeResponse(status_code=500, text="upstream boom")])

        with self.assertRaises(WorkdayApiError) as ctx:
            fetch_jobs(self.URL, "Acme", session=session)
        self.assertIn("500", str(ctx.exception))

    def test_missing_board_names_tenant_and_site(self) -> None:
        session = FakeSession([FakeResponse(status_code=404, text="not found")])

        with self.assertRaises(WorkdayApiError) as ctx:
            fetch_jobs(self.URL, "Acme", session=session)
        message = str(ctx.exception)
        self.assertIn("acme", message)
        self.assertIn("External", message)

    def test_transport_failure(self) -> None:
        session = FakeSession([requests.ConnectionError("connection reset")])

        with self.assertRaises(WorkdayApiError) as ctx:
            fetch_jobs(self.URL, "Acme", session=session)
        self.assertIn("connection reset", str(ctx.exception))

    def test_non_json_body(self) -> None:
        session = FakeSession([FakeResponse(body=None, status_code=200, text="<html>login</html>")])

        with self.assertRaises(WorkdayApiError) as ctx:
            fetch_jobs(self.URL, "Acme", session=session)
        self.assertIn("non-JSON", str(ctx.exception))

    def test_body_is_not_an_object(self) -> None:
        session = FakeSession([FakeResponse(["unexpected"])])

        with self.assertRaises(WorkdayApiError):
            fetch_jobs(self.URL, "Acme", session=session)

    def test_job_postings_wrong_type(self) -> None:
        session = FakeSession([FakeResponse({"total": 1, "jobPostings": {"nope": True}})])

        with self.assertRaises(WorkdayApiError):
            fetch_jobs(self.URL, "Acme", session=session)

    def test_bad_url_raises_before_any_request(self) -> None:
        session = FakeSession([])

        with self.assertRaises(WorkdayUrlError):
            fetch_jobs("https://boards.greenhouse.io/acme", "Acme", session=session)
        self.assertEqual(session.requests, [])

    def test_errors_share_a_base_class(self) -> None:
        from adapters.workday import WorkdayError

        self.assertTrue(issubclass(WorkdayUrlError, WorkdayError))
        self.assertTrue(issubclass(WorkdayApiError, WorkdayError))


class TestBuildSession(unittest.TestCase):
    """The default session retries transient failures, including on POST."""

    def test_retry_policy_covers_post_and_rate_limiting(self) -> None:
        session = build_session(retries=4)
        try:
            retry = session.get_adapter("https://acme.wd1.myworkdayjobs.com").max_retries
            self.assertEqual(retry.total, 3)
            self.assertIn("POST", retry.allowed_methods)
            self.assertIn(429, retry.status_forcelist)
            self.assertTrue(retry.backoff_factor > 0)
        finally:
            session.close()

    def test_retries_below_one_disable_retrying(self) -> None:
        session = build_session(retries=0)
        try:
            self.assertEqual(session.get_adapter("https://x.myworkdayjobs.com").max_retries.total, 0)
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
