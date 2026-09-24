"""The agent's working set: what a conversation is currently looking at.

A Control Room session keeps one working set — ordered company ids plus the
contacts, jobs, signals and opportunities attached to them, and the evidence for
every conclusion. Each tool reads it and narrows or enriches it, which is what
makes follow-ups work ("remove companies already in our CRM" acts on the 417
companies the previous request found).

It is the research agent's :class:`~cloud.intel.research.tools.ResearchState`
plus the other entity kinds, so the research tools are reused as-is. It is
persisted (capped) on the run and the session after every step, so a crashed
worker resumes from the last completed step instead of starting over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cloud.intel.core.context import Ctx
from cloud.intel.research.tools import ResearchState

__all__ = ["WorkingSet", "MAX_IDS"]

#: Upper bound on ids kept per entity kind between steps (large research jobs page within a step).
MAX_IDS = 5000
_KINDS = ("contacts", "jobs", "signals", "opportunities", "lists")


@dataclass
class WorkingSet:
    research: ResearchState
    #: entity kind -> ordered ids (companies live in research.order)
    ids: Dict[str, List[str]] = field(default_factory=lambda: {k: [] for k in _KINDS})
    #: facts gathered along the way (counts, removed ids, file references)
    facts: Dict[str, Any] = field(default_factory=dict)

    @property
    def company_ids(self) -> List[str]:
        return self.research.order

    def set_ids(self, kind: str, ids: List[str]) -> None:
        seen, out = set(), []
        for i in ids:
            if i and i not in seen:
                seen.add(i)
                out.append(i)
        self.ids[kind] = out[:MAX_IDS]

    def load_companies(self, ids: List[str]) -> None:
        """Make ``ids`` the company set, loading rows the state does not have yet."""
        store, ctx = self.research.platform.store, self.research.ctx
        order = []
        for cid in ids[:MAX_IDS]:
            if cid not in self.research.companies:
                row = store.find(ctx, "companies", cid)
                if row is None:
                    continue
                self.research.companies[cid] = row
            order.append(cid)
        self.research.order = order

    # --- persistence ---------------------------------------------------------

    def dump(self) -> Dict[str, Any]:
        order = self.research.order[:MAX_IDS]
        keep = set(order)
        extras = {}
        for cid in order:
            extra = self.research.extras.get(cid) or {}
            extras[cid] = {k: extra[k] for k in ("scores", "contact_gap", "campaign", "email_validation",
                                                  "intent_score") if k in extra}
            if extra.get("signals"):
                extras[cid]["signal_ids"] = [s["id"] for s in extra["signals"]][:20]
            if extra.get("jobs"):
                extras[cid]["job_ids"] = [j["id"] for j in extra["jobs"]][:20]
        return {
            "companies": order,
            "evidence": {cid: ev[:20] for cid, ev in self.research.evidence.items() if cid in keep},
            "extras": extras,
            "ids": {k: v[:MAX_IDS] for k, v in self.ids.items()},
            "facts": self.facts,
        }

    @classmethod
    def restore(cls, platform: Any, ctx: Ctx, data: Optional[Dict[str, Any]], *, allow_paid: bool = False
                ) -> "WorkingSet":
        state = cls(ResearchState(platform, ctx, allow_paid=allow_paid))
        if not data:
            return state
        state.load_companies(list(data.get("companies") or []))
        store = platform.store
        for cid, ev in (data.get("evidence") or {}).items():
            if cid in state.research.companies:
                state.research.evidence[cid] = list(ev)
        for cid, extra in (data.get("extras") or {}).items():
            if cid not in state.research.companies:
                continue
            target = state.research.extra(cid)
            target.update({k: v for k, v in extra.items() if k not in ("signal_ids", "job_ids")})
            if extra.get("signal_ids"):
                target["signals"] = [s for s in (store.find(ctx, "hiring_signals", i) for i in extra["signal_ids"]) if s]
            if extra.get("job_ids"):
                target["jobs"] = [j for j in (store.find(ctx, "job_postings", i) for i in extra["job_ids"]) if j]
        for kind, ids in (data.get("ids") or {}).items():
            if kind in state.ids:
                state.ids[kind] = list(ids)
        state.facts = dict(data.get("facts") or {})
        return state

    def counts(self) -> Dict[str, int]:
        return {"companies": len(self.research.order), **{k: len(v) for k, v in self.ids.items()}}
