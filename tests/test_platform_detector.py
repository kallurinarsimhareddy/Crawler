"""Unit tests for :mod:`crawler.platform_detector`.

Detection is URL-only, so every case here is a pure string-in / enum-out check
and the suite runs without network access or extra dependencies.
"""

from __future__ import annotations

import unittest

from crawler.platform_detector import (
    Platform,
    detect_by_host,
    detect_by_path,
    detect_platform,
    match_host,
    match_path,
    normalise_url,
    split_url,
)


class TestNormaliseUrl(unittest.TestCase):
    """Raw input-sheet cells become something urlsplit can read."""

    def test_adds_scheme_to_bare_host(self) -> None:
        self.assertEqual(normalise_url("www.acme.com/careers"), "https://www.acme.com/careers")

    def test_keeps_existing_scheme(self) -> None:
        self.assertEqual(normalise_url("http://acme.com"), "http://acme.com")

    def test_expands_protocol_relative(self) -> None:
        self.assertEqual(normalise_url("//acme.com/jobs"), "https://acme.com/jobs")

    def test_strips_padding_and_quotes(self) -> None:
        self.assertEqual(normalise_url('  "https://acme.com"  '), "https://acme.com")

    def test_empty_input_stays_empty(self) -> None:
        for value in ("", "   ", None):
            with self.subTest(value=value):
                self.assertEqual(normalise_url(value), "")


class TestSplitUrl(unittest.TestCase):
    """Hostname and path are extracted in the shape the rules expect."""

    def test_lowercases_and_drops_www(self) -> None:
        self.assertEqual(split_url("https://WWW.Acme.com/Careers"), ("acme.com", "/careers"))

    def test_keeps_query_appended_to_path(self) -> None:
        self.assertEqual(
            split_url("https://acme.com/careers?pid=42"),
            ("acme.com", "/careers?pid=42"),
        )

    def test_strips_port_and_trailing_dot(self) -> None:
        self.assertEqual(split_url("https://acme.com.:8443/jobs"), ("acme.com", "/jobs"))

    def test_rejects_non_http_scheme(self) -> None:
        self.assertIsNone(split_url("mailto:jobs@acme.com"))
        self.assertIsNone(split_url("ftp://acme.com/jobs"))

    def test_rejects_input_without_host(self) -> None:
        self.assertIsNone(split_url(""))
        self.assertIsNone(split_url("/careers"))

    def test_rejects_filler_cells_that_are_not_hostnames(self) -> None:
        for value in ("N/A", "TBD", "none", "not a url", "no careers page found"):
            with self.subTest(value=value):
                self.assertIsNone(split_url(value))


class TestMatchers(unittest.TestCase):
    """The two primitive matchers behave on label and substring boundaries."""

    def test_host_matches_domain_and_subdomain(self) -> None:
        self.assertTrue(match_host("greenhouse.io", ("greenhouse.io",)))
        self.assertTrue(match_host("boards.greenhouse.io", ("greenhouse.io",)))

    def test_host_does_not_match_lookalike(self) -> None:
        self.assertFalse(match_host("notgreenhouse.io", ("greenhouse.io",)))
        self.assertFalse(match_host("greenhouse.io.evil.com", ("greenhouse.io",)))

    def test_host_handles_empty_input(self) -> None:
        self.assertFalse(match_host("", ("greenhouse.io",)))

    def test_path_matches_substring(self) -> None:
        self.assertTrue(match_path("/en/careersection/2/jobsearch", ("/careersection/",)))
        self.assertFalse(match_path("/careers", ("/careersection/",)))

    def test_path_handles_empty_patterns(self) -> None:
        self.assertFalse(match_path("/careers", ()))


class TestDetectPlatformByHost(unittest.TestCase):
    """Every supported vendor is recognised from a realistic board URL."""

    CASES = (
        ("https://acme.wd1.myworkdayjobs.com/en-US/External", Platform.WORKDAY),
        ("https://boards.greenhouse.io/acme", Platform.GREENHOUSE),
        ("https://job-boards.greenhouse.io/acme", Platform.GREENHOUSE),
        ("https://jobs.lever.co/acme", Platform.LEVER),
        ("https://jobs.ashbyhq.com/acme", Platform.ASHBY),
        ("https://careers-acme.icims.com/jobs/search", Platform.ICIMS),
        ("https://recruiting.ultipro.com/ACM1001/JobBoard/abc-123", Platform.ULTIPRO),
        ("https://recruiting2.ultipro.com/ACM1001/JobBoard/abc-123", Platform.ULTIPRO),
        ("https://careers.ukgpro.com/acme", Platform.UKG),
        ("https://jobs.smartrecruiters.com/Acme", Platform.SMARTRECRUITERS),
        ("https://career5.successfactors.eu/career?company=acme", Platform.SUCCESSFACTORS),
        ("https://jobs.sap.com/search/", Platform.SUCCESSFACTORS),
        ("https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience", Platform.ORACLE),
        ("https://acme.taleo.net/careersection/ex/jobsearch.ftl", Platform.TALEO),
        ("https://jobs.jobvite.com/acme", Platform.JOBVITE),
        ("https://acme.teamtailor.com/jobs", Platform.TEAMTAILOR),
        ("https://acme.bamboohr.com/careers", Platform.BAMBOOHR),
        ("https://acme.recruitee.com/", Platform.RECRUITEE),
        ("https://apply.workable.com/acme/", Platform.WORKABLE),
        ("https://acme.dayforcehcm.com/CandidatePortal/en-US/acme", Platform.DAYFORCE),
        ("https://workforcenow.adp.com/jobs", Platform.ADP),
        ("https://acme.csod.com/ux/ats/careersite/4/home", Platform.CORNERSTONE),
        ("https://acme.eightfold.ai/careers", Platform.EIGHTFOLD),
        ("https://acme.phenompeople.com/us/en/search-results", Platform.PHENOM),
        # --- Added in version 2 ---------------------------------------------
        # myjobs.adp.com must win over the adp.com rule: it is a different
        # product with a different adapter, so its rule is listed first.
        ("https://myjobs.adp.com/acmecareers/cx/job-listing", Platform.ADP_RM),
        ("https://recruiting.paylocity.com/recruiting/jobs/All/abc/Acme", Platform.PAYLOCITY),
        ("https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey=A1", Platform.PAYCOM),
        ("https://recruiting.paycor.com/career/CareerHome.action?clientId=8", Platform.PAYCOR),
        ("https://secure.saashr.com/ta/6100000.careers?CompanyId=1", Platform.UKG_READY),
        ("https://acme.isolvedhire.com/jobs/", Platform.ISOLVED),
        ("https://acme.entertimeonline.com/ta/6100.careers", Platform.ASURE),
        ("https://acme.applytojob.com/apply/", Platform.JAZZHR),
        ("https://ats.rippling.com/acme/jobs", Platform.RIPPLING),
        ("https://acme.jobs.personio.de/", Platform.PERSONIO),
        ("https://acme.avature.net/careers/SearchJobs", Platform.AVATURE),
        ("https://acme.bullhornstaffing.com/careers/", Platform.BULLHORN),
        ("https://acme.breezy.hr/", Platform.BREEZYHR),
        ("https://acme.pinpointhq.com/", Platform.PINPOINT),
        ("https://www.comeet.co/jobs/acme/93.00A", Platform.COMEET),
        ("https://acme.fountain.com/", Platform.FOUNTAIN),
        ("https://www.governmentjobs.com/careers/acme", Platform.NEOGOV),
        ("https://acme.tal.net/vx/candidate/jobboard/vacancy/1/adv/", Platform.OLEEO),
        ("https://careers.jobscore.com/careers/acme", Platform.JOBSCORE),
        ("https://acme.gohire.io/", Platform.GOHIRE),
        ("https://acme.homerun.co/", Platform.HOMERUN),
        ("https://join.com/companies/acme", Platform.JOIN),
        ("https://acme.zohorecruit.com/jobs/Careers", Platform.ZOHO_RECRUIT),
        ("https://acme.manatal.com/", Platform.MANATAL),
        ("https://jobs.gem.com/acme", Platform.GEM),
        ("https://jobs.talentreef.com/acme", Platform.TALENTREEF),
        ("https://acme.applicantpro.com/jobs/", Platform.APPLICANTPRO),
        ("https://acme.applicantstack.com/x/openings", Platform.APPLICANTSTACK),
        ("https://acme.clearcompany.com/careers/jobs", Platform.CLEARCOMPANY),
        ("https://acme.careerplug.com/jobs", Platform.CAREERPLUG),
        ("https://acme.hireology.com/", Platform.HIREOLOGY),
        ("https://careers-acme.hrmdirect.com/employment/job-openings.php", Platform.HRMDIRECT),
        ("https://acme.silkroad.com/epostings/index.cfm", Platform.SILKROAD),
        ("https://acme.recruiterbox.com/jobs", Platform.RECRUITERBOX),
        ("https://acme.talentbrew.com/search-jobs", Platform.RADANCY),
        ("https://www.indeed.com/cmp/Acme/jobs", Platform.INDEED),
    )

    def test_known_boards(self) -> None:
        for url, expected in self.CASES:
            with self.subTest(url=url):
                self.assertIs(detect_platform(url), expected)

    def test_every_vendor_is_covered(self) -> None:
        """Guards against adding a Platform member with no host test."""
        covered = {expected for _, expected in self.CASES}
        expected = set(Platform) - {Platform.GENERIC_HTML, Platform.UNKNOWN}
        self.assertEqual(covered, expected)

    def test_detect_by_host_returns_none_for_company_domain(self) -> None:
        self.assertIsNone(detect_by_host("acme.com"))


class TestDetectPlatformByPath(unittest.TestCase):
    """Vendor URL layouts are recognised on a company's own domain."""

    CASES = (
        ("https://careers.acme.com/wday/cxs/acme/External/jobs", Platform.WORKDAY),
        ("https://acme.com/careers/careersection/2/jobsearch.ftl", Platform.TALEO),
        ("https://careers.acme.com/hcmUI/CandidateExperience/en/sites/CX", Platform.ORACLE),
        ("https://jobs.acme.com/JobBoard/abc-123/JobDetails", Platform.ULTIPRO),
        ("https://careers.acme.com/CandidatePortal/en-US/acme", Platform.DAYFORCE),
        ("https://careers.acme.com/ux/ats/careersite/4/home", Platform.CORNERSTONE),
        ("https://acme.com/embed/job_board?for=acme", Platform.GREENHOUSE),
        ("https://acme.com/jobs/embed2.php?version=1.0.0", Platform.BAMBOOHR),
        (
            "https://acme.com/mascsr/default/mdf/recruitment/recruitment.html?cid=1",
            Platform.ADP,
        ),
        ("https://careers.acme.com/sfcareer/jobreqcareer?jobId=1", Platform.SUCCESSFACTORS),
    )

    def test_company_hosted_boards(self) -> None:
        for url, expected in self.CASES:
            with self.subTest(url=url):
                self.assertIs(detect_platform(url), expected)

    def test_detect_by_path_returns_none_for_plain_careers_page(self) -> None:
        self.assertIsNone(detect_by_path("/about/careers"))


class TestPrecedence(unittest.TestCase):
    """Host beats path, and overlapping vendors resolve the documented way."""

    def test_host_wins_over_path(self) -> None:
        # A Greenhouse-hosted board whose path also carries the Taleo marker.
        self.assertIs(
            detect_platform("https://boards.greenhouse.io/acme/careersection/2"),
            Platform.GREENHOUSE,
        )

    def test_ultipro_wins_over_ukg(self) -> None:
        self.assertIs(
            detect_platform("https://recruiting.ultipro.com/ACM1001/JobBoard/x"),
            Platform.ULTIPRO,
        )

    def test_taleo_wins_over_oracle(self) -> None:
        self.assertIs(
            detect_platform("https://acme.taleo.net/careersection/ex/jobsearch.ftl"),
            Platform.TALEO,
        )


class TestFallbacks(unittest.TestCase):
    """Generic vs unknown split on whether the URL is crawlable at all."""

    def test_company_careers_page_is_generic(self) -> None:
        self.assertIs(detect_platform("https://acme.com/about/careers"), Platform.GENERIC_HTML)

    def test_scheme_less_company_url_is_generic(self) -> None:
        self.assertIs(detect_platform("www.acme.com/careers/"), Platform.GENERIC_HTML)

    def test_unusable_input_is_unknown(self) -> None:
        for url in (
            "",
            "   ",
            None,
            "not a url",
            "N/A",
            "TBD",
            "mailto:jobs@acme.com",
            "javascript:void(0)",
        ):
            with self.subTest(url=url):
                self.assertIs(detect_platform(url), Platform.UNKNOWN)


class TestPlatformEnum(unittest.TestCase):
    """The enum doubles as the label written to the Platform column."""

    def test_value_is_the_export_label(self) -> None:
        self.assertEqual(Platform.ICIMS.value, "iCIMS")
        self.assertEqual(Platform.SUCCESSFACTORS.value, "SAP SuccessFactors")
        self.assertEqual(str(Platform.GENERIC_HTML), "Generic HTML")

    def test_behaves_as_a_string(self) -> None:
        self.assertEqual(Platform.WORKDAY, "Workday")

    def test_labels_are_unique(self) -> None:
        labels = [member.value for member in Platform]
        self.assertEqual(len(labels), len(set(labels)))


if __name__ == "__main__":
    unittest.main()
