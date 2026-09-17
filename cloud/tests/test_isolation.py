"""The control plane stays out of the crawler's production state.

Phase 5A's rule was absolute: nothing under ``cloud/`` imported the crawler.
Phase 5B needs exactly one exception — the runner adapter has to call the
engine — so the rule is now an allowlist, checked three ways:

* **statically**: only ``cloud/worker/careercrawler_runner.py`` may import a
  crawler package, and only the engine-facing modules it declares. No cloud
  module may import ``sqlite3``, Google's clients, or the crawler's ``store``,
  ``sheets``, ``weekly_run``, ``checkpoint`` or ``sync``;
* **the API at runtime**: importing the whole API loads no crawler module at all;
* **the adapter at runtime**: importing it and building its engine loads none of
  the forbidden modules (checked in ``test_careercrawler_runner.py``).
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
#: Libraries that would mean the cloud is talking to Sheets or SQLite.
FORBIDDEN_LIBRARIES = frozenset({"sqlite3", "googleapiclient", "google", "gspread"})
#: Crawler modules that hold production state. Never importable from the cloud.
FORBIDDEN_CRAWLER_MODULES = frozenset(
    {"store", "sheets", "crawler.weekly_run", "crawler.checkpoint", "crawler.sync", "crawler.status", "main"}
)

ADAPTER = CLOUD / "worker" / "careercrawler_runner.py"
#: The adapter's own tests build a real engine with fake adapters, so they may
#: also name the platform enum. Nothing else.
ADAPTER_TESTS = CLOUD / "tests" / "test_careercrawler_runner.py"


def _python_files():
    for path in CLOUD.rglob("*.py"):
        if any(part in {".venv", "node_modules", ".localdev", "runtime"} for part in path.parts):
            continue
        yield path


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module, node.lineno


class TestStaticImports(unittest.TestCase):
    def test_there_is_code_to_check(self) -> None:
        self.assertGreater(len(list(_python_files())), 30)

    def test_only_the_adapter_imports_the_crawler_and_only_what_it_declares(self) -> None:
        from cloud.worker.careercrawler_runner import ALLOWED_CRAWLER_MODULES

        allowed = {
            ADAPTER: ALLOWED_CRAWLER_MODULES,
            ADAPTER_TESTS: ALLOWED_CRAWLER_MODULES | {"crawler.platform_detector"},
            # Proves the crawler's own HTTP sessions go through the egress guard.
            CLOUD / "tests" / "test_egress.py": frozenset({"utils.http"}),
        }
        offenders = []
        for path in _python_files():
            for module, line in _imports(path):
                root = module.split(".")[0]
                if root in FORBIDDEN_LIBRARIES:
                    offenders.append(f"{path.relative_to(REPO)}:{line} imports {module}")
                elif root in CRAWLER_PACKAGES and module not in allowed.get(path, frozenset()):
                    offenders.append(f"{path.relative_to(REPO)}:{line} imports {module}")
        self.assertEqual(offenders, [])

    def test_the_allowlist_itself_excludes_production_state(self) -> None:
        from cloud.worker.careercrawler_runner import ALLOWED_CRAWLER_MODULES

        for module in ALLOWED_CRAWLER_MODULES:
            self.assertFalse(
                any(module == bad or module.startswith(bad + ".") for bad in FORBIDDEN_CRAWLER_MODULES),
                module,
            )

    def test_the_adapter_actually_uses_the_engine(self) -> None:
        imported = {module for module, _ in _imports(ADAPTER)}
        self.assertIn("crawler.crawler_engine", imported)


class TestRuntimeImports(unittest.TestCase):
    def _loaded(self, statement: str, roots) -> str:
        probe = (
            f"import sys; {statement}; "
            f"bad = sorted(m for m in sys.modules if m.split('.')[0] in {sorted(roots)!r}); "
            "print(','.join(bad))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], cwd=REPO, capture_output=True, text=True, timeout=120,
            env={**__import__("os").environ, "CAREERCLOUD_ENV": "test"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_importing_the_api_loads_nothing_from_the_crawler(self) -> None:
        self.assertEqual(self._loaded("import cloud.api.main", CRAWLER_PACKAGES | FORBIDDEN_LIBRARIES), "")

    def test_the_adapter_and_its_engine_load_no_production_state(self) -> None:
        loaded = self._loaded(
            "import cloud.worker.careercrawler_runner as r; "
            "import crawler.crawler_engine as e; e.CrawlerEngine(); "
            "import exporters.excel_exporter, utils.http",
            {"store", "sheets", "sqlite3", "googleapiclient", "gspread"},
        )
        self.assertEqual(loaded, "")
        for module in ("crawler.weekly_run", "crawler.checkpoint", "crawler.sync"):
            probe = self._loaded(
                "import crawler.crawler_engine as e; e.CrawlerEngine(); "
                f"assert {module!r} not in sys.modules, {module!r}",
                set(),
            )
            self.assertEqual(probe, "")


if __name__ == "__main__":
    unittest.main()
