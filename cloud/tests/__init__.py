"""Tests for the CareerCloud control plane.

Run from the repository root with the cloud venv::

    cloud/.venv/Scripts/python -m unittest discover -s cloud/tests -t .

They need FastAPI and Pydantic, which the crawler's venv deliberately lacks, so
under that venv the whole package reports as skipped instead of failing. The
crawler's own suite (``python -m unittest discover -s tests``) never reaches here.
"""

import unittest

try:
    import fastapi  # noqa: F401
    import pydantic  # noqa: F401
except ImportError as missing:  # pragma: no cover - depends on the venv
    raise unittest.SkipTest(f"cloud dependencies not installed: {missing}")
