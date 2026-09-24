# VENDORED from cl-crawl-job-board-crawler linkedin_jobs_mvp/src/job_classify.py (not a git repo) on 2026-09-24 (pure logic, no network). Keep behaviour identical to the original.
"""
Rule-based job category and experience-years extraction from posting text.
"""

from __future__ import annotations

import re

_MAX_EXP_YEARS = 25

# (regex, canonical label) — order matters for overlapping terms.
_CATEGORY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bcorp[\s-]?to[\s-]?corp\b|\bcorp/\s*corp\b", re.I), "Corp-to-Corp"),
    (re.compile(r"\bc2c\b", re.I), "C2C"),
    (re.compile(r"\b1099\b", re.I), "1099"),
    (re.compile(r"\bw[\s-]?2\b(?!\s*only)", re.I), "W2"),
    (re.compile(r"\bw[\s-]?2\s*only\b", re.I), "W2 Only"),
    (re.compile(r"\bcontract[\s-]?to[\s-]?hire\b|\bc2h\b", re.I), "Contract-to-Hire"),
    (re.compile(r"\bcontract\b", re.I), "Contract"),
    (re.compile(r"\bfull[\s-]?time\b|\bfte\b", re.I), "Full-time"),
    (re.compile(r"\bpart[\s-]?time\b", re.I), "Part-time"),
    (re.compile(r"\bpermanent\b", re.I), "Permanent"),
    (re.compile(r"\btemp(?:orary)?\b|\btemp[\s-]?to[\s-]?hire\b", re.I), "Temporary"),
    (re.compile(r"\binternship\b", re.I), "Internship"),
    (re.compile(r"\bfreelance\b|\bindependent\s+contractor\b", re.I), "Freelance"),
]

_NEGATION_BEFORE_RE = re.compile(
    r"\b(?:not|no|without|excluding|exclude|aren['']?t|isn['']?t|doesn['']?t|"
    r"don['']?t|won['']?t|will not|are not)\b[^.]{0,90}$",
    re.I,
)

_REQ_SECTION_RE = re.compile(
    r"(?:required\s+skills|required\s*:|qualifications?|requirements?|"
    r"what\s+you(?:'|')?ll\s+need|must\s+have|minimum\s+qualifications?|"
    r"experience\s+required|what\s+we(?:'|')?re\s+looking\s+for|ideal\s+candidate|"
    r"you\s+have|required\s+experience|key\s+qualifications?)",
    re.I,
)

_COMPANY_SECTION_RE = re.compile(
    r"(?:why\s+(?:us|idr|\w+)|about\s+(?:us|the\s+company)|our\s+company|who\s+we\s+are|"
    r"company\s+overview|what(?:'|')?s\s+in\s+it\s+for|benefits|perks|"
    r"life\s+at\s+\w+|join\s+our\s+team)",
    re.I,
)

# Company tenure / marketing — not candidate requirements.
_COMPANY_EXP_CONTEXT_RE = re.compile(
    r"(?:proven\s+)?industry\s+experience|years?\s+in\s+(?:business|a\s+row|the\s+industry)|"
    r"years?\s+of\s+service|major\s+markets|staffing\s+industry|"
    r"years?\s+in\s+a\s+row|company(?:'|')?s\s+(?:history|track\s+record)|"
    r"since\s+19\d{2}|founded\s+in|accrue\s+\d+\s+days|every\s+\d+\s+hours\s+worked|"
    r"after\s+(?:five|ten|\d+)\s+years?\s+of\s+service|"
    r"(?:for|over)\s+(?:the\s+)?last\s+\d{1,2}\s+years?|"
    r"\d{1,2}\s+years?\s+of\s+(?:success|growth|service|history|track\s+record)|"
    r"(?:our|the)\s+(?:company|firm|organization|team|business)\b.{0,45}\d{1,2}\s+years?|"
    r"in\s+business\s+for\s+\d{1,2}\s+years?|celebrating\s+\d{1,2}\s+years?",
    re.I,
)

_BENEFIT_TENURE_RE = re.compile(
    r"\b(?:401\s*k|pto|paid\s+time\s+off|vacation|benefits|perks|holiday|sick\s+leave)\b",
    re.I,
)

_CANDIDATE_VERB_BEFORE_YEARS_RE = re.compile(
    r"(?:require|need|must\s+have|minimum|at\s+least|bring|with)\s+.{0,30}\d{1,2}\s*(?:\+|plus)?\s*years?",
    re.I,
)

_CANDIDATE_EXP_CONTEXT_RE = re.compile(
    r"(?:hands[\s-]?on|professional|software|related|relevant|technical|"
    r"engineering|development|data|gcp|sql|python|java|cloud|"
    r"experience\s+with|experience\s+in|skills?)",
    re.I,
)

_EXP_PHRASE_RE = re.compile(
    r"(?<![\d/])(\d{1,2})\s*(?:\+|\+?\s*)?\s*(?:to|–|-)\s*(\d{1,2})\s*(?:years?|yrs?)\b",
    re.I,
)

_EXP_PLUS_RE = re.compile(
    r"(?<![\d/])(\d{1,2})\s*\+\s*(?:years?|yrs?)\b",
    re.I,
)

_EXP_MIN_RE = re.compile(
    r"(?:minimum|min\.?|at\s+least)\s+(\d{1,2})\s*(?:years?|yrs?)\b",
    re.I,
)

_EXP_YEARS_RE = re.compile(
    r"(?<![\d/])(\d{1,2})\s*(?:–|-)\s*(\d{1,2})\s*(?:years?|yrs?)\b",
    re.I,
)

_EXP_SINGLE_RE = re.compile(
    r"(?<![\d/])(\d{1,2})\s*(?:years?|yrs?)\s+of\s+(?:related\s+)?(?:software\s+)?experience\b",
    re.I,
)


def _plausible_years(lo: int, hi: int | None = None) -> bool:
    if hi is None:
        return 0 < lo <= _MAX_EXP_YEARS
    return 0 < lo <= _MAX_EXP_YEARS and 0 < hi <= _MAX_EXP_YEARS and lo <= hi


def _section_starts(text: str, pattern: re.Pattern[str]) -> list[int]:
    return [m.start() for m in pattern.finditer(text)]


def _nearest_section_start(pos: int, starts: list[int]) -> int | None:
    before = [s for s in starts if s <= pos]
    return before[-1] if before else None


def _is_company_experience_match(text: str, start: int, end: int) -> bool:
    req_starts = _section_starts(text, _REQ_SECTION_RE)
    comp_starts = _section_starts(text, _COMPANY_SECTION_RE)
    req_sec = _nearest_section_start(start, req_starts)
    comp_sec = _nearest_section_start(start, comp_starts)

    local = text[max(0, start - 50) : min(len(text), end + 100)]
    before = text[max(0, start - 80) : start]

    if _BENEFIT_TENURE_RE.search(local):
        return True

    if _CANDIDATE_VERB_BEFORE_YEARS_RE.search(before + local):
        return False

    if req_sec is not None and (comp_sec is None or req_sec >= comp_sec):
        if start - req_sec < 2500:
            return False

    if _COMPANY_EXP_CONTEXT_RE.search(local):
        return True

    if comp_sec is not None and start - comp_sec < 1500:
        return True

    if re.search(
        r"\b(?:company|firm|organization|employer|we(?:'|')?ve|our\s+(?:company|team|firm))\b",
        local,
        re.I,
    ) and not _CANDIDATE_EXP_CONTEXT_RE.search(local):
        return True

    if re.search(r"\bwhy\s+\w{2,30}\?", before, re.I):
        return True
    return False


def _score_experience_match(text: str, start: int, end: int) -> int:
    if _is_company_experience_match(text, start, end):
        return -1000

    score = 0
    window = text[max(0, start - 80) : min(len(text), end + 120)]

    req_starts = _section_starts(text, _REQ_SECTION_RE)
    req_sec = _nearest_section_start(start, req_starts)
    if req_sec is not None and start - req_sec < 2500:
        score += 100

    if _CANDIDATE_EXP_CONTEXT_RE.search(window):
        score += 40

    comp_starts = _section_starts(text, _COMPANY_SECTION_RE)
    comp_sec = _nearest_section_start(start, comp_starts)
    if comp_sec is not None and start - comp_sec < 1500:
        score -= 80

    return score


def extract_job_categories(*texts: str) -> list[str]:
    blob = " ".join(t for t in texts if t)
    if not blob.strip():
        return []

    found: list[str] = []
    seen: set[str] = set()
    for rx, label in _CATEGORY_PATTERNS:
        for m in rx.finditer(blob):
            if label in seen:
                break
            before = blob[max(0, m.start() - 100) : m.start()]
            if _NEGATION_BEFORE_RE.search(before):
                continue
            seen.add(label)
            found.append(label)
            break
    return found


def categories_to_field(categories: list[str]) -> str | None:
    return ", ".join(categories) if categories else None


def _collect_experience_candidates(text: str) -> list[tuple[int, str, int, int]]:
    """(lower_bound, label, score, start_pos) — higher score = more likely candidate requirement."""
    out: list[tuple[int, str, int, int]] = []

    def push(lo: int, hi: int | None, start: int, end: int) -> None:
        if hi is not None and _plausible_years(lo, hi):
            label = f"{lo}-{hi}"
        elif _plausible_years(lo):
            label = f"{lo}+"
        else:
            return
        score = _score_experience_match(text, start, end)
        if score <= -500:
            return
        out.append((lo, label, score, start))

    for m in _EXP_PHRASE_RE.finditer(text):
        push(int(m.group(1)), int(m.group(2)), m.start(), m.end())

    for m in _EXP_YEARS_RE.finditer(text):
        push(int(m.group(1)), int(m.group(2)), m.start(), m.end())

    for m in _EXP_PLUS_RE.finditer(text):
        push(int(m.group(1)), None, m.start(), m.end())

    for m in _EXP_MIN_RE.finditer(text):
        push(int(m.group(1)), None, m.start(), m.end())

    for m in _EXP_SINGLE_RE.finditer(text):
        n = int(m.group(1))
        if _plausible_years(n):
            score = _score_experience_match(text, m.start(), m.end())
            if score > -500:
                out.append((n, str(n), score, m.start()))

    return out


def extract_experience_years(text: str) -> str | None:
    """
    Required years of experience for the candidate (e.g. 7+, 3-5).
    Ignores company history ('20+ years of proven industry experience'), 401(k), PTO, etc.
    """
    if not text:
        return None

    candidates = _collect_experience_candidates(text)
    if not candidates:
        return None

    positive = [c for c in candidates if c[2] > 0]
    pool = positive if positive else [c for c in candidates if c[2] >= 0]

    if not pool:
        return None

    # Prefer requirement-context matches; among those, use the highest bar (7+ over 5+).
    pool.sort(key=lambda x: (-x[2], -x[0], x[3]))
    best_score = pool[0][2]
    top = [c for c in pool if c[2] == best_score]
    # Do not return company-scale tenure (e.g. 15+ years) without strong requirement context.
    if best_score < 50 and top and top[0][0] >= 12:
        safer = [c for c in pool if c[0] <= 10 and c[2] > 0]
        if safer:
            safer.sort(key=lambda x: (-x[2], -x[0], x[3]))
            return safer[0][1]
        return None
    top.sort(key=lambda x: (-x[0], x[3]))
    return top[0][1]


def normalize_seniority_experience(seniority: str, exp_years: str | None) -> tuple[str, str | None]:
    """Build experience display without junk like 'Not Applicable (40 years)'."""
    sen = (seniority or "").strip()
    if sen.lower() in ("not applicable", "n/a", "none", ""):
        sen = ""
    exp = (exp_years or "").strip()
    if exp and not _exp_years_plausible_string(exp):
        exp = ""
    if sen and exp:
        return f"{sen} ({exp} years)", exp
    if sen:
        return sen, exp or None
    if exp:
        return f"{exp} years", exp
    return sen, exp or None


def _exp_years_plausible_string(exp: str) -> bool:
    m = re.match(r"^(\d{1,2})(?:\+|-(\d{1,2}))?$", exp.strip())
    if not m:
        return False
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else None
    return _plausible_years(lo, hi)


def _parse_exp_lower_bound(exp: str) -> int | None:
    m = re.match(r"^(\d{1,2})(?:\+|-(\d{1,2}))?$", (exp or "").strip())
    if not m:
        return None
    return int(m.group(1))


def experience_llm_compatible(rules_exp: str | None, llm_exp: str) -> bool:
    """Reject LLM values that wildly exceed rule-based candidate requirements (e.g. 20+ vs 7+)."""
    r = _parse_exp_lower_bound(rules_exp or "")
    l = _parse_exp_lower_bound(llm_exp)
    if l is None:
        return False
    if r is None:
        return _exp_years_plausible_string(llm_exp)
    # LLM may refine upward slightly (7+ when rules found 5+) but not company-scale jumps.
    return l <= max(r + 3, r * 2 + 1)
