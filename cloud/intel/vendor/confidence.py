# VENDORED from CareerCrawler-seamless zerocredit/confidence.py (uncommitted work on seamless-integration) on 2026-09-24 (pure logic, imports rewritten to cloud.intel.vendor).
# Keep behaviour identical to the original; its tests there remain the reference.
"""How much to believe a fact, and where the belief came from.

Everything this subsystem produces is scraped off the public web, so the useful
question is never "did we find something" but "how much weight does it carry".
A name on a company's own leadership page and a name in a search snippet are
both strings; only one of them is worth putting in front of a salesperson as
fact.

So every value carries a tier, and the tier is derived from the *source*, not
from how confident the parser felt::

    >>> tier_for("official_leadership")
    'VERIFIED'
    >>> tier_for("search_snippet")
    'LOW'

The five tiers, and what each is allowed to mean:

``VERIFIED``
    The organisation itself published it: its leadership page, its own contact
    page, a filing it signed, a press release on its own domain. The company is
    the authority on who its CFO is.

``HIGH``
    A reputable third party that names its own source, or a page on the company
    domain that is not specifically a leadership page.

``MEDIUM``
    A public directory or aggregator. Usually right, occasionally years stale.

``LOW``
    A search-result snippet, or a page whose relationship to the company could
    not be established.

``UNKNOWN``
    Provenance could not be determined. Never presented as fact.

**Inference never reaches VERIFIED.** :func:`combine` takes the best tier of the
sources that actually assert a value; a value this code derived rather than read
is capped at :data:`MAX_INFERRED_TIER` no matter how many places agree, because
agreement between guesses is not evidence.
"""

from __future__ import annotations

from typing import Dict, Final, Iterable, Sequence, Tuple

__all__ = [
    "MAX_INFERRED_TIER",
    "SOURCE_SCORES",
    "TIERS",
    "combine",
    "score_for",
    "tier_for",
    "tier_of_score",
]

#: The tiers, weakest first. Order is the comparison.
TIERS: Final[Tuple[str, ...]] = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "VERIFIED")

#: Numeric score per tier, for storing and sorting.
_TIER_SCORE: Final[Dict[str, int]] = {
    "UNKNOWN": 0,
    "LOW": 25,
    "MEDIUM": 50,
    "HIGH": 75,
    "VERIFIED": 100,
}

#: Source type to the tier it earns. A source type absent from this table is
#: ``UNKNOWN`` -- an unrecognised provenance is not a good one.
SOURCE_SCORES: Final[Dict[str, str]] = {
    # the organisation speaking about itself
    "official_leadership": "VERIFIED",
    "official_team": "VERIFIED",
    "official_about": "VERIFIED",
    "official_contact": "VERIFIED",
    "official_press_release": "VERIFIED",
    "sec_filing": "VERIFIED",
    "government_registry": "VERIFIED",
    "structured_data": "VERIFIED",       # schema.org on the company's own site

    # the company's own domain, but not a page about its people
    "official_other": "HIGH",
    "official_careers": "HIGH",
    "reputable_press": "HIGH",

    # third parties
    "public_directory": "MEDIUM",
    "professional_profile": "MEDIUM",

    # weakest
    "search_snippet": "LOW",
    "unclassified_page": "LOW",
}

#: The best a value may claim when this code derived it rather than read it.
#: Agreement between inferences is not evidence, so no amount of corroboration
#: promotes an inferred value past this.
MAX_INFERRED_TIER: Final[str] = "LOW"


def tier_for(source_type: str) -> str:
    """The tier a source type earns.

    Args:
        source_type: One of the keys of :data:`SOURCE_SCORES`.

    Returns:
        The tier. ``"UNKNOWN"`` for anything unrecognised, because a source
        this code cannot name is not one it should vouch for.
    """
    return SOURCE_SCORES.get((source_type or "").strip().lower(), "UNKNOWN")


def score_for(tier: str) -> int:
    """The numeric score for a tier.

    Args:
        tier: One of :data:`TIERS`.

    Returns:
        The score, ``0`` for anything unrecognised.
    """
    return _TIER_SCORE.get((tier or "").strip().upper(), 0)


def tier_of_score(score: int) -> str:
    """The tier a numeric score corresponds to.

    Args:
        score: A score.

    Returns:
        The highest tier whose score does not exceed it.
    """
    best = "UNKNOWN"
    for tier in TIERS:
        if score >= _TIER_SCORE[tier]:
            best = tier
    return best


def best(tiers: Iterable[str]) -> str:
    """The strongest of several tiers.

    Args:
        tiers: The tiers to compare.

    Returns:
        The strongest, or ``"UNKNOWN"`` when there are none.
    """
    strongest = "UNKNOWN"
    for tier in tiers:
        candidate = (tier or "UNKNOWN").strip().upper()
        if candidate not in _TIER_SCORE:
            continue
        if _TIER_SCORE[candidate] > _TIER_SCORE[strongest]:
            strongest = candidate
    return strongest


def combine(source_types: Sequence[str], inferred: bool = False) -> Tuple[str, int]:
    """Work out what a fact backed by these sources may claim.

    Corroboration raises the score a little but never the tier: two directories
    are still two directories, and the company's own page outranks both however
    many of them agree. The bump exists only so that, between two facts of the
    same tier, the better-attested one sorts first.

    Args:
        source_types: The source types asserting the value.
        inferred: Whether this code derived the value rather than read it. An
            inferred value is capped at :data:`MAX_INFERRED_TIER`.

    Returns:
        ``(tier, score)``.
    """
    tiers = [tier_for(source) for source in source_types]
    tier = best(tiers)

    if inferred and _TIER_SCORE[tier] > _TIER_SCORE[MAX_INFERRED_TIER]:
        tier = MAX_INFERRED_TIER

    score = score_for(tier)

    # A small bump for independent corroboration at the same tier, capped so it
    # can never carry a value into the tier above.
    corroboration = max(0, len({s for s in source_types if s}) - 1)
    ceiling = 24 if tier != "VERIFIED" else 0
    score += min(corroboration * 5, ceiling)

    return tier, score
