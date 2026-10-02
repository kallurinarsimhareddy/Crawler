"""NEW / CHANGED / UNCHANGED / REOPENED — comparing an observation with the master record.

CLOSED is not decided here: it needs the absence of a job across completed full
sweeps or direct gone evidence (see :mod:`.runner`), never a single observation.
A CLOSED or EXPIRED job that shows up again is REOPENED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from cloud.intel.job_monitor.schema import HASH_COLUMNS, clean_text

__all__ = ["Change", "classify", "NEW", "CHANGED", "UNCHANGED", "REOPENED"]

NEW, CHANGED, UNCHANGED, REOPENED = "new", "changed", "unchanged", "reopened"


@dataclass
class Change:
    kind: str
    changed_fields: List[str] = field(default_factory=list)
    before: Dict[str, Any] = field(default_factory=dict)
    after: Dict[str, Any] = field(default_factory=dict)


def _same(a: Any, b: Any) -> bool:
    return (clean_text(a, 2000) or "").casefold() == (clean_text(b, 2000) or "").casefold()


def classify(existing: Optional[Mapping[str, Any]], values: Mapping[str, Any]) -> Change:
    """``existing`` is the stored job_postings row (or None); ``values`` the normalised
    observation. A closed job that shows up again is REOPENED (with its field changes);
    a stored row without a hash (older sources) is compared field by field."""
    if existing is None:
        return Change(NEW)
    changed = [c for c in HASH_COLUMNS if not _same(existing.get(c), values.get(c))]
    if existing.get("content_hash") and existing.get("content_hash") == values.get("content_hash"):
        changed = []
    before = {c: existing.get(c) for c in changed}
    after = {c: values.get(c) for c in changed}
    if existing.get("status") in ("closed", "expired"):
        return Change(REOPENED, changed, before, after)
    return Change(CHANGED if changed else UNCHANGED, changed, before, after)
