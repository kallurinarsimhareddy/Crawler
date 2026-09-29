"""The AI research agent: plan -> preview -> approve -> execute -> propose -> apply.

    research = platform.service("research")
    run = research.plan(ctx, "Find 500 US manufacturing companies with ERP hiring, ...")
    # the user reviews run["plan"], run["estimated_credits"], run["proposed_actions"] (preview)
    research.approve(ctx, run["id"], allow_paid=False)        # queues a "research" task
    # ... the task writes research_results (each with evidence) and proposed actions
    research.apply_actions(ctx, run["id"], ["a1", "a2"])      # only now does CRM data change

Guarantees:

* Planning and execution never change CRM records. Lists, opportunities and
  campaign assignments are *proposed actions*; each is applied only through
  :meth:`ResearchService.apply_actions`, by a writer, and audited.
* Paid providers are used only when the approval says ``allow_paid=True``;
  otherwise every credit-spending step runs in its free mode and reports it.
* Every result row carries the evidence that put it there.
"""

from __future__ import annotations

import csv
import json
import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, ValidationError, utcnow
from cloud.intel.research.intent import parse_intent, refine_with_ai
from cloud.intel.research.planner import build_plan, estimate_credits
from cloud.intel.research.tools import TOOLS, ResearchState

__all__ = ["ResearchService", "run_research_task"]

log = logging.getLogger(__name__)

_PREVIEW_ACTIONS = {
    "assign_campaign": "Assign each result to its best-matching campaign (via its opportunity)",
    "create_list": "Create an outreach list containing the ranked companies",
    "create_opportunities": "Create one CRM opportunity per ranked company, with signals, score and campaign",
}


class ResearchService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform

    # --- plan / preview ---------------------------------------------------------

    def plan(self, ctx: Ctx, question: str) -> Dict[str, Any]:
        ctx.require_write()
        question = (question or "").strip()
        if len(question) < 5:
            raise ValidationError("ask a research question")
        if len(question) > 4000:
            raise ValidationError("the question is too long (4,000 characters at most)")
        intent = parse_intent(question)
        ai = self.platform.service("ai").for_ctx(ctx, "research_planning")
        planner = "rules"
        if ai.external:
            intent = refine_with_ai(intent, ai)
            planner = intent.get("parser", "rules")
        plan = build_plan(intent)
        preview = [{"id": f"p{i + 1}", "type": step["tool"], "description": _PREVIEW_ACTIONS[step["tool"]],
                    "status": "preview"} for i, step in enumerate(plan) if step["tool"] in _PREVIEW_ACTIONS]
        run = self.platform.store.insert(ctx, "research_runs", {
            "question": question, "intent": intent, "plan": plan, "status": "planned", "planner": planner,
            "estimated_credits": estimate_credits(plan), "proposed_actions": preview,
            "progress": {"message": "Planned; review and approve to run"}})
        audit(self.platform.store, ctx, "research.plan", entity_type="research_runs", entity_id=run["id"],
              summary=question[:500])
        return run

    def approve(self, ctx: Ctx, run_id: str, *, allow_paid: bool = False) -> Dict[str, Any]:
        ctx.require_write()
        run = self.platform.store.get(ctx, "research_runs", run_id)
        if run["status"] != "planned":
            raise ConflictError(f"only a planned run can be approved (this one is {run['status']})")
        run = self.platform.store.update(ctx, "research_runs", run_id, {
            "status": "approved", "approved_by": ctx.user_id,
            "progress": {"message": "Approved", "allow_paid": bool(allow_paid)}}, expected_version=run["version"])
        task = self.platform.tasks.submit(ctx, "research", {"run_id": run_id, "allow_paid": bool(allow_paid)},
                                          entity_type="research_runs", entity_id=run_id,
                                          idempotency_key=f"research:{run_id}")
        run = self.platform.store.update(ctx, "research_runs", run_id, {"task_id": task["id"]})
        audit(self.platform.store, ctx, "research.approve", entity_type="research_runs", entity_id=run_id,
              changes={"allow_paid": bool(allow_paid)})
        return run

    def results(self, ctx: Ctx, run_id: str, *, limit: int = 100, offset: int = 0):
        self.platform.store.get(ctx, "research_runs", run_id)
        return self.platform.store.list(ctx, "research_results", {"run_id": run_id}, order="rank", limit=limit,
                                        offset=offset)

    # --- apply proposals -----------------------------------------------------------

    def apply_actions(self, ctx: Ctx, run_id: str, action_ids: Sequence[str]) -> Dict[str, Any]:
        ctx.require_write()
        store = self.platform.store
        run = store.get(ctx, "research_runs", run_id)
        if run["status"] != "completed":
            raise ConflictError("actions can be applied only after the run has completed")
        actions = [dict(a) for a in run["proposed_actions"]]
        wanted = set(action_ids)
        unknown = wanted - {a["id"] for a in actions}
        if unknown:
            raise ValidationError(f"unknown actions: {', '.join(sorted(unknown))}")
        crm = self.platform.service("crm")
        for action in actions:
            if action["id"] not in wanted:
                continue
            if action.get("status") == "applied":
                continue
            try:
                action["result"] = self._apply(ctx, crm, action)
                action["status"] = "applied"
                action["applied_at"] = utcnow().isoformat()
                action["applied_by"] = ctx.user_id
            except Exception as error:  # noqa: BLE001 - record per-action failures; others still apply
                action["status"] = "failed"
                action["error"] = str(error)[:500]
            audit(store, ctx, f"research.apply.{action['type']}", entity_type="research_runs", entity_id=run_id,
                  summary=action.get("description"), changes={"action_id": action["id"], "status": action["status"]})
        return store.update(ctx, "research_runs", run_id, {"proposed_actions": actions})

    def _apply(self, ctx: Ctx, crm: Any, action: Dict[str, Any]) -> Dict[str, Any]:
        params = action.get("params") or {}
        if action["type"] == "create_list":
            row = self.platform.store.insert(ctx, "lists", {"name": params["name"][:200], "entity_type": "companies",
                                                            "source": "research"})
            added = crm.add_to_list(ctx, row["id"], "companies", params["company_ids"],
                                    reason=f"research run {action.get('run_id')}")
            return {"list_id": row["id"], "added": added}
        if action["type"] == "create_opportunities":
            created = []
            for item in params["items"]:
                opp = crm.create_opportunity(
                    ctx, item["company_id"], item["title"], signal_ids=item.get("signal_ids", ()),
                    signal_types=item.get("signal_types", ()), score=item.get("score"),
                    score_breakdown=item.get("score_breakdown"), reason=item.get("reason"),
                    campaign_id=item.get("campaign_id"), evidence=item.get("evidence", ()), source="research")
                created.append(opp["id"])
            return {"opportunity_ids": created}
        if action["type"] == "assign_campaign":
            tagged = 0
            for item in params["items"]:
                company = self.platform.store.get(ctx, "companies", item["company_id"])
                tag = f"campaign:{item['campaign_key']}"
                if tag not in company["tags"]:
                    self.platform.store.update(ctx, "companies", company["id"], {"tags": company["tags"] + [tag]})
                    tagged += 1
            return {"tagged": tagged}
        raise ValidationError(f"unsupported action {action['type']}")


# --- execution ------------------------------------------------------------------------


def _proposals(run: Mapping[str, Any], state: ResearchState) -> List[Dict[str, Any]]:
    tools = [s["tool"] for s in run["plan"]]
    ids = list(state.order)
    out: List[Dict[str, Any]] = []
    n = 0

    def add(kind: str, description: str, params: Dict[str, Any]) -> None:
        nonlocal n
        n += 1
        out.append({"id": f"a{n}", "type": kind, "description": description, "params": params,
                    "status": "proposed", "run_id": run["id"]})

    if not ids:
        return out
    if "create_list" in tools:
        step = next(s for s in run["plan"] if s["tool"] == "create_list")
        add("create_list", f"Create list '{step['params']['name'][:80]}' with {len(ids)} companies",
            {"name": step["params"]["name"], "company_ids": ids})
    campaign_items = []
    for cid in ids:
        campaign = state.extra(cid).get("campaign")
        if campaign:
            campaign_items.append({"company_id": cid, "campaign_id": campaign["id"], "campaign_key": campaign["key"],
                                   "reasons": campaign.get("reasons", [])})
    if "assign_campaign" in tools and campaign_items:
        add("assign_campaign", f"Tag {len(campaign_items)} companies with their best campaign",
            {"items": campaign_items})
    if "create_opportunities" in tools:
        items = []
        for cid in ids:
            extra = state.extra(cid)
            company = state.companies[cid]
            signals = extra.get("signals") or []
            scores = extra.get("scores") or {}
            campaign = extra.get("campaign") or {}
            items.append({
                "company_id": cid,
                "title": f"{company['name']} — {campaign.get('name') or 'hiring opportunity'}"[:300],
                "signal_ids": [s["id"] for s in signals][:20], "signal_types": sorted({s["signal_type"] for s in signals}),
                "score": scores.get("opportunity_score"), "score_breakdown": scores.get("breakdown") or {},
                "reason": "; ".join(e["reason"] for e in state.evidence.get(cid, [])[:6])[:2000],
                "campaign_id": campaign.get("id"), "evidence": state.evidence.get(cid, [])[:20]})
        add("create_opportunities", f"Create {len(items)} opportunities", {"items": items})
    return out


def _export(platform: Any, ctx: Ctx, run_id: str, rows: List[Dict[str, Any]], fmt: str) -> Dict[str, Any]:
    from cloud.worker.results import neutralise_cell

    columns = ["rank", "company", "domain", "industry", "country", "technologies", "opportunity_score",
               "signals", "relevant_jobs", "contact_gap", "campaign", "evidence"]
    flat = []
    for row in rows:
        data = row["data"]
        flat.append({
            "rank": row["rank"], "company": data.get("name"), "domain": data.get("domain"),
            "industry": data.get("industry"), "country": data.get("country"),
            "technologies": ", ".join(data.get("technologies") or []), "opportunity_score": row.get("score"),
            "signals": ", ".join(sorted({s["type"] for s in data.get("signals", [])})),
            "relevant_jobs": len(data.get("jobs", [])),
            "contact_gap": ", ".join(f"{k}:{v}" for k, v in (data.get("contact_gap") or {}).items()),
            "campaign": (data.get("campaign") or {}).get("name"),
            "evidence": " | ".join(e["reason"] for e in row["evidence"][:8])})
    key = f"platform/{ctx.workspace_id}/research/{run_id}/results.{fmt}"
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / f"results.{fmt}"
        if fmt == "json":
            path.write_text(json.dumps(rows, default=str, indent=1), encoding="utf-8")
            content_type = "application/json"
        elif fmt == "csv":
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                for item in flat:
                    writer.writerow({k: neutralise_cell(v) for k, v in item.items()})
            content_type = "text/csv"
        else:
            from openpyxl import Workbook

            book = Workbook()
            sheet = book.active
            sheet.title = "Research"
            sheet.append(columns)
            for item in flat:
                sheet.append([neutralise_cell(item[c]) for c in columns])
            book.save(path)
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        stored = platform.storage.put_file(key, path, content_type=content_type)
    return {"format": fmt, "storage_key": key, "content_type": content_type, "size_bytes": stored.size_bytes,
            "sha256": stored.sha256, "filename": f"research-{run_id}.{fmt}"}


def run_research_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    from cloud.intel.tasks.worker import PermanentTaskError, TaskCancelled

    if task["params"].get("agent_run_id"):
        # AI Control Room runs share the research task kind (task kinds are fixed by migration 0003).
        from cloud.intel.agent.service import run_agent_task

        return run_agent_task(platform, ctx, task, reporter)
    store = platform.store
    run = store.find(ctx, "research_runs", task["params"].get("run_id", ""))
    if run is None:
        raise PermanentTaskError("research run not found")
    if run["status"] not in ("approved", "running"):
        raise PermanentTaskError(f"research run is {run['status']}, not approved")
    allow_paid = bool(task["params"].get("allow_paid"))
    state = ResearchState(platform, ctx, allow_paid=allow_paid)
    plan = [dict(step) for step in run["plan"]]
    store.update(ctx, "research_runs", run["id"], {"status": "running", "plan": plan})
    try:
        for step in plan:
            if reporter.is_cancelled():
                store.update(ctx, "research_runs", run["id"], {"status": "cancelled", "plan": plan})
                raise TaskCancelled()
            tool = TOOLS[step["tool"]]
            reporter.progress(f"{step['id']}: {step['description']}", step=step["id"])
            report = tool["fn"](state, step["params"])
            if step["spends_credits"] and not allow_paid:
                report["detail"] = report.get("detail", "") + " — paid providers not used (not approved)"
            step.update({"status": report.get("status", "done"), "result": report, "finished_at": utcnow().isoformat()})
            store.update(ctx, "research_runs", run["id"], {"plan": plan, "progress": {
                "message": f"{step['id']} {step['tool']}: {report.get('detail', '')}"[:500],
                "companies": len(state.order)}})
    except TaskCancelled:
        raise
    except Exception as error:
        store.update(ctx, "research_runs", run["id"], {"status": "failed", "plan": plan, "error": str(error)[:4000]})
        raise

    # Results, ranked, each with its evidence.
    for old in store.all(ctx, "research_results", {"run_id": run["id"]}):
        store.delete(ctx, "research_results", old["id"])  # a retried attempt starts clean
    rows = []
    for rank, cid in enumerate(state.order, start=1):
        company = state.companies[cid]
        extra = state.extra(cid)
        scores = extra.get("scores") or {}
        data = {
            "name": company["name"], "domain": company.get("domain"), "website": company.get("website"),
            "industry": company.get("industry"), "country": company.get("country"), "state": company.get("state"),
            "technologies": company.get("technologies") or [], "scores": scores,
            "signals": [{"id": s["id"], "type": s["signal_type"], "summary": s.get("summary"),
                         "confidence": s.get("confidence")} for s in extra.get("signals") or []][:20],
            "jobs": [{"id": j["id"], "title": j["title"], "url": j.get("job_url")} for j in extra.get("jobs") or []][:20],
            "scraped_jobs": extra.get("scraped_jobs", [])[:50],
            "contact_gap": {k: v["status"] for k, v in (extra.get("contact_gap") or {}).items()},
            "contacts": extra.get("contacts", [])[:10], "email_validation": extra.get("email_validation", {}),
            "campaign": extra.get("campaign"), "possible_duplicates": extra.get("possible_duplicates", []),
        }
        row = store.insert(ctx, "research_results", {
            "run_id": run["id"], "rank": rank, "company_id": cid, "data": data,
            "evidence": state.evidence.get(cid, [])[:50], "score": scores.get("opportunity_score")})
        rows.append(row)

    proposals = _proposals(run, state)
    files = {}
    export_step = next((s for s in plan if s["tool"] == "export"), None)
    if export_step and rows:
        files = _export(platform, ctx, run["id"], rows, export_step["params"].get("format", "xlsx"))
        export_step.update({"status": "done", "result": {"detail": f"exported {len(rows)} rows", "file": files}})
    for step in plan:
        if step["tool"] in ("create_list", "create_opportunities") and step["status"] == "deferred":
            step["status"] = "proposed"
    summary = (f"{len(rows)} companies ranked. " + " ".join(
        f"[{s['id']} {s['tool']}] {s.get('result', {}).get('detail', '')}" for s in plan))[:8000]
    store.update(ctx, "research_runs", run["id"], {
        "status": "completed", "plan": plan, "result_count": len(rows), "summary": summary,
        "proposed_actions": proposals,
        "progress": {"message": "Completed", "files": files, "allow_paid": allow_paid}})
    try:
        platform.service("automation").emit(ctx, "research_completed", f"research:{run['id']}",
                                            {"run_id": run["id"], "result_count": len(rows)})
    except Exception:  # noqa: BLE001 - emitting is best-effort
        log.debug("automation emit skipped", exc_info=True)
    return {"run_id": run["id"], "results": len(rows), "proposed_actions": len(proposals)}
