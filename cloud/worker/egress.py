"""Stop crawler HTTP from reaching anything that is not the public internet.

The existing crawler follows redirects and builds its own ``requests`` sessions
(the Workday adapter creates one internally). Checking only the job's first URL
would not stop ``https://public.example`` from redirecting to
``http://169.254.169.254/``. The guard therefore sits at the one place every
``requests``/``urllib3`` connection passes through, inside the **worker process
only**: ``urllib3.util.connection.create_connection``. No crawler file changes.

For every new TCP connection it:

1. resolves the host itself;
2. refuses if **any** resolved address is loopback, private (RFC 1918, ULA),
   link-local (including cloud metadata 169.254.169.254), CGNAT, multicast,
   reserved or unspecified, with IPv4-mapped IPv6 unwrapped first;
3. connects to exactly the address it checked, so a DNS answer that changes
   between check and connect (rebinding) cannot slip through.

That covers the first request, every redirect hop, every adapter-created
session and every retry. It does **not** cover non-urllib3 traffic, such as a
Playwright browser or raw sockets, which is why staging also runs the nftables
egress policy in ``cloud/deploy/staging/nftables`` and keeps the browser
fallback off unless that firewall is confirmed.

Hosts the worker must reach for its own infrastructure, like the object-storage
endpoint, are listed in ``allow_hosts`` and bypass the check. Database and Redis
clients do not use urllib3 and are unaffected.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Callable, FrozenSet, Iterable, Optional

from cloud.shared.urls import is_public_address

__all__ = ["EgressBlockedError", "egress_guard_installed", "install_egress_guard", "uninstall_egress_guard"]

log = logging.getLogger(__name__)

_lock = threading.Lock()
_original: Optional[Callable] = None
_allow: FrozenSet[str] = frozenset()
_resolver: Callable = socket.getaddrinfo
blocked_count = 0


class EgressBlockedError(ConnectionError):
    """A crawler connection to a non-public address was refused."""


def _guarded_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, socket_options=None):  # noqa: SLF001
    global blocked_count
    host, port = address
    if host.startswith("["):
        host = host.strip("[]")
    if host.lower().rstrip(".") in _allow:
        return _original(address, timeout, source_address, socket_options)  # type: ignore[misc]

    try:
        infos = _resolver(host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror:
        raise
    blocked = sorted({info[4][0] for info in infos if not is_public_address(str(info[4][0]))})
    if not infos or blocked:
        with _lock:
            blocked_count += 1
        log.warning("egress blocked: %s resolves to non-public address(es) %s", host, ", ".join(blocked) or "none")
        raise EgressBlockedError(f"refusing to connect to {host}: not a public internet address")

    error: Optional[OSError] = None
    for family, socktype, proto, _canonname, sockaddr in infos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if socket_options:
                for option in socket_options:
                    sock.setsockopt(*option)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:  # noqa: SLF001
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)  # the address we checked, not a fresh lookup
            return sock
        except OSError as exc:
            error = exc
            if sock is not None:
                sock.close()
    raise error or OSError(f"could not connect to {host}")


def install_egress_guard(*, allow_hosts: Iterable[str] = (), resolver: Callable = socket.getaddrinfo) -> None:
    """Install the guard for this process. Idempotent; later calls update the allowlist."""
    global _original, _allow, _resolver
    import urllib3.util.connection as connection

    with _lock:
        _allow = frozenset(h.lower().rstrip(".") for h in allow_hosts if h)
        _resolver = resolver
        if _original is None:
            _original = connection.create_connection
            connection.create_connection = _guarded_create_connection
    log.info("egress guard installed (allowlisted infrastructure hosts: %s)", ", ".join(sorted(_allow)) or "none")


def uninstall_egress_guard() -> None:
    """Restore urllib3. For tests."""
    global _original
    import urllib3.util.connection as connection

    with _lock:
        if _original is not None:
            connection.create_connection = _original
            _original = None


def egress_guard_installed() -> bool:
    import urllib3.util.connection as connection

    return connection.create_connection is _guarded_create_connection
