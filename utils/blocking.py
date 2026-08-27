"""Name the reason a board could not be read, without trying to defeat it.

A version 2 run reports 389 technical failures as free-text error strings, which
is enough to count them and not enough to act on. "403" covers a board that
bans robots outright, one sitting behind a Cloudflare challenge, and one whose
WAF wants a browser — three different situations with three different correct
responses, only one of which is "try again later".

This module turns those strings and responses into a fixed vocabulary::

    >>> from utils.blocking import classify_text
    >>> classify_text("AdapterHttpError: GET https://x returned HTTP 403: 'Just a moment...'")
    <Block.CLOUDFLARE: 'cloudflare challenge'>

That vocabulary feeds three things: the dashboard's blocked-company counts, the
per-host cooldown that keeps a run from hammering something already refusing it,
and the decision of whether a retry could possibly help.

**Nothing here circumvents anything.** Detection exists so the crawler can stop
politely, record why, and move on to the next company. A CAPTCHA is a request
for a human, an authentication wall means the content is not public, and both
are final answers. The only "escalation" in the system is the version 2 browser
fallback, which renders a page the ordinary way a visitor would.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Final, Mapping, Optional, Tuple

__all__ = [
    "Block",
    "classify_error",
    "classify_response",
    "classify_text",
    "cooldown_seconds",
    "is_retryable",
]


class Block(str, Enum):
    """Why a board did not yield its postings.

    The value is the label written to reports and the dashboard.
    """

    #: Read successfully.
    NONE = "ok"

    #: The host refuses this client. Not a challenge — no amount of waiting
    #: changes it.
    FORBIDDEN = "403 forbidden"

    #: The URL is wrong or the board has moved.
    NOT_FOUND = "404 not found"

    #: Too many requests. The one blocker that a cooldown genuinely fixes.
    RATE_LIMITED = "429 rate limited"

    #: Cloudflare's interstitial. Sometimes self-clearing in a real browser.
    CLOUDFLARE = "cloudflare challenge"

    #: AWS WAF. The iCIMS tenants behind this serve the same gate on every path.
    AWS_WAF = "aws waf"

    #: A puzzle intended for a person. A final answer.
    CAPTCHA = "captcha"

    #: A bot-detection interstitial that is neither Cloudflare nor AWS.
    BOT_CHALLENGE = "bot challenge"

    #: The content is behind a login. Not public, so not ours to read.
    AUTH_REQUIRED = "authentication required"

    #: The URL in the sheet is not a usable board for the platform it names.
    #: Not a blocker at all — nothing is defending anything — and the fix is to
    #: correct the sheet, so it must not be counted among the blocked.
    BAD_URL = "unusable board url"

    #: The adapter says this board only exists once JavaScript has run.
    BROWSER_REQUIRED = "browser required"

    #: The page served markup, but the postings are not in it.
    JS_ONLY = "javascript-only page"

    #: The host is broken rather than defending itself.
    SERVER_ERROR = "server error"

    #: Connection refused, DNS failure, TLS error, timeout.
    NETWORK = "network failure"

    #: Read, but the shape was not understood.
    UNRECOGNISED = "unrecognised response"


#: Blockers worth attempting again later, and how long to wait first. Anything
#: absent from this table is a settled answer that a retry cannot change.
_COOLDOWNS: Final[Mapping[Block, float]] = {
    Block.RATE_LIMITED: 900.0,
    Block.SERVER_ERROR: 300.0,
    Block.NETWORK: 120.0,
    Block.CLOUDFLARE: 600.0,
    Block.BOT_CHALLENGE: 600.0,
}

#: Markers of a Cloudflare challenge. ``cf-ray`` alone is not one: Cloudflare
#: fronts an enormous share of the web perfectly happily, so the header only
#: counts alongside a challenge status or challenge markup.
_CLOUDFLARE_MARKUP: Final[Tuple[str, ...]] = (
    "just a moment...",
    "attention required! | cloudflare",
    "cdn-cgi/challenge-platform",
    "__cf_chl",
    "cf_chl_opt",
    "checking your browser before accessing",
    "ray id:",
    "error 1020",
    "cloudflare to restrict access",
)

#: AWS WAF's challenge and CAPTCHA bundles.
#:
#: ``"aws waf"`` spelled with a space is load-bearing: it is how the iCIMS
#: adapter words its own error, and that single population is 130 of the 389
#: technical failures on the reference sheet. Matching only the ``awswaf``
#: token filed every one of them under "browser required" instead.
_AWS_WAF_MARKUP: Final[Tuple[str, ...]] = (
    "awswaf",
    "aws waf",
    "aws-waf",
    "aws-waf-token",
    "token.awswaf.com",
    "challenge.js",
    "captcha.awswaf.com",
)

#: A puzzle meant for a person.
_CAPTCHA_MARKUP: Final[Tuple[str, ...]] = (
    "recaptcha",
    "hcaptcha",
    "g-recaptcha",
    "captcha",
    "human verification",
    "verify you are human",
    "are you a human",
    "i'm not a robot",
)

#: Vendors of bot detection other than Cloudflare and AWS.
_BOT_MARKUP: Final[Tuple[str, ...]] = (
    "datadome",
    "perimeterx",
    "px-captcha",
    "incapsula",
    "imperva",
    "distil",
    "botdefender",
    "access denied",
    "request unsuccessful",
    "pardon our interruption",
    "unusual traffic",
    "automated traffic",
)

#: A login wall. The content is not public, so it is not the crawler's.
_AUTH_MARKUP: Final[Tuple[str, ...]] = (
    "sign in to continue",
    "please log in",
    "please sign in",
    "login required",
    "authentication required",
    "session expired",
)

#: The wording adapters use when they mean "a browser would read this".
#: Deliberately the same phrases :data:`crawler.crawler_engine._NEEDS_A_BROWSER`
#: groups on, so the two cannot disagree about what a rescue is for.
_BROWSER_MARKUP: Final[Tuple[str, ...]] = (
    "browser-driven",
    "client-side",
    "needs a browser",
    "requires javascript",
    "enable javascript",
    "javascript is required",
)

#: An HTTP status quoted inside a recorded error message.
_STATUS_IN_TEXT: Final[re.Pattern[str]] = re.compile(r"\bHTTP\s+(\d{3})\b", re.IGNORECASE)

#: The exception family adapters raise when the *sheet* is wrong rather than
#: the board — ``AdapterUrlError``, ``WorkdayUrlError`` and their siblings.
#: 80 of the 389 reference failures are these, and filing them as blocked would
#: have the dashboard report a defence that does not exist.
_URL_ERROR: Final[re.Pattern[str]] = re.compile(r"^\s*\w*UrlError\b")

#: Transport failures as ``requests`` phrases them.
_NETWORK_MARKUP: Final[Tuple[str, ...]] = (
    "max retries exceeded",
    "connection refused",
    "connection aborted",
    "connection reset",
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "failed to resolve",
    "read timed out",
    "connect timeout",
    "timed out",
    "ssl",
    "certificate verify failed",
)

#: How much of a body is worth scanning. A challenge announces itself in the
#: first screenful; scanning megabytes of a real board would cost a run.
_SCAN_BYTES: Final[int] = 8192

#: A gate page carries nothing but the gate. Above this, a page that mentions a
#: CAPTCHA is a board with an apply form rather than a wall in front of one.
_CHALLENGE_PAGE_BYTES: Final[int] = 2048

#: A link that only a real board has. A challenge interstitial links to the
#: vendor's help page and to nothing else; it never links to a posting.
_BOARD_LINK: Final[re.Pattern[str]] = re.compile(
    r"href=[\"']?[^\"'>]*(?:/job|/career|/position|/opening|/vacanc|gh_jid|jobid)",
    re.IGNORECASE,
)


def _contains(haystack: str, needles: Tuple[str, ...]) -> bool:
    """Whether any needle appears in the haystack.

    Args:
        haystack: Text to search, already lowercased.
        needles: Lowercase markers.

    Returns:
        ``True`` on the first match.
    """
    return any(needle in haystack for needle in needles)


def _from_markup(body: str) -> Optional[Block]:
    """Identify a blocker from page content alone.

    Order matters: a Cloudflare CAPTCHA mentions both Cloudflare and CAPTCHA,
    and naming the vendor is more useful than naming the widget.

    Args:
        body: Lowercased page text.

    Returns:
        The blocker, or ``None`` if the markup shows no sign of one.
    """
    if _contains(body, _AWS_WAF_MARKUP):
        return Block.AWS_WAF
    if _contains(body, _CLOUDFLARE_MARKUP):
        return Block.CLOUDFLARE
    if _contains(body, _CAPTCHA_MARKUP):
        return Block.CAPTCHA
    if _contains(body, _BOT_MARKUP):
        return Block.BOT_CHALLENGE
    if _contains(body, _AUTH_MARKUP):
        return Block.AUTH_REQUIRED
    if _contains(body, _BROWSER_MARKUP):
        return Block.BROWSER_REQUIRED
    return None


def _from_status(status: int) -> Block:
    """Classify on the status code alone.

    Args:
        status: HTTP status.

    Returns:
        The blocker for that status.
    """
    if status == 403:
        return Block.FORBIDDEN
    if status == 404:
        return Block.NOT_FOUND
    if status == 429:
        return Block.RATE_LIMITED
    if status in (401, 407):
        return Block.AUTH_REQUIRED
    if 500 <= status < 600:
        return Block.SERVER_ERROR
    if 200 <= status < 300:
        return Block.NONE
    return Block.UNRECOGNISED


def classify_response(
    status: int,
    headers: Optional[Mapping[str, str]] = None,
    body: str = "",
) -> Block:
    """Classify a response the crawler actually received.

    Args:
        status: HTTP status code.
        headers: Response headers, in any case.
        body: The response body. Only the first :data:`_SCAN_BYTES` are read.

    Returns:
        What, if anything, blocked the read.
    """
    lowered_body = (body or "")[:_SCAN_BYTES].lower()
    lowered_headers = {
        str(name).lower(): str(value).lower() for name, value in (headers or {}).items()
    }

    # A WAF names itself in a header far more reliably than in its markup.
    if any(name.startswith("x-amzn-waf") for name in lowered_headers):
        return Block.AWS_WAF

    from_markup = _from_markup(lowered_body)

    if 200 <= status < 300:
        # A challenge served with a 200 is the common case: the page renders,
        # it just is not the board.
        if from_markup in (Block.AWS_WAF, Block.CLOUDFLARE, Block.BOT_CHALLENGE):
            return from_markup

        # A CAPTCHA widget on a 200 is ambiguous in a way the others are not:
        # every board with an apply form ships one, usually behind an
        # aria-label. What distinguishes a gate is that it is *only* the gate —
        # short, and linking to no posting. A page that does either of those
        # things is a board that happens to mention a CAPTCHA.
        if (
            from_markup is Block.CAPTCHA
            and len(lowered_body) <= _CHALLENGE_PAGE_BYTES
            and not _BOARD_LINK.search(lowered_body)
        ):
            return Block.CAPTCHA

        return Block.NONE

    # On a non-2xx, the body explains the status. A 403 whose body is a
    # Cloudflare interstitial is a challenge, not a ban.
    if from_markup is not None:
        return from_markup

    # Cloudflare fronting a plain refusal, with no challenge markup.
    if lowered_headers.get("server") == "cloudflare" and status in (403, 503):
        return Block.CLOUDFLARE

    return _from_status(status)


def classify_text(message: str) -> Block:
    """Classify from a recorded error message.

    Version 2 stores failures as one-line strings on
    :class:`~crawler.crawler_engine.CrawlResult`, and the dashboard has to
    account for runs that already happened. This reads those strings.

    Args:
        message: The recorded error, which usually quotes the status and the
            first two hundred characters of the body.

    Returns:
        What blocked the read, or :attr:`Block.NONE` for an empty message.
    """
    text = (message or "").strip()
    if not text:
        return Block.NONE

    lowered = text.lower()

    # Checked before the markup scan: these messages quote the offending URL,
    # and a careers URL can perfectly well contain a word like "captcha".
    if _URL_ERROR.match(text):
        return Block.BAD_URL

    from_markup = _from_markup(lowered)
    if from_markup is not None:
        return from_markup

    found = _STATUS_IN_TEXT.search(text)
    if found:
        return _from_status(int(found.group(1)))

    if _contains(lowered, _NETWORK_MARKUP):
        return Block.NETWORK

    if "no adapter for" in lowered:
        return Block.UNRECOGNISED

    return Block.UNRECOGNISED


def classify_error(error: BaseException) -> Block:
    """Classify an exception raised while reading a board.

    Args:
        error: The exception.

    Returns:
        What blocked the read.
    """
    return classify_text(f"{type(error).__name__}: {error}")


def is_retryable(block: Block) -> bool:
    """Whether attempting this board again later could succeed.

    Args:
        block: The blocker.

    Returns:
        ``True`` for transient conditions. A CAPTCHA, a login wall and a 403
        are settled answers, and retrying them is both futile and rude.
    """
    return block in _COOLDOWNS


def cooldown_seconds(block: Block) -> float:
    """How long to leave a host alone after this blocker.

    Args:
        block: The blocker.

    Returns:
        Seconds to wait before touching the host again. ``0`` when a retry
        would not help, so the run simply moves on.
    """
    return _COOLDOWNS.get(block, 0.0)
