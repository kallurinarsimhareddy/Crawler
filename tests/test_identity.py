"""Unit tests for :mod:`crawler.identity`.

Every case in :class:`TestRealBoardShapes` and :class:`TestRegressionsFoundOnRealData`
is taken from the 237,300 postings of an actual run. The regression class in
particular exists because each of those patterns silently merged hundreds of
real openings onto a single key — the failure that loses a job rather than
merely duplicating one.
"""

from __future__ import annotations

import unittest

from crawler.identity import job_id_from_url, job_identity, normalise_title


class TestNormaliseTitle(unittest.TestCase):
    """Folding a title for the content fallback."""

    def test_punctuation_and_case(self) -> None:
        self.assertEqual(
            normalise_title("Senior DevOps Engineer (Remote)"),
            normalise_title("senior devops engineer - remote"),
        )

    def test_blank(self) -> None:
        self.assertEqual(normalise_title(""), "")
        self.assertEqual(normalise_title(None), "")


class TestRealBoardShapes(unittest.TestCase):
    """Requisition ids as the major vendors actually spell them."""

    def test_greenhouse_board_path(self) -> None:
        self.assertEqual(
            job_id_from_url("https://boards.greenhouse.io/acme/jobs/4012345"), "4012345"
        )

    def test_greenhouse_query_on_a_company_domain(self) -> None:
        self.assertEqual(
            job_id_from_url("https://stripe.com/jobs/search?gh_jid=8130725"), "8130725"
        )

    def test_lever_uuid(self) -> None:
        self.assertEqual(
            job_id_from_url("https://jobs.lever.co/acme/8c150bcb-3e64-45e0-8870-9ea16662bbb2"),
            "8c150bcb-3e64-45e0-8870-9ea16662bbb2",
        )

    def test_workday_requisition(self) -> None:
        self.assertEqual(
            job_id_from_url(
                "https://acme.wd1.myworkdayjobs.com/en-US/External/job/Austin/Engineer_R-12345"
            ),
            "R-12345",
        )

    def test_successfactors_requisition(self) -> None:
        self.assertEqual(
            job_id_from_url("https://careers.acme.com/job/JR0098765/senior-dev"), "JR0098765"
        )

    def test_smartrecruiters_numeric_id(self) -> None:
        self.assertEqual(
            job_id_from_url("https://jobs.smartrecruiters.com/Acme/744000012345678-devops"),
            "744000012345678",
        )

    def test_icims_mid_path_id(self) -> None:
        self.assertEqual(
            job_id_from_url("https://careers.icims.com/jobs/12345/devops/job?iis=Job+Board"),
            "12345",
        )

    def test_ultipro_opportunity_uuid_in_the_query(self) -> None:
        self.assertEqual(
            job_id_from_url(
                "https://recruiting.ultipro.com/ABC/JobBoard/x/OpportunityDetail"
                "?opportunityId=8c150bcb-3e64-45e0-8870-9ea16662bbb2"
            ),
            "8c150bcb-3e64-45e0-8870-9ea16662bbb2",
        )

    def test_a_url_with_no_identifier_yields_nothing(self) -> None:
        self.assertEqual(job_id_from_url("https://acme.com/careers/senior-devops-engineer"), "")

    def test_a_year_in_a_slug_is_not_an_identifier(self) -> None:
        self.assertEqual(job_id_from_url("https://acme.com/careers/2024-summer-internship"), "")

    def test_extraction_is_deterministic(self) -> None:
        """Stability across weeks rests on this being a pure function."""
        url = "https://boards.greenhouse.io/acme/jobs/4012345"
        self.assertEqual(job_id_from_url(url), job_id_from_url(url))


class TestRegressionsFoundOnRealData(unittest.TestCase):
    """Each of these merged hundreds of real postings onto one key."""

    def test_workday_multipart_requisition(self) -> None:
        """``R2026-1792-1``: stopping at ``R2026`` merged 1,241 postings.

        Every requisition opened in 2026 by a tenant that numbers them this way
        produced the identical id.
        """
        first = job_id_from_url(
            "https://qtsdatacenters.wd5.myworkdayjobs.com/en-US/qts/job/Irving-TX/"
            "Development-Project-Manager--TFO-Construction-_R2026-1792-1"
        )
        second = job_id_from_url(
            "https://qtsdatacenters.wd5.myworkdayjobs.com/en-US/qts/job/US-PA-Remote/"
            "Design-and-Engineering-Systems-Engineer_R2026-1809-2"
        )
        self.assertEqual(first, "R2026-1792-1")
        self.assertEqual(second, "R2026-1809-2")
        self.assertNotEqual(first, second)

    def test_ukg_ready_tenant_number_is_not_the_job(self) -> None:
        """``/ta/6173477.careers?ApplyToJob=...`` merged 1,769 postings.

        The path number identifies the employer; the job is in the query.
        """
        first = job_id_from_url("https://secure6.saashr.com/ta/6173477.careers?ApplyToJob=738599114")
        second = job_id_from_url("https://secure6.saashr.com/ta/6173477.careers?ApplyToJob=738600706")
        self.assertEqual(first, "738599114")
        self.assertNotEqual(first, second)

    def test_asure_tenant_number_is_not_the_job(self) -> None:
        first = job_id_from_url(
            "https://secure6.entertimeonline.com/ta/6016426.careers?ApplyToJob=738599491"
        )
        second = job_id_from_url(
            "https://secure6.entertimeonline.com/ta/6016426.careers?ApplyToJob=738599490"
        )
        self.assertNotEqual(first, second)

    def test_a_uuid_high_in_the_path_is_not_a_posting(self) -> None:
        """``/data/<uuid>/news/13331`` merged 1,139 items onto 152 keys.

        That UUID keys a press-release feed, not a job.
        """
        first = job_id_from_url(
            "https://clientapi.gcs-web.com/data/1f9dba1e-a19a-4637-a1b4-ce42452b4bb0/news/13331"
        )
        second = job_id_from_url(
            "https://clientapi.gcs-web.com/data/1f9dba1e-a19a-4637-a1b4-ce42452b4bb0/news/13311"
        )
        self.assertEqual(first, "")
        self.assertEqual(second, "")

    def test_a_uuid_before_a_company_slug_is_still_a_posting(self) -> None:
        """Paylocity puts the id second to last, so depth 2 is required."""
        self.assertEqual(
            job_id_from_url(
                "https://recruiting.paylocity.com/recruiting/jobs/All/"
                "4e2d7ba5-4123-4d2e-9361-d333390a5ad3/Steinway-Inc"
            ),
            "4e2d7ba5-4123-4d2e-9361-d333390a5ad3",
        )

    def test_postings_sharing_one_page_stay_distinct(self) -> None:
        """A board that links every posting to its own listing page.

        Keying on the URL alone merged them and lost all but the first.
        """
        page = "https://qualitymfgcorp.com/careers/listings"
        machinist = job_identity("Quality Mfg", page, "CNC Machinist", website="qualitymfgcorp.com")
        press = job_identity("Quality Mfg", page, "Press Operator", website="qualitymfgcorp.com")
        self.assertNotEqual(machinist.job_uid, press.job_uid)
        self.assertEqual(machinist.basis, "url")


class TestIdentityStability(unittest.TestCase):
    """What must stay the same from one Friday to the next."""

    def setUp(self) -> None:
        self.canonical = job_identity(
            "Acme",
            "https://boards.greenhouse.io/acme/jobs/4012345",
            "Senior DevOps Engineer",
            platform="Greenhouse",
            website="acme.com",
        )

    def test_tracking_parameters_do_not_create_a_new_job(self) -> None:
        later = job_identity(
            "Acme",
            "https://boards.greenhouse.io/acme/jobs/4012345?utm_source=x&gh_src=y",
            "Senior DevOps Engineer",
            platform="Greenhouse",
            website="acme.com",
        )
        self.assertEqual(self.canonical.job_uid, later.job_uid)

    def test_a_renamed_company_does_not_create_a_new_job(self) -> None:
        later = job_identity(
            "Acme Corporation",
            "https://boards.greenhouse.io/acme/jobs/4012345",
            "Senior DevOps Engineer",
            platform="Greenhouse",
            website="www.acme.com",
        )
        self.assertEqual(self.canonical.job_uid, later.job_uid)

    def test_a_moved_url_does_not_create_a_new_job(self) -> None:
        """The whole point of preferring a requisition id over a URL."""
        later = job_identity(
            "Acme",
            "https://acme.com/careers/moved/4012345",
            "Senior DevOps Engineer",
            platform="Greenhouse",
            website="acme.com",
        )
        self.assertEqual(self.canonical.job_uid, later.job_uid)

    def test_basis_is_reported(self) -> None:
        self.assertEqual(self.canonical.basis, "platform-id")
        self.assertFalse(self.canonical.is_inferred)

    def test_every_key_is_carried_not_just_the_winner(self) -> None:
        """weekly_diff re-links on these when the primary key changes."""
        self.assertEqual(self.canonical.job_id, "4012345")
        self.assertTrue(self.canonical.url_key)
        self.assertTrue(self.canonical.content_key)


class TestIdentityIsolation(unittest.TestCase):
    """Requisition id 12345 exists at hundreds of companies."""

    def test_the_same_id_at_two_companies_is_two_jobs(self) -> None:
        first = job_identity("Acme", "https://x/1", "Engineer", platform="Workday",
                             job_id="12345", website="acme.com")
        second = job_identity("Other", "https://x/2", "Engineer", platform="Workday",
                              job_id="12345", website="other.com")
        self.assertNotEqual(first.job_uid, second.job_uid)

    def test_the_same_id_on_two_platforms_is_two_jobs(self) -> None:
        first = job_identity("Acme", "https://x/1", "Engineer", platform="Workday",
                             job_id="12345", website="acme.com")
        second = job_identity("Acme", "https://x/3", "Engineer", platform="Paylocity",
                              job_id="12345", website="acme.com")
        self.assertNotEqual(first.job_uid, second.job_uid)

    def test_an_adapter_supplied_id_outranks_the_url(self) -> None:
        supplied = job_identity("Acme", "https://acme.com/careers/eng", "Engineer",
                                platform="Workday", job_id="R-999", website="acme.com")
        self.assertEqual(supplied.basis, "platform-id")
        self.assertEqual(supplied.job_id, "R-999")


class TestContentFallback(unittest.TestCase):
    """Boards that publish a posting with no link at all."""

    def test_used_only_when_there_is_no_url(self) -> None:
        identity = job_identity("Acme", "", "Engineer", "Austin, TX", website="acme.com")
        self.assertEqual(identity.basis, "content")
        self.assertTrue(identity.is_inferred)

    def test_location_punctuation_does_not_matter(self) -> None:
        first = job_identity("Acme", "", "Engineer", "Austin, TX", website="acme.com")
        second = job_identity("Acme", "", "Engineer", "Austin TX", website="acme.com")
        self.assertEqual(first.job_uid, second.job_uid)

    def test_two_different_titles_are_two_jobs(self) -> None:
        first = job_identity("Acme", "", "Engineer", "Austin, TX", website="acme.com")
        second = job_identity("Acme", "", "Technician", "Austin, TX", website="acme.com")
        self.assertNotEqual(first.job_uid, second.job_uid)

    def test_a_job_with_nothing_at_all_still_gets_a_key(self) -> None:
        identity = job_identity("", "", "")
        self.assertTrue(identity.job_uid)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
