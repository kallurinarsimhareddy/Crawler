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
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, NotFoundError, ValidationError, utcnow

__all__ = ["ACTIONS", "AutomationEngine", "TRIGGERS", "evaluate", "run_workflow_task"]

log = logging.getLogger(__name__)

TRIGGERS = ("new_company", "hiring_spike", "technology_detected", "leadership_change", "new_contact",
            "email_validated", "job_posted", "long_open_job", "company_matched", "research_completed")

ACTIONS = ("enrich_company", "find_contacts", "validate_email", "create_task", "create_opportunity",
           "assign_owner", "add_to_list", "assign_campaign", "queue_sequence", "export", "webhook")

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
        if "actions" in values:
            actions = values["actions"]
            if not isinstance(actions, list):
                raise ValidationError("actions must be a list")
            normalised = []
            for action in actions:
                if not isinstance(action, Mapping) or action.get("type") not in ACTIONS:
                    raise ValidationError(f"each action needs a type in {', '.join(ACTIONS)}")
                action = dict(action)
                action["_saved_by_admin"] = bool(ctx.can_admin)
                normalised.append(action)
            values["actions"] = normalised
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
            if workflow.get("max_runs_per_day") is not None:
                since = utcnow() - timedelta(days=1)
                recent = self.store.count(system, "workflow_runs", {"workflow_id": workflow["id"],
                                                                    "created_at__gte": since})
                if recent >= workflow["max_runs_per_day"]:
                    log.info("workflow %s hit max_runs_per_day", workflow["id"])
                    continue
            try:
                run = self.store.insert(system, "workflow_runs", {
                    "workflow_id": workflow["id"], "trigger": trigger, "event_key": str(event_key)[:300],
                    "status": "pending", "input": _jsonable(dict(payload))})
            except ConflictError:
                continue  # this event already produced a run for this workflow
            task = self.platform.tasks.submit(system, "workflow", {"run_id": run["id"]},
                                              idempotency_key=f"workflow-run:{run['id']}",
                                              entity_type="workflow_runs", entity_id=run["id"])
            runs.append({**run, "task_id": task["id"]})
        return runs

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
        return {"workflow_id": workflow_id, "conditions_met": matched,
                "actions": [{"type": a["type"], "would_run": matched,
                             "config": {k: v for k, v in a.items() if not k.startswith("_")}}
                            for a in workflow["actions"]]}

    def execute_run(self, ctx: Ctx, run_id: str) -> Dict[str, Any]:
        system = ctx if ctx.system else ctx.as_system("workflow")
        run = self.store.get(system, "workflow_runs", run_id)
        if run["status"] in ("succeeded", "skipped"):
            return {"run_id": run_id, "status": run["status"], "already": True}
        workflow = self.store.get(system, "workflows", run["workflow_id"])
        run = self.store.update(system, "workflow_runs", run_id, {"status": "running",
                                                                 "attempts": run["attempts"] + 1})
        data = self._context_data(system, run["input"])
        steps: List[Dict[str, Any]] = list(run.get("steps") or [])
        done = {s["index"] for s in steps if s.get("status") == "succeeded"}
        if not evaluate(workflow["conditions"], data):
            self.store.update(system, "workflow_runs", run_id, {"status": "skipped", "finished_at": utcnow(),
                                                                "steps": steps + [{"conditions_met": False}]})
            return {"run_id": run_id, "status": "skipped"}
        try:
            for index, action in enumerate(workflow["actions"]):
                if index in done:
                    continue  # a retry resumes after the last successful action
                started = utcnow()
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

    # --- actions ---------------------------------------------------------------------------------

    def _run_action(self, ctx: Ctx, workflow: Mapping[str, Any], action: Mapping[str, Any],
                    data: Mapping[str, Any]) -> Dict[str, Any]:
        kind = action.get("type")
        handler = getattr(self, f"_action_{kind}", None)
        if kind not in ACTIONS or handler is None:
            raise ValidationError(f"unknown action {kind!r}")
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
        contact = data.get("contact") or {}
        email = action.get("email") or contact.get("email") or (data.get("payload") or {}).get("email")
        email = self._require(email, "email address")
        allow_paid = bool(action.get("allow_paid")) and bool(action.get("_saved_by_admin"))
        results = self.platform.service("email").validate(ctx, [email], allow_paid=allow_paid)
        return {"email": email, "results": results}

    def _action_create_task(self, ctx, workflow, action, data):
        title = action.get("title") or f"Follow up: {workflow['name']}"
        company = data.get("company") or {}
        if "{" in title:
            try:
                title = title.format(company=_Dot(company), contact=_Dot(data.get("contact") or {}),
                                     signal=_Dot(data.get("signal") or {}))
            except (KeyError, IndexError, ValueError):
                pass
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
