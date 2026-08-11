"""Fake HTTP session and response, shared by the adapter tests.

Every adapter takes a ``session`` keyword precisely so a test can hand it one
that replays scripted responses and never touches the network. That fake is
defined here once rather than in each test module.

The odd import guard other modules use to reach this one::

    try:
        from tests._fakes import FakeSession
    except ImportError:
        from _fakes import FakeSession

is not superstition. ``python -m unittest discover -s tests`` makes ``tests``
the top-level directory, so its modules are imported as ``test_adapters`` and
the package name does not exist; ``discover -s tests -t .`` imports them as
``tests.test_adapters`` and the bare name does not. Both invocations are in
use, so both spellings have to work.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

__all__ = ["FakeResponse", "FakeSession", "html"]


class FakeResponse:
    """Stand-in for :class:`requests.Response`.

    Args:
        body: Value returned by :meth:`json`. Also serialised into
            :attr:`text` when no explicit ``text`` is given.
        status_code: HTTP status to report.
        text: Body as text, for the HTML adapters.
    """

    def __init__(
        self,
        body: Any = None,
        status_code: int = 200,
        text: Optional[str] = None,
    ) -> None:
        self._body = body
        self.status_code = status_code
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")
        self.encoding = "utf-8"
        self.headers: Dict[str, str] = {"content-type": "application/json"}

    @property
    def ok(self) -> bool:
        """Whether the status is a success."""
        return 200 <= self.status_code < 300

    @property
    def content(self) -> bytes:
        """The body as bytes."""
        return self.text.encode("utf-8")

    def json(self) -> Any:
        """Return the decoded body.

        Returns:
            Whatever ``body`` was constructed with.

        Raises:
            ValueError: If the response carries no JSON, matching what
                ``requests`` does.
        """
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


class FakeSession:
    """Replays scripted responses and records every request.

    An unscripted request is an assertion failure rather than a silent empty
    response, so a test that makes more calls than it meant to is caught.

    Args:
        responses: Responses to return in order. An exception in the list is
            raised instead of returned, which is how transport failures are
            simulated.
    """

    def __init__(self, responses: Sequence[Any] = ()) -> None:
        self._responses = list(responses)
        self.requests: List[Dict[str, Any]] = []
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        """Record the request and return the next scripted response.

        Args:
            method: HTTP method.
            url: Absolute URL.
            **kwargs: Everything ``utils.http.request`` passes through.

        Returns:
            The next scripted response.

        Raises:
            AssertionError: If nothing is left to return.
        """
        self.requests.append({"method": method, "url": url, **kwargs})

        if not self._responses:
            raise AssertionError(f"unexpected request: {method} {url}")

        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        """Record that the session was closed."""
        self.closed = True


def html(body: str) -> FakeResponse:
    """Build an HTML response around a body fragment.

    Args:
        body: Markup to place inside ``<body>``.

    Returns:
        The response.
    """
    return FakeResponse(text=f"<html><body>{body}</body></html>")
