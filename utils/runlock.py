"""One crawler at a time, guaranteed by the kernel rather than by convention.

Every lock elsewhere in this project is a :class:`threading.Lock`: it keeps the
workers of *one* process out of each other's way and says nothing at all about a
second process. On 2026-09-05 that gap cost a run — a manually started crawl and
the Saturday scheduled task ran together against the same ``state/crawler.db``,
the same checkpoint and the same spreadsheet. 125 postings were closed on a
second, unluckier read of boards the first pass had already crawled, and the
run's ``WEEKLY_RUNS`` record was overwritten with the intruder's counters. The
Windows task's ``MultipleInstances: IgnoreNew`` did not help and could not: it
suppresses a second instance *of the scheduled task*, and a process launched
from a shell is not one.

    >>> with RunLock(Path("state/crawler.lock")):
    ...     ...                     # nobody else can be in here

**Why a file lock and not a row, a PID file or a named mutex.** The lock has to
survive the ways a crawl actually dies: a power cut, an OOM kill, ``kill -9``, a
laptop lid. A PID file left by a killed process is indistinguishable from a live
one without guessing about PID reuse; a database row needs a heartbeat, an
expiry and a clock all three processes agree on. An advisory file lock needs
none of that, because **the kernel releases it when the process ends, however it
ends**. There is no stale state to reason about and nothing to tune.

**The scope is one database, deliberately.** :func:`lock_path_for` puts the lock
beside the file it protects, so ``state/crawler.db`` is guarded by
``state/crawler.lock`` and a run pointed at a different ``--database`` takes a
different lock automatically. Nothing here is system-wide, nothing is named
globally, and nothing outside the CareerCrawler database's own directory is ever
opened — a separate tool with its own store is unaffected by design, not by
luck.

**Advisory, not mandatory.** Neither ``flock`` nor ``msvcrt.locking`` stops a
process that never asks. It stops *this* program running twice, which is the
whole problem, and it does so without making the database unreadable to the
read-only diagnostics that have to work while a crawl is in flight.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Final, Optional

from loguru import logger

__all__ = [
    "EXIT_ALREADY_RUNNING",
    "RunLock",
    "RunLockBusy",
    "holder_of",
    "lock_path_for",
]

#: Returned by a command that found the lock held. ``EX_TEMPFAIL`` from
#: ``sysexits.h``: the run did not fail, it declined to start, and a scheduler
#: reading exit codes should treat the two differently. systemd's
#: ``SuccessExitStatus=`` accepts it, so a skipped weekly run is not an alert.
EXIT_ALREADY_RUNNING: Final[int] = 75

#: Bytes reserved at the head of the file for the holder's description. Fixed so
#: a shorter record cannot leave the tail of a longer one behind it, which
#: matters because the file is never truncated: on Windows the lock is a byte
#: range on this same file, and shortening it underneath a live lock is not
#: something to find out about in production.
_HEADER_BYTES: Final[int] = 512

#: The byte the Windows lock is taken on, far past the header so that locking
#: and describing never contend. ``msvcrt.locking`` locks a range rather than
#: the file, and a range beyond end-of-file is legal. POSIX ignores this
#: entirely: ``flock`` locks the open file description, not a region.
_LOCK_OFFSET: Final[int] = 1 << 30

#: Every lock currently held by this process.
#:
#: Both platforms tie the lock to the *open file handle*, so the lock lasts
#: exactly as long as that handle does -- and a :class:`RunLock` nobody keeps a
#: reference to is collected, its handle closed, and the lock silently dropped
#: while the run it was protecting carries on believing it is safe.
#:
#: ``RunLock(path).acquire()`` is the natural way to write this and would be
#: exactly that bug, so a held lock keeps itself alive here and lets go in
#: :meth:`RunLock.release`. Found the honest way: a test helper wrote precisely
#: that line, and a second process walked straight in.
_HELD: Final[set] = set()


class RunLockBusy(RuntimeError):
    """Another process already holds the lock.

    Args:
        path: The lock file.
        holder: What that process recorded about itself, if it could be read.
    """

    def __init__(self, path: Path, holder: Optional[Dict[str, Any]] = None) -> None:
        self.path = Path(path)
        self.holder = dict(holder or {})

        who = ""
        if self.holder:
            who = (
                f" Held by {self.holder.get('host', '?')}"
                f" pid {self.holder.get('pid', '?')}"
                f", run {self.holder.get('run_id') or 'unnamed'}"
                f", since {self.holder.get('started_at', '?')}."
            )

        super().__init__(
            f"Another CareerCrawler run is already using this database.{who} "
            f"Lock: {self.path}. Wait for it to finish, or point this run at a "
            f"different --database. Do not pass --no-lock to get past this "
            f"unless you have confirmed the other process is gone."
        )


def lock_path_for(database_path: Path | str) -> Path:
    """The lock guarding one database.

    Args:
        database_path: The database this run will open.

    Returns:
        A sibling path with a ``.lock`` suffix — ``state/crawler.db`` gives
        ``state/crawler.lock``. Deriving it rather than fixing it is what keeps
        the lock scoped to the store it protects: two databases are two locks,
        and no path outside that database's own directory is ever touched.
    """
    resolved = Path(database_path)
    return resolved.with_suffix(".lock")


def holder_of(path: Path | str) -> Dict[str, Any]:
    """Read what the current holder recorded, without taking the lock.

    Safe to call while a run is in flight: reading the header does not contend
    with the lock, which lives at :data:`_LOCK_OFFSET`.

    Args:
        path: The lock file.

    Returns:
        The holder's details, or ``{}`` when the file is absent, empty or not
        readable as a record. Never raises — this exists to make an error
        message better, and an unreadable file must not replace the real one.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_HEADER_BYTES)
    except OSError:
        return {}

    text = raw.split(b"\x00", 1)[0].strip()
    if not text:
        return {}

    try:
        found = json.loads(text.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}

    return found if isinstance(found, dict) else {}


@dataclass(eq=False)
class RunLock:
    """An exclusive, kernel-backed claim on one crawler database.

    Usable as a context manager, which is how :mod:`crawler.weekly_run` takes
    it, or explicitly through :meth:`acquire` and :meth:`release` when the
    caller needs the lock to outlive a single block.

    ``eq=False`` so instances hash by identity: two locks on one path are two
    distinct claims, and :data:`_HELD` must be able to tell them apart.

    Args:
        path: Where the lock lives. Use :func:`lock_path_for` to derive it from
            the database rather than naming one, so the scoping rule holds.
        run_id: Recorded for whoever reads the error message next.
    """

    path: Path
    run_id: str = ""

    _handle: Optional[Any] = None

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    # -- taking and giving up the lock ---------------------------------------

    def acquire(self) -> "RunLock":
        """Take the lock, or report who has it.

        Returns:
            Self, so ``RunLock(p).acquire()`` reads as one expression.

        Raises:
            RunLockBusy: Another process holds it. Raised without waiting: a
                weekly crawl that queues behind another for six hours is worse
                than one that says so and exits.
            OSError: The lock file could not be created at all — a missing
                directory or a read-only disk, which is an operator problem and
                must not be mistaken for contention.
        """
        if self._handle is not None:
            return self

        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Read/write, created if absent, never truncated. Opened through
        # os.open because "a+b" would append: in append mode every write lands
        # at end-of-file whatever the seek position says, so the holder
        # description would accumulate instead of being replaced and a reader
        # would always get the *first* holder the file ever had. And truncating
        # is not an option either -- the previous holder's header is overwritten
        # in place once the lock is ours, while a process that merely *tried*
        # must not blank the live holder's description.
        handle = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644), "r+b")
        try:
            handle.seek(0)
            self._take(handle, self.path)
        except RunLockBusy:
            handle.close()
            raise
        except Exception:
            handle.close()
            raise

        self._handle = handle
        # Before anything else can drop the reference: from here the lock
        # outlives whatever the caller does with this object.
        _HELD.add(self)
        self._describe()
        logger.debug("Run lock held on {}", self.path)
        return self

    def release(self) -> None:
        """Give the lock up.

        Idempotent, and never raises: releasing is the last thing a run does
        and must not be able to turn a completed crawl into a failed one. The
        kernel would release it on exit regardless; this exists so that a
        long-lived process — the test suite, or a supervisor — can take the
        lock again without exiting first.
        """
        handle, self._handle = self._handle, None
        _HELD.discard(self)
        if handle is None:
            return

        try:
            self._give_up(handle)
        except Exception as exc:  # noqa: BLE001 - releasing is best effort
            logger.debug("Ignoring an error releasing {}: {}", self.path, exc)
        finally:
            try:
                handle.close()
            except Exception as exc:  # noqa: BLE001 - as above
                logger.debug("Ignoring an error closing {}: {}", self.path, exc)

        logger.debug("Run lock released on {}", self.path)

    @property
    def held(self) -> bool:
        """Whether this object currently holds the lock."""
        return self._handle is not None

    # -- context manager -----------------------------------------------------

    def __enter__(self) -> "RunLock":
        """Take the lock for the duration of a block."""
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        """Give it up, however the block ended."""
        self.release()

    # -- platform specifics --------------------------------------------------

    @staticmethod
    def _take(handle: Any, path: Path) -> None:
        """Lock the open file, without waiting.

        Args:
            handle: The open lock file. Its ``name`` is a file descriptor
                rather than a path -- it comes from :func:`os.fdopen` -- so the
                path is passed separately for the error message.
            path: The lock file, for reporting.

        Raises:
            RunLockBusy: Signalled by the platform call refusing.
        """
        if os.name == "nt":  # pragma: no cover - exercised on Windows only
            import msvcrt

            handle.seek(_LOCK_OFFSET)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RunLockBusy(path, holder_of(path)) from exc
            finally:
                handle.seek(0)
            return

        import fcntl  # pragma: no cover - exercised on POSIX only

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RunLockBusy(path, holder_of(path)) from exc

    @staticmethod
    def _give_up(handle: Any) -> None:
        """Unlock the open file.

        Args:
            handle: The open lock file.
        """
        if os.name == "nt":  # pragma: no cover - exercised on Windows only
            import msvcrt

            handle.seek(_LOCK_OFFSET)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                handle.seek(0)
            return

        import fcntl  # pragma: no cover - exercised on POSIX only

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # -- the holder's description --------------------------------------------

    def _describe(self) -> None:
        """Record who holds the lock, for the next process's error message.

        Written after the lock is ours and padded to :data:`_HEADER_BYTES`, so
        a shorter record never leaves the tail of a longer one behind it. Best
        effort throughout: failing to describe the holder must not fail a run
        that legitimately holds the lock.
        """
        handle = self._handle
        if handle is None:  # pragma: no cover - only reachable via misuse
            return

        try:
            host = socket.gethostname() or "unknown"
        except OSError:  # pragma: no cover - a host with no name
            host = "unknown"

        from utils.clock import iso

        payload = json.dumps(
            {
                "host": host,
                "pid": os.getpid(),
                "run_id": self.run_id,
                "started_at": iso(),
                "argv": " ".join(sys.argv[:6]),
            }
        ).encode("utf-8")[:_HEADER_BYTES]

        try:
            handle.seek(0)
            handle.write(payload.ljust(_HEADER_BYTES, b"\x00"))
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as exc:  # noqa: PERF203 - describing is not the point
            logger.debug("Could not describe the lock holder: {}", exc)
        finally:
            try:
                handle.seek(0)
            except OSError:  # pragma: no cover - the handle is already broken
                pass
