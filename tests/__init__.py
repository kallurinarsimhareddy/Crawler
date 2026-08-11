"""Unit tests for the career page crawler.

Run from the project root::

    python -m unittest discover -s tests

The suite is offline and sequential by contract:

* every adapter is handed a fake session;
* the browser fallback is off, so a test that finds nothing in its fixture
  cannot quietly launch Chromium and reach the real internet;
* career discovery and diagnostics are off, since both make their own requests;
* the worker pool is pinned to one, so results are deterministic and a test can
  still assert that a single injected session was shared. Concurrency has its
  own tests, which pass ``max_workers`` explicitly.

Those are the shipped defaults in :mod:`config.settings` — only :func:`main.main`
turns any of them on — so the guarantee holds however the suite is invoked.
``unittest discover -s tests`` makes ``tests`` the top-level directory and never
imports this file at all, which is exactly why the safety has to live in the
defaults rather than here. The call below is belt and braces for the case where
a previous import already reconfigured the process.

:mod:`tests.live_coverage` is the deliberate exception, and is named so that
``unittest discover`` skips it.
"""

from config.settings import configure

configure(
    max_workers=1,
    browser_fallback=False,
    discover_careers=False,
    diagnostics=False,
    per_host_delay=0.0,
)
