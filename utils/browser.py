"""Render a career page in a real browser when plain HTTP is not enough.

Roughly a third of the boards in a real company sheet publish nothing useful
over ``requests``: the listings arrive by XHR after page load, sit behind an
anti-bot interstitial that only clears once JavaScript runs, or appear a screen
at a time as the visitor scrolls. For those, this module drives headless
Chromium through Playwright and hands back three things:

* the **rendered DOM**, after the listings have painted;
* every **JSON response the page fetched**, which is usually the board's own
  private API and a far cleaner source than the DOM it produced;
* the **request log, headers and a screenshot**, which is what the
  unknown-platform diagnostics need in order to make the next adapter writable.

It also drives the two interactions that HTML alone cannot express — clicking
*Load more* until it stops appearing, and scrolling until the page stops
growing.

**Optional by design.** Playwright is a heavy dependency and a browser is slow,
so nothing here is imported at module load and every entry point degrades to
``None`` when Playwright or its browser is missing. The crawler then behaves
exactly as it did before, minus the browser fallback:

    >>> from utils.browser import render
    >>> page = render("https://acme.com/careers")
    >>> page.html if page else "browser unavailable"

**Threading.** Playwright's synchronous API is bound to the thread that created
it, so each worker thread lazily gets its own browser and must release it with
:func:`close_current_thread` before it exits. :func:`shutdown` is the
whole-process equivalent for the main thread.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Final, List, Optional, Tuple

from loguru import logger

from utils.http import USER_AGENT

__all__ = [
    "BROWSER_AVAILABLE",
    "RenderedPage",
    "browser_available",
    "close_current_thread",
    "render",
    "shutdown",
]

#: Milliseconds to wait for the document, and then for the network to settle.
DEFAULT_NAVIGATION_TIMEOUT: Final[int] = 45_000
DEFAULT_SETTLE_TIMEOUT: Final[int] = 12_000

#: Extra pause after settling, for boards that render a tick after their XHR.
DEFAULT_SETTLE_PAUSE: Final[int] = 1_200

#: Ceilings on the two growth interactions. Both stop early once the page stops
#: changing; these only bound a board that would otherwise page forever.
DEFAULT_LOAD_MORE_CLICKS: Final[int] = 25
DEFAULT_SCROLL_ROUNDS: Final[int] = 30

#: Largest JSON response body kept from the network log, in bytes.
MAX_CAPTURED_BYTES: Final[int] = 8 * 1024 * 1024

#: Ceiling on captured JSON responses, so a chatty page cannot exhaust memory.
MAX_CAPTURED_RESPONSES: Final[int] = 120

#: Text on the control that reveals more postings. Matched case-insensitively
#: against a button or link's own text.
_LOAD_MORE_TEXT: Final[Tuple[str, ...]] = (
    "load more",
    "show more",
    "view more",
    "see more",
    "more jobs",
    "more results",
    "more openings",
    "more positions",
    "load more jobs",
    "show more jobs",
    "show all",
    "view all jobs",
    "see all jobs",
    "next page",
)

#: Selectors that mean "the listings have painted". Tried in order; the first
#: that appears wins, and none appearing is not an error.
_READY_SELECTORS: Final[Tuple[str, ...]] = (
    "[data-automation-id='jobResults']",
    "[class*='job-list']",
    "[class*='jobList']",
    "[class*='job-card']",
    "[class*='jobCard']",
    "[class*='opening']",
    "[class*='position']",
    "[id*='job']",
    "a[href*='/job']",
    "a[href*='/career']",
    "main",
)

#: Resource types that never contain job data and cost the most to fetch.
_BLOCKED_RESOURCES: Final[frozenset] = frozenset({"image", "media", "font"})

#: Document titles that mean "this is not the page, it is the bouncer". iCIMS
#: fronts every tenant with the AWS WAF one, which is the single largest cause
#: of lost companies in a real run.
_CHALLENGE_TITLES: Final[Tuple[str, ...]] = (
    "human verification",
    "just a moment",
    "attention required",
    "checking your browser",
    "verifying you are human",
    "one moment, please",
    "access denied",
    "security check",
    "please wait",
)

#: Markup that identifies a challenge even when the title does not.
_CHALLENGE_MARKUP: Final[Tuple[str, ...]] = (
    "awswaf.com/",
    "challenge-platform",
    "cf-browser-verification",
    "_incapsula_resource",
    "px-captcha",
)

#: Markup that identifies a *CAPTCHA* — a puzzle put there for a human to
#: solve — as opposed to a challenge script that clears itself. Waiting on one
#: of these can only ever time out, so it is recognised and abandoned at once
#: rather than retried. Some iCIMS tenants sit behind one; those companies are
#: reported as blocked, which is the honest outcome.
_CAPTCHA_MARKUP: Final[Tuple[str, ...]] = (
    "captcha.awswaf.com",
    'id="captcha"',
    "g-recaptcha",
    "h-captcha",
    "hcaptcha.com",
    "recaptcha/api.js",
)

#: Times to wait out a challenge and reload before giving up on it.
DEFAULT_CHALLENGE_ATTEMPTS: Final[int] = 3

#: Milliseconds given to the challenge script to compute its token. AWS WAF
#: takes several seconds; anything under about four is reliably too short.
DEFAULT_CHALLENGE_WAIT: Final[int] = 6_000

#: Set once, the first time availability is probed.
BROWSER_AVAILABLE: Optional[bool] = None

#: Per-thread Playwright handle and browser, because the sync API is not
#: shareable across threads.
_LOCAL: Final[threading.local] = threading.local()

#: Guards the one-off availability probe.
_PROBE_LOCK: Final[threading.Lock] = threading.Lock()


@dataclass
class RenderedPage:
    """What a browser visit produced.

    Attributes:
        url: URL after any redirects — the page actually rendered.
        html: The rendered DOM, after load-more clicks and scrolling.
        status: HTTP status of the document response, or ``0`` if unknown.
        headers: Response headers of the document.
        payloads: Every JSON body the page fetched, decoded. Usually contains
            the board's own API response, which beats parsing the DOM.
        requests: URLs the page requested, for diagnostics.
        title: Document title.
        screenshot: Path of the screenshot written, when one was asked for.
        error: Why the visit failed, or ``None``.
    """

    url: str = ""
    html: str = ""
    status: int = 0
    headers: Dict[str, str] = field(default_factory=dict)
    payloads: List[Any] = field(default_factory=list)
    requests: List[str] = field(default_factory=list)
    title: str = ""
    screenshot: str = ""
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        """Whether the page rendered without error."""
        return self.error is None and bool(self.html)


def browser_available() -> bool:
    """Report whether Playwright and its browser can actually be used.

    The result is probed once and cached: importing Playwright is cheap, but
    discovering that its browser was never downloaded is not.

    Returns:
        ``True`` when :func:`render` can be expected to work.
    """
    global BROWSER_AVAILABLE

    if BROWSER_AVAILABLE is not None:
        return BROWSER_AVAILABLE

    with _PROBE_LOCK:
        if BROWSER_AVAILABLE is not None:  # pragma: no cover - lost the race
            return BROWSER_AVAILABLE

        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError as exc:
            logger.info("Browser fallback disabled: Playwright is not installed ({})", exc)
            BROWSER_AVAILABLE = False
            return False

        BROWSER_AVAILABLE = True
        logger.debug("Browser fallback available")
        return True


def _browser() -> Optional[Any]:
    """Return this thread's browser, launching it on first use.

    Returns:
        The browser, or ``None`` if one could not be launched — which is
        reported once per thread and then remembered, so a machine without a
        downloaded browser does not pay the launch cost on every company.
    """
    if getattr(_LOCAL, "failed", False):
        return None

    browser = getattr(_LOCAL, "browser", None)
    if browser is not None:
        return browser

    if not browser_available():
        _LOCAL.failed = True
        return None

    from playwright.sync_api import sync_playwright

    try:
        handle = sync_playwright().start()
        _LOCAL.playwright = handle
        _LOCAL.browser = handle.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
            ],
        )
    except Exception as exc:  # noqa: BLE001 - any launch failure disables the thread
        logger.warning("Browser unavailable on this thread: {}", exc)
        _LOCAL.failed = True
        _LOCAL.browser = None
        handle = getattr(_LOCAL, "playwright", None)
        if handle is not None:
            try:
                handle.stop()
            except Exception:  # noqa: BLE001 - already failing
                pass
            _LOCAL.playwright = None
        return None

    logger.debug("Launched headless Chromium on thread {}", threading.current_thread().name)
    return _LOCAL.browser


def close_current_thread() -> None:
    """Release the browser this thread launched, if any.

    Worker threads must call this before exiting: a Playwright handle is bound
    to its creating thread and cannot be closed from anywhere else.
    """
    browser = getattr(_LOCAL, "browser", None)
    handle = getattr(_LOCAL, "playwright", None)

    for resource in (browser, handle):
        if resource is None:
            continue
        closer = getattr(resource, "close", None) or getattr(resource, "stop", None)
        try:
            if callable(closer):
                closer()
        except Exception:  # noqa: BLE001 - teardown must not raise
            logger.debug("Ignoring browser teardown error", exc_info=True)

    _LOCAL.browser = None
    _LOCAL.playwright = None


def shutdown() -> None:
    """Release the calling thread's browser. Alias kept for readability."""
    close_current_thread()


def _capture(response: Any, payloads: List[Any], requests: List[str]) -> None:
    """Record one network response, keeping the JSON ones.

    A board's own XHR is a far better source than the DOM it renders, so every
    JSON body is decoded and kept for the caller to mine.

    Args:
        response: The Playwright response.
        payloads: Accumulator for decoded JSON bodies.
        requests: Accumulator for requested URLs.
    """
    try:
        url = response.url
    except Exception:  # noqa: BLE001 - the response may already be gone
        return

    if len(requests) < 400:
        requests.append(url)

    if len(payloads) >= MAX_CAPTURED_RESPONSES:
        return

    try:
        content_type = str(response.headers.get("content-type") or "").lower()
    except Exception:  # noqa: BLE001
        return

    looks_json = "json" in content_type or url.lower().rstrip("/").endswith(".json")
    if not looks_json:
        return

    try:
        body = response.body()
    except Exception:  # noqa: BLE001 - redirects and aborted requests have no body
        return

    if not body or len(body) > MAX_CAPTURED_BYTES:
        return

    try:
        payloads.append(json.loads(body.decode("utf-8", "replace")))
    except ValueError:
        return


def _is_challenge(page: Any) -> bool:
    """Report whether the browser is looking at an anti-bot interstitial.

    Args:
        page: The Playwright page.

    Returns:
        ``True`` when the document is a challenge rather than the board.
    """
    try:
        title = (page.title() or "").strip().lower()
    except Exception:  # noqa: BLE001 - mid-navigation
        return False

    if any(marker in title for marker in _CHALLENGE_TITLES):
        return True

    try:
        # Only the head is needed: every challenge names its script there, and
        # reading the whole document on every check is not free.
        head = page.content()[:4000].lower()
    except Exception:  # noqa: BLE001 - mid-navigation
        return False

    return any(marker in head for marker in _CHALLENGE_MARKUP)


def _is_captcha(page: Any) -> bool:
    """Report whether the page is a puzzle meant for a human to solve.

    Args:
        page: The Playwright page.

    Returns:
        ``True`` when the gate is a CAPTCHA rather than a self-clearing
        challenge.
    """
    try:
        head = page.content()[:6000].lower()
    except Exception:  # noqa: BLE001 - mid-navigation
        return False

    return any(marker in head for marker in _CAPTCHA_MARKUP)


def _clear_challenge(
    page: Any,
    attempts: int = DEFAULT_CHALLENGE_ATTEMPTS,
    wait_ms: int = DEFAULT_CHALLENGE_WAIT,
    settle_timeout: int = DEFAULT_SETTLE_TIMEOUT,
) -> bool:
    """Wait out an anti-bot interstitial and reload past it.

    A challenge is not a wall — it is a delay. Its script computes a token,
    sets a cookie and expects the visitor to come back. Plain HTTP cannot run
    that script, which is why version 1 lost every iCIMS tenant; a real browser
    only has to be patient enough to let it finish and then ask again.

    Args:
        page: The Playwright page.
        attempts: Times to wait and reload before giving up.
        wait_ms: Milliseconds to let the challenge script run each time.
        settle_timeout: Milliseconds allowed for the reload to settle.

    Returns:
        ``True`` if the page is no longer a challenge.
    """
    for attempt in range(attempts):
        if not _is_challenge(page):
            return True

        if _is_captcha(page):
            # A puzzle for a human. Waiting cannot resolve it and pretending
            # otherwise would cost every blocked company half a minute.
            logger.info("Browser: {} is behind a CAPTCHA, not retrying", page.url)
            return False

        logger.debug("Browser: waiting out an interstitial on {} (attempt {})", page.url, attempt + 1)
        page.wait_for_timeout(wait_ms)

        try:
            page.reload(wait_until="domcontentloaded", timeout=settle_timeout * 2)
            page.wait_for_load_state("networkidle", timeout=settle_timeout)
        except Exception:  # noqa: BLE001 - a busy page never idles
            page.wait_for_timeout(1_500)

    cleared = not _is_challenge(page)
    if not cleared:
        logger.debug("Browser: could not clear the interstitial on {}", page.url)
    return cleared


def _click_load_more(page: Any, limit: int) -> int:
    """Click the page's *Load more* control until it stops appearing.

    Args:
        page: The Playwright page.
        limit: Ceiling on clicks.

    Returns:
        How many times a control was clicked.
    """
    clicks = 0

    for _ in range(limit):
        clicked = False

        for label in _LOAD_MORE_TEXT:
            try:
                control = page.get_by_role(
                    "button", name=label, exact=False
                ).or_(page.get_by_role("link", name=label, exact=False)).first
                if not control.is_visible(timeout=800):
                    continue
                control.click(timeout=4_000)
                clicked = True
            except Exception:  # noqa: BLE001 - absent or detached control
                continue

            try:
                page.wait_for_load_state("networkidle", timeout=6_000)
            except Exception:  # noqa: BLE001 - a busy page never idles
                page.wait_for_timeout(900)
            break

        if not clicked:
            break
        clicks += 1

    if clicks:
        logger.debug("Browser: clicked a load-more control {} time(s)", clicks)
    return clicks


def _scroll_to_end(page: Any, rounds: int) -> int:
    """Scroll until the document stops growing.

    Args:
        page: The Playwright page.
        rounds: Ceiling on scroll steps.

    Returns:
        How many scroll steps ran.
    """
    previous = 0
    steps = 0
    stable = 0

    for _ in range(rounds):
        try:
            page.mouse.wheel(0, 20_000)
            page.wait_for_timeout(650)
            height = int(page.evaluate("document.body ? document.body.scrollHeight : 0") or 0)
        except Exception:  # noqa: BLE001 - navigation mid-scroll
            break

        steps += 1
        if height <= previous:
            stable += 1
            # Two idle rounds mean the page is genuinely finished, not just slow.
            if stable >= 2:
                break
        else:
            stable = 0
        previous = height

    return steps


def render(
    url: str,
    *,
    wait_selector: Optional[str] = None,
    navigation_timeout: int = DEFAULT_NAVIGATION_TIMEOUT,
    settle_timeout: int = DEFAULT_SETTLE_TIMEOUT,
    load_more_clicks: int = DEFAULT_LOAD_MORE_CLICKS,
    scroll_rounds: int = DEFAULT_SCROLL_ROUNDS,
    screenshot_path: Optional[str] = None,
    capture_network: bool = True,
    clear_challenges: bool = True,
) -> Optional[RenderedPage]:
    """Load a page in headless Chromium and return everything it revealed.

    The visit navigates, waits for the network to settle, clicks any *Load
    more* control until it stops appearing, scrolls until the document stops
    growing, and only then snapshots the DOM.

    Args:
        url: Page to visit.
        wait_selector: CSS selector to wait for before interacting. When
            omitted a short list of common job-listing selectors is tried and
            none matching is not an error.
        navigation_timeout: Milliseconds allowed for the document.
        settle_timeout: Milliseconds allowed for the network to go idle.
        load_more_clicks: Ceiling on *Load more* clicks. ``0`` disables it.
        scroll_rounds: Ceiling on scroll steps. ``0`` disables scrolling.
        screenshot_path: Where to write a full-page screenshot, for
            diagnostics. Nothing is written when omitted.
        capture_network: Whether to decode and keep JSON responses.
        clear_challenges: Whether to wait out an anti-bot interstitial and
            reload past it. Worth several seconds when one is present and
            nothing when one is not, since the check is a title read.

    Returns:
        The rendered page, or ``None`` when no browser is available. A page
        that failed to load is returned with :attr:`RenderedPage.error` set,
        so a caller can tell "no browser" from "the board is down".
    """
    target = str(url or "").strip()
    if not target:
        return None
    if "://" not in target:
        target = f"https://{target}"

    browser = _browser()
    if browser is None:
        return None

    payloads: List[Any] = []
    requests: List[str] = []
    result = RenderedPage(url=target)
    context = None

    try:
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 1200},
            locale="en-US",
            ignore_https_errors=True,
            java_script_enabled=True,
        )
        context.set_default_timeout(settle_timeout)

        page = context.new_page()

        # Images and fonts are pure cost here; the listings are text.
        def _route(route: Any) -> None:
            try:
                if route.request.resource_type in _BLOCKED_RESOURCES:
                    route.abort()
                else:
                    route.continue_()
            except Exception:  # noqa: BLE001 - the request may already be done
                pass

        try:
            page.route("**/*", _route)
        except Exception:  # noqa: BLE001 - routing is an optimisation, not a requirement
            logger.debug("Browser: could not install the resource filter")

        if capture_network:
            page.on("response", lambda response: _capture(response, payloads, requests))

        response = page.goto(target, timeout=navigation_timeout, wait_until="domcontentloaded")
        if response is not None:
            result.status = response.status
            try:
                result.headers = dict(response.headers)
            except Exception:  # noqa: BLE001
                result.headers = {}

        try:
            page.wait_for_load_state("networkidle", timeout=settle_timeout)
        except Exception:  # noqa: BLE001 - polling pages never reach idle
            logger.debug("Browser: {} never went idle, continuing", target)

        if clear_challenges and _clear_challenge(page, settle_timeout=settle_timeout):
            # The document was replaced, so its status is stale. Anything the
            # board fetched before the challenge cleared is noise, not data.
            result.status = 200 if result.status in {401, 403, 405, 429} else result.status

        selectors = (wait_selector,) if wait_selector else _READY_SELECTORS
        for selector in selectors:
            try:
                page.wait_for_selector(selector, timeout=2_500, state="attached")
                break
            except Exception:  # noqa: BLE001 - this page simply has no such node
                continue

        page.wait_for_timeout(DEFAULT_SETTLE_PAUSE)

        if load_more_clicks:
            _click_load_more(page, load_more_clicks)
        if scroll_rounds:
            _scroll_to_end(page, scroll_rounds)
            if load_more_clicks:
                # Scrolling often reveals a control that was below the fold.
                _click_load_more(page, max(1, load_more_clicks // 3))

        result.html = page.content()
        result.url = page.url
        try:
            result.title = page.title()
        except Exception:  # noqa: BLE001
            result.title = ""

        if screenshot_path:
            try:
                page.screenshot(path=screenshot_path, full_page=True)
                result.screenshot = screenshot_path
            except Exception as exc:  # noqa: BLE001 - a screenshot is never essential
                logger.debug("Browser: could not screenshot {}: {}", target, exc)

    except Exception as exc:  # noqa: BLE001 - one page must not end the run
        result.error = f"{type(exc).__name__}: {exc}"
        logger.debug("Browser: {} failed: {}", target, exc)
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass

    result.payloads = payloads
    result.requests = requests

    logger.debug(
        "Browser: {} -> {} chars, {} JSON payload(s), {} request(s)",
        target,
        len(result.html),
        len(payloads),
        len(requests),
    )
    return result
