"""The control plane stays out of the crawler's way.

Phase 5A's promise is that nothing under ``cloud/`` can reach the crawler, its
database or its Google Sheet. These tests hold it to that statically — no module
imports a crawler package — and at runtime, by importing the whole API and
checking what came along with it.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

CLOUD = Path(__file__).resolve().parent.parent
REPO = CLOUD.parent

#: Top-level packages that belong to the crawler (plus the Seamless worktree's).
CRAWLER_PACKAGES = frozenset(
    {"crawler", "adapters", "sheets", "store", "exporters", "models", "utils", "config",
     "tools", "main", "seamless", "discovery", "zerocredit", "tests"}
)
#: Libraries that would mean the cloud is talking to Sheets, SQLite or a browser.
FORBIDDEN_LIBRARIES = frozenset({"sqlite3", "googleapiclient", "google", "playwright", "gspread"})


def _python_files():
    for path in CLOUD.rglob("*.py"):
        if any(part in {".venv", "node_modules"} for part in path.parts):
            continue
        yield path


def _imported_roots(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0], node.lineno


class TestStaticImports(unittest.TestCase):
    def test_there_is_code_to_check(self) -> None:
        self.assertGreater(len(list(_python_files())), 10)

    def test_no_cloud_module_imports_the_crawler_or_its_stores(self) -> None:
        offenders = [
            f"{path.relative_to(REPO)}:{line} imports {root}"
            for path in _python_files()
            for root, line in _imported_roots(path)
            if root in CRAWLER_PACKAGES or root in FORBIDDEN_LIBRARIES
        ]
        self.assertEqual(offenders, [])


class TestRuntimeImports(unittest.TestCase):
    def test_importing_the_api_loads_nothing_from_the_crawler(self) -> None:
        probe = (
            "import sys, cloud.api.main; "
            f"bad = sorted(m for m in sys.modules if m.split('.')[0] in {sorted(CRAWLER_PACKAGES | FORBIDDEN_LIBRARIES)!r}); "
            "print(','.join(bad))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], cwd=REPO, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
