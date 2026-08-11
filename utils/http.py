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

from typing import Any, Dict, Final, Mapping, Optional, Tuple

import requests
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

__all__ = [
    "DEFAULT_RETRIES",
    "DEFAULT_TIMEOUT",
    "USER_AGENT",
    "AdapterError",
    "AdapterHttpError",
    "AdapterUrlError",
    "build_session",
    "get_json",
    "get_text",
    "post_json",
    "request",
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


class AdapterError(Exception):
    """Base class for every failure an adapter raises."""


class AdapterUrlError(AdapterError, ValueError):
    """The input URL is not usable for this platform."""


class AdapterHttpError(AdapterError, RuntimeError):
    """A request failed, or the response could not be parsed."""


def build_session(retries: int = DEFAULT_RETRIES) -> requests.Session:
    """Create a session that retries transient failures with backoff.

    Retries cover connection errors, read errors and :data:`RETRY_STATUSES`, for
    POST as well as GET, honouring ``Retry-After`` when the server sends it.

    Args:
        retries: Total attempts per request, including the first. Values below
            ``1`` are treated as ``1``.

    Returns:
        A configured session. The caller owns it and should close it.
    """
    attempts = max(1, int(retries))

    policy = Retry(
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
    try:
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
