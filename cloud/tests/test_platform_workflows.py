"""Phase 10: advanced workflows — graphs with branching, delays, approvals,
retries and failure policies, CRM proposals (PROPOSE -> REVIEW -> APPLY), new
actions, templates, schedules, and the HTTP routes."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from cloud.intel.automation.engine import AutomationEngine
from cloud.intel.automation.templates import TEMPLATES
from cloud.intel.core.context import Ctx, ForbiddenError, ValidationError, utcnow
from cloud.intel.platform import Platform
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks.worker import run_task_inline


class Response:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeNotifications:
    def __init__(self):
        self.sent = []

    def notify(self, ctx, **kw):
        self.sent.append(kw)
        return {"id": f"nf_{len(self.sent)}"}


class FakeEmailJobs:
    def __init__(self):
        self.calls = []

    def create_from_contacts(self, ctx, *, name, contact_ids, start=False):
        self.calls.append((name, list(contact_ids), start))
        return {"id": "evj_1"}


class FakeResearch:
    def __init__(self):
        self.questions = []

    def plan(self, ctx, question):
        self.questions.append(question)
        return {"id": "rr_1", "status": "planned"}


class FakeSequences:
    def __init__(self):
        self.calls = []

    def enroll(self, ctx, sequence_id, contact_ids, campaign_id=None, **kw):
        self.calls.append((sequence_id, list(contact_ids), campaign_id))
        return [{"contact_id": c, "status": "enrolled", "enrollment": {"status": "pending_approval"}}
                for c in contact_ids]


class WorkflowGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.platform = Platform(self.store)
        self.user = str(uuid.uuid4())
        ws = self.store.create_workspace(self.user, "Flows", f"flows-{uuid.uuid4().hex[:6]}")
        self.ws = ws["id"]
        self.ctx = Ctx(self.ws, self.user, "owner")
        self.member = str(uuid.uuid4())
        self.store.add_member(self.ctx, self.member, "member")
        self.mctx = Ctx(self.ws, self.member, "member")
        self.viewer = str(uuid.uuid4())
        self.store.add_member(self.ctx, self.viewer, "viewer")
        self.vctx = Ctx(self.ws, self.viewer, "viewer")
        self.post_statuses = []
        self.posts = []

        def post(url, **kw):
            self.posts.append(url)
            return Response(self.post_statuses.pop(0) if self.post_statuses else 200)

        public = [(2, 1, 6, "", ("93.184.216.34", 443))]
        self.engine = AutomationEngine(self.platform, http_post=post, resolver=lambda *a, **k: public)
        self.platform.override("automation", self.engine)
        self.notifications = FakeNotifications()
        self.platform.override("notifications", self.notifications)
        self.company = self.store.insert(self.ctx, "companies", {"name": "Acme", "industry": "Manufacturing",
                                                                 "account_score": 80})
        self.other = self.store.insert(self.ctx, "companies", {"name": "Bolt", "industry": "Retail"})
        self.contact = self.store.insert(self.ctx, "contacts", {"company_id": self.company["id"],
                                                                "full_name": "Jo Lee", "email": "jo@acme.example"})

    # --- helpers -----------------------------------------------------------------------------

    def save(self, graph, *, trigger="new_company", ctx=None, enabled=True, **extra):
        values = {"name": "graph wf", "trigger": trigger, "conditions": [], "actions": [], "graph": graph, **extra}
        wf = self.engine.save_workflow(ctx or self.ctx, values)
        if enabled:
            wf = self.engine.save_workflow(ctx or self.ctx, {"enabled": True}, workflow_id=wf["id"])
        return wf

    def run_pending(self):
        for task in self.store.all(self.ctx.as_system(), "platform_tasks", {"status": "queued"}, cap=100):
            run_task_inline(self.platform, self.ws, task["id"])

    def fire(self, company_id=None, key="e1", trigger="new_company"):
        runs = self.engine.emit(self.ctx, trigger, key, {"company_id": company_id or self.company["id"],
                                                          "contact_id": self.contact["id"]})
        self.run_pending()
        return [self.store.get(self.ctx, "workflow_runs", r["id"]) for r in runs]

    def make_due(self, run):
        self.store.update(self.ctx.as_system(), "workflow_runs", run["id"],
                          {"resume_at": utcnow() - timedelta(seconds=1)})

    def branch_graph(self):
        return {"start": "if", "nodes": {
            "if": {"type": "condition", "conditions": {"field": "company.industry", "op": "eq",
                                                       "value": "Manufacturing"},
                   "then": "yes", "else": "no"},
            "yes": {"type": "action", "action": {"type": "create_task", "title": "Mfg: {company.name}"}},
            "no": {"type": "action", "action": {"type": "create_task", "title": "Other: {company.name}"}},
        }}

    # --- graphs ----------------------------------------------------------------------------------

    def test_branching_follows_the_matching_path(self) -> None:
        wf = self.save(self.branch_graph())
        dry = self.engine.dry_run(self.ctx, wf["id"], {"company_id": self.other["id"]})
        self.assertEqual([(s["node"], s.get("branch")) for s in dry["path"]], [("if", "else"), ("no", None)])
        self.assertEqual(self.store.count(self.ctx, "crm_tasks"), 0)
        [run] = self.fire()
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual([h["node"] for h in run["history"]], ["if", "yes"])
        self.assertEqual(run["history"][0]["result"], {"branch": "then"})
        [run2] = self.fire(self.other["id"], key="e2")
        self.assertEqual([h["node"] for h in run2["history"]], ["if", "no"])
        titles = sorted(t["title"] for t in self.store.all(self.ctx, "crm_tasks"))
        self.assertEqual(titles, ["Mfg: Acme", "Other: Bolt"])

    def test_workflow_conditions_still_gate_graphs(self) -> None:
        self.save(self.branch_graph(), conditions={"field": "company.account_score", "op": "gte", "value": 90})
        [run] = self.fire()
        self.assertEqual(run["status"], "skipped")
        self.assertEqual(self.store.count(self.ctx, "crm_tasks"), 0)

    def test_delay_waits_and_tick_resumes(self) -> None:
        self.save({"start": "a", "nodes": {
            "a": {"type": "action", "action": {"type": "create_task", "title": "first"}, "next": "wait"},
            "wait": {"type": "delay", "days": 2, "next": "b"},
            "b": {"type": "action", "action": {"type": "create_task", "title": "second"}},
        }})
        [run] = self.fire()
        self.assertEqual(run["status"], "waiting")
        self.assertEqual(run["current_node"], "b")
        self.assertGreater(run["resume_at"], utcnow() + timedelta(days=1))
        self.assertEqual(self.engine.tick(self.ctx.as_system()), 0)  # not due yet: a cheap no-op
        self.make_due(run)
        self.assertEqual(self.engine.tick(self.ctx.as_system()), 1)
        self.assertEqual(self.engine.tick(self.ctx.as_system()), 1)  # same key: no second task
        self.assertEqual(self.store.count(self.ctx, "platform_tasks", {"kind": "workflow_resume"}), 1)
        self.run_pending()
        run = self.store.get(self.ctx, "workflow_runs", run["id"])
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual([h["node"] for h in run["history"]], ["a", "wait", "b"])
        self.assertEqual(self.store.count(self.ctx, "crm_tasks"), 2)

    def test_approval_step_waits_for_a_signed_in_writer(self) -> None:
        self.save({"start": "ask", "nodes": {
            "ask": {"type": "approval", "message": "Create the task?", "next": "do", "on_reject": "no"},
            "do": {"type": "action", "action": {"type": "create_task", "title": "approved"}},
            "no": {"type": "action", "action": {"type": "create_task", "title": "rejected path"}},
        }})
        [run] = self.fire()
        self.assertEqual(run["status"], "awaiting_approval")
        self.assertTrue(any("Approval needed" in n["title"] for n in self.notifications.sent))
        with self.assertRaises(ForbiddenError):
            self.engine.decide(self.ctx.as_system(), run["id"], approve=True)
        with self.assertRaises(ForbiddenError):
            self.engine.decide(self.vctx, run["id"], approve=True)
        # the queue never advances it by itself
        self.assertEqual(self.engine.execute_run(self.ctx, run["id"])["status"], "awaiting_approval")
        self.engine.decide(self.mctx, run["id"], approve=True, note="ok")
        self.run_pending()
        run = self.store.get(self.ctx, "workflow_runs", run["id"])
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["approved_by"], self.member)
        self.assertEqual([t["title"] for t in self.store.all(self.ctx, "crm_tasks")], ["approved"])
        with self.assertRaises(ValidationError):
            self.engine.decide(self.mctx, run["id"], approve=True)

    def test_rejection_without_a_reject_path_cancels(self) -> None:
        self.save({"start": "ask", "nodes": {
            "ask": {"type": "approval", "next": "do"},
            "do": {"type": "action", "action": {"type": "create_task", "title": "x"}}}})
        [run] = self.fire()
        row = self.engine.decide(self.ctx, run["id"], approve=False)
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(self.store.count(self.ctx, "crm_tasks"), 0)

    def test_retry_policy_backs_off_then_succeeds(self) -> None:
        self.post_statuses = [500]
        self.save({"start": "hook", "nodes": {
            "hook": {"type": "action", "action": {"type": "webhook", "url": "https://hooks.example.com/in"},
                     "retry": {"max_attempts": 3, "backoff_seconds": 30}}}})
        [run] = self.fire()
        self.assertEqual(run["status"], "waiting")
        self.assertEqual(run["current_node"], "hook")
        self.assertEqual(run["history"][-1]["status"], "failed")
        self.make_due(run)
        self.engine.tick(self.ctx.as_system())
        self.run_pending()
        run = self.store.get(self.ctx, "workflow_runs", run["id"])
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual([h.get("attempt") for h in run["history"]], [1, 2])
        self.assertEqual(len(self.posts), 2)

    def test_failure_policy_stop_and_continue(self) -> None:
        graph = {"start": "hook", "nodes": {
            "hook": {"type": "action", "action": {"type": "webhook", "url": "https://hooks.example.com/in"},
                     "next": "task"},
            "task": {"type": "action", "action": {"type": "create_task", "title": "after"}}}}
        self.post_statuses = [500]
        stop = self.save(graph)
        [run] = self.fire()
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.store.count(self.ctx, "crm_tasks"), 0)
        self.assertTrue(any("failed" in n["title"] for n in self.notifications.sent))
        self.engine.save_workflow(self.ctx, {"enabled": False}, workflow_id=stop["id"])

        self.post_statuses = [500]
        self.save(graph, failure_policy="continue", name="continue wf")
        [run] = self.fire(key="e2")
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual([h["status"] for h in run["history"]], ["failed", "succeeded"])
        self.assertEqual(self.store.count(self.ctx, "crm_tasks"), 1)

    def test_graph_validation(self) -> None:
        with self.assertRaises(ValidationError):  # loop
            self.save({"start": "a", "nodes": {
                "a": {"type": "action", "action": {"type": "create_task"}, "next": "b"},
                "b": {"type": "delay", "hours": 1, "next": "a"}}})
        with self.assertRaises(ValidationError):  # unknown edge
            self.save({"start": "a", "nodes": {"a": {"type": "action", "action": {"type": "create_task"},
                                                     "next": "zzz"}}})
        with self.assertRaises(ValidationError):  # delay too long
            self.save({"start": "a", "nodes": {"a": {"type": "delay", "days": 400}}})
        with self.assertRaises(ValidationError):  # unknown action
            self.save({"start": "a", "nodes": {"a": {"type": "action", "action": {"type": "send_email"}}}})
        with self.assertRaises(ValidationError):  # bad schedule
            self.save({"start": "a", "schedule": {"every_minutes": 1},
                       "nodes": {"a": {"type": "action", "action": {"type": "create_task"}}}}, trigger="schedule")

    def test_every_template_is_valid_and_saves_disabled(self) -> None:
        for item in TEMPLATES:
            wf = self.engine.save_workflow(self.ctx, {"name": item["name"], "trigger": item["trigger"],
                                                      "conditions": item.get("conditions") or [], "actions": [],
                                                      "graph": item["graph"], "template_key": item["key"]})
            self.assertFalse(wf["enabled"], item["key"])
            self.assertIn("nodes", wf["graph"])

    # --- proposals ------------------------------------------------------------------------------

    def test_crm_updates_are_proposed_then_reviewed_then_applied(self) -> None:
        self.save({"start": "u", "nodes": {"u": {"type": "action", "action": {
            "type": "update_company", "changes": {"lifecycle": "account"}}}}}, ctx=self.mctx)
        [run] = self.fire()
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(self.store.get(self.ctx, "companies", self.company["id"])["lifecycle"], "prospect")
        [proposal] = self.store.all(self.ctx, "workflow_proposals")
        self.assertEqual(proposal["status"], "proposed")
        self.assertEqual(self.engine.apply_proposals(self.mctx, [proposal["id"]]), [])  # not approved yet
        with self.assertRaises(ForbiddenError):
            self.engine.review_proposals(self.vctx, [proposal["id"]], approve=True)
        self.engine.review_proposals(self.mctx, [proposal["id"]], approve=True)
        [applied] = self.engine.apply_proposals(self.mctx, [proposal["id"]])
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(self.store.get(self.ctx, "companies", self.company["id"])["lifecycle"], "account")

    def test_safe_automation_applies_only_when_an_admin_saved_it(self) -> None:
        action = {"type": "update_contact", "changes": {"title": "CIO"}, "safe_automation": True}
        self.save({"start": "u", "nodes": {"u": {"type": "action", "action": action}}}, ctx=self.mctx)
        self.fire()
        self.assertIsNone(self.store.get(self.ctx, "contacts", self.contact["id"]).get("title"))
        self.assertEqual(self.store.count(self.ctx, "workflow_proposals"), 1)
        self.save({"start": "u", "nodes": {"u": {"type": "action", "action": action}}}, name="admin wf")
        self.fire(key="e2")
        self.assertEqual(self.store.get(self.ctx, "contacts", self.contact["id"])["title"], "CIO")

    def test_create_crm_proposal_is_never_applied_directly(self) -> None:
        self.save({"start": "p", "nodes": {"p": {"type": "action", "action": {
            "type": "create_crm_proposal", "entity": "company", "changes": {"industry": "Aerospace"},
            "safe_automation": True}}}})
        self.fire()
        self.assertEqual(self.store.get(self.ctx, "companies", self.company["id"])["industry"], "Manufacturing")
        self.assertEqual(self.store.count(self.ctx, "workflow_proposals", {"status": "proposed"}), 1)

    def test_protected_fields_cannot_be_proposed(self) -> None:
        self.save({"start": "u", "nodes": {"u": {"type": "action", "action": {
            "type": "update_company", "changes": {"workspace_id": str(uuid.uuid4())}}}}})
        [run] = self.fire()
        self.assertEqual(run["status"], "failed")
        self.assertIn("cannot change", run["error"])

    # --- other actions ----------------------------------------------------------------------------

    def test_notification_research_sequence_and_list_actions(self) -> None:
        research, sequences = FakeResearch(), FakeSequences()
        self.platform.override("research", research)
        self.platform.override("sequences", sequences)
        crm_list = self.store.insert(self.ctx, "lists", {"name": "Targets", "entity_type": "companies"})
        self.store.insert(self.ctx, "list_members", {"list_id": crm_list["id"], "entity_type": "companies",
                                                     "entity_id": self.company["id"]})
        self.save({"start": "n", "nodes": {
            "n": {"type": "action", "next": "r",
                  "action": {"type": "send_notification", "title": "Hi {company.name}", "severity": "success"}},
            "r": {"type": "action", "next": "s",
                  "action": {"type": "start_research", "question": "Who leads IT at {company.name}?"}},
            "s": {"type": "action", "next": "l", "action": {"type": "start_sequence", "sequence_id": "sq_x"}},
            "l": {"type": "action", "action": {"type": "remove_from_list", "list_id": crm_list["id"]}},
        }})
        [run] = self.fire()
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        self.assertEqual(self.notifications.sent[0]["title"], "Hi Acme")
        self.assertEqual(research.questions, ["Who leads IT at Acme?"])
        self.assertEqual(sequences.calls, [("sq_x", [self.contact["id"]], None)])
        self.assertEqual(run["history"][2]["result"]["status"], "pending_approval")
        self.assertEqual(self.store.count(self.ctx, "list_members"), 0)

    def test_notification_falls_back_to_the_table(self) -> None:
        class Broken:
            def notify(self, ctx, **kw):
                raise RuntimeError("down")

        self.platform.override("notifications", Broken())
        self.save({"start": "n", "nodes": {"n": {"type": "action", "action": {"type": "send_notification",
                                                                            "title": "Direct"}}}})
        [run] = self.fire()
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(self.store.first(self.ctx, "notifications", {"title": "Direct"})["kind"], "workflow")

    def test_validate_email_for_a_list_uses_validation_jobs(self) -> None:
        jobs = FakeEmailJobs()
        self.platform.override("email_jobs", jobs)
        crm_list = self.store.insert(self.ctx, "lists", {"name": "People", "entity_type": "contacts"})
        self.store.insert(self.ctx, "list_members", {"list_id": crm_list["id"], "entity_type": "contacts",
                                                     "entity_id": self.contact["id"]})
        self.save({"start": "v", "nodes": {"v": {"type": "action", "action": {
            "type": "validate_email", "list_id": crm_list["id"], "allow_paid": True}}}}, ctx=self.mctx)
        [run] = self.fire()
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        self.assertEqual(jobs.calls[0][1:], ([self.contact["id"]], True))

    def test_flat_workflows_support_wait(self) -> None:
        wf = self.engine.save_workflow(self.ctx, {"name": "flat", "trigger": "new_company", "conditions": [],
                                                  "actions": [{"type": "create_task", "title": "one"},
                                                              {"type": "wait", "hours": 3},
                                                              {"type": "create_task", "title": "two"}]})
        self.engine.save_workflow(self.ctx, {"enabled": True}, workflow_id=wf["id"])
        [run] = self.fire()
        self.assertEqual(run["status"], "waiting")
        self.assertEqual(self.store.count(self.ctx, "crm_tasks"), 1)
        self.make_due(run)
        self.engine.tick(self.ctx.as_system())
        self.run_pending()
        self.assertEqual(self.store.get(self.ctx, "workflow_runs", run["id"])["status"], "succeeded")
        self.assertEqual(self.store.count(self.ctx, "crm_tasks"), 2)

    # --- triggers ----------------------------------------------------------------------------------

    def test_manual_run_and_schedule(self) -> None:
        wf = self.save({"start": "t", "nodes": {"t": {"type": "action", "action": {"type": "create_task",
                                                                                 "title": "manual"}}}},
                       trigger="manual", enabled=False)
        with self.assertRaises(ForbiddenError):
            self.engine.run_now(self.vctx, wf["id"])
        run = self.engine.run_now(self.mctx, wf["id"], {"company_id": self.company["id"]})
        self.run_pending()
        self.assertEqual(self.store.get(self.ctx, "workflow_runs", run["id"])["status"], "succeeded")

        self.save({"start": "t", "schedule": {"every_minutes": 60},
                   "nodes": {"t": {"type": "action", "action": {"type": "create_task", "title": "tick"}}}},
                  trigger="schedule", name="hourly")
        system = self.ctx.as_system()
        now = utcnow()
        self.assertEqual(self.engine.tick(system, now=now), 1)
        self.assertEqual(self.engine.tick(system, now=now), 0)  # same hour bucket: no second run
        self.assertEqual(self.engine.tick(system, now=now + timedelta(hours=1)), 1)

    def test_cancel_and_resume_now(self) -> None:
        self.save({"start": "w", "nodes": {"w": {"type": "delay", "days": 1, "next": "t"},
                                           "t": {"type": "action", "action": {"type": "create_task"}}}})
        [run] = self.fire()
        self.engine.resume_now(self.mctx, run["id"])
        self.run_pending()
        self.assertEqual(self.store.get(self.ctx, "workflow_runs", run["id"])["status"], "succeeded")
        [run2] = self.fire(key="e2")
        self.assertEqual(self.engine.cancel_run(self.mctx, run2["id"])["status"], "cancelled")
        self.make_due(run2)
        self.assertEqual(self.engine.tick(self.ctx.as_system()), 0)


class WorkflowApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from cloud.api.auth import DevTokenIssuer
        from cloud.api.main import create_app
        from cloud.api.settings import Settings
        from cloud.intel.platform import PlatformConfig
        from cloud.shared.storage import LocalFileStorage
        from cloud.worker.dispatcher import NullDispatcher

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.platform = Platform(MemoryStore(), storage=LocalFileStorage(root / "platform"),
                                 config=PlatformConfig(files_dir=root / "platform"))
        issuer = DevTokenIssuer("workflow-api-tests-secret-0123456789abcdef")
        app = create_app(Settings(auth_mode="dev", results_dir=root / "results"),
                         storage=LocalFileStorage(root / "results"), token_verifier=issuer,
                         dispatcher=NullDispatcher(), platform=self.platform)
        self.client = TestClient(app)
        self.client.headers["Authorization"] = f"Bearer {issuer.issue('wf@example.com')['access_token']}"
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        r = self.client.post("/api/v1/workspaces", json={"name": "WF", "seed": False})
        self.assertEqual(r.status_code, 201, r.text)
        self.ws = r.json()["id"]
        self.base = f"/api/v1/w/{self.ws}"

    def run_pending(self):
        ctx = Ctx.for_system(self.ws)
        for task in self.platform.store.all(ctx, "platform_tasks", {"status": "queued"}, cap=50):
            run_task_inline(self.platform, self.ws, task["id"])

    def test_meta_templates_manual_run_and_approval(self) -> None:
        meta = self.client.get(self.base + "/workflow-meta").json()
        self.assertIn("schedule", meta["triggers"])
        self.assertIn("approval", meta["node_types"])
        templates = self.client.get(self.base + "/workflow-templates").json()["items"]
        self.assertGreaterEqual(len(templates), 5)
        r = self.client.post(self.base + "/workflow-templates/hiring_spike_task_notify", json={})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertFalse(r.json()["enabled"])
        self.assertEqual(self.client.post(self.base + "/workflow-templates/nope", json={}).status_code, 404)

        graph = {"start": "ask", "nodes": {
            "ask": {"type": "approval", "message": "go?", "next": "t"},
            "t": {"type": "action", "action": {"type": "create_task", "title": "via api"}}}}
        wf = self.client.post(self.base + "/workflows", json={"name": "api", "trigger": "manual",
                                                              "actions": [], "graph": graph}).json()
        run = self.client.post(self.base + f"/workflows/{wf['id']}/run", json={"payload": {}}).json()
        self.run_pending()
        runs = self.client.get(self.base + "/workflow-runs", params={"workflow_id": wf["id"]}).json()["items"]
        self.assertEqual(runs[0]["status"], "awaiting_approval")
        r = self.client.post(self.base + f"/workflow-runs/{run['id']}/approve", json={"note": "fine"})
        self.assertEqual(r.status_code, 200, r.text)
        self.run_pending()
        run_row = self.client.get(self.base + f"/workflow-runs/{run['id']}").json()
        self.assertEqual(run_row["status"], "succeeded")
        self.assertEqual(self.client.post(self.base + f"/workflow-runs/{run['id']}/approve").status_code, 422)

    def test_proposal_review_routes(self) -> None:
        company = self.client.post(self.base + "/companies", json={"name": "Api Co"}).json()
        graph = {"start": "u", "nodes": {"u": {"type": "action", "action": {
            "type": "update_company", "changes": {"industry": "Energy"}}}}}
        wf = self.client.post(self.base + "/workflows", json={"name": "p", "trigger": "manual", "actions": [],
                                                              "graph": graph}).json()
        self.client.post(self.base + f"/workflows/{wf['id']}/run", json={"payload": {"company_id": company["id"]}})
        self.run_pending()
        [proposal] = self.client.get(self.base + "/workflow-proposals").json()["items"]
        self.assertEqual(self.client.post(self.base + "/workflow-proposals/review",
                                          json={"ids": [proposal["id"]], "decision": "maybe"}).status_code, 422)
        self.client.post(self.base + "/workflow-proposals/review", json={"ids": [proposal["id"]],
                                                                         "decision": "approve"})
        applied = self.client.post(self.base + "/workflow-proposals/apply", json={"ids": [proposal["id"]]}).json()
        self.assertEqual(applied["items"][0]["status"], "applied")
        self.assertEqual(self.client.get(self.base + f"/companies/{company['id']}").json()["industry"], "Energy")


if __name__ == "__main__":
    unittest.main()
