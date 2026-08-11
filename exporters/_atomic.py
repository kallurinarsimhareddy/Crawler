"""Write an output file without ever destroying the previous one.

Every report this package produces is written the same way, and for the same
two reasons. A crawl costs tens of minutes, so a half-written file must never
replace a good one — hence writing beside the destination and swapping it in.
And on Windows a file open in Excel holds an exclusive lock, so the swap can
fail on a file the operator is simply *looking* at — hence writing to a
timestamped sibling rather than throwing the results away.

That policy is written here once, so the workbook, the failure CSV and the
version 2 reports cannot drift apart on it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Final

from loguru import logger

__all__ = ["fallback_path", "write_atomically"]

#: Suffix given to the file being written, before it is swapped into place.
_PARTIAL: Final[str] = ".partial"


def fallback_path(path: Path, stamp: str) -> Path:
    """Name a sibling file to use when the target cannot be replaced.

    Args:
        path: The blocked destination.
        stamp: A short marker making the name unique.

    Returns:
        The alternative path.
    """
    return path.with_name(f"{path.stem}-{stamp}{path.suffix}")


def write_atomically(
    path: Path,
    write: Callable[[Path], None],
    fallback_when_locked: bool = True,
) -> Path:
    """Write a file through a temporary, then swap it into place.

    Args:
        path: Final destination. Parent directories are created.
        write: Called with the temporary path; must write the whole file.
        fallback_when_locked: Whether to keep the data under a timestamped
            sibling when the destination cannot be replaced. ``False`` re-raises
            instead, for a caller that needs the exact path or nothing.

    Returns:
        The path actually written — the destination, or the sibling if the
        destination was locked.

    Raises:
        OSError: If the file cannot be written at all, or if the destination is
            locked and ``fallback_when_locked`` is ``False``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.stem}{_PARTIAL}{path.suffix}")

    try:
        write(temporary)

        try:
            os.replace(temporary, path)
        except OSError as exc:
            if not fallback_when_locked:
                raise

            # The data is already safely on disk in `temporary`; only the final
            # rename failed. Keep it under a name nothing else holds.
            alternative = fallback_path(path, time.strftime("%Y%m%d-%H%M%S"))
            os.replace(temporary, alternative)
            logger.warning(
                "Could not replace {} ({}); wrote {} instead — close the file and rename it",
                path,
                exc,
                alternative,
            )
            return alternative
    except OSError:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
        logger.error("Could not write {}", path)
        raise

    return path
