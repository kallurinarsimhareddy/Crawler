"""Understanding a research question, deterministically first.

    "Find 500 US manufacturing companies with ERP hiring, remove companies already
     in our CRM, find missing IT leaders, validate emails and create an outreach list."

becomes an :data:`INTENT_SCHEMA` object: what to find, how many, where, which
industries and technologies, which hiring evidence is required, what to exclude,
which contacts are wanted, and which follow-up actions were asked for.

The rules parser handles the platform's vocabulary offline. When the workspace
allows external AI, a model may *fill gaps* the rules left empty; its answer is
validated against the same schema and can only add values, never remove what
the rules understood.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["INTENT_SCHEMA", "TECHNOLOGIES", "parse_intent", "refine_with_ai"]

#: canonical technology -> phrases (lower-case). Aliases matter: AS400 = iSeries = IBM i.
TECHNOLOGIES: Dict[str, Tuple[str, ...]] = {
    "RPG": ("rpg", "rpgle", "rpg iv", "rpg/400"),
    "AS400": ("as400", "as/400", "as 400", "iseries", "i series", "ibm i", "system i", "ibm power i"),
    "ERP": ("erp", "enterprise resource planning"),
    "SAP": ("sap", "s/4hana", "s4hana", "sap ecc", "sap s/4"),
    "Oracle": ("oracle ebs", "oracle e-business", "oracle erp", "oracle cloud erp", "oracle fusion", "oracle"),
    "JD Edwards": ("jd edwards", "jde", "jdedwards"),
    "Infor": ("infor", "infor ln", "infor m3", "syteline", "infor cloudsuite"),
    "Microsoft Dynamics": ("microsoft dynamics", "dynamics 365", "d365", "dynamics ax", "dynamics nav",
                           "business central", "dynamics gp"),
    "NetSuite": ("netsuite",),
    "Epicor": ("epicor",),
    "Sage": ("sage x3", "sage 100", "sage 300", "sage intacct"),
    "QAD": ("qad",),
    "Plex": ("plex",),
    "WMS": ("wms", "warehouse management", "manhattan associates", "blue yonder"),
    "Salesforce": ("salesforce",),
    "ServiceNow": ("servicenow",),
    "Workday": ("workday",),
    "AWS": ("aws", "amazon web services"),
    "Azure": ("azure",),
    "Google Cloud": ("gcp", "google cloud"),
    "Snowflake": ("snowflake",),
    "Databricks": ("databricks",),
    "Kubernetes": ("kubernetes",),
    "Java": ("java",),
    ".NET": (".net", "dotnet", "c#"),
    "Python": ("python",),
}

INDUSTRIES: Dict[str, Tuple[str, ...]] = {
    "Manufacturing": ("manufacturing", "manufacturer", "manufacturers", "industrial", "factory", "factories"),
    "Healthcare": ("healthcare", "health care", "hospital", "hospitals"),
    "Financial Services": ("financial services", "finance", "banking", "bank", "banks", "insurance"),
    "Retail": ("retail", "retailer", "retailers", "ecommerce", "e-commerce"),
    "Distribution": ("distribution", "distributor", "distributors", "wholesale"),
    "Logistics": ("logistics", "transportation", "supply chain"),
    "Construction": ("construction",),
    "Energy": ("energy", "oil and gas", "utilities", "utility"),
    "Automotive": ("automotive",),
    "Aerospace": ("aerospace", "defense"),
    "Pharmaceutical": ("pharmaceutical", "pharma", "life sciences", "biotech"),
    "Food & Beverage": ("food and beverage", "food & beverage", "food"),
    "Technology": ("software", "technology companies", "tech companies", "saas"),
    "Education": ("education", "universities", "university", "schools"),
    "Government": ("government", "public sector"),
}

COUNTRIES: Dict[str, Tuple[str, ...]] = {
    "United States": ("us", "u.s.", "usa", "u.s.a.", "united states", "american", "america"),
    "Canada": ("canada", "canadian"),
    "United Kingdom": ("uk", "u.k.", "united kingdom", "british", "england"),
    "India": ("india", "indian"),
    "Germany": ("germany", "german"),
    "Mexico": ("mexico", "mexican"),
}

FUNCTIONS: Dict[str, Tuple[str, ...]] = {
    "it": ("it", "information technology", "technology leaders", "cio", "cto", "it leaders", "it leader",
           "it director", "it directors", "erp leaders", "erp leadership"),
    "hr": ("hr", "human resources", "recruiting", "recruiters", "talent acquisition", "chro", "people team"),
    "executive": ("c-level", "c level", "c-suite", "executives", "ceo", "coo", "cfo", "leadership"),
    "finance": ("finance leaders", "controller", "cfo"),
    "operations": ("operations", "supply chain leaders", "plant managers"),
}

SENIORITIES: Dict[str, Tuple[str, ...]] = {
    "C-Level": ("c-level", "c level", "c-suite", "cio", "cto", "chro", "ceo", "coo", "cfo", "chief"),
    "VP": ("vp", "vps", "vice president", "vice presidents"),
    "Director": ("director", "directors"),
    "Manager": ("manager", "managers"),
}

SIGNAL_WORDS: Dict[str, Tuple[str, ...]] = {
    "NEW_ROLE": ("new",),
    "HIRING_SPIKE": ("hiring spike", "spike", "surge"),
    "HIRING_VELOCITY": ("velocity", "fast-growing", "rapid hiring"),
    "LONG_OPEN_ROLE": ("long-open", "long open", "open for", "hard to fill", "hard-to-fill"),
    "HARD_TO_FILL": ("hard to fill", "hard-to-fill"),
    "LEADERSHIP_HIRING": ("leadership hiring", "hiring leaders", "executive hiring"),
    "PROJECT_IMPLEMENTATION": ("implementation", "migration", "modernization", "modernisation", "upgrade"),
    "EXPANSION_HIRING": ("expansion", "expanding"),
}

INTENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity": {"type": "string", "enum": ["companies", "contacts", "jobs"]},
        "count": {"type": ["integer", "null"]},
        "country": {"type": ["string", "null"]},
        "industries": {"type": "array", "items": {"type": "string"}},
        "technologies": {"type": "array", "items": {"type": "string"}},
        "hiring": {"type": "object", "properties": {
            "required": {"type": "boolean"}, "keywords": {"type": "array", "items": {"type": "string"}},
            "signal_types": {"type": "array", "items": {"type": "string"}},
            "window_days": {"type": "integer"}}},
        "match_internal": {"type": "boolean"},
        "exclude_existing_crm": {"type": "boolean"},
        "contact_functions": {"type": "array", "items": {"type": "string"}},
        "contact_seniorities": {"type": "array", "items": {"type": "string"}},
        "missing_contacts_only": {"type": "boolean"},
        "use_authorized_sources": {"type": "boolean"},
        "validate_emails": {"type": "boolean"},
        "rank": {"type": "boolean"},
        "assign_campaign": {"type": "boolean"},
        "create_list": {"type": "boolean"},
        "create_opportunities": {"type": "boolean"},
        "export": {"type": "boolean"},
        "export_format": {"type": "string", "enum": ["csv", "xlsx", "json"]},
    },
    "required": ["entity", "industries", "technologies", "hiring", "contact_functions"],
}

_NUMBER = re.compile(r"\b(?:top\s+)?(\d{1,3}(?:,\d{3})*|\d+)\s+(?:\w+\s+){0,4}?(?:companies|accounts|contacts|jobs|leads|prospects)\b", re.I)
_WINDOW = re.compile(r"(?:last|past)\s+(\d{1,3})\s+(days|day|weeks|week|months|month)", re.I)


def _has(text: str, phrase: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text) is not None


def _find(text: str, vocab: Dict[str, Tuple[str, ...]]) -> List[str]:
    return [name for name, phrases in vocab.items() if any(_has(text, p) for p in phrases)]


def _hiring_clauses(text: str) -> List[Tuple[int, int]]:
    """Spans of the words around each mention of hiring ("new ERP hiring", "hiring for SAP roles")."""
    spans = []
    for match in re.finditer(r"hiring|job postings?|open roles?|openings|recruiting for", text):
        window_start = max(0, match.start() - 40)
        before = text[window_start:match.start()]
        cuts = [m.end() for m in re.finditer(r"[,.;]|with|that|which|find", before)]
        start = window_start + (cuts[-1] if cuts else 0)
        after = text[match.end():match.end() + 40]
        stop = re.search(r"[,.;]|and|then", after)
        end = match.end() + (stop.start() if stop else len(after))
        spans.append((start, end))
    return spans


def parse_intent(question: str) -> Dict[str, Any]:
    """The rules parser. Always returns a complete intent object."""
    raw = re.sub(r"\s+", " ", question or "").strip()
    text = raw.lower()

    count: Optional[int] = None
    match = _NUMBER.search(raw)
    if match:
        count = int(match.group(1).replace(",", ""))
    entity = "companies"
    if re.search(r"\b(find|list|get)\s+(?:\d+\s+)?(?:\w+\s+){0,2}contacts\b", text) and "compan" not in text:
        entity = "contacts"

    country = None
    for name, phrases in COUNTRIES.items():
        if any(_has(text, p) for p in phrases):
            country = name
            break

    clauses = _hiring_clauses(text)
    hiring_text = " ".join(text[a:b] for a, b in clauses)
    hiring_keywords = _find(hiring_text, TECHNOLOGIES)
    # Technologies the company USES: mentioned outside the hiring clauses.
    chars = list(text)
    for a, b in clauses:
        chars[a:b] = " " * (b - a)
    outside = "".join(chars)
    technologies = _find(outside, TECHNOLOGIES)
    signal_types = [s for s, words in SIGNAL_WORDS.items() if any(_has(hiring_text, w) for w in words)]
    if "hiring signal" in text and not signal_types:
        signal_types = []
    window = _WINDOW.search(text)
    window_days = 90
    if window:
        n, unit = int(window.group(1)), window.group(2)
        window_days = n * (7 if unit.startswith("week") else 30 if unit.startswith("month") else 1)
    elif "NEW_ROLE" in signal_types:
        window_days = 30

    # Contacts: "missing IT/HR/VP contacts", "IT leaders", "HR".
    contact_part = " ".join(re.findall(r"(?:find|identify|get|add)[^.;]*?(?:contacts?|leaders?|leadership|people|"
                                       r"decision[- ]makers|executives|hr|recruiters?)\b[^.;,]*", text))
    contact_part = contact_part.replace("/", " ")
    functions = _find(contact_part, FUNCTIONS)
    seniorities = _find(contact_part, SENIORITIES)
    if seniorities and not functions and re.search(r"\bvps?\b|vice president", contact_part):
        functions = ["executive"]

    wants = lambda *ps: any(p in text for p in ps)  # noqa: E731
    create_list = wants("outreach list", "create a list", "create list", "build a list", "add them to a list",
                        "target list", "call list")
    export_format = "xlsx"
    for fmt in ("csv", "json", "xlsx", "excel"):
        if _has(text, fmt):
            export_format = "xlsx" if fmt == "excel" else fmt
            break
    return {
        "entity": entity,
        "count": count,
        "country": country,
        "industries": _find(text, INDUSTRIES),
        "technologies": technologies,
        "hiring": {"required": bool(clauses), "keywords": hiring_keywords, "signal_types": signal_types,
                   "window_days": window_days},
        "match_internal": wants("internal data", "our data", "my data", "match them", "match against",
                                "internal dataset", "master data"),
        "exclude_existing_crm": wants("already in our crm", "already in my crm", "already in the crm",
                                      "not in our crm", "not in my crm", "remove companies already",
                                      "exclude existing", "exclude customers", "not already"),
        "contact_functions": functions,
        "contact_seniorities": seniorities,
        "missing_contacts_only": wants("missing"),
        "use_authorized_sources": wants("authorized source", "authorised source", "my sources", "our sources",
                                        "zoominfo", "seamless"),
        "validate_emails": wants("validate", "verify the email", "verify emails", "email validation"),
        "rank": wants("rank", "prioritize", "prioritise", "score", "top "),
        "assign_campaign": wants("campaign"),
        "create_list": create_list,
        "create_opportunities": bool(re.search(r"(create|open|add|make)[^.;,]{0,30}(opportunit|deals?|pipeline)", text)),
        "export": wants("export", "download", "spreadsheet", "csv", "xlsx", "excel"),
        "export_format": export_format,
        "question": raw,
        "parser": "rules",
    }


def refine_with_ai(intent: Dict[str, Any], ai: Any) -> Dict[str, Any]:
    """Let a model fill fields the rules left empty. Validated; rules values always win."""
    from cloud.intel.ai.base import AIError, validate_against_schema

    if ai is None:
        return intent
    prompt = ("Parse this B2B research request into the JSON schema. Use only what the request says.\n\n"
              f"Request: {intent.get('question', '')}")
    try:
        answer = ai.complete_json("You convert sales-research requests into structured filters.", prompt,
                                  INTENT_SCHEMA, max_tokens=1500)
    except AIError:
        return intent
    if validate_against_schema(answer, INTENT_SCHEMA):
        return intent
    merged = dict(intent)
    for key, value in answer.items():
        if key not in INTENT_SCHEMA["properties"]:
            continue
        current = merged.get(key)
        if current in (None, [], False, "") and value not in (None, [], ""):
            merged[key] = value
    merged["parser"] = f"rules+{ai.name}"
    return merged
