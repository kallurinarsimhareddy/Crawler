"""Retry policy and per-domain rate limiting.

Two pieces of restraint. The retry policy decides whether a failure is worth
trying again and how long to wait; the limiter decides how fast one site may be
asked, regardless of how many workers are running.

Both are pure and offline: no sleeping on real clocks beyond milliseconds, no
network, no browser.
"""

from __future__ import annotations

import threading
import time
import unittest

from crawler.ratelimit import DomainLimiter, RateLimitConfig
from crawler.retry import RetryPolicy, Verdict, classify
from utils.blocking import Block


class TestClassification(unittest.TestCase):
    """A failure is named before it is acted on.

    The taxonomy is :class:`utils.blocking.Block`, which the crawler already
    uses for its failure reports. Inventing a second one here would mean two
    vocabularies drifting apart.
    """

    def test_a_timeout_is_a_network_failure(self) -> None:
        """utils.blocking folds timeouts into NETWORK; both are transient."""
        self.assertEqual(classify("Read timed out after 30s"), Block.NETWORK)

    def test_an_unnamed_failure_falls_through_to_unrecognised(self) -> None:
        """Which the policy treats as transient, bounded by the attempt cap."""
        self.assertEqual(classify("something nobody has seen before"),
                         Block.UNRECOGNISED)

    def test_rate_limiting_is_recognised(self) -> None:
        self.assertEqual(classify("GET https://x returned HTTP 429"), Block.RATE_LIMITED)

    def test_a_server_error_is_recognised(self) -> None:
        self.assertEqual(classify("GET https://x returned HTTP 503"), Block.SERVER_ERROR)

    def test_forbidden_is_recognised(self) -> None:
        self.assertEqual(classify("GET https://x returned HTTP 403"), Block.FORBIDDEN)

    def test_not_found_is_recognised(self) -> None:
        self.assertEqual(classify("GET https://x returned HTTP 404"), Block.NOT_FOUND)

    def test_an_aws_waf_challenge_is_recognised(self) -> None:
        self.assertEqual(
            classify("served an AWS WAF bot challenge instead of the job board"),
            Block.AWS_WAF,
        )

    def test_a_captcha_is_recognised(self) -> None:
        self.assertEqual(classify("please complete the captcha to continue"),
                         Block.CAPTCHA)

    def test_cloudflare_is_recognised(self) -> None:
        self.assertEqual(classify("Attention Required! | Cloudflare"), Block.CLOUDFLARE)

    def test_an_unusable_url_is_recognised(self) -> None:
        self.assertEqual(
            classify("AdapterUrlError: 'x' is not an iCIMS portal URL"), Block.BAD_URL
        )


class TestRetryVerdicts(unittest.TestCase):
    """Transient failures are retried. Refusals are not."""

    def setUp(self) -> None:
        self.policy = RetryPolicy(max_attempts=3)

    def transient(self):
        """Every failure worth another go, per utils.blocking."""
        return (Block.NETWORK, Block.RATE_LIMITED, Block.SERVER_ERROR,
                Block.UNRECOGNISED)

    def permanent(self):
        """Every failure where trying again is pointless or rude.

        Note Cloudflare and bot challenges are absent: the existing
        `utils.blocking` judgement is that those clear on their own after a
        cooldown, and that judgement is preserved rather than overridden.
        """
        return (Block.NOT_FOUND, Block.BAD_URL, Block.CAPTCHA,
                Block.AWS_WAF, Block.FORBIDDEN, Block.AUTH_REQUIRED)

    def test_transient_failures_are_retried(self) -> None:
        for blocker in self.transient():
            with self.subTest(blocker=blocker.value):
                self.assertTrue(self.policy.decide(blocker, attempt=1).retry)

    def test_permanent_failures_are_not_retried(self) -> None:
        for blocker in self.permanent():
            with self.subTest(blocker=blocker.value):
                self.assertFalse(self.policy.decide(blocker, attempt=1).retry)

    def test_a_refusal_is_marked_blocked_not_merely_failed(self) -> None:
        """Blocked and failed are different facts and are reported apart."""
        for blocker in (Block.CAPTCHA, Block.AWS_WAF, Block.FORBIDDEN):
            with self.subTest(blocker=blocker.value):
                self.assertTrue(self.policy.decide(blocker, attempt=1).blocked)

    def test_a_challenge_that_clears_itself_is_still_retried(self) -> None:
        """Preserving the existing judgement: these are worth waiting out."""
        for blocker in (Block.CLOUDFLARE, Block.BOT_CHALLENGE):
            with self.subTest(blocker=blocker.value):
                self.assertTrue(self.policy.decide(blocker, attempt=1).retry)

    def test_a_404_is_a_failure_not_a_block(self) -> None:
        """Nobody refused us; the board simply is not there."""
        verdict = self.policy.decide(Block.NOT_FOUND, attempt=1)
        self.assertFalse(verdict.retry)
        self.assertFalse(verdict.blocked)

    def test_attempts_are_capped(self) -> None:
        self.assertTrue(self.policy.decide(Block.NETWORK, attempt=2).retry)
        self.assertFalse(self.policy.decide(Block.NETWORK, attempt=3).retry)

    def test_the_cap_is_configurable(self) -> None:
        patient = RetryPolicy(max_attempts=6)
        self.assertTrue(patient.decide(Block.NETWORK, attempt=5).retry)

    def test_success_is_never_a_retry(self) -> None:
        self.assertFalse(self.policy.decide(Block.NONE, attempt=1).retry)


class TestBackoff(unittest.TestCase):
    """Waits grow, and no two waiters wake at the same instant."""

    def setUp(self) -> None:
        self.policy = RetryPolicy(max_attempts=5, base_delay=10.0, jitter=0.0)

    def test_the_delay_grows_with_each_attempt(self) -> None:
        delays = [self.policy.decide(Block.NETWORK, attempt=n).delay for n in (1, 2, 3)]
        self.assertLess(delays[0], delays[1])
        self.assertLess(delays[1], delays[2])

    def test_the_growth_is_exponential(self) -> None:
        first = self.policy.decide(Block.NETWORK, attempt=1).delay
        second = self.policy.decide(Block.NETWORK, attempt=2).delay
        self.assertAlmostEqual(second, first * 2, places=4)

    def test_the_delay_is_capped(self) -> None:
        policy = RetryPolicy(max_attempts=99, base_delay=10.0,
                             max_delay=60.0, jitter=0.0)
        self.assertLessEqual(policy.decide(Block.NETWORK, attempt=20).delay, 60.0)

    def test_jitter_spreads_simultaneous_waiters(self) -> None:
        """Without it, everything that failed together retries together."""
        policy = RetryPolicy(base_delay=10.0, jitter=0.5)
        delays = {policy.decide(Block.NETWORK, attempt=1).delay for _ in range(40)}
        self.assertGreater(len(delays), 1)

    def test_jitter_never_makes_a_delay_negative(self) -> None:
        policy = RetryPolicy(base_delay=1.0, jitter=1.0)
        for _ in range(50):
            self.assertGreaterEqual(policy.decide(Block.NETWORK, attempt=1).delay, 0.0)

    def test_rate_limiting_waits_longer_than_a_network_blip(self) -> None:
        """A 429 is the server asking for room; utils.blocking says 900s vs 120s."""
        policy = RetryPolicy(base_delay=10.0, jitter=0.0)
        self.assertGreater(
            policy.decide(Block.RATE_LIMITED, attempt=1).delay,
            policy.decide(Block.NETWORK, attempt=1).delay,
        )

    def test_retry_after_is_honoured_over_the_computed_delay(self) -> None:
        policy = RetryPolicy(base_delay=1.0, jitter=0.0)
        verdict = policy.decide(Block.RATE_LIMITED, attempt=1, retry_after=120.0)
        self.assertEqual(verdict.delay, 120.0)

    def test_a_shorter_retry_after_still_wins(self) -> None:
        """The server's answer is authoritative in both directions."""
        policy = RetryPolicy(max_attempts=9, base_delay=100.0, jitter=0.0)
        verdict = policy.decide(Block.RATE_LIMITED, attempt=3, retry_after=5.0)
        self.assertEqual(verdict.delay, 5.0)


class TestDomainLimiter(unittest.TestCase):
    """Workers are concurrent; a single domain is not."""

    def test_unrelated_domains_do_not_block_each_other(self) -> None:
        limiter = DomainLimiter(RateLimitConfig(min_delay=0.20, max_concurrent=1))
        started = time.monotonic()
        with limiter.hold("https://a.com/careers"):
            pass
        with limiter.hold("https://b.com/careers"):
            pass
        self.assertLess(time.monotonic() - started, 0.15)

    def test_one_domain_is_paced(self) -> None:
        limiter = DomainLimiter(RateLimitConfig(min_delay=0.10, max_concurrent=4))
        started = time.monotonic()
        for _ in range(3):
            with limiter.hold("https://a.com/careers"):
                pass
        # Three requests, two gaps.
        self.assertGreaterEqual(time.monotonic() - started, 0.18)

    def test_concurrency_on_one_domain_is_capped(self) -> None:
        limiter = DomainLimiter(RateLimitConfig(min_delay=0.0, max_concurrent=2))
        peak = 0
        current = 0
        lock = threading.Lock()

        def visit() -> None:
            """Hold the domain briefly, tracking how many hold it at once."""
            nonlocal peak, current
            with limiter.hold("https://a.com/careers"):
                with lock:
                    current += 1
                    peak = max(peak, current)
                time.sleep(0.05)
                with lock:
                    current -= 1

        threads = [threading.Thread(target=visit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertLessEqual(peak, 2)
        self.assertGreater(peak, 0)

    def test_different_domains_run_in_parallel(self) -> None:
        limiter = DomainLimiter(RateLimitConfig(min_delay=0.0, max_concurrent=1))
        peak = 0
        current = 0
        lock = threading.Lock()

        def visit(host: str) -> None:
            """Hold one domain while others hold theirs."""
            nonlocal peak, current
            with limiter.hold(f"https://{host}/careers"):
                with lock:
                    current += 1
                    peak = max(peak, current)
                time.sleep(0.05)
                with lock:
                    current -= 1

        threads = [threading.Thread(target=visit, args=(f"h{n}.com",)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertGreater(peak, 1, "unrelated domains were serialised")

    def test_a_blank_url_is_not_an_error(self) -> None:
        limiter = DomainLimiter(RateLimitConfig(min_delay=0.05))
        with limiter.hold(""):
            pass

    def test_the_limiter_is_reentrant_across_threads(self) -> None:
        """Twenty threads on one domain must neither deadlock nor crash."""
        limiter = DomainLimiter(RateLimitConfig(min_delay=0.0, max_concurrent=5))
        done = []

        def visit() -> None:
            """Take and release the domain."""
            with limiter.hold("https://a.com/x"):
                done.append(1)

        threads = [threading.Thread(target=visit) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(done), 20)

    def test_a_backoff_defers_that_domain(self) -> None:
        """A 429 should slow the whole domain, not just the one company."""
        limiter = DomainLimiter(RateLimitConfig(min_delay=0.0))
        limiter.back_off("https://a.com/x", seconds=0.20)
        started = time.monotonic()
        with limiter.hold("https://a.com/y"):
            pass
        self.assertGreaterEqual(time.monotonic() - started, 0.15)

    def test_a_backoff_does_not_touch_other_domains(self) -> None:
        limiter = DomainLimiter(RateLimitConfig(min_delay=0.0))
        limiter.back_off("https://a.com/x", seconds=5.0)
        started = time.monotonic()
        with limiter.hold("https://b.com/y"):
            pass
        self.assertLess(time.monotonic() - started, 0.10)

    def test_statistics_are_reported(self) -> None:
        limiter = DomainLimiter(RateLimitConfig(min_delay=0.02))
        for _ in range(3):
            with limiter.hold("https://a.com/x"):
                pass
        stats = limiter.stats()
        self.assertEqual(stats["requests"], 3)
        self.assertGreaterEqual(stats["throttled"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
