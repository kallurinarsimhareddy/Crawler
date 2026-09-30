"""The automation engine: TRIGGER -> CONDITIONS -> ACTIONS.

A workflow is a row in ``workflows``: one ``trigger`` (see :data:`TRIGGERS`),
a condition tree, and an ordered list of actions. Workflows are created
**disabled**; enabling one is an explicit edit.

**Idempotency.** :meth:`AutomationEngine.emit` creates one ``workflow_runs``
row per ``(workflow_id, event_key)`` — the table's unique index makes a
repeated event a no-op, however many times it is emitted or by how many
processes. The run then executes as a ``workflow`` platform task, so it gets
the task queue's retries, backoff, leases and dead-worker recovery.

**Audit and history.** Every run keeps its input and a per-step result list
(``steps``); every action that changes data is audited as ``actor_kind=workflow``.

**Safety rails.**

* Paid enrichment only when the action says ``allow_paid: true`` *and* the
  workflow's last editor was a workspace admin (recorded in
  ``conditions``-adjacent metadata at save time, see :meth:`save_workflow`).
* ``queue_sequence`` enrolls into ``pending_approval`` only — automation never
  approves an enrollment, so it can never cause an email to be sent.
* ``webhook`` posts only to public http(s) addresses (the platform SSRF guard),
  signs the body with HMAC-SHA256, and has a short timeout.

Condition syntax (JSON)::

    {"all": [ {"field": "company.industry", "op": "eq", "value": "Manufacturing"},
              {"any": [ {"field": "signal.signal_type", "op": "in", "value": ["HIRING_SPIKE"]},
                        {"field": "payload.score", "op": "gte", "value": 70} ]} ]}

A bare list is treated as ``{"all": [...]}``. Operators: eq, ne, gt, gte, lt,
lte, contains, in, exists.

**Graphs (advanced workflows).** A workflow whose ``graph`` has ``nodes`` runs
as a graph instead of the flat list (see :mod:`cloud.intel.automation.graph`):
condition (if/else) branches, delays (the run waits with ``resume_at``; the
worker's maintenance ``tick`` resumes it), approval steps (the run waits in
``awaiting_approval`` until a signed-in writer approves or rejects it), per-step
retries with backoff (``retry_policy``) and a failure policy (stop / continue /
retry). Every step is appended to the run's ``history``.

**CRM changes are proposed, not applied.** ``update_company``,
``update_contact`` and ``create_crm_proposal`` write a ``workflow_proposals``
row that a person reviews and applies -- unless an admin saved the workflow with
the update action marked ``safe_automation: true``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import timedelta
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from cloud.intel.core.audit import audit
from cloud.intel.automation.graph import delay_seconds, is_graph, retry_policy, validate_graph
from cloud.intel.core.context import ConflictError, Ctx, ForbiddenError, NotFoundError, ValidationError, utcnow

__all__ = ["ACTIONS", "AutomationEngine", "TRIGGERS", "evaluate", "run_workflow_resume_task", "run_workflow_task"]

log = logging.getLogger(__name__)

TRIGGERS = ("new_company", "hiring_spike", "technology_detected", "leadership_change", "new_contact",
            "email_validated", "job_posted", "long_open_job", "company_matched", "research_completed",
            # advanced workflows
            "reply_received", "scrape_completed", "import_completed", "validation_job_completed",
            "manual", "schedule")

ACTIONS = ("enrich_company", "find_contacts", "validate_email", "create_task", "create_opportunity",
           "assign_owner", "add_to_list", "assign_campaign", "queue_sequence", "export", "webhook",
           # advanced workflows
           "remove_from_list", "update_company", "update_contact", "create_crm_proposal", "start_research",
           "add_to_campaign", "start_sequence", "send_notification", "wait")

#: Actions that change CRM records: proposed for review unless marked safe by an admin.
CRM_CHANGE_ACTIONS = ("update_company", "update_contact", "create_crm_proposal")
#: Fields a workflow may never propose to change.
_PROTECTED_FIELDS = frozenset({"id", "workspace_id", "created_at", "updated_at", "created_by", "version",
                               "merged_into_id", "unsubscribed", "status"})
_TERMINAL_RUN = ("succeeded", "skipped", "cancelled")
_MAX_GRAPH_STEPS = 200

_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "exists")


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return "<missing>"


MISSING = _Missing()


def _resolve(data: Mapping[str, Any], path: str) -> Any:
    value: Any = data
    for part in str(path).split("."):
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        else:
            return MISSING
    return value


def _compare(op: str, actual: Any, expected: Any) -> bool:
    if op == "exists":
        present = actual is not MISSING and actual is not None and actual != "" and actual != []
        return present if expected in (None, True, "true", 1) else not present
    if actual is MISSING or actual is None:
        return op == "ne" and expected is not None
    try:
        if op == "eq":
            return actual == expected or (isinstance(actual, str) and isinstance(expected, str)
                                          and actual.lower() == expected.lower())
        if op == "ne":
            return not _compare("eq", actual, expected)
        if op in ("gt", "gte", "lt", "lte"):
            a, b = float(actual), float(expected)
            return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
        if op == "contains":
            if isinstance(actual, (list, tuple, set)):
                return any(_compare("eq", item, expected) for item in actual)
            return str(expected).lower() in str(actual).lower()
        if op == "in":
            options = expected if isinstance(expected, (list, tuple, set)) else [expected]
            if isinstance(actual, (list, tuple, set)):
                return any(_compare("in", item, options) for item in actual)
            return any(_compare("eq", actual, option) for option in options)
    except (TypeError, ValueError):
        return False
    raise ValidationError(f"unknown condition operator {op!r}")


def validate_conditions(node: Any) -> None:
    if node in (None, [], {}):
        return
    if isinstance(node, list):
        for child in node:
            validate_conditions(child)
        return
    if not isinstance(node, Mapping):
        raise ValidationError("a condition must be an object or a list")
    if "all" in node or "any" in node:
        children = node.get("all", node.get("any"))
        if not isinstance(children, list):
            raise ValidationError("all/any must hold a list")
        validate_conditions(children)
        return
    if "field" not in node or node.get("op") not in _OPS:
        raise ValidationError(f"a condition needs a field and an op in {', '.join(_OPS)}")


def evaluate(node: Any, data: Mapping[str, Any]) -> bool:
    """Evaluate a condition tree against ``data``. An empty tree is true."""
    if node in (None, [], {}):
        return True
    if isinstance(node, list):
        return all(evaluate(child, data) for child in node)
    if "all" in node:
        return all(evaluate(child, data) for child in node["all"])
    if "any" in node:
        children = node["any"]
        return any(evaluate(child, data) for child in children) if children else True
    return _compare(node["op"], _resolve(data, node["field"]), node.get("value"))


def _webhook_secret() -> Optional[bytes]:
    secret = os.environ.get("CAREERCLOUD_WEBHOOK_SIGNING_SECRET")
    return secret.encode() if secret else None


class AutomationEngine:
    def __init__(self, platform: Any, *, http_post: Optional[Callable[..., Any]] = None,
                 resolver: Optional[Callable[..., Any]] = None) -> None:
        self.platform = platform
        self.store = platform.store
        self._http_post = http_post
        #: DNS resolver for the webhook SSRF check (injected in tests).
        self._resolver = resolver

    # --- authoring --------------------------------------------------------------------

    def save_workflow(self, ctx: Ctx, values: Mapping[str, Any], workflow_id: Optional[str] = None
                      ) -> Dict[str, Any]:
        """Create or update a workflow after validating trigger, conditions and actions.

        New workflows are disabled unless ``enabled`` is passed explicitly. Whether
        the editor was an admin is recorded, because paid actions require it.
        """
        ctx.require_write()
        values = dict(values)
        if "trigger" in values and values["trigger"] not in TRIGGERS:
            raise ValidationError(f"trigger must be one of {', '.join(TRIGGERS)}")
        if "conditions" in values:
            validate_conditions(values["conditions"])
        def normalise(action: Any) -> Dict[str, Any]:
            if not isinstance(action, Mapping) or action.get("type") not in ACTIONS:
                raise ValidationError(f"each action needs a type in {', '.join(ACTIONS)}")
            action = dict(action)
            action["_saved_by_admin"] = bool(ctx.can_admin)
            return action

        if "actions" in values:
            actions = values["actions"]
            if not isinstance(actions, list):
                raise ValidationError("actions must be a list")
            values["actions"] = [normalise(a) for a in actions]
        if "graph" in values:
            graph = values["graph"]
            if graph in (None, {}):
                values["graph"] = {}
            elif not isinstance(graph, Mapping):
                raise ValidationError("graph must be an object")
            elif is_graph(graph):
                values["graph"] = validate_graph(graph, validate_conditions=validate_conditions,
                                                 normalise_action=normalise)
            else:
                # No nodes: only schedule / builder metadata for a flat workflow.
                meta = {k: v for k, v in dict(graph).items() if k in ("schedule", "ui")}
                validate_graph({"start": "_", "nodes": {"_": {"type": "end"}}, **meta},
                               validate_conditions=validate_conditions, normalise_action=normalise)
                values["graph"] = meta
        if "retry_policy" in values:
            if not isinstance(values["retry_policy"], Mapping):
                raise ValidationError("retry_policy must be an object")
            retry_policy({"retry_policy": values["retry_policy"]})
            values["retry_policy"] = {k: int(v) for k, v in values["retry_policy"].items()
                                      if k in ("max_attempts", "backoff_seconds") and v not in (None, "")}
        if workflow_id is None:
            values.setdefault("enabled", False)
            if "trigger" not in values:
                raise ValidationError("trigger is required")
            row = self.store.insert(ctx, "workflows", values)
            audit(self.store, ctx, "workflow.create", entity_type="workflows", entity_id=row["id"],
                  summary=row["name"])
        else:
            row = self.store.update(ctx, "workflows", workflow_id, values)
            audit(self.store, ctx, "workflow.update", entity_type="workflows", entity_id=workflow_id,
                  changes={k: v for k, v in values.items() if k != "actions"})
        return row

    # --- emitting ------------------------------------------------------------------------

    def emit(self, ctx: Ctx, trigger: str, event_key: str, payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """Record one run per enabled workflow on ``trigger`` and queue it. Duplicate
        ``event_key`` for the same workflow is a no-op. Returns the new runs."""
        if trigger not in TRIGGERS:
            raise ValidationError(f"unknown trigger {trigger!r}")
        system = ctx if ctx.system else ctx.as_system("workflow")
        runs = []
        for workflow in self.store.all(system, "workflows", {"trigger": trigger, "enabled": True}, cap=500):
            run = self._start_run(system, workflow, trigger, event_key, payload)
            if run is not None:
                runs.append(run)
        return runs

    def _start_run(self, system: Ctx, workflow: Mapping[str, Any], trigger: str, event_key: str,
                   payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        if workflow.get("max_runs_per_day") is not None:
            since = utcnow() - timedelta(days=1)
            recent = self.store.count(system, "workflow_runs", {"workflow_id": workflow["id"],
                                                                "created_at__gte": since})
            if recent >= workflow["max_runs_per_day"]:
                log.info("workflow %s hit max_runs_per_day", workflow["id"])
                return None
        try:
            run = self.store.insert(system, "workflow_runs", {
                "workflow_id": workflow["id"], "trigger": trigger, "event_key": str(event_key)[:300],
                "status": "pending", "input": _jsonable(dict(payload))})
        except ConflictError:
            return None  # this event already produced a run for this workflow
        try:
            self.store.update(system, "workflows", workflow["id"], {"last_run_at": utcnow()})
        except Exception:  # noqa: BLE001 - bookkeeping only
            log.debug("could not stamp last_run_at", exc_info=True)
        task = self.platform.tasks.submit(system, "workflow", {"run_id": run["id"]},
                                          idempotency_key=f"workflow-run:{run['id']}",
                                          entity_type="workflow_runs", entity_id=run["id"])
        return {**run, "task_id": task["id"]}

    def run_now(self, ctx: Ctx, workflow_id: str, payload: Optional[Mapping[str, Any]] = None
                ) -> Dict[str, Any]:
        """A person starts a workflow by hand (the ``manual`` trigger, or a live test)."""
        ctx.require_write()
        system = ctx if ctx.system else ctx.as_system("workflow")
        workflow = self.store.get(system, "workflows", workflow_id)
        run = self._start_run(system, {**workflow, "max_runs_per_day": None}, "manual",
                              f"manual:{uuid.uuid4().hex}", payload or {})
        audit(self.store, ctx, "workflow.run_manual", entity_type="workflows", entity_id=workflow_id,
              summary=workflow["name"])
        return run or {}

    # --- periodic work ------------------------------------------------------------------------

    def tick(self, ctx: Ctx, now: Optional[Any] = None) -> int:
        """Resume due delayed/retrying runs and start due scheduled workflows. Cheap when idle."""
        now = now or utcnow()
        system = ctx if ctx.system else ctx.as_system("workflow")
        done = 0
        due = self.store.list(system, "workflow_runs", {"status": "waiting", "resume_at__lte": now},
                              order="resume_at", limit=100).rows
        for run in due:
            stamp = run["resume_at"].isoformat() if hasattr(run["resume_at"], "isoformat") else str(run["resume_at"])
            self.platform.tasks.submit(system, "workflow_resume", {"run_id": run["id"]},
                                       idempotency_key=f"workflow-resume:{run['id']}:{stamp}"[:200],
                                       entity_type="workflow_runs", entity_id=run["id"])
            done += 1
        for workflow in self.store.all(system, "workflows", {"trigger": "schedule", "enabled": True}, cap=200):
            schedule = (workflow.get("graph") or {}).get("schedule") or {}
            try:
                every = int(schedule.get("every_minutes") or 0)
            except (TypeError, ValueError):
                continue
            if every < 5:
                continue
            bucket = int(now.timestamp() // (every * 60))
            if self._start_run(system, workflow, "schedule", f"schedule:{bucket}",
                               {"scheduled_at": now.isoformat()}) is not None:
                done += 1
        return done

    # --- approvals and control ------------------------------------------------------------------

    def decide(self, ctx: Ctx, run_id: str, *, approve: bool, note: Optional[str] = None) -> Dict[str, Any]:
        """Approve or reject a run waiting at an approval step. Signed-in writers only."""
        ctx.require_write()
        if ctx.system or ctx.user_id is None:
            raise ForbiddenError("workflow approvals must be made by a signed-in user")
        system = ctx.as_system("workflow")
        run = self.store.get(system, "workflow_runs", run_id)
        if run["status"] != "awaiting_approval":
            raise ValidationError("this run is not waiting for approval")
        workflow = self.store.get(system, "workflows", run["workflow_id"])
        node_id = run.get("current_node")
        node = ((workflow.get("graph") or {}).get("nodes") or {}).get(node_id) or {}
        target = node.get("next") if approve else node.get("on_reject")
        now = utcnow()
        history = list(run.get("history") or [])
        history.append({"node": node_id, "type": "approval", "status": "approved" if approve else "rejected",
                        "by": ctx.user_id, "note": (note or "")[:500] or None, "at": now.isoformat()})
        changes: Dict[str, Any] = {"history": history, "approved_by": ctx.user_id, "approved_at": now}
        if target:
            changes.update({"status": "pending", "current_node": target})
        elif approve:
            changes.update({"status": "succeeded", "current_node": None, "finished_at": now})
        else:
            changes.update({"status": "cancelled", "current_node": None, "finished_at": now,
                            "error": "rejected at the approval step"})
        row = self.store.update(system, "workflow_runs", run_id, changes)
        audit(self.store, ctx, "workflow.approve" if approve else "workflow.reject", entity_type="workflow_runs",
              entity_id=run_id, summary=f"{workflow['name']} at step {node_id}")
        if target:
            self.platform.tasks.submit(system, "workflow_resume", {"run_id": run_id},
                                       idempotency_key=f"workflow-approval:{run_id}:{node_id}"[:200],
                                       entity_type="workflow_runs", entity_id=run_id)
        return row

    def cancel_run(self, ctx: Ctx, run_id: str) -> Dict[str, Any]:
        ctx.require_write()
        system = ctx if ctx.system else ctx.as_system("workflow")
        run = self.store.get(system, "workflow_runs", run_id)
        if run["status"] in _TERMINAL_RUN:
            return run
        history = list(run.get("history") or []) + [{"type": "cancel", "status": "cancelled",
                                                      "by": ctx.user_id, "at": utcnow().isoformat()}]
        row = self.store.update(system, "workflow_runs", run_id, {
            "status": "cancelled", "resume_at": None, "finished_at": utcnow(), "history": history})
        audit(self.store, ctx, "workflow.run_cancel", entity_type="workflow_runs", entity_id=run_id)
        return row

    def resume_now(self, ctx: Ctx, run_id: str) -> Dict[str, Any]:
        """Skip the rest of a delay (or a retry backoff) and continue now."""
        ctx.require_write()
        system = ctx if ctx.system else ctx.as_system("workflow")
        run = self.store.get(system, "workflow_runs", run_id)
        if run["status"] != "waiting":
            raise ValidationError("only a waiting run can be resumed")
        row = self.store.update(system, "workflow_runs", run_id, {"resume_at": utcnow()})
        self.platform.tasks.submit(system, "workflow_resume", {"run_id": run_id},
                                   idempotency_key=f"workflow-resume-now:{run_id}:{uuid.uuid4().hex[:8]}",
                                   entity_type="workflow_runs", entity_id=run_id)
        audit(self.store, ctx, "workflow.run_resume", entity_type="workflow_runs", entity_id=run_id)
        return row

    # --- proposals (PROPOSE -> REVIEW -> APPLY) -------------------------------------------------

    def review_proposals(self, ctx: Ctx, proposal_ids: Sequence[str], *, approve: bool) -> List[Dict[str, Any]]:
        ctx.require_write()
        if ctx.system or ctx.user_id is None:
            raise ForbiddenError("proposals must be reviewed by a signed-in user")
        out = []
        for proposal_id in proposal_ids:
            proposal = self.store.get(ctx, "workflow_proposals", proposal_id)
            if proposal["status"] != "proposed":
                continue
            out.append(self.store.update(ctx, "workflow_proposals", proposal_id, {
                "status": "approved" if approve else "rejected", "reviewed_by": ctx.user_id,
                "reviewed_at": utcnow()}))
            audit(self.store, ctx, "workflow.proposal_" + ("approve" if approve else "reject"),
                  entity_type="workflow_proposals", entity_id=proposal_id)
        return out

    def apply_proposals(self, ctx: Ctx, proposal_ids: Sequence[str]) -> List[Dict[str, Any]]:
        """Apply approved proposals through the CRM service, as the reviewing user."""
        ctx.require_write()
        if ctx.system or ctx.user_id is None:
            raise ForbiddenError("proposals must be applied by a signed-in user")
        crm = self.platform.service("crm")
        out = []
        for proposal_id in proposal_ids:
            proposal = self.store.get(ctx, "workflow_proposals", proposal_id)
            if proposal["status"] != "approved":
                continue
            try:
                if proposal["entity_type"] == "companies":
                    crm.update_company(ctx, proposal["entity_id"], proposal["changes"])
                else:
                    crm.update_contact(ctx, proposal["entity_id"], proposal["changes"])
                row = self.store.update(ctx, "workflow_proposals", proposal_id, {"status": "applied", "error": None})
            except Exception as error:  # noqa: BLE001 - recorded on the proposal
                row = self.store.update(ctx, "workflow_proposals", proposal_id,
                                        {"status": "failed", "error": str(error)[:1000]})
            audit(self.store, ctx, "workflow.proposal_apply", entity_type="workflow_proposals",
                  entity_id=proposal_id, changes={"status": row["status"]})
            out.append(row)
        return out

    # --- execution ----------------------------------------------------------------------------

    def _context_data(self, ctx: Ctx, payload: Mapping[str, Any]) -> Dict[str, Any]:
        data: Dict[str, Any] = {"payload": dict(payload)}
        for key, entity in (("company", "companies"), ("contact", "contacts"), ("signal", "hiring_signals"),
                            ("job", "job_postings"), ("opportunity", "opportunities")):
            row_id = payload.get(f"{key}_id")
            if row_id:
                row = self.store.find(ctx, entity, row_id)
                if row is not None:
                    data[key] = row
            elif isinstance(payload.get(key), Mapping):
                data[key] = dict(payload[key])
        return data

    def dry_run(self, ctx: Ctx, workflow_id: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Evaluate conditions and describe the actions, changing nothing."""
        workflow = self.store.get(ctx, "workflows", workflow_id)
        data = self._context_data(ctx, payload)
        matched = evaluate(workflow["conditions"], data)
        out = {"workflow_id": workflow_id, "conditions_met": matched,
               "actions": [{"type": a["type"], "would_run": matched,
                            "config": {k: v for k, v in a.items() if not k.startswith("_")}}
                           for a in workflow["actions"]]}
        if is_graph(workflow.get("graph")):
            out["path"] = self._dry_path(workflow["graph"], data) if matched else []
        return out

    @staticmethod
    def _dry_path(graph: Mapping[str, Any], data: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """The steps a run would take now, branches evaluated; delays and approvals marked as pauses."""
        path: List[Dict[str, Any]] = []
        node_id, nodes = graph.get("start"), graph.get("nodes") or {}
        while node_id and node_id in nodes and len(path) < _MAX_GRAPH_STEPS:
            node = nodes[node_id]
            step: Dict[str, Any] = {"node": node_id, "type": node.get("type")}
            path.append(step)
            if node.get("type") == "condition":
                matched = evaluate(node.get("conditions"), data)
                step["branch"] = "then" if matched else "else"
                node_id = node.get("then") if matched else node.get("else")
            elif node.get("type") in ("delay", "approval"):
                step["pauses"] = True
                node_id = node.get("next")
            else:
                if node.get("type") == "action":
                    step["action"] = (node.get("action") or {}).get("type")
                node_id = node.get("next")
        return path

    def execute_run(self, ctx: Ctx, run_id: str) -> Dict[str, Any]:
        system = ctx if ctx.system else ctx.as_system("workflow")
        run = self.store.get(system, "workflow_runs", run_id)
        if run["status"] in _TERMINAL_RUN:
            return {"run_id": run_id, "status": run["status"], "already": True}
        if run["status"] == "awaiting_approval":
            return {"run_id": run_id, "status": "awaiting_approval", "node": run.get("current_node")}
        if run["status"] == "waiting" and run.get("resume_at") and run["resume_at"] > utcnow():
            return {"run_id": run_id, "status": "waiting", "resume_at": run["resume_at"]}
        workflow = self.store.get(system, "workflows", run["workflow_id"])
        run = self.store.update(system, "workflow_runs", run_id, {"status": "running", "resume_at": None,
                                                                 "attempts": run["attempts"] + 1})
        data = self._context_data(system, run["input"])
        if is_graph(workflow.get("graph")):
            return self._execute_graph(system, workflow, run, data)
        steps: List[Dict[str, Any]] = list(run.get("steps") or [])
        done = {s["index"] for s in steps if s.get("status") == "succeeded"}
        if not done and not evaluate(workflow["conditions"], data):
            self.store.update(system, "workflow_runs", run_id, {"status": "skipped", "finished_at": utcnow(),
                                                                "steps": steps + [{"conditions_met": False}]})
            return {"run_id": run_id, "status": "skipped"}
        try:
            for index, action in enumerate(workflow["actions"]):
                if index in done:
                    continue  # a retry resumes after the last successful action
                started = utcnow()
                if action.get("type") == "wait":
                    resume = started + timedelta(seconds=delay_seconds(action) or 3600)
                    steps.append({"index": index, "type": "wait", "status": "succeeded",
                                  "result": {"resume_at": resume.isoformat()}, "at": started.isoformat()})
                    self.store.update(system, "workflow_runs", run_id, {"status": "waiting", "steps": steps,
                                                                        "resume_at": resume})
                    return {"run_id": run_id, "status": "waiting", "resume_at": resume, "steps": steps}
                try:
                    result = self._run_action(system, workflow, action, data)
                except Exception as error:
                    steps.append({"index": index, "type": action.get("type"), "status": "failed",
                                  "error": f"{type(error).__name__}: {error}"[:1000],
                                  "at": started.isoformat()})
                    raise
                steps.append({"index": index, "type": action.get("type"), "status": "succeeded",
                              "result": _jsonable(result), "at": started.isoformat()})
                self.store.update(system, "workflow_runs", run_id, {"steps": steps})
        except Exception as error:
            self.store.update(system, "workflow_runs", run_id, {"status": "failed", "steps": steps,
                                                                "error": str(error)[:4000]})
            audit(self.store, system, "workflow.run_failed", entity_type="workflow_runs", entity_id=run_id,
                  summary=str(error)[:500])
            raise
        self.store.update(system, "workflow_runs", run_id, {"status": "succeeded", "steps": steps, "error": None,
                                                            "finished_at": utcnow()})
        audit(self.store, system, "workflow.run", entity_type="workflow_runs", entity_id=run_id,
              summary=f"{workflow['name']}: {len(steps)} step(s)")
        return {"run_id": run_id, "status": "succeeded", "steps": steps}

    def _execute_graph(self, system: Ctx, workflow: Mapping[str, Any], run: Mapping[str, Any],
                       data: Dict[str, Any]) -> Dict[str, Any]:
        run_id = run["id"]
        graph = workflow["graph"]
        nodes = graph["nodes"]
        history: List[Dict[str, Any]] = list(run.get("history") or [])
        node_id = run.get("current_node")
        if not node_id and not history:
            if not evaluate(workflow["conditions"], data):
                history.append({"type": "trigger", "status": "skipped", "result": {"conditions_met": False},
                                "at": utcnow().isoformat()})
                self.store.update(system, "workflow_runs", run_id, {"status": "skipped", "history": history,
                                                                    "finished_at": utcnow()})
                return {"run_id": run_id, "status": "skipped", "history": history}
            node_id = graph["start"]

        def save(**changes: Any) -> None:
            self.store.update(system, "workflow_runs", run_id, {"history": history, **changes})

        visited = 0
        while node_id:
            visited += 1
            if visited > _MAX_GRAPH_STEPS:
                return self._fail_graph(system, workflow, run_id, history, node_id, "too many steps in one run")
            node = nodes.get(node_id)
            if node is None:
                return self._fail_graph(system, workflow, run_id, history, node_id, f"unknown step {node_id!r}")
            kind = node.get("type")
            now = utcnow()
            entry: Dict[str, Any] = {"node": node_id, "type": kind, "at": now.isoformat()}
            if kind == "end":
                entry["status"] = "succeeded"
                history.append(entry)
                break
            if kind == "condition":
                matched = evaluate(node.get("conditions"), data)
                entry.update({"status": "succeeded", "result": {"branch": "then" if matched else "else"}})
                history.append(entry)
                node_id = node.get("then") if matched else node.get("else")
                save(current_node=node_id)
                continue
            if kind == "delay" or (kind == "action" and (node.get("action") or {}).get("type") == "wait"):
                seconds = delay_seconds(node if kind == "delay" else node["action"]) or 3600
                resume = now + timedelta(seconds=seconds)
                entry.update({"status": "succeeded", "result": {"resume_at": resume.isoformat()}})
                history.append(entry)
                next_node = node.get("next")
                if not next_node:
                    break
                save(status="waiting", current_node=next_node, resume_at=resume)
                return {"run_id": run_id, "status": "waiting", "resume_at": resume, "history": history}
            if kind == "approval":
                entry.update({"status": "waiting", "result": {"message": node.get("message")}})
                history.append(entry)
                save(status="awaiting_approval", current_node=node_id)
                self._notify(system, title=f"Approval needed: {workflow['name']}",
                             body=node.get("message") or "A workflow is waiting for your approval.",
                             link="/workflows", severity="warning", entity_id=run_id)
                return {"run_id": run_id, "status": "awaiting_approval", "node": node_id, "history": history}
            # an action step
            action = node.get("action") or {}
            attempt = 1 + sum(1 for h in history if h.get("node") == node_id and h.get("status") == "failed")
            entry.update({"action": action.get("type"), "attempt": attempt})
            try:
                result = self._run_action(system, workflow, action, data, node_id=node_id, run_id=run_id)
            except Exception as error:  # noqa: BLE001 - the retry and failure policies decide
                entry.update({"status": "failed", "error": f"{type(error).__name__}: {error}"[:1000]})
                history.append(entry)
                policy = retry_policy(workflow, node)
                if attempt < policy["max_attempts"]:
                    resume = now + timedelta(seconds=policy["backoff_seconds"] * (2 ** (attempt - 1)))
                    save(status="waiting", current_node=node_id, resume_at=resume, error=entry["error"])
                    return {"run_id": run_id, "status": "waiting", "resume_at": resume, "history": history,
                            "retry": attempt + 1}
                if workflow.get("failure_policy") == "continue":
                    node_id = node.get("next")
                    save(current_node=node_id, error=entry["error"])
                    continue
                return self._fail_graph(system, workflow, run_id, history, node_id, entry["error"])
            entry.update({"status": "succeeded", "result": _jsonable(result)})
            history.append(entry)
            node_id = node.get("next")
            save(current_node=node_id)
        self.store.update(system, "workflow_runs", run_id, {
            "status": "succeeded", "history": history, "current_node": None, "resume_at": None,
            "finished_at": utcnow(), "error": None})
        audit(self.store, system, "workflow.run", entity_type="workflow_runs", entity_id=run_id,
              summary=f"{workflow['name']}: {len(history)} step(s)")
        return {"run_id": run_id, "status": "succeeded", "history": history}

    def _fail_graph(self, system: Ctx, workflow: Mapping[str, Any], run_id: str, history: List[Dict[str, Any]],
                    node_id: Optional[str], error: str) -> Dict[str, Any]:
        self.store.update(system, "workflow_runs", run_id, {"status": "failed", "history": history,
                                                            "current_node": node_id, "resume_at": None,
                                                            "error": str(error)[:4000], "finished_at": utcnow()})
        audit(self.store, system, "workflow.run_failed", entity_type="workflow_runs", entity_id=run_id,
              summary=str(error)[:500])
        self._notify(system, title=f"Workflow failed: {workflow['name']}", body=str(error)[:500],
                     link="/workflows", severity="error", entity_id=run_id)
        return {"run_id": run_id, "status": "failed", "error": error, "history": history}

    def _notify(self, ctx: Ctx, *, title: str, body: Optional[str] = None, link: Optional[str] = None,
                severity: str = "info", entity_id: Optional[str] = None) -> Dict[str, Any]:
        """Best-effort: the notifications service when it exists, otherwise the table directly."""
        title = title[:300]
        severity = severity if severity in ("info", "success", "warning", "error") else "info"
        notify = None
        try:
            notify = getattr(self.platform.service("notifications"), "notify", None)
        except (KeyError, ImportError, AttributeError):
            notify = None
        if callable(notify):
            try:
                return notify(ctx, title=title, body=body, kind="workflow", link=link, severity=severity) or {}
            except Exception:  # noqa: BLE001 - fall back to writing the row
                log.debug("notifications service failed; writing the row directly", exc_info=True)
        try:
            row = self.store.insert(ctx, "notifications", {
                "kind": "workflow", "title": title, "body": str(body)[:2000] if body else None, "link": link,
                "severity": severity, "entity_type": "workflow_runs" if entity_id else None,
                "entity_id": entity_id})
            return {"id": row["id"]}
        except Exception:  # noqa: BLE001 - notifying never breaks a run
            log.debug("notification skipped", exc_info=True)
            return {}

    # --- actions ---------------------------------------------------------------------------------

    def _run_action(self, ctx: Ctx, workflow: Mapping[str, Any], action: Mapping[str, Any],
                    data: Mapping[str, Any], *, node_id: Optional[str] = None,
                    run_id: Optional[str] = None) -> Dict[str, Any]:
        kind = action.get("type")
        handler = getattr(self, f"_action_{kind}", None)
        if kind not in ACTIONS or handler is None:
            raise ValidationError(f"unknown action {kind!r}")
        if kind in CRM_CHANGE_ACTIONS:
            return handler(ctx, workflow, action, data, node_id=node_id, run_id=run_id)
        return handler(ctx, workflow, action, data)

    @staticmethod
    def _id(data: Mapping[str, Any], key: str, action: Mapping[str, Any]) -> Optional[str]:
        if action.get(f"{key}_id"):
            return action[f"{key}_id"]
        row = data.get(key)
        if isinstance(row, Mapping) and row.get("id"):
            return row["id"]
        return (data.get("payload") or {}).get(f"{key}_id")

    def _require(self, value: Optional[str], what: str) -> str:
        if not value:
            raise ValidationError(f"this action needs a {what}")
        return value

    def _action_enrich_company(self, ctx, workflow, action, data):
        company_id = self._require(self._id(data, "company", action), "company")
        allow_paid = bool(action.get("allow_paid")) and bool(action.get("_saved_by_admin"))
        task = self.platform.tasks.submit(ctx, "enrichment", {
            "company_ids": [company_id], "allow_paid": allow_paid, "source": f"workflow:{workflow['id']}",
            "functions": action.get("functions")}, idempotency_key=f"wf:{workflow['id']}:enrich:{company_id}:"
                                                                   f"{data.get('payload', {}).get('event_key', '')}")
        return {"task_id": task["id"], "allow_paid": allow_paid}

    def _action_find_contacts(self, ctx, workflow, action, data):
        company_id = self._require(self._id(data, "company", action), "company")
        contacts = self.platform.service("contacts")
        allow_paid = bool(action.get("allow_paid")) and bool(action.get("_saved_by_admin"))
        result = contacts.find_contacts(ctx, [company_id], allow_paid=allow_paid,
                                        **({"functions": tuple(action["functions"])} if action.get("functions")
                                           else {}))
        return {"company_id": company_id, "allow_paid": allow_paid, "result": result}

    def _action_validate_email(self, ctx, workflow, action, data):
        allow_paid = bool(action.get("allow_paid")) and bool(action.get("_saved_by_admin"))
        if action.get("list_id") or action.get("contact_ids"):
            return self._validate_many(ctx, workflow, action, allow_paid)
        contact = data.get("contact") or {}
        email = action.get("email") or contact.get("email") or (data.get("payload") or {}).get("email")
        email = self._require(email, "email address")
        results = self.platform.service("email").validate(ctx, [email], allow_paid=allow_paid)
        return {"email": email, "results": results}

    def _validate_many(self, ctx, workflow, action, allow_paid):
        contact_ids = list(action.get("contact_ids") or [])
        if action.get("list_id"):
            contact_ids += [m["entity_id"] for m in self.store.all(ctx, "list_members",
                                                                   {"list_id": action["list_id"]}, cap=50_000)]
        if not contact_ids:
            raise ValidationError("validate_email found no contacts to validate")
        create = None
        try:
            create = getattr(self.platform.service("email_jobs"), "create_from_contacts", None)
        except (KeyError, ImportError, AttributeError):
            create = None
        if callable(create):
            job = create(ctx, name=f"Workflow: {workflow['name']}"[:200], contact_ids=contact_ids, start=True)
            return {"job_id": (job or {}).get("id"), "contacts": len(contact_ids)}
        emails = [c["email"] for c in (self.store.find(ctx, "contacts", i) for i in contact_ids)
                  if c and c.get("email")]
        results = self.platform.service("email").validate(ctx, emails, allow_paid=allow_paid)
        return {"emails": len(emails), "counts": _count_status(results), "allow_paid": allow_paid}

    def _action_create_task(self, ctx, workflow, action, data):
        title = _fill(action.get("title") or f"Follow up: {workflow['name']}", data)
        due_days = int(action.get("due_in_days") or 0)
        row = self.store.insert(ctx, "crm_tasks", {
            "title": title[:300], "description": action.get("description"), "status": "open",
            "priority": action.get("priority") or "normal", "due_at": utcnow() + timedelta(days=due_days),
            "assignee_id": action.get("assignee_id"), "company_id": self._id(data, "company", action),
            "contact_id": self._id(data, "contact", action),
            "opportunity_id": self._id(data, "opportunity", action), "source": f"workflow:{workflow['id']}"[:60]})
        audit(self.store, ctx, "workflow.create_task", entity_type="crm_tasks", entity_id=row["id"])
        return {"task_id": row["id"]}

    def _action_create_opportunity(self, ctx, workflow, action, data):
        company_id = self._require(self._id(data, "company", action), "company")
        if action.get("use_campaign_mapping", True) and not action.get("title"):
            result = self.platform.service("campaigns").map_signal_to_opportunity(
                ctx, company_id, create=True, campaign_id=action.get("campaign_id"))
            return {"status": result.get("status"),
                    "opportunity_id": (result.get("opportunity") or {}).get("id")}
        company = data.get("company") or self.store.get(ctx, "companies", company_id)
        signal = data.get("signal") or {}
        opportunity = self.platform.service("crm").create_opportunity(
            ctx, company_id, (action.get("title") or f"{company.get('name')} – {workflow['name']}")[:300],
            signal_ids=[signal["id"]] if signal.get("id") else (),
            signal_types=[signal["signal_type"]] if signal.get("signal_type") else (),
            reason=action.get("reason") or f"created by workflow {workflow['name']}",
            campaign_id=action.get("campaign_id"), source="workflow")
        return {"opportunity_id": opportunity.get("id")}

    def _action_assign_owner(self, ctx, workflow, action, data):
        owner = self._require(action.get("owner_id"), "owner_id")
        entity = action.get("entity", "company")
        table = {"company": "companies", "contact": "contacts", "opportunity": "opportunities"}.get(entity)
        if table is None:
            raise ValidationError("entity must be company, contact or opportunity")
        row_id = self._require(self._id(data, entity, action), entity)
        self.store.update(ctx, table, row_id, {"owner_id": owner})
        audit(self.store, ctx, "workflow.assign_owner", entity_type=table, entity_id=row_id,
              changes={"owner_id": owner})
        return {"entity": table, "id": row_id, "owner_id": owner}

    def _action_add_to_list(self, ctx, workflow, action, data):
        list_id = self._require(action.get("list_id"), "list_id")
        entity = action.get("entity", "company")
        table = {"company": "companies", "contact": "contacts", "job": "job_postings",
                 "opportunity": "opportunities"}.get(entity)
        row_id = self._require(self._id(data, entity, action), entity)
        try:
            added = self.platform.service("crm").add_to_list(ctx, list_id, table, [row_id],
                                                             reason=f"workflow {workflow['name']}")
        except (KeyError, ImportError, AttributeError):
            try:
                self.store.insert(ctx, "list_members", {"list_id": list_id, "entity_type": table, "entity_id": row_id,
                                                        "added_reason": f"workflow {workflow['name']}"[:500]})
                added = 1
            except ConflictError:
                added = 0
        return {"list_id": list_id, "added": added}

    def _action_assign_campaign(self, ctx, workflow, action, data):
        campaign_id = self._require(action.get("campaign_id"), "campaign_id")
        opportunity_id = self._id(data, "opportunity", action)
        if opportunity_id:
            self.platform.service("campaigns").assign_campaign(ctx, opportunity_id, campaign_id)
            return {"opportunity_id": opportunity_id, "campaign_id": campaign_id}
        company_id = self._require(self._id(data, "company", action), "company or opportunity")
        company = self.store.get(ctx, "companies", company_id)
        campaign = self.store.get(ctx, "campaigns", campaign_id)
        tags = list(company.get("tags") or [])
        tag = f"campaign:{campaign['key']}"
        if tag not in tags:
            self.store.update(ctx, "companies", company_id, {"tags": tags + [tag]})
        return {"company_id": company_id, "campaign_id": campaign_id, "tag": tag}

    def _action_queue_sequence(self, ctx, workflow, action, data):
        sequence_id = self._require(action.get("sequence_id"), "sequence_id")
        contact_ids = list(action.get("contact_ids") or [])
        contact_id = self._id(data, "contact", action)
        if contact_id:
            contact_ids.append(contact_id)
        if not contact_ids:
            raise ValidationError("queue_sequence needs a contact")
        results = self.platform.service("sequences").enroll(ctx, sequence_id, contact_ids,
                                                            campaign_id=action.get("campaign_id"))
        # Enrollments are pending_approval: a human approves before anything is sent.
        return {"results": [{k: v for k, v in r.items() if k != "enrollment"} for r in results],
                "status": "pending_approval"}

    def _action_export(self, ctx, workflow, action, data):
        entity = action.get("entity_type", "companies")
        fmt = action.get("format", "csv")
        task = self.platform.tasks.submit(ctx, "export", {"entity_type": entity, "format": fmt,
                                                          "filters": dict(action.get("filters") or {})})
        return {"task_id": task["id"]}

    def _action_webhook(self, ctx, workflow, action, data):
        from cloud.intel.core.http import check_url

        url = check_url(self._require(action.get("url"), "url"), resolver=self._resolver)
        body = json.dumps(_jsonable({"workflow_id": workflow["id"], "workflow": workflow["name"],
                                     "trigger": workflow["trigger"], "workspace_id": ctx.workspace_id,
                                     "data": {k: v for k, v in data.items()}}), sort_keys=True)
        headers = {"Content-Type": "application/json", "User-Agent": "CareerCrawler-Webhooks/1.0"}
        secret = _webhook_secret()
        if secret:
            headers["X-CareerCrawler-Signature"] = "sha256=" + hmac.new(secret, body.encode(),
                                                                        hashlib.sha256).hexdigest()
        post = self._http_post
        if post is None:
            import requests

            post = requests.post
        response = post(url, data=body, headers=headers, timeout=10, allow_redirects=False)
        status = getattr(response, "status_code", 0)
        if status >= 400 or status == 0:
            raise RuntimeError(f"webhook returned HTTP {status}")
        return {"url": url, "status": status, "signed": bool(secret)}


    # --- advanced actions ------------------------------------------------------------------------

    def _action_remove_from_list(self, ctx, workflow, action, data):
        list_id = self._require(action.get("list_id"), "list_id")
        entity = action.get("entity", "company")
        row_id = self._require(self._id(data, entity, action), entity)
        removed = self.platform.service("crm").remove_from_list(ctx, list_id, [row_id])
        return {"list_id": list_id, "removed": removed}

    @staticmethod
    def _proposal_changes(table: str, changes: Any) -> Dict[str, Any]:
        from cloud.intel.store.spec import get_spec

        if not isinstance(changes, Mapping) or not changes:
            raise ValidationError("this action needs the changes to make")
        columns = get_spec(table).columns
        bad = [k for k in changes if k not in columns or k in _PROTECTED_FIELDS]
        if bad:
            raise ValidationError(f"a workflow cannot change: {', '.join(sorted(bad))}")
        return dict(changes)

    def _propose(self, ctx, workflow, action, data, *, entity: str, kind: str, node_id, run_id, safe: bool):
        table = {"company": "companies", "contact": "contacts"}[entity]
        row_id = self._require(self._id(data, entity, action), entity)
        changes = self._proposal_changes(table, action.get("changes"))
        if safe and action.get("safe_automation") and action.get("_saved_by_admin"):
            crm = self.platform.service("crm")
            if table == "companies":
                crm.update_company(ctx, row_id, changes)
            else:
                crm.update_contact(ctx, row_id, changes)
            audit(self.store, ctx, f"workflow.{kind}", entity_type=table, entity_id=row_id, changes=changes,
                  summary=f"safe automation in {workflow['name']}")
            return {"applied": True, "entity_type": table, "entity_id": row_id}
        node = str(node_id or f"{kind}:{row_id}")[:80]
        run_key = run_id or f"direct:{uuid.uuid4().hex}"
        try:
            proposal = self.store.insert(ctx, "workflow_proposals", {
                "workflow_id": workflow["id"], "run_id": run_key, "node": node,
                "action": "update_company" if table == "companies" else "update_contact",
                "entity_type": table, "entity_id": row_id, "changes": _jsonable(changes),
                "reason": (action.get("reason") or f"proposed by workflow {workflow['name']}")[:1000],
                "status": "proposed"})
        except ConflictError:
            proposal = self.store.first(ctx, "workflow_proposals", {"run_id": run_key, "node": node})
        audit(self.store, ctx, "workflow.propose", entity_type="workflow_proposals", entity_id=proposal["id"],
              changes={"entity_type": table, "entity_id": row_id})
        return {"applied": False, "proposal_id": proposal["id"], "status": "proposed"}

    def _action_update_company(self, ctx, workflow, action, data, *, node_id=None, run_id=None):
        return self._propose(ctx, workflow, action, data, entity="company", kind="update_company",
                             node_id=node_id, run_id=run_id, safe=True)

    def _action_update_contact(self, ctx, workflow, action, data, *, node_id=None, run_id=None):
        return self._propose(ctx, workflow, action, data, entity="contact", kind="update_contact",
                             node_id=node_id, run_id=run_id, safe=True)

    def _action_create_crm_proposal(self, ctx, workflow, action, data, *, node_id=None, run_id=None):
        entity = action.get("entity", "company")
        if entity not in ("company", "contact"):
            raise ValidationError("entity must be company or contact")
        return self._propose(ctx, workflow, action, data, entity=entity, kind="create_crm_proposal",
                             node_id=node_id, run_id=run_id, safe=False)

    def _action_start_research(self, ctx, workflow, action, data):
        question = _fill(self._require(action.get("question"), "question"), data)
        run = self.platform.service("research").plan(ctx, question)
        # Only planned: a person reviews and approves the research plan before it runs.
        return {"research_run_id": run.get("id"), "status": run.get("status", "planned")}

    def _action_add_to_campaign(self, ctx, workflow, action, data):
        return self._action_assign_campaign(ctx, workflow, action, data)

    def _action_start_sequence(self, ctx, workflow, action, data):
        return self._action_queue_sequence(ctx, workflow, action, data)

    def _action_send_notification(self, ctx, workflow, action, data):
        title = _fill(action.get("title") or f"Workflow: {workflow['name']}", data)
        body = _fill(action["body"], data) if action.get("body") else None
        result = self._notify(ctx, title=title, body=body, link=action.get("link") or "/workflows",
                              severity=action.get("severity") or "info")
        return {"notified": bool(result), "notification_id": (result or {}).get("id"), "title": title[:300]}

    def _action_wait(self, ctx, workflow, action, data):  # pragma: no cover - the runners handle waits
        return {"waited": True}


def _fill(text: str, data: Mapping[str, Any]) -> str:
    """``"{company.name}"``-style placeholders from the run data; a bad pattern stays as written."""
    if "{" not in (text or ""):
        return text
    try:
        return text.format(company=_Dot(data.get("company") or {}), contact=_Dot(data.get("contact") or {}),
                           signal=_Dot(data.get("signal") or {}), job=_Dot(data.get("job") or {}),
                           payload=_Dot(data.get("payload") or {}))
    except (KeyError, IndexError, ValueError, AttributeError):
        return text


def _count_status(results: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for result in results:
        key = str(result.get("status"))
        counts[key] = counts.get(key, 0) + 1
    return counts


class _Dot(dict):
    """Lets ``"{company.name}"``-style action titles read row fields."""

    def __getattr__(self, item: str) -> Any:
        return self.get(item, "")


def _jsonable(value: Any) -> Any:
    from cloud.intel.core.audit import _jsonable as inner

    return inner(value)


def run_workflow_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    run_id = (task.get("params") or {}).get("run_id")
    if not run_id:
        from cloud.intel.tasks.worker import PermanentTaskError

        raise PermanentTaskError("workflow task without a run_id")
    engine = platform.service("automation")
    reporter.progress("Running workflow")
    try:
        return engine.execute_run(ctx, run_id)
    except (ValidationError, NotFoundError) as error:
        from cloud.intel.tasks.worker import PermanentTaskError

        raise PermanentTaskError(str(error)) from error


def run_workflow_resume_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    """Continue a run after a delay, a retry backoff or an approval."""
    return run_workflow_task(platform, ctx, task, reporter)
