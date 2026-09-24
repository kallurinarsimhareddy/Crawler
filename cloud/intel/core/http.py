"""The only way platform code fetches an arbitrary URL.

Used by the scraper, discovery (website verification, careers pages), the
public-web contact source and ATS job-board APIs. It enforces:

* **SSRF protection.** http(s) only, ports 80/443/8080/8443, no credentials in
  the URL, and every hop — the first request and each redirect — is resolved and
  refused if *any* address is non-public (loopback, private, link-local/metadata,
  CGNAT, multicast, reserved), using :mod:`cloud.shared.urls`. Redirects are
  followed manually so each one is checked, capped at ``max_redirects``.
* **Bounded responses.** A size cap and a timeout; bodies over the cap are cut
  and flagged, never buffered unbounded.
* **Politeness.** An honest User-Agent, per-host minimum spacing, and
  ``robots.txt`` respected by default. No CAPTCHA solving, no stealth, no
  proxy rotation, no login: a page that blocks us is reported as ``blocked``.

The DNS check and the connection are not atomic here (requests resolves again);
the worker process additionally runs CareerCloud's egress guard
(:mod:`cloud.worker.egress`), which pins the checked address at connect time.
"""

from __future__ import annotations

import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

from cloud.shared.urls import UnsafeTargetError, check_public_host, resolve_public_addresses

__all__ = ["FetchResult", "SafeFetcher", "UnsafeTargetError", "USER_AGENT", "check_url"]

USER_AGENT = "CareerCrawlerBot/1.0 (+company research; respects robots.txt)"
_ALLOWED_PORTS = {None, 80, 443, 8080, 8443}
_BLOCK_STATUSES = {401, 403, 407, 429, 451, 503}


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    text: str = ""
    content_type: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    truncated: bool = False
    error: Optional[str] = None
    redirects: List[str] = field(default_factory=list)
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def blocked(self) -> bool:
        return self.status in _BLOCK_STATUSES or (self.error or "").startswith("robots")

    def json(self):
        import json

        return json.loads(self.text)


def check_url(url: str, *, resolve: bool = True, resolver: Optional[Callable] = None) -> str:
    """Return the URL if it is a fetchable public http(s) URL, else raise UnsafeTargetError."""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme not in ("http", "https"):
        raise UnsafeTargetError("only http and https URLs can be fetched")
    if parts.username or parts.password:
        raise UnsafeTargetError("URLs with credentials are refused")
    if not parts.hostname:
        raise UnsafeTargetError("the URL has no host")
    try:
        port = parts.port
    except ValueError:
        raise UnsafeTargetError("the URL has an invalid port") from None
    if port not in _ALLOWED_PORTS:
        raise UnsafeTargetError("only ports 80, 443, 8080 and 8443 are allowed")
    check_public_host(parts.hostname, port)
    if resolve:
        if resolver is not None:
            resolve_public_addresses(parts.hostname, port or (443 if parts.scheme == "https" else 80),
                                     resolver=resolver)
        else:
            resolve_public_addresses(parts.hostname, port or (443 if parts.scheme == "https" else 80))
    return parts.geturl()


class SafeFetcher:
    def __init__(self, *, session=None, timeout: float = 20.0, max_bytes: int = 3_000_000,
                 max_redirects: int = 5, per_host_delay: float = 1.0, respect_robots: bool = True,
                 resolver: Optional[Callable] = None, user_agent: str = USER_AGENT) -> None:
        import requests

        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.8"})
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.per_host_delay = per_host_delay
        self.respect_robots = respect_robots
        self.user_agent = user_agent
        self._resolver = resolver
        self._last: Dict[str, float] = {}
        self._robots: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._lock = threading.Lock()

    # --- politeness ----------------------------------------------------------

    def _pace(self, host: str) -> None:
        with self._lock:
            wait = self._last.get(host, 0.0) + self.per_host_delay - time.monotonic()
            self._last[host] = max(time.monotonic(), self._last.get(host, 0.0) + self.per_host_delay)
        if wait > 0:
            time.sleep(wait)

    def allowed_by_robots(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser: Optional[urllib.robotparser.RobotFileParser] = None
            result = self._get(origin + "/robots.txt", check_robots=False, max_bytes=300_000)
            if result.status == 200 and result.text:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(result.text.splitlines())
            self._robots[origin] = parser  # missing/unreadable robots.txt = no restrictions
        parser = self._robots[origin]
        return True if parser is None else parser.can_fetch(self.user_agent, url)

    # --- fetching -------------------------------------------------------------

    def fetch(self, url: str, *, method: str = "GET", json_body=None, headers: Optional[Dict[str, str]] = None,
              accept: str = "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8") -> FetchResult:
        return self._get(url, method=method, json_body=json_body, headers=headers, accept=accept)

    def _get(self, url: str, *, method: str = "GET", json_body=None, headers=None, accept: str = "*/*",
             check_robots: bool = True, max_bytes: Optional[int] = None) -> FetchResult:
        started = time.monotonic()
        redirects: List[str] = []
        current = url
        try:
            for _ in range(self.max_redirects + 1):
                check_url(current, resolver=self._resolver)
                if check_robots and not self.allowed_by_robots(current):
                    return FetchResult(url, current, 0, error="robots.txt disallows this URL", redirects=redirects)
                self._pace(urlsplit(current).hostname or "")
                response = self._session.request(
                    method, current, json=json_body, timeout=self.timeout, allow_redirects=False, stream=True,
                    headers={"Accept": accept, **(headers or {})})
                if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    response.close()
                    if not location:
                        return FetchResult(url, current, response.status_code, error="redirect without Location")
                    current = urljoin(current, location)
                    redirects.append(current)
                    if response.status_code == 303:
                        method, json_body = "GET", None
                    continue
                limit = max_bytes or self.max_bytes
                chunks, size, truncated = [], 0, False
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > limit:
                        chunks.append(chunk[: limit - (size - len(chunk))])
                        truncated = True
                        break
                    chunks.append(chunk)
                response.close()
                raw = b"".join(chunks)
                encoding = response.encoding or "utf-8"
                text = raw.decode(encoding if encoding.lower() != "iso-8859-1" else "utf-8", errors="replace")
                return FetchResult(url, current, response.status_code, text=text,
                                   content_type=response.headers.get("Content-Type", ""),
                                   headers={k: v for k, v in response.headers.items()}, truncated=truncated,
                                   redirects=redirects, elapsed_ms=(time.monotonic() - started) * 1000)
            return FetchResult(url, current, 0, error="too many redirects", redirects=redirects)
        except UnsafeTargetError as error:
            return FetchResult(url, current, 0, error=f"unsafe target: {error}", redirects=redirects)
        except Exception as error:  # noqa: BLE001 - network failures are results, not crashes
            return FetchResult(url, current, 0, error=f"{type(error).__name__}: {error}", redirects=redirects,
                               elapsed_ms=(time.monotonic() - started) * 1000)
