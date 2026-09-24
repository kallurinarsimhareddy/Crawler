"""Company identity resolution: is this incoming record a company we already hold?

Two steps, deliberately separate:

1. **Candidate generation** (cheap, generous): existing companies that share the
   registrable domain, the normalised name (legal form kept), a legal-form-
   stripped name *prefix*, or an alias. Merged companies are excluded — their
   survivor is found instead.
2. **Decision** (strict): the vendored discovery matcher
   (:func:`cloud.intel.vendor.identity.match`) compares each candidate signal by
   signal. A name alone never merges ("Acme Inc" vs "Acme LLC" is AMBIGUOUS);
   a shared domain is strong; conflicting domains always block an automatic merge.

The best outcome wins. If two *different* companies are both mergeable, the
answer is AMBIGUOUS and a person decides.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from cloud.intel.core.context import Ctx
from cloud.intel.core.normalize import company_name_key, domain_of, normalize_name
from cloud.intel.vendor import identity

__all__ = ["CompanyResolver", "MERGEABLE"]

MERGEABLE = frozenset({identity.EXACT, identity.STRONG})
_RANK = {outcome: i for i, outcome in enumerate(identity.OUTCOMES)}
_MAX_CANDIDATES = 25


def _evidence(record: Mapping[str, Any]) -> identity.CandidateEvidence:
    website = record.get("domain") or record.get("website") or ""
    return identity.evidence_from(
        name=str(record.get("name") or ""),
        website=str(website or ""),
        email=str(record.get("email") or ""),
        linkedin=str(record.get("linkedin_url") or ""),
        city=str(record.get("city") or ""),
        region=str(record.get("state") or ""),
        country=str(record.get("country") or ""),
    )


class CompanyResolver:
    def __init__(self, platform: Any) -> None:
        self.platform = platform

    @property
    def store(self):
        return self.platform.store

    def candidates(self, ctx: Ctx, candidate: Mapping[str, Any]) -> List[Dict[str, Any]]:
        found: Dict[str, Dict[str, Any]] = {}

        def add(filters: Dict[str, Any]) -> None:
            if len(found) >= _MAX_CANDIDATES:
                return
            filters = {**filters, "status__ne": "merged"}
            for row in self.store.list(ctx, "companies", filters, limit=_MAX_CANDIDATES).rows:
                found.setdefault(row["id"], row)

        domain = domain_of(candidate.get("domain") or candidate.get("website") or "")
        if domain:
            add({"domain": domain})
        name = str(candidate.get("name") or "").strip()
        full = normalize_name(name)
        if full:
            add({"normalized_name": full})
        slug = company_name_key(name)
        if slug and len(slug) >= 3:
            add({"normalized_name__ilike": slug})
        if name:
            add({"aliases": name})
        for alias in candidate.get("aliases") or []:
            add({"aliases": alias})
        return list(found.values())

    def resolve(self, ctx: Ctx, candidate: Mapping[str, Any]) -> Dict[str, Any]:
        """``{"outcome", "company_id", "reasons", "candidates"}`` (see CONTRACTS.md)."""
        incoming = _evidence(candidate)
        if not incoming.has_identity:
            return {"outcome": identity.NONE, "company_id": None, "reasons": ["no identifying signal"],
                    "candidates": []}
        scored = []
        for row in self.candidates(ctx, candidate):
            result = identity.match(incoming, _evidence(row))
            # aliases: a recorded alias that equals the incoming name is as good as the name
            if result.outcome not in MERGEABLE and candidate.get("name") in (row.get("aliases") or []):
                alias_result = identity.match(incoming, _evidence({**row, "name": candidate.get("name")}))
                if _RANK[alias_result.outcome] < _RANK[result.outcome]:
                    result = alias_result
            # The matcher answers AMBIGUOUS whenever a hard signal conflicts. With *nothing*
            # agreeing (different domain, different name) that is simply another company.
            if (result.outcome == identity.AMBIGUOUS and not result.matched_signals
                    and not result.near_signals):
                result = identity.MatchResult(outcome=identity.NONE, reasons=list(result.reasons))
            scored.append((row, result))
        if not scored:
            return {"outcome": identity.NONE, "company_id": None, "reasons": ["no existing company shares a "
                                                                             "domain, name or alias"],
                    "candidates": []}
        scored.sort(key=lambda pair: _RANK[pair[1].outcome])
        best_row, best = scored[0]
        mergeable = [row for row, result in scored if result.outcome in MERGEABLE]
        ids = [row["id"] for row, _ in scored]
        if len({row["id"] for row in mergeable}) > 1:
            return {"outcome": identity.AMBIGUOUS, "company_id": None,
                    "reasons": [f"{len(mergeable)} existing companies match equally well; a person must choose"],
                    "candidates": [row["id"] for row in mergeable]}
        return {"outcome": best.outcome, "company_id": best_row["id"] if best.outcome != identity.NONE else None,
                "reasons": list(best.reasons), "candidates": ids}
