"""Unit tests for :mod:`utils.urlkey`.

Two failure modes matter, and they pull in opposite directions. Keeping too much
of a URL means a tracking parameter reports an existing job as new every Friday.
Discarding too much means two distinct postings collapse onto one key and a real
opening silently disappears from the weekly diff. The second is worse, so the
cases that guard against over-normalisation are the ones to read first.
"""

from __future__ import annotations

import unittest

from utils.urlkey import canonical_url, is_tracking_parameter, url_key


class TestTrackingParameters(unittest.TestCase):
    """Which query parameters describe the referral rather than the target."""

    def test_utm_family_by_prefix(self) -> None:
        for name in ("utm_source", "utm_medium", "utm_campaign", "utm_id", "utm_anything"):
            self.assertTrue(is_tracking_parameter(name), name)

    def test_ad_click_identifiers(self) -> None:
        for name in ("gclid", "fbclid", "msclkid", "ttclid", "igshid"):
            self.assertTrue(is_tracking_parameter(name), name)

    def test_ats_source_attribution(self) -> None:
        for name in ("gh_src", "lever-source", "iis", "iisn"):
            self.assertTrue(is_tracking_parameter(name), name)

    def test_case_is_ignored(self) -> None:
        self.assertTrue(is_tracking_parameter("UTM_Source"))
        self.assertTrue(is_tracking_parameter("GCLID"))

    def test_identifiers_are_not_tracking(self) -> None:
        """The parameters that name the posting itself must survive."""
        for name in ("gh_jid", "jobId", "opportunityId", "ApplyToJob", "requisitionId"):
            self.assertFalse(is_tracking_parameter(name), name)


class TestCanonicalisation(unittest.TestCase):
    """Folding the spellings of one URL together."""

    def test_tracking_parameters_are_dropped(self) -> None:
        self.assertEqual(
            url_key("https://acme.com/jobs/42?utm_source=news&gh_src=abc"),
            "https://acme.com/jobs/42",
        )

    def test_host_case_and_www_are_folded(self) -> None:
        self.assertEqual(url_key("https://WWW.Acme.com/jobs/42"), "https://acme.com/jobs/42")

    def test_default_port_is_dropped(self) -> None:
        self.assertEqual(url_key("https://acme.com:443/jobs/42"), "https://acme.com/jobs/42")

    def test_non_default_port_is_kept(self) -> None:
        self.assertIn(":8080", url_key("https://acme.com:8080/jobs/42"))

    def test_trailing_slash_is_folded(self) -> None:
        self.assertEqual(url_key("https://acme.com/jobs/42/"), url_key("https://acme.com/jobs/42"))

    def test_repeated_slashes_are_collapsed(self) -> None:
        self.assertEqual(url_key("https://acme.com//jobs///42"), "https://acme.com/jobs/42")

    def test_query_order_does_not_matter(self) -> None:
        self.assertEqual(
            url_key("https://acme.com/jobs?b=2&a=1"),
            url_key("https://acme.com/jobs?a=1&b=2"),
        )

    def test_every_variant_of_one_posting_agrees(self) -> None:
        variants = (
            "https://WWW.Acme.com/jobs/42/?utm_source=x&gh_src=y#apply",
            "https://acme.com/jobs/42",
            "https://acme.com:443/jobs/42/",
            "http://www.acme.com/jobs/42?fbclid=z",
        )
        keys = {url_key(variant) for variant in variants}
        # http and https differ by scheme, which is deliberate: they can be
        # served by different hosts. Everything else must collapse.
        self.assertEqual(len({key.split("://", 1)[1] for key in keys}), 1)


class TestOverNormalisationIsRefused(unittest.TestCase):
    """The dangerous direction: two postings must never share a key."""

    def test_posting_identifiers_survive(self) -> None:
        self.assertEqual(
            url_key("https://stripe.com/jobs/search?gh_jid=8130725&gh_src=x"),
            "https://stripe.com/jobs/search?gh_jid=8130725",
        )

    def test_two_greenhouse_postings_stay_distinct(self) -> None:
        self.assertNotEqual(
            url_key("https://stripe.com/jobs/search?gh_jid=1"),
            url_key("https://stripe.com/jobs/search?gh_jid=2"),
        )

    def test_routing_fragments_are_kept(self) -> None:
        """Single-page portals put the posting in the fragment."""
        first = url_key("https://acme.com/careers?company=X#/jobdetail?jobId=123")
        second = url_key("https://acme.com/careers?company=X#/jobdetail?jobId=456")
        self.assertNotEqual(first, second)
        self.assertIn("jobId=123", first)

    def test_plain_anchors_are_dropped(self) -> None:
        self.assertEqual(url_key("https://acme.com/jobs/42#apply"), "https://acme.com/jobs/42")

    def test_path_case_is_preserved(self) -> None:
        """Paths are case-sensitive; folding them could merge two postings."""
        self.assertNotEqual(url_key("https://acme.com/Jobs/A"), url_key("https://acme.com/jobs/a"))

    def test_blank_query_value_round_trips(self) -> None:
        # UltiPro boards emit "?q=&o=postedDateDesc"; dropping the blank would
        # change the URL the crawler actually visits.
        self.assertIn("q=", url_key("https://acme.com/board?q=&o=postedDateDesc"))


class TestUnusableInput(unittest.TestCase):
    """Anything that is not a crawlable URL is left exactly as it was."""

    def test_blank(self) -> None:
        self.assertEqual(url_key(""), "")
        self.assertEqual(url_key("   "), "")

    def test_non_http_scheme_is_untouched(self) -> None:
        self.assertEqual(url_key("mailto:jobs@acme.com"), "mailto:jobs@acme.com")

    def test_free_text_is_untouched(self) -> None:
        self.assertEqual(url_key("not a url"), "not a url")

    def test_malformed_url_does_not_raise(self) -> None:
        self.assertIsInstance(canonical_url("https://[not-an-ipv6/jobs"), str)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
