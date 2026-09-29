"""Fetching one page and saying honestly what happened.

All HTTP traffic goes through :class:`cloud.intel.core.http.SafeFetcher` (SSRF
checks on every hop, robots.txt, size/time limits, per-host pacing, an honest
User-Agent). This module adds:

* **classification** — a response becomes an :class:`~cloud.intel.scraper.models.Outcome`
  (``CAPTCHA``, ``WAF``, ``LOGIN_REQUIRED``, ``ROBOTS``, ``TIMEOUT``…) so the run can
  report it;
* **bounded retries** of transient failures only (408/429/5xx/timeouts), with
  exponential backoff and ``Retry-After`` (see :mod:`cloud.intel.scraper.limits`);
* **per-domain limits** — concurrency, a request budget and a pause after 429/503;
* **browser rendering** — optional, off by default, bounded per run.

Nothing here tries to get past a refusal. A challenge page is recorded as such
and the scraper moves on: no CAPTCHA solving, no stealth, no proxy rotation, no
login, and a browser never waits out a challenge.

Browser safety: every request the page makes (documents, scripts, XHR) passes
the same public-address check as HTTP fetches and is aborted otherwise; images,
media and fonts are not loaded. Still, enable the browser only on a host whose
firewall enforces public-internet-only egress, because the process-wide egress
guard covers ``requests`` and not a browser.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

from cloud.intel.scraper.limits import DomainLimiter, RunBudget, backoff_delay, is_transient, retry_after_seconds
from cloud.intel.scraper.models import CrawlOptions, FetchedPage, Outcome

__all__ = ["BrowserRenderer", "PageFetcher", "PlaywrightRenderer", "RenderResult", "browser_renderer", "classify",
           "looks_like_js_shell", "has_load_more"]

#: Challenge pages that stand in for the site. Seen on any status.
_CHALLENGE = re.compile(r"challenges\.cloudflare\.com|/cdn-cgi/challenge-platform|captcha-delivery\.com|px-captcha"
                        r"|captcha\.awswaf\.com|verify you are (?:a )?human|are you a robot|prove you'?re not a robot",
                        re.I)
#: CAPTCHA widgets. Contact forms embed these too, so they count only on an error status.
_CAPTCHA_WIDGET = re.compile(r"g-recaptcha|recaptcha/api\.js|hcaptcha\.com|h-captcha|cf-turnstile|arkoselabs"
                             r"|funcaptcha", re.I)
_WAF_BODY = re.compile(r"attention required! \| cloudflare|just a moment\.\.\.|checking your browser|ddos protection by"
                       r"|incapsula incident|_incapsula_resource|access denied.{0,80}reference #|errors\.edgesuite\.net"
                       r"|request unsuccessful\. incapsula|sucuri website firewall|blocked by (?:the )?waf"
                       r"|web application firewall|akamai|awswaf", re.I | re.S)
_WAF_HEADERS = ("cf-mitigated", "x-sucuri-block", "x-iinfo", "x-amzn-waf-action")
_LOGIN_URL = re.compile(r"/(?:login|log-in|signin|sign-in|sso|auth(?:orize)?|account/login|users/sign_in)(?:[/?#.]|$)",
                        re.I)
_PASSWORD_INPUT = re.compile(r"<input[^>]+type=[\"']?password", re.I)
_LOAD_MORE = re.compile(r"<(?:button|a)\b[^>]*>(?:(?!</(?:button|a)>).){0,200}?\b(?:load|show|view|see)\s+more\b"
                        r"|\b(?:load|show)\s+more\s+(?:jobs|results|positions|openings|roles)\b", re.I | re.S)


def classify(status: int, body: str, headers: Any, final_url: str, error: Optional[str]) -> Optional[str]:
    """The failure outcome for a response, or ``None`` when it is a usable page."""
    error = error or ""
    if error.startswith("unsafe target"):
        return Outcome.UNSAFE
    if error.startswith("robots"):
        return Outcome.ROBOTS
    if error:
        return Outcome.TIMEOUT if re.search(r"timeout|timed out", error, re.I) else Outcome.FAILED
    head = (body or "")[:60000]
    lowered_headers = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    if status == 429:
        return Outcome.RATE_LIMITED
    if status == 401 or status == 407:
        return Outcome.LOGIN_REQUIRED
    if status in (404, 410):
        return Outcome.NOT_FOUND
    if _CHALLENGE.search(head) and (status >= 400 or len(re.sub(r"<[^>]+>", " ", head).split()) < 400):
        return Outcome.CAPTCHA
    if status >= 400 and _CAPTCHA_WIDGET.search(head):
        return Outcome.CAPTCHA
    waf_header = any(h in lowered_headers for h in _WAF_HEADERS)
    if status in (403, 503, 406) and (waf_header or _WAF_BODY.search(head)):
        return Outcome.WAF
    if status in (403, 451):
        return Outcome.BLOCKED
    if status >= 400:
        return Outcome.FAILED
    if waf_header and lowered_headers.get("cf-mitigated") == "challenge":
        return Outcome.CAPTCHA
    if _LOGIN_URL.search(final_url or "") and _PASSWORD_INPUT.search(head):
        return Outcome.LOGIN_REQUIRED
    return None


def looks_like_js_shell(html: str) -> bool:
    """True when the HTML has almost no visible text but loads an app (React/Vue/Next shells)."""
    if not html:
        return True
    body = re.sub(r"(?is)<(script|style|noscript|template|svg)[^>]*>.*?</\1>", " ", html)
    words = len(re.sub(r"<[^>]+>", " ", body).split())
    return words < 60 and (html.count("<script") >= 2 or "enable javascript" in html.lower())


def has_load_more(html: str) -> bool:
    """A "Load more"/"Show more" control that needs a click (not a plain link to a next page)."""
    return bool(_LOAD_MORE.search(html or ""))


# --- browser -----------------------------------------------------------------------------------


@dataclass
class RenderResult:
    html: str = ""
    final_url: str = ""
    status: int = 0
    duration_ms: float = 0.0
    clicks: int = 0
    scrolls: int = 0
    error: Optional[str] = None


class BrowserRenderer:
    """Renders a page with JavaScript. Off by default (see the module docstring).

    ``render`` may return a :class:`RenderResult` or, for simple renderers, the HTML string.
    """

    def render(self, url: str, *, interact: bool = False, max_clicks: int = 10
               ) -> Union[None, str, RenderResult]:  # pragma: no cover - interface
        return None


_LOAD_MORE_TEXT = ("load more", "show more", "view more", "see more", "more jobs", "more results", "more openings",
                   "more positions", "load more jobs", "show more jobs")


class PlaywrightRenderer(BrowserRenderer):  # pragma: no cover - needs a browser; exercised by the live test
    """Headless Chromium through Playwright (same approach as the crawler's ``utils/browser.py``,
    minus waiting out challenges, which the platform never does)."""

    def __init__(self, timeout_ms: int = 30000, resolver: Optional[Callable] = None) -> None:
        from playwright.sync_api import sync_playwright  # noqa: F401 - availability check

        self.timeout_ms = timeout_ms
        self.resolver = resolver
        self.blocked: list = []   # requests refused by the public-address check

    def _route(self, route: Any) -> None:
        from cloud.intel.core.http import UnsafeTargetError, check_url

        request = route.request
        if request.resource_type in ("image", "media", "font"):
            route.abort()
            return
        try:
            if request.url.startswith(("data:", "blob:")):
                route.continue_()
                return
            check_url(request.url, resolver=self.resolver) if self.resolver else check_url(request.url)
        except UnsafeTargetError:
            self.blocked.append(request.url)
            route.abort()
            return
        except Exception:  # noqa: BLE001 - anything unparseable is refused
            self.blocked.append(request.url)
            route.abort()
            return
        self._fulfill(route)

    def _fulfill(self, route: Any) -> None:
        """Serve a request that passed the safety check (tests serve pages offline here)."""
        route.continue_()

    def render(self, url: str, *, interact: bool = False, max_clicks: int = 10) -> RenderResult:
        from playwright.sync_api import sync_playwright

        from cloud.intel.core.http import USER_AGENT, check_url

        started = time.monotonic()
        check_url(url, resolver=self.resolver) if self.resolver else check_url(url)
        result = RenderResult(final_url=url)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=USER_AGENT, java_script_enabled=True)
                context.route("**/*", self._route)
                page = context.new_page()
                response = page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
                result.status = response.status if response is not None else 0
                try:
                    page.wait_for_load_state("networkidle", timeout=min(12000, self.timeout_ms))
                except Exception:  # noqa: BLE001 - a chatty page never goes idle; the DOM is still usable
                    pass
                html = page.content()
                if classify(result.status, html, {}, page.url, None) is None and interact:
                    for _ in range(max_clicks):
                        clicked = False
                        for text in _LOAD_MORE_TEXT:
                            button = page.get_by_role("button", name=re.compile(text, re.I))
                            if button.count() == 0:
                                button = page.get_by_role("link", name=re.compile(rf"^\s*{text}\s*$", re.I))
                            if button.count() and button.first.is_visible():
                                button.first.click(timeout=5000)
                                clicked = True
                                break
                        if not clicked:
                            break
                        result.clicks += 1
                        try:
                            page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:  # noqa: BLE001
                            pass
                    previous = -1
                    for _ in range(max_clicks):   # infinite scroll: stop once the page stops growing
                        height = page.evaluate("document.body.scrollHeight")
                        if height == previous:
                            break
                        previous = height
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(800)
                        result.scrolls += 1
                    html = page.content()
                result.html, result.final_url = html, page.url
            except Exception as error:  # noqa: BLE001 - a failed render is an outcome, not a crash
                result.error = f"{type(error).__name__}: {str(error)[:200]}"
            finally:
                browser.close()
        result.duration_ms = (time.monotonic() - started) * 1000
        return result


def browser_renderer(enabled: bool) -> Optional[BrowserRenderer]:
    if not enabled:
        return None
    try:
        return PlaywrightRenderer()
    except Exception:  # noqa: BLE001 - playwright not installed
        return None


# --- the fetcher ---------------------------------------------------------------------------------


class PageFetcher:
    """``SafeFetcher`` plus outcome classification, retries, per-domain limits and rendering."""

    def __init__(self, http: Any, renderer: Optional[BrowserRenderer] = None, *,
                 options: Optional[CrawlOptions] = None, limiter: Optional[DomainLimiter] = None,
                 budget: Optional[RunBudget] = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.http = http
        self.renderer = renderer
        self.options = options or CrawlOptions(max_retries=0)
        self.limiter = limiter or DomainLimiter(concurrency=self.options.domain_concurrency,
                                                max_requests=self.options.max_requests_per_domain)
        self.budget = budget
        self._sleep = sleep
        self._browser_slots = threading.Semaphore(max(1, self.options.browser_concurrency))
        self._lock = threading.Lock()
        self.requests = 0
        self.retries = 0
        self.browser_pages = 0

    # --- HTTP ---------------------------------------------------------------------------------

    def _request(self, url: str, **kwargs: Any) -> tuple:
        """``(result, attempts)``, or ``(None, 0)`` when the domain's request budget is spent."""
        attempt = 0
        while True:
            if not self.limiter.acquire(url):
                return None, attempt
            try:
                result = self.http.fetch(url, **kwargs)
            finally:
                self.limiter.release(url)
            attempt += 1
            with self._lock:
                self.requests += 1
            if self.budget is not None:
                self.budget.count_request()
            failure = classify(result.status, result.text, result.headers, result.final_url, result.error)
            transient = failure not in (Outcome.WAF, Outcome.CAPTCHA) and is_transient(result.status, result.error)
            if not transient or attempt > self.options.max_retries:
                return result, attempt
            wait_hint = retry_after_seconds(result.headers)
            if wait_hint is not None and wait_hint > self.options.max_backoff_s:
                return result, attempt   # the site asked for longer than we are willing to wait
            delay = backoff_delay(attempt, cap=self.options.max_backoff_s, retry_after=wait_hint)
            if result.status in (429, 503):
                self.limiter.pause(url, delay)
            with self._lock:
                self.retries += 1
            if self.budget is not None and self.budget.expired():
                return result, attempt
            self._sleep(delay)

    def fetch(self, url: str, *, allow_render: bool = True) -> FetchedPage:
        result, attempts = self._request(url)
        if result is None:
            return FetchedPage(url, url, Outcome.LIMIT, reason="the per-domain request limit was reached",
                               attempts=attempts)
        failure = classify(result.status, result.text, result.headers, result.final_url, result.error)
        if failure is not None:
            reason = result.error or f"HTTP {result.status}"
            if attempts > 1:
                reason += f" after {attempts} attempts"
            return FetchedPage(url, result.final_url or url, failure, result.status, reason=reason[:300],
                               attempts=attempts)
        content_type = (result.content_type or "").lower()
        if content_type and not any(t in content_type for t in ("html", "xml", "text/plain")):
            return FetchedPage(url, result.final_url, Outcome.FAILED, result.status,
                               reason=f"not a web page ({content_type.split(';')[0]})", attempts=attempts)
        page = FetchedPage(url, result.final_url, Outcome.OK, result.status, html=result.text,
                           truncated=bool(result.truncated), attempts=attempts)
        if allow_render and looks_like_js_shell(page.html):
            rendered = self.render(result.final_url, reason="the page is an empty JavaScript shell")
            if rendered is not None:
                if rendered.outcome != Outcome.OK:
                    return rendered
                rendered.attempts, rendered.http_status = attempts, result.status
                return rendered
        return page

    def fetch_json(self, url: str, *, method: str = "GET", json_body: Any = None) -> Optional[Any]:
        """A public JSON API (an ATS job board). ``None`` on any failure."""
        result, _ = self._request(url, method=method, json_body=json_body, accept="application/json")
        if result is None or not result.ok:
            return None
        try:
            return result.json()
        except ValueError:
            return None

    # --- browser ------------------------------------------------------------------------------

    def render(self, url: str, *, reason: str, interact: bool = False) -> Optional[FetchedPage]:
        """Render ``url`` in the browser when enabled and within budget, else ``None``."""
        if self.renderer is None:
            return None
        if self.budget is not None and not self.budget.take_browser_page():
            return None
        if not self.limiter.acquire(url):
            return None
        started = time.monotonic()
        try:
            with self._browser_slots:
                try:
                    raw = self.renderer.render(url, interact=interact)
                except TypeError:   # a simple renderer: render(url)
                    raw = self.renderer.render(url)
                except Exception as error:  # noqa: BLE001 - rendering is best effort
                    raw = RenderResult(final_url=url, error=f"{type(error).__name__}: {str(error)[:200]}")
        finally:
            self.limiter.release(url)
        with self._lock:
            self.browser_pages += 1
        if isinstance(raw, str) or raw is None:
            raw = RenderResult(html=raw or "", final_url=url, status=200 if raw else 0,
                               error=None if raw else "the browser returned nothing")
        duration = raw.duration_ms or (time.monotonic() - started) * 1000
        base = dict(rendered=True, browser_reason=reason[:200], browser_duration_ms=round(duration, 1))
        if raw.error:
            return FetchedPage(url, raw.final_url or url, Outcome.FAILED, raw.status, reason=raw.error,
                               browser_outcome=Outcome.FAILED, **base)
        failure = classify(raw.status or 200, raw.html, {}, raw.final_url or url, None)
        if failure is not None:
            return FetchedPage(url, raw.final_url or url, failure, raw.status,
                               reason=f"{failure} in the browser; not bypassed", browser_outcome=failure, **base)
        return FetchedPage(url, raw.final_url or url, Outcome.OK, raw.status or 200, html=raw.html,
                           browser_outcome=Outcome.OK, **base)
