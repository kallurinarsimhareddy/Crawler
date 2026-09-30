"""Company discovery: turn raw candidates into reviewed, evidenced companies.

::

    sources ─► candidates ─► identity resolution ─► duplicate detection ─► website verification
           ─► careers page discovery ─► ATS detection ─► industry/category ─► location
           ─► confidence ─► NEW_COMPANY_DISCOVERY | DUPLICATE | NEEDS_REVIEW ─► approve ─► CRM

:meth:`DiscoveryService.submit_candidates` runs the offline steps (normalise,
resolve against the company master, duplicates within the batch and against
open candidates) and stores every candidate with its evidence. The network
steps (website verification, careers page, ATS, JSON-LD facts) run in the
worker via ``run_discovery_task`` using :class:`~cloud.intel.core.http.SafeFetcher`
— SSRF-guarded, robots-respecting, no stealth. Nothing reaches the company
master until a person approves the candidate.

Every step appends to ``evidence``: ``{"step", "result", "detail", "at"}`` — so a
reviewer sees *why* a candidate is new, a duplicate or doubtful.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
from urllib.parse import urljoin, urlsplit

from cloud.intel.core.audit import audit, provenance
from cloud.intel.core.context import ConflictError, Ctx, NotFoundError, ValidationError, utcnow
from cloud.intel.core.normalize import (blank, company_name_key, domain_of, normalize_country, normalize_name,
                                        normalize_website)
from cloud.intel.vendor import ats_detect

__all__ = ["CandidateSource", "DiscoveryService", "JobPostingSource", "RowsSource", "UrlListSource",
           "run_discovery_task", "score_candidate"]

log = logging.getLogger(__name__)

_CAREERS_LINK = re.compile(r"(careers?|jobs?|join[- ]us|work[- ]with[- ]us|opportunities|employment|openings|"
                           r"vacancies|hiring)", re.I)
_OPEN = ("NEW_COMPANY_DISCOVERY", "NEEDS_REVIEW")


# --- candidate sources --------------------------------------------------------------


class CandidateSource(ABC):
    """Where candidates come from. ``candidates`` returns dicts with at least a ``name``."""

    kind: str = "api"
    name: str = "source"

    @abstractmethod
    def candidates(self, ctx: Ctx, limit: int = 500) -> List[Dict[str, Any]]: ...


class RowsSource(CandidateSource):
    """Uploaded or pasted rows (e.g. from an import batch)."""

    kind = "import"

    def __init__(self, rows: Iterable[Mapping[str, Any]], name: str = "upload") -> None:
        self.rows, self.name = [dict(r) for r in rows], name

    def candidates(self, ctx: Ctx, limit: int = 500) -> List[Dict[str, Any]]:
        return self.rows[:limit]


class UrlListSource(CandidateSource):
    """A list of company websites found on the web; names are filled in by verification."""

    kind = "public_web"

    def __init__(self, urls: Iterable[str], name: str = "web") -> None:
        self.urls, self.name = [u for u in urls if u], name

    def candidates(self, ctx: Ctx, limit: int = 500) -> List[Dict[str, Any]]:
        return [{"name": domain_of(u) or u, "website": u} for u in self.urls[:limit]]


class JobPostingSource(CandidateSource):
    """Employers seen in job postings that are not linked to any company yet."""

    kind = "crawler"
    name = "job_postings"

    def __init__(self, store: Any) -> None:
        self.store = store

    def candidates(self, ctx: Ctx, limit: int = 500) -> List[Dict[str, Any]]:
        seen, out = set(), []
        for job in self.store.all(ctx, "job_postings", {"company_id__isnull": True}, cap=20000):
            key = (job.get("domain") or "", (job.get("company_name") or "").lower())
            if key in seen or job.get("company_name") in (None, "(unknown company)"):
                continue
            seen.add(key)
            out.append({"name": job["company_name"], "domain": job.get("domain"), "ats": job.get("ats"),
                        "careers_url": None, "source_ref": job["job_url"]})
            if len(out) >= limit:
                break
        return out


class ZoomInfoCompanySource(CandidateSource):
    """Companies from the workspace's ZoomInfo connection (credit-free company search). Results enter the
    discovery review queue like any other candidate; nothing is written to the CRM until approved."""

    kind = "zoominfo"
    name = "zoominfo"

    def __init__(self, connector: Any, filters: Mapping[str, Any]) -> None:
        self.connector, self.filters = connector, dict(filters or {})

    def candidates(self, ctx: Ctx, limit: int = 500) -> List[Dict[str, Any]]:
        return [{"name": r.get("name"), "website": r.get("website"), "city": r.get("city"), "state": r.get("state"),
                 "country": r.get("country"), "source_ref": f"zoominfo:{r['zoominfo_id']}" if r.get("zoominfo_id")
                 else None}
                for r in self.connector.search_companies(self.filters, limit=limit) if r.get("name")]


# --- scoring ------------------------------------------------------------------------


def score_candidate(steps: Mapping[str, Any]) -> float:
    """Confidence that a candidate is a real, correctly identified company (0-1).

    base 0.30; +0.25 website verified; +0.15 site names the same company (JSON-LD
    or title); +0.15 careers page found; +0.10 ATS detected; +0.05 industry known;
    −0.30 when the website redirects to a different registrable domain.
    """
    score = 0.30
    if steps.get("website_verified"):
        score += 0.25
    if steps.get("name_confirmed"):
        score += 0.15
    if steps.get("careers_url"):
        score += 0.15
    if steps.get("ats"):
        score += 0.10
    if steps.get("industry"):
        score += 0.05
    if steps.get("domain_mismatch"):
        score -= 0.30
    return round(max(0.0, min(1.0, score)), 2)


def _ev(step: str, result: str, detail: Any = None) -> Dict[str, Any]:
    return {"step": step, "result": result, "detail": detail, "at": utcnow().isoformat()}


class DiscoveryService:
    def __init__(self, platform: Any, *, fetcher_factory: Optional[Callable[[], Any]] = None) -> None:
        self.platform = platform
        self.store = platform.store
        self._fetcher_factory = fetcher_factory

    def fetcher(self):
        if self._fetcher_factory is not None:
            return self._fetcher_factory()
        from cloud.intel.core.http import SafeFetcher

        return SafeFetcher(per_host_delay=1.0, max_bytes=2_000_000)

    # --- identity -----------------------------------------------------------------

    def _resolve(self, ctx: Ctx, candidate: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            result = self.platform.service("dedupe").resolve(ctx, dict(candidate))
            return {"outcome": result.get("outcome", "NONE"), "company_id": result.get("company_id"),
                    "reasons": list(result.get("reasons") or []), "via": "company_resolver"}
        except Exception:  # noqa: BLE001 - the resolver belongs to another track; fall back to exact keys
            log.debug("company resolver unavailable; exact matching", exc_info=True)
        if candidate.get("domain"):
            row = self.store.first(ctx, "companies", {"domain": candidate["domain"], "status__ne": "merged"})
            if row:
                return {"outcome": "STRONG", "company_id": row["id"],
                        "reasons": [f"same website domain ({candidate['domain']})"], "via": "exact_domain"}
        if candidate.get("normalized_name"):
            rows = self.store.list(ctx, "companies", {"normalized_name": candidate["normalized_name"]}, limit=3).rows
            if len(rows) == 1:
                return {"outcome": "PROBABLE", "company_id": rows[0]["id"],
                        "reasons": [f"same company name ({candidate['normalized_name']}); name alone is not identity"],
                        "via": "exact_name"}
            if len(rows) > 1:
                return {"outcome": "AMBIGUOUS", "company_id": None, "reasons": ["several companies share this name"],
                        "via": "exact_name"}
        return {"outcome": "NONE", "company_id": None, "reasons": [], "via": "none"}

    # --- submit --------------------------------------------------------------------

    def submit_candidates(self, ctx: Ctx, candidates: List[Dict[str, Any]], *, source_kind: str,
                          source_name: str) -> List[Dict[str, Any]]:
        ctx.require_write()
        rows: List[Dict[str, Any]] = []
        batch_domains: Dict[str, str] = {}
        batch_names: Dict[str, str] = {}
        for raw in candidates:
            name = str(raw.get("name") or raw.get("company_name") or "").strip()
            website = normalize_website(raw.get("website") or raw.get("domain"))
            domain = domain_of(raw.get("domain") or website)
            if not name and not domain:
                continue
            name = name or domain
            norm = {"name": name[:300], "domain": domain, "website": website, "normalized_name": normalize_name(name),
                    "name_key": company_name_key(name)}
            evidence = [_ev("normalize", "ok", {"domain": domain, "normalized_name": norm["normalized_name"]})]
            status = "NEW_COMPANY_DISCOVERY"
            matched_id, strength = None, "none"

            resolution = self._resolve(ctx, norm)
            evidence.append(_ev("identity_resolution", resolution["outcome"], resolution))
            if resolution["outcome"] in ("EXACT", "STRONG") and resolution["company_id"]:
                status, matched_id, strength = "DUPLICATE", resolution["company_id"], "strong"
            elif resolution["outcome"] in ("PROBABLE", "AMBIGUOUS"):
                status, matched_id = "NEEDS_REVIEW", resolution["company_id"]
                strength = "probable" if resolution["outcome"] == "PROBABLE" else "ambiguous"

            dup_of = (domain and batch_domains.get(domain)) or (
                not domain and norm["name_key"] and batch_names.get(norm["name_key"]))
            if status == "NEW_COMPANY_DISCOVERY" and dup_of:
                status = "DUPLICATE"
                evidence.append(_ev("duplicate_detection", "duplicate_in_batch", {"candidate_id": dup_of}))
            elif status == "NEW_COMPANY_DISCOVERY" and domain:
                other = self.store.first(ctx, "discovery_candidates", {"domain": domain, "status__in": list(_OPEN)})
                if other:
                    status = "DUPLICATE"
                    evidence.append(_ev("duplicate_detection", "duplicate_of_open_candidate",
                                        {"candidate_id": other["id"]}))
                else:
                    evidence.append(_ev("duplicate_detection", "none"))

            values = {
                "name": norm["name"], "domain": domain, "website": website,
                "source_kind": source_kind, "source_name": source_name[:200], "status": status,
                "matched_company_id": matched_id, "match_strength": strength,
                "careers_url": normalize_website(raw.get("careers_url")) if raw.get("careers_url") else None,
                "ats": raw.get("ats") or None, "industry": raw.get("industry") or None,
                "category": raw.get("category") or None, "country": normalize_country(raw.get("country")),
                "state": raw.get("state") or None, "city": raw.get("city") or None,
                "evidence": evidence,
                "steps": {"verified": False, "source_ref": raw.get("source_ref"), "industry": raw.get("industry")},
            }
            values["confidence"] = score_candidate({"industry": values["industry"], "ats": values["ats"],
                                                    "careers_url": values["careers_url"]})
            row = self.store.insert(ctx, "discovery_candidates", values)
            if domain:
                batch_domains.setdefault(domain, row["id"])
            elif norm["name_key"]:
                batch_names.setdefault(norm["name_key"], row["id"])
            rows.append(row)
        audit(self.store, ctx, "discovery.submit", entity_type="discovery_candidates",
              summary=f"{len(rows)} candidate(s) from {source_name}")
        return rows

    def submit_from_source(self, ctx: Ctx, source: CandidateSource, *, limit: int = 500) -> List[Dict[str, Any]]:
        return self.submit_candidates(ctx, source.candidates(ctx, limit), source_kind=source.kind,
                                      source_name=source.name)

    # --- verification (network; worker only) --------------------------------------------

    def verify(self, ctx: Ctx, candidate_id: str, *, fetcher: Any = None) -> Dict[str, Any]:
        candidate = self.store.get(ctx, "discovery_candidates", candidate_id)
        if candidate["status"] not in _OPEN:
            return candidate
        fetcher = fetcher or self.fetcher()
        evidence = list(candidate.get("evidence") or [])
        steps = dict(candidate.get("steps") or {})
        changes: Dict[str, Any] = {}
        website = candidate.get("website")
        html = ""
        if not website:
            evidence.append(_ev("website_verification", "skipped", "no website"))
        else:
            result = fetcher.fetch(website)
            if result.ok:
                final_domain = domain_of(result.final_url)
                steps["website_verified"] = True
                changes["website_verified"] = True
                html = result.text or ""
                detail = {"status": result.status, "final_url": result.final_url}
                if candidate.get("domain") and final_domain and final_domain != candidate["domain"]:
                    steps["domain_mismatch"] = True
                    detail["redirected_to_domain"] = final_domain
                    evidence.append(_ev("website_verification", "redirects_elsewhere", detail))
                else:
                    evidence.append(_ev("website_verification", "verified", detail))
            else:
                changes["website_verified"] = False
                evidence.append(_ev("website_verification", "blocked" if result.blocked else "failed",
                                    {"status": result.status, "error": result.error}))
        if html:
            facts = self._page_facts(html, website)
            if facts.get("names"):
                wanted = normalize_name(candidate["name"])
                confirmed = any(normalize_name(n) == wanted or (company_name_key(n) and
                                company_name_key(n) == company_name_key(candidate["name"])) for n in facts["names"])
                steps["name_confirmed"] = confirmed
                evidence.append(_ev("name_check", "confirmed" if confirmed else "different", facts["names"][:3]))
                if candidate["name"] == candidate.get("domain") and facts["names"]:
                    changes["name"] = facts["names"][0][:300]  # a URL-only candidate learns its name
            for field in ("industry", "country", "state", "city"):
                if facts.get(field) and not candidate.get(field):
                    changes[field] = facts[field][:200]
                    steps[field] = facts[field]
            if facts.get("industry") or facts.get("country"):
                evidence.append(_ev("organization_facts", "found",
                                    {k: facts.get(k) for k in ("industry", "country", "state", "city")}))
            careers = candidate.get("careers_url") or facts.get("careers_url")
            if careers:
                changes["careers_url"] = careers
                steps["careers_url"] = careers
                evidence.append(_ev("careers_page", "found", careers))
            else:
                evidence.append(_ev("careers_page", "not_found"))
            ats = candidate.get("ats") or facts.get("ats")
            if not ats and careers:
                det = ats_detect.detect(careers)
                ats = det["platform"] if det else None
            if not ats and careers and careers != website:
                page = fetcher.fetch(careers)
                if page.ok:
                    found = ats_detect.find_ats_in_text(page.text or "")
                    if found:
                        ats = found[0]["platform"]
                        evidence.append(_ev("ats_detection", "found_on_careers_page", found[0]))
            if ats:
                changes["ats"] = ats
                steps["ats"] = ats
                evidence.append(_ev("ats_detection", "detected", ats))
            else:
                evidence.append(_ev("ats_detection", "not_detected"))
        steps["verified"] = True
        steps.setdefault("industry", candidate.get("industry"))
        confidence = score_candidate(steps)
        changes.update({"steps": steps, "evidence": evidence, "confidence": confidence})
        if candidate["status"] == "NEW_COMPANY_DISCOVERY" and (confidence < 0.5 or steps.get("domain_mismatch")):
            changes["status"] = "NEEDS_REVIEW"
            evidence.append(_ev("confidence", "needs_review", confidence))
        else:
            evidence.append(_ev("confidence", "scored", confidence))
        return self.store.update(ctx, "discovery_candidates", candidate_id, changes)

    @staticmethod
    def _page_facts(html: str, base_url: str) -> Dict[str, Any]:
        from cloud.intel.vendor.html import json_ld_objects, parse_html

        soup = parse_html(html)
        facts: Dict[str, Any] = {"names": []}
        try:
            objects = json_ld_objects(soup)
        except Exception:  # noqa: BLE001 - malformed JSON-LD is common
            objects = []
        for obj in objects:
            types = obj.get("@type")
            types = types if isinstance(types, list) else [types]
            if not any(t in ("Organization", "Corporation", "LocalBusiness", "Company") for t in types if t):
                continue
            for key in ("name", "legalName"):
                if isinstance(obj.get(key), str) and obj[key].strip():
                    facts["names"].append(obj[key].strip())
            industry = obj.get("industry") or obj.get("naics") or obj.get("knowsAbout")
            if isinstance(industry, str):
                facts["industry"] = industry
            address = obj.get("address")
            if isinstance(address, list) and address:
                address = address[0]
            if isinstance(address, dict):
                facts["city"] = address.get("addressLocality")
                facts["state"] = address.get("addressRegion")
                country = address.get("addressCountry")
                if isinstance(country, dict):
                    country = country.get("name")
                facts["country"] = normalize_country(country) if country else None
        og = soup.find("meta", attrs={"property": "og:site_name"})
        if og and og.get("content"):
            facts["names"].append(og["content"].strip())
        found = ats_detect.find_ats_in_text(html)
        if found:
            facts["ats"] = found[0]["platform"]
            facts["careers_url"] = found[0]["url"]
        for link in soup.find_all("a", href=True):
            text = f"{link.get_text(' ', strip=True)} {link['href']}"
            if _CAREERS_LINK.search(text):
                url = urljoin(base_url, link["href"])
                if url.startswith("http"):
                    facts.setdefault("careers_url", url)
                    break
        return facts

    # --- decisions ---------------------------------------------------------------------

    def approve(self, ctx: Ctx, candidate_id: str) -> Dict[str, Any]:
        ctx.require_write()
        candidate = self.store.get(ctx, "discovery_candidates", candidate_id)
        if candidate["status"] in ("APPROVED", "REJECTED"):
            raise ConflictError(f"candidate is already {candidate['status'].lower()}")
        values = {k: candidate.get(k) for k in ("name", "domain", "website", "careers_url", "ats", "industry",
                                                 "country", "state", "city") if candidate.get(k)}
        values["confidence"] = candidate.get("confidence")
        try:
            result = self.platform.service("crm").upsert_company(
                ctx, values, source_kind="discovery", source_name=candidate["source_name"],
                source_ref=(candidate.get("steps") or {}).get("source_ref"),
                original={"candidate_id": candidate_id, "evidence": candidate.get("evidence")},
                confidence=candidate.get("confidence"))
            company = result.get("company")
        except (ImportError, KeyError, AttributeError, ModuleNotFoundError):
            company = self._insert_company(ctx, candidate, values)
        if company is None:
            raise ConflictError("the company master needs a reviewer decision for this match first")
        row = self.store.update(ctx, "discovery_candidates", candidate_id, {
            "status": "APPROVED", "company_id": company["id"], "decided_by": ctx.user_id, "decided_at": utcnow()})
        audit(self.store, ctx, "discovery.approve", entity_type="discovery_candidates", entity_id=candidate_id,
              summary=f"{candidate['name']} → {company['id']}")
        from cloud.intel.technology.service import emit_best_effort

        emit_best_effort(self.platform, ctx, "new_company", company["id"],
                         {"company_id": company["id"], "source": "discovery", "candidate_id": candidate_id})
        return {"candidate": row, "company": company}

    def _insert_company(self, ctx: Ctx, candidate: Mapping[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
        """Fallback when the CRM service is not available: a plain insert with provenance."""
        values = {**values, "normalized_name": normalize_name(values["name"]), "first_seen_at": utcnow(),
                  "source_count": 1}
        company = self.store.insert(ctx, "companies", values)
        provenance(self.store, ctx, "companies", company["id"], source_kind="discovery",
                   source_name=candidate["source_name"], original={"candidate_id": candidate["id"]},
                   normalized=values, confidence=candidate.get("confidence"))
        return company

    def reject(self, ctx: Ctx, candidate_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        ctx.require_write()
        candidate = self.store.get(ctx, "discovery_candidates", candidate_id)
        if candidate["status"] in ("APPROVED", "REJECTED"):
            raise ConflictError(f"candidate is already {candidate['status'].lower()}")
        evidence = list(candidate.get("evidence") or []) + [_ev("decision", "rejected", reason)]
        row = self.store.update(ctx, "discovery_candidates", candidate_id, {
            "status": "REJECTED", "decided_by": ctx.user_id, "decided_at": utcnow(), "evidence": evidence})
        audit(self.store, ctx, "discovery.reject", entity_type="discovery_candidates", entity_id=candidate_id,
              summary=reason)
        return row


def run_discovery_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    """Params: ``candidate_ids`` (verify these), or ``source`` in {"job_postings", "urls", "zoominfo"} with
    ``urls`` / ``filters`` (ZoomInfo CompanySearch attributes, credit-free);
    with neither, verifies every open, unverified candidate."""
    service: DiscoveryService = platform.service("discovery")
    params = task.get("params") or {}
    submitted = []
    if params.get("source") == "job_postings":
        submitted = service.submit_from_source(ctx, JobPostingSource(platform.store), limit=int(params.get("limit", 500)))
    elif params.get("source") == "urls":
        submitted = service.submit_from_source(ctx, UrlListSource(params.get("urls") or []))
    elif params.get("source") == "zoominfo":
        registry = platform.service("providers")
        if not registry.configured(ctx, "zoominfo"):
            raise ValidationError("ZoomInfo is not connected for this workspace; add its credentials in Settings")
        source = ZoomInfoCompanySource(registry.enrichment(ctx, "zoominfo"), params.get("filters") or {})
        submitted = service.submit_from_source(ctx, source, limit=min(int(params.get("limit", 100)), 1000))
    ids = params.get("candidate_ids") or [c["id"] for c in submitted] or [
        c["id"] for c in platform.store.all(ctx, "discovery_candidates", {"status__in": list(_OPEN)}, cap=5000)
        if not (c.get("steps") or {}).get("verified")]
    fetcher = service.fetcher()
    totals = {"submitted": len(submitted), "verified": 0, "needs_review": 0, "errors": 0}
    for index, cid in enumerate(ids):
        if reporter is not None:
            if reporter.is_cancelled():
                break
            if reporter.should_pause():
                from cloud.intel.tasks.worker import TaskPaused

                raise TaskPaused({"index": index})
            reporter.progress(f"Verifying {index + 1}/{len(ids)}", done=index, total=len(ids))
        try:
            row = service.verify(ctx, cid, fetcher=fetcher)
            totals["verified"] += 1
            totals["needs_review"] += row["status"] == "NEEDS_REVIEW"
        except NotFoundError:
            continue
        except Exception:  # noqa: BLE001
            log.exception("verifying candidate %s failed", cid)
            totals["errors"] += 1
    return totals
