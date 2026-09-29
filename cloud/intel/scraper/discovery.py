"""From a company homepage to its careers page / job board, without crawling the domain.

Candidates come from what the page itself links to, ranked:

====  ================================================================
100   a known ATS board (Greenhouse, Lever, Workday…) the page links to
 80   a same-site link whose text says Careers / Jobs / Join us…
 70   a same-site link whose path is a careers path (/careers, /jobs…)
 40   a common careers path on the same site, *guessed* — only when the page
      links to nothing better, at most :data:`MAX_GUESSES` of them
====  ================================================================

Only the best few are ever fetched (the crawler's ``max_pages`` and the
per-domain request budget still apply), and robots.txt is respected for each.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from cloud.intel.vendor import ats_detect

__all__ = ["CANDIDATE_PATHS", "MAX_GUESSES", "rank_candidates"]

CANDIDATE_PATHS = ("/careers", "/careers/jobs", "/jobs", "/jobs/search", "/work-with-us", "/join-us",
                   "/opportunities", "/open-positions", "/company/careers", "/about/careers", "/about-us/careers")
MAX_GUESSES = 3
_TEXT = re.compile(r"\b(?:careers?|jobs?|join (?:us|our team)|work (?:with|for) us|open (?:positions|roles)|"
                   r"we'?re hiring|we are hiring|job openings|vacancies|opportunities)\b", re.I)
_PATH = re.compile(r"/(?:careers?|jobs?|join(?:-us)?|work-with-us|opportunities|open-positions|vacancies)(?:[/?#.]|$)",
                   re.I)


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def rank_candidates(page_url: str, links: Sequence[Tuple[str, str]], detection: Optional[dict] = None, *,
                    limit: int = 3, guess: bool = True) -> List[Tuple[str, int, str]]:
    """``[(url, score, why)]``, best first, for the careers pages worth fetching from ``page_url``."""
    scores: dict = {}
    home = _host(page_url)
    current = page_url.split("#")[0].rstrip("/")

    def offer(url: str, score: int, why: str) -> None:
        key = url.split("#")[0].rstrip("/")
        if key == current:
            return
        if key not in scores or scores[key][0] < score:
            scores[key] = (score, why, url)

    if detection and detection.get("url"):
        offer(detection["url"], 100, f"{detection.get('platform')} job board")
    for text, href in links:
        host = _host(href)
        same = host == home or host.endswith("." + home) or home.endswith("." + host)
        found = ats_detect.detect(href)
        if found:
            offer(found.get("url") or href, 100, f"{found.get('platform')} job board")
        elif same and _TEXT.search(text or ""):
            offer(href, 80 + (5 if _PATH.search(urlsplit(href).path) else 0), f"link \"{(text or '')[:40]}\"")
        elif same and _PATH.search(urlsplit(href).path):
            offer(href, 70, "careers path")
    if guess and not scores:
        parts = urlsplit(page_url)
        for path in CANDIDATE_PATHS[:MAX_GUESSES]:
            offer(f"{parts.scheme}://{parts.netloc}{path}", 40, f"common careers path {path}")
    ranked = sorted(scores.values(), key=lambda item: -item[0])
    return [(url, score, why) for score, why, url in ranked[:limit]]
