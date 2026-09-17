"""Per-job scratch storage, and the guard that keeps it away from production.

Each attempt of each job gets its own directory::

    <runtime root>/<job_id>/attempt-<n>/
        state/     anything the crawler would otherwise keep in state/
        output/    exports (jobs.xlsx, jobs.csv, summary.json)
        logs/      crawl.log

The runtime root defaults to ``cloud/runtime`` and is refused if it is, or is
inside, CareerCrawler's own ``state/`` or ``output/`` directories — or contains
them. The job id is validated against its strict pattern before it becomes a
path segment, so no input can walk out of the root. When the attempt is done,
:meth:`JobWorkspace.remove` deletes the directory, again only after proving it
is inside the root.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List

__all__ = ["JobWorkspace", "UnsafeRuntimeRootError", "check_runtime_root", "sweep_abandoned_workspaces"]

CLOUD_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = CLOUD_ROOT.parent
DEFAULT_RUNTIME_ROOT = CLOUD_ROOT / "runtime"

#: CareerCrawler's production state. Nothing a cloud job writes may land here.
PROTECTED_DIRECTORIES = (
    REPO_ROOT / "state",
    REPO_ROOT / "output",
    REPO_ROOT / "secrets",
    REPO_ROOT / "input",
)

_JOB_ID = re.compile(r"^job_[0-9a-f]{32}$")
_ATTEMPT = re.compile(r"^attempt-(\d+)$")


class UnsafeRuntimeRootError(ValueError):
    """The runtime root overlaps CareerCrawler's production directories."""


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def check_runtime_root(root: Path, protected: Iterable[Path] = PROTECTED_DIRECTORIES) -> Path:
    resolved = Path(root).resolve()
    for directory in protected:
        if _overlaps(resolved, Path(directory).resolve()):
            raise UnsafeRuntimeRootError(
                f"runtime root {resolved} overlaps CareerCrawler's {directory}; "
                "cloud jobs must never write there"
            )
    if resolved == REPO_ROOT.resolve() or resolved in REPO_ROOT.resolve().parents:
        raise UnsafeRuntimeRootError(f"runtime root {resolved} would contain the whole repository")
    return resolved


@dataclass(frozen=True)
class JobWorkspace:
    root: Path
    path: Path

    @property
    def state(self) -> Path:
        return self.path / "state"

    @property
    def output(self) -> Path:
        return self.path / "output"

    @property
    def logs(self) -> Path:
        return self.path / "logs"

    @classmethod
    def create(cls, runtime_root: Path, job_id: str, attempt: int) -> "JobWorkspace":
        if not _JOB_ID.match(job_id):
            raise ValueError(f"refusing to build a workspace for job id {job_id!r}")
        if attempt < 0:
            raise ValueError("attempt must not be negative")
        root = check_runtime_root(runtime_root)
        path = (root / job_id / f"attempt-{attempt}").resolve()
        if root not in path.parents:  # belt and braces; the regex already guarantees it
            raise ValueError("workspace escapes the runtime root")
        # Anything left by this or an earlier attempt belongs to a worker that died:
        # this worker holds the claim now, so those attempts can never write again.
        if path.parent.is_dir():
            for leftover in path.parent.glob("attempt-*"):
                match = _ATTEMPT.match(leftover.name)
                if match and int(match.group(1)) <= attempt and leftover.is_dir():
                    shutil.rmtree(leftover, ignore_errors=True)
        workspace = cls(root=root, path=path)
        for directory in (workspace.state, workspace.output, workspace.logs):
            directory.mkdir(parents=True, exist_ok=True)
        return workspace

    def remove(self) -> None:
        """Delete this attempt's directory, and the job's directory if now empty."""
        path = self.path.resolve()
        if self.root not in path.parents:
            raise ValueError(f"refusing to delete {path}: not inside {self.root}")
        shutil.rmtree(path, ignore_errors=True)
        parent = path.parent
        try:
            if parent != self.root and self.root in parent.parents and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass


def _newest_mtime(directory: Path) -> float:
    newest = directory.stat().st_mtime
    for item in directory.rglob("*"):
        try:
            newest = max(newest, item.stat().st_mtime)
        except OSError:
            continue
    return newest


def sweep_abandoned_workspaces(
    runtime_root: Path,
    *,
    older_than_seconds: float,
    clock: Callable[[], float] = time.time,
) -> List[str]:
    """Delete job workspaces nothing has written to for ``older_than_seconds``.

    A worker killed mid-job cannot clean up after itself. A running job keeps
    writing its log, so a workspace whose newest file is older than every lease
    has no live owner. Only directories named like a job id are touched.
    """
    root = check_runtime_root(runtime_root)
    if not root.is_dir():
        return []
    cutoff = clock() - older_than_seconds
    removed = []
    for candidate in root.iterdir():
        if not candidate.is_dir() or not _JOB_ID.match(candidate.name):
            continue
        try:
            if _newest_mtime(candidate) < cutoff:
                shutil.rmtree(candidate, ignore_errors=True)
                removed.append(candidate.name)
        except OSError:
            continue
    return removed
