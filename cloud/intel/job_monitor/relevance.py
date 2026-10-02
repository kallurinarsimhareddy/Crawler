"""Job relevance: score a job 0-100 against the workspace's keyword universe and classify it
HIGH / REVIEW / REJECT, with the matched keywords, categories and a readable reason.

The keyword universe comes from a workbook such as ``IT_Crawler_Keywords.xlsx``
(``All Keywords``: Keyword + Category, and ``By Category``: category sections) and is
stored per workspace in ``job_keyword_sets``. Workbook categories roll up into the
groups the business reasons about (ERP / Enterprise Applications, Cloud, Cybersecurity,
Data / AI, DevOps, Infrastructure, Engineering, Manufacturing Systems, ...).

Matching is whole-word only: a keyword must not touch a letter or digit on either side,
so ``erp`` inside "enterprise" or ``SAP`` inside "ASAP" never counts. Short all-caps
acronyms (SAP, ERP, MES, MM, SD...) match case-sensitively; compound names also match
their spaced/dotted spellings (``PowerBI`` = "Power BI", ``S4HANA`` = "S/4HANA",
``NextJS`` = "Next.js"). Keywords that are ordinary English words or names ("Edge",
"Monday", "Chef", "Swift", "Sage", "Epic"...) and 2-3 letter codes only count as
*context*: they need another real IT keyword or an IT role in the title, and SAP module
codes (MM, SD, PP...) need SAP itself. A title that reads like a non-IT job (driver,
nurse, cashier, warehouse, security guard...) is noise and is pushed down hard.

Built-in vocabulary (IT role words, manufacturing context, noise titles) is listed below
and labelled "built-in" in every reason, so it is never confused with the workbook.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

__all__ = ["RelevanceEngine", "parse_keyword_workbook", "DEFAULT_GROUPS", "DEFAULT_THRESHOLDS", "ENGINE_VERSION"]

ENGINE_VERSION = "rel-v5"
DEFAULT_THRESHOLDS = {"high": 70, "review": 40}
PRIORITY_GROUPS = ("ERP / Enterprise Applications", "Manufacturing Systems")

#: workbook category -> business group
DEFAULT_GROUPS: Dict[str, str] = {
    "ERP Platforms": "ERP / Enterprise Applications",
    "ERP Modules (SAP Specific)": "ERP / Enterprise Applications",
    "Healthcare ERP": "ERP / Enterprise Applications",
    "CRM Platforms": "ERP / Enterprise Applications",
    "ITSM & Service Management": "Business Systems",
    "Middleware & Integration": "Business Systems",
    "Low Code / No Code Platforms": "Business Systems",
    "RPA Tools": "Business Systems",
    "Collaboration & Project Management": "Business Systems",
    "Cloud Platforms": "Cloud",
    "Containerization & Orchestration": "Cloud",
    "Virtualization": "Infrastructure",
    "Cybersecurity Tools": "Cybersecurity",
    "Data & Analytics": "Data / AI",
    "AI & ML Frameworks": "Data / AI",
    "Relational Databases": "Data / AI",
    "NoSQL Databases": "Data / AI",
    "DevOps & CI/CD": "DevOps",
    "Infrastructure & IaC": "DevOps",
    "Monitoring & Observability": "DevOps",
    "Networking": "Infrastructure",
    "Mainframe & Legacy": "Infrastructure",
    "Programming Languages": "Engineering",
    "Frontend Frameworks": "Engineering",
    "Backend Frameworks": "Engineering",
    "Mobile & Cross Platform": "Engineering",
    "Testing Tools": "Engineering",
    "Version Control": "Engineering",
    "Emerging Tech": "Emerging Tech",
    "Certifications": "Certifications",
}
#: keyword-level group for the broad "Category Words" (and manufacturing systems everywhere)
KEYWORD_GROUPS: Dict[str, Tuple[str, ...]] = {
    "erp": ("ERP / Enterprise Applications",), "crm": ("ERP / Enterprise Applications",),
    "hrms": ("ERP / Enterprise Applications",), "hris": ("ERP / Enterprise Applications",),
    "wms": ("ERP / Enterprise Applications",), "tms": ("ERP / Enterprise Applications",),
    "scm": ("ERP / Enterprise Applications",), "eam": ("ERP / Enterprise Applications",),
    "fsm": ("ERP / Enterprise Applications",), "itsm": ("Business Systems",),
    "mrp": ("ERP / Enterprise Applications", "Manufacturing Systems"),
    "plm": ("ERP / Enterprise Applications", "Manufacturing Systems"),
    "mes": ("Manufacturing Systems",), "iqms": ("ERP / Enterprise Applications", "Manufacturing Systems"),
    "modbus": ("Manufacturing Systems",), "profibus": ("Manufacturing Systems",), "canbus": ("Manufacturing Systems",),
    "devops": ("DevOps",), "sre": ("DevOps",), "cybersecurity": ("Cybersecurity",),
    "networking": ("Infrastructure",), "mainframe": ("Infrastructure",), "virtualization": ("Infrastructure",),
    "middleware": ("Business Systems",), "sysadmin": ("Infrastructure",), "dba": ("Data / AI",),
    "microservices": ("Engineering",), "fullstack": ("Engineering",), "frontend": ("Engineering",),
    "backend": ("Engineering",), "sdet": ("Engineering",), "aiml": ("Data / AI",), "genai": ("Data / AI",),
    "blockchain": ("Emerging Tech",), "iot": ("Emerging Tech",), "scrum": ("Engineering",), "agile": ("Engineering",),
}

#: ordinary words / names that are also product names: context-only
COMMON_WORDS = {w.lower() for w in (
    "Edge", "Monday", "Linear", "Express", "Ember", "Drone", "Harvest", "Move", "BASIC", "Expandable", "Visibility",
    "Spark", "Hive", "Sentinel", "Defender", "Prisma", "Orca", "Phoenix", "Rocket", "Gin", "Fiber", "Feast", "Triton",
    "Gemini", "Mistral", "Llama", "Slack", "Notion", "Shortcut", "Glide", "Bubble", "Kong", "Camel", "Nexus", "Bamboo",
    "Octopus", "Harness", "Concourse", "Chef", "Puppet", "Vagrant", "Packer", "Sage", "Infor", "Abra", "Cyborg",
    "Pronto", "Ellipse", "Agile", "Scrum", "Swift", "Rust", "Ruby", "Julia", "Ada", "Lua", "Delphi", "Pascal", "Cairo",
    "Vyper", "Copilot", "Vertex", "Bedrock", "Athena", "Elastic", "Sentry", "Nomad", "Helm", "Stitch", "Domo", "Epic",
    "Lawson", "Kronos", "Spring", "Rails", "Backbone", "Bootstrap", "Gatsby", "Parcel", "Vite", "Express", "Monday",
    "Asana", "Miro", "Figma", "Trello", "Basecamp", "Wrike", "Airtable", "Retool", "Webflow", "Expo", "Ionic",
    "Capacitor", "Cordova", "Feast", "Tecton", "Seldon", "Mabl", "Squish", "Eggplant", "Perfecto", "Harvest",
    "Dimensions", "Firebird", "Fauna", "Riak", "Solace", "Celigo", "Boomi", "Kryon", "Contextor", "Quantum", "Robotics",
    "Embedded", "Lidar", "Metaverse", "Fastly", "Linode", "Vultr", "Heroku", "Equinix", "Greentree", "Visibility",
    "Expandable", "Abra", "Famis", "Mincom", "Movex", "Baan", "Pervasive", "Ingres", "Interbase", "Informix",
    "Defender", "Wiz", "Lacework", "Okta", "Tanium", "Sophos", "Eset", "Guardduty", "Opengear", "Drone",
    "Basis", "Visibility", "Nexus", "Harness", "Spark", "Triton", "Assembler", "Move", "Edge", "Drone",
)}
#: keywords whose plain spelling is mostly something else: only these forms count
OVERRIDES = {"monday": r"(?<![A-Za-z0-9])monday\.com(?![A-Za-z0-9])"}
#: Built-in spelling variants of one-word workbook keywords (the same keyword written as two
#: words). They add no keyword: a match is reported as the workbook keyword itself.
SPELLINGS = {"cybersecurity": "cyber security", "devops": "dev ops", "fullstack": "full stack",
             "frontend": "front end", "backend": "back end", "microservices": "micro services",
             "sysadmin": "sys admin", "powerapps": "power apps", "genai": "gen ai", "aiml": "ai ml",
             "springboot": "spring boot", "reactnative": "react native", "nextjs": "next js",
             "servicenow": "service now", "peoplesoft": "people soft", "netsuite": "net suite",
             "successfactors": "success factors", "jdedwards": "jd edwards", "powerbi": "power bi"}

#: Built-in: an unmistakable IT role in the title (worth a REVIEW on its own)
IT_ROLE = re.compile(
    r"(?<![a-z])(developer|programmer|software engineer|software developer|devops|dev\s*ops|sre|"
    r"site reliability|sysadmin|dba|database (?:administrator|engineer|developer)|"
    r"data (?:engineer|scientist|architect)|machine learning|ml engineer|cloud (?:engineer|architect)|"
    r"network (?:engineer|administrator|architect)|cyber[\s-]?security|cyber (?:analyst|engineer|specialist|"
    r"operations|defense|defence)|information security|infosec|"
    r"security (?:engineer|analyst|architect)|it (?:support|manager|director|specialist|analyst|technician|"
    r"consultant|engineer)|information technology|help ?desk|service desk|desktop support|"
    r"systems? (?:administrator)|erp|sap|oracle|salesforce|servicenow|workday|business systems|"
    r"(?:computer|network|information|it|application) security|enterprise architect|solutions? architect|"
    r"it architect|data analyst|analyst programmer|applications? (?:developer|engineer|analyst)|"
    r"(?:linux|windows|unix) (?:administrator|engineer)|"
    r"full[\s-]?stack|front[\s-]?end developer|back[\s-]?end|web developer|bi developer|"
    r"qa engineer|test automation|sdet)(?![a-z])", re.I)
#: Built-in: roles that are often, not always, IT (weaker)
IT_ROLE_WEAK = re.compile(
    r"(?<![a-z])(software|architect|administrator|database|cloud|network|systems? (?:engineer|analyst|specialist)|"
    r"applications? manager|security (?:specialist|manager)|configuration manager|technical support|"
    r"business analyst|functional consultant|technical consultant|technical lead|tech lead|qa|"
    r"quality assurance|test(?:ing)? engineer|application|integration|digital systems|scrum master|"
    r"product owner|technical program manager)(?![a-z])", re.I)
#: Built-in: a job word that makes a title keyword a role ("MES Engineer", "Workday Lead")
JOB_WORD = re.compile(r"(?<![a-z])(engineer|analyst|specialist|consultant|manager|lead|administrator|architect|"
                      r"developer|technician|coordinator|director)(?![a-z])", re.I)
#: Built-in: manufacturing / engineering context (not IT by itself)
MANUFACTURING = re.compile(
    r"(?<![a-z])(manufacturing|plant|factory|production|mes|scada|plc|automation engineer|controls engineer|"
    r"process engineer|industrial engineer|quality engineer|mechanical engineer|electrical engineer)(?![a-z])", re.I)
#: Built-in: titles that are not IT jobs
NOISE = re.compile(
    r"(?<![a-z])(nurse|rn|lpn|cna|caregiver|home health|physician|therapist|pharmac\w*|dental|dentist|"
    r"medical assistant|phlebotom\w*|driver|cdl|truck|delivery driver|courier|cashier|retail (?:associate|sales)|"
    r"store associate|sales associate|merchandiser|warehouse|picker|forklift|material handler|cook|dishwasher|"
    r"food server|restaurant server|"
    r"bartender|barista|housekeep\w*|janitor|custodian|cleaner|(?<!information )(?<!cyber )(?<!it )security "
    r"(?:guard|officer)|receptionist|teacher|tutor|nanny|landscap\w*|electrician|plumber|hvac|carpenter|painter|"
    r"welder|machinist|assembler|production (?:associate|worker|operator)|machine operator|mechanic|"
    r"data entry|call center|customer service representative|insurance agent|real estate|loan officer|"
    r"recruiter|crew member|line worker|laborer|stocker)(?![a-z])", re.I)


def _split_compound(keyword: str) -> List[str]:
    """``PowerBI`` -> [Power, BI]; ``JDEdwards`` -> [JD, Edwards]; ``S4HANA`` -> [S, 4, HANA]."""
    parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+|[^A-Za-z\d\s]+", keyword)
    return [p for p in parts if p]


def _pattern(keyword: str) -> str:
    parts = _split_compound(keyword)
    if len(parts) > 1 and all(re.fullmatch(r"[A-Za-z0-9]+", p) for p in parts):
        body = r"[\s.\-/]?".join(re.escape(p) for p in parts)
    else:
        # Separators inside a keyword are interchangeable: PL/SQL = PL-SQL = PLSQL = PL SQL.
        pieces = [p for p in re.split(r"[\s./\-]+", keyword) if p]
        if len(pieces) > 1 and all(re.fullmatch(r"[A-Za-z0-9#+]+", p) for p in pieces):
            body = r"[\s.\-/]?".join(re.escape(p) for p in pieces)
        else:
            body = re.escape(keyword).replace(r"\ ", r"[\s\-]")
    return r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])"


@dataclass
class _Keyword:
    keyword: str
    categories: List[str]
    groups: List[str]
    regex: Any
    ambiguous: bool
    sap_module: bool
    #: Lower-cased text every match must contain (a cheap test before the regex).
    needle: str = ""


@dataclass
class Score:
    score: int
    classification: str
    matched_keywords: List[str] = field(default_factory=list)
    matched_categories: List[str] = field(default_factory=list)
    groups: List[str] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"relevance_score": float(self.score), "relevance_class": self.classification,
                "matched_keywords": self.matched_keywords, "matched_categories": self.matched_categories,
                "relevance_reason": self.reason[:1000]}


class RelevanceEngine:
    def __init__(self, keywords: Sequence[Mapping[str, Any]], *, groups: Optional[Mapping[str, str]] = None,
                 thresholds: Optional[Mapping[str, int]] = None, negative_terms: Sequence[str] = (),
                 version: str = ENGINE_VERSION) -> None:
        self.groups = {**DEFAULT_GROUPS, **dict(groups or {})}
        self.thresholds = {**DEFAULT_THRESHOLDS, **dict(thresholds or {})}
        self.version = version
        merged: Dict[str, Dict[str, Any]] = {}
        for item in keywords:
            word = str(item.get("keyword") or "").strip()
            if not word:
                continue
            entry = merged.setdefault(word.lower(), {"keyword": word, "categories": []})
            category = str(item.get("category") or "Uncategorized")
            if category not in entry["categories"]:
                entry["categories"].append(category)
        self.keywords: List[_Keyword] = []
        for low, entry in merged.items():
            word = entry["keyword"]
            cats = entry["categories"]
            groups_for = list(KEYWORD_GROUPS.get(low, ()))
            for cat in cats:
                group = self.groups.get(cat)
                if group and group not in groups_for and not KEYWORD_GROUPS.get(low):
                    groups_for.append(group)
            acronym = word.isupper() and len(word) <= 5 or len(word) <= 3
            ambiguous = low in COMMON_WORDS or len(word) <= 2 or (len(word) == 3 and not word.isupper())
            # Acronyms and everyday words match only in the workbook's own capitalisation
            # ("Edge", not "cutting-edge"; "SAP", not "sap").
            flags = 0 if (acronym or ambiguous) else re.I
            pattern = OVERRIDES.get(low) or _pattern(word)
            if low in SPELLINGS and low not in OVERRIDES:
                pattern = f"(?:{pattern}|{_pattern(SPELLINGS[low])})"
            if ambiguous and low not in OVERRIDES and not word.isupper():
                pattern = f"(?:{pattern}|{_pattern(word.upper())})"   # "Basis" or "BASIS", never "basis"
            regex = re.compile(pattern, re.I if low in OVERRIDES else flags)
            sap_module = "ERP Modules (SAP Specific)" in cats and (len(word) <= 3 or ambiguous)
            parts = _split_compound(word)
            needle = (parts[0] if parts else word).lower()
            if low in OVERRIDES:
                needle = low
            elif low in SPELLINGS:
                needle = SPELLINGS[low].split()[0]     # shared by both spellings
            self.keywords.append(_Keyword(word, cats, groups_for or ["Other IT"], regex, ambiguous, sap_module,
                                          needle))
        self.negative = re.compile(r"(?<![a-z])(" + "|".join(re.escape(t) for t in negative_terms) + r")(?![a-z])",
                                   re.I) if negative_terms else None

    def _find(self, text: str) -> List[_Keyword]:
        if not text:
            return []
        low = text.lower()
        return [k for k in self.keywords if k.needle in low and k.regex.search(text)]

    def score(self, *, title: str, description: str = "", tags: Iterable[str] = (), search_term: Optional[str] = None,
              ) -> Score:
        title = title or ""
        context = "\n".join([description or "", " | ".join(t for t in tags if t)])
        in_title = self._find(title)
        in_context = [k for k in self._find(context) if k not in in_title]
        role = IT_ROLE.search(title)
        weak_role = None if role else IT_ROLE_WEAK.search(title)
        strong_title = [k for k in in_title if not k.ambiguous and not k.sap_module]
        strong_context = [k for k in in_context if not k.ambiguous and not k.sap_module]
        has_sap = any(k.keyword.lower() in ("sap", "s4hana", "hana", "abap", "fico") for k in in_title + in_context)
        anchored = bool(strong_title or strong_context or role or weak_role)

        def usable(k: _Keyword) -> bool:
            if k.sap_module:
                return has_sap
            return not k.ambiguous or anchored

        title_hits = [k for k in in_title if usable(k)]
        context_hits = [k for k in in_context if usable(k)]
        ignored = [k.keyword for k in in_title + in_context if not usable(k)]
        reasons: List[str] = []
        score = 0
        strong_t = [k for k in title_hits if not k.ambiguous]
        if strong_t:
            score += min(45, 30 + 8 * (len(strong_t) - 1))
            reasons.append("title: " + ", ".join(k.keyword for k in strong_t))
        weak_t = [k for k in title_hits if k.ambiguous]
        if weak_t:
            score += min(10, 5 * len(weak_t))
            reasons.append("title (context-only words): " + ", ".join(k.keyword for k in weak_t))
        if role:
            score += 40
            reasons.append(f"built-in IT role in title: '{role.group(0)}'")
        elif weak_role:
            score += 25
            reasons.append(f"built-in possible IT role in title: '{weak_role.group(0)}'")
        elif strong_t and JOB_WORD.search(title):
            score += 15
            reasons.append(f"built-in job word with a keyword in the title: '{JOB_WORD.search(title).group(0)}'")
        if context_hits:
            weight = sum(6 if not k.ambiguous else 1 for k in context_hits)
            score += min(35, weight)
            reasons.append("description/tags: " + ", ".join(k.keyword for k in context_hits[:12])
                           + (f" (+{len(context_hits) - 12} more)" if len(context_hits) > 12 else ""))
        groups: List[str] = []
        for k in title_hits + context_hits:
            for g in k.groups:
                if g not in groups:
                    groups.append(g)
        manufacturing = MANUFACTURING.search(title) or MANUFACTURING.search(context[:5000])
        if manufacturing and "Manufacturing" not in groups and "Manufacturing Systems" not in groups:
            groups.append("Manufacturing")
            reasons.append(f"built-in manufacturing context: '{manufacturing.group(0)}'")
        if len(groups) > 1:
            score += min(10, 5 * (len(groups) - 1))
        if any(g in PRIORITY_GROUPS for g in groups):
            score += 5
        if search_term:
            term = re.compile(_pattern(search_term.strip()), re.I)
            if term.search(title):
                score += 10
                reasons.append(f"search term '{search_term}' in title")
            elif term.search(context):
                score += 5
                reasons.append(f"search term '{search_term}' in description")
        noise = NOISE.search(title) or (self.negative.search(title) if self.negative else None)
        if noise:
            score -= 45
            reasons.insert(0, f"noise: title looks like a non-IT job ('{noise.group(0)}')")
        if not title_hits and not context_hits and not role and not weak_role:
            reasons.append("no workbook keyword and no IT role found")
        if ignored:
            reasons.append("ignored without IT context: " + ", ".join(sorted(set(ignored))[:8]))
        score = max(0, min(100, score))
        high, review = int(self.thresholds["high"]), int(self.thresholds["review"])
        classification = "HIGH" if score >= high else ("REVIEW" if score >= review else "REJECT")
        matched = [k.keyword for k in title_hits + context_hits]
        categories: List[str] = []
        for k in title_hits + context_hits:
            for c in k.categories:
                if c not in categories:
                    categories.append(c)
        return Score(score, classification, matched[:50], categories[:30], groups,
                     f"{classification} {score}: " + "; ".join(reasons) + (f" | groups: {', '.join(groups)}"
                                                                         if groups else ""))


def parse_keyword_workbook(data: bytes) -> Dict[str, Any]:
    """Keywords and categories from a workbook like IT_Crawler_Keywords.xlsx.

    ``All Keywords`` (columns Keyword, Category) and ``By Category`` (a "Name (N keywords)"
    header row followed by keyword cells) are merged; a keyword listed without a category
    stays "Uncategorized" (never guessed). Problems (e.g. a Summary category with no
    keywords) are reported."""
    import io

    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    pairs: List[Tuple[str, str]] = []
    problems: List[str] = []
    names = {ws.title.lower(): ws for ws in book.worksheets}
    if "all keywords" in names:
        rows = list(names["all keywords"].iter_rows(values_only=True))
        header = [str(c or "").strip().lower() for c in rows[0]] if rows else []
        ki = header.index("keyword") if "keyword" in header else 1
        ci = header.index("category") if "category" in header else 2
        for row in rows[1:]:
            kw = row[ki] if len(row) > ki else None
            if kw in (None, ""):
                continue
            cat = row[ci] if len(row) > ci else None
            if cat in (None, ""):
                problems.append(f"'{kw}' has no category in All Keywords (kept as Uncategorized)")
            pairs.append((str(kw).strip(), str(cat).strip() if cat not in (None, "") else "Uncategorized"))
    if "by category" in names:
        category = None
        for row in names["by category"].iter_rows(values_only=True):
            cells = [c for c in row if c not in (None, "")]
            if not cells:
                continue
            match = re.match(r"^\s*(.+?)\s*\((\d+) keywords?\)\s*$", str(cells[0]))
            if len(cells) == 1 and match:
                category = match.group(1).strip()
                continue
            if category is None:
                continue
            pairs.extend((str(c).strip(), category) for c in cells)
    if not pairs:
        raise ValueError("no keywords found (expected an 'All Keywords' or 'By Category' sheet)")
    seen: Set[Tuple[str, str]] = set()
    keywords = []
    for kw, cat in pairs:
        key = (kw.lower(), cat)
        if key not in seen:
            seen.add(key)
            keywords.append({"keyword": kw, "category": cat})
    categories = list(dict.fromkeys(k["category"] for k in keywords))
    if "summary" in names:
        listed = [str(r[0]).strip() for r in names["summary"].iter_rows(values_only=True)
                  if r and r[0] and str(r[0]).strip() not in ("Category", "TOTAL", "IT Keyword Categories Summary")]
        for cat in listed:
            if cat not in categories:
                problems.append(f"Summary lists category '{cat}' but no keyword carries it")
    unmapped = [c for c in categories if c not in DEFAULT_GROUPS and c not in ("Category Words (Broad Net)",
                                                                               "Uncategorized")]
    if unmapped:
        problems.append("categories without a business group (scored as 'Other IT'): " + ", ".join(unmapped))
    return {"keywords": keywords, "categories": categories, "problems": problems,
            "distinct_keywords": len({k['keyword'].lower() for k in keywords})}
