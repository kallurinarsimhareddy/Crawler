"""The browser pool: bounded, reusable, and never launched by a unit test.

Every test here injects a fake launcher. No Chromium process starts, which is
what keeps the suite fast enough to run on every change — the existing
browser tests already prove the real driver works.

What the pool is for: launching Chromium costs a second or two and a hundred
megabytes. Doing that per page, across twenty workers and twelve thousand
companies, is most of a run. Reuse is the point; bounding it is what stops
reuse becoming an unbounded pile of processes.
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Optional

from utils.browser_pool import BrowserPool, PoolConfig


class FakeBrowser:
    """A stand-in for a Playwright browser handle."""

    def __init__(self, index: int) -> None:
        self.index = index
        self.closed = False
        self.pages = 0

    def close(self) -> None:
        """Record that the pool released it."""
        self.closed = True


class FakeLauncher:
    """Hands out fake browsers and counts how many were ever made."""

    def __init__(self, fail_after: Optional[int] = None) -> None:
        self.launched = 0
        self.fail_after = fail_after
        self.lock = threading.Lock()

    def __call__(self) -> FakeBrowser:
        """Launch one.

        Returns:
            A fake browser.

        Raises:
            RuntimeError: Once ``fail_after`` launches have happened, to
                simulate a machine that runs out of room for processes.
        """
        with self.lock:
            if self.fail_after is not None and self.launched >= self.fail_after:
                raise RuntimeError("no browser could be launched")
            self.launched += 1
            return FakeBrowser(self.launched)


class TestPoolBounds(unittest.TestCase):
    """However many workers ask, only so many browsers exist."""

    def test_a_browser_is_reused_rather_than_relaunched(self) -> None:
        launcher = FakeLauncher()
        pool = BrowserPool(PoolConfig(size=2), launcher=launcher)

        for _ in range(5):
            with pool.acquire():
                pass

        self.assertEqual(launcher.launched, 1)
        pool.shutdown()

    def test_the_pool_never_exceeds_its_size(self) -> None:
        launcher = FakeLauncher()
        pool = BrowserPool(PoolConfig(size=3), launcher=launcher)
        peak = 0
        current = 0
        lock = threading.Lock()

        def visit() -> None:
            """Hold a browser briefly."""
            nonlocal peak, current
            with pool.acquire():
                with lock:
                    current += 1
                    peak = max(peak, current)
                time.sleep(0.03)
                with lock:
                    current -= 1

        threads = [threading.Thread(target=visit) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertLessEqual(peak, 3)
        self.assertLessEqual(launcher.launched, 3)
        pool.shutdown()

    def test_browser_concurrency_is_independent_of_worker_count(self) -> None:
        """Twenty HTTP workers may share two browsers. That is the point."""
        launcher = FakeLauncher()
        pool = BrowserPool(PoolConfig(size=2), launcher=launcher)
        done = []

        def visit() -> None:
            """Take a browser and give it back."""
            with pool.acquire():
                done.append(1)

        threads = [threading.Thread(target=visit) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(done), 20)
        self.assertLessEqual(launcher.launched, 2)
        pool.shutdown()


class TestPoolLifecycle(unittest.TestCase):
    """Cleanup, crash recovery and the per-company budget."""

    def test_shutdown_closes_every_browser(self) -> None:
        launcher = FakeLauncher()
        pool = BrowserPool(PoolConfig(size=2), launcher=launcher)
        held = []
        with pool.acquire() as browser:
            held.append(browser)
        pool.shutdown()

        self.assertTrue(all(browser.closed for browser in held))

    def test_a_crashed_browser_is_replaced_not_reused(self) -> None:
        launcher = FakeLauncher()
        pool = BrowserPool(PoolConfig(size=1), launcher=launcher)

        with pool.acquire() as browser:
            pool.discard(browser)          # the driver died mid-visit

        with pool.acquire() as replacement:
            self.assertIsNotNone(replacement)

        self.assertEqual(launcher.launched, 2)
        pool.shutdown()

    def test_a_launch_failure_yields_none_rather_than_raising(self) -> None:
        """A machine with no Chromium must degrade, not crash the run."""
        pool = BrowserPool(PoolConfig(size=1), launcher=FakeLauncher(fail_after=0))

        with pool.acquire() as browser:
            self.assertIsNone(browser)

        pool.shutdown()

    def test_the_pool_survives_an_exception_inside_the_block(self) -> None:
        launcher = FakeLauncher()
        pool = BrowserPool(PoolConfig(size=1), launcher=launcher)

        with self.assertRaises(ValueError):
            with pool.acquire():
                raise ValueError("boom")

        # The browser must have gone back, not leaked.
        with pool.acquire() as browser:
            self.assertIsNotNone(browser)
        pool.shutdown()

    def test_shutdown_is_idempotent(self) -> None:
        pool = BrowserPool(PoolConfig(size=1), launcher=FakeLauncher())
        with pool.acquire():
            pass
        pool.shutdown()
        pool.shutdown()


class TestPerCompanyBudget(unittest.TestCase):
    """One company cannot spend the whole run's browser time."""

    def test_a_company_budget_is_enforced(self) -> None:
        pool = BrowserPool(PoolConfig(size=2, per_company=2), launcher=FakeLauncher())

        self.assertTrue(pool.may_render("acme"))
        pool.note_render("acme")
        self.assertTrue(pool.may_render("acme"))
        pool.note_render("acme")
        self.assertFalse(pool.may_render("acme"))
        pool.shutdown()

    def test_budgets_are_per_company(self) -> None:
        pool = BrowserPool(PoolConfig(size=2, per_company=1), launcher=FakeLauncher())
        pool.note_render("acme")

        self.assertFalse(pool.may_render("acme"))
        self.assertTrue(pool.may_render("other"))
        pool.shutdown()

    def test_a_run_wide_budget_is_enforced(self) -> None:
        pool = BrowserPool(
            PoolConfig(size=2, per_company=99, total=3), launcher=FakeLauncher()
        )
        for index in range(3):
            self.assertTrue(pool.may_render(f"c{index}"))
            pool.note_render(f"c{index}")

        self.assertFalse(pool.may_render("c4"))
        pool.shutdown()

    def test_statistics_are_reported(self) -> None:
        launcher = FakeLauncher()
        pool = BrowserPool(PoolConfig(size=2), launcher=launcher)
        with pool.acquire():
            pass
        pool.note_render("acme")

        stats = pool.stats()
        self.assertEqual(stats["launched"], 1)
        self.assertEqual(stats["renders"], 1)
        pool.shutdown()

    def test_a_failed_launch_is_counted(self) -> None:
        pool = BrowserPool(PoolConfig(size=1), launcher=FakeLauncher(fail_after=0))
        with pool.acquire():
            pass
        self.assertEqual(pool.stats()["failures"], 1)
        pool.shutdown()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
