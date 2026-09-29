"""Job detail pages: the fields a listing rarely has, and merging them into the listing row.

From one detail page, deterministically:

* ``JobPosting`` JSON-LD / microdata (title, location, department/occupational
  category, employment type, remote, salary, description, posted date, hiring
  organisation);
* the page's main text: salary (vendored salary parser), skills (vendored skills
  catalogue), years of experience (vendored extractor), certifications, and
  technologies (the platform's technology taxonomy);
* seniority and job family are *derived* from the title by the normaliser.

:func:`merge_job` combines listing and detail values field by field with
:func:`~cloud.intel.scraper.models.merge_value`: a stronger source keeps its value,
a disagreeing weaker one is kept as an alternative (a conflict), and each value
keeps its own source URL.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from cloud.intel.scraper.models import FieldValue, merge_value

__all__ = ["CERTIFICATIONS", "extract_detail", "merge_job"]

CERTIFICATIONS = ("PMP", "CAPM", "CPA", "CMA", "CFA", "CISSP", "CISM", "CISA", "CEH", "CompTIA Security+", "Security+",
                  "Network+", "A+", "CCNA", "CCNP", "ITIL", "Six Sigma", "Lean Six Sigma", "Scrum Master", "CSM",
                  "PSM", "SHRM-CP", "SHRM-SCP", "PHR", "SPHR", "AWS Certified", "Azure Certified", "Google Cloud Certified",
                  "SAP Certified", "Oracle Certified", "Salesforce Certified", "PE license", "RN", "CDL")
_CERT_PATTERNS = [(c, re.compile(r"(?<![A-Za-z0-9])" + re.escape(c) + r"(?![A-Za-z0-9])")) for c in CERTIFICATIONS]


def _fv(value: Any, method: str, url: str, evidence: str, confidence: float) -> FieldValue:
    return FieldValue(value, method, confidence, evidence[:200], url)


def _main_text(soup: Any) -> str:
    for tag in soup(["script", "style", "noscript", "template", "svg", "iframe", "nav", "header", "footer"]):
        tag.decompose()
    main = soup.find("main") or soup.find(attrs={"id": re.compile(r"content|job|posting", re.I)}) or soup.body or soup
    return re.sub(r"\s+", " ", main.get_text(" ")).strip()


def extract_detail(html: str, url: str) -> Dict[str, FieldValue]:
    """Job fields from one detail page (see the module docstring)."""
    from bs4 import BeautifulSoup

    from cloud.intel.scraper.extractor import _clean, _first, _location_of, _salary_of, _types
    from cloud.intel.vendor.html import json_ld_objects

    soup = BeautifulSoup(html or "", "lxml")
    out: Dict[str, FieldValue] = {}

    def put(name: str, value: Any, method: str, evidence: str, confidence: float) -> None:
        if value in (None, "", []) or name in out:
            return
        out[name] = _fv(value, method, url, evidence, confidence)

    try:
        objects = json_ld_objects(soup)
    except Exception:  # noqa: BLE001 - malformed JSON-LD is common
        objects = []
    for obj in objects:
        if "JobPosting" not in _types(obj):
            continue
        org = obj.get("hiringOrganization") or {}
        put("job_title", _first(obj.get("title"), obj.get("name")), "json-ld", "JobPosting.title", 0.95)
        put("location", _location_of(obj), "json-ld", "JobPosting.jobLocation", 0.95)
        put("department", _first(obj.get("occupationalCategory"), obj.get("industry")), "json-ld",
            "JobPosting.occupationalCategory", 0.9)
        put("employment_type", _first(obj.get("employmentType")), "json-ld", "JobPosting.employmentType", 0.95)
        if str(obj.get("jobLocationType", "")).upper() == "TELECOMMUTE":
            put("remote_mode", "Remote", "json-ld", "JobPosting.jobLocationType=TELECOMMUTE", 0.95)
        put("salary", _salary_of(obj), "json-ld", "JobPosting.baseSalary", 0.95)
        put("posted_date", obj.get("datePosted"), "json-ld", "JobPosting.datePosted", 0.95)
        put("hiring_organization", _first(org.get("name") if isinstance(org, dict) else org), "json-ld",
            "JobPosting.hiringOrganization", 0.95)
        put("company_name", _first(org.get("name") if isinstance(org, dict) else org), "json-ld",
            "JobPosting.hiringOrganization", 0.95)
        description = _clean(BeautifulSoup(str(obj.get("description") or ""), "lxml").get_text(" "), 20000)
        put("description", description, "json-ld", "JobPosting.description", 0.95)
        years = obj.get("experienceRequirements")
        if isinstance(years, dict):
            months = years.get("monthsOfExperience")
            if isinstance(months, (int, float)) and months > 0:
                put("years_experience", int(months // 12), "json-ld", "JobPosting.experienceRequirements", 0.95)
        break

    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        put("job_title", _clean(og_title["content"], 300), "meta", "og:title", 0.7)
    heading = soup.find("h1")
    if heading is not None:
        put("job_title", _clean(heading.get_text(" "), 300), "heading", "<h1>", 0.75)

    text = _main_text(soup)
    if text:
        put("description", text[:20000], "regex", "the page's main text", 0.6)
    body = (out["description"].value if "description" in out else text) or ""
    try:
        from cloud.intel.vendor.salary_extract import extract_salary_from_text

        put("salary", extract_salary_from_text(body[:20000]), "regex", "salary in the posting text", 0.6)
    except Exception:  # noqa: BLE001 - a vendored helper failing must not sink the page
        pass
    try:
        from cloud.intel.vendor.skills_extract import extract_skills_from_text

        put("skills", extract_skills_from_text(body[:20000])[:40], "regex", "skills named in the posting text", 0.6)
    except Exception:  # noqa: BLE001
        pass
    try:
        from cloud.intel.vendor.job_classify import extract_experience_years

        years = extract_experience_years(body[:20000])
        match = re.match(r"(\d+)", str(years or ""))
        if match:
            put("years_experience", int(match.group(1)), "regex", f"experience requirement {years!r}", 0.6)
    except Exception:  # noqa: BLE001
        pass
    certs = [name for name, pattern in _CERT_PATTERNS if pattern.search(body)]
    put("certifications", list(dict.fromkeys(certs))[:20], "regex", "certifications named in the posting text", 0.6)
    try:
        from cloud.intel.technology.service import TechnologyService

        technologies = list(dict.fromkeys(t["technology"] for t in TechnologyService.detect_in_text(body[:50000])))
        put("technology", technologies[:30], "regex", "technology names in the posting text", 0.6)
    except Exception:  # noqa: BLE001
        pass
    location = soup.find(attrs={"class": re.compile(r"location", re.I)})
    if location is not None:
        put("location", _clean(location.get_text(" "), 300), "heading", "location element", 0.7)
    department = soup.find(attrs={"class": re.compile(r"department|team", re.I)})
    if department is not None:
        put("department", _clean(department.get_text(" "), 200), "heading", "department element", 0.7)
    manager = re.search(r"\b(?:Hiring Manager|Recruiter)\s*[:\-–]\s*([A-Z][a-zA-Z'’.-]+(?:\s+[A-Z][a-zA-Z'’-]+){1,2})",
                        text)
    if manager:
        put("hiring_manager", manager.group(1), "regex", manager.group(0)[:120], 0.6)
    return out


def merge_job(listing: Dict[str, FieldValue], detail: Dict[str, FieldValue]) -> Dict[str, FieldValue]:
    """Listing + detail, field by field, by evidence precedence (conflicts kept).

    The job title is the exception: a listing already names the posting, and a detail
    page's ``og:title`` / ``<h1>`` is often decorated ("Job: X at Y | Site"). Only a
    structured ``JobPosting.title`` may replace it; any other detail title is kept as an
    alternative."""
    merged: Dict[str, FieldValue] = dict(listing)
    for name, fv in detail.items():
        if name == "job_title" and "job_title" in merged and fv.method != "json-ld":
            current = merged["job_title"]
            if fv.value != current.value and all(a.value != fv.value for a in current.alternatives):
                current.alternatives.append(FieldValue(fv.value, fv.method, fv.confidence, fv.evidence,
                                                       fv.source_url, fv.browser))
            continue
        merged[name] = merge_value(merged.get(name), fv)
    return merged


def job_detail_urls(jobs: List[Dict[str, FieldValue]]) -> List[Optional[str]]:
    return [job["job_url"].value if "job_url" in job else None for job in jobs]
