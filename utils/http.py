"""Shared HTTP client for the adapters.

Every adapter talks to the network the same way: a session that retries
transient failures with backoff, a browser-like user agent, bounded timeouts,
and errors that name the URL that failed. That policy lives here so it is
written once and changed once.

    >>> from utils.http import build_session, get_json
    >>> with build_session() as session:
    ...     payload = get_json(session, "https://boards-api.greenhouse.io/v1/boards/acme/jobs")

Failures surface as :class:`AdapterHttpError`, which carries the URL and the
status or transport reason. Adapters translate a bad *input* URL into
:class:`AdapterUrlError` instead, so a mis-typed sheet cell is distinguishable
from a board that is down.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Final, Mapping, Optional, Tuple

import requests
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InvalidHeader
from urllib3.util.retry import Retry

from config.settings import SETTINGS
# crawler.ratelimit imports nothing from this package, so this is acyclic. The
# limiter lives there because that is where it was written and tested; wiring
# it in from here is what Phase 4 does, not moving it.
from crawler.ratelimit import DomainLimiter, RateLimitConfig

__all__ = [
    "DEFAULT_RETRIES",
    "DEFAULT_TIMEOUT",
    "MAX_RETRY_AFTER",
    "USER_AGENT",
    "AdapterError",
    "AdapterHttpError",
    "AdapterUrlError",
    "BoundedRetry",
    "build_session",
    "get_json",
    "get_text",
    "host_limiter",
    "limiter_stats",
    "post_json",
    "request",
    "reset_host_limiter",
]

#: Seconds to wait for connect and for read, respectively.
DEFAULT_TIMEOUT: Final[Tuple[float, float]] = (10.0, 30.0)

#: Attempts per request, including the first.
DEFAULT_RETRIES: Final[int] = 4

#: Statuses worth retrying: rate limiting and transient upstream failures.
RETRY_STATUSES: Final[frozenset] = frozenset({429, 500, 502, 503, 504})

#: Several ATS vendors reject unknown clients outright, so present as a browser.
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

#: Ceiling on a response body an adapter will parse, in bytes. Guards against a
#: mis-detected page streaming a large file into memory.
MAX_BODY_BYTES: Final[int] = 12 * 1024 * 1024

#: Ceiling on a wait a *server* asks for, in seconds.
#:
#: ``Retry-After`` is authoritative about when a board wants to be asked again,
#: and it is honoured -- but it is a number the other side chooses, and some of
#: them choose an hour. urllib3 imposes no useful bound of its own: its
#: ``Retry.DEFAULT_RETRY_AFTER_MAX`` is 21,600 seconds, and the ``backoff_max``
#: that does cap the *computed* delay is never consulted once a header is
#: present, because :meth:`urllib3.util.Retry.sleep` returns as soon as
#: ``sleep_for_retry`` has slept.
#:
#: The consequence at this scale is not theoretical: one worker of six parked
#: for an hour on a single ``Retry-After: 3600`` costs a sixth of a
#: twelve-thousand-company run's throughput, and a shared vendor answering that
#: way parks most of the pool. Two minutes is long enough to outlast the burst
#: limits these boards actually enforce and short enough that being wrong costs
#: a company rather than an evening.
MAX_RETRY_AFTER: Final[float] = 120.0


class BoundedRetry(Retry):
    """A retry policy that honours ``Retry-After`` but not without limit.

    Everything else is :class:`urllib3.util.Retry` unchanged -- the same
    statuses, the same exponential backoff, the same behaviour when no header
    is sent. The single difference is that a server-supplied wait is clamped to
    :attr:`max_retry_after` before it is slept.

    Implemented by overriding :meth:`get_retry_after` rather than by passing
    urllib3's own ``retry_after_max``, because that parameter exists only in
    urllib3 2.x and nothing in ``requirements.txt`` pins a major version. This
    works on both.

    Attributes:
        max_retry_after: The ceiling, in seconds. A class attribute so that a
            policy built with the default needs no arguments, and an instance
            attribute the moment one is set -- which :meth:`new` then carries
            onto every copy urllib3 makes as it counts a request's attempts
            down.
    """

    max_retry_after: float = MAX_RETRY_AFTER

    def get_retry_after(self, response: Any) -> Optional[float]:
        """The wait this response asks for, bounded.

        Args:
            response: The response carrying the header.

        Returns:
            Seconds to wait, never above :attr:`max_retry_after` and never
            below zero, or ``None`` when the response named no usable wait --
            in which case the caller falls through to exponential backoff
            exactly as it always has.
        """
        try:
            seconds = super().get_retry_after(response)
        except InvalidHeader:
            # urllib3 raises rather than returns on a header it cannot parse --
            # a negative number, or prose. Uncaught, that turns a server's
            # malformed reply into an exception escaping ``Retry.sleep``, which
            # is a worse outcome than the header it came from. Treated as "no
            # wait named" instead, so the computed backoff decides.
            logger.debug("Ignoring an unparseable Retry-After header")
            return None

        if seconds is None:
            return None

        try:
            wanted = float(seconds)
        except (TypeError, ValueError):  # pragma: no cover - urllib3 parses it
            return None

        # A negative or non-finite value is a malformed header, not an
        # instruction. Treated as "no wait named", so backoff decides.
        if wanted != wanted or wanted < 0:
            return None

        capped = min(wanted, float(self.max_retry_after))
        if capped < wanted:
            logger.debug(
                "Retry-After asked for {:.0f}s; waiting {:.0f}s instead",
                wanted,
                capped,
            )
        return capped

    def new(self, **kw: Any) -> "BoundedRetry":
        """Copy this policy, keeping the ceiling.

        urllib3 rebuilds the policy after every attempt through this method,
        and it copies only the fields it knows about. Without this the ceiling
        would apply to the first attempt and be lost for the rest -- which is
        precisely the attempt that would then sleep for an hour.

        Args:
            **kw: Fields urllib3 is overriding on the copy.

        Returns:
            The copy.
        """
        other = super().new(**kw)
        other.max_retry_after = self.max_retry_after
        return other


#: The process-wide per-host limiter, and the concurrency it was built for.
#:
#: Module state rather than a parameter because every adapter, the careers-page
#: discovery and the filter reader all call :func:`request` and none of them
#: should have to know the limiter exists. Rebuilt when
#: :data:`~config.settings.SETTINGS` changes, so ``configure()`` at startup --
#: and a test that changes its mind -- both take effect.
_LIMITER: Optional["DomainLimiter"] = None
_LIMITER_LIMIT: Optional[int] = None
_LIMITER_LOCK: Final[threading.Lock] = threading.Lock()


def host_limiter() -> Optional["DomainLimiter"]:
    """The limiter this process is using, if any.

    Returns:
        The limiter, or ``None`` when ``host_concurrency`` is ``0`` and every
        request may proceed unbounded -- which is exactly the code path that
        existed before the limiter was wired in.
    """
    global _LIMITER, _LIMITER_LIMIT

    wanted = max(0, int(getattr(SETTINGS, "host_concurrency", 0) or 0))

    with _LIMITER_LOCK:
        if wanted == 0:
            _LIMITER, _LIMITER_LIMIT = None, 0
            return None

        if _LIMITER is None or _LIMITER_LIMIT != wanted:
            _LIMITER = DomainLimiter(
                RateLimitConfig(
                    # Concurrency only. crawler_engine._HostThrottle already
                    # spaces companies out per host; pacing every request on
                    # top would slow every paginated adapter twice over.
                    min_delay=0.0,
                    max_concurrent=wanted,
                    burst=1,
                    group_shared_vendors=False,
                )
            )
            _LIMITER_LIMIT = wanted

        return _LIMITER


def reset_host_limiter() -> None:
    """Forget the limiter and its counters.

    For tests, and for a benchmark that wants one run's numbers rather than the
    process's.
    """
    global _LIMITER, _LIMITER_LIMIT

    with _LIMITER_LOCK:
        _LIMITER, _LIMITER_LIMIT = None, None


def limiter_stats() -> Dict[str, Any]:
    """What the limiter has done so far.

    Returns:
        Its counters, or ``{}`` when no limiter is configured.
    """
    limiter = host_limiter()
    return dict(limiter.stats()) if limiter is not None else {}


class AdapterError(Exception):
    """Base class for every failure an adapter raises."""


class AdapterUrlError(AdapterError, ValueError):
    """The input URL is not usable for this platform."""


class AdapterHttpError(AdapterError, RuntimeError):
    """A request failed, or the response could not be parsed."""


def build_session(
    retries: int = DEFAULT_RETRIES,
    retry_after_max: float = MAX_RETRY_AFTER,
) -> requests.Session:
    """Create a session that retries transient failures with backoff.

    Retries cover connection errors, read errors and :data:`RETRY_STATUSES`, for
    POST as well as GET, honouring ``Retry-After`` when the server sends it --
    up to ``retry_after_max``, and no further.

    Args:
        retries: Total attempts per request, including the first. Values below
            ``1`` are treated as ``1``.
        retry_after_max: Ceiling on a server-supplied ``Retry-After``, in
            seconds. Defaults to :data:`MAX_RETRY_AFTER`. ``0`` ignores the
            header entirely and always uses the computed backoff.

    Returns:
        A configured session. The caller owns it and should close it.
    """
    attempts = max(1, int(retries))

    policy = BoundedRetry(
        total=attempts - 1,
        connect=attempts - 1,
        read=attempts - 1,
        status=attempts - 1,
        status_forcelist=sorted(RETRY_STATUSES),
        allowed_methods=frozenset({"GET", "POST"}),
        backoff_factor=1.0,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    policy.max_retry_after = max(0.0, float(retry_after_max))

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    adapter = HTTPAdapter(max_retries=policy, pool_maxsize=16)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    json_body: Optional[Any] = None,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
    allow_statuses: Tuple[int, ...] = (),
) -> requests.Response:
    """Perform one request and validate its status.

    Args:
        session: Session to use.
        method: ``"GET"`` or ``"POST"``.
        url: Absolute URL.
        json_body: Body to send as JSON, for POST.
        params: Query parameters.
        headers: Extra headers for this request only.
        timeout: ``(connect, read)`` timeout in seconds.
        allow_statuses: Non-2xx statuses to accept rather than raise on, so a
            caller can probe an endpoint that may legitimately 404.

    Returns:
        The response.

    Raises:
        AdapterHttpError: On a transport failure, or a status that is neither
            successful nor listed in ``allow_statuses``.
    """
    # One slot on this hostname, held for exactly this call and given back
    # however it ends. Deliberately around the request rather than around an
    # adapter: a company must not hold a host to itself while it paginates, and
    # two companies on different hosts must not wait for each other.
    #
    # The slot spans the retries urllib3 performs inside `session.request`,
    # which is the right side to err on -- a host that is answering 429 should
    # keep its slot rather than let the next worker walk into the same wall.
    limiter = host_limiter()

    try:
        if limiter is None:
            response = session.request(
                method,
                url,
                json=json_body,
                params=params,
                headers=dict(headers) if headers else None,
                timeout=timeout,
            )
        else:
            with limiter.hold(url):
                response = session.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    headers=dict(headers) if headers else None,
                    timeout=timeout,
                )
    except requests.RequestException as exc:
        raise AdapterHttpError(f"{method} {url} failed: {exc}") from exc

    if not response.ok and response.status_code not in allow_statuses:
        raise AdapterHttpError(
            f"{method} {url} returned HTTP {response.status_code}: {response.text[:200]!r}"
        )

    return response


def _decode_json(response: requests.Response, url: str) -> Any:
    """Decode a response body as JSON.

    Args:
        response: The response to decode.
        url: URL, for the error message.

    Returns:
        The decoded body.

    Raises:
        AdapterHttpError: If the body is not JSON.
    """
    try:
        return response.json()
    except ValueError as exc:
        raise AdapterHttpError(
            f"{url} returned a non-JSON body: {response.text[:200]!r}"
        ) from exc


def get_json(
    session: requests.Session,
    url: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
) -> Any:
    """GET a URL and decode the body as JSON.

    Args:
        session: Session to use.
        url: Absolute URL.
        params: Query parameters.
        headers: Extra headers.
        timeout: ``(connect, read)`` timeout in seconds.

    Returns:
        The decoded body.

    Raises:
        AdapterHttpError: On a request failure or a non-JSON body.
    """
    response = request(session, "GET", url, params=params, headers=headers, timeout=timeout)
    return _decode_json(response, url)


def post_json(
    session: requests.Session,
    url: str,
    body: Any,
    *,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
) -> Any:
    """POST a JSON body and decode the response as JSON.

    Args:
        session: Session to use.
        url: Absolute URL.
        body: Payload to send.
        params: Query parameters.
        headers: Extra headers.
        timeout: ``(connect, read)`` timeout in seconds.

    Returns:
        The decoded body.

    Raises:
        AdapterHttpError: On a request failure or a non-JSON body.
    """
    merged: Dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
    merged.update(headers or {})

    response = request(
        session, "POST", url, json_body=body, params=params, headers=merged, timeout=timeout
    )
    return _decode_json(response, url)


def get_text(
    session: requests.Session,
    url: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
    allow_statuses: Tuple[int, ...] = (),
) -> str:
    """GET a URL and return its body as text.

    Args:
        session: Session to use.
        url: Absolute URL.
        params: Query parameters.
        headers: Extra headers.
        timeout: ``(connect, read)`` timeout in seconds.
        allow_statuses: Non-2xx statuses whose body should be returned rather
            than raised on, so a caller can inspect an error page — an
            anti-bot interstitial, say — and report what it actually found.

    Returns:
        The decoded body, truncated at :data:`MAX_BODY_BYTES`.

    Raises:
        AdapterHttpError: On a request failure.
    """
    merged: Dict[str, str] = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    merged.update(headers or {})

    response = request(
        session,
        "GET",
        url,
        params=params,
        headers=merged,
        timeout=timeout,
        allow_statuses=allow_statuses,
    )

    if len(response.content) > MAX_BODY_BYTES:
        logger.warning("{} returned {} bytes, truncating", url, len(response.content))
        return response.content[:MAX_BODY_BYTES].decode(response.encoding or "utf-8", "replace")

    return response.text
