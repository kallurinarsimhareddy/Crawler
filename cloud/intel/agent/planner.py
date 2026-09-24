"""Request → intent → plan: which tools, in what order, with what parameters.

The planner composes a plan from the capabilities a request actually asks for —
search, match, exclude, hiring evidence, contact gaps, enrichment, validation,
ranking, campaign mapping, lists, opportunities, exports, monitors — so there is
no single hard-coded workflow. It understands five kinds of request:

``memory``      "Whenever I say ERP, include SAP, Oracle, JDE, Infor and Dynamics."
``follow_up``   acts on the session's current results ("remove companies already in our CRM")
``crm_query``   natural-language CRM questions ("which accounts have no IT decision maker?")
``monitor``     "monitor these 1,000 companies and tell me when hiring increases"
``research``    everything else, through the research intent parser

When the workspace allows external AI, a model may propose the plan instead of
the rules, seeing only the user's request, the workspace memory and the tool
catalogue — **never** scraped page content or tool output, so a web page cannot
inject instructions. Its plan is validated against the registry (unknown tools
and invalid parameters are dropped), and risk, approvals and credit estimates
are always recomputed by the server: the model cannot mark a step as safe.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from cloud.intel.agent.memory import expand_aliases
from cloud.intel.agent.tools import TOOLS
from cloud.intel.ai.base import validate_against_schema
from cloud.intel.research.intent import parse_intent

__all__ = ["understand", "build_steps", "CAMPAIGN_KEYS", "ai_plan"]

log = logging.getLogger(__name__)

CAMPAIGN_KEYS = {"cox-little": ("cox-little", "cox little", "coxlittle", "cox"), "riseit": ("riseit", "rise it"),
                 "itech-us": ("itech us", "itech-us", "itech")}

_FOLLOW_UP = [
    (re.compile(r"\b(remove|exclude|drop|filter out)\b.*\b(already|existing)\b.*\b(crm|accounts?|customers?)\b", re.I),
     "exclude_crm_accounts"),
    (re.compile(r"\b(find|identify|show|which)\b.*\bmissing\b.*\b(it|hr|vp|leaders?|contacts?|decision)\b", re.I),
     "contact_gaps"),
    (re.compile(r"\bvalidate\b.*\bemails?\b", re.I), "validate_email"),
    (re.compile(r"\b(rank|score|prioriti[sz]e)\b", re.I), "calculate_opportunity_score"),
    (re.compile(r"\b(keep|only|top)\b\s+(?:the\s+)?(?:top\s+)?(\d{1,5})\b", re.I), "top_n"),
    (re.compile(r"\bexport\b", re.I), "export_results"),
    (re.compile(r"\b(create|make|build|prepare)\b.*\blist\b", re.I), "create_list"),
    (re.compile(r"\b(create|open)\b.*\bopportunit", re.I), "create_opportunity"),
    (re.compile(r"\b(monitor|watch|track)\b", re.I), "start_monitor"),
    (re.compile(r"\b(campaign|cox|riseit|itech)\b", re.I), "campaign_proposal"),
    (re.compile(r"\b(crawl|refresh)\b.*\b(career|jobs?)\b", re.I), "run_career_crawler"),
    (re.compile(r"\bhiring\b|\bsignals?\b", re.I), "run_hiring_intelligence"),
]
_RUN_IT = re.compile(r"^\s*(run it|go ahead|approve( it| all)?|yes,? (run|go|do) it|do it|proceed|execute)\W*\s*$", re.I)
_MEMORY = re.compile(r"^\s*(please\s+)?(remember|from now on|whenever i (say|type|mention)|note that)\b", re.I)
_MONITOR = re.compile(r"\b(monitor|watch|keep an eye on|track)\b.*\b(compan|accounts?|these|them)\b", re.I)
_REFERS_TO_RESULTS = re.compile(r"\b(them|these|those|the results|this list|remaining|the list|those companies)\b", re.I)


def _campaign_key(text: str) -> Optional[str]:
    lowered = text.lower()
    for key, words in CAMPAIGN_KEYS.items():
        if any(w in lowered for w in words):
            return key
    return None


def _functions(text: str) -> Tuple[List[str], List[str]]:
    lowered = text.lower()
    functions, seniorities = [], []
    if re.search(r"\b(it|cio|cto|technology|information technology|erp)\b", lowered):
        functions.append("it")
    if re.search(r"\b(hr|human resources|talent|recruit\w*|people)\b", lowered):
        functions.append("hr")
    if re.search(r"\b(ceo|coo|cfo|c-level|c-suite|executive|leadership)\b", lowered):
        functions.append("executive")
    if re.search(r"\bvp\b|vice president", lowered):
        seniorities.append("vp")
    if re.search(r"\bdirector", lowered):
        seniorities.append("director")
    return functions or ["it", "hr", "executive"], seniorities


# --- natural-language CRM queries ---------------------------------------------------------

def _crm_query(text: str) -> Optional[List[Dict[str, Any]]]:
    t = text.lower().strip()
    # CRM questions are short, single-clause lookups. A multi-step instruction
    # ("find …, remove …, validate …, rank …") is a research request.
    clauses = len(re.findall(r"[.;]\s+\w", t)) + len(re.findall(r",\s*(and\s+)?(then\s+)?(remove|find|validate|rank|"
                                                                r"prepare|export|create|assign|identify|match)\b", t))
    if len(t) > 180 or clauses >= 1 or re.search(r"\b(validate|export|prepare|rank|campaign|enrich)\b", t):
        return None
    days = None
    m = re.search(r"(?:last|past)\s+(\d{1,3})\s+days?", t)
    if m:
        days = int(m.group(1))
    elif "this week" in t or "last week" in t:
        days = 7
    elif "this month" in t or "last month" in t:
        days = 30
    if re.search(r"\bopportunit\w*\b.*\b(assigned to me|my|mine|i own)\b|\bmy opportunit", t):
        return [_s("search_opportunities", {"owner": "me"}, "Your open opportunities")]
    if re.search(r"\bcontacts?\b.*\b(added|created|new)\b", t):
        return [_s("search_contacts", {"scope": "all", "created_within_days": days or 7},
                   f"Contacts added in the last {days or 7} days")]
    if re.search(r"\bhiring spikes?\b", t):
        return [_s("search_signals", {"signal_types": ["HIRING_SPIKE"], "within_days": days or 30, "scope": "all"},
                   f"Companies with hiring spikes in the last {days or 30} days"),
                _s("calculate_opportunity_score", {"signal_types": ["HIRING_SPIKE"]}, "Rank them")]
    if re.search(r"\b(technology|erp|ats|platform|stack)\b.*\bchanged\b|\bchanged\b.*\b(technology|erp|ats)\b", t):
        types = ["ats_changed"] if "ats" in t or "platform" in t else ["technology_added", "technology_removed"]
        return [_s("search_changes", {"change_types": types, "contains": "erp" if "erp" in t else "",
                                      "within_days": days}, "Companies whose technology changed")]
    if re.search(r"\b(no|without|missing|lack)\b.*\b(it|hr|decision|leader|cio|cto|contact)", t) and \
            re.search(r"\b(accounts?|customers?|companies)\b", t):
        lifecycles = ["account", "customer"] if re.search(r"\baccounts?|customers?\b", t) else []
        functions, seniorities = _functions(t)
        steps = [_s("search_companies", {"lifecycles": lifecycles} if lifecycles else {},
                    "Accounts in the CRM" if lifecycles else "Companies in the CRM"),
                 _s("contact_gaps", {"functions": functions if functions != ["it", "hr", "executive"] else ["it"],
                                     "seniorities": seniorities, "only_missing": True},
                    "Keep those missing the decision maker")]
        return steps
    if re.match(r"^(show|list|which|what|how many|find|give me)\b", t) and re.search(r"\bhiring\b", t) \
            and not re.search(r"\b(contacts?|validate|campaign|export|list)\b", t):
        intent = parse_intent(text)
        steps = [_s("search_companies", {"country": intent.get("country"), "industries": intent.get("industries", []),
                                         "technologies": intent.get("technologies", [])}, "Matching companies"),
                 _s("run_hiring_intelligence", {"keywords": (intent.get("hiring") or {}).get("keywords") or
                                                intent.get("technologies", []),
                                                "signal_types": (intent.get("hiring") or {}).get("signal_types", []),
                                                "window_days": (intent.get("hiring") or {}).get("window_days", 90),
                                                "required": True}, "Keep those with matching hiring evidence"),
                 _s("calculate_opportunity_score", {"technologies": intent.get("technologies", [])}, "Rank them")]
        return steps
    return None


def _s(tool: str, params: Dict[str, Any], title: str, why: str = "") -> Dict[str, Any]:
    return {"tool": tool, "params": params, "title": title, "why": why or title}


# --- the research planner ------------------------------------------------------------------

def build_steps(intent: Dict[str, Any], text: str, profile: Dict[str, Any], *, has_results: bool) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    lowered = text.lower()
    technologies = intent.get("technologies") or []
    hiring = intent.get("hiring") or {}
    count = intent.get("count")
    country = intent.get("country") or _country_from_memory(profile)
    if not has_results or not _REFERS_TO_RESULTS.search(text):
        steps.append(_s("search_companies", {"country": country, "industries": intent.get("industries", []),
                                             "technologies": technologies},
                        "Search internal company database and technology intelligence",
                        f"country={country or 'any'}, industries={intent.get('industries') or 'any'}, "
                        f"technologies={technologies or 'any'}"))
    if intent.get("match_internal"):
        steps.append(_s("match_companies", {}, "Match against internal records (identity resolution)"))
    excluded = list(profile.get("exclude_lifecycles") or [])
    if intent.get("exclude_existing_crm") or excluded:
        lifecycles = sorted(set(["account", "customer", "partner"] if intent.get("exclude_existing_crm") else []) | set(excluded))
        steps.append(_s("exclude_crm_accounts", {"lifecycles": lifecycles, "with_opportunities": bool(intent.get("exclude_existing_crm"))},
                        "Remove companies already in the CRM",
                        "asked in the request" if intent.get("exclude_existing_crm") else "workspace memory: default filter"))
    if hiring.get("required") or hiring.get("keywords") or hiring.get("signal_types"):
        steps.append(_s("run_hiring_intelligence", {
            "keywords": hiring.get("keywords") or technologies, "signal_types": hiring.get("signal_types", []),
            "window_days": hiring.get("window_days", 90), "required": bool(hiring.get("required", True))},
            "Analyze job and hiring signals", "keep companies with matching hiring evidence"))
    functions = intent.get("contact_functions") or []
    seniorities = intent.get("contact_seniorities") or []
    if functions or seniorities:
        fns, sens = functions or ["it", "hr", "executive"], seniorities
        steps.append(_s("contact_gaps", {"functions": fns, "seniorities": sens}, "Find contact gaps (existing contacts)"))
        if intent.get("use_authorized_sources") or re.search(r"\bfind\b.*\bcontacts?|\bmissing\b", lowered):
            steps.append(_s("find_contacts", {"functions": fns, "seniorities": sens,
                                              "authorized_sources": bool(intent.get("use_authorized_sources", True))},
                            "Find missing contacts (internal → company websites → authorized providers)"))
    if intent.get("validate_emails"):
        steps.append(_s("validate_email", {"paid": True}, "Validate available emails",
                        "cache and free checks first; the paid provider only for undecided addresses, after approval"))
    steps.append(_s("calculate_opportunity_score", {"technologies": technologies,
                                                    "keywords": hiring.get("keywords") or technologies,
                                                    "signal_types": hiring.get("signal_types", []), "limit": count},
                    "Score and rank opportunities", "explainable opportunity + intent scores with reason codes"))
    campaign = _campaign_key(text)
    if intent.get("assign_campaign") or campaign:
        steps.append(_s("campaign_proposal", {"campaign_key": campaign} if campaign else {},
                        f"Map signals to {'the ' + campaign.upper() + ' campaign' if campaign else 'the best campaign'}"))
    if intent.get("create_list") or re.search(r"\b(outreach|campaign|target)\s+list\b|\bprepare\b.*\blist\b", lowered):
        name = f"{(campaign or 'Research').upper()} — {text[:80]}"
        steps.append(_s("create_list", {"name": name[:200]}, "Create the proposed list"))
    if intent.get("create_opportunities"):
        steps.append(_s("create_opportunity", {}, "Create opportunities for the ranked companies"))
    if _MONITOR.search(text):
        steps.append(_s("start_monitor", {"frequency": "daily" if "daily" in lowered else "weekly"},
                        "Monitor these companies for hiring changes"))
    if intent.get("export"):
        steps.append(_s("export_results", {"format": intent.get("export_format") or "xlsx"}, "Export the results"))
    return steps


def _country_from_memory(profile: Dict[str, Any]) -> Optional[str]:
    country = (profile.get("country") or "").lower()
    return {"us": "United States", "usa": "United States", "united states": "United States", "canada": "Canada",
            "uk": "United Kingdom", "india": "India"}.get(country)


def _follow_up(text: str) -> List[Dict[str, Any]]:
    steps = []
    lowered = text.lower()
    for pattern, tool in _FOLLOW_UP:
        m = pattern.search(text)
        if not m:
            continue
        if tool == "top_n":
            steps.append(_s("calculate_opportunity_score", {"limit": int(m.group(2))}, f"Keep the top {m.group(2)}"))
        elif tool == "contact_gaps":
            functions, seniorities = _functions(text)
            steps.append(_s("contact_gaps", {"functions": functions, "seniorities": seniorities, "only_missing": True},
                            "Companies missing those contacts"))
            if re.search(r"\b(find|fill|get|look up)\b", lowered):
                steps.append(_s("find_contacts", {"functions": functions, "seniorities": seniorities,
                                                  "authorized_sources": True}, "Find the missing contacts"))
        elif tool == "validate_email":
            steps.append(_s("validate_email", {"paid": True}, "Validate available emails"))
        elif tool == "create_list":
            key = _campaign_key(text)
            steps.append(_s("create_list", {"name": f"{(key or 'Control room').upper()} list"}, "Create the list"))
        elif tool == "campaign_proposal":
            key = _campaign_key(text)
            steps.append(_s("campaign_proposal", {"campaign_key": key} if key else {}, "Map to campaign"))
        elif tool == "export_results":
            fmt = "csv" if "csv" in lowered else "json" if "json" in lowered else "xlsx"
            steps.append(_s("export_results", {"format": fmt}, "Export"))
        elif tool == "start_monitor":
            steps.append(_s("start_monitor", {"frequency": "daily" if "daily" in lowered else "weekly"}, "Monitor them"))
        elif tool == "run_hiring_intelligence" and not steps:
            intent = parse_intent(text)
            steps.append(_s("run_hiring_intelligence", {"keywords": (intent.get("hiring") or {}).get("keywords") or
                                                        intent.get("technologies", []), "required": True,
                                                        "detect": True}, "Hiring evidence for these companies"))
        else:
            steps.append(_s(tool, {}, tool.replace("_", " ").capitalize()))
        if tool in ("exclude_crm_accounts",):
            continue
    seen, unique = set(), []
    for step in steps:
        key = (step["tool"], str(sorted(step["params"].items())))
        if key not in seen:
            seen.add(key)
            unique.append(step)
    return unique


def understand(text: str, profile: Dict[str, Any], *, has_results: bool = False) -> Dict[str, Any]:
    """Classify the request and build candidate steps (before risk/approval/estimates)."""
    raw = (text or "").strip()
    expanded, applied = expand_aliases(raw, profile.get("aliases") or {})
    if _MEMORY.match(raw):
        return {"kind": "memory", "text": raw, "steps": [], "aliases_applied": []}
    if has_results and _RUN_IT.match(raw):
        return {"kind": "run_it", "text": raw, "steps": [], "aliases_applied": []}
    if has_results and len(raw) < 240:
        follow = _follow_up(expanded)
        new_subject = parse_intent(expanded)
        starts_new = bool(re.match(r"^\s*(find|search|discover|show me|list)\b", raw, re.I)) and not _REFERS_TO_RESULTS.search(raw) \
            and bool(new_subject.get("industries") or new_subject.get("count"))
        if follow and not starts_new:
            return {"kind": "follow_up", "text": raw, "steps": follow, "aliases_applied": applied}
    crm = _crm_query(expanded)
    if crm is not None:
        return {"kind": "crm_query", "text": raw, "steps": crm, "aliases_applied": applied}
    intent = parse_intent(expanded)
    steps = build_steps(intent, expanded, profile, has_results=has_results)
    kind = "monitor" if _MONITOR.search(raw) else "research"
    return {"kind": kind, "text": raw, "intent": intent, "steps": steps, "aliases_applied": applied}


# --- the optional AI planner ------------------------------------------------------------------

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {"steps": {"type": "array", "items": {
        "type": "object",
        "properties": {"tool": {"type": "string"}, "params": {"type": "object"}, "title": {"type": "string"}},
        "required": ["tool", "params", "title"]}}},
    "required": ["steps"],
}


def ai_plan(ai: Any, text: str, profile: Dict[str, Any], tool_names: List[str]) -> Optional[List[Dict[str, Any]]]:
    """Ask the workspace's AI provider for a plan. Returns validated steps or None to fall back to rules."""
    catalogue = [{"name": n, "description": TOOLS[n].description, "input_schema": TOOLS[n].schema} for n in tool_names]
    system = ("You plan data-research workflows for a B2B company-intelligence platform. Choose tools from the "
              "catalogue, in order, to satisfy the user's request. Use only tools and parameters from the catalogue. "
              "Prefer internal data and free sources before paid providers. Do not invent data. Return JSON only.")
    prompt = (f"Tool catalogue:\n{catalogue}\n\nWorkspace vocabulary (aliases): {profile.get('aliases') or {}}\n"
              f"Default filters: exclude lifecycles {profile.get('exclude_lifecycles') or []}, "
              f"country {profile.get('country')}\n\nUser request:\n<<<\n{text}\n>>>")
    try:
        answer = ai.complete_json(system, prompt, _PLAN_SCHEMA, max_tokens=3000)
    except Exception as error:  # noqa: BLE001 - the rules planner is always there
        log.info("AI planner unavailable, using rules: %s", error)
        return None
    steps = []
    for step in (answer or {}).get("steps") or []:
        name = step.get("tool")
        if name not in tool_names:
            log.info("AI planner proposed unknown/unavailable tool %r; dropped", name)
            continue
        params = step.get("params") or {}
        if validate_against_schema(params, TOOLS[name].schema):
            log.info("AI planner gave invalid params for %s; dropped", name)
            continue
        steps.append(_s(name, params, str(step.get("title") or name)[:200], "proposed by the AI planner"))
    return steps or None
