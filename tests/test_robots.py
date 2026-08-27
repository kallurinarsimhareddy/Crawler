"""Unit tests for :mod:`utils.robots`.

Every session here is a fake, so the suite stays offline.

The central case is :meth:`TestUserAgentMatters.test_a_cloudflare_403_on_the_rules_file`.
The stock :class:`urllib.robotparser.RobotFileParser` fetches with
``Python-urllib``, which Cloudflare answers with a 403, and its ``read()`` turns
that into ``disallow_all``. Measured live against the eight sources version 3
uses, that made four of them look forbidden when their published rules allow us.
A crawler that silently refuses every permitted source looks compliant and is
simply broken, so that behaviour is tested against explicitly.
"""

from __future__ import annotations

import unittest
from typing import Dict, List, Optional

from utils.robots import RobotsCache

WORKABLE_ROBOTS = """User-agent: *
Content-Signal: search=yes, ai-input=yes, ai-train=no
Allow: /search/*
Disallow: /search*?*
Disallow: /search
Disallow: /profile*

Sitemap: https://jobs.workable.com/sitemap.xml
"""

REMOTEOK_ROBOTS = """# commentary about content signals

User-agent: *
Content-Signal: search=yes,ai-train=no,use=reference
Allow: /

User-agent: Amazonbot
Disallow: /
"""

STRICT_ROBOTS = """User-agent: *
Disallow: /private/
Crawl-delay: 5
"""


class FakeResponse:
    """The parts of a ``requests`` response this module reads."""

    def __init__(self, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class FakeSession:
    """Records every fetch and returns canned rules."""

    def __init__(self, responses: Optional[Dict[str, FakeResponse]] = None) -> None:
        self.responses = responses or {}
        self.calls: List[Dict[str, object]] = []
        self.closed = False

    def get(self, url: str, headers=None, timeout=None, allow_redirects=True) -> FakeResponse:
        """Return the canned response for a URL, recording the request."""
        self.calls.append({"url": url, "headers": dict(headers or {})})
        return self.responses.get(url, FakeResponse(404, ""))

    def close(self) -> None:
        self.closed = True


def cache_for(rules: Dict[str, FakeResponse], **kwargs) -> tuple:
    """Build a cache backed by one fake session.

    Args:
        rules: URL to canned response.
        **kwargs: Passed through to :class:`RobotsCache`.

    Returns:
        ``(cache, session)``.
    """
    session = FakeSession(rules)
    return RobotsCache(session_factory=lambda: session, **kwargs), session


class TestAllowAndDisallow(unittest.TestCase):
    """Applying published rules."""

    def setUp(self) -> None:
        self.cache, self.session = cache_for(
            {"https://jobs.workable.com/robots.txt": FakeResponse(200, WORKABLE_ROBOTS)}
        )

    def test_the_api_path_is_allowed(self) -> None:
        self.assertTrue(self.cache.can_fetch("https://jobs.workable.com/api/v1/jobs?query=devops"))

    def test_a_disallowed_path_is_refused(self) -> None:
        self.assertFalse(self.cache.can_fetch("https://jobs.workable.com/profile/me"))

    def test_the_verdict_explains_itself(self) -> None:
        verdict = self.cache.verdict("https://jobs.workable.com/profile/me")
        self.assertFalse(verdict.allowed)
        self.assertTrue(verdict.checked)
        self.assertIn("robots.txt", verdict.reason)

    def test_crawl_delay_is_reported(self) -> None:
        cache, _ = cache_for({"https://acme.com/robots.txt": FakeResponse(200, STRICT_ROBOTS)})
        self.assertEqual(cache.verdict("https://acme.com/jobs").crawl_delay, 5.0)


class TestUserAgentMatters(unittest.TestCase):
    """The reason this module exists rather than a call to the stock parser."""

    def test_rules_are_fetched_with_the_crawlers_user_agent(self) -> None:
        cache, session = cache_for(
            {"https://acme.com/robots.txt": FakeResponse(200, "User-agent: *\nAllow: /\n")}
        )
        cache.can_fetch("https://acme.com/jobs")

        agent = str(session.calls[0]["headers"].get("User-Agent", ""))
        self.assertIn("Mozilla", agent)
        self.assertNotIn("urllib", agent.lower())

    def test_a_cloudflare_403_on_the_rules_file_does_not_forbid_everything(self) -> None:
        """The stock parser turns this into ``disallow_all``.

        A 403 on ``robots.txt`` is a bot-defence product answering, not the
        site owner publishing a rule, so it is treated as "rules unknown" and
        resolved by the on-error policy.
        """
        cache, _ = cache_for({"https://remotive.com/robots.txt": FakeResponse(403, "Just a moment")})

        verdict = cache.verdict("https://remotive.com/api/remote-jobs")

        self.assertTrue(verdict.allowed)
        self.assertFalse(verdict.checked)
        self.assertIn("unreadable", verdict.reason)

    def test_the_deny_policy_is_available_for_a_caller_that_wants_it(self) -> None:
        cache, _ = cache_for(
            {"https://remotive.com/robots.txt": FakeResponse(403, "")}, on_error="deny"
        )
        self.assertFalse(cache.can_fetch("https://remotive.com/api/remote-jobs"))

    def test_a_404_means_no_rules_published_which_permits_everything(self) -> None:
        cache, _ = cache_for({"https://hn.algolia.com/robots.txt": FakeResponse(404, "")})
        verdict = cache.verdict("https://hn.algolia.com/api/v1/search")
        self.assertTrue(verdict.allowed)
        self.assertTrue(verdict.checked)

    def test_a_transport_failure_is_rules_unknown(self) -> None:
        class ExplodingSession:
            def get(self, *args, **kwargs):
                raise OSError("connection reset")

        cache = RobotsCache(session_factory=ExplodingSession)
        verdict = cache.verdict("https://acme.com/jobs")
        self.assertTrue(verdict.allowed)
        self.assertFalse(verdict.checked)


class TestContentSignals(unittest.TestCase):
    """Honouring the ``Content-Signal`` declarations sources publish."""

    def test_signals_are_parsed(self) -> None:
        cache, _ = cache_for(
            {"https://jobs.workable.com/robots.txt": FakeResponse(200, WORKABLE_ROBOTS)}
        )
        verdict = cache.verdict("https://jobs.workable.com/api/v1/jobs")
        self.assertEqual(verdict.content_signals["search"], "yes")
        self.assertEqual(verdict.content_signals["ai-train"], "no")

    def test_permits_reads_a_declared_signal(self) -> None:
        cache, _ = cache_for(
            {"https://remoteok.com/robots.txt": FakeResponse(200, REMOTEOK_ROBOTS)}
        )
        verdict = cache.verdict("https://remoteok.com/api")
        self.assertTrue(verdict.permits("search"))
        self.assertFalse(verdict.permits("ai-train"))

    def test_an_undeclared_signal_neither_grants_nor_restricts(self) -> None:
        cache, _ = cache_for(
            {"https://remoteok.com/robots.txt": FakeResponse(200, REMOTEOK_ROBOTS)}
        )
        self.assertIsNone(cache.verdict("https://remoteok.com/api").permits("something-else"))

    def test_signals_outside_the_wildcard_group_are_ignored(self) -> None:
        rules = "User-agent: Amazonbot\nContent-Signal: search=no\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
        cache, _ = cache_for({"https://acme.com/robots.txt": FakeResponse(200, rules)})
        self.assertEqual(cache.verdict("https://acme.com/jobs").content_signals, {})

    def test_comments_are_not_parsed_as_signals(self) -> None:
        cache, _ = cache_for(
            {"https://remoteok.com/robots.txt": FakeResponse(200, REMOTEOK_ROBOTS)}
        )
        signals = cache.verdict("https://remoteok.com/api").content_signals
        self.assertEqual(set(signals), {"search", "ai-train", "use"})


class TestCaching(unittest.TestCase):
    """One fetch per host per run, however many workers ask."""

    def test_repeated_urls_on_one_host_fetch_once(self) -> None:
        cache, session = cache_for(
            {"https://acme.com/robots.txt": FakeResponse(200, "User-agent: *\nAllow: /\n")}
        )
        for index in range(5):
            cache.can_fetch(f"https://acme.com/jobs/{index}")

        self.assertEqual(len(session.calls), 1)

    def test_separate_hosts_are_fetched_separately(self) -> None:
        cache, session = cache_for(
            {
                "https://acme.com/robots.txt": FakeResponse(200, "User-agent: *\nAllow: /\n"),
                "https://other.com/robots.txt": FakeResponse(200, "User-agent: *\nAllow: /\n"),
            }
        )
        cache.can_fetch("https://acme.com/jobs")
        cache.can_fetch("https://other.com/jobs")
        self.assertEqual(len(session.calls), 2)

    def test_clear_forces_a_refetch(self) -> None:
        cache, session = cache_for(
            {"https://acme.com/robots.txt": FakeResponse(200, "User-agent: *\nAllow: /\n")}
        )
        cache.can_fetch("https://acme.com/jobs")
        cache.clear()
        cache.can_fetch("https://acme.com/jobs")
        self.assertEqual(len(session.calls), 2)


class TestUnusableInput(unittest.TestCase):
    """Nothing that is not a crawlable URL may be declared fetchable."""

    def test_blank(self) -> None:
        cache, _ = cache_for({})
        self.assertFalse(cache.can_fetch(""))

    def test_non_http_scheme(self) -> None:
        cache, _ = cache_for({})
        self.assertFalse(cache.can_fetch("mailto:jobs@acme.com"))

    def test_no_network_call_is_made_for_unusable_input(self) -> None:
        cache, session = cache_for({})
        cache.can_fetch("not a url")
        self.assertEqual(session.calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
