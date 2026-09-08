"""The operator's keyword list, read from the sheet on every run.

The requirement this exists to satisfy is a small one with large consequences:
adding ``Salesforce | CRM Platforms | TRUE`` to the ``IT_KEYWORDS`` tab must
change what next week's run recognises, **without a code change and without a
deployment**. Setting ``Enabled`` to ``FALSE`` must stop a term being used the
same way.

    >>> keywords = load_keywords(client)
    >>> keywords.category_of("sap")
    'ERP Platforms'

So nothing here names a keyword. The seventy starter terms live in
:data:`sheets.init.DEFAULT_IT_KEYWORDS`, written into the tab once when it is
first created and never again — after that the sheet is the configuration and
this module only reads it.

**Cached for one run, never across runs.** A :class:`KeywordSet` is an
immutable snapshot: a run loads it once, hands it to the classifier, and every
posting that run classifies is judged against the same list. The next run calls
:func:`load_keywords` again and gets whatever the operator has since edited.
There is deliberately no process-level cache — that is the one thing that would
break the promise above.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Final, Iterable, Iterator, List, Optional, Tuple

from loguru import logger

__all__ = [
    "DEFAULT_MATCH_TYPE",
    "Keyword",
    "KeywordSet",
    "load_keywords",
]

#: How a term is matched when the operator does not say. Whole-word matching:
#: ``SAP`` should not fire on ``"sapphire"``, which substring matching would.
DEFAULT_MATCH_TYPE: Final[str] = "phrase"

#: Spellings of "no" a person actually types. Everything else — including a
#: blank cell — counts as enabled, so a keyword typed in a hurry works.
_DISABLED: Final[frozenset] = frozenset({
    "false", "no", "n", "0", "off", "disabled", "inactive",
})


@dataclass(frozen=True)
class Keyword:
    """One term the operator wants recognised.

    Attributes:
        keyword: The term, exactly as written in the sheet.
        category: What it belongs to, e.g. ``"ERP Platforms"``. Reported
            alongside a match so a human can see *why* a posting was flagged.
        enabled: Whether this run should use it.
        match_type: ``"phrase"`` for whole words, ``"substring"`` to match
            inside a longer word.
        notes: The operator's own note. Never read by the crawler.
    """

    keyword: str
    category: str = ""
    enabled: bool = True
    match_type: str = DEFAULT_MATCH_TYPE
    notes: str = ""

    @property
    def folded(self) -> str:
        """The term lowercased, which is how matching compares it.

        Returns:
            The comparison form.
        """
        return self.keyword.strip().lower()


class KeywordSet:
    """One run's keyword configuration, frozen at the moment it was read.

    Args:
        keywords: The terms, in sheet order. Repeats collapse onto the first
            spelling, so an operator who types ``SAP`` twice gets one term
            rather than a silently doubled weight.
    """

    def __init__(self, keywords: Iterable[Keyword]) -> None:
        self._by_folded: Dict[str, Keyword] = {}
        for keyword in keywords:
            folded = keyword.folded
            if folded and folded not in self._by_folded:
                self._by_folded[folded] = keyword

    def __len__(self) -> int:
        """How many terms are active."""
        return len(self._by_folded)

    def __bool__(self) -> bool:
        """Whether any term is configured."""
        return bool(self._by_folded)

    def __iter__(self) -> Iterator[Keyword]:
        """Every term, in the order the sheet listed it."""
        return iter(self._by_folded.values())

    def all(self) -> List[Keyword]:
        """Every term.

        Returns:
            The terms.
        """
        return list(self._by_folded.values())

    def phrases(self) -> List[str]:
        """Every term, folded, for handing to a matcher.

        Returns:
            The comparison forms.
        """
        return list(self._by_folded)

    def get(self, term: str) -> Optional[Keyword]:
        """One term, by any casing.

        Args:
            term: The term to look up.

        Returns:
            It, or ``None``.
        """
        return self._by_folded.get(str(term or "").strip().lower())

    def category_of(self, term: str) -> str:
        """What a term belongs to.

        Args:
            term: The term.

        Returns:
            Its category, or ``""`` when the term is not configured.
        """
        found = self.get(term)
        return found.category if found else ""

    def categories(self) -> List[str]:
        """Every distinct category, sorted.

        Returns:
            The category names.
        """
        return sorted({k.category for k in self._by_folded.values() if k.category})

    def fingerprint(self) -> str:
        """A short digest of this configuration.

        Recorded against a run so a verdict can be traced back to the keyword
        list that produced it — which matters once the list changes weekly and
        somebody asks why a posting was classified differently last month.

        Returns:
            A twelve-character hex digest.
        """
        material = "|".join(
            f"{keyword.folded}:{keyword.category}:{keyword.match_type}"
            for keyword in self._by_folded.values()
        )
        return hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]


def _enabled_from(value: object) -> bool:
    """Read an ``Enabled`` cell the way a person meant it.

    Args:
        value: Whatever the cell held.

    Returns:
        ``False`` only for a recognised negative. A blank means enabled.
    """
    return str(value or "").strip().lower() not in _DISABLED


def load_keywords(client: Any, only_enabled: bool = True) -> KeywordSet:
    """Read the current ``IT_KEYWORDS`` configuration.

    Called once at the start of a run. Never cached beyond it.

    Args:
        client: A :class:`sheets.client.SheetsClient`.
        only_enabled: Drop rows whose ``Enabled`` is a recognised negative.
            ``False`` returns everything, for a report that wants to show what
            is switched off.

    Returns:
        The configuration. Empty when the tab is absent or holds nothing —
        a spreadsheet nobody has set up yet must not stop a crawl, it simply
        means no keyword-driven classification happens.
    """
    from sheets.schema import IT_KEYWORDS
    from sheets.storage import TabStore

    try:
        records = TabStore(client, IT_KEYWORDS).read()
    except Exception as exc:  # noqa: BLE001 - an unreadable tab must not end a run
        # Loud, not silent. This used to be a debug line, from a time when
        # nothing in production called this function -- so an unreadable tab
        # cost nothing. It now decides how every posting of the run is
        # classified, and a run that quietly classified two hundred thousand
        # postings against an empty keyword list would look exactly like a
        # successful one. The crawl still proceeds on the built-in tables,
        # because losing the crawl is worse than losing the operator's terms,
        # but the operator is told which happened.
        logger.error(
            "IT_KEYWORDS could not be read ({}): {}. No operator keywords are in "
            "effect this run -- classification falls back to the built-in tables "
            "in crawler.tech_filter alone.",
            type(exc).__name__,
            exc,
        )
        return KeywordSet([])

    keywords: List[Keyword] = []
    disabled = 0

    for record in records:
        term = str(record.get("keyword") or "").strip()
        if not term:
            continue

        enabled = _enabled_from(record.get("enabled"))
        if only_enabled and not enabled:
            disabled += 1
            continue

        keywords.append(
            Keyword(
                keyword=term,
                category=str(record.get("category") or "").strip(),
                enabled=enabled,
                match_type=(str(record.get("match_type") or "").strip().lower()
                            or DEFAULT_MATCH_TYPE),
                notes=str(record.get("notes") or "").strip(),
            )
        )

    found = KeywordSet(keywords)

    if not found:
        # An empty tab and an unreadable one are different faults with the same
        # consequence, and both need saying out loud for the same reason.
        logger.warning(
            "IT_KEYWORDS holds no enabled term{}. No operator keywords are in "
            "effect this run -- classification falls back to the built-in tables "
            "in crawler.tech_filter alone. Add terms to the IT_KEYWORDS tab, or "
            "run: python -m sheets.init --seed-keywords",
            f" ({disabled} are switched off)" if disabled else "",
        )
        return found

    logger.info(
        "IT_KEYWORDS: {} term(s) active across {} category(ies){}",
        len(found),
        len(found.categories()),
        f", {disabled} disabled" if disabled else "",
    )
    return found
