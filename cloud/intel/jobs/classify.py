"""Deterministic job classification: no AI, every label traceable to a rule.

``classify(title, description, location)`` returns workplace type, seniority,
department, technologies, skills, minimum years of experience, certifications,
employment type and country. Rules are ordered most-specific first; the first
rule that fires wins, and the rule that fired is returned in ``reasons`` so a
label can be argued with.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from cloud.intel.core.normalize import normalize_country
from cloud.intel.technology.taxonomy import detect
from cloud.intel.vendor import job_classify as _jc
from cloud.intel.vendor import skills_extract as _skills

__all__ = ["classify", "SENIORITIES", "DEPARTMENTS", "is_leadership"]

SENIORITIES = ("intern", "entry", "mid", "senior", "lead", "manager", "director", "vp", "c_level")

_I = re.IGNORECASE


def _rx(*words: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(words) + r")(?![A-Za-z0-9])", _I)


# (seniority, pattern) — first match wins; checked against the title only.
_SENIORITY: List[Tuple[str, re.Pattern]] = [
    ("c_level", _rx(r"chief\s+\w+(?:\s+\w+)?\s+officer", "CEO", "CIO", "CTO", "CFO", "COO", "CISO", "CHRO", "CDO",
                    "CMO", "president")),
    ("vp", _rx(r"s?vp", r"vice\s+president", r"head\s+of", r"evp")),
    ("director", _rx("director", r"dir\.")),
    ("manager", _rx("manager", r"mgr", "supervisor")),
    ("lead", _rx("lead", "principal", "architect", r"staff\s+(?:\w+\s+)?(?:engineer|developer|scientist)")),
    ("intern", _rx("intern", "internship", "co-op", "apprentice")),
    ("entry", _rx("junior", r"jr\.?", "entry[- ]level", "associate", "graduate", r"I(?!\w)")),
    ("senior", _rx("senior", r"sr\.?", "III", "IV", "expert")),
]

DEPARTMENTS = ("leadership", "erp", "data", "qa", "security", "it", "engineering", "hr", "finance", "sales",
               "marketing", "operations", "supply_chain", "other")

_DEPARTMENT: List[Tuple[str, re.Pattern]] = [
    ("erp", _rx("ERP", "SAP", "JD\\s?Edwards", "JDE", "Oracle\\s+EBS", "NetSuite", "Dynamics", "D365", "Infor",
                "Epicor", "SyteLine", "ABAP", "FICO", "S/4\\s?HANA", "RPG", "AS/?400", "iSeries", "IBM\\s+i", "QAD",
                "SYSPRO", "Acumatica", "Sage")),
    ("data", _rx("data", "analytics", "BI", "machine\\s+learning", "ML", "AI", "ETL", "data\\s+warehouse",
                 "Power\\s?BI", "Tableau", "Snowflake", "Databricks")),
    ("qa", _rx("QA", "quality\\s+assurance", "test(?:er|ing)?", "SDET", "automation\\s+engineer")),
    ("security", _rx("security", "cyber\\w*", "SOC", "IAM", "infosec")),
    ("it", _rx("IT", "systems?\\s+admin\\w*", "network", "help\\s?desk", "service\\s+desk", "infrastructure",
               "cloud", "DevOps", "SRE", "sys\\s?admin", "technical\\s+support", "desktop\\s+support")),
    ("engineering", _rx("software", "developer", "engineer(?:ing)?", "programmer", "full[- ]stack", "front[- ]end",
                        "back[- ]end", "\\.NET", "Java", "Python", "mobile")),
    ("hr", _rx("HR", "human\\s+resources", "recruit\\w*", "talent", "people\\s+ops", "payroll", "benefits",
               "HRIS")),
    ("finance", _rx("finance", "accountant", "accounting", "controller", "FP&A", "tax", "treasury", "audit\\w*",
                    "AP", "AR", "bookkeep\\w*")),
    ("sales", _rx("sales", "account\\s+executive", "business\\s+development", "BDR", "SDR", "account\\s+manager")),
    ("marketing", _rx("marketing", "brand", "content", "SEO", "demand\\s+gen\\w*", "communications")),
    ("supply_chain", _rx("supply\\s+chain", "logistics", "warehouse", "procurement", "purchasing", "buyer",
                         "inventory", "planner", "WMS")),
    ("operations", _rx("operations", "plant", "production", "manufacturing", "maintenance", "machinist",
                       "technician", "operator")),
]

_REMOTE = _rx("remote", "work\\s+from\\s+home", "WFH", "telecommute", "anywhere\\s+in\\s+the\\s+US", "fully\\s+remote",
              "100%\\s+remote")
_HYBRID = _rx("hybrid", r"\d\s+days?\s+(?:a|per)\s+week\s+(?:in|on)[- ]?(?:site|office)", "partially\\s+remote")
_ONSITE = _rx("on[- ]?site", "in[- ]office", "in\\s+person", "office[- ]based")

_EMPLOYMENT: List[Tuple[str, re.Pattern]] = [
    ("internship", _rx("internship", "intern")),
    ("contract_to_hire", _rx("contract[- ]to[- ]hire", "C2H", "temp[- ]to[- ]perm")),
    ("contract", _rx("contract", "contractor", "C2C", "1099", "W2\\s+contract", "consultant\\s+\\(contract\\)")),
    ("part_time", _rx("part[- ]time")),
    ("temporary", _rx("temporary", "seasonal")),
    ("full_time", _rx("full[- ]time", "FTE", "permanent")),
]

_CERTIFICATIONS: List[Tuple[str, re.Pattern]] = [
    ("PMP", _rx("PMP", "Project\\s+Management\\s+Professional")),
    ("CISSP", _rx("CISSP")), ("CISM", _rx("CISM")), ("CISA", _rx("CISA")), ("CompTIA Security+", _rx("Security\\+")),
    ("AWS Certified", _rx("AWS\\s+Certified(?:\\s+[\\w-]+){0,3}", "AWS\\s+certification")),
    ("Azure Certified", _rx("Azure\\s+(?:Certified|certification)", "AZ-\\d{3}")),
    ("Google Cloud Certified", _rx("Google\\s+Cloud\\s+Certified", "GCP\\s+certification")),
    ("SAP Certified", _rx("SAP\\s+certifi\\w+", "SAP\\s+Certified(?:\\s+[\\w-]+){0,4}")),
    ("Oracle Certified", _rx("Oracle\\s+Certified\\s+\\w+", "OCP", "OCA")),
    ("Microsoft Certified", _rx("Microsoft\\s+Certified", "MCSE", "MCSA")),
    ("ITIL", _rx("ITIL")), ("Scrum", _rx("CSM", "Certified\\s+Scrum\\s+Master", "PSM", "Scrum\\s+certification")),
    ("SAFe", _rx("SAFe(?:\\s+\\d)?\\s+(?:Agilist|certif\\w+)")), ("Six Sigma", _rx("Six\\s+Sigma", "Lean\\s+Six\\s+Sigma")),
    ("CPA", _rx("CPA")), ("SHRM", _rx("SHRM-(?:CP|SCP)", "SHRM\\s+certif\\w+")), ("PHR", _rx("PHR", "SPHR")),
    ("CCNA", _rx("CCNA", "CCNP", "CCIE")), ("APICS", _rx("APICS", "CPIM", "CSCP")),
    ("Salesforce Certified", _rx("Salesforce\\s+Certified(?:\\s+[\\w-]+){0,3}")),
]

_LEADERSHIP_TITLE = _rx(r"chief\s+\w+(?:\s+\w+)?\s+officer", "CEO", "CIO", "CTO", "CFO", "COO", "CISO", "CHRO",
                        r"vice\s+president", "s?vp", "evp", "director", r"head\s+of", "president")


from cloud.intel.vendor.identity import _REGIONS as _REGION_CODES  # noqa: E402 - US states + CA provinces

_CA_PROVINCES = {"on", "qc", "bc", "ab", "mb", "sk", "ns", "nb", "nl"}
_US_STATES = {code.upper() for code in _REGION_CODES.values() if code not in _CA_PROVINCES}
_US_STATE_NAMES = {name for name, code in _REGION_CODES.items() if code not in _CA_PROVINCES}


def is_leadership(title: str) -> bool:
    return bool(_LEADERSHIP_TITLE.search(title or ""))


def _first(rules: List[Tuple[str, re.Pattern]], text: str) -> Tuple[Optional[str], Optional[str]]:
    for label, pattern in rules:
        m = pattern.search(text or "")
        if m:
            return label, m.group(0)
    return None, None


def _years(text: str) -> Optional[int]:
    try:
        value = _jc.extract_experience_years(text or "")
    except Exception:  # noqa: BLE001 - vendored helper; a parse failure means "unknown"
        value = None
    if not value:
        return None
    m = re.search(r"\d+", str(value))
    if not m:
        return None
    years = int(m.group(0))
    return years if 0 <= years <= 60 else None


def classify(title: str, description: str = "", location: str = "") -> Dict[str, Any]:
    title = (title or "").strip()
    description = description or ""
    everything = f"{title}\n{location}\n{description}"
    reasons: List[str] = []

    seniority, hit = _first(_SENIORITY, title)
    if seniority is None:
        seniority = "mid"
        reasons.append("seniority: default mid (no title marker)")
    else:
        reasons.append(f"seniority: {seniority} ('{hit}' in title)")
    if seniority in ("vp", "c_level", "director") and is_leadership(title):
        department = "leadership"
        reasons.append(f"department: leadership ({seniority} title)")
    else:
        department, hit = _first(_DEPARTMENT, title)
        if department is None:
            department, hit = _first(_DEPARTMENT, description[:1500])
            if department:
                reasons.append(f"department: {department} ('{hit}' in description)")
        else:
            reasons.append(f"department: {department} ('{hit}' in title)")
        department = department or "other"

    loc_and_title = f"{title} {location}"
    if _REMOTE.search(loc_and_title) and not _HYBRID.search(loc_and_title):
        workplace = "remote"
    elif _HYBRID.search(loc_and_title) or _HYBRID.search(description):
        workplace = "hybrid"
    elif _REMOTE.search(description) and not _ONSITE.search(description):
        workplace = "remote"
    elif _ONSITE.search(everything):
        workplace = "onsite"
    elif location and not _REMOTE.search(everything):
        workplace = "onsite" if re.search(r"[A-Za-z]", location) else "unknown"
    else:
        workplace = "unknown"
    reasons.append(f"workplace: {workplace}")

    employment, hit = _first(_EMPLOYMENT, f"{title}\n{description[:3000]}")
    technologies = [m.technology for m in detect(everything)]
    try:
        skills = list(_skills.extract_skills_from_text(everything))
    except Exception:  # noqa: BLE001
        skills = []
    certifications = [label for label, pattern in _CERTIFICATIONS if pattern.search(description or title)]
    country = None
    if location:
        tail = location.split(",")[-1].strip()
        # "Tulsa, OK" / "Irvine, CA": a US state code, not Canada ("CA") or India ("IN").
        if tail.upper() in _US_STATES or tail.lower() in _US_STATE_NAMES:
            country = "United States"
        elif tail and not re.search(r"\d", tail):
            country = normalize_country(tail)
            if country and len(country) <= 3 and country not in ("UK",):
                country = None  # an unknown short code is not evidence of a country
    if workplace == "remote" and re.search(r"(?<![A-Za-z])(?:US|USA|United\s+States)(?![A-Za-z])", everything):
        country = country or "United States"

    return {
        "workplace_type": workplace,
        "seniority": seniority,
        "department": department,
        "technologies": technologies,
        "skills": skills,
        "years_experience_min": _years(description or title),
        "certifications": certifications,
        "employment_type": employment,
        "country": country,
        "is_leadership": is_leadership(title),
        "reasons": reasons,
    }
