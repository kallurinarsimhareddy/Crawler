"""Decide whether a posting is an IT or technology role.

``CURRENT_JOBS`` holds technology roles only. The reference sheet carries
237,300 postings across 8,275 companies — manufacturers, hospitals, retailers,
universities — and the great majority are not roles this crawler's operator
recruits for. Filtering them out of the current view is what keeps the tab
usable; ``JOB_HISTORY`` still records every posting, so nothing is lost and the
filter can be changed later without re-crawling.

    >>> from crawler.tech_filter import is_tech_job
    >>> is_tech_job("Senior Software Engineer")
    True
    >>> is_tech_job("Sales Engineer")
    False
    >>> is_tech_job("Process Engineer")
    False

The hard part is the word *engineer*. It appears in roughly as many
manufacturing postings as technical ones — process, quality, mechanical,
project, field service, environmental — so matching it alone would fill the tab
with plant jobs. Three rules handle it:

1. **An unambiguous phrase decides immediately.** ``"software engineer"``,
   ``"devops"``, ``"data scientist"``, ``"penetration tester"`` mean one thing.
2. **A generic role needs a technical qualifier.** ``"engineer"``,
   ``"analyst"``, ``"architect"``, ``"manager"`` count only alongside a word
   like ``"software"``, ``"cloud"``, ``"security"`` or ``"network"``.
3. **An exclusion overrules both.** ``"sales engineer"`` contains a technical
   qualifier by any reading and is not a technology role; nor is
   ``"security guard"``, however much it matches on ``"security"``.

The tables are deliberately visible and editable rather than clever. When the
operator finds the filter wrong about a title, the fix is a line here.
"""

from __future__ import annotations

import re
from typing import Final, FrozenSet, Iterable, Optional, Sequence, Tuple

from utils.encoding import strip_accents

__all__ = [
    "CORE_TECH",
    "EXCLUSIONS",
    "HARD_EXCLUSIONS",
    "SOFT_EXCLUSIONS",
    "GENERIC_ROLES",
    "TECH_PHRASES",
    "TECH_QUALIFIERS",
    "is_tech_job",
    "why",
]

#: Phrases that identify a technology role on their own. Matched as substrings
#: of the normalised title, so word order and punctuation inside them matter.
TECH_PHRASES: Final[FrozenSet[str]] = frozenset(
    {
        # Software.
        "software engineer", "software developer", "software architect",
        "software development", "software test", "developer", "programmer",
        "full stack", "fullstack", "back end", "backend", "front end", "frontend",
        "web developer", "mobile developer", "ios developer", "android developer",
        "application developer", "applications developer", "api developer",
        "embedded software", "firmware", "game developer", "salesforce developer",
        # Operations and infrastructure.
        "devops", "devsecops", "site reliability", "sre", "platform engineer",
        "infrastructure engineer", "cloud engineer", "cloud architect",
        "systems engineer", "systems administrator", "system administrator",
        "sysadmin", "network engineer", "network administrator", "network architect",
        "linux administrator", "windows administrator", "kubernetes", "terraform",
        # Data.
        "data engineer", "data scientist", "data analyst", "data architect",
        "database administrator", "database engineer", "business intelligence",
        "machine learning", "artificial intelligence", "deep learning",
        "data warehouse", "analytics engineer", "etl developer", "big data",
        # Security.
        "cyber security", "cybersecurity", "information security", "infosec",
        "security engineer", "security analyst", "security architect",
        "penetration test", "pen tester", "soc analyst", "threat intelligence",
        "application security", "identity and access",
        # Quality and release.
        "qa engineer", "software test", "software qa", "test automation",
        "qa automation", "automation tester", "sdet", "release engineer",
        "build engineer", "quality assurance analyst",
        # Support and service.
        "help desk", "helpdesk", "service desk", "technical support",
        "desktop support", "it support", "it technician", "it specialist",
        "it manager", "it director", "it analyst", "it administrator",
        "information technology",
        # Enterprise systems.
        "erp", "sap consultant", "sap analyst", "oracle developer",
        "salesforce administrator", "sharepoint", "dynamics 365", "netsuite",
        "workday integration",
        # Roles around the work.
        "scrum master", "solutions architect", "solution architect",
        "enterprise architect", "technical architect", "technical program manager",
        "technical product manager", "product owner", "ux designer",
        "ui designer", "ux researcher",
    }
)

#: Words that make an otherwise generic role technical.
TECH_QUALIFIERS: Final[FrozenSet[str]] = frozenset(
    {
        "software", "application", "applications", "systems", "system",
        "network", "cloud", "data", "database", "security", "cyber",
        "infrastructure", "platform", "digital", "technology", "technical",
        "computer", "informatics", "integration", "api",
        "web", "mobile", "devops", "it", "ai", "ml", "analytics",
        "python", "java", "javascript", "dotnet", ".net", "c++", "golang",
        "aws", "azure", "gcp", "sql", "linux", "windows", "sap", "oracle",
        "salesforce", "servicenow", "informatica", "tableau", "powerbi",
    }
)

#: Roles that are technical only when qualified.
GENERIC_ROLES: Final[FrozenSet[str]] = frozenset(
    {
        "engineer", "engineers", "engineering", "analyst", "analysts",
        "architect", "architects", "administrator", "administrators",
        "specialist", "specialists", "consultant", "consultants",
        "developer", "developers", "manager", "director", "lead", "principal",
        "technician", "technicians", "coordinator", "associate", "intern",
        "designer", "designers", "scientist", "scientists", "researcher",
        "operator", "vice president", "head of", "chief", "officer",
    }
)

#: Terms whose presence settles it: the role is not a technology one, whatever
#: else the title says. ``"Sales Engineer - Software Platform"`` is a sales job,
#: and ``"CNC Programmer"`` is shop-floor work however much it matches
#: ``"programmer"``.
HARD_EXCLUSIONS: Final[FrozenSet[str]] = frozenset(
    {
        # Selling technology is not building it.
        "sales engineer", "sales", "account executive", "account manager",
        "business development", "pre sales", "presales", "inside sales",
        "customer success", "territory manager", "sales representative",
        "technical sales",
        # Machining and industrial controls. Hard rather than soft, because
        # "CNC Programmer" and "PLC Programmer" both match the software phrase
        # "programmer" -- 67 and 5 postings respectively on the reference sheet.
        "cnc", "plc", "scada", "hvac", "instrumentation", "calibration",
        "industrial automation", "substation", "tendering",
        "automation technician", "controls technician", "robotics technician",
        "test technician", "quality systems", "automotive systems",
        # Servicing equipment in the field, which matches "technical".
        "technical service", "service technician", "field service technician",
        # Physical security, which matches "security" and is not infosec.
        "security guard", "security officer", "security supervisor",
        "loss prevention",
        # Clinical, laboratory and trades.
        "nurse", "physician", "therapist", "pharmacist", "dental",
        "patient care", "laboratory technician", "lab technician", "phlebotom",
        "electrician", "plumber", "welder", "machinist", "millwright",
        "driver", "forklift", "assembler", "janitor", "custodian",
        "housekeep", "cook", "cashier", "geotechnical", "hydraulic",
        # Physical work that matches a qualifier. Narrow on purpose: a bare
        # "warehouse" exclusion also rejected "Warehouse Management Systems
        # (WMS) Developer", which is a software role.
        "warehouse associate", "warehouse worker", "warehouse clerk",
        "warehouse supervisor", "warehouse manager", "warehouse lead",
        "material handler", "machine operator", "production operator",
        # Non-technical office roles that can match a qualifier.
        "financial analyst", "accounting", "accountant", "payroll",
        "human resources", "hr generalist", "recruiter", "talent acquisition",
        "marketing", "social media", "copywriter", "paralegal",
        "customer service", "data entry",
    }
)

#: Engineering and operations disciplines that are not information technology —
#: unless the title also names something unmistakably software or IT.
#:
#: The distinction earns its keep: on the reference sheet a flat exclusion list
#: rejected ``"Software Controls Engineer"``, ``"Senior Software Quality
#: Engineer"`` and ``"Software Commissioning & Support Engineer"``, all of which
#: are software roles that happen to sit next to a plant.
SOFT_EXCLUSIONS: Final[FrozenSet[str]] = frozenset(
    {
        "process engineer", "mechanical engineer", "electrical engineer",
        "chemical engineer", "civil engineer", "structural engineer",
        "industrial engineer", "manufacturing engineer", "production engineer",
        "quality engineer", "packaging engineer", "environmental engineer",
        "safety engineer", "reliability engineer", "maintenance engineer",
        "project engineer", "design engineer", "product engineer",
        "field service engineer", "field engineer", "service engineer",
        "application engineer", "applications engineer", "plant engineer",
        "facilities engineer", "controls engineer", "automotive engineer",
        "aerospace engineer", "materials engineer", "tooling engineer",
        "welding engineer", "petroleum engineer", "mining engineer",
        "commissioning",
    }
)

#: Terms unmistakable enough to overrule a soft exclusion. Deliberately much
#: narrower than :data:`TECH_QUALIFIERS`: "systems" or "technical" beside
#: "Controls Engineer" means industrial controls, but "software" does not.
CORE_TECH: Final[FrozenSet[str]] = frozenset(
    {
        "software", "developer", "programmer", "devops", "cloud",
        "cybersecurity", "cyber security", "information security",
        "data engineer", "data scientist", "database", "network engineer",
        "full stack", "backend", "frontend", "firmware", "embedded software",
        "it support", "sysadmin", "systems administrator", "web",
        "python", "java", "sql", "linux", "api",
    }
)

#: Kept for callers that want the whole vocabulary in one place.
EXCLUSIONS: Final[FrozenSet[str]] = HARD_EXCLUSIONS | SOFT_EXCLUSIONS

#: Applied before matching, so ``"Sr. Software Eng."`` and
#: ``"Senior Software Engineer"`` are compared on the same footing.
_ABBREVIATIONS: Final[Tuple[Tuple[str, str], ...]] = (
    (r"\bsr\b", "senior"),
    (r"\bjr\b", "junior"),
    (r"\beng\b", "engineer"),
    (r"\bengr\b", "engineer"),
    (r"\bdev\b", "developer"),
    (r"\badmin\b", "administrator"),
    (r"\bmgr\b", "manager"),
    (r"\bqa\b", "qa"),
    (r"\bi\.?t\.?\b", "it"),
    (r"\bs\.?w\.?\b", "software"),
    (r"\bsw\b", "software"),
    (r"\bml\b", "ml"),
    (r"\bai\b", "ai"),
)

#: Anything that is not a letter, digit, ``+``, ``#`` or ``.`` — the characters
#: that carry meaning in ``C++``, ``C#`` and ``.NET``.
_PUNCTUATION: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9+#. ]+")

#: Runs of whitespace.
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Fold a title to the form the tables are written against.

    Args:
        text: A job title, department, or both.

    Returns:
        The text lowercased, accent-folded, punctuation-stripped, with common
        abbreviations expanded and padded with spaces so that whole-word
        matching can be done with plain substring tests.
    """
    folded = strip_accents(str(text or "")).lower()
    folded = _PUNCTUATION.sub(" ", folded)
    folded = _WHITESPACE.sub(" ", folded).strip()

    for pattern, replacement in _ABBREVIATIONS:
        folded = re.sub(pattern, replacement, folded)

    return f" {_WHITESPACE.sub(' ', folded).strip()} "


def _contains(haystack: str, needle: str) -> bool:
    """Whether a normalised haystack contains a phrase as whole words.

    Args:
        haystack: Normalised text, space-padded at both ends.
        needle: The phrase to look for.

    Returns:
        ``True`` on a whole-word match, so ``"it"`` does not match ``"unit"``.
    """
    return f" {needle} " in haystack or haystack.startswith(f" {needle} ")


def why(title: str, department: str = "", extra_keywords: Iterable[str] = ()) -> Tuple[bool, str]:
    """Classify a posting and explain the verdict.

    Args:
        title: The posting title.
        department: The department, when the board publishes one. Considered
            alongside the title, since ``"Engineer"`` in an ``"IT"`` department
            is a technology role and the title alone does not say so.
        extra_keywords: Additional phrases that should count as technical, for
            an operator who recruits into a niche the tables do not cover.

    Returns:
        ``(is_tech, reason)``. The reason names the phrase that decided it, so
        a wrong verdict can be traced to the line that produced it.
    """
    subject = _normalise(f"{title} {department}")

    if not subject.strip():
        return False, "no title"

    for phrase in HARD_EXCLUSIONS:
        if _contains(subject, phrase):
            return False, f"excluded by {phrase!r}"

    # A discipline exclusion yields to an unmistakable software term, so
    # "Software Controls Engineer" is admitted and "Controls Engineer" is not.
    soft = next((phrase for phrase in SOFT_EXCLUSIONS if _contains(subject, phrase)), None)
    if soft is not None:
        core = next((phrase for phrase in CORE_TECH if _contains(subject, phrase)), None)
        if core is None:
            return False, f"excluded by {soft!r}"

    for phrase in extra_keywords:
        folded = _normalise(phrase).strip()
        if folded and _contains(subject, folded):
            return True, f"matched configured keyword {folded!r}"

    for phrase in TECH_PHRASES:
        if _contains(subject, phrase):
            return True, f"matched {phrase!r}"

    role = next((word for word in GENERIC_ROLES if _contains(subject, word)), None)
    if role is not None:
        qualifier = next(
            (word for word in TECH_QUALIFIERS if _contains(subject, word)), None
        )
        if qualifier is not None:
            return True, f"{role!r} qualified by {qualifier!r}"
        return False, f"{role!r} with no technical qualifier"

    return False, "no technical term"


def is_tech_job(
    title: str,
    department: str = "",
    extra_keywords: Iterable[str] = (),
) -> bool:
    """Whether a posting is an IT or technology role.

    Args:
        title: The posting title.
        department: The department, when known.
        extra_keywords: Additional phrases that count as technical.

    Returns:
        ``True`` for a technology role.
    """
    return why(title, department, extra_keywords)[0]
