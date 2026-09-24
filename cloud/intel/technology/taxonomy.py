"""The technology taxonomy: what we look for in text, and what it means.

Each :class:`Tech` has one canonical name, a primary ``category`` and the
``families`` it also belongs to (``SAP ECC`` is category ``SAP``, families
``ERP`` and ``SAP``), a vendor, and the patterns that identify it.

**Matching is deliberately conservative.** A false technology claim poisons
hiring signals and campaign routing, so:

* every pattern is bounded by non-alphanumerics on both sides — ``RPG`` does
  not match ``RPGs`` (games) or ``RPGA``; ``Java`` does not match ``JavaScript``;
* ambiguous short names are *case-sensitive* (``RPG``, ``CL``, ``ECC``, ``JDE``);
* some names only count with supporting context elsewhere in the text
  (``ECC`` only near ``SAP``; ``CL`` only with ``AS/400``/``iSeries``/``RPG``);
* vendor words alone (``SAP``, ``Oracle``, ``Infor``, ``Sage``) are never a
  product — ``SAP`` is recorded as the vendor family ``SAP``, not ``SAP ECC``.

The ERP aliases vendored from the ZoomInfo ERP catalogue
(``data/erp_catalog.json``) extend the ERP entries; its ``excluded`` aliases are
honoured as exclusions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = ["CATEGORIES", "TAXONOMY", "Tech", "Match", "detect", "taxonomy_as_dict"]

CATEGORIES = ("ERP", "CRM", "Cloud", "MarTech", "Data", "Infrastructure", "Programming", "Analytics", "WMS",
              "iSeries/RPG", "SAP", "Oracle", "Dynamics", "JD Edwards", "Infor")


@dataclass(frozen=True)
class Tech:
    name: str
    category: str
    families: Tuple[str, ...]
    vendor: Optional[str]
    patterns: Tuple[str, ...]
    case_sensitive: bool = False
    #: The text must also contain one of these (regex, case-insensitive) for a match to count.
    requires_context: Tuple[str, ...] = ()
    #: Phrases that, when they are what matched, are rejected (e.g. "SAP Business One" is not "SAP ECC").
    excludes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Match:
    technology: str
    category: str
    families: Tuple[str, ...]
    vendor: Optional[str]
    matched: str
    evidence_text: str
    start: int


def _t(name, category, patterns, *, families=None, vendor=None, cs=False, ctx=(), excludes=()) -> Tech:
    fams = tuple(families) if families else (category,)
    if category not in fams:
        fams = (category,) + fams
    return Tech(name, category, fams, vendor, tuple(patterns), cs, tuple(ctx), tuple(excludes))


_ISERIES_CTX = (r"as/?400", r"i\s?series", r"ibm\s?i\b", r"\brpg", r"system\s?i\b", r"\bpower\s?i\b")

_BASE: List[Tech] = [
    # --- SAP ---------------------------------------------------------------
    _t("SAP S/4HANA", "SAP", [r"S/4\s?HANA", r"S4\s?HANA", r"SAP\s+S/4"], families=["ERP"], vendor="SAP SE"),
    _t("SAP ECC", "SAP", [r"SAP\s+ECC(?:\s?6(?:\.0)?)?", r"SAP\s+ERP\s+Central\s+Component", r"ECC\s?6(?:\.0)?",
                          r"ECC"], families=["ERP"], vendor="SAP SE", cs=False, ctx=(r"\bSAP\b",)),
    _t("SAP Business One", "SAP", [r"SAP\s+Business\s+One", r"SAP\s+B1"], families=["ERP"], vendor="SAP SE"),
    _t("SAP Business ByDesign", "SAP", [r"SAP\s+Business\s+ByDesign", r"SAP\s+ByD"], families=["ERP"], vendor="SAP SE"),
    _t("SAP ABAP", "SAP", [r"ABAP"], families=["Programming"], vendor="SAP SE", cs=True),
    _t("SAP FICO", "SAP", [r"SAP\s+FI/?CO", r"FICO"], families=["ERP"], vendor="SAP SE", cs=True, ctx=(r"\bSAP\b",)),
    _t("SAP MM", "SAP", [r"SAP\s+MM"], families=["ERP"], vendor="SAP SE", cs=True),
    _t("SAP SD", "SAP", [r"SAP\s+SD"], families=["ERP"], vendor="SAP SE", cs=True),
    _t("SAP PP", "SAP", [r"SAP\s+PP"], families=["ERP"], vendor="SAP SE", cs=True),
    _t("SAP BW", "SAP", [r"SAP\s+BW(?:/4HANA)?"], families=["Data"], vendor="SAP SE", cs=True),
    _t("SAP Basis", "SAP", [r"SAP\s+Basis"], families=["Infrastructure"], vendor="SAP SE"),
    _t("SAP SuccessFactors", "SAP", [r"SuccessFactors"], families=["ERP"], vendor="SAP SE"),
    _t("SAP Ariba", "SAP", [r"SAP\s+Ariba", r"Ariba"], vendor="SAP SE"),
    _t("SAP EWM", "SAP", [r"SAP\s+EWM", r"Extended\s+Warehouse\s+Management"], families=["WMS"], vendor="SAP SE"),
    _t("SAP (vendor)", "SAP", [r"SAP"], vendor="SAP SE", cs=True),
    # --- Oracle ------------------------------------------------------------
    _t("Oracle E-Business Suite", "Oracle", [r"Oracle\s+E-?\s?Business\s+Suite", r"Oracle\s+EBS", r"EBS\s+R12",
                                             r"Oracle\s+Apps\s+R12"], families=["ERP"], vendor="Oracle"),
    _t("Oracle NetSuite", "Oracle", [r"NetSuite"], families=["ERP"], vendor="Oracle"),
    _t("Oracle Fusion Cloud ERP", "Oracle", [r"Oracle\s+Fusion", r"Oracle\s+Cloud\s+ERP", r"Oracle\s+ERP\s+Cloud"],
       families=["ERP"], vendor="Oracle"),
    _t("Oracle PeopleSoft", "Oracle", [r"PeopleSoft"], families=["ERP"], vendor="Oracle"),
    _t("Oracle Database", "Oracle", [r"Oracle\s+(?:Database|DB|RDBMS|1[89]c|12c|11g)", r"PL/SQL"], families=["Data"],
       vendor="Oracle"),
    # --- JD Edwards --------------------------------------------------------
    _t("JD Edwards EnterpriseOne", "JD Edwards", [r"JD\s?Edwards\s+EnterpriseOne", r"JDE\s+E1", r"JDE\s+EnterpriseOne",
                                                  r"EnterpriseOne", r"E1\s+9\.2"],
       families=["ERP", "Oracle"], vendor="Oracle", ctx=(r"JD\s?Edwards|JDE|EnterpriseOne",)),
    _t("JD Edwards World", "JD Edwards", [r"JD\s?Edwards\s+World", r"JDE\s+World", r"JDE\s+A7\.3", r"JDE\s+A9\.\d"],
       families=["ERP", "Oracle", "iSeries/RPG"], vendor="Oracle"),
    _t("JD Edwards", "JD Edwards", [r"J\.?D\.?\s?Edwards", r"JDE"], families=["ERP", "Oracle"], vendor="Oracle", cs=True),
    # --- Infor -------------------------------------------------------------
    _t("Infor LN", "Infor", [r"Infor\s+LN", r"Baan(?:\s?IV|\s?V)?"], families=["ERP"], vendor="Infor"),
    _t("Infor M3", "Infor", [r"Infor\s+M3", r"Movex"], families=["ERP"], vendor="Infor"),
    _t("Infor SyteLine", "Infor", [r"SyteLine", r"CloudSuite\s+Industrial", r"Infor\s+CSI"], families=["ERP"], vendor="Infor"),
    _t("Infor XA", "Infor", [r"Infor\s+XA", r"MAPICS"], families=["ERP", "iSeries/RPG"], vendor="Infor"),
    _t("Infor Visual", "Infor", [r"Infor\s+VISUAL", r"VISUAL\s+Manufacturing"], families=["ERP"], vendor="Infor"),
    _t("Infor CloudSuite", "Infor", [r"Infor\s+CloudSuite"], families=["ERP"], vendor="Infor"),
    _t("Infor (vendor)", "Infor", [r"Infor"], vendor="Infor", cs=True),
    # --- Microsoft Dynamics --------------------------------------------------
    _t("Dynamics 365 Finance & Operations", "Dynamics",
       [r"D365\s?F&?O", r"D365\s+Finance(?:\s+(?:&|and)\s+Operations)?", r"Dynamics\s+365\s+(?:for\s+)?Finance",
        r"Dynamics\s+365\s+F&?O", r"Dynamics\s+365\s+Supply\s+Chain"], families=["ERP"], vendor="Microsoft"),
    _t("Dynamics 365 Business Central", "Dynamics", [r"Business\s+Central", r"D365\s?BC"], families=["ERP"],
       vendor="Microsoft", ctx=(r"Dynamics|D365|Navision|\bNAV\b|Microsoft",)),
    _t("Dynamics AX", "Dynamics", [r"Dynamics\s+AX", r"AX\s?2012", r"Axapta"], families=["ERP"], vendor="Microsoft"),
    _t("Dynamics NAV", "Dynamics", [r"Dynamics\s+NAV", r"Navision"], families=["ERP"], vendor="Microsoft"),
    _t("Dynamics GP", "Dynamics", [r"Dynamics\s+GP", r"Great\s+Plains"], families=["ERP"], vendor="Microsoft"),
    _t("Dynamics 365 CRM", "Dynamics", [r"Dynamics\s+365\s+(?:CRM|Sales|Customer\s+Service)", r"Dynamics\s+CRM"],
       families=["CRM"], vendor="Microsoft"),
    _t("Dynamics 365", "Dynamics", [r"Dynamics\s+365", r"D365"], families=["ERP"], vendor="Microsoft"),
    # --- other ERP ---------------------------------------------------------
    _t("Epicor", "ERP", [r"Epicor(?:\s+(?:Kinetic|ERP|10|Prophet\s?21|Vantage))?", r"Prophet\s?21"], vendor="Epicor"),
    _t("QAD", "ERP", [r"QAD(?:\s+(?:Adaptive|Enterprise))?", r"MFG/PRO"], vendor="QAD", cs=True),
    _t("Sage X3", "ERP", [r"Sage\s+X3"], vendor="Sage"),
    _t("Sage Intacct", "ERP", [r"Sage\s+Intacct", r"Intacct"], vendor="Sage"),
    _t("Sage 100", "ERP", [r"Sage\s+100", r"Sage\s+MAS\s?90", r"Sage\s+MAS\s?200", r"MAS\s?90", r"MAS\s?200"], vendor="Sage"),
    _t("Sage 300", "ERP", [r"Sage\s+300", r"Accpac"], vendor="Sage"),
    _t("SYSPRO", "ERP", [r"SYSPRO"], vendor="SYSPRO"),
    _t("IQMS / DELMIAworks", "ERP", [r"IQMS", r"DELMIAworks", r"EnterpriseIQ"], vendor="Dassault Systèmes"),
    _t("Plex", "ERP", [r"Plex\s+(?:Systems|ERP|Manufacturing\s+Cloud|Smart\s+Manufacturing)", r"Rockwell\s+Plex"],
       vendor="Rockwell Automation"),
    _t("Acumatica", "ERP", [r"Acumatica"], vendor="Acumatica"),
    _t("Global Shop Solutions", "ERP", [r"Global\s+Shop\s+Solutions", r"One-System\s+ERP"], vendor="Global Shop Solutions"),
    _t("Glovia", "ERP", [r"Glovia"], vendor="Fujitsu"),
    _t("Macola", "ERP", [r"Macola"], vendor="ECI"),
    _t("Aptean Ross", "ERP", [r"Aptean\s+Ross", r"Ross\s+ERP", r"iRenaissance"], vendor="Aptean"),
    _t("Workday", "ERP", [r"Workday\s+(?:Financials|HCM|Financial\s+Management)"], vendor="Workday"),
    _t("Odoo", "ERP", [r"Odoo"], vendor="Odoo"),
    _t("IFS", "ERP", [r"IFS\s+(?:Applications|Cloud)"], vendor="IFS"),
    _t("Unit4", "ERP", [r"Unit4"], vendor="Unit4"),
    _t("ERP (generic)", "ERP", [r"ERP"], cs=True),
    # --- iSeries / RPG -------------------------------------------------------
    _t("IBM AS/400", "iSeries/RPG", [r"AS/?400", r"AS-400"], vendor="IBM"),
    _t("IBM iSeries", "iSeries/RPG", [r"i\s?Series", r"IBM\s+i", r"System\s+i", r"Power\s?i"], vendor="IBM"),
    _t("RPG", "iSeries/RPG", [r"RPG\s?IV", r"RPG\s?III", r"RPG\s?/?400", r"RPGLE", r"ILE\s+RPG", r"SQLRPGLE",
                              r"RPG\s+Free(?:-?form)?", r"RPG"],
       families=["Programming"], vendor="IBM", cs=True),
    _t("CL / CLLE", "iSeries/RPG", [r"CLLE", r"CL/400", r"CL"], families=["Programming"], vendor="IBM", cs=True,
       ctx=_ISERIES_CTX),
    _t("DB2 for i", "iSeries/RPG", [r"DB2/400", r"DB2\s+for\s+i", r"DB2/i"], families=["Data"], vendor="IBM"),
    _t("COBOL", "Programming", [r"COBOL(?:/400)?"], cs=False),
    # --- WMS ---------------------------------------------------------------
    _t("Manhattan WMS", "WMS", [r"Manhattan\s+(?:Associates|WMS|SCALE|Active|WMOS)"], vendor="Manhattan Associates"),
    _t("Blue Yonder", "WMS", [r"Blue\s+Yonder", r"JDA\s+(?:WMS|Software|Dispatcher)", r"RedPrairie"], vendor="Blue Yonder"),
    _t("HighJump / Körber", "WMS", [r"HighJump", r"K[öo]rber\s+(?:WMS|Supply\s+Chain)"], vendor="Körber"),
    _t("Oracle WMS", "WMS", [r"Oracle\s+(?:WMS|Warehouse\s+Management)"], families=["Oracle"], vendor="Oracle"),
    _t("WMS (generic)", "WMS", [r"WMS", r"Warehouse\s+Management\s+System"], cs=False),
    # --- CRM / MarTech ---------------------------------------------------------
    _t("Salesforce", "CRM", [r"Salesforce(?:\.com)?", r"SFDC", r"Sales\s?Cloud", r"Service\s?Cloud"], vendor="Salesforce"),
    _t("HubSpot", "CRM", [r"HubSpot"], families=["MarTech"], vendor="HubSpot"),
    _t("Zoho CRM", "CRM", [r"Zoho\s+CRM"], vendor="Zoho"),
    _t("Marketo", "MarTech", [r"Marketo"], vendor="Adobe"),
    _t("Pardot", "MarTech", [r"Pardot", r"Account\s+Engagement"], vendor="Salesforce"),
    _t("Eloqua", "MarTech", [r"Eloqua"], vendor="Oracle"),
    _t("Google Analytics", "MarTech", [r"Google\s+Analytics", r"GA4"], families=["Analytics"], vendor="Google"),
    # --- Cloud / Infrastructure --------------------------------------------------
    _t("AWS", "Cloud", [r"AWS", r"Amazon\s+Web\s+Services"], vendor="Amazon", cs=True),
    _t("Microsoft Azure", "Cloud", [r"Azure"], vendor="Microsoft"),
    _t("Google Cloud", "Cloud", [r"GCP", r"Google\s+Cloud(?:\s+Platform)?"], vendor="Google", cs=False),
    _t("Kubernetes", "Infrastructure", [r"Kubernetes", r"K8s", r"EKS", r"AKS", r"GKE"], cs=False),
    _t("Docker", "Infrastructure", [r"Docker"]),
    _t("Terraform", "Infrastructure", [r"Terraform"]),
    _t("VMware", "Infrastructure", [r"VMware", r"vSphere", r"ESXi"]),
    _t("ServiceNow", "Infrastructure", [r"ServiceNow"], vendor="ServiceNow"),
    _t("Linux", "Infrastructure", [r"Linux", r"RHEL", r"Red\s+Hat"]),
    _t("Active Directory", "Infrastructure", [r"Active\s+Directory", r"Entra\s+ID", r"Azure\s+AD"], vendor="Microsoft"),
    # --- Data / Analytics ----------------------------------------------------------
    _t("Snowflake", "Data", [r"Snowflake"], vendor="Snowflake"),
    _t("Databricks", "Data", [r"Databricks"], vendor="Databricks"),
    _t("Apache Spark", "Data", [r"(?:Apache\s+)?Spark", r"PySpark"]),
    _t("SQL Server", "Data", [r"SQL\s+Server", r"MSSQL", r"T-SQL"], vendor="Microsoft"),
    _t("PostgreSQL", "Data", [r"PostgreSQL", r"Postgres"]),
    _t("MySQL", "Data", [r"MySQL"]),
    _t("MongoDB", "Data", [r"MongoDB"]),
    _t("Informatica", "Data", [r"Informatica"]),
    _t("Kafka", "Data", [r"Kafka"]),
    _t("Power BI", "Analytics", [r"Power\s?BI"], vendor="Microsoft"),
    _t("Tableau", "Analytics", [r"Tableau"], vendor="Salesforce"),
    _t("Qlik", "Analytics", [r"Qlik(?:\s?Sense|View)?"]),
    _t("Looker", "Analytics", [r"Looker"], vendor="Google"),
    # --- Programming ---------------------------------------------------------------
    _t("Python", "Programming", [r"Python"]),
    _t("Java", "Programming", [r"Java(?!\s*Script)", r"J2EE", r"Spring\s+Boot"]),
    _t("JavaScript", "Programming", [r"JavaScript", r"Node\.?js", r"React(?:\.js)?", r"Angular"]),
    _t("TypeScript", "Programming", [r"TypeScript"]),
    _t(".NET", "Programming", [r"\.NET(?:\s+Core)?", r"C#", r"ASP\.NET"], vendor="Microsoft"),
    _t("Go", "Programming", [r"Golang"]),
    _t("SQL", "Programming", [r"SQL"], cs=True),
]

_ERP_KEY_TO_NAME = {
    "SAP_ECC": "SAP ECC", "JD_EDWARDS": "JD Edwards EnterpriseOne", "ORACLE_EBS": "Oracle E-Business Suite",
    "INFOR_VISUAL": "Infor Visual", "INFOR_LN": "Infor LN", "INFOR_SYTELINE": "Infor SyteLine", "QAD_MFG_PRO": "QAD",
    "DYNAMICS_NAV": "Dynamics NAV", "SAGE_100": "Sage 100", "SAGE_300": "Sage 300", "GLOVIA": "Glovia",
    "GLOBAL_SHOP_SOLUTIONS": "Global Shop Solutions", "IQMS_DELMIAWORKS": "IQMS / DELMIAworks", "SYSPRO": "SYSPRO",
    "MACOLA_10": "Macola", "APTEAN_ROSS": "Aptean Ross",
}

# Aliases in the ZoomInfo catalogue that are too ambiguous to match free text
# on their own (they are fine as ZoomInfo *filters*, not as regexes over a job ad).
_AMBIGUOUS_ALIASES = {"ECC", "ECC 6.0", "SAP EC", "SAP ERP", "Oracle Applications", "Visual Manufacturing", "Ross Systems"}


def _with_catalogue(base: List[Tech]) -> List[Tech]:
    path = Path(__file__).with_name("data") / "erp_catalog.json"
    try:
        catalogue = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return base
    by_name = {t.name: t for t in base}
    for erp in catalogue.get("erps", []):
        name = _ERP_KEY_TO_NAME.get(erp["key"])
        tech = by_name.get(name or "")
        if tech is None:
            continue
        aliases = [a for a in erp["aliases"].get("exact", []) + erp["aliases"].get("acceptable", [])
                   if a not in _AMBIGUOUS_ALIASES]
        extra = tuple(re.escape(a).replace(r"\ ", r"\s+") for a in aliases)
        by_name[name] = Tech(tech.name, tech.category, tech.families, tech.vendor, tech.patterns + extra,
                             tech.case_sensitive, tech.requires_context,
                             tech.excludes + tuple(erp["aliases"].get("excluded", [])))
    return [by_name[t.name] for t in base]


TAXONOMY: List[Tech] = _with_catalogue(_BASE)

_BOUND_L = r"(?<![A-Za-z0-9])"
_BOUND_R = r"(?![A-Za-z0-9])"


def _compile(tech: Tech) -> List[re.Pattern]:
    flags = 0 if tech.case_sensitive else re.IGNORECASE
    out = []
    for pattern in tech.patterns:
        # A pattern starting/ending with a non-word char (".NET", "C#") needs a looser bound on that side.
        left = _BOUND_L if re.match(r"[A-Za-z0-9\\(]", pattern) and not pattern.startswith(r"\.") else ""
        right = _BOUND_R if not pattern.endswith("#") else ""
        out.append(re.compile(left + "(?:" + pattern + ")" + right, flags))
    return out


_COMPILED: List[Tuple[Tech, List[re.Pattern], List[re.Pattern]]] = [
    (t, _compile(t), [re.compile(c, re.IGNORECASE) for c in t.requires_context]) for t in TAXONOMY
]

#: When a more specific product matched, drop these umbrella entries.
_UMBRELLAS = {
    "SAP (vendor)": "SAP", "Infor (vendor)": "Infor", "JD Edwards": "JD Edwards", "Dynamics 365": "Dynamics",
    "ERP (generic)": "ERP", "WMS (generic)": "WMS",
}


def _snippet(text: str, start: int, end: int, width: int = 90) -> str:
    left = max(0, start - width)
    right = min(len(text), end + width)
    return re.sub(r"\s+", " ", ("…" if left else "") + text[left:right] + ("…" if right < len(text) else "")).strip()


def _excluded(text: str, m: re.Match, excludes: Sequence[str]) -> bool:
    """A match is excluded when it *is* an excluded alias, or sits inside a longer
    excluded phrase ("ECC" inside "ECC SD"; "SAP ERP" inside "mySAP ERP")."""
    matched = m.group(0).lower()
    for ex in excludes:
        if matched == ex.lower():
            return True
        if len(ex) <= len(matched):
            continue
        for occ in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(ex) + r"(?![A-Za-z0-9])", text, re.IGNORECASE):
            if occ.start() <= m.start() and occ.end() >= m.end():
                return True
    return False


def detect(text: str) -> List[Match]:
    """Every technology found in ``text``, one :class:`Match` per technology (first occurrence)."""
    if not text:
        return []
    found: Dict[str, Match] = {}
    for tech, patterns, contexts in _COMPILED:
        if contexts and not any(c.search(text) for c in contexts):
            continue
        for pattern in patterns:
            for m in pattern.finditer(text):
                if _excluded(text, m, tech.excludes):
                    continue
                if tech.name not in found or m.start() < found[tech.name].start:
                    found[tech.name] = Match(tech.name, tech.category, tech.families, tech.vendor, m.group(0),
                                             _snippet(text, m.start(), m.end()), m.start())
                break
    # "SAP" alone is only the vendor family when nothing more specific from SAP matched, etc.
    for umbrella, category in _UMBRELLAS.items():
        if umbrella in found and any(m.technology != umbrella and m.technology not in _UMBRELLAS
                                     and (m.category == category or category in m.families)
                                     for m in found.values()):
            del found[umbrella]
    return sorted(found.values(), key=lambda m: m.start)


def taxonomy_as_dict() -> List[Dict[str, object]]:
    return [{"name": t.name, "category": t.category, "families": list(t.families), "vendor": t.vendor}
            for t in TAXONOMY]
