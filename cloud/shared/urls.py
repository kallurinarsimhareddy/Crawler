"""Which websites the cloud is willing to crawl.

A crawl job makes the worker fetch a URL a stranger typed. Without a check,
``http://169.254.169.254/`` reads the cloud provider's metadata service,
``http://localhost:6379/`` pokes Redis, and ``http://10.0.0.5/admin`` scans the
private network. So there are two gates:

* :func:`check_public_host` — **syntax**, at the API. Refuses IP literals that
  are not public, names that can only be local (``localhost``, ``*.internal``,
  single-label hosts) and ports other than the web's.
* :func:`resolve_public_addresses` — **resolution**, in the worker, immediately
  before crawling. A public-looking name that resolves to a private address
  (DNS rebinding, or simply an internal record) is refused there.

What neither gate can see is a redirect the crawler follows *after* the first
request, because that happens inside the existing crawler, which Phase 5B does
not modify. That residual risk is closed at the network layer on deployment: the
worker host's egress firewall blocks private, loopback, link-local and metadata
ranges. See the Phase 5C checklist in ``cloud/README.md``.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Callable, List, Optional, Sequence, Tuple

__all__ = [
    "ALLOWED_PORTS",
    "UnsafeTargetError",
    "check_public_host",
    "is_public_address",
    "resolve_public_addresses",
]

#: Ports a company website may use. Anything else is almost always an attempt to
#: reach a non-web service.
ALLOWED_PORTS = frozenset({None, 80, 443, 8080, 8443})

_LOCAL_SUFFIXES: Tuple[str, ...] = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".lan",
    ".home",
    ".corp",
    ".home.arpa",
    ".localdomain",
)
_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})


class UnsafeTargetError(ValueError):
    """The website points somewhere the crawler must not go."""


def is_public_address(address: str) -> bool:
    """Whether an IP address is globally routable and not special-purpose."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_global
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_private
        and not ip.is_unspecified
    )


def _as_ip(host: str) -> Optional[str]:
    candidate = host.strip("[]")
    try:
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        pass
    # Integer, octal and hex spellings of IPv4 that resolvers still honour,
    # e.g. "2130706433" or "0x7f.1" for 127.0.0.1.
    try:
        packed = socket.inet_aton(candidate)
    except (OSError, ValueError):
        return None
    return socket.inet_ntoa(packed)


def check_public_host(host: str, port: Optional[int] = None) -> None:
    """Refuse a host or port that can only mean something local.

    Raises:
        UnsafeTargetError: With a message fit to show the user.
    """
    name = (host or "").strip().lower().rstrip(".")
    if not name:
        raise UnsafeTargetError("website must include a domain")
    if port not in ALLOWED_PORTS:
        raise UnsafeTargetError("website port must be 80, 443, 8080 or 8443")

    literal = _as_ip(name)
    if literal is not None:
        if not is_public_address(literal):
            raise UnsafeTargetError("website must be a public address")
        return

    if name in _LOCAL_NAMES or name.endswith(_LOCAL_SUFFIXES):
        raise UnsafeTargetError("website must be a public domain")
    if "." not in name:
        raise UnsafeTargetError("website must include a domain, e.g. example.com")


Resolver = Callable[..., Sequence[Tuple]]


def resolve_public_addresses(
    host: str,
    port: Optional[int] = None,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> List[str]:
    """Resolve ``host`` and insist every address it maps to is public.

    Every address, not just the first: a name with one public and one private
    record is a rebinding setup, and the crawler's HTTP client may pick either.

    Raises:
        UnsafeTargetError: The host is syntactically local, does not resolve,
            or resolves to any non-public address.
    """
    check_public_host(host, port)
    try:
        infos = resolver(host.strip("[]"), port or 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as error:
        raise UnsafeTargetError(f"website domain does not resolve: {host}") from error

    addresses = sorted({str(info[4][0]) for info in infos})
    if not addresses:
        raise UnsafeTargetError(f"website domain does not resolve: {host}")
    blocked = [address for address in addresses if not is_public_address(address)]
    if blocked:
        raise UnsafeTargetError(f"website resolves to a non-public address: {host}")
    return addresses

