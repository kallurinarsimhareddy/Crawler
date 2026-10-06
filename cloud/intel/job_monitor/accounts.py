"""Account Builder: hiring companies (from job postings and hiring signals) -> CRM accounts -> lists.

A *candidate* is one normalised company identity among the jobs the Jobs page filters
select (``job_postings.company_name`` normalised with the CRM's own ``normalize_name``):
its open / HIGH / REVIEW job counts, the technologies its jobs match, its job-derived
signals and its CRM state:

==============  =====================================================================
LINKED          its jobs already point at a CRM company
ALREADY IN CRM  exactly one CRM company has this normalised name — "Create" links to it
MATCHED         the CRM dedupe resolver finds a safe (exact / strong) match by other
                evidence (alias, domain) — "Create" links to it
REVIEW          ambiguous: several candidates, a near match, or a pending review row —
                nothing happens without an explicit Link / Create-new decision
NEW ACCOUNT     no CRM company resembles it
==============  =====================================================================

Creating goes through ``CrmService.upsert_company`` (the CRM's dedupe and provenance:
a ``source_records`` row with source_kind ``discovery`` / "Account Builder" and the
originating job names, signal ids and sample job ids). A safe match links instead of
creating; an ambiguous one is queued for review unless the user explicitly overrides.
Linking attaches the company's jobs (``job_postings.company_id``) and job-derived signals
(``hiring_signals.company_id``), resolves its review row, refreshes ``hiring_count`` and
the company's signals/scores with the existing engines. Every step is idempotent.

Bounded by design: at most :data:`MAX_PER_ACTION` companies per action, an explicit
``confirm`` above :data:`CONFIRM_ABOVE`, a background task above :data:`SYNC_LIMIT`, and a
database-capacity guard (``CAREERCLOUD_DB_CAPACITY_MB``, default 500) before large work.
Companies are never created automatically.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import Counter
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, NotFoundError, ValidationError, utcnow
from cloud.intel.core.normalize import company_name_key, normalize_name

__all__ = ["AccountBuilderService", "run_account_build_task", "MAX_PER_ACTION", "CONFIRM_ABOVE", "SYNC_LIMIT",
           "STATES"]

log = logging.getLogger(__name__)

MAX_PER_ACTION = 500
CONFIRM_ABOVE = 50
#: Larger selections run as a background task: a large employer takes several seconds to link and
#: rescore, and a long request would hit the proxy timeout (Cloudflare ~100 s).
SYNC_LIMIT = 5
#: Refuse large background work when the database is above this share of its capacity.
CAPACITY_SHARE = 0.9
#: Rough growth per created account (company row + provenance + audit + list member), bytes.
BYTES_PER_ACCOUNT = 6 * 1024
ACTIVE = ("open", "stale")
STATES = ("NEW ACCOUNT", "ALREADY IN CRM", "MATCHED", "REVIEW", "LINKED")
SOURCE_NAME = "Account Builder"
_LINK_BATCH = 500
#: Seconds a computed candidate grouping is reused for paging / sorting (cleared by any build).
CACHE_SECONDS = 60
_REVIEW_REASONS = ("several CRM companies share this name", "possible duplicate")
#: The job monitor's reason for a name with no CRM company at all (nothing to choose between).
UNMATCHED_REASON = "no CRM company has this name"
NEAR_REASON = "same name as a CRM company apart from legal form or punctuation (Inc, Ltd, LLC…)"


def _key(name: Any) -> Optional[str]:
    return normalize_name(name) if name else None


def _reason(**fields: Any) -> str:
    """List-member provenance in ``added_reason`` (500 chars): compact JSON, ids trimmed to fit."""
    payload = {"source": "account_builder", **{k: v for k, v in fields.items() if v}}
    for limit in (3, 2, 1, 0):
        trimmed = {k: (v[:limit] if isinstance(v, list) else v) for k, v in payload.items()}
        text = json.dumps(trimmed, separators=(",", ":"), default=str)
        if len(text) <= 500:
            return text
    return json.dumps({"source": "account_builder"})


class AccountBuilderService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store
        self._cache: Dict[Tuple[str, str], Tuple[float, List[Dict[str, Any]], Tuple[Any, ...]]] = {}
        self._local = threading.local()          # "building" flag: the build links itself (no re-match)

    @property
    def jobs(self):
        return self.platform.service("job_monitors")

    # ------------------------------------------------------------------------------------
    # candidates
    # ------------------------------------------------------------------------------------

    def _filters(self, ctx: Ctx, params: Mapping[str, Any]) -> Dict[str, Any]:
        from cloud.intel.job_monitor.exports import clean_params

        return self.jobs.query_filters(ctx, clean_params(params))

    @staticmethod
    def _with(filters: Mapping[str, Any], extra: Mapping[str, Any]) -> Dict[str, Any]:
        return {"all_of": [dict(filters), dict(extra)]} if filters else dict(extra)

    def _crm_index(self, ctx: Ctx) -> Dict[str, List[Dict[str, Any]]]:
        return self._crm_indexes(ctx)[0]

    def _crm_indexes(self, ctx: Ctx) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]]]:
        """CRM companies by exact normalised name, and by name key (legal form and punctuation
        removed: "Booz Allen Hamilton, Inc." and "Booz Allen Hamilton" share one)."""
        exact: Dict[str, List[Dict[str, Any]]] = {}
        loose: Dict[str, List[Dict[str, Any]]] = {}
        for row in self.store.walk(ctx, "companies", {"status__ne": "merged"}, cap=200_000):
            ref = {"id": row["id"], "name": row["name"], "domain": row.get("domain"),
                   "hiring_score": row.get("hiring_score"), "hiring_count": row.get("hiring_count")}
            if row.get("normalized_name"):
                exact.setdefault(row["normalized_name"], []).append(ref)
            slug = company_name_key(row.get("name") or "")
            if slug and len(slug) >= 3:
                loose.setdefault(slug, []).append(ref)
        return exact, loose

    def _near(self, ctx: Ctx, item: Mapping[str, Any], names: Sequence[str]) -> List[Dict[str, Any]]:
        """CRM companies whose name differs from this company's only in legal form/punctuation."""
        loose = getattr(self._local, "loose", None)
        if loose is None:
            loose = self._crm_indexes(ctx)[1]
        found: Dict[str, Dict[str, Any]] = {}
        for name in list(names)[:20] + [item["display_name"]]:
            for ref in loose.get(company_name_key(name) or "", []):
                found.setdefault(ref["id"], ref)
        return list(found.values())

    def _fingerprint(self, ctx: Ctx) -> Tuple[Any, ...]:
        """Cheap marker of CRM-side changes (a company created, linked, refreshed or merged; a
        review queued or resolved) — from any process — so a cached grouping is never stale."""
        latest = self.store.rows(ctx, "companies", {}, order="-updated_at", limit=1)
        review = self.store.rows(ctx, "job_company_reviews", {}, order="-updated_at", limit=1)
        return (self.store.count(ctx, "companies", {}), str(latest[0]["updated_at"]) if latest else "",
                str(review[0]["updated_at"]) if review else "")

    def candidates(self, ctx: Ctx, params: Mapping[str, Any], *, limit: int = 50, offset: int = 0,
                   order: str = "open", state: Optional[str] = None, fresh: bool = False) -> Dict[str, Any]:
        """Hiring companies among the jobs ``params`` select, grouped by normalised name. The
        grouping is reused for paging/sorting for up to CACHE_SECONDS while the CRM is unchanged;
        ``fresh`` recomputes it."""
        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))
        cache_key = (ctx.workspace_id, json.dumps(self._clean(params), sort_keys=True, default=str))
        mark = self._fingerprint(ctx)
        cached = self._cache.get(cache_key)
        if cached and not fresh and time.monotonic() - cached[0] < CACHE_SECONDS and cached[2] == mark:
            all_rows = cached[1]
        else:
            all_rows, _ambiguous = self._group(ctx, params)
            self._cache = {k: v for k, v in self._cache.items() if time.monotonic() - v[0] < CACHE_SECONDS}
            self._cache[cache_key] = (time.monotonic(), all_rows, mark)
        rows = [dict(r) for r in all_rows]
        counts = Counter(r["state"] for r in rows)              # every state, whatever is selected
        all_total = len(rows)
        if state:
            wanted = {s.strip().upper() for s in str(state).split(",") if s.strip()}
            rows = [r for r in rows if r["state"] in wanted]
        sort_keys = {"open": lambda r: (-r["open_jobs"], -r["high"], r["key"]),
                     "high": lambda r: (-r["high"], -r["open_jobs"], r["key"]),
                     "jobs": lambda r: (-r["jobs"], r["key"]), "name": lambda r: r["key"]}
        rows.sort(key=sort_keys.get(order, sort_keys["open"]))
        page = rows[offset:offset + limit]
        page_reviews = {r["normalized_name"]: r for r in self.store.rows(
            ctx, "job_company_reviews", {"status": "pending", "normalized_name": [r["key"] for r in page]},
            limit=len(page) + 5)} if page else {}
        for row in page:
            row["names"] = Counter(row["names"])
        self._decorate(ctx, page, page_reviews)
        return {"rows": page, "total": len(rows), "all_total": all_total, "limit": limit, "offset": offset,
                "states": {s: counts.get(s, 0) for s in STATES}, "jobs_considered": sum(r["jobs"] for r in rows)}

    @staticmethod
    def _clean(params: Mapping[str, Any]) -> Dict[str, Any]:
        from cloud.intel.job_monitor.exports import clean_params

        return clean_params(params)

    def _group(self, ctx: Ctx, params: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """One grouped scan of the filtered jobs (total, open, HIGH, REVIEW, linked per raw company
        name), merged by normalised name and classified against the CRM."""
        filters = self._filters(ctx, params)
        stats = self.store.group_stats(ctx, "job_postings", "company_name", filters, {
            # HIGH / REVIEW are counted among the open jobs, like the Open jobs column
            "active": ("status", "in", list(ACTIVE)),
            "high": [("status", "in", list(ACTIVE)), ("relevance_class", "eq", "HIGH")],
            "review": [("status", "in", list(ACTIVE)), ("relevance_class", "eq", "REVIEW")],
            "linked": ("company_id", "notnull", None)})
        groups: Dict[str, Dict[str, Any]] = {}
        for raw, s in stats.items():
            key = _key(raw)
            if not key:
                continue
            g = groups.setdefault(key, {"key": key, "names": {}, "jobs": 0, "open_jobs": 0, "high": 0,
                                        "review": 0, "linked_jobs": 0})
            g["names"][raw] = g["names"].get(raw, 0) + s["count"]
            g["jobs"] += s["count"]
            g["open_jobs"] += s["active"]
            g["high"] += s["high"]
            g["review"] += s["review"]
            g["linked_jobs"] += s["linked"]
        crm, loose = self._crm_indexes(ctx)
        ambiguous = {r["normalized_name"] for r in self.store.walk(ctx, "job_company_reviews", {
            "status": "pending", "any_of": [{"reason__ilike": reason} for reason in _REVIEW_REASONS]}, cap=50_000)}
        rows = []
        for key, g in groups.items():
            matches = crm.get(key, [])
            near: List[Dict[str, Any]] = []
            if not matches:
                seen = set()
                for raw in g["names"]:
                    for ref in loose.get(company_name_key(raw) or "", []):
                        if ref["id"] not in seen:
                            seen.add(ref["id"])
                            near.append(ref)
            if g["linked_jobs"] and g["linked_jobs"] >= g["jobs"]:
                state = "LINKED"
            elif len(matches) == 1:
                state = "ALREADY IN CRM"
            elif len(matches) > 1 or near or key in ambiguous:
                state = "REVIEW"
            else:
                state = "NEW ACCOUNT"
            reason = ("several CRM companies share this exact name" if len(matches) > 1 else
                      NEAR_REASON if state == "REVIEW" and near else
                      "waiting in the company review queue" if state == "REVIEW" else None)
            if near and state == "REVIEW":
                matches = near
            rows.append({**g, "state": state, "crm": matches[:5], "match_reason": reason})
        return rows, {"ambiguous": len(ambiguous)}

    def invalidate(self, ctx: Ctx) -> None:
        self._cache = {k: v for k, v in self._cache.items() if k[0] != ctx.workspace_id}

    def _decorate(self, ctx: Ctx, page: List[Dict[str, Any]], reviews: Mapping[str, Mapping[str, Any]]) -> None:
        """Technologies, signals, score and a precise CRM state for the rows on one page only."""
        if not page:
            return
        names = sorted({n for r in page for n in r["names"]})
        keywords: Dict[str, Counter] = {r["key"]: Counter() for r in page}
        job_ids: Dict[str, List[str]] = {r["key"]: [] for r in page}
        for start in range(0, len(names), 200):
            chunk = {"company_name": names[start:start + 200], "status": list(ACTIVE)}
            # technologies: exact keyword counts over every open job, in one grouped query
            for raw, tags in self.store.group_tags(ctx, "job_postings", "company_name", "matched_keywords",
                                                   chunk).items():
                key = _key(raw)
                if key in keywords:
                    keywords[key].update(tags)
            # a few recent jobs per company, recorded as the originating jobs
            for job in self.store.rows(ctx, "job_postings", chunk, order="-first_seen_at", limit=500):
                key = _key(job.get("company_name"))
                if key in job_ids and len(job_ids[key]) < 5:
                    job_ids[key].append(job["id"])
        # a linked company IS the CRM company its jobs point at (even when several CRM companies
        # share the name): read the id from the jobs themselves
        linked_rows = {r["key"]: r for r in page if r["state"] == "LINKED"}
        if linked_rows:
            owner: Dict[str, str] = {}
            linked_names = sorted({n for r in linked_rows.values() for n in r["names"]})
            for start in range(0, len(linked_names), 200):
                for job in self.store.rows(ctx, "job_postings", {"company_name": linked_names[start:start + 200],
                                                                 "company_id__isnull": False}, order="id", limit=500):
                    owner.setdefault(_key(job.get("company_name")) or "", job["company_id"])
            for key, row in linked_rows.items():
                company = self.store.find(ctx, "companies", owner[key]) if key in owner else None
                if company is not None:
                    row["crm"] = [{"id": company["id"], "name": company["name"], "domain": company.get("domain"),
                                   "hiring_score": company.get("hiring_score"),
                                   "hiring_count": company.get("hiring_count")}]
        signals: Dict[str, List[Dict[str, Any]]] = {r["key"]: [] for r in page}
        # a candidate's own signals and score are not this company's: only rows that ARE the CRM
        # company (already in CRM / linked) borrow them
        same_company = ("ALREADY IN CRM", "LINKED")
        company_ids = [c["id"] for r in page if r["state"] in same_company for c in r["crm"]]
        for start in range(0, len(names), 200):
            for sig in self.store.walk(ctx, "hiring_signals", {"status": "active", "any_of": [
                    {"company_name": names[start:start + 200]},
                    *([{"company_id": company_ids}] if company_ids else [])]}, cap=20_000):
                key = _key(sig.get("company_name"))
                if key not in signals:
                    owner = next((r["key"] for r in page if r["state"] in same_company
                                  and any(c["id"] == sig.get("company_id") for c in r["crm"])), None)
                    key = owner
                if key in signals:
                    signals[key].append(sig)
        resolver = self.platform.service("dedupe")
        from cloud.intel.imports.dedupe import MERGEABLE
        from cloud.intel.signals.service import SIGNAL_WEIGHTS

        for row in page:
            row["display_name"] = row["names"].most_common(1)[0][0]
            row["names"] = [n for n, _ in row["names"].most_common()]
            row["technologies"] = [k for k, _ in keywords[row["key"]].most_common(8)]
            sigs = signals[row["key"]]
            row["signals"] = sorted({s["signal_type"] for s in sigs})
            row["signal_ids"] = [s["id"] for s in sorted(sigs, key=lambda s: -(s.get("strength") or 0))][:20]
            row["job_ids"] = job_ids[row["key"]]
            # The existing hiring score formula (sum of weight x strength x confidence, cap 100),
            # read from the CRM company when there is one, else previewed from the job signals.
            linked = row["crm"][0] if len(row["crm"]) == 1 and row["state"] in same_company else None
            if linked and linked.get("hiring_score") is not None:
                row["hiring_score"], row["score_source"] = linked["hiring_score"], "crm"
            else:
                value = sum(SIGNAL_WEIGHTS.get(s["signal_type"], 0) * (s.get("strength") or 0) / 100 *
                            (s.get("confidence") or 0) for s in sigs)
                row["hiring_score"], row["score_source"] = round(min(100.0, value), 1), "signals"
            pending = reviews.get(row["key"])
            row["review_id"] = pending["id"] if pending else None
            if row["state"] == "NEW ACCOUNT":
                match = resolver.resolve(ctx, {"name": row["display_name"]})
                if match.get("candidates"):
                    if match["outcome"] in MERGEABLE and match.get("company_id"):
                        row["state"], row["match_reason"] = "MATCHED", "; ".join(match.get("reasons") or [])[:200]
                        found = self.store.find(ctx, "companies", match["company_id"])
                        row["crm"] = [{"id": found["id"], "name": found["name"]}] if found else row["crm"]
                    else:
                        row["state"], row["match_reason"] = "REVIEW", "; ".join(match.get("reasons") or [])[:200]
                        row["crm"] = self._names(ctx, match["candidates"][:5])
            elif row["state"] == "REVIEW" and not row["crm"] and pending:
                row["crm"] = self._names(ctx, pending.get("candidate_ids") or [])
                row["match_reason"] = pending.get("reason")
            elif row["state"] == "ALREADY IN CRM":
                row["match_reason"] = "exact normalised company name"

    def _names(self, ctx: Ctx, ids: Iterable[str]) -> List[Dict[str, Any]]:
        out = []
        for company_id in ids:
            found = self.store.find(ctx, "companies", company_id)
            if found is not None:
                out.append({"id": found["id"], "name": found["name"], "domain": found.get("domain")})
        return out

    # ------------------------------------------------------------------------------------
    # guards
    # ------------------------------------------------------------------------------------

    def capacity(self) -> Dict[str, Any]:
        cap_mb = float(self.platform.config.extra.get("db_capacity_mb")
                       or os.environ.get("CAREERCLOUD_DB_CAPACITY_MB") or 500)
        size = None
        probe = getattr(self.store, "database_size_bytes", None)
        if callable(probe):
            try:
                size = probe()
            except Exception:  # noqa: BLE001 - an unknown size never blocks small work
                log.exception("could not read the database size")
        return {"capacity_mb": cap_mb, "size_mb": None if size is None else round(size / 2**20, 1)}

    def check(self, items: Sequence[Any], *, confirm: bool) -> Dict[str, Any]:
        if not items:
            raise ValidationError("select at least one company")
        if len(items) > MAX_PER_ACTION:
            raise ValidationError(f"at most {MAX_PER_ACTION} companies per action ({len(items)} selected)")
        if len(items) > CONFIRM_ABOVE and not confirm:
            raise ValidationError(f"{len(items)} companies selected: confirm to continue (over {CONFIRM_ABOVE})")
        cap = self.capacity()
        if cap["size_mb"] is not None:
            projected = cap["size_mb"] + len(items) * BYTES_PER_ACCOUNT / 2**20
            cap["projected_mb"] = round(projected, 1)
            if len(items) > SYNC_LIMIT and projected > cap["capacity_mb"] * CAPACITY_SHARE:
                raise ValidationError(f"database capacity guard: {cap['size_mb']} MB used of {cap['capacity_mb']:g} MB "
                                      f"(limit {CAPACITY_SHARE:.0%}); free space before large account builds")
        return cap

    # ------------------------------------------------------------------------------------
    # create / link
    # ------------------------------------------------------------------------------------

    def _item(self, ctx: Ctx, raw: Any) -> Dict[str, Any]:
        """``{"key", "names", "display_name", "signal_ids", "job_ids", "override"}`` from a request item."""
        if isinstance(raw, str):
            raw = {"name": raw}
        names = [str(n) for n in (raw.get("names") or [raw.get("name") or raw.get("display_name") or ""]) if n]
        key = _key(raw.get("key") or (names[0] if names else None))
        if not key:
            raise ValidationError("every company needs a name")
        if not names:
            names = [raw.get("display_name") or key]
        return {"key": key, "names": names[:20], "display_name": str(raw.get("display_name") or names[0])[:300],
                "signal_ids": [str(s) for s in (raw.get("signal_ids") or [])][:20],
                "job_ids": [str(j) for j in (raw.get("job_ids") or [])][:10],
                "override": bool(raw.get("create_new") or raw.get("override"))}

    def _name_index(self, ctx: Ctx) -> Dict[str, List[str]]:
        """normalised name -> every raw company spelling in the job database (one grouped
        query, cached for the current build so a batch reads it once)."""
        cached = getattr(self, "_index_cache", None)
        if cached is not None and cached[0] == ctx.workspace_id:
            return cached[1]
        index: Dict[str, List[str]] = {}
        for raw in self.store.group_count(ctx, "job_postings", "company_name", {}):
            key = _key(raw)
            if key:
                index.setdefault(key, []).append(raw)
        self._index_cache = (ctx.workspace_id, index)
        return index

    def _all_names(self, ctx: Ctx, item: Mapping[str, Any]) -> List[str]:
        """Every raw spelling in the job database that normalises EXACTLY to the item's key. A
        bulk run reads the full index once; a single operation asks only for names containing
        the key's most distinctive word (one grouped query instead of every company name)."""
        cached = getattr(self, "_index_cache", None)
        if cached is not None and cached[0] == ctx.workspace_id:
            return sorted(set(item["names"]) | set(cached[1].get(item["key"], [])))
        words = sorted((w for w in str(item["key"]).split() if len(w) >= 3), key=len, reverse=True)
        if not words:
            return sorted(set(item["names"]) | set(self._name_index(ctx).get(item["key"], [])))
        found = [raw for raw in self.store.group_count(ctx, "job_postings", "company_name",
                                                       {"company_name__ilike": words[0]})
                 if _key(raw) == item["key"]]
        return sorted(set(item["names"]) | set(found))

    def build(self, ctx: Ctx, items: Sequence[Any], *, confirm: bool = False, list_id: Optional[str] = None,
              new_list: Optional[str] = None) -> Dict[str, Any]:
        """Create (or link) accounts for the selected candidates; small selections run now,
        larger ones as a background task (kind ``signals``, operation ``account_build``)."""
        ctx.require_write()
        parsed = [self._item(ctx, raw) for raw in items]
        cap = self.check(parsed, confirm=confirm)
        target = self._target_list(ctx, list_id, new_list) if (list_id or new_list) else None
        if len(parsed) <= SYNC_LIMIT:
            result = self.run_items(ctx, parsed, list_id=target["id"] if target else None)
            return {**result, "capacity": cap, "list": target, "mode": "sync"}
        task = self.platform.tasks.submit(ctx, "signals", {"operation": "account_build", "items": parsed,
                                                           "list_id": target["id"] if target else None},
                                          max_attempts=3)
        audit(self.store, ctx, "accounts.build_queued", entity_type="platform_tasks", entity_id=task["id"],
              summary=f"{len(parsed)} hiring companies")
        return {"mode": "task", "task_id": task["id"], "queued": len(parsed), "capacity": cap, "list": target}

    def run_items(self, ctx: Ctx, items: Sequence[Mapping[str, Any]], *, list_id: Optional[str] = None,
                  start: int = 0, progress: Optional[Callable[[int, Dict[str, Any]], None]] = None,
                  stop: Optional[Callable[[], Optional[str]]] = None) -> Dict[str, Any]:
        # near-duplicate index and job-name index for this run only (cleared even when a step fails)
        self._local.loose = self._crm_indexes(ctx)[1]
        self._index_cache = None
        if len(items) - start > SYNC_LIMIT:
            self._name_index(ctx)                      # one full read beats a lookup per company
        try:
            return self._run_items(ctx, items, list_id=list_id, start=start, progress=progress, stop=stop)
        finally:
            self._local.loose = None
            self._index_cache = None

    def _run_items(self, ctx: Ctx, items: Sequence[Mapping[str, Any]], *, list_id: Optional[str],
                   start: int, progress: Optional[Callable[[int, Dict[str, Any]], None]],
                   stop: Optional[Callable[[], Optional[str]]]) -> Dict[str, Any]:
        stats = {"created": 0, "linked": 0, "review": 0, "skipped": 0, "errors": 0, "jobs_linked": 0,
                 "signals_linked": 0, "listed": 0}
        results: List[Dict[str, Any]] = []
        for index in range(start, len(items)):
            if stop is not None:
                why = stop()
                if why:
                    return {"stats": stats, "results": results, "stopped": why, "index": index}
            item = items[index]
            try:
                outcome = self.build_one(ctx, item)
            except (ValidationError, NotFoundError) as error:
                outcome = {"key": item["key"], "name": item["display_name"], "outcome": "error", "error": str(error)}
            if outcome["outcome"] == "error":
                stats["errors"] += 1
            else:
                stats[outcome["outcome"]] = stats.get(outcome["outcome"], 0) + 1
                if outcome["outcome"] in ("created", "linked"):
                    stats["jobs_linked"] += outcome.get("jobs_attached", outcome.get("jobs_linked", 0))
                    stats["signals_linked"] += outcome.get("signals_attached", outcome.get("signals_linked", 0))
                if list_id and outcome.get("company_id"):
                    stats["listed"] += self._add(ctx, list_id, outcome["company_id"], item)
            results.append(outcome)
            if progress is not None:
                progress(index + 1, stats)
        return {"stats": stats, "results": results, "stopped": None, "index": len(items)}

    def build_one(self, ctx: Ctx, item: Mapping[str, Any]) -> Dict[str, Any]:
        """One candidate: link to its CRM company when it already has one or a safe match exists,
        queue an ambiguous one for review (unless ``override``), else create the account."""
        names = self._all_names(ctx, item)
        linked_to = self.store.first(ctx, "job_postings", {"company_name": names, "company_id__isnull": False})
        if linked_to is not None and self.store.find(ctx, "companies", linked_to["company_id"]) is not None:
            done = self.link(ctx, item, linked_to["company_id"], reason="already linked", _names=names)
            return {**done, "outcome": "linked" if done["jobs_linked"] or done["signals_linked"] else "skipped"}
        same = self.store.all(ctx, "companies", {"normalized_name": item["key"], "status__ne": "merged"}, cap=10)
        if len(same) == 1 and not item["override"]:
            # an exact normalised-name match is the same company: link, never create a duplicate
            done = self.link(ctx, item, same[0]["id"], reason="exact normalised name", _names=names)
            return {**done, "outcome": "linked"}
        if len(same) > 1 and not item["override"]:
            match = {"candidates": [c["id"] for c in same], "reasons": ["several CRM companies share this name"]}
            review = self._queue_review(ctx, item, names, match)
            return {"key": item["key"], "name": item["display_name"], "outcome": "review", "review_id": review["id"],
                    "candidates": self._names(ctx, match["candidates"]), "reason": match["reasons"][0]}
        if not item["override"]:
            near = self._near(ctx, item, names)
            if near:
                match = {"candidates": [c["id"] for c in near][:10], "reasons": [NEAR_REASON]}
                review = self._queue_review(ctx, item, names, match)
                return {"key": item["key"], "name": item["display_name"], "outcome": "review",
                        "review_id": review["id"], "candidates": [{"id": c["id"], "name": c["name"]} for c in near][:10],
                        "reason": NEAR_REASON}
        if not item["job_ids"]:
            item["job_ids"] = [j["id"] for j in self.store.rows(
                ctx, "job_postings", {"company_name": names[:_LINK_BATCH]}, order="-first_seen_at", limit=5)]
        original = {"job_company_names": names[:20], "signal_ids": item["signal_ids"], "job_ids": item["job_ids"],
                    "account_builder": True}
        self._local.building = True
        try:
            result = self.platform.service("crm").upsert_company(
                ctx, {"name": item["display_name"]}, source_kind="discovery", source_name=SOURCE_NAME,
                source_ref=item["key"], original=original, auto_merge=True, create_if_ambiguous=item["override"])
        finally:
            self._local.building = False
        if result["needs_review"]:
            review = self._queue_review(ctx, item, names, result["match"])
            return {"key": item["key"], "name": item["display_name"], "outcome": "review", "review_id": review["id"],
                    "candidates": self._names(ctx, result["match"].get("candidates") or []),
                    "reason": "; ".join(result["match"].get("reasons") or [])[:300]}
        company = result["company"]
        loose = getattr(self._local, "loose", None)
        slug = company_name_key(company.get("name") or "")
        if result["created"] and loose is not None and slug:
            # later companies in this run must see this one as a possible duplicate
            loose.setdefault(slug, []).append({"id": company["id"], "name": company["name"]})
        done = self.link(ctx, item, company["id"], reason="created" if result["created"] else "matched",
                         _names=names)
        return {**done, "outcome": "created" if result["created"] else "linked"}

    def link(self, ctx: Ctx, item: Any, company_id: str, *, reason: str = "linked by user",
             _names: Optional[List[str]] = None) -> Dict[str, Any]:
        """Attach every job and job-derived signal of this company name to ``company_id``, resolve
        its review row and refresh the company's hiring count, signals and scores."""
        ctx.require_write()
        item = self._item(ctx, item) if not isinstance(item, Mapping) or "key" not in item else item
        company = self.store.get(ctx, "companies", company_id)
        names = _names or self._all_names(ctx, item)
        jobs_linked = self._attach(ctx, "job_postings", {"company_name": names, "company_id__isnull": True},
                                   {"company_id": company["id"], "company_match": "matched"})
        signals_linked = self._attach(ctx, "hiring_signals", {"company_name": names, "company_id__isnull": True},
                                      {"company_id": company["id"]})
        reviews = self.store.all(ctx, "job_company_reviews", {"normalized_name": item["key"], "status": "pending"},
                                 cap=5)
        for review in reviews:
            self.store.update(ctx, "job_company_reviews", review["id"], {"status": "linked", "company_id": company["id"]})
        # an already-linked company with nothing new attached is unchanged: no rescore, no audit
        # noise (adding linked companies to a list stays fast)
        if jobs_linked or signals_linked or reviews or reason != "already linked":
            self.refresh(ctx, company["id"])
            self.invalidate(ctx)
            audit(self.store, ctx, "accounts.linked", entity_type="companies", entity_id=company["id"],
                  summary=f"{item['display_name']}: {jobs_linked:,} jobs, {signals_linked:,} signals ({reason})",
                  changes={"names": names[:20], "reason": reason, "signal_ids": item.get("signal_ids", [])[:20]})
        jobs_attached = self.store.count(ctx, "job_postings", {"company_id": company["id"]})
        signals_attached = self.store.count(ctx, "hiring_signals", {"company_id": company["id"]})
        return {"key": item["key"], "name": item["display_name"], "company_id": company["id"],
                "company_name": company["name"], "jobs_linked": jobs_linked, "signals_linked": signals_linked,
                "jobs_attached": jobs_attached, "signals_attached": signals_attached, "reason": reason}

    def _attach(self, ctx: Ctx, entity: str, filters: Mapping[str, Any], patch: Mapping[str, Any]) -> int:
        """One set-based update (a large employer can have thousands of jobs); company names are
        sent in chunks so the statement stays bounded."""
        names = list(filters.get("company_name") or [])
        rest = {k: v for k, v in filters.items() if k != "company_name"}
        total = 0
        for start in range(0, len(names), _LINK_BATCH):
            total += self.store.update_where(ctx, entity, {**rest, "company_name": names[start:start + _LINK_BATCH]},
                                             dict(patch))
        return total

    def refresh(self, ctx: Ctx, company_id: str) -> None:
        """Hiring count (open + stale jobs) and the existing signal / score engines."""
        try:
            self.platform.service("jobs").refresh_company(ctx, company_id)
        except Exception:  # noqa: BLE001 - derived data; the link itself is stored
            log.exception("hiring count refresh failed for %s", company_id)
        try:
            self.platform.service("signals").detect_for_company(ctx, company_id)
        except Exception:  # noqa: BLE001
            log.exception("signal/score refresh failed for %s", company_id)

    def _queue_review(self, ctx: Ctx, item: Mapping[str, Any], names: List[str], match: Mapping[str, Any]
                      ) -> Dict[str, Any]:
        existing = self.store.first(ctx, "job_company_reviews", {"normalized_name": item["key"]})
        values = {"status": "pending", "candidate_ids": list(match.get("candidates") or [])[:10],
                  "reason": ("possible duplicate: " + "; ".join(match.get("reasons") or []))[:300],
                  "last_seen_at": utcnow()}
        if existing is not None:
            return self.store.update(ctx, "job_company_reviews", existing["id"], values)
        count = self.store.count(ctx, "job_postings", {"company_name": names})
        try:
            return self.store.insert(ctx, "job_company_reviews", {
                "company_name": item["display_name"][:300], "normalized_name": item["key"][:300],
                "job_count": count, **values})
        except ConflictError:
            return self.store.first(ctx, "job_company_reviews", {"normalized_name": item["key"]})

    # ------------------------------------------------------------------------------------
    # lists
    # ------------------------------------------------------------------------------------

    def _target_list(self, ctx: Ctx, list_id: Optional[str], new_list: Optional[str]) -> Dict[str, Any]:
        crm = self.platform.service("crm")
        if list_id:
            target = self.store.get(ctx, "lists", list_id)
            if target["entity_type"] != "companies":
                raise ValidationError(f"list {target['name']} holds {target['entity_type']}, not companies")
            return target
        name = str(new_list or "").strip()[:200]
        if not name:
            raise ValidationError("name the new list")
        existing = self.store.first(ctx, "lists", {"name": name})
        if existing is not None:
            if existing["entity_type"] != "companies":
                raise ValidationError(f"a list named {name!r} already holds {existing['entity_type']}")
            return existing
        return crm.create_list(ctx, name, "companies", description="Built from hiring companies (Account Builder)",
                               source="account_builder")

    def _add(self, ctx: Ctx, list_id: str, company_id: str, item: Mapping[str, Any]) -> int:
        reason = _reason(at=utcnow().isoformat(timespec="seconds"), signal_ids=item.get("signal_ids") or [],
                         job_ids=item.get("job_ids") or [], company_name=item.get("display_name"))
        return self.platform.service("crm").add_to_list(ctx, list_id, "companies", [company_id], reason=reason)

    def add_to_list(self, ctx: Ctx, company_items: Sequence[Mapping[str, Any]], *, list_id: Optional[str] = None,
                    new_list: Optional[str] = None) -> Dict[str, Any]:
        """Add CRM companies (``{"company_id", "signal_ids", "job_ids"}``) to a company list."""
        ctx.require_write()
        if not company_items:
            raise ValidationError("select at least one company")
        if len(company_items) > MAX_PER_ACTION:
            raise ValidationError(f"at most {MAX_PER_ACTION} companies per action")
        target = self._target_list(ctx, list_id, new_list)
        added = 0
        for raw in company_items:
            company = self.store.get(ctx, "companies", str(raw.get("company_id") or ""))
            added += self._add(ctx, target["id"], company["id"], {
                "signal_ids": [str(s) for s in raw.get("signal_ids") or []][:20],
                "job_ids": [str(j) for j in raw.get("job_ids") or []][:10], "display_name": company["name"]})
        return {"list": self.store.get(ctx, "lists", target["id"]), "added": added,
                "already": len(company_items) - added}

    # ------------------------------------------------------------------------------------
    # company list -> contacts -> email validation
    # ------------------------------------------------------------------------------------

    def _list_companies(self, ctx: Ctx, list_id: str) -> Tuple[Dict[str, Any], List[str]]:
        target = self.store.get(ctx, "lists", list_id)
        if target["entity_type"] != "companies":
            raise ValidationError(f"list {target['name']} holds {target['entity_type']}, not companies")
        ids = [m["entity_id"] for m in self.store.all(ctx, "list_members", {"list_id": list_id}, cap=MAX_PER_ACTION * 4)]
        return target, ids

    def list_summary(self, ctx: Ctx, list_id: str) -> Dict[str, Any]:
        """Where a company list is in the prospecting flow: companies, companies with contacts,
        contacts, and their email status."""
        target, company_ids = self._list_companies(ctx, list_id)
        contacts = self.store.all(ctx, "contacts", {"company_id": company_ids}, cap=20_000) if company_ids else []
        statuses = Counter(str(c.get("email_status") or ("NO EMAIL" if not c.get("email") else "NOT VALIDATED"))
                           for c in contacts)
        return {"list_id": target["id"], "companies": len(company_ids),
                "companies_with_contacts": len({c["company_id"] for c in contacts}),
                "contacts": len(contacts), "with_email": sum(1 for c in contacts if c.get("email")),
                "email_status": dict(statuses)}

    def validate_list_contacts(self, ctx: Ctx, list_id: str) -> Dict[str, Any]:
        """One email-validation job (built-in checks; paid checks stay a separate, confirmed step)
        for the contacts of every company in the list."""
        ctx.require_write()
        target, company_ids = self._list_companies(ctx, list_id)
        contacts = self.store.all(ctx, "contacts", {"company_id": company_ids}, cap=20_000) if company_ids else []
        ids = [c["id"] for c in contacts if c.get("email")]
        if not ids:
            raise ValidationError("no contact with an email address yet: find contacts for this list first")
        return self.platform.service("email_jobs").create_from_contacts(
            ctx, name=f"Contacts of {target['name']}"[:200], contact_ids=ids, start=True)

    # ------------------------------------------------------------------------------------
    # auto re-match (called after any company is created)
    # ------------------------------------------------------------------------------------

    def rematch(self, ctx: Ctx, company: Mapping[str, Any]) -> Dict[str, Any]:
        """A new CRM company may resolve pending job/company reviews: link only when its normalised
        name equals the review's and it is now the ONLY CRM company with that name."""
        key = company.get("normalized_name")
        if not key or getattr(self._local, "building", False):
            return {"linked": False}
        review = self.store.first(ctx, "job_company_reviews", {"normalized_name": key, "status": "pending"})
        if review is None:
            return {"linked": False}
        same = self.store.count(ctx, "companies", {"normalized_name": key, "status__ne": "merged"})
        if same != 1:
            self.store.update(ctx, "job_company_reviews", review["id"], {
                "candidate_ids": [c["id"] for c in self.store.all(ctx, "companies", {
                    "normalized_name": key, "status__ne": "merged"}, cap=10)],
                "reason": "several CRM companies share this name"})
            return {"linked": False, "reason": "ambiguous"}
        done = self.link(ctx, {"key": key, "names": [review["company_name"]], "display_name": review["company_name"],
                               "signal_ids": [], "job_ids": [], "override": False}, company["id"],
                         reason="auto re-match: exact normalised name")
        return {"linked": True, **done}


def run_account_build_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    """Background account build: checkpointed per company, pausable and cancellable."""
    from cloud.intel.tasks.worker import TaskCancelled, TaskPaused

    params = task.get("params") or {}
    items = list(params.get("items") or [])
    service: AccountBuilderService = platform.service("account_builder")
    checkpoint = getattr(reporter, "checkpoint", None) or {}
    start = int(checkpoint.get("index", 0))
    totals = dict(checkpoint.get("stats") or {})

    def progress(done: int, stats: Dict[str, Any]) -> None:
        merged = {k: totals.get(k, 0) + v for k, v in stats.items()}
        reporter.progress(f"Account Builder: {done:,} of {len(items):,} companies", done=done, total=len(items),
                          stats=merged, checkpoint={"index": done, "stats": merged})

    def stop() -> Optional[str]:
        if reporter.is_cancelled():
            return "cancelled"
        if reporter.should_pause():
            return "paused"
        return None

    result = service.run_items(ctx, items, list_id=params.get("list_id"), start=start, progress=progress, stop=stop)
    merged = {k: totals.get(k, 0) + v for k, v in result["stats"].items()}
    if result["stopped"] == "cancelled":
        raise TaskCancelled()
    if result["stopped"] == "paused":
        raise TaskPaused({"index": result["index"], "stats": merged})
    audit(platform.store, ctx, "accounts.build_completed", summary=f"{len(items):,} hiring companies",
          changes=merged)
    return {"operation": "account_build", "companies": len(items), **merged}
