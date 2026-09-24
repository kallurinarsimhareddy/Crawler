# VENDORED from CareerCrawler-seamless seamless/targeting.py (uncommitted work on seamless-integration) on 2026-09-24 (pure logic, imports rewritten to cloud.intel.vendor).
# Keep behaviour identical to the original; its tests there remain the reference.
"""Which of the people a search returned are worth a credit.

A contact search costs one credit whether it returns one person or ten. That
single measured fact (see :mod:`seamless.credits`) changes the shape of the
problem: discovery is effectively free at the margin, and the expensive step is
deciding *who to research*. So the right move is to ask for a generous pool of
candidates in the one paid call and then spend the research credits on the best
of them, rather than asking for exactly four and buying whoever the index
happened to rank first.

That is what this module decides. Two rules, and the second is the one that
makes the difference:

**Seniority is scored, not filtered.** A "Chief Human Resources Officer" and an
"HR Coordinator" both come back from a search filtered to Human Resources. Only
one of them signs anything.

**Functions are covered before they are stacked.** Four people from the same
department is a worse buy than one each from HR, Finance, IT and the executive
suite -- the message that lands depends on who reads it, and four HR generalists
at one company are four attempts at the same conversation. So selection goes
round the functions first and only doubles up once every function present has
been represented::

    >>> titles = ["HR Manager", "CHRO", "CFO", "HR Business Partner"]
    >>> [c.title for c in select(_as_matches(titles), wanted=2)]  # doctest: +SKIP
    ['CHRO', 'CFO']

Nothing here invents a person or a title. It only ranks what Seamless returned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Final, FrozenSet, List, Sequence, Tuple

__all__ = [
    "DEPARTMENTS",
    "VALID_DEPARTMENTS",
    "validate_departments",
    "FUNCTIONS",
    "SENIORITIES",
    "classify",
    "score",
    "select",
]

#: Every department value the API actually accepts, discovered by probing: a
#: request naming an unknown one is rejected with HTTP 400 and a message listing
#: the offenders, which costs nothing, so the whole enum can be mapped for free.
#:
#: This list exists because guessing was expensive. A first run went out with
#: "Executive" and "Information Technology" -- both plausible, both wrong -- and
#: burned 54 credits on company searches whose follow-up contact search 400'd
#: every time. :func:`validate_departments` now refuses before anything is spent.
VALID_DEPARTMENTS: Final[FrozenSet[str]] = frozenset({
    "Human Resources",
    "Finance",
    "Operations",
    "Sales",
    "Marketing",
    "Engineering",
    "IT",
    "Legal",
    "Support",
})

#: The department filter sent to Seamless: where the people who own a hiring
#: requisition or the budget behind it actually sit. Four, not five, because
#: there is no "Executive" department -- a CEO comes back with ``department:
#: "Other"``, so no department filter reaches one. The COO does arrive, under
#: Operations, which is the most senior person this filter can reach.
DEPARTMENTS: Final[Tuple[str, ...]] = (
    "Human Resources",
    "Finance",
    "IT",
    "Operations",
)


def validate_departments(values: Sequence[str]) -> List[str]:
    """Reject unknown department values before a run can spend on them.

    Args:
        values: The departments a run intends to filter by.

    Returns:
        The offending values, in the order given. Empty when all are valid.
    """
    return [value for value in values if value and value not in VALID_DEPARTMENTS]

#: The seniority bands asked for. Also capped at five by the API. "Manager" is
#: included because an HR Manager at a five-hundred-person firm is often the
#: person who actually runs the requisition, and excluding them loses the
#: buyer at exactly the company size this campaign targets.
SENIORITIES: Final[Tuple[str, ...]] = ("C-Level", "VP", "Director", "Manager")

#: The functions a title can belong to, most valuable to this campaign first.
#: Order matters: it breaks ties when two candidates score the same.
FUNCTIONS: Final[Tuple[str, ...]] = (
    "hr",
    "executive",
    "it",
    "finance",
    "operations",
    "other",
)

#: Title fragments that place somebody in a function. Matched case-insensitively
#: on word boundaries, longest first, so "chief people officer" is read as HR
#: rather than as a generic chief.
_FUNCTION_PATTERNS: Final[Tuple[Tuple[str, Tuple[str, ...]], ...]] = (
    (
        "hr",
        (
            "chief human resources", "chief people", "chro", "cpo",
            "human resources", "talent acquisition", "talent management",
            "people operations", "people ops", "recruiting", "recruitment",
            "recruiter", "staffing", "hr business partner", "hrbp",
            "head of people", "head of talent", "employee experience", "hris",
            " hr ", "hr",
        ),
    ),
    (
        "finance",
        (
            "chief financial", "cfo", "controller", "treasurer",
            "financial planning", "finance", "accounting", "procurement",
        ),
    ),
    (
        "it",
        (
            "chief information security", "chief technology", "chief information",
            "chief digital", "ciso", "cto", "cio", "information technology",
            "information security", "engineering", "technology", "software",
            "infrastructure", "cybersecurity", "security", "data", "it ",
        ),
    ),
    (
        "executive",
        (
            "chief executive", "chief operating", "ceo", "coo", "president",
            "owner", "founder", "co-founder", "managing director",
            "general manager", "principal", "partner", "chief",
        ),
    ),
    (
        "operations",
        ("operations", "supply chain", "manufacturing", "plant manager", "logistics"),
    ),
)

#: Seniority fragments and what they are worth. Scored from the title, because
#: the ``seniority`` field Seamless returns is often empty on a search result
#: while the title almost never is.
_SENIORITY_SCORES: Final[Tuple[Tuple[str, int], ...]] = (
    ("chief", 100),
    ("chro", 100),
    ("ceo", 100),
    ("cfo", 100),
    ("coo", 100),
    ("cio", 100),
    ("cto", 100),
    ("ciso", 100),
    ("cpo", 100),
    ("president", 95),
    ("owner", 92),
    ("founder", 92),
    ("partner", 80),
    ("managing director", 88),
    ("executive vice president", 85),
    ("evp", 85),
    ("senior vice president", 82),
    ("svp", 82),
    ("vice president", 78),
    ("vp", 78),
    ("head of", 70),
    ("director", 65),
    ("controller", 62),
    ("treasurer", 62),
    ("senior manager", 50),
    ("manager", 45),
    ("lead", 30),
    ("supervisor", 25),
)

#: Titles that are never worth a research credit for this campaign, whatever
#: else the string contains. An "HR Intern" matches "hr" and would otherwise
#: outrank nobody but still consume a credit.
_DISQUALIFYING: Final[Tuple[str, ...]] = (
    "intern", "trainee", "apprentice", "assistant", "administrative assistant",
    "receptionist", "coordinator", "clerk", "student", "volunteer", "retired",
    "former", "specialist", "generalist", "associate", "analyst",
)

#: Bonus for a function this campaign most wants to reach, applied on top of
#: the seniority score so that a Director of Talent Acquisition outranks a
#: Director of Facilities without the seniority table needing to know why.
_FUNCTION_BONUS: Final[Dict[str, int]] = {
    "hr": 30,
    "executive": 22,
    "it": 18,
    "finance": 16,
    "operations": 4,
    "other": 0,
}

_WORD = re.compile(r"[^a-z0-9]+")


def _normalise(title: str) -> str:
    """Reduce a title to a padded, lowercase, single-spaced form.

    Padding with spaces lets a pattern like ``" hr "`` match the word without
    also matching the ``hr`` inside "Chairman".

    Args:
        title: The job title.

    Returns:
        The comparable form.
    """
    return f" {_WORD.sub(' ', (title or '').lower()).strip()} "


def _contains(text: str, fragment: str) -> bool:
    """Whether a normalised title contains a fragment as whole words.

    Word boundaries are not a nicety here. ``"cto"`` is a substring of
    ``"director"``, so a loose ``in`` test scores every Director in the world as
    a chief technology officer and files "Sales Director" under IT. Both were
    observed before this function existed. Since :func:`_normalise` pads the
    text and collapses every separator to a single space, requiring the
    fragment to be space-delimited is both the correct test and a cheap one.

    Args:
        text: A title already through :func:`_normalise`.
        fragment: The phrase to look for.

    Returns:
        Whether the fragment appears as a whole word or whole phrase.
    """
    cleaned = fragment.strip()
    return bool(cleaned) and f" {cleaned} " in text


def classify(title: str) -> str:
    """Work out which function a job title belongs to.

    Args:
        title: The job title, as Seamless returned it.

    Returns:
        One of :data:`FUNCTIONS`. ``"other"`` when nothing matches, which is a
        real answer rather than a failure: a "VP of Sales" is a senior person
        this campaign is not aimed at.
    """
    text = _normalise(title)

    for function, fragments in _FUNCTION_PATTERNS:
        for fragment in fragments:
            if _contains(text, fragment):
                return function

    return "other"


def score(title: str, seniority: str = "", department: str = "") -> int:
    """Rate how much a person is worth a research credit.

    Args:
        title: The job title.
        seniority: The band Seamless reported, used only as a fallback when the
            title says nothing recognisable.
        department: The department Seamless reported, used the same way.

    Returns:
        A score. ``0`` means "do not spend a credit on this person" -- either
        the title disqualifies them or nothing in it suggests seniority.
    """
    text = _normalise(title)

    for banned in _DISQUALIFYING:
        if _contains(text, banned):
            return 0

    rank = 0
    for fragment, value in _SENIORITY_SCORES:
        if _contains(text, fragment):
            rank = max(rank, value)

    if rank == 0:
        # Nothing in the title. Fall back to what the index said, which is
        # weaker but better than discarding somebody the API called a C-Level.
        band = (seniority or "").strip().lower()
        rank = {
            "c-level": 90, "c level": 90, "owner": 88, "partner": 78,
            "vp": 70, "director": 60, "manager": 40,
        }.get(band, 0)

    if rank == 0:
        return 0

    function = classify(title)
    if function == "other" and department:
        function = classify(department)

    return rank + _FUNCTION_BONUS.get(function, 0)


@dataclass(frozen=True)
class _Ranked:
    """One candidate, with everything selection needs to order them.

    Attributes:
        index: Position in the original results, so a tie falls back to the
            order Seamless itself considered most relevant.
        match: The candidate.
        function: Which function they belong to.
        points: Their score.
    """

    index: int
    match: Any
    function: str
    points: int


def select(matches: Sequence[Any], wanted: int) -> List[Any]:
    """Choose which of a search's results to spend research credits on.

    Covers each function once before giving any function a second person, so
    that four credits at one company buy four different conversations rather
    than four variations of the same one.

    Args:
        matches: Contact search results. Each needs ``title``, and may carry
            ``seniority`` and ``department``.
        wanted: How many to research.

    Returns:
        The chosen candidates, best first, never more than ``wanted``. Anybody
        scoring zero is excluded even when that returns fewer than asked for --
        an unspent credit is worth more than a coordinator's email address.
    """
    if wanted <= 0:
        return []

    ranked: List[_Ranked] = []
    for index, match in enumerate(matches):
        title = str(getattr(match, "title", "") or "")
        points = score(
            title,
            str(getattr(match, "seniority", "") or ""),
            str(getattr(match, "department", "") or ""),
        )
        if points <= 0:
            continue
        ranked.append(_Ranked(index, match, classify(title) if title else "other", points))

    if not ranked:
        return []

    # Best first; a tie goes to the function this campaign values more, then to
    # the order Seamless returned.
    order = {name: position for position, name in enumerate(FUNCTIONS)}
    ranked.sort(key=lambda item: (-item.points, order.get(item.function, 99), item.index))

    by_function: Dict[str, List[_Ranked]] = {}
    for item in ranked:
        by_function.setdefault(item.function, []).append(item)

    chosen: List[_Ranked] = []
    while len(chosen) < wanted:
        took_one = False
        for function in sorted(by_function, key=lambda name: order.get(name, 99)):
            queue = by_function[function]
            if not queue:
                continue
            chosen.append(queue.pop(0))
            took_one = True
            if len(chosen) == wanted:
                break
        if not took_one:
            break

    chosen.sort(key=lambda item: (-item.points, item.index))
    return [item.match for item in chosen]
