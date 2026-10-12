"""Skip markers for tests that need something the self-hosted app image deliberately leaves out.

The image (deploy/selfhost/Dockerfile) has no Redis client, no fakeredis, and none of the
repository-only files (Windows launchers, staging templates, git metadata, deploy/ tooling).
`sanagtm selftest` runs this suite inside the image, so those tests skip there with a reason,
exactly as the Playwright/moto/pgserver tests skip where those are missing.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def needs_module(*names: str):
    missing = [name for name in names if importlib.util.find_spec(name) is None]
    return unittest.skipIf(bool(missing), f"{', '.join(missing)} not installed (the Redis queue's test "
                                          "dependencies; the self-hosted image uses the PostgreSQL queue)")


def needs_repo_files(*relative: str):
    missing = [path for path in relative if not (REPO / path).exists()]
    return unittest.skipIf(bool(missing), f"repository file(s) not present (e.g. inside the app image): "
                                          f"{', '.join(missing)}")
