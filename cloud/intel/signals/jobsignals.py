"""Job- and contact-derived signals: hiring clusters, stack migration, departures.

Pure functions (no store access) so the rules are testable on their own; the
:class:`~cloud.intel.signals.service.SignalService` loads the rows and persists the
results as ``hiring_signals`` (idempotent by fingerprint).

HIRING_CLUSTER
    Open (ACTIVE or STALE) jobs grouped by company (CRM company id, else domain, else
    the normalised company name) and technology. A technology is a keyword the
    workspace's relevance engine matched in the job (the workbook universe, so the
    curated false-positive rules already apply), and only HIGH / REVIEW jobs count.
    ``CLUSTER_MIN`` (3) or more related open jobs make a cluster.

STACK_MIGRATION
    The same company shows a legacy ERP product and the modern product of the same
    vendor family within ``MIGRATION_WINDOW_DAYS``. Evidence must be either one job
    that names both with migration wording ("migrate", "upgrade", "conversion",
    "transition", "move to", ...), or at least one legacy job AND at least one modern
    job. A single job naming only one product never qualifies, and neither does a
    job naming both without migration wording.

DEPARTURE
    Only from authorized provider snapshots of the same person (same provider and
    provider contact id): the provider itself says the person left
    (``employment_status = left_company``) or now reports a different current company,
    after an earlier snapshot placed them at the company. A missing or empty provider
    result is never a departure. Only relevant ERP / technology managers count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cloud.intel.core.normalize import normalize_name

__all__ = ["CLUSTER_MIN", "MIGRATION_WINDOW_DAYS", "MIGRATION_FAMILIES", "JobSignal", "company_key",
           "detect_clusters", "detect_migrations", "detect_departures", "is_tech_manager"]

CLUSTER_MIN = 3
MIGRATION_WINDOW_DAYS = 180
ACTIVE = ("open", "stale")
RELEVANT = ("HIGH", "REVIEW")

#: vendor family -> (legacy patterns, modern patterns). Patterns are matched case-insensitively
#: on word boundaries in the job's title, keywords, matched keywords, technologies and text.
#: Ambiguous short forms are anchored to the vendor ("SAP ECC", not "ECC"; "Oracle EBS", not
#: "EBS", which is also an AWS service).
MIGRATION_FAMILIES: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "SAP": ((r"sap\s+ecc", r"ecc\s*6(\.0)?", r"sap\s+r/?3", r"sap\s+erp\s+6(\.0)?"),
            (r"s/?4\s*hana", r"sap\s+s/?4", r"rise\s+with\s+sap")),
    "Oracle": ((r"oracle\s+e-?business\s+suite", r"e-?business\s+suite", r"oracle\s+ebs", r"oracle\s+11i",
                r"oracle\s+r12", r"jd\s*edwards", r"jde\s+(e1|enterpriseone|world)", r"peoplesoft"),
               (r"oracle\s+fusion", r"fusion\s+cloud\s+erp", r"oracle\s+cloud\s+erp", r"oracle\s+erp\s+cloud",
                r"oracle\s+cloud\s+(financials|scm|hcm)")),
    "Microsoft Dynamics": ((r"dynamics\s+gp", r"great\s+plains", r"dynamics\s+nav", r"navision",
                            r"dynamics\s+ax", r"axapta"),
                           (r"dynamics\s+365", r"\bd365\b", r"business\s+central",
                            r"finance\s+(and|&)\s+operations")),
    "Infor": ((r"infor\s+ln", r"\bbaan\b", r"infor\s+xa", r"infor\s+lx", r"bpcs"),
              (r"infor\s+cloudsuite", r"cloudsuite\s+industrial")),
    "Sage": ((r"sage\s+(100|300|500)", r"sage\s+mas\s*(90|200|500)"),
             (r"sage\s+intacct",)),
}
_MIGRATION_WORDS = re.compile(r"\b(migrat\w*|upgrad\w*|conver(t|sion)\w*|transition\w*|move\s+to|moving\s+to|"
                              r"replac\w*|sunset\w*|re-?platform\w*|implementation\s+of)\b", re.I)
_COMPILED = {family: (tuple(re.compile(rf"(?<![\w/]){p}(?![\w])", re.I) for p in legacy),
                      tuple(re.compile(rf"(?<![\w/]){p}(?![\w])", re.I) for p in modern))
             for family, (legacy, modern) in MIGRATION_FAMILIES.items()}

_TECH_TITLE = re.compile(r"\b(erp|sap|oracle|netsuite|dynamics|d365|workday|infor|epicor|peoplesoft|jd\s*edwards|"
                         r"it|information\s+technology|technology|systems?|applications?|infrastructure|cio|cto|"
                         r"digital|data|software|engineering|enterprise\s+architecture)\b", re.I)
_MANAGER_TITLE = re.compile(r"\b(manager|director|head|vp|vice\s+president|lead|chief|cio|cto|owner|principal)\b",
                            re.I)


@dataclass
class JobSignal:
    signal_type: str
    key: str                       # company key (stable across runs)
    fingerprint: str
    summary: str
    strength: float
    confidence: float
    company_id: Optional[str] = None
    company_name: Optional[str] = None
    domain: Optional[str] = None
    contact_id: Optional[str] = None
    technologies: List[str] = field(default_factory=list)
    reason_codes: List[str] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    job_posting_ids: List[str] = field(default_factory=list)
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None


def company_key(job: Mapping[str, Any]) -> Optional[str]:
    """The company identity of a job: CRM company id, else its domain, else its normalised name."""
    if job.get("company_id"):
        return f"id:{job['company_id']}"
    if job.get("domain"):
        return f"domain:{str(job['domain']).lower().removeprefix('www.')}"
    name = normalize_name(job.get("company_name") or "")
    return f"name:{name}" if name else None


def _job_evidence(job: Mapping[str, Any], **extra: Any) -> Dict[str, Any]:
    seen = job.get("first_seen_at")
    return {"kind": "job", "job_posting_id": job["id"], "title": job.get("title"), "url": job.get("job_url"),
            "status": job.get("status"), "first_seen_at": seen.isoformat() if isinstance(seen, datetime) else seen,
            **extra}


def _company_fields(jobs: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    first = jobs[0]
    return {"company_id": next((j.get("company_id") for j in jobs if j.get("company_id")), None),
            "company_name": first.get("company_name"),
            "domain": next((j.get("domain") for j in jobs if j.get("domain")), None)}


def detect_clusters(jobs: Iterable[Mapping[str, Any]], *, now: datetime, minimum: int = CLUSTER_MIN
                    ) -> List[JobSignal]:
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for job in jobs:
        if job.get("status") not in ACTIVE or job.get("relevance_class") not in RELEVANT:
            continue
        key = company_key(job)
        if key is None:
            continue
        for tech in {str(k).strip() for k in (job.get("matched_keywords") or []) if str(k).strip()}:
            slot = groups.setdefault((key, tech.casefold()), {"tech": tech, "jobs": {}})
            slot["jobs"][job["id"]] = job
    out: List[JobSignal] = []
    for (key, tech_key), slot in sorted(groups.items()):
        members = sorted(slot["jobs"].values(), key=lambda j: str(j.get("first_seen_at") or ""))
        if len(members) < minimum:
            continue
        firsts = [j["first_seen_at"] for j in members if isinstance(j.get("first_seen_at"), datetime)]
        fields = _company_fields(members)
        name = fields["company_name"] or key.split(":", 1)[1]
        out.append(JobSignal(
            "HIRING_CLUSTER", key, f"HIRING_CLUSTER:{key}:{tech_key}"[:200],
            f"{name}: {len(members)} open {slot['tech']} jobs", strength=min(100.0, 20.0 * len(members)),
            confidence=0.9 if len(members) >= minimum + 2 else 0.75, technologies=[slot["tech"]],
            reason_codes=[f"open_jobs:{len(members)}", f"technology:{slot['tech']}"],
            evidence=[_job_evidence(j) for j in members[:50]], job_posting_ids=[j["id"] for j in members][:200],
            window_start=min(firsts) if firsts else None, window_end=now, **fields))
    return out


def _families_in(text: str) -> Dict[str, Tuple[List[str], List[str]]]:
    found: Dict[str, Tuple[List[str], List[str]]] = {}
    for family, (legacy, modern) in _COMPILED.items():
        old = sorted({m.group(0) for p in legacy for m in p.finditer(text)})
        new = sorted({m.group(0) for p in modern for m in p.finditer(text)})
        if old or new:
            found[family] = (old, new)
    return found


def _job_text(job: Mapping[str, Any]) -> str:
    parts: List[str] = [str(job.get("title") or ""), str(job.get("description") or "")]
    for column in ("matched_keywords", "technologies", "skills"):
        parts.extend(str(v) for v in (job.get(column) or []))
    parts.extend(str(job.get(f"keyword_{i}") or "") for i in range(1, 6))
    return " \n ".join(p for p in parts if p)


def detect_migrations(jobs: Iterable[Mapping[str, Any]], *, now: datetime,
                      window_days: int = MIGRATION_WINDOW_DAYS) -> List[JobSignal]:
    start = now - timedelta(days=window_days)
    by_company: Dict[str, List[Tuple[Mapping[str, Any], Dict[str, Tuple[List[str], List[str]]]]]] = {}
    for job in jobs:
        seen = job.get("last_seen_at") or job.get("first_seen_at")
        if isinstance(seen, datetime) and seen < start:
            continue
        key = company_key(job)
        if key is None:
            continue
        found = _families_in(_job_text(job))
        if found:
            by_company.setdefault(key, []).append((job, found))
    out: List[JobSignal] = []
    for key, items in sorted(by_company.items()):
        for family in MIGRATION_FAMILIES:
            legacy_jobs, modern_jobs, both = [], [], []
            for job, found in items:
                old, new = found.get(family, ([], []))
                text = _job_text(job)
                if old and new and _MIGRATION_WORDS.search(text):
                    both.append((job, old, new))
                elif old and not new:
                    legacy_jobs.append((job, old))
                elif new and not old:
                    modern_jobs.append((job, new))
            if not both and not (legacy_jobs and modern_jobs):
                continue
            evidence = ([_job_evidence(j, legacy=o, modern=n, rule="one job: legacy + modern + migration wording")
                         for j, o, n in both]
                        + [_job_evidence(j, legacy=o, rule="legacy job") for j, o in legacy_jobs]
                        + [_job_evidence(j, modern=n, rule="modern job") for j, n in modern_jobs])
            members = [e[0] for e in both] + [e[0] for e in legacy_jobs] + [e[0] for e in modern_jobs]
            legacy_terms = sorted({t for _, o, _ in both for t in o} | {t for _, o in legacy_jobs for t in o})
            modern_terms = sorted({t for _, _, n in both for t in n} | {t for _, n in modern_jobs for t in n})
            fields = _company_fields(members)
            name = fields["company_name"] or key.split(":", 1)[1]
            rule = ("migration_wording_in_job" if both else "legacy_and_modern_jobs")
            out.append(JobSignal(
                "STACK_MIGRATION", key, f"STACK_MIGRATION:{key}:{family}"[:200],
                f"{name}: {family} {', '.join(legacy_terms) or 'legacy'} -> {', '.join(modern_terms) or 'modern'} "
                f"({len(members)} job{'s' if len(members) != 1 else ''} in {window_days} days)",
                strength=min(100.0, 50.0 + 15.0 * len(members)), confidence=0.85 if both else 0.7,
                technologies=legacy_terms + modern_terms,
                reason_codes=[f"family:{family}", rule, f"jobs:{len(members)}", f"window_days:{window_days}"],
                evidence=evidence[:50], job_posting_ids=[j["id"] for j in members][:200], window_start=start,
                window_end=now, **fields))
    return out


def is_tech_manager(title: Optional[str]) -> bool:
    text = title or ""
    return bool(_TECH_TITLE.search(text) and _MANAGER_TITLE.search(text))


def detect_departures(contacts: Mapping[str, Mapping[str, Any]],
                      snapshots: Iterable[Mapping[str, Any]]) -> List[JobSignal]:
    """``contacts``: id -> contact row; ``snapshots``: contact_snapshots rows (any order)."""
    series: Dict[Tuple[str, str, str], List[Mapping[str, Any]]] = {}
    for snap in snapshots:
        if snap.get("provider") not in ("zoominfo", "seamless") or not snap.get("provider_contact_id"):
            continue  # only authorized provider evidence about an identified person
        series.setdefault((snap["contact_id"], snap["provider"], str(snap["provider_contact_id"])), []).append(snap)
    out: List[JobSignal] = []
    for (contact_id, provider, pid), snaps in sorted(series.items()):
        snaps = sorted(snaps, key=lambda s: s["observed_at"])
        if len(snaps) < 2:
            continue
        contact = contacts.get(contact_id) or {}
        previous, latest = snaps[-2], snaps[-1]
        if previous.get("employment_status") != "current":
            continue
        before = normalize_name(previous.get("company_name") or "")
        after = normalize_name(latest.get("company_name") or "")
        left = latest.get("employment_status") == "left_company"
        moved = bool(before and after and after != before)
        if not (left or moved):
            continue
        title = previous.get("title") or contact.get("title")
        if not is_tech_manager(title):
            continue
        reason = "provider_reports_left_company" if left else "provider_reports_new_company"
        out.append(JobSignal(
            "DEPARTURE", f"contact:{contact_id}", f"DEPARTURE:{contact_id}:{latest['id']}"[:200],
            f"{contact.get('full_name') or 'A contact'} ({title}) appears to have left "
            f"{previous.get('company_name') or 'the company'} — potential vacancy",
            strength=70.0, confidence=0.8 if left else 0.65, company_id=previous.get("company_id") or
            contact.get("company_id"), company_name=previous.get("company_name"), contact_id=contact_id,
            reason_codes=[reason, f"provider:{provider}"],
            evidence=[{"kind": "contact_snapshot", "snapshot_id": s["id"], "provider": provider,
                       "provider_contact_id": pid, "company_name": s.get("company_name"), "title": s.get("title"),
                       "employment_status": s.get("employment_status"),
                       "observed_at": s["observed_at"].isoformat() if isinstance(s["observed_at"], datetime)
                       else s["observed_at"]} for s in (previous, latest)],
            window_start=previous["observed_at"], window_end=latest["observed_at"]))
    return out
