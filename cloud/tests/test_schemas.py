"""Request validation: what each job type accepts, refuses and normalises."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from cloud.shared.models import CompanyTarget
from cloud.shared.schemas import (
    MAX_BULK_COMPANIES,
    BulkCompaniesJobRequest,
    DiscoveryJobRequest,
    SingleCompanyJobRequest,
    WeeklyCrawlJobRequest,
    normalise_website,
    parse_job_request,
    request_targets,
)


class TestNormaliseWebsite(unittest.TestCase):
    def test_equivalent_spellings_become_one(self) -> None:
        for spelling in (
            "example.com",
            "Example.COM",
            "https://example.com",
            "https://example.com/",
            "  https://EXAMPLE.com/  ",
            "https://example.com/?utm_source=x#top",
        ):
            with self.subTest(spelling=spelling):
                self.assertEqual(normalise_website(spelling), "https://example.com")

    def test_http_path_and_port_are_kept(self) -> None:
        self.assertEqual(
            normalise_website("http://jobs.example.com:8080/careers/"),
            "http://jobs.example.com:8080/careers",
        )

    def test_unusable_values_are_refused(self) -> None:
        for bad in (
            "",
            "   ",
            "not a url",
            "localhost",
            "ftp://example.com",
            "javascript:alert(1)",
            "https://user:pw@example.com",
            "https://example.com:notaport",
            "https://" + "a" * 2050 + ".com",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalise_website(bad)


class TestSingleCompany(unittest.TestCase):
    def test_the_documented_example(self) -> None:
        request = parse_job_request({"type": "single_company", "website": "https://example.com"})
        self.assertIsInstance(request, SingleCompanyJobRequest)
        self.assertEqual(request.website, "https://example.com")

    def test_a_name_alone_is_enough(self) -> None:
        request = parse_job_request({"type": "single_company", "company_name": " Acme "})
        self.assertEqual(request.company_name, "Acme")
        self.assertIsNone(request.website)

    def test_nothing_to_crawl_is_refused(self) -> None:
        for payload in (
            {"type": "single_company"},
            {"type": "single_company", "website": None, "company_name": "   "},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                parse_job_request(payload)

    def test_an_invalid_website_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            parse_job_request({"type": "single_company", "website": "ftp://example.com"})

    def test_unknown_fields_are_refused(self) -> None:
        with self.assertRaises(ValidationError):
            parse_job_request({"type": "single_company", "website": "example.com", "workers": 16})

    def test_an_overlong_name_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            parse_job_request({"type": "single_company", "company_name": "x" * 201})


class TestOtherTypes(unittest.TestCase):
    def test_discovery_takes_a_company(self) -> None:
        request = parse_job_request({"type": "discovery", "company_name": "Acme"})
        self.assertIsInstance(request, DiscoveryJobRequest)
        with self.assertRaises(ValidationError):
            parse_job_request({"type": "discovery"})

    def test_weekly_crawl_takes_no_target(self) -> None:
        self.assertIsInstance(parse_job_request({"type": "weekly_crawl"}), WeeklyCrawlJobRequest)
        with self.assertRaises(ValidationError):
            parse_job_request({"type": "weekly_crawl", "website": "example.com"})

    def test_bulk_takes_a_non_empty_bounded_list(self) -> None:
        request = parse_job_request(
            {
                "type": "bulk_companies",
                "companies": [{"website": "a.com"}, {"company_name": "Bee"}],
            }
        )
        self.assertIsInstance(request, BulkCompaniesJobRequest)
        self.assertEqual(
            request_targets(request),
            [CompanyTarget(website="https://a.com"), CompanyTarget(company_name="Bee")],
        )
        for companies in ([], [{"website": "a.com"}] * (MAX_BULK_COMPANIES + 1)):
            with self.subTest(size=len(companies)), self.assertRaises(ValidationError):
                parse_job_request({"type": "bulk_companies", "companies": companies})

    def test_one_bad_row_rejects_the_bulk_request_and_says_which(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            parse_job_request(
                {"type": "bulk_companies", "companies": [{"website": "a.com"}, {"website": "nope"}]}
            )
        self.assertIn(1, caught.exception.errors()[0]["loc"])

    def test_an_unknown_or_missing_type_is_refused(self) -> None:
        for payload in ({"type": "full_internet"}, {"website": "example.com"}, {}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                parse_job_request(payload)


if __name__ == "__main__":
    unittest.main()
