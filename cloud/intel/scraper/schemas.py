"""The field vocabulary the planner recognises, and the JSON schemas sent to AI.

Nothing here limits what a user may ask for: a phrase the vocabulary does not
know becomes a custom field (see :mod:`cloud.intel.scraper.planner`). The
vocabulary only means that common fields get a type, a description and a
deterministic extractor without asking a model.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple

__all__ = ["COMPANY_FIELDS", "FIELD_TYPES", "FIELDS", "JOB_FIELDS", "ai_fields_schema", "ai_jobs_schema",
           "SCHEMA_JSON_SCHEMA", "field_spec"]

#: The value types a field may have. ``list`` values are exported joined by " | ".
FIELD_TYPES = ("string", "url", "email", "date", "number", "boolean", "list")

#: name -> (type, level, description, phrases that ask for it). ``level`` is "job" for
#: facts about one posting, "company" for facts about the page's organisation and
#: "any" for fields that follow the request (a job's location, or the company's).
#: The longest matching phrase wins, so "job url" beats "job".
FIELDS: Dict[str, Tuple[str, str, str, Tuple[str, ...]]] = {
    "company_name": ("string", "company", "The company's name",
                     ("company name", "company names", "company", "companies", "employer", "organization name",
                      "organisation name", "business name", "organization", "organisation")),
    "website": ("url", "company", "The company's main website",
                ("company website", "website", "websites", "homepage", "home page", "web site", "company url",
                 "company site")),
    "domain": ("string", "company", "The company's registrable domain", ("domain", "domains", "domain name")),
    "careers_url": ("url", "company", "The careers / jobs page URL",
                    ("careers url", "careers page", "career page", "careers link", "jobs page", "career url",
                     "careers site", "career site", "careers")),
    "ats": ("string", "company", "Applicant tracking system / job platform behind the careers page",
            ("ats/platform", "ats", "applicant tracking system", "applicant tracking", "job platform",
             "hiring platform", "recruiting platform", "platform")),
    "location": ("string", "any", "Location (city, region, country)",
                 ("location", "locations", "city", "address", "headquarters", "hq", "office location")),
    "industry": ("string", "company", "Industry", ("industry", "industries", "sector")),
    "contact_page": ("url", "company", "The contact page URL",
                     ("contact page", "contact url", "contact us page", "contact link", "contact us")),
    "email": ("email", "company", "Email address published on the page",
              ("email", "emails", "email address", "email addresses", "contact email")),
    "phone": ("string", "company", "Phone number published on the page",
              ("phone", "phones", "phone number", "phone numbers", "telephone")),
    "linkedin_url": ("url", "company", "The company's LinkedIn page",
                     ("linkedin url", "linkedin page", "linkedin", "linkedin profile")),
    "social_links": ("list", "company", "Public social media profile links",
                     ("social links", "social media links", "social media", "social profiles", "socials")),
    "ceo": ("string", "company", "Name of the chief executive, as published on the page",
            ("ceo", "chief executive", "chief executive officer")),
    "technology": ("list", "company", "Technologies / software named on the page",
                   ("technology", "technologies", "tech stack", "software", "tools")),
    "job_title": ("string", "job", "A job posting's title",
                  ("job post titles", "job post title", "job posting titles", "job posting title", "job title",
                   "job titles", "titles", "title of jobs", "job", "jobs", "position", "positions", "role", "roles",
                   "openings", "vacancies", "job postings", "job posts")),
    "job_url": ("url", "job", "Link to the job posting",
                ("job url", "job urls", "job link", "job links", "posting url", "apply link", "apply url",
                 "job page")),
    "posted_date": ("date", "job", "When the job was posted",
                    ("posted date", "date posted", "post date", "posting date", "posted on", "publish date")),
    "department": ("string", "job", "Department / team of the job", ("department", "departments", "team", "teams")),
    "employment_type": ("string", "job", "Full-time, part-time, contract…",
                        ("employment type", "job type", "job types", "contract type")),
    "remote_mode": ("string", "job", "Remote, hybrid or on-site",
                    ("remote mode", "remote", "remote status", "work mode", "hybrid", "on-site", "onsite")),
    "salary": ("string", "job", "Salary / pay range as published",
               ("salary", "salaries", "pay", "pay range", "compensation", "salary range")),
    "description": ("string", "any", "Description text", ("description", "descriptions", "summary", "about")),
    "title": ("string", "company", "The page title", ("page title", "title")),
}

JOB_FIELDS = frozenset(name for name, spec in FIELDS.items() if spec[1] == "job")
COMPANY_FIELDS = frozenset(name for name, spec in FIELDS.items() if spec[1] == "company")


def field_spec(name: str, entity: str, *, required: bool = False, source: str = "rules") -> Dict[str, Any]:
    kind, level, description, _ = FIELDS[name]
    if level == "any":
        level = "job" if entity == "job" else "company"
    return {"name": name, "type": kind, "level": level, "description": description, "required": required,
            "source": source}


#: What the planner asks a model for when a phrase is not in the vocabulary.
SCHEMA_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"},
                           "type": {"type": "string", "enum": list(FIELD_TYPES)},
                           "level": {"type": "string", "enum": ["company", "job"]},
                           "description": {"type": "string"}},
            "required": ["name", "type", "level", "description"], "additionalProperties": False}},
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
