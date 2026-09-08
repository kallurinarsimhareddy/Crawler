"""Let an interrupted run pick up where it stopped.

A full crawl of the reference sheet takes five hours. Over that window a laptop
sleeps, a VPN drops, Windows installs an update, and somebody closes the console
window. Without a checkpoint every one of those costs the whole run::

    >>> from crawler.checkpoint import Checkpoint
    >>> checkpoint = Checkpoint.resume_or_start(run_id, total=7570)
    >>> checkpoint.remaining(every_company_key)
    ['domain:acme.com', ...]

**The checkpoint is a local file, not a spreadsheet row.** That is deliberate,
and it is the one piece of state version 3 keeps outside Google Sheets. A resume
mechanism that itself depends on the network fails in exactly the circumstances
it exists for — and the Sheets read quota is sixty calls a minute, which a
per-batch checkpoint write would spend on bookkeeping rather than on work.

**It holds operational state and nothing else**: a run identifier, company keys
with their status, and timestamps. No credentials, no company names, no URLs, no
job data. Everything of substance lives in the spreadsheet; this file only
records how far the run got, and deleting it costs nothing but a repeat.

**Writes are atomic.** The file is written beside itself and swapped into place,
so a run killed mid-write finds an intact checkpoint rather than a truncated
one — which would be a worse failure than having no checkpoint at all.

**It is archived on success**, not deleted, so a completed run leaves evidence
of what it did without the next run mistaking it for work in progress.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Final, Iterable, List, Mapping, Optional, Sequence, Set

from loguru import logger

from utils.clock import iso, utc_now, week_of

__all__ = [
    "DEFAULT_CHECKPOINT_PATH",
    "STATUS_DONE",
    "STATUS_FAILED",
    "Checkpoint",
]

#: Project root, so the default path resolves from any working directory.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: Where the checkpoint lives. Under ``state/``, which ``.gitignore`` excludes.
DEFAULT_CHECKPOINT_PATH: Final[Path] = PROJECT_ROOT / "state" / "checkpoint.json"

#: Where a completed run's checkpoint is moved.
ARCHIVE_DIRECTORY: Final[str] = "completed"

#: A company was read successfully — including a board that proved empty.
STATUS_DONE: Final[str] = "done"

#: A company was attempted and could not be read.
STATUS_FAILED: Final[str] = "failed"

#: Version of the file format, so a future change can be detected rather than
#: misread.
FORMAT_VERSION: Final[int] = 1


@dataclass
class Checkpoint:
    """How far a run has got.

    Attributes:
        run_id: The run this belongs to.
        started_at: When the run began.
        updated_at: When the checkpoint was last written.
        week_start: The ISO week the run belongs to, so a stale checkpoint from
            a previous week is recognised rather than resumed.
        total: How many companies the run intends to crawl.
        companies: Company key to status — ``"done"`` or ``"failed"``.
        path: Where this checkpoint is stored.
        recorded_now: Companies recorded by *this* process, as opposed to ones
            loaded from a checkpoint an earlier attempt left behind. Not
            persisted: a fresh process has recorded nothing yet, which is
            exactly what it needs to know.
        durable: Whether the run that wrote this checkpoint persisted each
            batch's postings *before* recording its companies. Only such a
            checkpoint can be resumed: one written by a run that held its
            results in memory marks companies done whose postings were never
            stored anywhere, and resuming it would skip them forever. Absent
            from a checkpoint written before this guarantee existed, which is
            precisely the case it has to catch.
    """

    run_id: str
    started_at: str = ""
    updated_at: str = ""
    week_start: str = ""
    total: int = 0
    companies: Dict[str, str] = field(default_factory=dict)
    path: Path = DEFAULT_CHECKPOINT_PATH
    recorded_now: Set[str] = field(default_factory=set)
    durable: bool = False

    # -- state ---------------------------------------------------------------

    @property
    def completed(self) -> int:
        """How many companies have been dealt with.

        Returns:
            The count, successes and failures alike.
        """
        return len(self.companies)

    @property
    def crawled_keys(self) -> Set[str]:
        """Companies that were **successfully read**.

        This is what :func:`crawler.weekly_diff.compare` needs, and it is
        deliberately narrower than every company attempted: a company that was
        blocked has been dealt with, but nothing was learned about its
        postings, so its jobs must not be closed.

        Returns:
            Their company keys.
        """
        return {key for key, status in self.companies.items() if status == STATUS_DONE}

    @property
    def crawled_this_session(self) -> Set[str]:
        """Companies **this process** read successfully.

        Narrower than :attr:`crawled_keys`, and the difference is the whole
        point. A resumed run does not re-crawl what an earlier attempt already
        finished, so it never observes those companies' postings — yet their
        keys are still in the loaded checkpoint. Handing that wider set to
        :func:`crawler.weekly_diff.compare` tells it "these companies were read
        and their jobs are gone", closing every posting the first segment
        found.

        A company is only ever compared against by the segment that actually
        crawled it. Across an interrupted run and its resume, each segment
        closes for its own companies, and the union is what one uninterrupted
        run would have closed.

        Returns:
            Their company keys.
        """
        return {key for key in self.recorded_now if self.companies.get(key) == STATUS_DONE}

    @property
    def failed_keys(self) -> Set[str]:
        """Companies that were attempted and could not be read.

        Returns:
            Their company keys.
        """
        return {key for key, status in self.companies.items() if status != STATUS_DONE}

    def remaining(self, company_keys: Iterable[str]) -> List[str]:
        """Which companies a resumed run still has to crawl.

        Args:
            company_keys: Every company the run intends to cover, in order.

        Returns:
            Those not yet recorded, in the order given.
        """
        return [key for key in company_keys if key not in self.companies]

    def is_for_week(self, week_start: Optional[str] = None) -> bool:
        """Whether this checkpoint belongs to the current week.

        Args:
            week_start: The week to compare against. Defaults to this week.

        Returns:
            ``True`` when it is current. A checkpoint from a fortnight ago is
            not resumable: the boards have moved on, and finishing that crawl
            would file stale postings under this week's comparison.
        """
        return self.week_start == (week_start or week_of()[0])

    # -- recording -----------------------------------------------------------

    def record(self, company_key: str, status: str = STATUS_DONE) -> None:
        """Note that one company has been dealt with.

        Args:
            company_key: The company.
            status: ``"done"`` if it was read, ``"failed"`` otherwise.
        """
        if company_key:
            self.companies[company_key] = status
            self.recorded_now.add(company_key)

    def record_many(self, outcomes: Mapping[str, str]) -> None:
        """Note a whole batch at once.

        Args:
            outcomes: Company key to status.
        """
        for company_key, status in outcomes.items():
            self.record(company_key, status)

    # -- persistence ---------------------------------------------------------

    def to_dict(self) -> Dict[str, object]:
        """Render as the JSON the file holds.

        Returns:
            The checkpoint as plain data. Operational state only: no
            credentials, no company names, no URLs, no job data.
        """
        return {
            "format": FORMAT_VERSION,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "week_start": self.week_start,
            "total": self.total,
            "completed": self.completed,
            "durable": bool(self.durable),
            "companies": dict(self.companies),
        }

    def save(self) -> Path:
        """Write the checkpoint, atomically.

        Returns:
            The path written.

        Raises:
            OSError: If it cannot be written at all. The caller decides whether
                that should stop the run; losing a checkpoint costs a repeat,
                not the results.
        """
        self.updated_at = iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Written beside the target and swapped in, so a process killed during
        # the write leaves the previous checkpoint intact rather than a
        # half-written file that cannot be parsed.
        temporary = self.path.with_name(f"{self.path.stem}.partial{self.path.suffix}")

        try:
            temporary.write_text(
                json.dumps(self.to_dict(), indent=2), encoding="utf-8"
            )
            os.replace(temporary, self.path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

        return self.path

    def archive(self) -> Optional[Path]:
        """Move a completed run's checkpoint out of the way.

        Archived rather than deleted, so a finished run leaves evidence of what
        it covered — but out of the active path, so the next run does not
        mistake it for work in progress.

        Returns:
            Where it was moved, or ``None`` if there was nothing to move.
        """
        if not self.path.is_file():
            return None

        destination = self.path.parent / ARCHIVE_DIRECTORY / f"{self.run_id}.json"

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.path, destination)
        except OSError as exc:
            logger.warning("Could not archive the checkpoint to {}: {}", destination, exc)
            return None

        logger.info("Archived the checkpoint to {}", destination)
        return destination

    def discard(self) -> None:
        """Delete the checkpoint without archiving it."""
        self.path.unlink(missing_ok=True)

    # -- construction --------------------------------------------------------

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CHECKPOINT_PATH) -> Optional["Checkpoint"]:
        """Read an existing checkpoint.

        Args:
            path: Where to look.

        Returns:
            The checkpoint, or ``None`` when there is none or it cannot be
            read. A corrupt checkpoint is never fatal: the run starts over,
            which costs time and nothing else.
        """
        source = Path(path)
        if not source.is_file():
            return None

        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring an unreadable checkpoint at {}: {}", source, exc)
            return None

        if not isinstance(payload, dict) or not payload.get("run_id"):
            logger.warning("Ignoring a checkpoint at {} with no run id", source)
            return None

        stored = payload.get("companies")
        companies = (
            {str(key): str(value) for key, value in stored.items()}
            if isinstance(stored, dict)
            else {}
        )

        return cls(
            run_id=str(payload.get("run_id")),
            started_at=str(payload.get("started_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            week_start=str(payload.get("week_start") or ""),
            total=int(payload.get("total") or 0),
            companies=companies,
            path=source,
            durable=bool(payload.get("durable", False)),
        )

    @classmethod
    def start(
        cls,
        run_id: str,
        total: int = 0,
        path: Path | str = DEFAULT_CHECKPOINT_PATH,
    ) -> "Checkpoint":
        """Begin a fresh checkpoint.

        Args:
            run_id: The run it belongs to.
            total: How many companies the run intends to crawl.
            path: Where to store it.

        Returns:
            The checkpoint, not yet written.
        """
        started = utc_now()
        return cls(
            run_id=run_id,
            started_at=iso(started),
            updated_at=iso(started),
            week_start=week_of(started)[0],
            total=total,
            path=Path(path),
        )

    @classmethod
    def resume_or_start(
        cls,
        run_id: str,
        total: int = 0,
        path: Path | str = DEFAULT_CHECKPOINT_PATH,
        resume: bool = True,
    ) -> "Checkpoint":
        """Continue this week's unfinished run, or begin a new one.

        Args:
            run_id: The identifier to use if a new checkpoint is started. An
                existing checkpoint keeps its own.
            total: How many companies the run intends to crawl.
            path: Where the checkpoint lives.
            resume: Whether to look for one at all.

        Returns:
            The checkpoint to use.
        """
        if not resume:
            return cls.start(run_id, total, path)

        existing = cls.load(path)

        if existing is None:
            return cls.start(run_id, total, path)

        if not existing.is_for_week():
            logger.warning(
                "Checkpoint at {} is from week {}, not this one; starting fresh",
                path,
                existing.week_start or "unknown",
            )
            return cls.start(run_id, total, path)

        if not existing.durable:
            # Written by a run that accumulated its postings in memory and
            # wrote them only at the end. Its companies are marked done, but
            # nothing was stored for them: resuming would skip exactly the
            # companies whose results were lost. The file is left on disk for
            # inspection — starting fresh does not delete it — and every
            # company is crawled again.
            logger.warning(
                "Checkpoint at {} records {} company(ies) from run {}, but was written "
                "before results were persisted per batch, so those postings were never "
                "stored. Starting fresh and crawling them again; the file is left in "
                "place.",
                path,
                existing.completed,
                existing.run_id,
            )
            return cls.start(run_id, total, path)

        logger.success(
            "Resuming run {} — {} of {} company(ies) already done",
            existing.run_id,
            existing.completed,
            existing.total or total,
        )
        if total:
            existing.total = total
        return existing
