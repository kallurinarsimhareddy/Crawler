"""Offline doubles for platform tests: an HTTP session, a DNS resolver, a Claude client.

Nothing here touches the network. The fake session serves canned pages; the
fake resolver maps hostnames to addresses so the real SSRF checks in
:class:`cloud.intel.core.http.SafeFetcher` run unchanged.
"""

from __future__ import annotations

import json
import socket
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

PUBLIC_IP = "93.184.216.34"


def fake_resolver(private_hosts=("internal.example",)):
    def resolve(host, port, type=None, **_):
        address = "10.0.0.7" if host in private_hosts else PUBLIC_IP
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    return resolve


class FakeResponse:
    def __init__(self, status: int, body: str = "", headers: Optional[Dict[str, str]] = None) -> None:
        self.status_code = status
        self._body = body.encode("utf-8")
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
        self.encoding = "utf-8"

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308) and "Location" in self.headers

    def iter_content(self, size: int):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    def close(self) -> None:
        pass


class FakeSession:
    """``pages``: url -> (status, body[, headers]). Unknown URLs are 404."""

    def __init__(self, pages: Dict[str, Tuple]) -> None:
        self.pages = pages
        self.headers: Dict[str, str] = {}
        self.calls: List[str] = []

    def request(self, method, url, **kwargs):
        self.calls.append(url)
        entry = self.pages.get(url)
        if entry is None:
            return FakeResponse(404, "not found")
        status, body, *rest = entry
        if not isinstance(body, str):
            body = json.dumps(body)
        return FakeResponse(status, body, rest[0] if rest else None)


def fetcher_for(pages: Dict[str, Tuple], **kwargs):
    from cloud.intel.core.http import SafeFetcher

    return SafeFetcher(session=FakeSession(pages), resolver=fake_resolver(), per_host_delay=0, **kwargs)


class FakeClaudeMessages:
    def __init__(self, replies: List) -> None:
        self.replies = list(replies)
        self.requests: List[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def claude_reply(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, stop_details=None,
                           content=[SimpleNamespace(type="text", text=text)])


def fake_claude_client(*replies):
    return SimpleNamespace(messages=FakeClaudeMessages(list(replies)))
