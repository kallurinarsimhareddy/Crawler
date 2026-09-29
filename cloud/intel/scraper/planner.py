"""Instruction -> extraction schema.

    "Find US manufacturing companies using SAP, include company name, website, ERP, industry,
     employee count, hiring manager, and all open SAP jobs posted in the last 14 days."
    -> {"entity": "job", "entities": ["company", "job"],
        "fields": [company_name, website, industry, employee_count (integer), erp, hiring_manager,
                   job_title (required), posted_date (date)],
        "filters": [{"field": "posted_date", "op": "within_days", "value": 14, "mode": "hard"},
                    {"field": "job_title", "op": "contains_any", "value": ["SAP"], "mode": "hard"}],
        "criteria": {"country": "US", "industries": ["manufacturing"], "technologies": ["SAP"]}, ...}

Rules first: the vocabulary in :mod:`cloud.intel.scraper.schemas`, date filters
("posted in the last 14 days"), keyword filters ("SAP jobs"), and research
criteria (country, industry, technology) that are shown and used to match, but
never used to drop a row the page did not describe. Any other phrase becomes a
*custom* field with a guessed type ("number of X" -> integer, "uses X?" ->
boolean, "... date" -> date, "... URL" -> url…). When the workspace allows AI, a
model may name, type and add hints to the custom fields — each answer must name
the user's phrase it came from, so AI can never add a field the user did not ask
for. A failure (quota, network, refusal) keeps the rule-based schema.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from cloud.intel.scraper.schemas import (FIELD_TYPES, FIELDS, NORMALIZE_RULES, SCHEMA_JSON_SCHEMA, canonical_type,
                                         field_spec)

__all__ = ["JOB_DEFINING", "instruction_to_schema", "plan", "custom_field"]

#: Fields that make a request about job postings (one row per job) rather than companies.
JOB_DEFINING = frozenset({"job_title", "job_url", "posted_date", "department", "employment_type", "remote_mode",
                          "salary", "seniority", "skills", "years_experience", "certifications", "job_family",
                          "hiring_organization"})

_STOP = {"get", "the", "and", "a", "an", "of", "for", "each", "every", "their", "its", "from", "all", "extract",
         "find", "me", "list", "please", "with", "also", "plus", "that", "were", "was", "in", "on", "page",
         "pages", "url", "urls", "this", "these", "those", "include", "including", "give", "return", "show",
         "posted", "last", "days", "day", "week", "weeks", "within", "past", "only", "is", "are", "to", "want",
         "i", "need", "collect", "scrape", "and/or", "any", "available", "there", "if", "site", "sites", "them",
         "it", "they", "what", "which", "who", "can", "you", "could", "would", "should", "into", "out", "by",
         "listed", "published", "current", "open", "each", "per", "etc", "&", "+", "we", "our", "my", "using",
         "use", "uses", "based", "located", "headquartered", "month", "months", "year", "years", "new", "recent",
         "recently", "latest", "hiring", "companies", "company", "firms", "businesses"}

_WITHIN = re.compile(r"(?:posted|published|listed)?\s*(?:in|within|over)\s+the\s+(?:last|past)\s+(\d{1,3})\s+"
                     r"(days|day|weeks|week|months|month)|(?:last|past)\s+(\d{1,3})\s+(days|day|weeks|week|months|month)",
                     re.I)
_COUNTRIES = {"us": "US", "u.s.": "US", "usa": "US", "united states": "US", "american": "US", "uk": "GB",
              "united kingdom": "GB", "british": "GB", "canada": "CA", "canadian": "CA", "india": "IN",
              "indian": "IN", "germany": "DE", "german": "DE", "australia": "AU", "mexico": "MX"}
_INDUSTRIES = ("manufacturing", "healthcare", "finance", "financial services", "banking", "insurance", "retail",
               "logistics", "transportation", "energy", "utilities", "construction", "education", "government",
               "technology", "software", "pharmaceutical", "automotive", "aerospace", "telecom", "hospitality",
               "real estate", "media", "food", "chemicals", "distribution", "wholesale")
_TECH_PATTERN = re.compile(r"\b(?:using|use|uses|running|on|with)\s+([A-Z][A-Za-z0-9/+.&-]{1,30}(?:\s+[A-Z0-9][A-Za-z0-9/+.&-]*)?)")
_KEYWORD_JOBS = re.compile(r"\b(?:open\s+|all\s+|new\s+)*([A-Z][A-Za-z0-9/+.#&-]{1,30})\s+(?:jobs|roles|positions|openings)\b")
_NOT_KEYWORDS = {"All", "Open", "New", "The", "Job", "Jobs", "Any", "US", "USA", "Get", "Find", "Extract", "Company"}


def _phrase_index() -> List[Tuple[str, str]]:
    pairs = [(phrase, name) for name, spec in FIELDS.items() for phrase in spec[3]]
    return sorted(pairs, key=lambda p: -len(p[0]))


_PHRASES = _phrase_index()


def _snake(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:60]


def _guess_type(raw: str) -> Tuple[str, Optional[str]]:
    """``(type, hint)`` for a custom phrase, from its wording."""
    text = raw.strip().lower()
    uses = re.match(r"^(?:uses|use|using|has|have|is|does it use|runs|supports)\s+(.+?)\??$", text)
    if uses or text.endswith("?"):
        return "boolean", (uses.group(1) if uses else text.rstrip("?")).strip()
    if re.search(r"\b(number of|count|how many|headcount|size)\b", text):
        return "integer", None
    if re.search(r"\b(revenue|amount|price|cost|budget|rate)\b", text):
        return "decimal", None
    if re.search(r"\b(date|when|deadline)\b", text):
        return "date", None
    if re.search(r"\b(url|link|page|site|website)\b", text):
        return "url", None
    if "email" in text:
        return "email", None
    if re.search(r"\b(phone|telephone|mobile)\b", text):
        return "phone", None
    if re.search(r"\b(list of|lists|technologies|partners|providers|tools)\b", text):
        return "array", None
    return "string", None


def custom_field(phrase: str, raw: str, entity: str) -> Dict[str, Any]:
    kind, hint = _guess_type(raw or phrase)
    name = _snake(phrase)
    if kind == "boolean" and not name.startswith(("uses_", "has_", "is_")):
        name = _snake("uses " + (hint or phrase))
    label = re.sub(r"\s+", " ", raw or phrase).strip(" ,.;:")
    return {"name": name, "label": (label[:1].upper() + label[1:])[:120], "type": kind, "level": "company",
            "description": phrase, "required": False, "source": "custom",
            "normalize": {"integer": "integer", "decimal": "decimal", "date": "iso_date", "url": "url",
                          "email": "email", "phone": "phone"}.get(kind), "enum": None, "pattern": None,
            "max_length": None, "hint": hint}


_INDUSTRY_NOUNS = (r"(?:companies|company|firms|businesses|manufacturers|providers|organizations|organisations|"
                   r"employers|brands)")


def _industry_pattern(industry: str) -> "re.Pattern[str]":
    word = re.escape(industry)
    return re.compile(r"\b(?:in|within)\s+(?:the\s+)?" + word + r"\s+(?:sector|industry|space)\b"
                      r"|\b" + word + r"\s+(?:sector|industry)\b"
                      r"|\b" + word + r"(?=\s+" + _INDUSTRY_NOUNS + r"\b)")


def _criteria(original: str, lowered: str) -> Dict[str, Any]:
    criteria: Dict[str, Any] = {}
    for word, code in _COUNTRIES.items():
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", lowered):
            criteria["country"] = code
            break
    # An industry is a criterion only where it describes the companies ("manufacturing companies",
    # "in the healthcare sector"); "technology" on its own is a field, not an industry.
    industries = [i for i in _INDUSTRIES if _industry_pattern(i).search(lowered)]
    if industries:
        criteria["industries"] = industries
    techs = [m.group(1).strip() for m in _TECH_PATTERN.finditer(original)
             if m.group(1).split()[0] not in _NOT_KEYWORDS]
    if techs:
        criteria["technologies"] = list(dict.fromkeys(techs))
    return criteria


def instruction_to_schema(instruction: str, *, ai: Any = None) -> Dict[str, Any]:
    """Parse ``instruction`` into a schema (see the module docstring for the shape).

    Args:
        instruction: What the user asked for, in plain language.
        ai: An :class:`~cloud.intel.ai.base.AIProvider`, consulted only to name,
            type and hint phrases the rules did not recognise.
    """
    original = re.sub(r"\s+", " ", instruction or "").strip()
    text = " " + original.lower() + " "
    filters: List[Dict[str, Any]] = []
    for match in _WITHIN.finditer(text):
        number = int(match.group(1) or match.group(3))
        unit = (match.group(2) or match.group(4)).lower()
        days = number * (7 if unit.startswith("week") else 30 if unit.startswith("month") else 1)
        filters.append({"field": "posted_date", "op": "within_days", "value": days, "mode": "hard"})
    text = _WITHIN.sub(" ", text)
    keywords = [m.group(1) for m in _KEYWORD_JOBS.finditer(original) if m.group(1) not in _NOT_KEYWORDS
                and m.group(1).lower() not in FIELDS]
    criteria = _criteria(original, text)
    # Criteria words are not fields: "US manufacturing companies using SAP".
    for word in [w for w, _ in _COUNTRIES.items() if criteria.get("country")]:
        text = re.sub(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", " ", text)
    for industry in criteria.get("industries", []):
        text = _industry_pattern(industry).sub(" ", text)
    for tech in criteria.get("technologies", []):
        text = re.sub(r"\b(?:using|use|uses|running|on|with)\s+" + re.escape(tech.lower()) + r"\b", " ", text)
    for keyword in keywords:
        text = re.sub(r"\b" + re.escape(keyword.lower()) + r"(?=\s+(?:jobs|roles|positions|openings)\b)", " ", text)

    found: List[str] = []
    for phrase, name in _PHRASES:
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])")
        if pattern.search(text):
            text = pattern.sub(" , ", text)
            if name not in found:
                found.append(name)
    if any(f["field"] == "posted_date" for f in filters) and "posted_date" not in found:
        found.append("posted_date")

    custom: List[str] = []
    raw: Dict[str, str] = {}
    for chunk in re.split(r"[,;:/()]|\band\b|\bor\b|\.(?!\w)", text):
        words = [w for w in re.findall(r"[a-z0-9&+#.'?-]+", chunk) if w.strip("?") not in _STOP and not w.isdigit()]
        phrase = " ".join(words).strip(" .-'")
        if phrase.strip("?") and _snake(phrase) and phrase not in custom:
            custom.append(phrase)
            raw[phrase] = chunk

    entity = "job" if any(name in JOB_DEFINING for name in found) or keywords else "company"
    if entity == "job" and "job_title" not in found:
        found.append("job_title")   # a job row needs a title, even for "job URLs and locations"
    order = list(FIELDS)
    found.sort(key=order.index)
    fields = [field_spec(name, entity) for name in found]
    names = {f["name"] for f in fields}
    for phrase in custom:
        spec = custom_field(phrase, raw[phrase], entity)
        if spec["name"] and spec["name"] not in names:
            names.add(spec["name"])
            fields.append(spec)
    if not fields:
        fields = [field_spec("title", entity)]
    if keywords:
        filters.append({"field": "job_title", "op": "contains_any", "value": keywords, "mode": "hard"})
    _mark_required(fields, entity)
    entities = ["company", "job"] if entity == "job" and any(f["level"] == "company" for f in fields) else [entity]
    schema: Dict[str, Any] = {"version": 3, "entity": entity, "entities": entities, "fields": fields,
                              "filters": filters, "criteria": criteria, "custom": custom, "parser": "rules",
                              "instruction": instruction}
    if custom and ai is not None:
        schema = _refine_with_ai(schema, ai)
    return schema


#: The name :func:`plan` is imported under by the service.
plan = instruction_to_schema


def _mark_required(fields: List[Dict[str, Any]], entity: str) -> None:
    if any(f.get("required") for f in fields):
        return
    key = "job_title" if entity == "job" else "company_name"
    target = next((f for f in fields if f["name"] == key), None) or fields[0]
    target["required"] = True


def _refine_with_ai(schema: Dict[str, Any], ai: Any) -> Dict[str, Any]:
    """Let a model name, type and hint the custom fields. Keeps the rules' answer on any failure,
    and ignores any field that does not name one of the user's own phrases."""
    from cloud.intel.ai.base import AIError

    prompt = ("A web scraper user asked for these things, which are not in its standard field list: "
              + "; ".join(f'"{p}"' for p in schema["custom"])
              + ". For each one, give: phrase (exactly as quoted above), a snake_case field name, a type, whether it "
                "is a fact about one job posting (level=job) or about the company (level=company), a one-line "
                "description, the allowed values if it is an enum, a short extraction hint, and a normalisation rule "
                f"from {list(NORMALIZE_RULES)} or null. Only include things that can appear on a public web page. "
                "Do not add fields for anything that was not quoted.")
    try:
        answer = ai.complete_json("You design data extraction schemas.", prompt, SCHEMA_JSON_SCHEMA, max_tokens=1500)
    except AIError as error:
        return {**schema, "ai_note": f"AI schema help unavailable: {error}"}
    by_phrase = {p.lower(): p for p in schema["custom"]}
    standard = [f for f in schema["fields"] if f.get("source") != "custom"]
    custom_specs = {f["description"].lower(): f for f in schema["fields"] if f.get("source") == "custom"}
    known = {f["name"] for f in standard}
    ignored = 0
    for item in (answer.get("fields") or [])[:40]:
        phrase = str(item.get("phrase") or "").strip().lower()
        name = _snake(item.get("name", ""))
        if phrase not in by_phrase or not name or phrase not in custom_specs:
            ignored += 1
            continue
        if name in FIELDS and name not in known:   # the model mapped a phrase onto a standard field
            spec = field_spec(name, schema["entity"], source="ai")
        else:
            kind = canonical_type(item.get("type"))
            level = "job" if item.get("level") == "job" and schema["entity"] == "job" else "company"
            normalize = item.get("normalize") if item.get("normalize") in NORMALIZE_RULES else None
            enum = [str(v)[:100] for v in (item.get("enum") or [])][:50] if kind == "enum" else None
            if kind == "enum" and not enum:
                kind = "string"
            spec = {**custom_specs[phrase], "name": name, "type": kind, "level": level,
                    "description": str(item.get("description") or phrase)[:300], "source": "ai",
                    "normalize": normalize or custom_specs[phrase].get("normalize"), "enum": enum,
                    "hint": (str(item["hint"])[:300] if item.get("hint") else custom_specs[phrase].get("hint"))}
        if spec["name"] in known:
            continue
        known.add(spec["name"])
        custom_specs[phrase] = spec
    fields = standard + list(custom_specs.values())
    for f in fields:
        f["required"] = False
    _mark_required(fields, schema["entity"])
    out = {**schema, "fields": fields, "parser": f"rules+{getattr(ai, 'name', 'ai')}"}
    if ignored:
        out["ai_note"] = f"{ignored} AI field suggestion(s) ignored: they did not match a phrase you wrote"
    return out
