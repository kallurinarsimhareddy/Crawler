"""Finding the next pages of a careers listing.

Detected, in order of trust:

* ``rel="next"`` on a ``<link>`` or ``<a>``;
* a *next* control: "Next", "Next page", "›", "»", "→", or ``aria-label`` containing "next";
* numbered pagination: links whose text is a page number, inside a pagination
  container or sharing the current page's query parameter (``page=``, ``p=``…);
* cursor pagination: a next link carrying ``cursor=``, ``after=``, ``pageToken=``…;
* a *Load more* / *Show more* **link** with a real ``href`` or a ``data-url`` /
  ``data-href`` / ``data-next`` attribute — followed over HTTP;
* a *Load more* **button** with no URL, or an infinite-scroll listing — only a
  browser can follow those (:attr:`Pagination.needs_browser`).

Only same-site URLs (or the same ATS board) are returned, never the current page,
and never ``javascript:`` links. The crawler de-duplicates and bounds the rest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple
from urllib.parse import parse_qsl, urljoin, urlsplit

__all__ = ["Pagination", "find_pagination"]

_NEXT_TEXT = re.compile(r"^(?:next(?:\s+page)?|more\s+results|older(?:\s+posts)?|›|»|→|>|>>|next\s*[›»→>])$", re.I)
_LOAD_MORE_TEXT = re.compile(r"\b(?:load|show|view|see)\s+more\b|\bmore\s+(?:jobs|results|openings|positions|roles)\b",
                             re.I)
_PAGE_PARAMS = ("page", "p", "pg", "pagenum", "page_number", "pagenumber", "currentpage", "start", "offset", "from",
                "startrow", "folderoffset")
_CURSOR_PARAMS = ("cursor", "after", "pagetoken", "page_token", "next_token", "nexttoken", "continuation")
_PAGINATION_CONTAINER = re.compile(r"pagination|pager|paging|page-numbers|pagenav", re.I)
_SCROLL_HINT = re.compile(r"infinite[-_ ]?scroll|data-infinite|IntersectionObserver.{0,200}(?:jobs|results|load)",
                          re.I | re.S)


@dataclass
class Pagination:
    #: ``(url, kind, page_no)`` — kind is next | numbered | cursor | load_more
    links: List[Tuple[str, str, Optional[int]]] = field(default_factory=list)
    load_more_button: bool = False
    infinite_scroll: bool = False

    @property
    def needs_browser(self) -> bool:
        return (self.load_more_button or self.infinite_scroll) and not self.links


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _same_site(a: str, b: str) -> bool:
    ha, hb = _host(a), _host(b)
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def _page_param(url: str) -> Optional[Tuple[str, str]]:
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if key.lower() in _PAGE_PARAMS:
            return key.lower(), value
    return None


def _in_pagination(tag: Any) -> bool:
    node = tag
    for _ in range(5):
        node = node.parent
        if node is None or node.name in ("body", "html"):
            return False
        label = " ".join(node.get("class") or []) + " " + str(node.get("id") or "") + " " + \
            str(node.get("aria-label") or "") + " " + str(node.get("role") or "")
        if _PAGINATION_CONTAINER.search(label) or (node.name == "nav" and "page" in label.lower()):
            return True
    return False


def find_pagination(soup: Any, url: str, html: str = "") -> Pagination:
    out = Pagination()
    seen = {url.split("#")[0].rstrip("/")}
    current_param = _page_param(url)

    def add(href: Optional[str], kind: str, page_no: Optional[int] = None) -> None:
        if not href:
            return
        href = href.strip()
        if href.lower().startswith(("javascript:", "mailto:", "tel:")) or href.startswith("#"):
            return
        absolute = urljoin(url, href).split("#")[0]
        if not absolute.startswith("http") or not _same_site(absolute, url):
            return
        key = absolute.rstrip("/")
        if key in seen:
            return
        seen.add(key)
        out.links.append((absolute, kind, page_no))

    for tag in soup.find_all(["link", "a"], rel=True):
        rel = tag.get("rel")
        rels = [r.lower() for r in (rel if isinstance(rel, list) else [rel])]
        if "next" in rels:
            add(tag.get("href"), "cursor" if any(p in (tag.get("href") or "").lower() for p in _CURSOR_PARAMS)
                else "next")
    for anchor in soup.find_all("a"):
        href = anchor.get("href") or anchor.get("data-url") or anchor.get("data-href") or anchor.get("data-next")
        text = re.sub(r"\s+", " ", anchor.get_text(" ")).strip()
        label = str(anchor.get("aria-label") or anchor.get("title") or "")
        lowered_href = (href or "").lower()
        if _NEXT_TEXT.match(text) or re.search(r"\bnext\b", label, re.I):
            add(href, "cursor" if any(p + "=" in lowered_href for p in _CURSOR_PARAMS) else "next")
        elif _LOAD_MORE_TEXT.search(text) and len(text) < 40:
            add(href, "load_more")
        elif text.isdigit() and 1 < int(text) < 1000 and href:
            param = _page_param(urljoin(url, href))
            if _in_pagination(anchor) or (param and (current_param is None or param[0] == current_param[0])):
                add(href, "numbered", int(text))
    for button in soup.find_all(["button", "div", "span"]):
        text = re.sub(r"\s+", " ", button.get_text(" ")).strip()
        if not text or len(text) > 40 or not _LOAD_MORE_TEXT.search(text):
            continue
        target = button.get("data-url") or button.get("data-href") or button.get("data-next")
        if target:
            add(target, "load_more")
        elif button.name == "button" or button.get("role") == "button":
            out.load_more_button = True
    out.infinite_scroll = bool(_SCROLL_HINT.search(html or ""))
    # Numbered links in page order; "next" first so a simple chain is followed in order.
    order = {"next": 0, "cursor": 1, "load_more": 2, "numbered": 3}
    out.links.sort(key=lambda link: (order[link[1]], link[2] or 0))
    return out
