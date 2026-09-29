"""Fetching one page and saying honestly what happened.

All traffic goes through :class:`cloud.intel.core.http.SafeFetcher` (SSRF checks on
every hop, robots.txt, size/time limits, per-host pacing, an honest User-Agent).
This module adds only classification: a response is turned into an
:class:`~cloud.intel.scraper.models.Outcome` — ``CAPTCHA``, ``WAF``,
``LOGIN_REQUIRED``, ``ROBOTS``, ``TIMEOUT``… — so the run can report it.

Nothing here tries to get past a refusal. A challenge page is recorded as such
and the scraper moves on: no CAPTCHA solving, no stealth headers, no proxy
rotation, no login, no retries through another route.

Browser rendering (Playwright) is used only when explicitly enabled for the
platform *and* the fetched HTML is an empty JavaScript shell; never to get past
a block. The egress guard does not cover a browser, so enable it only on a host
whose firewall enforces public-internet-only egress.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from cloud.intel.scraper.models import FetchedPage, Outcome

__all__ = ["BrowserRenderer", "PageFetcher", "browser_renderer", "classify", "looks_like_js_shell"]

#: Challenge pages that stand in for the site. Seen on any status.
_CHALLENGE = re.compile(r"challenges\.cloudflare\.com|/cdn-cgi/challenge-platform|captcha-delivery\.com|px-captcha"
                        r"|verify you are (?:a )?human|are you a robot|prove you'?re not a robot", re.I)
#: CAPTCHA widgets. Contact forms embed these too, so they count only on an error status.
_CAPTCHA_WIDGET = re.compile(r"g-recaptcha|recaptcha/api\.js|hcaptcha\.com|h-captcha|cf-turnstile|arkoselabs"
                             r"|funcaptcha", re.I)
_WAF_BODY = re.compile(r"attention required! \| cloudflare|just a moment\.\.\.|checking your browser|ddos protection by"
                       r"|incapsula incident|_incapsula_resource|access denied.{0,80}reference #|errors\.edgesuite\.net"
                       r"|request unsuccessful\. incapsula|sucuri website firewall|blocked by (?:the )?waf"
                       r"|web application firewall|akamai", re.I | re.S)
_WAF_HEADERS = ("cf-mitigated", "x-sucuri-block", "x-iinfo", "x-amzn-waf-action")
_LOGIN_URL = re.compile(r"/(?:login|log-in|signin|sign-in|sso|auth(?:orize)?|account/login|users/sign_in)(?:[/?#.]|$)",
                        re.I)
_PASSWORD_INPUT = re.compile(r"<input[^>]+type=[\"']?password", re.I)


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


class BrowserRenderer:
    """Renders a page with JavaScript. Off by default (see the module docstring)."""

    def render(self, url: str) -> Optional[str]:  # pragma: no cover - interface
        return None


class PlaywrightRenderer(BrowserRenderer):  # pragma: no cover - needs a browser install
    def __init__(self, timeout_ms: int = 30000) -> None:
        from playwright.sync_api import sync_playwright  # noqa: F401 - availability check

        self.timeout_ms = timeout_ms

    def render(self, url: str) -> Optional[str]:
        from playwright.sync_api import sync_playwright

        from cloud.intel.core.http import USER_AGENT

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=USER_AGENT)
                page.goto(url, timeout=self.timeout_ms, wait_until="networkidle")
                return page.content()
            finally:
                browser.close()


def browser_renderer(enabled: bool) -> Optional[BrowserRenderer]:
    if not enabled:
        return None
    try:
        return PlaywrightRenderer()
    except Exception:  # noqa: BLE001 - playwright not installed
        return None


class PageFetcher:
    """``SafeFetcher`` plus outcome classification and optional rendering."""

    def __init__(self, http: Any, renderer: Optional[BrowserRenderer] = None) -> None:
        self.http = http
        self.renderer = renderer

    def fetch(self, url: str) -> FetchedPage:
        result = self.http.fetch(url)
        failure = classify(result.status, result.text, result.headers, result.final_url, result.error)
        if failure is not None:
            reason = result.error or f"HTTP {result.status}"
            return FetchedPage(url, result.final_url or url, failure, result.status, reason=reason[:300])
        content_type = (result.content_type or "").lower()
        if content_type and not any(t in content_type for t in ("html", "xml", "text/plain")):
            return FetchedPage(url, result.final_url, Outcome.FAILED, result.status,
                               reason=f"not a web page ({content_type.split(';')[0]})")
        page = FetchedPage(url, result.final_url, Outcome.OK, result.status, html=result.text,
                           truncated=bool(result.truncated))
        if self.renderer is not None and looks_like_js_shell(page.html):
            try:
                rendered = self.renderer.render(result.final_url)
            except Exception as error:  # noqa: BLE001 - rendering is best effort
                rendered = None
                page.reason = f"browser rendering failed: {type(error).__name__}"
            if rendered:
                failure = classify(200, rendered, {}, result.final_url, None)
                if failure is not None:
                    return FetchedPage(url, result.final_url, failure, result.status, reason="challenge after rendering")
                page.html, page.rendered = rendered, True
        return page

    def fetch_json(self, url: str, *, method: str = "GET", json_body: Any = None) -> Optional[Any]:
        """A public JSON API (an ATS job board). ``None`` on any failure."""
        result = self.http.fetch(url, method=method, json_body=json_body, accept="application/json")
        if not result.ok:
            return None
        try:
            return result.json()
        except ValueError:
            return None
