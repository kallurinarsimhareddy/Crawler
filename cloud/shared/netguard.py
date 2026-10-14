"""Outbound connections that only reach the public internet — checked at connect time.

A URL check before a request is not enough: the HTTP client resolves the name again when it
connects, so a DNS answer that changes between the check and the connect (rebinding), a
redirect, or a client the caller never sees (an adapter's own ``requests`` session) can still
reach ``127.0.0.1``, ``10.x`` or ``169.254.169.254``. This module sits at the one place every
``requests``/``urllib3`` connection passes through — ``urllib3.util.connection.create_connection``
— and for every new TCP connection:

1. resolves the host itself;
2. refuses if **any** resolved address is not public (:func:`cloud.shared.urls.is_public_address`:
   loopback, RFC 1918, CGNAT, link-local incl. cloud metadata, ULA, multicast, reserved,
   unspecified; IPv4-mapped IPv6 unwrapped);
3. connects to exactly the address it checked, so rebinding cannot slip through.

Two ways to turn it on:

* **process-wide** (:func:`install` — what ``cloud.worker.egress.install_egress_guard`` and the
  platform worker use): every urllib3 connection in the process;
* **scoped** (:func:`guarded` — a context manager): only connections made inside the block, on
  this thread. :class:`cloud.intel.core.http.SafeFetcher`, webhook delivery and the browser
  renderer use it, so they are pinned even in a process without the process-wide guard.

Hosts the process must reach for its own infrastructure (an S3 endpoint, a local model) are
passed as ``allow_hosts`` and use normal resolution. Database clients, SMTP and DNS do not use
urllib3 and are not affected (their own call sites check addresses).
"""

from __future__ import annotations

import contextvars
import logging
import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, FrozenSet, Iterable, Iterator, Optional

from cloud.shared.urls import is_public_address

__all__ = ["EgressBlockedError", "Policy", "guarded", "install", "uninstall", "installed", "process_policy",
           "blocked_total"]

log = logging.getLogger(__name__)


class EgressBlockedError(ConnectionError):
    """A connection to a non-public address was refused."""


@dataclass(frozen=True)
class Policy:
    allow_hosts: FrozenSet[str] = frozenset()
    resolver: Callable = field(default=socket.getaddrinfo)


_lock = threading.Lock()
_original: Optional[Callable] = None
_process: Optional[Policy] = None
_scoped: contextvars.ContextVar[Optional[Policy]] = contextvars.ContextVar("netguard_policy", default=None)
_blocked = 0


def blocked_total() -> int:
    return _blocked


def _normal(host: str) -> str:
    return host.strip("[]").lower().rstrip(".")


def _connect(policy: Policy, address, timeout, source_address, socket_options):
    global _blocked
    host, port = address
    if _normal(host) in policy.allow_hosts:
        return _original(address, timeout, source_address, socket_options)  # type: ignore[misc]
    infos = policy.resolver(host.strip("[]"), port, type=socket.SOCK_STREAM)
    blocked = sorted({str(info[4][0]) for info in infos if not is_public_address(str(info[4][0]))})
    if not infos or blocked:
        with _lock:
            _blocked += 1
        log.warning("egress blocked: %s resolves to non-public address(es) %s", host, ", ".join(blocked) or "none")
        raise EgressBlockedError(f"refusing to connect to {host}: not a public internet address")
    error: Optional[OSError] = None
    for family, socktype, proto, _canonname, sockaddr in infos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            for option in socket_options or ():
                sock.setsockopt(*option)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:  # noqa: SLF001
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)  # the address that was checked, not a fresh lookup
            return sock
        except OSError as exc:
            error = exc
            if sock is not None:
                sock.close()
    raise error or OSError(f"could not connect to {host}")


def _create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None,  # noqa: SLF001
                       socket_options=None):
    policy = _scoped.get() or _process
    if policy is None:
        return _original(address, timeout, source_address, socket_options)  # type: ignore[misc]
    return _connect(policy, address, timeout, source_address, socket_options)


def _hook() -> None:
    """Route urllib3's connection factory through this module (once; a no-op when no policy)."""
    global _original
    import urllib3.util.connection as connection

    with _lock:
        if _original is None:
            _original = connection.create_connection
            connection.create_connection = _create_connection


def _policy(allow_hosts: Iterable[str], resolver: Callable) -> Policy:
    return Policy(frozenset(_normal(h) for h in allow_hosts if h), resolver)


def install(*, allow_hosts: Iterable[str] = (), resolver: Callable = socket.getaddrinfo) -> Policy:
    """Guard every urllib3 connection of this process. Idempotent; a later call replaces the policy."""
    global _process
    _hook()
    with _lock:
        _process = _policy(allow_hosts, resolver)
    log.info("egress guard installed (allowlisted infrastructure hosts: %s)",
             ", ".join(sorted(_process.allow_hosts)) or "none")
    return _process


def uninstall() -> None:
    """Drop the process-wide policy and restore urllib3's own factory (for tests)."""
    global _process, _original
    import urllib3.util.connection as connection

    with _lock:
        _process = None
        if _original is not None:
            connection.create_connection = _original
            _original = None


def installed() -> bool:
    return _process is not None


def process_policy() -> Optional[Policy]:
    return _process


@contextmanager
def guarded(*, allow_hosts: Iterable[str] = (), resolver: Optional[Callable] = None) -> Iterator[Policy]:
    """Guard the urllib3 connections opened inside this block (this thread / task only)."""
    _hook()
    policy = _policy(allow_hosts, resolver or socket.getaddrinfo)
    token = _scoped.set(policy)
    try:
        yield policy
    finally:
        _scoped.reset(token)


_LOCAL_MODEL_HOSTS = ("localhost", "127.0.0.1", "::1")
_DEPLOYED = ("staging", "production")


def allow_hosts_from_env(env) -> list:
    """The process's own infrastructure that may be non-public: the S3 endpoint, the Supabase
    Auth URL (a self-hosted auth server can be local), a localhost OpenAI-compatible model and
    ``CAREERCLOUD_EGRESS_ALLOW_HOSTS`` (comma separated). Nothing else is exempt."""
    from urllib.parse import urlsplit

    hosts = []
    for name in ("CAREERCLOUD_S3_ENDPOINT", "CAREERCLOUD_SUPABASE_URL", "OPENAI_COMPATIBLE_BASE_URL"):
        host = urlsplit(env.get(name) or "").hostname
        if host and (name != "OPENAI_COMPATIBLE_BASE_URL" or host in _LOCAL_MODEL_HOSTS):
            hosts.append(host)
    hosts += [h.strip() for h in (env.get("CAREERCLOUD_EGRESS_ALLOW_HOSTS") or "").split(",") if h.strip()]
    return sorted({_normal(h) for h in hosts})


def install_from_env(env, *, role: str, default_on: bool, resolver: Optional[Callable] = None) -> bool:
    """Install the process-wide guard as configured. ``CAREERCLOUD_EGRESS_GUARD`` overrides the
    role's default; switching it off is refused in staging and production."""
    from cloud.shared.config import env_bool

    environment = (env.get("CAREERCLOUD_ENV") or "development").strip().lower()
    if not env_bool(env, "CAREERCLOUD_EGRESS_GUARD", default_on):
        if environment in _DEPLOYED:
            raise SystemExit(f"{environment} must not disable CAREERCLOUD_EGRESS_GUARD")
        if default_on:
            log.warning("egress guard OFF for the %s (CAREERCLOUD_EGRESS_GUARD=false): it can reach private "
                        "addresses", role)
        return False
    install(allow_hosts=allow_hosts_from_env(env), **({"resolver": resolver} if resolver else {}))
    return True


def api_guard_default(env) -> bool:
    """The API process guards by default where it is deployed (staging, production, self-hosted).
    Test and development processes import the API module; they keep plain networking unless
    ``CAREERCLOUD_EGRESS_GUARD`` asks for the guard."""
    from cloud.shared.config import env_bool

    environment = (env.get("CAREERCLOUD_ENV") or "development").strip().lower()
    return environment in _DEPLOYED or env_bool(env, "CAREERCLOUD_SELF_HOSTED", False)
