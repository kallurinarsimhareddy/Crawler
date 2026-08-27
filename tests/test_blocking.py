"""Unit tests for :mod:`utils.blocking`.

Every string in :class:`TestRealFailureStrings` is taken verbatim from
``output/technical_failures.csv`` of a real 8,275-company run. Two of them
encode bugs this module had: the iCIMS population words its wall "AWS WAF" with
a space, and the ``*UrlError`` family is a mistake in the input sheet rather
than a defence on the far end. Together those were 243 of 389 failures.
"""

from __future__ import annotations

import unittest

from utils.blocking import (
    Block,
    classify_error,
    classify_response,
    classify_text,
    cooldown_seconds,
    is_retryable,
)


class TestStatusCodes(unittest.TestCase):
    """Classification from the status alone."""

    def test_success(self) -> None:
        self.assertIs(classify_response(200, {}, "<html>jobs</html>"), Block.NONE)

    def test_forbidden(self) -> None:
        self.assertIs(classify_response(403, {}, "Forbidden"), Block.FORBIDDEN)

    def test_not_found(self) -> None:
        self.assertIs(classify_response(404, {}, "Not found"), Block.NOT_FOUND)

    def test_rate_limited(self) -> None:
        self.assertIs(classify_response(429, {}, "Slow down"), Block.RATE_LIMITED)

    def test_auth_required(self) -> None:
        self.assertIs(classify_response(401, {}, ""), Block.AUTH_REQUIRED)

    def test_server_error(self) -> None:
        self.assertIs(classify_response(503, {}, "unavailable"), Block.SERVER_ERROR)


class TestChallengeDetection(unittest.TestCase):
    """A challenge is named by its vendor, whatever status carries it."""

    def test_cloudflare_interstitial_on_a_403(self) -> None:
        body = '<!DOCTYPE html><html><head><title>Just a moment...</title>'
        self.assertIs(classify_response(403, {}, body), Block.CLOUDFLARE)

    def test_cloudflare_interstitial_on_a_200(self) -> None:
        """The common case: the page renders, it just is not the board."""
        body = '<html><head><title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/x">'
        self.assertIs(classify_response(200, {}, body), Block.CLOUDFLARE)

    def test_aws_waf_named_in_a_header(self) -> None:
        self.assertIs(
            classify_response(202, {"x-amzn-waf-action": "challenge"}, ""), Block.AWS_WAF
        )

    def test_aws_waf_outranks_the_captcha_widget_it_uses(self) -> None:
        body = "<html>captcha.awswaf.com recaptcha</html>"
        self.assertIs(classify_response(403, {}, body), Block.AWS_WAF)

    def test_captcha(self) -> None:
        self.assertIs(classify_response(403, {}, "<div class='g-recaptcha'>"), Block.CAPTCHA)

    def test_other_bot_vendors(self) -> None:
        for markup in ("DataDome", "Powered by PerimeterX", "Incapsula incident"):
            self.assertIs(classify_response(403, {}, markup), Block.BOT_CHALLENGE, markup)

    def test_a_real_board_mentioning_captcha_is_not_blocked(self) -> None:
        """A 200 that is genuinely the board must not be called a challenge.

        Boards carry the word in an aria-label on their apply form.
        """
        body = "<html><h1>Open roles</h1><label>captcha</label><a href='/jobs/1'>Engineer</a>"
        self.assertIs(classify_response(200, {}, body), Block.NONE)

    def test_cloudflare_fronting_a_plain_refusal(self) -> None:
        self.assertIs(classify_response(403, {"Server": "cloudflare"}, "denied"), Block.CLOUDFLARE)

    def test_a_healthy_site_behind_cloudflare_is_not_blocked(self) -> None:
        # Cloudflare fronts an enormous share of the web perfectly happily.
        self.assertIs(
            classify_response(200, {"Server": "cloudflare", "cf-ray": "abc"}, "<h1>Careers</h1>"),
            Block.NONE,
        )


class TestRealFailureStrings(unittest.TestCase):
    """Verbatim messages from ``output/technical_failures.csv``."""

    def test_icims_aws_waf_wall(self) -> None:
        """130 of 389 failures. Filed as "browser required" until "aws waf"
        was matched with a space as well as without."""
        message = (
            "AdapterHttpError: https://careers-hamiltonmedical.icims.com/jobs/search"
            "?ss=1&in_iframe=1&pr=0 served an AWS WAF bot challenge instead of the job "
            "board. iCIMS fronts its portals with a human-verification interstitial that "
            "cannot be satisfied over plain HTTP; reading this tenant needs the "
            "browser-driven path"
        )
        self.assertIs(classify_text(message), Block.AWS_WAF)

    def test_cloudflare_403(self) -> None:
        message = (
            "AdapterHttpError: GET https://celero.inc/careers/ returned HTTP 403: "
            "'<!DOCTYPE html><html lang=\"en-US\"><head><title>Just a moment...</title>'"
        )
        self.assertIs(classify_text(message), Block.CLOUDFLARE)

    def test_adapter_url_error_is_a_sheet_problem_not_a_block(self) -> None:
        """80 of 389 failures. Nothing is defending anything here."""
        message = (
            "AdapterUrlError: No Asure company id in "
            "'https://secure2.entertimeonline.com/ta/PittTankTower.careers?CareersSearch=' "
            "(expected something like https://<host>/ta/<id>.careers)"
        )
        self.assertIs(classify_text(message), Block.BAD_URL)
        self.assertFalse(is_retryable(Block.BAD_URL))

    def test_workday_url_error(self) -> None:
        message = (
            "WorkdayUrlError: Workday URL is missing tenant or site after /recruiting/: "
            "'https://wd5.myworkdaysite.com/recruiting/oatey'"
        )
        self.assertIs(classify_text(message), Block.BAD_URL)

    def test_a_url_error_quoting_a_careers_url_is_not_read_as_markup(self) -> None:
        """The message quotes a URL, which may contain any word at all."""
        message = "AdapterUrlError: not a board: 'https://acme.com/careers/captcha-team'"
        self.assertIs(classify_text(message), Block.BAD_URL)

    def test_plain_403(self) -> None:
        message = (
            "AdapterHttpError: GET https://www.eandm.com/AboutUs/Careers.aspx "
            "returned HTTP 403: 'Forbidden - Security'"
        )
        self.assertIs(classify_text(message), Block.FORBIDDEN)

    def test_network_failure(self) -> None:
        message = (
            "AdapterHttpError: GET https://pilotchemical.com/careers/ failed: "
            "HTTPSConnectionPool(host='pilotchemical.com', port=443): Max retries exceeded"
        )
        self.assertIs(classify_text(message), Block.NETWORK)

    def test_rate_limited(self) -> None:
        message = "AdapterHttpError: GET https://swdurethane.com/careers/ returned HTTP 429: '<html>'"
        self.assertIs(classify_text(message), Block.RATE_LIMITED)

    def test_empty_message_is_not_a_block(self) -> None:
        self.assertIs(classify_text(""), Block.NONE)
        self.assertIs(classify_text(None), Block.NONE)


class TestRetryPolicy(unittest.TestCase):
    """What may be attempted again, and what is a settled answer."""

    def test_transient_blockers_are_retryable(self) -> None:
        for block in (Block.RATE_LIMITED, Block.SERVER_ERROR, Block.NETWORK):
            self.assertTrue(is_retryable(block), block)
            self.assertGreater(cooldown_seconds(block), 0)

    def test_settled_answers_are_not_retried(self) -> None:
        """Retrying these is both futile and rude."""
        for block in (Block.CAPTCHA, Block.AUTH_REQUIRED, Block.FORBIDDEN, Block.NOT_FOUND):
            self.assertFalse(is_retryable(block), block)
            self.assertEqual(cooldown_seconds(block), 0.0)

    def test_rate_limiting_waits_longest(self) -> None:
        self.assertGreater(cooldown_seconds(Block.RATE_LIMITED), cooldown_seconds(Block.NETWORK))

    def test_success_is_not_retryable(self) -> None:
        self.assertFalse(is_retryable(Block.NONE))


class TestClassifyError(unittest.TestCase):
    """Classifying a live exception rather than a stored string."""

    def test_uses_the_type_name_and_message(self) -> None:
        class AdapterUrlError(ValueError):
            pass

        self.assertIs(classify_error(AdapterUrlError("no tenant in 'https://x'")), Block.BAD_URL)

    def test_a_timeout(self) -> None:
        self.assertIs(classify_error(TimeoutError("read timed out")), Block.NETWORK)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
