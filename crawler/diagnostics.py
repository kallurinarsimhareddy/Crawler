"""Record what an unreadable career page actually looked like.

When a board defeats every adapter, the run's only output is a one-line error —
which is enough to count the failure and useless for fixing it. Writing the
next adapter needs the evidence: the markup that was served, what the page
looked like once rendered, which endpoints its JavaScript called, and what the
server said in its headers.

This module captures that evidence into ``output/unknown_platforms/``, one
directory per company::

    output/unknown_platforms/
        index.csv                  every company recorded, for triage
        acme-corporation/
            report.md              what was tried and what was found
            page.html              the markup as served
            rendered.html          the DOM after JavaScript ran
            screenshot.png         what a visitor would see
            network.json           requests the page made, and their responses
            headers.json           response headers of the document

The capture is bounded — a run cap, a per-file size cap, and a browser visit
only when the run already allows one — so a sheet full of dead links cannot
fill a disk or dominate a crawl.

Nothing here raises. Diagnostics are an aid, and failing to write them must
never turn a recorded failure into a crashed run.
"""

from __future__ import annotations

import csv
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Final, List, Optional

from loguru import logger

from config.settings import SETTINGS
from utils.discovery import script_endpoints

__all__ = ["Diagnostic", "record_unknown", "reset"]

#: Columns of the triage index.
INDEX_COLUMNS: Final[tuple] = (
    "Company",
    "Platform",
    "URL",
    "Failure Reason",
    "HTTP Status",
    "Rendered",
    "JSON Payloads Seen",
    "Evidence Directory",
)

#: Largest markup file written, in bytes. Enough to read the head, the scripts
#: and the first screen of body; anything beyond that is not diagnostic.
MAX_MARKUP_BYTES: Final[int] = 1_500_000

#: Non-filename characters in a company name.
_UNSAFE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: Guards the run counter and the index file, both shared across workers.
_LOCK: Final[threading.Lock] = threading.Lock()

#: How many diagnostics this run has written, against
#: :attr:`config.settings.Settings.diagnostics_limit`.
_WRITTEN: List[int] = [0]


@dataclass(frozen=True)
class Diagnostic:
    """Where one company's evidence was written.

    Attributes:
        company: Company as named in the input sheet.
        directory: Directory holding the evidence files.
        files: Names of the files actually written.
    """

    company: str
    directory: Path
    files: List[str]


def reset() -> None:
    """Forget how many diagnostics have been written.

    Called at the start of a run so the per-run cap applies per run rather than
    per process, which matters to the tests and to any caller that crawls twice.
    """
    with _LOCK:
        _WRITTEN[0] = 0


def _slug(company: str, fallback: str = "unnamed") -> str:
    """Turn a company name into a directory name.

    Args:
        company: Company as named in the input sheet.
        fallback: Name to use when nothing usable survives.

    Returns:
        A lowercase, hyphenated, filesystem-safe name.
    """
    cleaned = _UNSAFE.sub("-", str(company or "").lower()).strip("-")
    return (cleaned or fallback)[:80]


def _write(path: Path, content: str, limit: int = MAX_MARKUP_BYTES) -> bool:
    """Write one evidence file, truncating it if need be.

    Args:
        path: Destination.
        content: What to write.
        limit: Ceiling in characters.

    Returns:
        ``True`` if something was written.
    """
    if not content:
        return False

    try:
        path.write_text(content[:limit], encoding="utf-8", errors="replace")
        return True
    except OSError as exc:
        logger.debug("Diagnostics: could not write {}: {}", path, exc)
        return False


def _write_json(path: Path, payload: Any) -> bool:
    """Write one evidence file as JSON.

    Args:
        path: Destination.
        payload: Any JSON-serialisable value.

    Returns:
        ``True`` if something was written.
    """
    try:
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.debug("Diagnostics: could not write {}: {}", path, exc)
        return False


def _append_index(row: Dict[str, str]) -> None:
    """Add one line to the triage index, creating it with a header if new.

    Args:
        row: Values keyed by :data:`INDEX_COLUMNS`.
    """
    index = SETTINGS.diagnostics_dir / "index.csv"

    try:
        is_new = not index.exists()
        with index.open("a", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(INDEX_COLUMNS))
            if is_new:
                writer.writeheader()
            writer.writerow(row)
    except OSError as exc:
        logger.debug("Diagnostics: could not update the index: {}", exc)


def _report(
    company: str, url: str, platform: str, error: str, page: Any, endpoints: List[str], bundles: List[str]
) -> str:
    """Compose the human-readable summary that fronts the evidence.

    Args:
        company: Company as named in the input sheet.
        url: The page that could not be read.
        platform: Platform detected, if any.
        error: Why the crawl produced nothing.
        page: The rendered page, or ``None`` if none was captured.
        endpoints: API-looking paths found in the page's scripts.
        bundles: JavaScript bundle URLs the page loads.

    Returns:
        The report, as Markdown.
    """
    lines: List[str] = [
        f"# {company}",
        "",
        f"- **URL:** {url}",
        f"- **Detected platform:** {platform or 'none'}",
        f"- **Outcome:** {error or 'no postings found'}",
    ]

    if page is not None:
        lines += [
            f"- **Rendered:** yes (HTTP {page.status})",
            f"- **Rendered title:** {page.title or '(none)'}",
            f"- **JSON responses captured:** {len(page.payloads)}",
            f"- **Requests observed:** {len(page.requests)}",
        ]
    else:
        lines.append("- **Rendered:** no (browser unavailable or disabled)")

    if endpoints:
        lines += ["", "## API-looking paths in the page's scripts", ""]
        lines += [f"- `{path}`" for path in endpoints[:30]]

    if bundles:
        lines += ["", "## JavaScript bundles", ""]
        lines += [f"- {bundle}" for bundle in bundles[:20]]

    lines += [
        "",
        "## How to use this",
        "",
        "1. Open `rendered.html` (or `page.html`) and find one job title in it.",
        "2. Open `network.json` and look for a response that contains that title —",
        "   that request is the board's own API and is what a new adapter should call.",
        "3. If no response contains it, the listings are in the markup: find the link",
        "   shape the postings share and add it as a `job_url_pattern`.",
        "",
    ]
    return "\n".join(lines)


def record_unknown(
    company: str,
    url: str,
    platform: str = "",
    error: str = "",
    markup: str = "",
    status: int = 0,
    render: Optional[bool] = None,
) -> Optional[Diagnostic]:
    """Capture the evidence needed to write an adapter for an unreadable board.

    Args:
        company: Company as named in the input sheet.
        url: The page that could not be read.
        platform: Platform detected, if any.
        error: Why the crawl produced nothing.
        markup: The markup as served, when the caller already has it.
        status: HTTP status of that fetch, if known.
        render: Whether to visit the page in a browser for a rendered DOM,
            screenshot and network log. Defaults to whether the run allows the
            browser at all.

    Returns:
        Where the evidence was written, or ``None`` when diagnostics are off,
        the run's cap is reached, or nothing could be written.
    """
    if not SETTINGS.diagnostics:
        return None

    with _LOCK:
        if SETTINGS.diagnostics_limit and _WRITTEN[0] >= SETTINGS.diagnostics_limit:
            return None
        _WRITTEN[0] += 1

    directory = SETTINGS.diagnostics_dir / _slug(company)

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("Diagnostics: could not create {}: {}", directory, exc)
        return None

    written: List[str] = []
    page = None

    if _write(directory / "page.html", markup):
        written.append("page.html")

    should_render = SETTINGS.browser_fallback if render is None else render
    if should_render:
        # Imported here so a machine without Playwright pays nothing.
        from utils.browser import render as render_page

        page = render_page(url, screenshot_path=str(directory / "screenshot.png"))

    if page is not None:
        if _write(directory / "rendered.html", page.html):
            written.append("rendered.html")
        if page.screenshot:
            written.append("screenshot.png")
        if _write_json(directory / "headers.json", page.headers):
            written.append("headers.json")
        if _write_json(
            directory / "network.json",
            {"requests": page.requests, "json_responses": page.payloads[:40]},
        ):
            written.append("network.json")
        status = status or page.status

    source = (page.html if page is not None and page.html else "") or markup
    bundles, endpoints = script_endpoints(source, url)
    if _write_json(directory / "endpoints.json", {"bundles": bundles, "paths": endpoints}):
        written.append("endpoints.json")

    if _write(directory / "report.md", _report(company, url, platform, error, page, endpoints, bundles)):
        written.append("report.md")

    _append_index(
        {
            "Company": company,
            "Platform": platform,
            "URL": url,
            "Failure Reason": error,
            "HTTP Status": str(status or ""),
            "Rendered": "yes" if page is not None and page.ok else "no",
            "JSON Payloads Seen": str(len(page.payloads)) if page is not None else "0",
            "Evidence Directory": str(directory),
        }
    )

    logger.info("Diagnostics: wrote {} file(s) for {!r} to {}", len(written), company, directory)
    return Diagnostic(company=company, directory=directory, files=written)
