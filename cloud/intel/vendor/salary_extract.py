# VENDORED from cl-crawl-job-board-crawler linkedin_jobs_mvp/src/salary_extract.py (not a git repo) on 2026-09-24 (pure logic, no network). Keep behaviour identical to the original.
"""
Extract compensation from job descriptions — annual and hourly; avoid industry stats.
"""

from __future__ import annotations

import re

# Section headers that usually introduce real pay.
_SALARY_SECTION_RE = re.compile(
    r"(?:what you(?:'|')?ll earn|base pay(?:\s+range)?|salary(?:\s+range)?|"
    r"compensation(?:\s+range)?|pay range|annual salary|target base|"
    r"compensation\s*[:\-]|rate\s*[:\-])",
    re.I,
)

# $170,000 - $235,000 or $170k-$235k
_RANGE_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})+|\d{2,3})\s*(?:k|K)?\s*[-–—to]+\s*\$?\s?(\d{1,3}(?:,\d{3})+|\d{2,3})\s*(?:k|K)?",
    re.I,
)

_HOURLY_BOTH_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})?|\d{1,3})\s*/\s*(?:hr|hour|hours)\s*"
    r"[-–—to]+\s*\$?\s?(\d{1,3}(?:,\d{3})?|\d{1,3})\s*/\s*(?:hr|hour|hours)\b",
    re.I,
)

_HOURLY_RANGE_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})?|\d{1,3})\s*"
    r"[-–—to]+\s*\$?\s?(\d{1,3}(?:,\d{3})?|\d{1,3})\s*(?:/\s*)?(?:hr|hour|hours)\b",
    re.I,
)

_HOURLY_SINGLE_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})?|\d{1,3})\s*(?:/\s*)?(?:hr|hour|hours)\b",
    re.I,
)

_HOURLY_LABELED_RE = re.compile(
    r"(?:compensation|salary|pay|rate|hourly)\s*[:\-]?\s*"
    r"\$?\s?(\d{1,3}(?:,\d{3})?|\d{1,3})\s*"
    r"[-–—to]+\s*\$?\s?(\d{1,3}(?:,\d{3})?|\d{1,3})\s*(?:/\s*)?(?:hr|hour|hours)\b",
    re.I,
)

_SINGLE_RE = re.compile(
    r"(?:base(?:\s+pay)?|salary|compensation|pay)\s*(?:range)?\s*[:\-]?\s*"
    r"\$\s?(\d{1,3}(?:,\d{3})+|\d{2,3})\s*(?:k|K)?",
    re.I,
)

_ANY_DOLLAR_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})+|\d{2,3})(?:\s*(?:k|K))?",
    re.I,
)

_BAD_AFTER_RE = re.compile(r"\s*(billion|million|trillion|industry|revenue|market)\b", re.I)


def _parse_amount(num: str, *, k_suffix: bool = False) -> int | None:
    raw = num.replace(",", "").strip()
    try:
        val = int(raw)
    except ValueError:
        return None
    if k_suffix:
        val *= 1000
    return val


def _format_range(lo: int, hi: int) -> str:
    return f"${lo:,} - ${hi:,}"


def _format_hourly_range(lo: int, hi: int) -> str:
    return f"${lo}/hr - ${hi}/hr"


def _format_hourly_single(amt: int) -> str:
    return f"${amt}/hr"


def _is_plausible_annual(lo: int, hi: int | None = None) -> bool:
    if hi is None:
        return 25_000 <= lo <= 750_000
    return 25_000 <= lo <= 750_000 and 25_000 <= hi <= 750_000 and lo <= hi


def _is_plausible_hourly(lo: int, hi: int | None = None) -> bool:
    if hi is None:
        return 12 <= lo <= 500
    return 12 <= lo <= 500 and 12 <= hi <= 500 and lo <= hi


def extract_salary_from_text(text: str) -> str | None:
    if not text:
        return None

    for sec in _SALARY_SECTION_RE.finditer(text):
        chunk = text[sec.start() : sec.start() + 1200]
        found = _scan_chunk(chunk)
        if found:
            return found

    m = re.search(
        r"base pay range\s*[:\-]\s*(\$\s?[\d,]+(?:\s*[-–—]\s*\$?\s?[\d,]+)?)",
        text,
        re.I,
    )
    if m:
        found = _scan_chunk(m.group(1))
        if found:
            return found

    return _scan_chunk(text)


def _best_comp(
    hourly: list[tuple[int, str]], annual: list[tuple[int, str]]
) -> str | None:
    """Pick the most representative comp figure from a salary section.

    Priority: hourly range → annual range → hourly single → annual single.

    Rationale: a *range* is the listed pay band, while a lone *single* is often
    incidental (e.g. an on-call "$75/hour" mentioned next to an annual range), so
    ranges outrank singles. Within a tier we prefer hourly, because the contract
    roles this crawler targets are usually quoted on an hourly basis. Within a
    bucket the highest figure wins. So "$180,000 - $210,000 … $75/hour on-call"
    returns the annual range, but "$65/hr - $75/hr" (with an annual equivalent)
    keeps the hourly rate.
    """
    hourly_ranges = [h for h in hourly if " - " in h[1]]
    annual_ranges = [a for a in annual if " - " in a[1]]
    for bucket in (hourly_ranges, annual_ranges, hourly, annual):
        if bucket:
            return max(bucket, key=lambda x: x[0])[1]
    return None


def _scan_chunk(chunk: str) -> str | None:
    hourly: list[tuple[int, str]] = []
    annual: list[tuple[int, str]] = []

    for m in _HOURLY_LABELED_RE.finditer(chunk):
        lo = _parse_amount(m.group(1))
        hi = _parse_amount(m.group(2))
        if lo and hi and _is_plausible_hourly(lo, hi):
            hourly.append((lo, _format_hourly_range(lo, hi)))

    for m in _HOURLY_BOTH_RE.finditer(chunk):
        lo = _parse_amount(m.group(1))
        hi = _parse_amount(m.group(2))
        if lo and hi and _is_plausible_hourly(lo, hi):
            hourly.append((lo, _format_hourly_range(lo, hi)))

    for m in _HOURLY_RANGE_RE.finditer(chunk):
        lo = _parse_amount(m.group(1))
        hi = _parse_amount(m.group(2))
        if lo and hi and _is_plausible_hourly(lo, hi):
            hourly.append((lo, _format_hourly_range(lo, hi)))

    for m in _HOURLY_SINGLE_RE.finditer(chunk):
        amt = _parse_amount(m.group(1))
        if amt and _is_plausible_hourly(amt):
            hourly.append((amt, _format_hourly_single(amt)))

    for m in _RANGE_RE.finditer(chunk):
        tail = chunk[m.end() : m.end() + 24]
        if _BAD_AFTER_RE.match(tail):
            continue
        lo_raw, hi_raw = m.group(1), m.group(2)
        k_lo = "k" in m.group(0).lower()
        lo = _parse_amount(lo_raw, k_suffix=k_lo)
        hi = _parse_amount(hi_raw, k_suffix=k_lo)
        if lo and hi and _is_plausible_annual(lo, hi):
            annual.append((lo, _format_range(lo, hi)))

    for m in _SINGLE_RE.finditer(chunk):
        lo = _parse_amount(m.group(1), k_suffix="k" in m.group(0).lower())
        if lo and _is_plausible_annual(lo):
            annual.append((lo, f"${lo:,}"))

    # Last resort: a broad, context-free "$NNN,NNN" catch-all (noisy, so guarded
    # by _BAD_AFTER_RE), used only when no hourly/annual pattern matched at all.
    # Must run BEFORE the final pick: the range-first rework (cc765e0) replaced
    # the old fall-through with a single unconditional return, which left this
    # fallback unreachable and silently dropped salaries only it could find.
    if not hourly and not annual:
        for m in _ANY_DOLLAR_RE.finditer(chunk):
            tail = chunk[m.end() : m.end() + 20]
            if _BAD_AFTER_RE.match(tail):
                continue
            amt = _parse_amount(m.group(1), k_suffix="k" in m.group(0).lower())
            if amt and _is_plausible_annual(amt):
                annual.append((amt, f"${amt:,}"))

    return _best_comp(hourly, annual)
