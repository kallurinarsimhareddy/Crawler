"""The hiring-signal rules. Pure functions over a company's postings: no I/O.

Every rule below is written down, not learned. Each detected signal carries the
postings that produced it (ids, titles, URLs, dates), the phrases that matched,
the time window it covers, a confidence, a 0-100 strength and reason codes. A
signal whose evidence is not in the data is not emitted.

=========================  ==========================================================
Signal                     Rule (defaults; see the constants below)
=========================  ==========================================================
NEW_ROLE                   ≥1 relevant posting first seen in the last 7 days
MULTIPLE_RELEVANT_ROLES    ≥3 relevant postings open at once
HIRING_SPIKE               relevant postings first seen in the last 14 days ≥ 3 AND
                           ≥ 2× the average 14-day count over the previous 84 days
HIRING_VELOCITY            ≥4 relevant postings in the last 30 days AND ≥2 in the 30
                           days before (sustained, not a one-off burst)
LONG_OPEN_ROLE             a relevant posting open ≥45 days (posted date, else first seen)
HARD_TO_FILL               open ≥60 days AND (specialised technology OR senior+), or the
                           same title closed and reposted within 90 days
SPECIALIZED_TECHNOLOGY     an open relevant posting names a niche/legacy technology
                           (iSeries/RPG, JD Edwards, Infor, SAP products, legacy ERP…)
PROJECT_IMPLEMENTATION     implementation/migration/upgrade/go-live language in a posting
                           that also names an ERP/enterprise technology
EXPANSION_HIRING           explicit expansion language (new plant/facility/office, newly
                           created role, expanding team) OR open roles in ≥3 locations
                           never seen before for this company within 30 days
BACKFILL_REPLACEMENT       ONLY explicit backfill/replacement language, or the same title
                           closed then reposted within 60 days. Never inferred otherwise.
LEADERSHIP_HIRING          an open director / VP / C-level posting
=========================  ==========================================================

"Relevant" means the posting matched a workspace campaign (``is_relevant``), or,
when the workspace has no campaign rules yet, it sits in a technology department.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

__all__ = ["DetectedSignal", "SIGNAL_TYPES", "detect_signals", "aggregate", "TECH_DEPARTMENTS", "SPECIALIZED"]

SIGNAL_TYPES = ("NEW_ROLE", "MULTIPLE_RELEVANT_ROLES", "HIRING_SPIKE", "HIRING_VELOCITY", "LONG_OPEN_ROLE",
                "HARD_TO_FILL", "SPECIALIZED_TECHNOLOGY", "PROJECT_IMPLEMENTATION", "EXPANSION_HIRING",
                "BACKFILL_REPLACEMENT", "LEADERSHIP_HIRING")

TECH_DEPARTMENTS = {"erp", "it", "data", "engineering", "qa", "security"}

NEW_ROLE_DAYS = 7
MULTIPLE_MIN = 3
SPIKE_WINDOW = 14
SPIKE_BASELINE_DAYS = 84
SPIKE_MIN = 3
SPIKE_RATIO = 2.0
VELOCITY_WINDOW = 30
VELOCITY_MIN = 4
VELOCITY_PREVIOUS_MIN = 2
LONG_OPEN_DAYS = 45
HARD_TO_FILL_DAYS = 60
REPOST_DAYS_HARD = 90
REPOST_DAYS_BACKFILL = 60
EXPANSION_NEW_LOCATIONS = 3

SPECIALIZED = {
    "RPG", "CL / CLLE", "IBM AS/400", "IBM iSeries", "DB2 for i", "COBOL", "JD Edwards EnterpriseOne",
    "JD Edwards World", "JD Edwards", "Infor LN", "Infor M3", "Infor SyteLine", "Infor XA", "Infor Visual",
    "Infor CloudSuite", "SAP ECC", "SAP S/4HANA", "SAP ABAP", "SAP FICO", "SAP MM", "SAP SD", "SAP PP", "SAP EWM",
    "SAP Basis", "SAP BW", "Oracle E-Business Suite", "Oracle PeopleSoft", "Dynamics AX", "Dynamics NAV",
    "Dynamics GP", "Dynamics 365 Finance & Operations", "QAD", "SYSPRO", "Glovia", "Macola", "IQMS / DELMIAworks",
    "Plex", "Epicor", "Aptean Ross", "Global Shop Solutions", "Manhattan WMS", "Blue Yonder", "HighJump / Körber",
}
ENTERPRISE_FAMILIES = {"ERP", "SAP", "Oracle", "Dynamics", "JD Edwards", "Infor", "WMS", "CRM", "iSeries/RPG"}

_I = re.IGNORECASE
PROJECT_PHRASES = re.compile(
    r"(?<![a-z])(implementation|implementing|implement|migration|migrating|migrate|upgrade|upgrading|go-live|"
    r"go live|rollout|roll-out|roll out|conversion|modernization|modernisation|re-?platform\w*|greenfield|"
    r"deployment of|transformation program\w*|cutover)(?![a-z])", _I)
EXPANSION_PHRASES = re.compile(
    r"(new (?:plant|facility|facilities|office|site|location|distribution center|warehouse)|expansion|"
    r"expanding (?:our|the) (?:team|operations|footprint)|newly created (?:role|position)|new(?:ly)? created|"
    r"newly formed team|growing (?:team|department)|opening (?:a|our) new)", _I)
BACKFILL_PHRASES = re.compile(
    r"(back-?fill|replacement (?:for|of)|replacing (?:a|an|our) (?:departing|retiring)|"
    r"due to (?:a |an )?(?:retirement|departure|resignation|promotion)|vacated by|"
    r"position (?:is )?(?:open|available) due to)", _I)
SENIOR = {"senior", "lead", "manager", "director", "vp", "c_level"}
LEADERSHIP = {"director", "vp", "c_level"}


@dataclass
class DetectedSignal:
    signal_type: str
    window_start: datetime
    window_end: datetime
    confidence: float
    strength: float
    reason_codes: List[str]
    summary: str
    evidence: List[Dict[str, Any]]
    job_posting_ids: List[str] = field(default_factory=list)

    def fingerprint(self, company_id: str, bucket: str) -> str:
        ids = ",".join(sorted(self.job_posting_ids))
        digest = hashlib.sha256(ids.encode()).hexdigest()[:16]
        return f"{self.signal_type}:{company_id}:{bucket}:{digest}"[:200]


def _opened(job: Mapping[str, Any]) -> datetime:
    """When a posting opened: its posted date when known and earlier, else when we first saw it."""
    posted, seen = job.get("posted_at"), job["first_seen_at"]
    return posted if posted is not None and posted <= seen else seen


def _ev(job: Mapping[str, Any], **extra: Any) -> Dict[str, Any]:
    return {"job_posting_id": job["id"], "title": job["title"], "url": job.get("job_url"),
            "first_seen_at": job["first_seen_at"].isoformat(),
            "posted_at": job["posted_at"].isoformat() if job.get("posted_at") else None,
            "status": job.get("status"), **extra}


def _phrases(pattern: re.Pattern, job: Mapping[str, Any]) -> List[str]:
    text = f"{job.get('title') or ''}\n{job.get('description') or ''}"
    out = []
    for m in pattern.finditer(text):
        start, end = max(0, m.start() - 60), min(len(text), m.end() + 60)
        snippet = re.sub(r"\s+", " ", text[start:end]).strip()
        if snippet not in out:
            out.append(snippet)
        if len(out) >= 3:
            break
    return out


def _families(job: Mapping[str, Any], family_of: Callable[[str], Sequence[str]]) -> set:
    fams = set()
    for tech in job.get("technologies") or []:
        fams.update(family_of(tech))
    return fams


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def detect_signals(jobs: Sequence[Mapping[str, Any]], *, now: datetime, has_campaigns: bool,
                   family_of: Callable[[str], Sequence[str]]) -> List[DetectedSignal]:
    def relevant(job: Mapping[str, Any]) -> bool:
        return bool(job.get("is_relevant")) or (not has_campaigns and job.get("department") in TECH_DEPARTMENTS)

    rel = [j for j in jobs if relevant(j)]
    open_rel = [j for j in rel if j.get("status") == "open"]
    out: List[DetectedSignal] = []

    # 1 NEW_ROLE
    start = now - timedelta(days=NEW_ROLE_DAYS)
    new = [j for j in open_rel if j["first_seen_at"] >= start]
    if new:
        out.append(DetectedSignal("NEW_ROLE", start, now, 0.9, _clamp(40 + 20 * len(new)), ["first_seen_within_7d"],
                                  f"{len(new)} new relevant role(s) in the last {NEW_ROLE_DAYS} days",
                                  [_ev(j) for j in new[:20]], [j["id"] for j in new]))

    # 2 MULTIPLE_RELEVANT_ROLES
    if len(open_rel) >= MULTIPLE_MIN:
        out.append(DetectedSignal("MULTIPLE_RELEVANT_ROLES", min(_opened(j) for j in open_rel), now, 0.85,
                                  _clamp(30 + 10 * len(open_rel)), [f"open_relevant_roles_{len(open_rel)}"],
                                  f"{len(open_rel)} relevant roles open at once",
                                  [_ev(j) for j in open_rel[:20]], [j["id"] for j in open_rel]))

    # 3 HIRING_SPIKE
    spike_start = now - timedelta(days=SPIKE_WINDOW)
    base_start = spike_start - timedelta(days=SPIKE_BASELINE_DAYS)
    recent = [j for j in rel if j["first_seen_at"] >= spike_start]
    baseline = [j for j in rel if base_start <= j["first_seen_at"] < spike_start]
    periods = SPIKE_BASELINE_DAYS / SPIKE_WINDOW
    average = len(baseline) / periods
    earliest = min((j["first_seen_at"] for j in jobs), default=now)
    if len(recent) >= SPIKE_MIN and len(recent) >= SPIKE_RATIO * max(average, 0.5):
        history_days = (spike_start - earliest).days
        codes = [f"recent_{len(recent)}_vs_avg_{average:.1f}"]
        confidence = 0.8
        if history_days < SPIKE_WINDOW * 2:
            codes.append("limited_history")
            confidence = 0.45  # without a baseline, "spike" may just be the first crawl
        out.append(DetectedSignal("HIRING_SPIKE", spike_start, now, confidence,
                                  _clamp(40 + 12 * (len(recent) - average)), codes,
                                  f"{len(recent)} relevant roles in {SPIKE_WINDOW} days vs a baseline of "
                                  f"{average:.1f} per {SPIKE_WINDOW} days",
                                  [_ev(j) for j in recent[:20]], [j["id"] for j in recent]))

    # 4 HIRING_VELOCITY
    v_start = now - timedelta(days=VELOCITY_WINDOW)
    v_prev = v_start - timedelta(days=VELOCITY_WINDOW)
    last30 = [j for j in rel if j["first_seen_at"] >= v_start]
    prev30 = [j for j in rel if v_prev <= j["first_seen_at"] < v_start]
    if len(last30) >= VELOCITY_MIN and len(prev30) >= VELOCITY_PREVIOUS_MIN:
        out.append(DetectedSignal("HIRING_VELOCITY", v_prev, now, 0.8, _clamp(25 * len(last30) / VELOCITY_MIN),
                                  [f"last30_{len(last30)}", f"prev30_{len(prev30)}"],
                                  f"Sustained hiring: {len(last30)} relevant roles in 30 days after {len(prev30)} "
                                  f"the 30 days before", [_ev(j) for j in (last30 + prev30)[:20]],
                                  [j["id"] for j in last30 + prev30]))

    # 5 LONG_OPEN_ROLE / 6 HARD_TO_FILL
    long_open, hard = [], []
    for j in open_rel:
        age = (now - _opened(j)).days
        basis = "posted_at" if j.get("posted_at") and j["posted_at"] <= j["first_seen_at"] else "first_seen_at"
        if age >= LONG_OPEN_DAYS:
            long_open.append((j, age, basis))
        specialized = [t for t in j.get("technologies") or [] if t in SPECIALIZED]
        reasons = []
        if age >= HARD_TO_FILL_DAYS and specialized:
            reasons.append("open_60d_specialized_technology")
        if age >= HARD_TO_FILL_DAYS and j.get("seniority") in SENIOR:
            reasons.append("open_60d_senior_role")
        reposts = [o for o in jobs if o["id"] != j["id"] and o.get("normalized_title") == j.get("normalized_title")
                   and o.get("status") == "closed" and o.get("closed_at")
                   and timedelta(0) <= j["first_seen_at"] - o["closed_at"] <= timedelta(days=REPOST_DAYS_HARD)]
        if reposts:
            reasons.append("closed_and_reposted_within_90d")
        if reasons:
            hard.append((j, age, reasons, reposts, specialized))
    if long_open:
        oldest = max(a for _, a, _ in long_open)
        out.append(DetectedSignal("LONG_OPEN_ROLE", now - timedelta(days=oldest), now, 0.85,
                                  _clamp(30 + oldest / 2), [f"open_{oldest}d"] + sorted({b for *_, b in long_open}),
                                  f"{len(long_open)} relevant role(s) open ≥{LONG_OPEN_DAYS} days (oldest {oldest}d)",
                                  [_ev(j, days_open=a, age_basis=b) for j, a, b in long_open[:20]],
                                  [j["id"] for j, _, _ in long_open]))
    if hard:
        codes = sorted({r for _, _, rs, _, _ in hard for r in rs})
        out.append(DetectedSignal("HARD_TO_FILL", now - timedelta(days=max(a for _, a, *_ in hard)), now,
                                  0.7 if "closed_and_reposted_within_90d" in codes else 0.65,
                                  _clamp(40 + 15 * len(hard)), codes,
                                  f"{len(hard)} role(s) look hard to fill",
                                  [_ev(j, days_open=a, reasons=rs, reposted_from=[o["id"] for o in reps],
                                       specialized_technologies=sp) for j, a, rs, reps, sp in hard[:20]],
                                  [j["id"] for j, *_ in hard]))

    # 7 SPECIALIZED_TECHNOLOGY
    spec_jobs = [(j, [t for t in j.get("technologies") or [] if t in SPECIALIZED]) for j in open_rel]
    spec_jobs = [(j, ts) for j, ts in spec_jobs if ts]
    if spec_jobs:
        techs = sorted({t for _, ts in spec_jobs for t in ts})
        out.append(DetectedSignal("SPECIALIZED_TECHNOLOGY", min(_opened(j) for j, _ in spec_jobs), now, 0.85,
                                  _clamp(40 + 10 * len(techs) + 5 * len(spec_jobs)),
                                  [f"tech:{t}" for t in techs[:10]], f"Hiring for {', '.join(techs[:5])}",
                                  [_ev(j, technologies=ts) for j, ts in spec_jobs[:20]], [j["id"] for j, _ in spec_jobs]))

    # 8 PROJECT_IMPLEMENTATION
    projects = []
    for j in open_rel:
        fams = _families(j, family_of) & ENTERPRISE_FAMILIES
        phrases = _phrases(PROJECT_PHRASES, j)
        if fams and phrases:
            projects.append((j, sorted(fams), phrases))
    if projects:
        out.append(DetectedSignal("PROJECT_IMPLEMENTATION", min(_opened(j) for j, *_ in projects), now, 0.75,
                                  _clamp(45 + 15 * len(projects)),
                                  sorted({f"family:{f}" for _, fs, _ in projects for f in fs}) + ["project_language"],
                                  f"{len(projects)} role(s) describe an implementation/migration/upgrade",
                                  [_ev(j, families=fs, phrases=ps) for j, fs, ps in projects[:20]],
                                  [j["id"] for j, *_ in projects]))

    # 9 EXPANSION_HIRING
    exp_jobs = [(j, _phrases(EXPANSION_PHRASES, j)) for j in [x for x in jobs if x.get("status") == "open"]]
    exp_jobs = [(j, ps) for j, ps in exp_jobs if ps]
    window = now - timedelta(days=30)
    older_locations = {(j.get("location") or "").strip().lower() for j in jobs if j["first_seen_at"] < window}
    new_locations: Dict[str, Mapping[str, Any]] = {}
    for j in jobs:
        loc = (j.get("location") or "").strip().lower()
        if j["first_seen_at"] >= window and j.get("status") == "open" and loc and loc not in older_locations \
                and older_locations:
            new_locations.setdefault(loc, j)
    codes = []
    if exp_jobs:
        codes.append("expansion_language")
    if len(new_locations) >= EXPANSION_NEW_LOCATIONS:
        codes.append(f"new_locations_{len(new_locations)}")
    if codes:
        evidence = [_ev(j, phrases=ps) for j, ps in exp_jobs[:10]] + [
            _ev(j, new_location=loc) for loc, j in list(new_locations.items())[:10]]
        ids = [j["id"] for j, _ in exp_jobs] + ([j["id"] for j in new_locations.values()]
                                                if len(new_locations) >= EXPANSION_NEW_LOCATIONS else [])
        out.append(DetectedSignal("EXPANSION_HIRING", window, now, 0.7 if exp_jobs else 0.6,
                                  _clamp(40 + 10 * len(ids)), codes, "Hiring that points to expansion", evidence,
                                  sorted(set(ids))))

    # 10 BACKFILL_REPLACEMENT — explicit evidence only
    backfill = []
    for j in [x for x in jobs if x.get("status") == "open"]:
        phrases = _phrases(BACKFILL_PHRASES, j)
        reposts = [o for o in jobs if o["id"] != j["id"] and o.get("normalized_title") == j.get("normalized_title")
                   and o.get("status") == "closed" and o.get("closed_at")
                   and timedelta(0) <= j["first_seen_at"] - o["closed_at"] <= timedelta(days=REPOST_DAYS_BACKFILL)]
        if phrases:
            backfill.append((j, "explicit_backfill_language", phrases, reposts))
        elif reposts:
            backfill.append((j, "same_title_reposted_within_60d", [], reposts))
    if backfill:
        explicit = any(kind == "explicit_backfill_language" for _, kind, _, _ in backfill)
        out.append(DetectedSignal("BACKFILL_REPLACEMENT", now - timedelta(days=REPOST_DAYS_BACKFILL), now,
                                  0.8 if explicit else 0.55, _clamp(30 + 10 * len(backfill)),
                                  sorted({kind for _, kind, _, _ in backfill}),
                                  "Role(s) that are explicitly a backfill/replacement" if explicit else
                                  "Same title closed and reposted within 60 days (possible replacement)",
                                  [_ev(j, basis=kind, phrases=ps, reposted_from=[o["id"] for o in reps])
                                   for j, kind, ps, reps in backfill[:20]], [j["id"] for j, *_ in backfill]))

    # 11 LEADERSHIP_HIRING
    leaders = [j for j in jobs if j.get("status") == "open" and j.get("seniority") in LEADERSHIP]
    if leaders:
        out.append(DetectedSignal("LEADERSHIP_HIRING", min(_opened(j) for j in leaders), now, 0.9,
                                  _clamp(50 + 15 * len(leaders)),
                                  sorted({f"{j.get('seniority')}:{j.get('department')}" for j in leaders}),
                                  f"{len(leaders)} leadership role(s) open",
                                  [_ev(j, seniority=j.get("seniority"), department=j.get("department"))
                                   for j in leaders[:20]], [j["id"] for j in leaders]))
    return out


def aggregate(jobs: Sequence[Mapping[str, Any]], *, now: datetime, has_campaigns: bool) -> Dict[str, Any]:
    """The company-level hiring picture the scores and the UI are built from."""
    def relevant(job):
        return bool(job.get("is_relevant")) or (not has_campaigns and job.get("department") in TECH_DEPARTMENTS)

    open_jobs = [j for j in jobs if j.get("status") == "open"]
    rel = [j for j in open_jobs if relevant(j)]
    count = lambda items, key: {k: sum(1 for j in items if j.get(key) == k) for k in {j.get(key) for j in items}}
    techs: Dict[str, int] = {}
    for j in rel:
        for t in j.get("technologies") or []:
            techs[t] = techs.get(t, 0) + 1
    ages = sorted((now - _opened(j)).days for j in open_jobs)
    last30 = sum(1 for j in jobs if relevant(j) and j["first_seen_at"] >= now - timedelta(days=30))
    return {
        "open_jobs": len(open_jobs),
        "relevant_open_jobs": len(rel),
        "technologies": dict(sorted(techs.items(), key=lambda kv: -kv[1])[:25]),
        "departments": count(open_jobs, "department"),
        "seniority": count(open_jobs, "seniority"),
        "velocity_30d": last30,
        "median_age_days": ages[len(ages) // 2] if ages else None,
        "oldest_age_days": ages[-1] if ages else None,
    }
