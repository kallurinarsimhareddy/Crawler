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
from typing import Any, Callable, Optional, Tuple, Union
from urllib.parse import urlsplit

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


#: Chromium is pointed at a proxy that does not exist and loses its implicit loopback bypass:
#: every request is answered by :meth:`PlaywrightRenderer._fulfill` from Python, so anything
#: that escaped interception cannot reach any network. WebRTC may not send unproxied UDP,
#: and there is no DNS prefetching.
_BROWSER_ARGS = ("--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=<-loopback>",
                 "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                 "--webrtc-ip-handling-policy=disable_non_proxied_udp", "--dns-prefetch-disable")
#: Response headers not passed back to the page (requests already decoded the body).
_DROP_RESPONSE_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length",
                          "proxy-authenticate", "trailer", "upgrade", "set-cookie"}
_DROP_REQUEST_HEADERS = {"host", "connection", "content-length", "keep-alive", "proxy-authorization",
                         "transfer-encoding", "upgrade", "te"}


class PlaywrightRenderer(BrowserRenderer):  # pragma: no cover - needs a browser; exercised by the live test
    """Headless Chromium through Playwright (same approach as the crawler's ``utils/browser.py``,
    minus waiting out challenges, which the platform never does).

    Network containment: Chromium never connects anywhere itself. Every request — navigation,
    frame, popup, subresource, fetch/XHR, redirect hop — is intercepted, refused unless its URL
    passes :func:`check_url` (http(s), allowed port, public host after resolution, alternate IP
    spellings normalised), and then fetched **by Python through** :func:`cloud.shared.netguard.guarded`,
    which resolves again and connects only to an address it checked (DNS rebinding cannot
    redirect the fetch). Redirects are returned to the page unfollowed, so each hop is checked
    again. Service workers are blocked and downloads off. WebSockets are not intercepted (a
    synchronous WebSocket route deadlocks Playwright): they go to the dead proxy and fail, as does
    anything else not intercepted."""

    def __init__(self, timeout_ms: int = 30000, resolver: Optional[Callable] = None, *,
                 allow_hosts: Tuple[str, ...] = (), max_bytes: int = 15_000_000) -> None:
        from playwright.sync_api import sync_playwright  # noqa: F401 - availability check

        self.timeout_ms = timeout_ms
        self.resolver = resolver
        #: Hosts exempt from the public-address rule (tests serve a local site; empty in production).
        self.allow_hosts = tuple(h.lower() for h in allow_hosts)
        self.max_bytes = max_bytes
        self.blocked: list = []   # requests refused by the public-address check or at connect time
        self._http: Any = None
        self.redirected: dict = {}  # request URL -> final URL of a redirect followed in _fulfill

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
            self._check(request.url)
        except UnsafeTargetError:
            self.blocked.append(request.url)
            route.abort("blockedbyclient")
            return
        except Exception:  # noqa: BLE001 - anything unparseable is refused
            self.blocked.append(request.url)
            route.abort("blockedbyclient")
            return
        self._fulfill(route)

    def _check(self, url: str) -> None:
        from cloud.intel.core.http import check_url

        if (urlsplit(url).hostname or "").lower() not in self.allow_hosts:
            check_url(url, resolver=self.resolver) if self.resolver else check_url(url)

    def _fulfill(self, route: Any) -> None:
        """Fetch a request that passed the URL check from Python, pinned to a checked address, and
        hand the response to the page (tests override this to serve pages offline).

        Redirects are followed here, each hop URL-checked and pinned (Playwright does not route
        the request that follows a fulfilled redirect, so the browser must never follow one
        itself); the page gets the final response, and an HTML document gets a ``<base href>``
        so its relative links resolve against the final URL."""
        from urllib.parse import urljoin

        from cloud.intel.core.http import UnsafeTargetError
        from cloud.shared import netguard

        request = route.request
        headers = {k: v for k, v in request.all_headers().items()
                   if not k.startswith(":") and k.lower() not in _DROP_REQUEST_HEADERS}
        url, method, data = request.url, request.method, request.post_data_buffer
        try:
            for _hop in range(6):
                with netguard.guarded(resolver=self.resolver, allow_hosts=self.allow_hosts):
                    response = self._http.request(method, url, headers=headers, data=data,
                                                  timeout=self.timeout_ms / 1000, allow_redirects=False, stream=True)
                location = response.headers.get("Location")
                if response.status_code not in (301, 302, 303, 307, 308) or not location:
                    break
                response.close()
                url = urljoin(url, location)
                self._check(url)
                if response.status_code == 303 or (response.status_code in (301, 302) and method == "POST"):
                    method, data = "GET", None
            else:
                route.abort("failed")         # too many redirects
                return
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > self.max_bytes:
                    response.close()
                    route.abort("failed")
                    return
                chunks.append(chunk)
            response.close()
        except (netguard.EgressBlockedError, UnsafeTargetError):
            self.blocked.append(url)
            route.abort("blockedbyclient")
            return
        except Exception:  # noqa: BLE001 - a network failure is the page's failed request
            route.abort("failed")
            return
        body = b"".join(chunks)
        out = {k: v for k, v in response.headers.items() if k.lower() not in _DROP_RESPONSE_HEADERS}
        cookies = response.raw.headers.getlist("Set-Cookie") if hasattr(response.raw, "headers") else []
        if cookies:
            out["set-cookie"] = chr(10).join(cookies)
        if url != request.url:
            self.redirected[request.url] = url
            if "html" in (response.headers.get("Content-Type") or "").lower() and b"<base" not in body[:4096].lower():
                body = re.sub(rb"(<head[^>]*>)", lambda m: m.group(1) + b'<base href="' + url.encode() + b'">',
                              body, count=1, flags=re.I)
        route.fulfill(status=response.status_code, headers=out, body=body)

    def render(self, url: str, *, interact: bool = False, max_clicks: int = 10) -> RenderResult:
        from playwright.sync_api import sync_playwright

        from cloud.intel.core.http import USER_AGENT, check_url

        import requests

        started = time.monotonic()
        self._check(url)
        result = RenderResult(final_url=url)
        self._http = requests.Session()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=list(_BROWSER_ARGS))
            try:
                context = browser.new_context(user_agent=USER_AGENT, java_script_enabled=True,
                                              service_workers="block", accept_downloads=False)
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
                result.html, result.final_url = html, self.redirected.get(page.url, page.url)
            except Exception as error:  # noqa: BLE001 - a failed render is an outcome, not a crash
                result.error = f"{type(error).__name__}: {str(error)[:200]}"
            finally:
                browser.close()
                self._http.close()
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
