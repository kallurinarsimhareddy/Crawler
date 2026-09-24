"""Turning a plain-English instruction into an extraction schema.

    "Get company name, careers URL and ATS."
    -> {"entity": "company",
        "fields": [{"name": "company_name", ...}, {"name": "careers_url", ...}, {"name": "ats", ...}],
        "filters": [], "unknown": []}

The deterministic parser recognises the field vocabulary in :data:`FIELDS` and
filters like "posted in the last 7 days". Anything it does not recognise is
listed in ``unknown``. Only then, and only when the workspace allows external
AI, a model is asked to describe the unknown fields — its answer is validated
and merged, never trusted blindly. The schema is always shown to the user
before a run starts.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["FIELDS", "instruction_to_schema", "SCHEMA_JSON_SCHEMA"]

#: name -> (type, description, phrases that ask for it). Longest phrases win.
FIELDS: Dict[str, Tuple[str, str, Tuple[str, ...]]] = {
    "company_name": ("string", "The company's name", ("company name", "company names", "company", "companies",
                                                     "employer", "organization name", "business name")),
    "website": ("url", "The company's main website", ("website", "websites", "homepage", "web site", "domain",
                                                     "company url", "company site")),
    "job_title": ("string", "A job posting's title", ("job title", "job titles", "titles", "job", "jobs",
                                                     "position", "positions", "role", "roles", "openings")),
    "location": ("string", "Location (city, region, country)", ("location", "locations", "city", "address",
                                                               "headquarters", "hq")),
    "careers_url": ("url", "The careers / jobs page URL", ("careers url", "careers page", "career page",
                                                          "careers link", "jobs page", "career url", "careers")),
    "ats": ("string", "Applicant tracking system behind the jobs page", ("ats", "applicant tracking system",
                                                                        "applicant tracking")),
    "ceo": ("string", "Name of the chief executive, as published on the page", ("ceo", "chief executive",
                                                                                  "chief executive officer")),
    "linkedin_url": ("url", "The company's LinkedIn page", ("linkedin url", "linkedin page", "linkedin",
                                                           "linkedin profile")),
    "email": ("email", "Email address published on the page", ("email", "emails", "email address",
                                                              "contact email")),
    "phone": ("string", "Phone number published on the page", ("phone", "phones", "phone number", "telephone")),
    "posted_date": ("date", "When the job was posted", ("posted date", "date posted", "post date", "posting date")),
    "description": ("string", "Description text", ("description", "summary", "about")),
    "industry": ("string", "Industry", ("industry", "sector")),
    "job_url": ("url", "Link to the job posting", ("job url", "job link", "posting url", "apply link")),
    "employment_type": ("string", "Full-time, part-time, contract…", ("employment type", "job type")),
}

_JOB_FIELDS = {"job_title", "posted_date", "job_url", "employment_type"}
_STOP = {"get", "the", "and", "a", "an", "of", "for", "each", "every", "their", "its", "from", "all", "extract",
         "find", "me", "list", "please", "with", "also", "plus", "that", "were", "was", "in", "on", "page",
         "pages", "url", "urls", "this", "these", "those", "include", "including", "give", "return", "show",
         "posted", "last", "days", "day", "week", "weeks", "within", "past", "only", "is", "are", "to", "want",
         "i", "need", "collect", "scrape", "and/or"}

_WITHIN = re.compile(r"(?:posted|published|listed)?\s*(?:in|within|over)\s+the\s+(?:last|past)\s+(\d{1,3})\s+(days|day|weeks|week)"
                     r"|(?:last|past)\s+(\d{1,3})\s+(days|day|weeks|week)", re.I)

SCHEMA_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "type": {"type": "string",
                           "enum": ["string", "url", "email", "date", "number", "boolean"]},
                           "description": {"type": "string"}},
            "required": ["name", "type", "description"], "additionalProperties": False}},
    },
    "required": ["fields"], "additionalProperties": False,
}


def _phrase_index() -> List[Tuple[str, str]]:
    pairs = [(phrase, name) for name, (_t, _d, phrases) in FIELDS.items() for phrase in phrases]
    return sorted(pairs, key=lambda p: -len(p[0]))


def _snake(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60]


def instruction_to_schema(instruction: str, *, ai: Any = None) -> Dict[str, Any]:
    """Parse ``instruction`` into ``{"entity","fields","filters","unknown","parser"}``.

    Args:
        instruction: What the user asked for.
        ai: An :class:`~cloud.intel.ai.base.AIProvider`, consulted only for
            phrases the rules did not understand.
    """
    text = " " + re.sub(r"\s+", " ", (instruction or "").lower()) + " "
    filters: List[Dict[str, Any]] = []
    for match in _WITHIN.finditer(text):
        number = int(match.group(1) or match.group(3))
        unit = (match.group(2) or match.group(4)).lower()
        days = number * 7 if unit.startswith("week") else number
        filters.append({"field": "posted_date", "op": "within_days", "value": days})
    text = _WITHIN.sub(" ", text)

    found: List[str] = []
    for phrase, name in _phrase_index():
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])")
        if pattern.search(text):
            text = pattern.sub(" ", text)
            if name not in found:
                found.append(name)

    # What is left: split on separators and drop filler words.
    unknown: List[str] = []
    for chunk in re.split(r"[,;:/]|\band\b|\bor\b|\.", text):
        words = [w for w in re.findall(r"[a-z0-9&+#.-]+", chunk) if w not in _STOP and not w.isdigit()]
        if words:
            unknown.append(" ".join(words))

    if filters and "posted_date" not in found:
        found.append("posted_date")
    # Job-shaped requests need a title even if the user only said "jobs".
    order = list(FIELDS)
    found.sort(key=order.index)
    fields = [{"name": n, "type": FIELDS[n][0], "description": FIELDS[n][1], "source": "rules"} for n in found]
    entity = "job" if any(f in _JOB_FIELDS for f in found) else ("company" if found else "page")
    schema = {"entity": entity, "fields": fields, "filters": filters, "unknown": unknown, "parser": "rules",
              "instruction": instruction}

    if unknown and ai is not None:
        schema = _extend_with_ai(schema, ai)
    if not schema["fields"]:
        schema["fields"] = [{"name": "title", "type": "string", "description": "Page title", "source": "rules"}]
    return schema


def _extend_with_ai(schema: Dict[str, Any], ai: Any) -> Dict[str, Any]:
    from cloud.intel.ai.base import AIError

    prompt = ("The user asked a web scraper to collect these things it did not recognise: "
              + "; ".join(schema["unknown"])
              + ". Describe each as an extraction field with a snake_case name, a type and a one-line "
                "description. Only include things that could appear on a public web page.")
    try:
        answer = ai.complete_json("You design data extraction schemas.", prompt, SCHEMA_JSON_SCHEMA, max_tokens=1500)
    except AIError:
        return schema
    known = {f["name"] for f in schema["fields"]}
    added = []
    for field in answer.get("fields", [])[:20]:
        name = _snake(field.get("name", ""))
        if name and name not in known:
            known.add(name)
            added.append({"name": name, "type": field["type"], "description": field["description"][:300],
                          "source": "ai"})
    if added:
        schema = {**schema, "fields": schema["fields"] + added, "unknown": [], "parser": f"rules+{ai.name}"}
    return schema
