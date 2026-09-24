"""Technology intelligence: detect technologies in text and record them per
company with evidence (source, date, confidence, evidence URL).

Sources feed :meth:`TechnologyService.record`: job descriptions (the job
intelligence track), scraped pages, imports, and :class:`TechnologyProvider`
implementations such as the ZoomInfo connector (Track E/H). Every record keeps
its evidence; ``companies.technologies`` is only a denormalised summary of the
active ``company_technologies`` rows.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Mapping, Optional

from cloud.intel.core.audit import audit, provenance
from cloud.intel.core.context import ConflictError, Ctx, utcnow
from cloud.intel.technology.taxonomy import CATEGORIES, detect, taxonomy_as_dict

__all__ = ["TechnologyProvider", "TechnologyService", "emit_best_effort"]

log = logging.getLogger(__name__)

#: How much a source is trusted when it reports a technology.
SOURCE_CONFIDENCE = {
    "zoominfo": 0.85, "import": 0.7, "job_posting": 0.75, "crawler": 0.75, "scraper": 0.6,
    "public_web": 0.6, "manual": 0.9, "research": 0.6,
}


def emit_best_effort(platform: Any, ctx: Ctx, trigger: str, event_key: str, payload: Mapping[str, Any]) -> None:
    """Tell the automation engine something happened; never let it break the caller."""
    try:
        platform.service("automation").emit(ctx, trigger, event_key, dict(payload))
    except Exception:  # noqa: BLE001 - automation is optional and best-effort
        log.debug("automation emit %s skipped", trigger, exc_info=True)


class TechnologyProvider(ABC):
    """Anything that can say which technologies a company uses (e.g. ZoomInfo).

    ``lookup`` returns ``[{"technology", "category", "vendor", "evidence_url",
    "evidence_text", "confidence"}]``. Implementations that spend credits must
    go through the credit ledger and refuse without an explicit action.
    """

    name: str = "provider"

    @abstractmethod
    def lookup(self, ctx: Ctx, company: Mapping[str, Any]) -> List[Dict[str, Any]]: ...


class TechnologyService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    # --- detection ----------------------------------------------------------

    @staticmethod
    def detect_in_text(text: str) -> List[Dict[str, Any]]:
        return [{"technology": m.technology, "category": m.category, "families": list(m.families),
                 "vendor": m.vendor, "matched": m.matched, "evidence_text": m.evidence_text}
                for m in detect(text or "")]

    @staticmethod
    def taxonomy() -> Dict[str, Any]:
        return {"categories": list(CATEGORIES), "technologies": taxonomy_as_dict()}

    # --- recording ------------------------------------------------------------

    def record(self, ctx: Ctx, company_id: str, technology: str, *, source: str, category: Optional[str] = None,
               evidence_url: Optional[str] = None, evidence_text: Optional[str] = None,
               confidence: Optional[float] = None, families: Optional[List[str]] = None,
               vendor: Optional[str] = None) -> Dict[str, Any]:
        """Upsert one (company, technology, source) observation with its evidence."""
        company = self.store.get(ctx, "companies", company_id)
        now = utcnow()
        conf = confidence if confidence is not None else SOURCE_CONFIDENCE.get(source, 0.5)
        existing = self.store.first(ctx, "company_technologies",
                                    {"company_id": company_id, "technology": technology, "source": source})
        values = {"category": category, "vendor": vendor, "evidence_url": evidence_url,
                  "evidence_text": (evidence_text or "")[:2000] or None, "observed_at": now,
                  "confidence": max(0.0, min(1.0, conf)), "status": "active"}
        was_known = bool(self.store.first(ctx, "company_technologies",
                                          {"company_id": company_id, "technology": technology, "status": "active"}))
        if existing:
            row = self.store.update(ctx, "company_technologies", existing["id"], values)
        else:
            try:
                row = self.store.insert(ctx, "company_technologies",
                                        {"company_id": company_id, "technology": technology, "source": source, **values})
            except ConflictError:  # a concurrent twin inserted it
                existing = self.store.first(ctx, "company_technologies",
                                            {"company_id": company_id, "technology": technology, "source": source})
                row = self.store.update(ctx, "company_technologies", existing["id"], values)
            provenance(self.store, ctx, "company_technologies", row["id"], source_kind=_source_kind(source),
                       source_name=source, source_ref=evidence_url,
                       original={"technology": technology, "evidence_text": evidence_text},
                       normalized={"technology": technology, "category": category}, confidence=conf)
        self._refresh_company(ctx, company, families or ([category] if category else []), technology)
        if not was_known:
            try:
                self.platform.service("monitoring").record_change(
                    ctx, company_id, "technology_added", after={"technology": technology, "source": source},
                    summary=f"{technology} detected ({source})", source=source)
            except Exception:  # noqa: BLE001
                log.debug("change event skipped", exc_info=True)
            emit_best_effort(self.platform, ctx, "technology_detected", f"{company_id}:{technology}",
                             {"company_id": company_id, "technology": technology, "category": category,
                              "source": source, "evidence_url": evidence_url})
            audit(self.store, ctx, "technology.detect", entity_type="companies", entity_id=company_id,
                  summary=f"{technology} via {source}")
        return row

    def record_from_text(self, ctx: Ctx, company_id: str, text: str, *, source: str,
                         evidence_url: Optional[str] = None, confidence: Optional[float] = None) -> List[Dict[str, Any]]:
        rows = []
        for m in self.detect_in_text(text):
            rows.append(self.record(ctx, company_id, m["technology"], source=source, category=m["category"],
                                    families=m["families"], vendor=m["vendor"], evidence_url=evidence_url,
                                    evidence_text=m["evidence_text"], confidence=confidence))
        return rows

    def remove(self, ctx: Ctx, company_id: str, technology: str, *, source: str) -> None:
        for row in self.store.all(ctx, "company_technologies", {"company_id": company_id, "technology": technology,
                                                                 "source": source, "status": "active"}):
            self.store.update(ctx, "company_technologies", row["id"], {"status": "removed"})
        if not self.store.first(ctx, "company_technologies",
                                {"company_id": company_id, "technology": technology, "status": "active"}):
            company = self.store.get(ctx, "companies", company_id)
            self.store.update(ctx, "companies", company_id,
                              {"technologies": [t for t in company["technologies"] if t != technology]})
            try:
                self.platform.service("monitoring").record_change(
                    ctx, company_id, "technology_removed", before={"technology": technology},
                    summary=f"{technology} no longer observed ({source})", source=source)
            except Exception:  # noqa: BLE001
                log.debug("change event skipped", exc_info=True)

    def run_provider(self, ctx: Ctx, provider: TechnologyProvider, company_id: str) -> List[Dict[str, Any]]:
        company = self.store.get(ctx, "companies", company_id)
        return [self.record(ctx, company_id, item["technology"], source=provider.name,
                            category=item.get("category"), vendor=item.get("vendor"),
                            evidence_url=item.get("evidence_url"), evidence_text=item.get("evidence_text"),
                            confidence=item.get("confidence"))
                for item in provider.lookup(ctx, company)]

    def for_company(self, ctx: Ctx, company_id: str) -> List[Dict[str, Any]]:
        return self.store.all(ctx, "company_technologies", {"company_id": company_id, "status": "active"},
                              order="technology")

    def _refresh_company(self, ctx: Ctx, company: Dict[str, Any], families: List[str], technology: str) -> None:
        techs = list(company.get("technologies") or [])
        if technology not in techs:
            techs.append(technology)
            self.store.update(ctx, "companies", company["id"], {"technologies": techs})


def _source_kind(source: str) -> str:
    return {"job_posting": "crawler", "crawler": "crawler", "zoominfo": "zoominfo", "import": "import",
            "scraper": "scraper", "public_web": "public_web", "manual": "manual", "research": "research",
            "seamless": "seamless"}.get(source, "system")
