"""The field vocabulary the planner recognises, field types, and the JSON schemas sent to AI.

Nothing here limits what a user may ask for: a phrase the vocabulary does not
know becomes a custom field (see :mod:`cloud.intel.scraper.planner`). The
vocabulary only means that common fields get a type, a description, a
normalisation rule and a deterministic extractor without asking a model.

A field in a schema::

    {"name": "employee_count", "label": "Employee count", "type": "integer",
     "level": "company", "required": false, "description": "...", "source": "rules",
     "normalize": "integer", "enum": null, "pattern": null, "max_length": null, "hint": null}
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = ["COMPANY_FIELDS", "FIELD_TYPES", "FIELDS", "JOB_FIELDS", "LEGACY_TYPES", "NORMALIZE_RULES",
           "SCHEMA_JSON_SCHEMA", "SENIORITY", "ai_fields_schema", "ai_jobs_schema", "canonical_type", "field_spec"]

#: The value types a field may have.
FIELD_TYPES = ("string", "integer", "decimal", "boolean", "date", "datetime", "url", "email", "phone", "enum",
               "array", "object")
#: V1 type names, still accepted.
LEGACY_TYPES = {"number": "decimal", "list": "array", "int": "integer", "float": "decimal", "bool": "boolean",
                "text": "string"}
NORMALIZE_RULES = ("trim", "lower", "upper", "url", "website", "domain", "email", "phone", "iso_date", "iso_datetime",
                   "company_name", "job_title", "location", "integer", "decimal")
SENIORITY = ("Intern", "Entry", "Mid", "Senior", "Lead", "Manager", "Director", "VP", "Executive")
_DEFAULT_NORMALIZE = {"url": "url", "email": "email", "phone": "phone", "date": "iso_date", "datetime": "iso_datetime",
                      "integer": "integer", "decimal": "decimal"}


def canonical_type(kind: Any) -> str:
    text = str(kind or "string").lower()
    text = LEGACY_TYPES.get(text, text)
    return text if text in FIELD_TYPES else "string"


#: name -> (type, level, description, phrases, extra). ``level`` is "job" for facts about
#: one posting, "company" for facts about the page's organisation and "any" for fields
#: that follow the request. The longest matching phrase wins, so "job url" beats "job".
FIELDS: Dict[str, Tuple[str, str, str, Tuple[str, ...], Dict[str, Any]]] = {
    "company_name": ("string", "company", "The company's name",
                     ("company name", "company names", "company", "companies", "employer", "organization name",
                      "organisation name", "business name", "organization", "organisation"),
                     {"normalize": "company_name"}),
    "website": ("url", "company", "The company's main website",
                ("company website", "website", "websites", "homepage", "home page", "web site", "company url",
                 "company site"), {"normalize": "website"}),
    "domain": ("string", "company", "The company's registrable domain", ("domain", "domains", "domain name"),
               {"normalize": "domain"}),
    "careers_url": ("url", "company", "The careers / jobs page URL",
                    ("careers url", "careers page", "career page", "careers link", "jobs page", "career url",
                     "careers site", "career site", "careers", "job board"), {}),
    "ats": ("string", "company", "Applicant tracking system / job platform behind the careers page",
            ("ats/platform", "ats", "applicant tracking system", "applicant tracking", "job platform",
             "hiring platform", "recruiting platform", "platform"), {}),
    "location": ("string", "any", "Location (city, region, country)",
                 ("location", "locations", "city", "address", "office location", "job location"),
                 {"normalize": "location"}),
    "headquarters": ("string", "company", "Headquarters location", ("headquarters", "hq", "head office"),
                     {"normalize": "location"}),
    "industry": ("string", "company", "Industry", ("industry", "industries", "sector"), {}),
    "employee_count": ("integer", "company", "Number of employees",
                       ("employee count", "number of employees", "employees", "headcount", "company size",
                        "employee size", "staff count"), {}),
    "revenue": ("decimal", "company", "Annual revenue as published", ("revenue", "annual revenue", "turnover"), {}),
    "founded_year": ("integer", "company", "Year the company was founded",
                     ("founded year", "year founded", "founded", "founding year"), {}),
    "erp": ("string", "company", "ERP system(s) named on the page",
            ("erp system", "erp systems", "erp software", "erp"), {}),
    "cloud_provider": ("string", "company", "Cloud provider(s) named on the page",
                       ("cloud provider", "cloud providers", "cloud platform"), {}),
    "contact_page": ("url", "company", "The contact page URL",
                     ("contact page", "contact url", "contact us page", "contact link", "contact us"), {}),
    "email": ("email", "company", "Email address published on the page",
              ("email", "emails", "email address", "email addresses", "contact email"), {}),
    "phone": ("phone", "company", "Phone number published on the page",
              ("phone", "phones", "phone number", "phone numbers", "telephone"), {}),
    "linkedin_url": ("url", "company", "The company's LinkedIn page",
                     ("linkedin url", "linkedin page", "linkedin", "linkedin profile"), {}),
    "social_links": ("array", "company", "Public social media profile links",
                     ("social links", "social media links", "social media", "social profiles", "socials"), {}),
    "ceo": ("string", "company", "Name of the chief executive, as published on the page",
            ("ceo", "chief executive", "chief executive officer"), {}),
    "hiring_manager": ("string", "any", "Hiring manager / recruiter named on the page",
                       ("hiring manager", "hiring managers", "recruiter", "recruiters", "hiring contact"), {}),
    "decision_maker": ("string", "company", "Decision maker named on the page", ("decision maker", "decision makers"),
                       {}),
    "technology": ("array", "any", "Technologies / software named on the page",
                   ("technology", "technologies", "tech stack", "software", "tools"), {}),
    "job_title": ("string", "job", "A job posting's title",
                  ("job post titles", "job post title", "job posting titles", "job posting title", "job title",
                   "job titles", "titles", "title of jobs", "job", "jobs", "position", "positions", "role", "roles",
                   "openings", "vacancies", "job postings", "job posts", "open jobs", "open positions"),
                  {"normalize": "job_title"}),
    "job_url": ("url", "job", "Link to the job posting",
                ("job url", "job urls", "job link", "job links", "posting url", "apply link", "apply url",
                 "job page"), {}),
    "posted_date": ("date", "job", "When the job was posted",
                    ("posted date", "date posted", "post date", "posting date", "posted on", "publish date"), {}),
    "department": ("string", "job", "Department / team of the job", ("department", "departments", "team", "teams"),
                   {}),
    "seniority": ("enum", "job", "Seniority level", ("seniority", "seniority level", "level", "job level"),
                  {"enum": list(SENIORITY)}),
    "job_family": ("string", "job", "Job family (engineering, sales, finance…)", ("job family", "job category",
                                                                                  "job categories", "function"), {}),
    "employment_type": ("string", "job", "Full-time, part-time, contract…",
                        ("employment type", "job type", "job types", "contract type"), {}),
    "remote_mode": ("enum", "job", "Remote, hybrid or on-site",
                    ("remote mode", "remote", "remote status", "work mode", "hybrid", "on-site", "onsite",
                     "remote/hybrid/on-site", "workplace type"), {"enum": ["Remote", "Hybrid", "On-site"]}),
    "salary": ("string", "job", "Salary / pay range as published",
               ("salary", "salaries", "pay", "pay range", "compensation", "salary range"), {}),
    "skills": ("array", "job", "Skills the posting asks for", ("skills", "skill", "required skills", "skill set"), {}),
    "years_experience": ("integer", "job", "Minimum years of experience asked for",
                         ("years of experience", "experience years", "years experience", "experience"), {}),
    "certifications": ("array", "job", "Certifications the posting asks for",
                       ("certifications", "certification", "certificates", "certs"), {}),
    "hiring_organization": ("string", "job", "Hiring organisation / team named on the posting",
                            ("hiring organization", "hiring organisation", "hiring team", "hiring company"), {}),
    "description": ("string", "any", "Description text", ("description", "descriptions", "summary", "about",
                                                          "job description"), {"max_length": 20000}),
    "title": ("string", "company", "The page title", ("page title", "title"), {}),
}

JOB_FIELDS = frozenset(name for name, spec in FIELDS.items() if spec[1] == "job")
COMPANY_FIELDS = frozenset(name for name, spec in FIELDS.items() if spec[1] == "company")


def field_spec(name: str, entity: str, *, required: bool = False, source: str = "rules") -> Dict[str, Any]:
    kind, level, description, _, extra = FIELDS[name]
    if level == "any":
        level = "job" if entity == "job" else "company"
    spec = {"name": name, "label": name.replace("_", " ").capitalize(), "type": kind, "level": level,
            "description": description, "required": required, "source": source,
            "normalize": extra.get("normalize") or _DEFAULT_NORMALIZE.get(kind), "enum": extra.get("enum"),
            "pattern": extra.get("pattern"), "max_length": extra.get("max_length"), "hint": None}
    return spec


#: What the planner asks a model for when phrases are not in the vocabulary. Every
#: field must name the user's phrase it came from; fields for other phrases are dropped.
SCHEMA_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {"type": "array", "items": {
            "type": "object",
            "properties": {"phrase": {"type": "string"},
                           "name": {"type": "string"},
                           "type": {"type": "string", "enum": list(FIELD_TYPES)},
                           "level": {"type": "string", "enum": ["company", "job"]},
                           "description": {"type": "string"},
                           "enum": {"type": ["array", "null"], "items": {"type": "string"}},
                           "hint": {"type": ["string", "null"]},
                           "normalize": {"type": ["string", "null"]}},
            "required": ["phrase", "name", "type", "level", "description"], "additionalProperties": False}},
    },
    "required": ["fields"], "additionalProperties": False,
}


def ai_fields_schema(fields: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Page-level extraction: each field is ``{value, evidence}`` (or null) so a value
    can be checked against the page before it is kept."""
    item = {"type": ["object", "null"], "additionalProperties": False, "required": ["value", "evidence"],
            "properties": {"value": {"type": ["string", "null"]},
                           "evidence": {"type": ["string", "null"]}}}
    return {"type": "object", "additionalProperties": False, "required": [f["name"] for f in fields],
            "properties": {f["name"]: item for f in fields}}


def ai_jobs_schema(fields: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """A list of job postings with the requested job-level fields (title and URL always)."""
    names: List[str] = ["job_title", "job_url"] + [f["name"] for f in fields
                                                   if f["name"] not in ("job_title", "job_url")]
    props = {name: {"type": ["string", "null"]} for name in names}
    return {"type": "object", "additionalProperties": False, "required": ["jobs"],
            "properties": {"jobs": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["job_title", "job_url"],
                "properties": props}}}}


def optional(value: Any) -> Optional[Any]:
    return value if value not in (None, "", []) else None
