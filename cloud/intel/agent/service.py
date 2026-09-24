"""The AI Control Room orchestrator.

    intent detection → planning → tool selection → (approval) → execution → validation → synthesis

    agent = platform.service("agent")
    turn = agent.ask(ctx, "Find 500 US manufacturing companies with SAP hiring ...", execute=False)  # Research
    run = turn["run"]            # plan, per-step risk/approval/credits, estimate — nothing has run
    agent.run(ctx, run["id"])    # Run: safe steps execute now; high-impact steps wait
    agent.decide(ctx, approval_id, approve=True)   # one approval → that step runs
    agent.ask(ctx, "Remove companies already in our CRM", session_id=...)   # follow-up on the results

Rules the orchestrator enforces (the model cannot change them):

* **Approval.** read/compute/export run automatically; ``mutate``/``send``/
  ``destructive`` steps and the paid part of ``paid`` steps wait for an explicit
  approval by a user whose role the tool allows; ``background``/``config`` steps
  wait when they exceed their bulk limit. Nothing high-impact runs silently.
* **Credits.** Every plan carries a per-provider estimate with the reason. An
  approval places a *hold* on the ledger (proving the credits exist and are
  within limits); just before the paid call the hold is released so the provider
  service reserves the actual amount per call; if the step fails, the hold is
  released (rollback). Actual usage is read back from the ledger.
* **Idempotency and recovery.** Steps that finished are never re-run; the
  working set is persisted after every step, so a crashed worker's retry resumes
  where it stopped. Mutating tools also remember what they created.
* **Audit.** The request, plan, every tool call (parameters with secrets
  redacted), credits, results, mutations, approvals, user and errors.
* **Untrusted content.** Planning sees only the user's words, workspace memory
  and the tool catalogue — never tool output or scraped pages.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from cloud.intel.agent import ai_assist, planner
from cloud.intel.agent.memory import MemoryService, looks_secret
from cloud.intel.agent.state import WorkingSet
from cloud.intel.agent.tools import MODES, TOOLS, ToolCall, results_snapshot
from cloud.intel.ai.base import validate_against_schema
from cloud.intel.ai.registry import FREE_QUOTA_EXHAUSTED
from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, ForbiddenError, NotFoundError, ValidationError, utcnow

__all__ = ["AgentService", "run_agent_task", "redact_params"]

log = logging.getLogger(__name__)

_SECRET_KEYS = re.compile(r"secret|token|password|passwd|api[_-]?key|authorization|credential", re.I)
_PAID_CONTACT_PROVIDERS = ("seamless", "zoominfo")
#: Runs with only these risks and at most this many companies execute inside the request.
_INLINE_RISKS = frozenset({"read", "compute", "export"})
_INLINE_MAX_COMPANIES = 5000


def redact_params(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[redacted]" if _SECRET_KEYS.search(str(k)) else redact_params(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_params(v) for v in value]
    if isinstance(value, str) and looks_secret(value) and len(value) > 24:
        return "[redacted]"
    return value


class AgentService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.memory = MemoryService(platform)

    @property
    def store(self):
        return self.platform.store

    # ------------------------------------------------------------------------------------
    # sessions and chat
    # ------------------------------------------------------------------------------------

    def create_session(self, ctx: Ctx, *, title: str = "New conversation", mode: str = "auto") -> Dict[str, Any]:
        ctx.require_write()
        if mode not in MODES:
            raise ValidationError(f"unknown agent mode {mode!r}")
        return self.store.insert(ctx, "agent_sessions", {"title": title[:300] or "New conversation", "mode": mode})

    def _message(self, ctx: Ctx, session_id: str, role: str, content: str, *, run_id: Optional[str] = None,
                 data: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        return self.store.insert(ctx, "agent_messages", {"session_id": session_id, "role": role,
                                                         "content": content[:20000] or "…", "run_id": run_id,
                                                         "data": dict(data or {})})

    def ask(self, ctx: Ctx, text: str, *, session_id: Optional[str] = None, mode: str = "auto",
            execute: bool = False) -> Dict[str, Any]:
        """One turn: understand the message, plan, optionally run, and answer."""
        ctx.require_write()
        text = (text or "").strip()
        if len(text) < 2:
            raise ValidationError("tell me what you want me to do")
        if len(text) > 8000:
            raise ValidationError("the request is too long (8,000 characters at most)")
        session = (self.store.get(ctx, "agent_sessions", session_id) if session_id
                   else self.create_session(ctx, title=text[:120], mode=mode))
        mode = mode if mode != "auto" or not session_id else session["mode"]
        self._message(ctx, session["id"], "user", text)
        has_results = bool((session.get("working_set") or {}).get("companies") or
                           any((session.get("working_set") or {}).get("ids", {}).values()))
        profile = self.memory.profile(ctx)
        understood = planner.understand(text, profile, has_results=has_results)
        if session.get("last_run_id") and planner._RUN_IT.match(text):
            understood = {"kind": "run_it", "text": text, "steps": [], "aliases_applied": []}
        elif has_results and understood["kind"] == "research":
            # The rules did not recognise a follow-up; the model may, as validated tool calls on the results.
            ai = self.platform.service("ai").for_ctx(ctx, "follow_up_understanding")
            if ai.external:
                context = ai_assist.build_context(self.platform, ctx, text=text, profile=profile, mode=mode,
                                                  working_set=session.get("working_set"))
                steps = ai_assist.follow_up(ai, context)
                if steps:
                    understood = {"kind": "follow_up", "text": text, "steps": steps, "aliases_applied": [],
                                  "ai_follow_up": ai.name}

        if understood["kind"] == "memory":
            row = self.memory.remember(ctx, text)
            reply = f"Saved to workspace memory ({row['kind'].replace('_', ' ')}: {row['key']})."
            if row["kind"] == "alias":
                reply = f"Got it — whenever you say {row['key'].upper()}, I'll include {', '.join(row['value']['expands_to'])}."
            msg = self._message(ctx, session["id"], "assistant", reply, data={"memory_id": row["id"]})
            return {"session": session, "run": None, "message": msg, "memory": row}

        if understood["kind"] == "run_it":
            run = self.store.get(ctx, "agent_runs", session["last_run_id"]) if session.get("last_run_id") else None
            if run is None:
                raise ValidationError("there is nothing to run yet")
            if run["status"] == "planned":
                run = self.run(ctx, run["id"])
            else:
                approved = self.approve_all(ctx, run["id"])
                run = self.store.get(ctx, "agent_runs", run["id"])
                if not approved:
                    msg = self._message(ctx, session["id"], "assistant",
                                        "Nothing is waiting for approval on the last run.", run_id=run["id"])
                    return {"session": session, "run": run, "message": msg}
            msg = self._message(ctx, session["id"], "assistant", self._narrate(ctx, run), run_id=run["id"])
            return {"session": self.store.get(ctx, "agent_sessions", session["id"]), "run": run, "message": msg}

        run = self._create_run(ctx, text, mode, understood, profile, session=session)
        session = self.store.update(ctx, "agent_sessions", session["id"], {"last_run_id": run["id"]})
        if execute or understood["kind"] in ("follow_up", "crm_query"):
            run = self.run(ctx, run["id"])
        msg = self._message(ctx, session["id"], "assistant", self._narrate(ctx, run), run_id=run["id"])
        return {"session": self.store.get(ctx, "agent_sessions", session["id"]), "run": run, "message": msg}

    # ------------------------------------------------------------------------------------
    # planning
    # ------------------------------------------------------------------------------------

    def plan(self, ctx: Ctx, request: str, *, mode: str = "auto", session_id: Optional[str] = None) -> Dict[str, Any]:
        """The Research button: understand and plan, run nothing."""
        return self.ask(ctx, request, session_id=session_id, mode=mode, execute=False)["run"]

    def _create_run(self, ctx: Ctx, text: str, mode: str, understood: Dict[str, Any], profile: Dict[str, Any],
                    *, session: Mapping[str, Any]) -> Dict[str, Any]:
        if mode not in MODES:
            raise ValidationError(f"unknown agent mode {mode!r}")
        allowed = {name for name, t in TOOLS.items() if mode in t.modes}
        steps = list(understood["steps"])
        planner_name = "rules"
        registry = self.platform.service("ai")
        ai_info: Dict[str, Any] = {"used": [], "reason": None}
        if understood["kind"] in ("research", "monitor", "crm_query"):
            if understood.get("intent") is None:
                from cloud.intel.research.intent import parse_intent

                understood["intent"] = parse_intent(text)
            context = ai_assist.build_context(self.platform, ctx, text=text, profile=profile, mode=mode,
                                              working_set=session.get("working_set"), intent=understood.get("intent"))
            interpreter = registry.for_ctx(ctx, "intent_interpretation")
            if interpreter.external and understood.get("intent") is not None:
                refined = ai_assist.interpret_intent(interpreter, context, understood["intent"])
                if refined is not None:
                    understood["intent"] = refined
                    has_results = bool((session.get("working_set") or {}).get("companies"))
                    steps = planner.build_steps(refined, text, profile, has_results=has_results)
                    ai_info["used"].append("intent_interpretation")
            ai = registry.for_ctx(ctx, "research_planning")
            if ai.external:
                proposed = ai_assist.plan_steps(ai, context)
                if proposed:
                    steps, planner_name = proposed, f"ai:{ai.name}"
                    ai_info["used"].append("research_planning")
                elif (getattr(ai, "last_error", None) or "").startswith(FREE_QUOTA_EXHAUSTED):
                    ai_info["reason"] = FREE_QUOTA_EXHAUSTED   # the quota ran out on this very call
            else:
                ai_info["reason"] = getattr(ai, "reason", "AI provider not configured")
        elif understood.get("ai_follow_up"):
            planner_name = f"ai:{understood['ai_follow_up']}"
            ai_info["used"].append("follow_up_understanding")
        notes = []
        kept = []
        for step in steps:
            name = step["tool"]
            if name not in TOOLS:
                notes.append(f"dropped unknown tool {name}")
                continue
            if name not in allowed:
                notes.append(f"{name} is not available to the {MODES[mode]['title']}")
                continue
            if not TOOLS[name].allowed_for(ctx.role):
                notes.append(f"{name} needs the {TOOLS[name].min_role} role")
                continue
            if profile.get("allowed_providers") is not None and name in ("query_zoominfo", "query_seamless") and \
                    name.split("_")[1] not in profile["allowed_providers"]:
                notes.append(f"{name} skipped: not in the workspace's allowed providers")
                continue
            kept.append(step)
        if not kept:
            raise ValidationError("I couldn't turn that into steps I'm allowed to run. " + "; ".join(notes))
        run = self.store.insert(ctx, "agent_runs", {
            "session_id": session["id"], "request": text[:8000], "mode": mode, "status": "planned",
            "planner": planner_name,
            "intent": {"kind": understood["kind"], "parsed": understood.get("intent") or {},
                       "aliases_applied": understood.get("aliases_applied") or [], "notes": notes,
                       "requested_by": ctx.user_id, "role": ctx.role, "ai": ai_info},
            "progress": {"lines": [], "message": "Planned — review the plan, then run"}})
        base = session.get("working_set") if understood["kind"] in ("follow_up",) or (
            understood["kind"] == "research" and planner._REFERS_TO_RESULTS.search(text)) else None
        estimate = self._materialise_steps(ctx, run, kept, base)
        explainer = registry.for_ctx(ctx, "plan_explanation", run_id=run["id"])
        if explainer.external:
            explanation = ai_assist.explain_plan(explainer, text, self._plan_view(ctx, run["id"]), estimate)
            if explanation:
                estimate["ai_explanation"] = explanation
                ai_info["used"].append("plan_explanation")
        run = self.store.update(ctx, "agent_runs", run["id"], {
            "estimate": estimate, "plan": self._plan_view(ctx, run["id"]),
            "intent": {**run["intent"], "ai": ai_info}, "result": {"state": base or {}}})
        audit(self.store, ctx, "agent.plan", entity_type="agent_runs", entity_id=run["id"], summary=text[:500],
              changes={"planner": planner_name, "steps": [s["tool"] for s in kept], "estimate": estimate.get("credits"),
                       "ai_used": ai_info["used"]})
        return run

    def _materialise_steps(self, ctx: Ctx, run: Mapping[str, Any], steps: List[Dict[str, Any]],
                           base: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Estimate counts by a dry read-only pass, then store steps with risk, approval and credit estimate."""
        sys = ctx.as_system("agent")
        counts = self._dry_counts(ctx, steps, base)
        credits: Dict[str, float] = {}
        explain: List[str] = []
        for position, step in enumerate(steps):
            tool = TOOLS[step["tool"]]
            estimate = tool.estimate(self.platform, step["params"], counts)
            estimate["credits"] = self._map_credit_kinds(ctx, estimate.get("credits") or {})
            for provider, amount in estimate["credits"].items():
                credits[provider] = credits.get(provider, 0) + amount
            if estimate.get("explain"):
                explain.append(estimate["explain"])
            needs = tool.needs_approval(estimate)
            row = self.store.insert(sys, "agent_steps", {
                "run_id": run["id"], "position": position, "tool": tool.name, "params": redact_params(step["params"]),
                "risk": tool.risk, "status": "planned", "requires_approval": needs,
                "estimate": {**estimate, "title": step.get("title"), "why": step.get("why"),
                             "raw_params": step["params"] if not _contains_secret(step["params"]) else {}},
                "idempotency_key": f"{run['id']}:{position}"})
            if needs:
                self.store.insert(sys, "agent_approvals", {
                    "run_id": run["id"], "step_id": row["id"], "risk": tool.risk,
                    "action": _action_label(tool.name, step, estimate),
                    "reason": estimate.get("explain") or step.get("why") or tool.description,
                    "impact": {"affected": estimate.get("affected"), "tool": tool.name},
                    "credits": estimate["credits"], "status": "pending"})
        return {"counts": counts, "credits": credits, "explain": explain,
                "expected": _expected_text(counts, credits),
                "note": "Estimates come from a read-only pass over your data; paid providers are only used after "
                        "you approve, and only for what internal data and free sources could not fill."}

    def _dry_counts(self, ctx: Ctx, steps: List[Dict[str, Any]], base: Optional[Dict[str, Any]]) -> Dict[str, int]:
        ws = WorkingSet.restore(self.platform, ctx, base)
        call = ToolCall(self.platform, ctx, ws, allow_paid=False)
        dry = {"search_companies", "search_contacts", "search_jobs", "search_signals", "search_opportunities",
               "search_changes", "exclude_crm_accounts", "contact_gaps", "match_companies"}
        for step in steps:
            name = step["tool"]
            params = dict(step["params"])
            if name == "run_hiring_intelligence":
                params["detect"] = False
            elif name == "find_contacts":
                TOOLS["contact_gaps"].fn(call, {"functions": params.get("functions"),
                                                "seniorities": params.get("seniorities")})
                continue
            elif name not in dry:
                continue
            try:
                TOOLS[name].fn(call, params)
            except Exception:  # noqa: BLE001 - an estimate must never block planning
                log.debug("dry count failed for %s", name, exc_info=True)
        counts = ws.counts()
        counts["missing_contacts"] = int(ws.facts.get("missing_contacts") or 0) if "missing_contacts" in ws.facts else None
        jobs = 0
        emails = 0
        for cid in ws.company_ids[:3000]:
            jobs += self.store.count(ctx, "job_postings", {"company_id": cid, "status": "open"})
        contact_rows = []
        for cid in ws.company_ids[:3000]:
            contact_rows += ws.research.extra(cid).get("contacts") or self.store.all(
                ctx, "contacts", {"company_id": cid, "status": "active"}, cap=300)
        for contact in contact_rows:
            email = (contact.get("email") or "").lower()
            if email and contact.get("email_status") in (None, "UNVERIFIED", "UNKNOWN", "RISKY") and \
                    not self.store.first(ctx, "email_validations", {"email": email}):
                emails += 1
        counts["jobs"] = counts.get("jobs") or jobs
        counts["emails_unvalidated"] = emails
        return {k: v for k, v in counts.items() if v is not None}

    def _map_credit_kinds(self, ctx: Ctx, credits: Mapping[str, float]) -> Dict[str, float]:
        """'contact_enrichment' becomes the provider that would actually be used (by workspace priority)."""
        out: Dict[str, float] = {}
        for kind, amount in credits.items():
            if not amount:
                continue
            provider = kind
            if kind == "contact_enrichment":
                provider = self._contact_provider(ctx) or "contact_enrichment (no paid provider connected)"
            out[provider] = out.get(provider, 0) + float(amount)
        return out

    def _contact_provider(self, ctx: Ctx) -> Optional[str]:
        profile = self.memory.profile(ctx)
        order = [p for p in (profile.get("source_priority") or []) if p in _PAID_CONTACT_PROVIDERS]
        order += [p for p in _PAID_CONTACT_PROVIDERS if p not in order]
        allowed = profile.get("allowed_providers")
        try:
            registry = self.platform.service("providers")
        except Exception:  # noqa: BLE001
            return None
        for provider in order:
            if allowed is not None and provider not in allowed:
                continue
            try:
                if registry.configured(ctx, provider):
                    return provider
            except Exception:  # noqa: BLE001
                continue
        return None

    def edit_plan(self, ctx: Ctx, run_id: str, steps: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """EDIT PLAN: replace the steps of a planned run (validated, re-estimated, approvals rebuilt)."""
        ctx.require_write()
        run = self.store.get(ctx, "agent_runs", run_id)
        if run["status"] != "planned":
            raise ConflictError("only a planned run can be edited")
        cleaned = []
        for step in steps:
            name = step.get("tool")
            if name not in TOOLS:
                raise ValidationError(f"unknown tool {name!r}")
            if not TOOLS[name].allowed_for(ctx.role) or run["mode"] not in TOOLS[name].modes:
                raise ForbiddenError(f"{name} is not available here")
            params = {k: v for k, v in dict(step.get("params") or {}).items() if v is not None}
            problems = validate_against_schema(params, TOOLS[name].schema)
            if problems:
                raise ValidationError(f"{name}: {'; '.join(problems)}")
            cleaned.append({"tool": name, "params": params, "title": str(step.get("title") or name)[:200],
                            "why": "edited by user"})
        if not cleaned:
            raise ValidationError("a plan needs at least one step")
        sys = ctx.as_system("agent")
        for old in self.store.all(sys, "agent_approvals", {"run_id": run_id}):
            self.store.update(sys, "agent_approvals", old["id"], {"status": "expired"})
        for old in self.store.all(sys, "agent_steps", {"run_id": run_id}):
            self.store.delete(sys, "agent_steps", old["id"])
        estimate = self._materialise_steps(ctx, run, cleaned, (run.get("result") or {}).get("state"))
        run = self.store.update(ctx, "agent_runs", run_id, {"estimate": estimate, "plan": self._plan_view(ctx, run_id)})
        audit(self.store, ctx, "agent.plan_edit", entity_type="agent_runs", entity_id=run_id,
              changes={"steps": [s["tool"] for s in cleaned]})
        return run

    def _plan_view(self, ctx: Ctx, run_id: str) -> List[Dict[str, Any]]:
        steps = self.store.all(ctx, "agent_steps", {"run_id": run_id}, order="position")
        return [{"id": s["id"], "position": s["position"], "tool": s["tool"], "title": s["estimate"].get("title"),
                 "why": s["estimate"].get("why"), "risk": s["risk"], "requires_approval": s["requires_approval"],
                 "status": s["status"], "credits": s["estimate"].get("credits") or {},
                 "affected": s["estimate"].get("affected"), "explain": s["estimate"].get("explain"),
                 "detail": (s.get("output") or {}).get("detail"), "count": (s.get("output") or {}).get("count"),
                 "params": s["params"]} for s in steps]

    # ------------------------------------------------------------------------------------
    # running, approvals, cancel
    # ------------------------------------------------------------------------------------

    def run(self, ctx: Ctx, run_id: str, *, background: Optional[bool] = None) -> Dict[str, Any]:
        """The Run button. Safe steps execute; high-impact steps wait for approval."""
        ctx.require_write()
        run = self.store.get(ctx, "agent_runs", run_id)
        if run["status"] not in ("planned", "awaiting_approval", "failed"):
            raise ConflictError(f"a {run['status']} run cannot be started")
        run = self.store.update(ctx, "agent_runs", run_id, {"status": "running",
                                                            "progress": {**run["progress"], "message": "Running"}})
        audit(self.store, ctx, "agent.run", entity_type="agent_runs", entity_id=run_id)
        return self._dispatch(ctx, run, background)

    def _dispatch(self, ctx: Ctx, run: Mapping[str, Any], background: Optional[bool]) -> Dict[str, Any]:
        steps = self.store.all(ctx, "agent_steps", {"run_id": run["id"]}, order="position")
        approved = {a["step_id"] for a in self.store.all(ctx, "agent_approvals", {"run_id": run["id"],
                                                                                  "status": "approved"})}
        # Only steps that will actually execute now decide where the run goes. Steps still
        # waiting for approval are just marked, so they never force the background queue.
        to_execute = [s for s in steps if s["status"] not in ("done", "skipped", "rejected")
                      and (not s["requires_approval"] or s["id"] in approved)]
        heavy = [s for s in to_execute if s["risk"] in ("background", "config")
                 or (s["risk"] == "paid" and s["id"] in approved)]
        light = not heavy and (run["estimate"].get("counts") or {}).get("companies", 0) <= _INLINE_MAX_COMPANIES
        if background is None:
            background = not light
        if not background:
            execute_run(self.platform, ctx.as_system("agent"), run["id"])
            return self.store.get(ctx, "agent_runs", run["id"])
        task = self.platform.tasks.submit(ctx, "research", {"agent_run_id": run["id"]}, entity_type="agent_runs",
                                          entity_id=run["id"], idempotency_key=f"agent:{run['id']}:{run['version']}")
        return self.store.update(ctx, "agent_runs", run["id"], {"task_id": task["id"]})

    def approvals(self, ctx: Ctx, run_id: Optional[str] = None, status: str = "pending") -> List[Dict[str, Any]]:
        filters: Dict[str, Any] = {"status": status}
        if run_id:
            filters["run_id"] = run_id
        return self.store.all(ctx, "agent_approvals", filters, cap=500)

    def decide(self, ctx: Ctx, approval_id: str, *, approve: bool, background: Optional[bool] = None
               ) -> Dict[str, Any]:
        """APPROVE / REJECT one proposed action. Approval places the credit hold and runs the step."""
        ctx.require_write()
        sys = ctx.as_system("agent")
        approval = self.store.get(ctx, "agent_approvals", approval_id)
        if approval["status"] != "pending":
            raise ConflictError(f"this approval is already {approval['status']}")
        step = self.store.get(ctx, "agent_steps", approval["step_id"])
        tool = TOOLS[step["tool"]]
        if not tool.allowed_for(ctx.role):
            raise ForbiddenError(f"approving {tool.name} needs the {tool.min_role} role")
        run = self.store.get(ctx, "agent_runs", approval["run_id"])
        if run["status"] in ("cancelled",):
            raise ConflictError("the run was cancelled")
        if not approve:
            self.store.update(sys, "agent_approvals", approval_id, {"status": "rejected", "decided_by": ctx.user_id,
                                                                    "decided_at": utcnow()})
            self.store.update(sys, "agent_steps", step["id"], {"status": "rejected"})
            audit(self.store, ctx, "agent.reject", entity_type="agent_steps", entity_id=step["id"],
                  summary=approval["action"])
            self._refresh(ctx, run["id"])
            return self.store.get(ctx, "agent_runs", run["id"])
        holds = []
        ledger = self.platform.service("credits")
        try:
            for provider, amount in (approval.get("credits") or {}).items():
                if amount and not provider.startswith("contact_enrichment"):
                    holds.append(ledger.reserve(ctx, provider, amount, reason=f"hold for approved step: {approval['action']}"[:500],
                                                idempotency_key=f"agent-hold:{approval_id}:{provider}",
                                                action=f"agent:{tool.name}")["id"])
        except Exception as error:  # noqa: BLE001 - e.g. CreditError: not enough credits / not synced / over limit
            for hold in holds:
                ledger.release(ctx, hold, reason="approval could not reserve every provider")
            raise ConflictError(f"cannot approve: {error}") from error
        self.store.update(sys, "agent_approvals", approval_id, {"status": "approved", "decided_by": ctx.user_id,
                                                                "decided_at": utcnow()})
        self.store.update(sys, "agent_steps", step["id"], {"status": "planned", "reservation_ids": holds,
                                                           "estimate": {**step["estimate"], "approved_by": ctx.user_id}})
        # Anything computed after this step is recomputed with its output.
        for later in self.store.all(sys, "agent_steps", {"run_id": run["id"], "position__gt": step["position"]}):
            if later["status"] == "done" and later["risk"] in ("compute", "export"):
                self.store.update(sys, "agent_steps", later["id"], {"status": "planned"})
        audit(self.store, ctx, "agent.approve", entity_type="agent_steps", entity_id=step["id"],
              summary=approval["action"], changes={"credits": approval.get("credits"), "holds": holds})
        run = self.store.update(ctx, "agent_runs", run["id"], {"status": "running"})
        return self._dispatch(ctx, run, background)

    def approve_all(self, ctx: Ctx, run_id: str) -> List[str]:
        """Approve every pending, non-destructive action of a run ("Run it")."""
        done = []
        for approval in self.approvals(ctx, run_id):
            if approval["risk"] == "destructive":
                continue
            self.decide(ctx, approval["id"], approve=True, background=False)
            done.append(approval["id"])
        return done

    def cancel(self, ctx: Ctx, run_id: str) -> Dict[str, Any]:
        ctx.require_write()
        run = self.store.get(ctx, "agent_runs", run_id)
        if run["status"] in ("completed", "cancelled"):
            raise ConflictError(f"the run is already {run['status']}")
        sys = ctx.as_system("agent")
        ledger = self.platform.service("credits")
        for step in self.store.all(sys, "agent_steps", {"run_id": run_id}):
            for hold in step.get("reservation_ids") or []:
                try:
                    ledger.release(ctx, hold, reason="run cancelled")
                except Exception:  # noqa: BLE001
                    pass
        for approval in self.approvals(ctx, run_id):
            self.store.update(sys, "agent_approvals", approval["id"], {"status": "expired"})
        if run.get("task_id"):
            try:
                self.platform.tasks.cancel(ctx, run["task_id"])
            except Exception:  # noqa: BLE001 - already finished
                pass
        audit(self.store, ctx, "agent.cancel", entity_type="agent_runs", entity_id=run_id)
        return self.store.update(ctx, "agent_runs", run_id, {"status": "cancelled"})

    def _refresh(self, ctx: Ctx, run_id: str) -> None:
        pending = self.approvals(ctx, run_id)
        steps = self.store.all(ctx, "agent_steps", {"run_id": run_id})
        status = "awaiting_approval" if pending else (
            "completed" if all(s["status"] in ("done", "skipped", "rejected") for s in steps) else None)
        changes: Dict[str, Any] = {"plan": self._plan_view(ctx, run_id)}
        if status:
            changes["status"] = status
        self.store.update(ctx.as_system("agent"), "agent_runs", run_id, changes)

    _RESULT_ACTIONS = {"add_to_list": "add_to_list", "create_list": "create_list",
                       "create_opportunity": "create_opportunity", "create_task": "create_task",
                       "campaign_proposal": "campaign_proposal", "export": "export_results",
                       "start_monitor": "start_monitor", "validate_email": "validate_email",
                       "find_contacts": "find_contacts"}

    def act_on_results(self, ctx: Ctx, run_id: str, action: str, *, company_ids: Sequence[str] = (),
                       params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """A result-table action on selected rows becomes its own small, approval-gated run."""
        ctx.require_write()
        if action not in self._RESULT_ACTIONS:
            raise ValidationError(f"unknown action {action!r}")
        source = self.store.get(ctx, "agent_runs", run_id)
        state = dict((source.get("result") or {}).get("state") or {})
        chosen = [c for c in (company_ids or state.get("companies") or []) if c in set(state.get("companies") or [])]
        if not chosen:
            raise ValidationError("select at least one company from the results")
        state["companies"] = chosen
        tool = self._RESULT_ACTIONS[action]
        step = {"tool": tool, "params": dict(params or {}), "title": f"{tool.replace('_', ' ').capitalize()} "
                                                                      f"({len(chosen)} selected)", "why": "result action"}
        problems = validate_against_schema(step["params"], TOOLS[tool].schema)
        if problems:
            raise ValidationError("; ".join(problems))
        session = self.store.get(ctx, "agent_sessions", source["session_id"]) if source.get("session_id") else             self.create_session(ctx, title=f"Actions on {run_id}")
        understood = {"kind": "follow_up", "steps": [step], "aliases_applied": []}
        run = self.store.insert(ctx, "agent_runs", {
            "session_id": session["id"], "request": f"{action} on {len(chosen)} results of {run_id}", "mode": "auto",
            "status": "planned", "planner": "result_action",
            "intent": {"kind": "result_action", "source_run": run_id, "requested_by": ctx.user_id, "role": ctx.role},
            "progress": {"lines": [], "message": "Planned"}})
        estimate = self._materialise_steps(ctx, run, understood["steps"], state)
        run = self.store.update(ctx, "agent_runs", run["id"], {"estimate": estimate, "plan": self._plan_view(ctx, run["id"]),
                                                               "result": {"state": state}})
        return self.run(ctx, run["id"], background=False)

    # ------------------------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------------------------

    def results(self, ctx: Ctx, run_id: str, *, view: str = "companies", limit: int = 100, offset: int = 0,
                order: Optional[str] = None) -> Dict[str, Any]:
        run = self.store.get(ctx, "agent_runs", run_id)
        if view == "companies":
            page = self.store.list(ctx, "agent_results", {"run_id": run_id, "entity_type": "companies"},
                                   order=order or "rank", limit=limit, offset=offset)
            return {"items": page.rows, "total": page.total}
        ids = ((run.get("result") or {}).get("state") or {}).get("ids", {}).get(
            {"jobs": "jobs", "contacts": "contacts", "signals": "signals", "opportunities": "opportunities",
             "lists": "lists"}.get(view, ""), [])
        table = {"jobs": "job_postings", "contacts": "contacts", "signals": "hiring_signals",
                 "opportunities": "opportunities", "lists": "lists"}.get(view)
        if view == "evidence":
            rows = []
            for row in self.store.all(ctx, "agent_results", {"run_id": run_id}, order="rank", cap=500):
                rows += [{"company": row["title"], "company_id": row["entity_id"], **e} for e in row["evidence"]]
            return {"items": rows[offset:offset + limit], "total": len(rows)}
        if view == "sources":
            steps = self.store.all(ctx, "agent_steps", {"run_id": run_id}, order="position")
            items = [{"tool": s["tool"], "status": s["status"], "risk": s["risk"],
                      "detail": (s.get("output") or {}).get("detail"),
                      "credits": (s.get("output") or {}).get("credits_used") or {}} for s in steps]
            return {"items": items, "total": len(items)}
        if not table:
            raise ValidationError(f"unknown result view {view!r}")
        chosen = ids[offset:offset + limit]
        rows = [r for r in (self.store.find(ctx, table, i) for i in chosen) if r]
        return {"items": rows, "total": len(ids)}

    def trail(self, ctx: Ctx, run_id: str) -> Dict[str, Any]:
        """Everything that happened in one run, for the execution-history inspector."""
        run = self.store.get(ctx, "agent_runs", run_id)
        return {
            "run": run,
            "steps": self.store.all(ctx, "agent_steps", {"run_id": run_id}, order="position"),
            "approvals": self.store.all(ctx, "agent_approvals", {"run_id": run_id}),
            "audit": self.store.all(ctx, "audit_log", {"entity_id": run_id}, cap=500) +
            [a for s in self.store.all(ctx, "agent_steps", {"run_id": run_id})
             for a in self.store.all(ctx, "audit_log", {"entity_id": s["id"]}, cap=100)],
            "credits": self.store.all(ctx, "credit_ledger", {"action__ilike": "agent:"}, cap=500),
        }

    def _narrate(self, ctx: Ctx, run: Mapping[str, Any]) -> str:
        run = self.store.get(ctx, "agent_runs", run["id"])
        lines = []
        if run["status"] == "planned":
            est = run.get("estimate") or {}
            lines.append("Here's the plan. Nothing has run yet.")
            for step in run["plan"]:
                flag = " — needs approval" if step["requires_approval"] else ""
                lines.append(f"• {step['title']}{flag}")
            lines.append(f"Estimated: {est.get('expected', '')}")
            if est.get("explain"):
                lines += [f"  {e}" for e in est["explain"] if e]
            if est.get("ai_explanation"):
                lines.append(f"In plain words: {est['ai_explanation']}")
            ai = (run.get("intent") or {}).get("ai") or {}
            if not ai.get("used") and ai.get("reason"):
                lines.append(f"(Planned with rules — {ai['reason']}.)")
            return "\n".join(lines)
        for line in (run.get("progress") or {}).get("lines") or []:
            lines.append(("✓ " if line.get("ok", True) else "⏳ ") + line["text"])
        pending = self.approvals(ctx, run["id"])
        if pending:
            lines.append(f"{len(pending)} action(s) need your approval:")
            for approval in pending:
                credits = ", ".join(f"{v:g} {k}" for k, v in (approval.get("credits") or {}).items())
                lines.append(f"• {approval['action']}" + (f" ({credits} credits)" if credits else ""))
        if run.get("summary"):
            lines.append(run["summary"])
        return "\n".join(lines) or "Done."


# ----------------------------------------------------------------------------------------
# execution (inline or in the worker)
# ----------------------------------------------------------------------------------------

def _contains_secret(params: Any) -> bool:
    import json

    return looks_secret(json.dumps(params, default=str)) if params else False


def _action_label(tool: str, step: Mapping[str, Any], estimate: Mapping[str, Any]) -> str:
    n = estimate.get("affected") or 0
    labels = {
        "create_opportunity": f"Create {n} opportunities",
        "create_list": f"Create list '{(step['params'].get('name') or 'results')[:80]}' with {n} records",
        "find_contacts": f"Find missing contacts for {n} companies (may add contacts)",
        "validate_email": f"Validate {n} email addresses with the paid provider",
        "create_task": f"Create {max(n, 1)} tasks",
        "create_note": f"Add {n} notes",
        "enroll_in_sequence": f"Enrol {n} contacts in a sequence (pending approval there; nothing is sent)",
        "merge_companies": f"Merge {n} companies",
        "start_monitor": f"Monitor {n} companies",
        "run_career_crawler": f"Crawl careers pages of {n} companies",
    }
    return labels.get(tool, f"{step.get('title') or tool} ({n} records)")[:300]


def _expected_text(counts: Mapping[str, int], credits: Mapping[str, float]) -> str:
    parts = [f"~{counts.get('companies', 0):,} companies"]
    if counts.get("jobs"):
        parts.append(f"~{counts['jobs']:,} jobs")
    if counts.get("contacts"):
        parts.append(f"~{counts['contacts']:,} contacts")
    if counts.get("missing_contacts") is not None and "missing_contacts" in counts:
        parts.append(f"{counts['missing_contacts']:,} contact gaps")
    paid = sum(v for v in credits.values())
    parts.append(f"paid credits: {paid:g} (upper bound, only after approval)" if paid else "paid credits: 0")
    return ", ".join(parts)


def _credits_used(platform: Any, ctx: Ctx, since) -> Dict[str, float]:
    used: Dict[str, float] = {}
    for entry in platform.store.all(ctx, "credit_ledger", {"entry_type": "consume", "created_at__gte": since}, cap=5000):
        used[entry["provider"]] = used.get(entry["provider"], 0) + float(entry["amount"])
    return used


def execute_run(platform: Any, ctx: Ctx, run_id: str, reporter: Any = None) -> Dict[str, Any]:
    """Run every runnable step of an agent run in order, resuming after completed steps."""
    from cloud.intel.tasks.worker import TaskCancelled

    store = platform.store
    run = store.get(ctx, "agent_runs", run_id)
    if run["status"] == "cancelled":
        return run
    approvals = {a["step_id"]: a for a in store.all(ctx, "agent_approvals", {"run_id": run_id})}
    steps = store.all(ctx, "agent_steps", {"run_id": run_id}, order="position")
    state = (run.get("result") or {}).get("state") or {}
    requester = (run.get("intent") or {}).get("requested_by")
    exec_ctx = Ctx.for_system(ctx.workspace_id, actor_kind="agent", user_id=requester,
                              ai_external_allowed=ctx.ai_external_allowed)
    ws = WorkingSet.restore(platform, exec_ctx, state)
    lines = list((run.get("progress") or {}).get("lines") or [])
    ledger = platform.service("credits")
    store.update(ctx, "agent_runs", run_id, {"status": "running"})
    for step in steps:
        if step["status"] in ("done", "skipped", "rejected"):
            continue
        if reporter is not None and reporter.is_cancelled():
            raise TaskCancelled()
        if store.get(ctx, "agent_runs", run_id)["status"] == "cancelled":
            return store.get(ctx, "agent_runs", run_id)
        tool = TOOLS[step["tool"]]
        approval = approvals.get(step["id"])
        approved = approval is not None and approval["status"] == "approved"
        params = dict(step["estimate"].get("raw_params") or step["params"])
        free_only = False
        if step["requires_approval"] and not approved:
            if tool.risk == "paid" and tool.free_mode:
                free_only = True
            else:
                store.update(ctx, "agent_steps", step["id"], {"status": "awaiting_approval"})
                lines.append({"ok": False, "text": f"{step['estimate'].get('title') or tool.name}: waiting for approval"})
                continue
        started = utcnow()
        clock = time.monotonic()
        store.update(ctx, "agent_steps", step["id"], {"status": "running", "started_at": started})
        if reporter is not None:
            reporter.progress(f"{tool.name}", step=step["position"])
        allow_paid = approved and tool.risk in ("paid", "mutate") and not free_only
        call = ToolCall(platform, exec_ctx, ws, allow_paid=allow_paid, run_id=run_id, step_id=step["id"],
                        request={"text": run["request"]})
        for hold in step.get("reservation_ids") or []:
            ledger.release(ctx, hold, reason="hold converted to per-call reservations")
        try:
            problems = validate_against_schema(params, tool.schema)
            if problems:
                raise ValidationError("; ".join(problems))
            output = tool.fn(call, params)
        except Exception as error:  # noqa: BLE001 - record, then let the task retry transient failures
            store.update(ctx, "agent_steps", step["id"], {
                "status": "failed", "error": f"{type(error).__name__}: {error}"[:4000], "finished_at": utcnow(),
                "duration_ms": (time.monotonic() - clock) * 1000})
            lines.append({"ok": False, "text": f"{step['estimate'].get('title') or tool.name} failed: {error}"[:300]})
            store.update(ctx, "agent_runs", run_id, {"result": {"state": ws.dump()}, "error": str(error)[:4000],
                                                     "progress": {"lines": lines, "message": "Failed"}})
            audit(store, exec_ctx, f"agent.tool.{tool.name}", entity_type="agent_steps", entity_id=step["id"],
                  summary=f"failed: {error}"[:1000], changes={"params": redact_params(params)})
            if isinstance(error, (ValidationError, ForbiddenError, NotFoundError)):
                store.update(ctx, "agent_runs", run_id, {"status": "failed"})
                return store.get(ctx, "agent_runs", run_id)
            store.update(ctx, "agent_runs", run_id, {"status": "failed"})
            raise
        used = _credits_used(platform, ctx, started) if allow_paid else {}
        status = "awaiting_approval" if free_only else ("done" if output.get("status", "done") in ("done", "deferred")
                                                         else output.get("status", "done"))
        if status not in ("done", "failed", "skipped", "awaiting_approval"):
            status = "done"
        output = {**{k: v for k, v in output.items() if k != "rows"}, "credits_used": used,
                  "rows": (output.get("rows") or [])[:50], "free_only": free_only}
        store.update(ctx, "agent_steps", step["id"], {
            "status": status, "output": output, "finished_at": utcnow(), "duration_ms": (time.monotonic() - clock) * 1000})
        text = f"{step['estimate'].get('title') or tool.name}: {output.get('detail', '')}"
        if free_only:
            text += " — free checks done; the paid part is waiting for approval"
        lines.append({"ok": status == "done", "text": text[:500]})
        store.update(ctx, "agent_runs", run_id, {"result": {"state": ws.dump()},
                                                 "progress": {"lines": lines, "message": text[:500],
                                                              "counts": ws.counts()}})
        audit(store, exec_ctx, f"agent.tool.{tool.name}", entity_type="agent_steps", entity_id=step["id"],
              summary=text[:1000], changes={"params": redact_params(params), "risk": tool.risk,
                                            "approved": approved, "credits_used": used,
                                            "count": output.get("count")})
    return _synthesise(platform, ctx, run_id, ws, lines)


def _synthesise(platform: Any, ctx: Ctx, run_id: str, ws: WorkingSet, lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    store = platform.store
    for old in store.all(ctx, "agent_results", {"run_id": run_id}, cap=10000):
        store.delete(ctx, "agent_results", old["id"])
    snapshot = results_snapshot(ws)
    for row in snapshot:
        store.insert(ctx, "agent_results", {"run_id": run_id, **row})
    pending = store.count(ctx, "agent_approvals", {"run_id": run_id, "status": "pending"})
    failed = store.count(ctx, "agent_steps", {"run_id": run_id, "status": "failed"})
    counts = ws.counts()
    credits = {}
    for step in store.all(ctx, "agent_steps", {"run_id": run_id}):
        for provider, amount in ((step.get("output") or {}).get("credits_used") or {}).items():
            credits[provider] = credits.get(provider, 0) + amount
    top = snapshot[:3]
    summary = [f"{counts['companies']:,} companies in the results"]
    if counts.get("contacts"):
        summary.append(f"{counts['contacts']:,} contacts")
    if counts.get("jobs"):
        summary.append(f"{counts['jobs']:,} jobs")
    if counts.get("signals"):
        summary.append(f"{counts['signals']:,} hiring signals")
    text = ", ".join(summary) + "."
    if top:
        text += " Top: " + "; ".join(f"{r['title']} ({round(r['score'] or 0)}: " +
                                     ", ".join(c["code"] for c in r["reasons"][:3]) + ")" for r in top)
    text += f" Credits used: {', '.join(f'{v:g} {k}' for k, v in credits.items()) or 'none'}."
    if pending:
        text += f" {pending} action(s) still need approval."
    status = "failed" if failed else ("awaiting_approval" if pending else "completed")
    ai_summary = None
    if snapshot and not failed:
        try:
            ai = platform.service("ai").for_ctx(ctx, "result_summarization", run_id=run_id)
            if ai.external:
                ai_summary = ai_assist.summarize_results(ai, store.get(ctx, "agent_runs", run_id)["request"],
                                                         snapshot, counts)
        except Exception:  # noqa: BLE001 - a summary is optional; the rules summary always exists
            log.exception("AI result summary failed")
    if ai_summary:
        text += f"\nAI summary: {ai_summary}"
    run = store.update(ctx, "agent_runs", run_id, {
        "status": status, "summary": text[:20000],
        "result": {"state": ws.dump(), "counts": counts, "credits_used": credits,
                   "exports": ws.facts.get("exports", []), "ai_summary": ai_summary},
        "progress": {"lines": lines, "message": "Completed" if status == "completed" else text[:500], "counts": counts}})
    session_id = run.get("session_id")
    if session_id:
        store.update(ctx, "agent_sessions", session_id, {"working_set": ws.dump(), "last_run_id": run_id})
    AgentService(platform)._refresh(ctx, run_id)
    return store.get(ctx, "agent_runs", run_id)


def run_agent_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    """Worker handler (dispatched through the ``research`` task kind)."""
    run = execute_run(platform, ctx, task["params"]["agent_run_id"], reporter)
    return {"agent_run_id": run["id"], "status": run["status"]}
