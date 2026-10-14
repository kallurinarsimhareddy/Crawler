"""Stop crawler HTTP from reaching anything that is not the public internet.

The existing crawler follows redirects and builds its own ``requests`` sessions
(the Workday adapter creates one internally). Checking only the job's first URL
would not stop ``https://public.example`` from redirecting to
``http://169.254.169.254/``. The guard therefore sits at the one place every
``requests``/``urllib3`` connection passes through:
``urllib3.util.connection.create_connection``. No crawler file changes.

For every new TCP connection it resolves the host itself, refuses if **any**
resolved address is not public, and connects to exactly the address it checked
(no DNS rebinding). That covers the first request, every redirect hop, every
adapter-created session and every retry. It does **not** cover non-urllib3
traffic (a Playwright browser, raw sockets): see ``cloud/shared/netguard.py`` and
the browser renderer for those.

The implementation lives in :mod:`cloud.shared.netguard` (also used, scoped, by the
platform's SafeFetcher, webhooks and browser renderer); this module keeps the
worker-facing names. Hosts the worker must reach for its own infrastructure are
listed in ``allow_hosts`` and use normal resolution.
"""

from __future__ import annotations

import socket
from typing import Any, Callable, Iterable

from cloud.shared import netguard
from cloud.shared.netguard import EgressBlockedError

__all__ = ["EgressBlockedError", "egress_guard_installed", "install_egress_guard", "uninstall_egress_guard"]


def install_egress_guard(*, allow_hosts: Iterable[str] = (), resolver: Callable = socket.getaddrinfo) -> None:
    """Install the guard for this process. Idempotent; later calls update the allowlist."""
    netguard.install(allow_hosts=allow_hosts, resolver=resolver)


def uninstall_egress_guard() -> None:
    """Restore urllib3. For tests."""
    netguard.uninstall()


def egress_guard_installed() -> bool:
    return netguard.installed()


def __getattr__(name: str) -> Any:
    if name == "blocked_count":  # how many connections were refused so far (tests, diagnostics)
        return netguard.blocked_total()
    raise AttributeError(name)
