"""One crawler per database, enforced by the kernel.

The failure these defend against is not hypothetical. On 2026-09-05 a manually
started crawl and the Saturday scheduled task ran together against one
``state/crawler.db``: 125 postings were closed on a second, unluckier read of
boards the first pass had already crawled, the run's ``WEEKLY_RUNS`` record was
overwritten with the intruder's counters, and an archived checkpoint was
resurrected. Every lock in the project at the time was a
:class:`threading.Lock`, which says nothing whatever about a second process, and
the Windows task's ``MultipleInstances: IgnoreNew`` could not help because it
suppresses a second instance of the *task* — a process started from a shell is
not one.

So the tests that matter here run a **real second process**. A same-process
check would prove something about file descriptors; only a subprocess proves the
thing that actually went wrong.

The other property worth its own test is what happens when a holder dies badly.
A PID file or a lease row has to guess whether a recorded owner is still alive;
an advisory file lock does not, because the kernel drops it when the process
ends however it ends. :meth:`TestAKilledHolder.test_a_killed_holder_leaves_no_stale_lock`
kills a holder outright and takes the lock immediately afterwards.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest import mock

from crawler.weekly_run import RunSummary, main
from utils.runlock import (
    EXIT_ALREADY_RUNNING,
    RunLock,
    RunLockBusy,
    holder_of,
    lock_path_for,
)

#: Source of a process whose only job is to take the lock and sit on it.
#:
#: It announces readiness on stdout so the parent never has to sleep and hope,
#: and reports **its own** pid because ``venv/Scripts/python.exe`` on Windows is
#: a launcher stub that re-execs the real interpreter: ``Popen.pid`` is the
#: stub, the lock is held by its child, and killing the stub would leave the
#: holder running. Every assertion and every kill here uses the reported pid.
#:
#: The lock is assigned to a name deliberately. ``RunLock(...).acquire()`` on
#: its own would leave the object unreferenced, and before :data:`_HELD`
#: existed that dropped the lock the moment it was collected.
_HOLDER = """
import os, sys, time
sys.path.insert(0, {root!r})
from utils.runlock import RunLock
lock = RunLock(sys.argv[1], run_id="held-by-child").acquire()
print("READY", os.getpid(), flush=True)
time.sleep(300)
"""


@dataclass
class Holder:
    """A subprocess holding the lock, addressed by the pid that really holds it.

    Attributes:
        process: The process this test started, which on Windows may be a
            launcher stub rather than the interpreter holding the lock.
        pid: The interpreter's own pid, as it reported on stdout. This is what
            assertions compare against and what :meth:`kill` ends.
    """

    process: subprocess.Popen
    pid: int

    def kill(self) -> None:
        """End the holder and wait for the lock to be gone.

        Kills the reported pid rather than ``process.pid`` -- ending the stub
        would leave the real holder alive and the lock still taken.
        """
        for target in (self.pid, self.process.pid):
            try:
                os.kill(target, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass

        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged child
            self.process.kill()
            self.process.wait(timeout=30)

        if self.process.stdout is not None:
            self.process.stdout.close()


class LockTest(unittest.TestCase):
    """A temporary directory, and a helper for holding the lock elsewhere."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "crawler.db"
        self.lock_path = lock_path_for(self.database)
        self._children: List[Holder] = []

    def tearDown(self) -> None:
        for holder in self._children:
            holder.kill()
        self.directory.cleanup()

    def hold_elsewhere(self, path: Optional[Path] = None) -> "Holder":
        """Start a process that holds the lock and does not let go.

        Args:
            path: The lock to take. Defaults to this test's.

        Returns:
            The holder, already holding the lock, addressed by its real pid.
        """
        script = self.root / f"hold{len(self._children)}.py"
        script.write_text(
            _HOLDER.format(root=str(Path(__file__).resolve().parent.parent)),
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [sys.executable, str(script), str(path or self.lock_path)],
            stdout=subprocess.PIPE,
            text=True,
        )

        announced = process.stdout.readline().split() if process.stdout else []
        self.assertEqual(
            announced[:1], ["READY"], "the holder process never took the lock"
        )

        holder = Holder(process=process, pid=int(announced[1]))
        self._children.append(holder)
        return holder


# ---------------------------------------------------------------------------
# 1. A second process is refused
# ---------------------------------------------------------------------------


class TestASecondProcessIsRefused(LockTest):
    """The whole point: two crawlers cannot share one database."""

    def test_a_second_process_cannot_acquire_a_held_lock(self) -> None:
        self.hold_elsewhere()

        with self.assertRaises(RunLockBusy):
            RunLock(self.lock_path).acquire()

    def test_the_refusal_is_immediate_rather_than_a_wait(self) -> None:
        """A weekly crawl queueing behind another for hours is worse than one
        that says so and exits."""
        import time

        self.hold_elsewhere()

        started = time.monotonic()
        with self.assertRaises(RunLockBusy):
            RunLock(self.lock_path).acquire()
        self.assertLess(time.monotonic() - started, 5.0)

    def test_the_holder_is_named_in_the_error(self) -> None:
        running = self.hold_elsewhere()

        with self.assertRaises(RunLockBusy) as caught:
            RunLock(self.lock_path).acquire()

        holder = caught.exception.holder
        self.assertEqual(holder.get("pid"), running.pid)
        self.assertEqual(holder.get("run_id"), "held-by-child")
        self.assertTrue(holder.get("host"))
        self.assertTrue(holder.get("started_at"))

        message = str(caught.exception)
        self.assertIn(str(running.pid), message)
        self.assertIn("held-by-child", message)
        self.assertIn(str(self.lock_path), message)

    def test_a_failed_attempt_does_not_disturb_the_holder(self) -> None:
        """The loser must not blank the winner's description, nor free it."""
        self.hold_elsewhere()

        for _ in range(3):
            with self.assertRaises(RunLockBusy):
                RunLock(self.lock_path).acquire()

        self.assertEqual(holder_of(self.lock_path).get("run_id"), "held-by-child")
        with self.assertRaises(RunLockBusy):
            RunLock(self.lock_path).acquire()


# ---------------------------------------------------------------------------
# 2. Releasing
# ---------------------------------------------------------------------------


class TestReleasing(LockTest):
    """A lock given up is a lock another process can take."""

    def test_a_normal_release_lets_another_holder_in(self) -> None:
        lock = RunLock(self.lock_path).acquire()
        self.assertTrue(lock.held)
        lock.release()
        self.assertFalse(lock.held)

        self.hold_elsewhere()  # asserts it succeeded

    def test_the_context_manager_releases_on_a_clean_exit(self) -> None:
        with RunLock(self.lock_path) as lock:
            self.assertTrue(lock.held)
        self.assertFalse(lock.held)

        self.hold_elsewhere()

    def test_the_context_manager_releases_after_an_exception(self) -> None:
        """A crawl that raises must not leave the lock behind it."""
        lock = RunLock(self.lock_path)

        with self.assertRaises(ZeroDivisionError):
            with lock:
                raise ZeroDivisionError("the crawl exploded")

        self.assertFalse(lock.held)
        self.hold_elsewhere()

    def test_release_is_idempotent(self) -> None:
        lock = RunLock(self.lock_path).acquire()
        lock.release()
        lock.release()
        self.assertFalse(lock.held)

    def test_acquiring_twice_on_one_object_is_a_no_op(self) -> None:
        lock = RunLock(self.lock_path).acquire()
        self.assertIs(lock.acquire(), lock)
        self.assertTrue(lock.held)
        lock.release()
        self.assertFalse(lock.held)


# ---------------------------------------------------------------------------
# 3. A holder that dies badly
# ---------------------------------------------------------------------------


class TestAKilledHolder(LockTest):
    """The reason this is a kernel lock and not a PID file."""

    def test_a_killed_holder_leaves_no_stale_lock(self) -> None:
        """SIGKILL, then take the lock immediately.

        No heartbeat, no expiry, no PID liveness check — the kernel drops the
        lock when the process ends, so there is no stale state to reason about.
        """
        holder = self.hold_elsewhere()

        with self.assertRaises(RunLockBusy):
            RunLock(self.lock_path).acquire()

        holder.kill()

        lock = RunLock(self.lock_path).acquire()
        self.assertTrue(lock.held)
        lock.release()

    def test_the_dead_holders_description_does_not_mislead(self) -> None:
        """The file outlives the process; the lock does not.

        The stale description is still readable, which is why the lock and not
        the description is what decides.
        """
        holder = self.hold_elsewhere()
        holder.kill()

        self.assertEqual(holder_of(self.lock_path).get("pid"), holder.pid)

        with RunLock(self.lock_path, run_id="the-new-run") as lock:
            self.assertTrue(lock.held)
            self.assertEqual(holder_of(self.lock_path).get("run_id"), "the-new-run")


# ---------------------------------------------------------------------------
# 4. Scoping: one lock per database, never global
# ---------------------------------------------------------------------------


class TestScoping(LockTest):
    """The lock guards one database and nothing else."""

    def test_the_lock_path_is_derived_from_the_database(self) -> None:
        self.assertEqual(lock_path_for(Path("state/crawler.db")), Path("state/crawler.lock"))
        self.assertEqual(lock_path_for("state/crawler.db").name, "crawler.lock")

    def test_the_lock_sits_beside_its_database(self) -> None:
        """Never a system-wide path, and never outside the database's own
        directory."""
        self.assertEqual(lock_path_for(self.database).parent, self.database.parent)

    def test_two_databases_do_not_block_each_other(self) -> None:
        """A run pointed at another store is unaffected by construction."""
        other = self.root / "other.db"
        self.assertNotEqual(lock_path_for(other), self.lock_path)

        self.hold_elsewhere(lock_path_for(other))

        with RunLock(self.lock_path) as mine:
            self.assertTrue(mine.held)

    def test_a_missing_directory_is_created(self) -> None:
        nested = self.root / "deeper" / "still" / "crawler.db"
        with RunLock(lock_path_for(nested)) as lock:
            self.assertTrue(lock.held)
            self.assertTrue(lock_path_for(nested).is_file())


# ---------------------------------------------------------------------------
# 5. The holder description
# ---------------------------------------------------------------------------


class TestHolderDescription(LockTest):
    """Diagnostics for whoever reads the error message next."""

    def test_the_description_is_readable_while_held(self) -> None:
        with RunLock(self.lock_path, run_id="2026-W37-abc") as lock:
            self.assertTrue(lock.held)
            holder = holder_of(self.lock_path)

        self.assertEqual(holder.get("run_id"), "2026-W37-abc")
        self.assertTrue(holder.get("host"))
        self.assertTrue(holder.get("started_at"))
        self.assertIsInstance(holder.get("pid"), int)

    def test_an_absent_file_describes_nobody(self) -> None:
        self.assertEqual(holder_of(self.root / "never-made.lock"), {})

    def test_an_unreadable_file_describes_nobody_rather_than_raising(self) -> None:
        """A corrupt description must not replace the real error."""
        self.lock_path.write_bytes(b"\xff\xfe not json at all")
        self.assertEqual(holder_of(self.lock_path), {})

    def test_a_shorter_record_does_not_leave_a_longer_one_behind(self) -> None:
        """The header is padded, so re-describing cannot leave a tail."""
        with RunLock(self.lock_path, run_id="a-very-long-run-identifier-indeed"):
            pass
        with RunLock(self.lock_path, run_id="short"):
            holder = holder_of(self.lock_path)

        self.assertEqual(holder.get("run_id"), "short")


# ---------------------------------------------------------------------------
# 6. The command line
# ---------------------------------------------------------------------------


def _stub_connection() -> SimpleNamespace:
    """Enough of a Sheets connection for main() to get past connect()."""
    stats = SimpleNamespace(describe=lambda: "0 read(s), 0 write(s)")
    return SimpleNamespace(
        client=SimpleNamespace(stats=stats),
        account="test@example.invalid",
        spreadsheet_id="fake",
        read_only=False,
    )


class TestTheCommandLine(LockTest):
    """``main()`` takes the lock, and says so usefully when it cannot."""

    def setUp(self) -> None:
        super().setUp()

        # main() calls configure(), which mutates a module-level singleton the
        # whole process shares -- and it switches on browser fallback and
        # careers discovery. Left unrestored, every test module that runs after
        # this one inherits them and starts launching Chromium and probing real
        # websites, which turns a fifteen-second suite into one that does not
        # finish. tests.test_incremental_persistence guards main() the same way,
        # for the same reason.
        from dataclasses import fields

        from config.settings import SETTINGS

        self._settings = {item.name: getattr(SETTINGS, item.name) for item in fields(SETTINGS)}

    def tearDown(self) -> None:
        from config.settings import SETTINGS

        for name, value in self._settings.items():
            setattr(SETTINGS, name, value)
        super().tearDown()

    def run_main(self, *extra: str) -> int:
        """Call main() against this test's temporary paths."""
        argv = [
            "--database", str(self.database),
            "--checkpoint", str(self.root / "checkpoint.json"),
            "--no-browser", "--no-discover",
            *extra,
        ]
        with mock.patch("sheets._cli.connect", return_value=(_stub_connection(), 0)), \
             mock.patch("crawler.weekly_run._configure_logging"), \
             mock.patch(
                 "crawler.weekly_run.WeeklyRun.execute",
                 return_value=RunSummary(run_id="stub"),
             ):
            return main(argv)

    def test_main_returns_75_when_another_run_holds_the_database(self) -> None:
        self.hold_elsewhere()

        self.assertEqual(self.run_main(), EXIT_ALREADY_RUNNING)

    def test_75_is_distinct_from_a_failure(self) -> None:
        """A scheduler must be able to tell "already running" from "broke"."""
        self.assertEqual(EXIT_ALREADY_RUNNING, 75)
        self.assertNotIn(EXIT_ALREADY_RUNNING, (0, 1, 2, 130))

    def test_main_takes_the_lock_for_a_real_run(self) -> None:
        """While main() runs, nobody else can have the database."""
        seen: List[bool] = []

        def busy(*_a: Any, **_k: Any) -> RunSummary:
            try:
                RunLock(self.lock_path).acquire()
                seen.append(False)
            except RunLockBusy:
                seen.append(True)
            return RunSummary(run_id="stub")

        with mock.patch("sheets._cli.connect", return_value=(_stub_connection(), 0)), \
             mock.patch("crawler.weekly_run._configure_logging"), \
             mock.patch("crawler.weekly_run.WeeklyRun.execute", side_effect=busy):
            code = main([
                "--database", str(self.database),
                "--checkpoint", str(self.root / "checkpoint.json"),
                "--no-browser", "--no-discover",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(seen, [True], "main() ran without holding the lock")

    def test_main_releases_the_lock_when_it_finishes(self) -> None:
        self.assertEqual(self.run_main(), 0)
        self.hold_elsewhere()  # asserts the lock is free again

    def test_no_lock_bypasses_a_held_lock(self) -> None:
        """The escape hatch works, and is the only thing that does."""
        self.hold_elsewhere()

        self.assertEqual(self.run_main(), EXIT_ALREADY_RUNNING)
        self.assertEqual(self.run_main("--no-lock"), 0)

    def test_lock_path_overrides_where_the_lock_lives(self) -> None:
        elsewhere = self.root / "chosen.lock"
        self.hold_elsewhere(elsewhere)

        # The derived lock is free, so only --lock-path can collide.
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(
            self.run_main("--lock-path", str(elsewhere)), EXIT_ALREADY_RUNNING
        )

    def test_a_dry_run_takes_no_lock(self) -> None:
        """A dry run opens no database and writes nothing, so it is safe
        beside a live crawl -- which is exactly when one is wanted."""
        self.hold_elsewhere()

        with mock.patch("sheets._cli.connect", return_value=(_stub_connection(), 0)), \
             mock.patch("crawler.weekly_run._configure_logging"), \
             mock.patch(
                 "crawler.weekly_run.WeeklyRun.execute",
                 return_value=RunSummary(run_id="stub"),
             ):
            code = main([
                "--database", str(self.database),
                "--checkpoint", str(self.root / "checkpoint.json"),
                "--no-browser", "--no-discover", "--dry-run",
            ])

        self.assertEqual(code, 0)

    def test_the_flags_are_documented(self) -> None:
        from crawler.weekly_run import _parse_args

        args = _parse_args([])
        self.assertIsNone(args.lock_path)
        self.assertFalse(args.no_lock)

        parsed = _parse_args(["--no-lock", "--lock-path", "x.lock"])
        self.assertTrue(parsed.no_lock)
        self.assertEqual(parsed.lock_path, Path("x.lock"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
