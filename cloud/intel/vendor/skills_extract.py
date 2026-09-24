# VENDORED from cl-crawl-job-board-crawler linkedin_jobs_mvp/src/skills_extract.py (not a git repo) on 2026-09-24 (pure logic, no network). Keep behaviour identical to the original.
"""
Extract technical skills from job descriptions and related text (all portals).
"""

from __future__ import annotations

import functools
import logging
import re
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1024)
def _skill_token_re(skill: str) -> re.Pattern[str]:
    # Left boundary excludes +/#/. so "asp.net" doesn't match ".net"; right
    # boundary excludes letters/digits/+/# (but NOT ".") so "java" doesn't match
    # "javascript" while "node" still matches "node.js", and "c#"/".net" match.
    return re.compile(r"(?<![a-z0-9+#.])" + re.escape(skill) + r"(?![a-z0-9+#])")


def skill_in_text(skill: str, text_lower: str) -> bool:
    """Boundary-aware skill membership used by the simple per-adapter keyword
    lists. Avoids substring false positives ("java" in "javascript", "react" in
    "reactor") while keeping "c#", ".net", and "node" (in "node.js")."""
    return bool(_skill_token_re(skill.lower()).search(text_lower))

_SKILLS_CONFIG_PATH = Path(__file__).resolve().parent / "data" / "skills.txt"


def _load_skill_phrases(
    path: Path = _SKILLS_CONFIG_PATH,
) -> tuple[list[tuple[str, str]], set[str]]:
    """Parse config/skills.txt into (phrases ordered list, boundary phrase set)."""
    phrases: list[tuple[str, str]] = []
    boundary: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read %s (%s); skills extraction will return nothing.", path, exc)
        return phrases, boundary

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            logger.warning("Skipping malformed skill line %s:%s: %r", path.name, lineno, raw)
            continue
        phrase, label = parts[0].lower(), parts[1]
        phrases.append((phrase, label))
        if len(parts) >= 3 and parts[2].lower() == "boundary":
            boundary.add(phrase)
    return phrases, boundary


_SKILL_PHRASES, _BOUNDARY_SKILLS = _load_skill_phrases()


def _normalize_skill_list(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        s = (item or "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def extract_skills_from_text(text: str, *, extra_labels: Iterable[str] | None = None) -> list[str]:
    """Match known skill phrases and section bullets in free-form job text."""
    if not text:
        return _normalize_skill_list(extra_labels or [])

    low = text.lower()
    found: list[str] = []

    for phrase, label in _SKILL_PHRASES:
        if phrase in _BOUNDARY_SKILLS:
            if re.search(rf"\b{re.escape(phrase)}\b", low):
                found.append(label)
        elif phrase in low:
            found.append(label)

    found.extend(_skills_from_sections(text))
    if extra_labels:
        found.extend(extra_labels)
    return _normalize_skill_list(found)


def _skills_from_sections(text: str) -> list[str]:
    """Pull comma/bullet lists under Skills / Requirements style headers."""
    out: list[str] = []
    header = re.compile(
        r"(?:^|\n)\s*"
        r"(?:"
        r"(?:required\s+)?(?:technical\s+)?skills?(?:\s*(?:&|and)\s*qualifications)?"
        r"|qualifications?"
        r"|requirements?"
        r"|must\s+have"
        r"|nice\s+to\s+have"
        r"|what\s+you.ll\s+bring"
        r")\s*[:\-]?\s*",
        re.I,
    )
    for m in header.finditer(text):
        chunk = text[m.end() : m.end() + 2500]
        stop = re.search(r"\n\s*\n|\n\s*(?:about|responsibilities|benefits|equal opportunity)\b", chunk, re.I)
        if stop:
            chunk = chunk[: stop.start()]
        for part in re.split(r"[\n•●▪·;]|,(?=\s*[A-Za-z])", chunk):
            token = part.strip(" \t-–—•")
            if not token or len(token) > 48:
                continue
            if len(token) < 2:
                continue
            low_t = token.lower()
            if any(p in low_t for p in ("year", "experience", "degree", "bachelor", "master")):
                continue
            # Re-run phrase match on short tokens
            sub = extract_skills_from_text(token)
            if sub:
                out.extend(sub)
            elif 2 <= len(token.split()) <= 4 and token[0].isupper():
                out.append(token)
    return out


def merge_skill_fields(*parts: str | list[str] | None) -> str | None:
    """Merge multiple skill strings/lists into one comma-separated DB field."""
    combined: list[str] = []
    for part in parts:
        if not part:
            continue
        if isinstance(part, list):
            combined.extend(str(p).strip() for p in part if p)
            continue
        for piece in re.split(r"[,;|]", str(part)):
            piece = piece.strip()
            if piece:
                combined.append(piece)
    merged = _normalize_skill_list(combined)
    return ", ".join(merged) if merged else None
