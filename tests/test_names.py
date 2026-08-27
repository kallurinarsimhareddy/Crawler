"""Unit tests for :mod:`utils.names`.

Company identity decides whether next Friday's discovery run adds a company or
recognises one it already has. Two errors are possible and they are not equally
bad: a near-duplicate in the master list is visible and can be merged by hand,
while two genuinely different companies folded onto one key means one of them is
never crawled again. The tests below are weighted accordingly — the ones that
assert two things stay *apart* are the important ones.
"""

from __future__ import annotations

import unittest

from utils.names import (
    company_key,
    company_slug,
    domains_match,
    normalise_name,
    registrable_domain,
    same_company,
)


class TestNormaliseName(unittest.TestCase):
    """Folding the ways one name gets typed."""

    def test_case_and_spacing(self) -> None:
        self.assertEqual(normalise_name("  ACME   Corp  "), "acme corp")

    def test_accents_are_folded(self) -> None:
        self.assertEqual(normalise_name("Nestlé"), "nestle")
        self.assertEqual(normalise_name("Schrödinger"), "schrodinger")

    def test_ampersand_is_spelled_out(self) -> None:
        self.assertEqual(normalise_name("Smith & Wesson"), "smith and wesson")

    def test_leading_article_is_dropped(self) -> None:
        self.assertEqual(normalise_name("The Acme Company"), "acme company")

    def test_blank(self) -> None:
        self.assertEqual(normalise_name(""), "")
        self.assertEqual(normalise_name("   "), "")


class TestCompanySlug(unittest.TestCase):
    """Reducing a name to one comparison token."""

    def test_legal_forms_are_stripped(self) -> None:
        for spelling in ("Acme Corporation", "ACME Corp.", "Acme, Inc.", "Acme LLC", "Acme Ltd"):
            self.assertEqual(company_slug(spelling), "acme", spelling)

    def test_stacked_legal_forms_are_all_stripped(self) -> None:
        self.assertEqual(company_slug("Acme Pvt Ltd"), "acme")

    def test_international_legal_forms(self) -> None:
        self.assertEqual(company_slug("Acme GmbH"), "acme")
        self.assertEqual(company_slug("Acme B.V."), "acme")
        self.assertEqual(company_slug("Acme Sdn Bhd"), "acme")

    def test_accents_and_ampersands_agree(self) -> None:
        self.assertEqual(company_slug("Smith & Wesson"), company_slug("Smith and Wesson"))

    def test_a_name_that_is_only_a_legal_form_is_kept(self) -> None:
        # Stripping to nothing would key every such row identically.
        self.assertEqual(company_slug("Ltd"), "ltd")

    def test_blank(self) -> None:
        self.assertEqual(company_slug(""), "")


class TestSlugDoesNotOverMerge(unittest.TestCase):
    """Words that look like noise but distinguish real companies."""

    def test_group_is_not_stripped(self) -> None:
        self.assertNotEqual(company_slug("Bosch Group"), company_slug("Bosch"))

    def test_holdings_is_not_stripped(self) -> None:
        self.assertNotEqual(company_slug("Acme Holdings"), company_slug("Acme"))

    def test_technologies_is_not_stripped(self) -> None:
        self.assertNotEqual(company_slug("Acme Technologies"), company_slug("Acme"))

    def test_different_companies_stay_apart(self) -> None:
        self.assertNotEqual(company_slug("Acme Systems"), company_slug("Acme Solutions"))


class TestRegistrableDomain(unittest.TestCase):
    """Extracting the domain a company actually owns."""

    def test_plain_domain(self) -> None:
        self.assertEqual(registrable_domain("https://www.acme.com/careers"), "acme.com")
        self.assertEqual(registrable_domain("acme.com"), "acme.com")

    def test_bare_host_without_a_scheme(self) -> None:
        # The sheet's Website column holds "www.acme.com" more often than a URL.
        self.assertEqual(registrable_domain("www.acme.com"), "acme.com")

    def test_subdomains_are_folded(self) -> None:
        self.assertEqual(registrable_domain("careers.eu.acme.com"), "acme.com")

    def test_known_multipart_suffix(self) -> None:
        self.assertEqual(registrable_domain("http://careers.acme.co.uk/jobs"), "acme.co.uk")
        self.assertEqual(registrable_domain("www.acme.com.au"), "acme.com.au")

    def test_unlisted_country_suffix_is_handled_by_the_heuristic(self) -> None:
        """An unlisted ``com.<cc>`` must not collapse every company onto it."""
        self.assertEqual(registrable_domain("https://acme.com.xy/x"), "acme.com.xy")

    def test_port_and_path_are_ignored(self) -> None:
        self.assertEqual(registrable_domain("https://acme.com:8443/a/b?c=d"), "acme.com")

    def test_unusable_input(self) -> None:
        for value in ("", "   ", "localhost", "not a url", "acme"):
            self.assertEqual(registrable_domain(value), "", value)


class TestSharedAtsHostsAreRefused(unittest.TestCase):
    """A board host names the vendor, never the company on it."""

    def test_greenhouse(self) -> None:
        self.assertEqual(registrable_domain("https://boards.greenhouse.io/acme"), "")

    def test_workday(self) -> None:
        self.assertEqual(registrable_domain("https://acme.wd1.myworkdayjobs.com/External"), "")

    def test_a_range_of_vendors(self) -> None:
        for url in (
            "https://jobs.lever.co/acme",
            "https://recruiting.ultipro.com/ABC",
            "https://acme.applytojob.com/apply",
            "https://www.linkedin.com/company/acme/jobs",
        ):
            self.assertEqual(registrable_domain(url), "", url)

    def test_two_tenants_on_one_vendor_are_not_merged(self) -> None:
        self.assertFalse(
            domains_match(
                "https://boards.greenhouse.io/acme",
                "https://boards.greenhouse.io/other",
            )
        )


class TestCompanyKey(unittest.TestCase):
    """The identity a company is stored under."""

    def test_website_wins(self) -> None:
        self.assertEqual(company_key("Acme Corporation", "www.acme.com"), "domain:acme.com")

    def test_career_url_is_used_when_there_is_no_website(self) -> None:
        self.assertEqual(
            company_key("Acme Corp.", "", "https://acme.com/careers"), "domain:acme.com"
        )

    def test_name_is_the_fallback(self) -> None:
        self.assertEqual(company_key("Acme Corporation"), "name:acme")

    def test_an_ats_career_url_falls_through_to_the_name(self) -> None:
        self.assertEqual(
            company_key("Acme Corp.", "", "https://boards.greenhouse.io/acme"), "name:acme"
        )

    def test_nothing_identifiable(self) -> None:
        self.assertEqual(company_key("", "", ""), "")


class TestSameCompany(unittest.TestCase):
    """The question the discovery pipeline actually asks."""

    def test_same_domain_different_names(self) -> None:
        self.assertTrue(
            same_company(
                ("Acme Corporation", "acme.com", ""),
                ("ACME", "https://www.acme.com/x", ""),
            )
        )

    def test_same_name_no_domains(self) -> None:
        self.assertTrue(same_company(("Acme Corporation", "", ""), ("Acme Corp.", "", "")))

    def test_different_domains_are_different_companies(self) -> None:
        self.assertFalse(same_company(("Acme", "acme.com", ""), ("Acme", "acme.io", "")))

    def test_two_blanks_are_not_a_match(self) -> None:
        """Absence of evidence is not evidence of identity."""
        self.assertFalse(same_company(("", "", ""), ("", "", "")))

    def test_a_domain_outranks_a_matching_name(self) -> None:
        # Same name, different owners: the domains settle it.
        self.assertFalse(same_company(("Apex", "apex-tools.com", ""), ("Apex", "apexlogistics.com", "")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
