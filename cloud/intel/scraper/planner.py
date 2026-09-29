"""Instruction -> extraction schema.

    "Get company name, careers URL, ATS/platform and job titles."
    -> {"entity": "job",
        "fields": [{"name": "company_name", "type": "string", "level": "company", "required": false, ...},
                   {"name": "careers_url", ...}, {"name": "ats", ...},
                   {"name": "job_title", "type": "string", "level": "job", "required": true, ...}],
        "filters": [], "custom": [], "parser": "rules"}

Rules first: the vocabulary in :mod:`cloud.intel.scraper.schemas` plus filters
like "posted in the last 7 days". Any other phrase becomes a *custom* field
(snake-case name, a guessed type) rather than being dropped — the user may ask
for anything. When the workspace allows AI, the model is asked once to name and
type the custom fields; its answer is validated, never trusted blindly, and a
failure (quota, network, refusal) keeps the rule-based schema.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from cloud.intel.scraper.schemas import FIELD_TYPES, FIELDS, SCHEMA_JSON_SCHEMA, field_spec

__all__ = ["JOB_DEFINING", "instruction_to_schema", "plan"]

#: Fields that make a request about job postings (one row per job) rather than companies.
JOB_DEFINING = frozenset({"job_title", "job_url", "posted_date", "department", "employment_type", "remote_mode",
                          "salary"})

_STOP = {"get", "the", "and", "a", "an", "of", "for", "each", "every", "their", "its", "from", "all", "extract",
         "find", "me", "list", "please", "with", "also", "plus", "that", "were", "was", "in", "on", "page",
         "pages", "url", "urls", "this", "these", "those", "include", "including", "give", "return", "show",
         "posted", "last", "days", "day", "week", "weeks", "within", "past", "only", "is", "are", "to", "want",
         "i", "need", "collect", "scrape", "and/or", "any", "available", "there", "if", "site", "sites", "them",
         "it", "they", "what", "which", "who", "can", "you", "could", "would", "should", "into", "out", "by",
         "listed", "published", "current", "open", "each", "per", "etc", "&", "+", "we", "our", "my"}

_WITHIN = re.compile(r"(?:posted|published|listed)?\s*(?:in|within|over)\s+the\s+(?:last|past)\s+(\d{1,3})\s+"
                     r"(days|day|weeks|week)|(?:last|past)\s+(\d{1,3})\s+(days|day|weeks|week)", re.I)


def _phrase_index() -> List[Tuple[str, str]]:
    pairs = [(phrase, name) for name, spec in FIELDS.items() for phrase in spec[3]]
    return sorted(pairs, key=lambda p: -len(p[0]))


_PHRASES = _phrase_index()


def _snake(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:60]


def _guess_type(phrase: str) -> str:
    if re.search(r"\b(url|link|page|site)\b", phrase):
        return "url"
    if "email" in phrase:
        return "email"
    if re.search(r"\b(date|when)\b", phrase):
        return "date"
    if re.search(r"\b(number of|count|how many|revenue|employees|headcount|size)\b", phrase):
        return "number"
    return "string"


def instruction_to_schema(instruction: str, *, ai: Any = None) -> Dict[str, Any]:
    """Parse ``instruction`` into ``{"entity", "fields", "filters", "custom", "parser", "instruction"}``.

    Args:
        instruction: What the user asked for, in plain language.
        ai: An :class:`~cloud.intel.ai.base.AIProvider`, consulted only to name and
            type phrases the rules did not recognise.
    """
    text = " " + re.sub(r"\s+", " ", (instruction or "").lower()) + " "
    filters: List[Dict[str, Any]] = []
    for match in _WITHIN.finditer(text):
        number = int(match.group(1) or match.group(3))
        unit = (match.group(2) or match.group(4)).lower()
        filters.append({"field": "posted_date", "op": "within_days",
                        "value": number * 7 if unit.startswith("week") else number})
    text = _WITHIN.sub(" ", text)

    found: List[str] = []
    for phrase, name in _PHRASES:
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])")
        if pattern.search(text):
            text = pattern.sub(" , ", text)
            if name not in found:
                found.append(name)
    if filters and "posted_date" not in found:
        found.append("posted_date")

    custom: List[str] = []
    raw: Dict[str, str] = {}
    for chunk in re.split(r"[,;:/()]|\band\b|\bor\b|\.(?!\w)", text):
        words = [w for w in re.findall(r"[a-z0-9&+#.'-]+", chunk) if w not in _STOP and not w.isdigit()]
        phrase = " ".join(words).strip(" .-'")
        if phrase and _snake(phrase) and phrase not in custom:
            custom.append(phrase)
            raw[phrase] = chunk

    entity = "job" if any(name in JOB_DEFINING for name in found) else "company"
    if entity == "job" and "job_title" not in found:
        found.append("job_title")   # a job row needs a title, even for "job URLs and locations"
    order = list(FIELDS)
    found.sort(key=order.index)
    fields = [field_spec(name, entity) for name in found]
    names = {f["name"] for f in fields}
    for phrase in custom:
        name = _snake(phrase)
        if name not in names:
            names.add(name)
            fields.append({"name": name, "type": _guess_type(raw[phrase]), "level": entity if entity == "job" else "company",
                           "description": phrase, "required": False, "source": "custom"})
    if not fields:
        fields = [field_spec("title", entity)]
    _mark_required(fields, entity)
    schema: Dict[str, Any] = {"entity": entity, "fields": fields, "filters": filters, "custom": custom,
                              "parser": "rules", "instruction": instruction}
    if custom and ai is not None:
        schema = _refine_with_ai(schema, ai)
    return schema


#: The name :func:`plan` is imported under by the service.
plan = instruction_to_schema


def _mark_required(fields: List[Dict[str, Any]], entity: str) -> None:
    key = "job_title" if entity == "job" else "company_name"
    target = next((f for f in fields if f["name"] == key), None) or fields[0]
    target["required"] = True


def _refine_with_ai(schema: Dict[str, Any], ai: Any) -> Dict[str, Any]:
    """Let a model name and type the custom fields. Keeps the rules' answer on any failure."""
    from cloud.intel.ai.base import AIError

    prompt = ("A web scraper user asked for these things, which are not in its standard field list: "
              + "; ".join(schema["custom"])
              + ". For each one, give a snake_case field name, a type, whether it is a fact about one job posting "
                "(level=job) or about the company (level=company), and a one-line description. Only include things "
                "that can appear on a public web page. Ignore filler words.")
    try:
        answer = ai.complete_json("You design data extraction schemas.", prompt, SCHEMA_JSON_SCHEMA, max_tokens=1500)
    except AIError as error:
        return {**schema, "ai_note": f"AI schema help unavailable: {error}"}
    standard = [f for f in schema["fields"] if f.get("source") != "custom"]
    known = {f["name"] for f in standard}
    added: List[Dict[str, Any]] = []
    for item in (answer.get("fields") or [])[:20]:
        name = _snake(item.get("name", ""))
        kind = item.get("type") if item.get("type") in FIELD_TYPES else "string"
        if not name or name in known:
            continue
        if name in FIELDS:   # the model mapped a phrase onto a standard field
            spec = field_spec(name, schema["entity"], source="ai")
        else:
            level = "job" if item.get("level") == "job" and schema["entity"] == "job" else "company"
            spec = {"name": name, "type": kind, "level": level, "description": str(item.get("description", ""))[:300],
                    "required": False, "source": "ai"}
        known.add(name)
        added.append(spec)
    if not added:
        return schema
    fields = standard + added
    for f in fields:
        f["required"] = False
    _mark_required(fields, schema["entity"])
    return {**schema, "fields": fields, "parser": f"rules+{getattr(ai, 'name', 'ai')}"}
