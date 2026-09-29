r"""Staged update with rollback for an installed SANA GTM.

    <install>\staging\<version>\        the new release, fully extracted before anything changes
    <install>\versions\<old version>\    the release it replaced (kept for rollback; newest 2 kept)
    <install>\runtime app bin manager version.json   the live release
    <install>\config data logs state     NEVER touched by an update

:meth:`Updater.apply` = stage -> swap -> health check -> (rollback on failure)
-> prune. A failed extraction leaves the live release untouched; a failed health
check puts the previous release back exactly as it was. Moves are renames on the
same volume, so a swap is quick and never half-copies a directory.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Callable, List, Optional

__all__ = ["RELEASE_ITEMS", "PRESERVED", "Updater", "UpdateError"]

#: What a release consists of (replaced by an update).
RELEASE_ITEMS = ("runtime", "app", "bin", "manager", "version.json")
#: What an update never touches.
PRESERVED = ("config", "data", "logs", "state")


class UpdateError(RuntimeError):
    pass


def _retry(fn, what: str, attempts: int = 10, delay: float = 1.0):
    for attempt in range(attempts):
        try:
            return fn()
        except OSError:
            if attempt == attempts - 1:
                raise UpdateError(f"{what} is in use; close SANA GTM windows and run Setup again") from None
            time.sleep(delay)


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


class Updater:
    def __init__(self, root: Path, *, keep_versions: int = 2, retry_delay: float = 1.0,
                 report: Optional[Callable[[str], None]] = None):
        self.root = Path(root)
        self.staging_root = self.root / "staging"
        self.versions_root = self.root / "versions"
        self.keep_versions = keep_versions
        self.retry_delay = retry_delay
        self.report = report or (lambda msg: None)

    # --- versions --------------------------------------------------------------------------
    def current_version(self) -> Optional[str]:
        try:
            return json.loads((self.root / "version.json").read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError):
            return None

    def installed_versions(self) -> List[str]:
        """Previous releases kept for rollback, oldest first."""
        if not self.versions_root.exists():
            return []
        dirs = [d for d in self.versions_root.iterdir() if d.is_dir()]
        return [d.name for d in sorted(dirs, key=lambda d: d.stat().st_mtime)]

    # --- steps -----------------------------------------------------------------------------
    def stage(self, payload: Path) -> Path:
        """Extract the release into staging\\<version>. Nothing live changes."""
        with zipfile.ZipFile(payload) as z:
            try:
                version = json.loads(z.read("version.json"))["version"]
            except (KeyError, ValueError):
                raise UpdateError("the update package has no version.json") from None
            for name in z.namelist():
                top = name.replace("\\", "/").split("/", 1)[0]
                if top not in RELEASE_ITEMS or ".." in name.replace("\\", "/").split("/"):
                    raise UpdateError(f"the update package contains an unexpected entry: {name[:80]}")
            target = self.staging_root / version
            _remove(target)
            target.mkdir(parents=True)
            members = z.namelist()
            for i, m in enumerate(members):
                z.extract(m, target)
                if i % 500 == 0 and members:
                    self.report(f"{i * 100 // len(members)}%")
        self.report(f"staged {version}")
        return target

    def swap(self, staged: Path) -> Optional[Path]:
        """Move the live release to versions\\<old> and the staged one into place.
        Returns the backup directory (None on a first install)."""
        old = self.current_version()
        backup = None
        live = [n for n in RELEASE_ITEMS if (self.root / n).exists()]
        if live:
            backup = self.versions_root / (old or f"unknown-{int(time.time())}")
            if backup.exists():
                _remove(backup)
            backup.mkdir(parents=True)
            for name in live:
                _retry(lambda n=name: os.replace(self.root / n, backup / n), str(self.root / name),
                       delay=self.retry_delay)
        for name in RELEASE_ITEMS:
            src = staged / name
            if src.exists():
                _retry(lambda n=name, s=src: os.replace(s, self.root / n), str(self.root / name),
                       delay=self.retry_delay)
        shutil.rmtree(staged, ignore_errors=True)
        return backup

    def rollback(self, backup: Optional[Path]) -> bool:
        """Put the release in ``backup`` back. Config, data and logs are untouched."""
        if backup is None or not backup.exists():
            return False
        for name in RELEASE_ITEMS:
            if (backup / name).exists():
                _retry(lambda n=name: _remove(self.root / n), str(self.root / name), delay=self.retry_delay)
                _retry(lambda n=name: os.replace(backup / n, self.root / n), str(self.root / name),
                       delay=self.retry_delay)
            elif (self.root / name).exists():
                _remove(self.root / name)   # did not exist in the previous release
        shutil.rmtree(backup, ignore_errors=True)
        self.report("rolled back")
        return True

    def prune(self) -> List[str]:
        removed = []
        versions = self.installed_versions()
        for name in versions[:max(0, len(versions) - self.keep_versions)]:
            shutil.rmtree(self.versions_root / name, ignore_errors=True)
            removed.append(name)
        return removed

    # --- the whole update --------------------------------------------------------------------
    def apply(self, payload: Path, health_check: Optional[Callable[[], bool]] = None) -> dict:
        """Stage, swap, check health; roll back when the check fails or raises."""
        before = self.current_version()
        staged = self.stage(payload)
        backup = self.swap(staged)
        healthy, error = True, ""
        if health_check is not None:
            try:
                healthy = bool(health_check())
            except Exception as e:  # noqa: BLE001 - any failure means "not healthy"
                healthy, error = False, f"{type(e).__name__}: {e}"[:300]
        if not healthy:
            rolled = self.rollback(backup)
            return {"ok": False, "from": before, "to": staged.name, "rolled_back": rolled,
                    "version": self.current_version(), "error": error or "health check failed"}
        pruned = self.prune()
        return {"ok": True, "from": before, "to": self.current_version(), "rolled_back": False,
                "backup": str(backup) if backup else None, "pruned": pruned}
