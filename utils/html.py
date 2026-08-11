"""HTML parsing helpers shared by the adapters that have no JSON API.

Covers the three things every HTML-scraping adapter needs: a parser that never
raises on malformed markup, structured-data extraction (JSON-LD and microdata
``JobPosting``), and the small text/URL cleaning routines that would otherwise
be copied into every adapter.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Final, Iterator, List, Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from loguru import logger

__all__ = [
    "absolute_url",
    "clean_text",
    "job_postings_from_microdata",
    "json_ld_objects",
    "json_ld_job_postings",
    "parse_html",
    "same_site",
]

#: Parser preference. lxml is fast and lenient; html.parser is the stdlib
#: fallback so the crawler still runs if lxml is unavailable.
_PARSERS: Final[tuple] = ("lxml", "html.parser")

#: Collapses the runs of whitespace that pretty-printed markup leaves behind.
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")


def parse_html(markup: str) -> BeautifulSoup:
    """Parse markup into a soup, tolerating anything.

    Args:
        markup: Raw HTML.

    Returns:
        The parsed document. An empty document if ``markup`` is empty or no
        parser could read it — callers then simply find nothing.
    """
    if not markup:
        return BeautifulSoup("", "html.parser")

    for parser in _PARSERS:
        try:
            return BeautifulSoup(markup, parser)
        except Exception as exc:  # noqa: BLE001 - fall through to the next parser
            logger.debug("HTML parser {!r} failed ({}), trying the next", parser, exc)

    return BeautifulSoup("", "html.parser")


def clean_text(value: Any) -> str:
    """Reduce a node or string to a single line of readable text.

    Args:
        value: A BeautifulSoup node, a string, or ``None``.

    Returns:
        The text with whitespace collapsed and non-breaking spaces normalised.
    """
    if value is None:
        return ""

    text = value.get_text(" ", strip=True) if isinstance(value, Tag) else str(value)
    return _WHITESPACE.sub(" ", text.replace("\xa0", " ")).strip()


def absolute_url(base: str, href: Optional[str]) -> str:
    """Resolve a link against the page it was found on.

    Args:
        base: URL of the page containing the link.
        href: The link, absolute or relative.

    Returns:
        The absolute URL, or ``""`` for an empty link or one that points
        nowhere useful (``#``, ``javascript:``, ``mailto:``).
    """
    if not href:
        return ""

    link = str(href).strip()
    if not link or link.startswith("#"):
        return ""

    lowered = link.lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:")):
        return ""

    try:
        return urljoin(base, link)
    except ValueError:
        return ""


def same_site(first: str, second: str) -> bool:
    """Report whether two URLs share a registrable-ish host.

    Compares the last two labels of each hostname, so ``careers.acme.com`` and
    ``www.acme.com`` match while ``acme.com`` and ``example.com`` do not.

    Args:
        first: A URL.
        second: A URL.

    Returns:
        ``True`` when both hosts look like the same site.
    """
    try:
        left = (urlsplit(first).hostname or "").lower().split(".")[-2:]
        right = (urlsplit(second).hostname or "").lower().split(".")[-2:]
    except ValueError:
        return False

    return bool(left) and left == right


def _walk_json(node: Any) -> Iterator[Dict[str, Any]]:
    """Yield every dictionary nested anywhere inside a decoded JSON value.

    Args:
        node: Any decoded JSON value.

    Yields:
        Each dictionary found, outermost first.
    """
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_json(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_json(value)


def json_ld_objects(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """Decode every JSON-LD block on the page.

    Handles the shapes publishers actually emit: a single object, an array of
    objects, and ``@graph`` wrappers. Malformed blocks are skipped, since one
    bad script tag must not cost the whole page.

    Args:
        soup: The parsed document.

    Returns:
        Every JSON object found across all JSON-LD blocks, flattened.
    """
    objects: List[Dict[str, Any]] = []

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue

        try:
            decoded = json.loads(raw)
        except ValueError:
            # Some sites emit JS-escaped or trailing-comma JSON; not worth
            # repairing, and other extraction routes still apply.
            logger.debug("Skipping malformed JSON-LD block ({} chars)", len(raw))
            continue

        objects.extend(_walk_json(decoded))

    return objects


def _is_job_posting(obj: Dict[str, Any]) -> bool:
    """Report whether a JSON-LD object declares itself a ``JobPosting``.

    Args:
        obj: One decoded JSON-LD object.

    Returns:
        ``True`` if its ``@type`` is or includes ``JobPosting``.
    """
    declared = obj.get("@type") or obj.get("type")
    if isinstance(declared, str):
        return declared.strip().lower() == "jobposting"
    if isinstance(declared, list):
        return any(str(item).strip().lower() == "jobposting" for item in declared)
    return False


def json_ld_job_postings(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """Extract every ``schema.org/JobPosting`` published as JSON-LD.

    Args:
        soup: The parsed document.

    Returns:
        The ``JobPosting`` objects, in document order.
    """
    return [obj for obj in json_ld_objects(soup) if _is_job_posting(obj)]


def job_postings_from_microdata(soup: BeautifulSoup) -> List[Dict[str, str]]:
    """Extract ``schema.org/JobPosting`` published as inline microdata.

    Older career pages annotate the markup itself with ``itemtype`` and
    ``itemprop`` rather than embedding JSON-LD.

    Args:
        soup: The parsed document.

    Returns:
        One flat mapping of ``itemprop`` name to text per posting found. A
        ``url`` key is added from the posting's link when one is present.
    """
    postings: List[Dict[str, str]] = []

    for node in soup.find_all(attrs={"itemtype": True}):
        itemtype = str(node.get("itemtype") or "").lower()
        if "jobposting" not in itemtype:
            continue

        posting: Dict[str, str] = {}
        for prop in node.find_all(attrs={"itemprop": True}):
            name = str(prop.get("itemprop") or "").strip()
            if not name or name in posting:
                continue

            value = (
                prop.get("content")
                or prop.get("href")
                or prop.get("datetime")
                or clean_text(prop)
            )
            posting[name] = clean_text(value)

        link = node.find("a", href=True)
        if link is not None and "url" not in posting:
            posting["url"] = str(link.get("href") or "")

        if posting:
            postings.append(posting)

    return postings
