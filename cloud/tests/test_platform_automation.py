"""Track I: the automation engine — idempotent emit, conditions, actions, safety rails, retries."""

from __future__ import annotations

import unittest
import uuid

from cloud.intel.automation.engine import AutomationEngine, evaluate
from cloud.intel.core.context import Ctx, ValidationError
from cloud.intel.platform import Platform
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks.worker import run_task_inline
from cloud.tests.test_platform_gtm import StubCrm


class StubContacts:
    def __init__(self):
        self.calls = []

    def find_contacts(self, ctx, company_ids, **kw):
        self.calls.append((list(company_ids), kw))
        return {"found": 0}

    def gap_analysis(self, ctx, company_id, functions=("hr", "it", "executive")):
        return {"company_id": company_id}


class StubEmail:
    def __init__(self):
        self.calls = []

    def validate(self, ctx, emails, **kw):
        self.calls.append((list(emails), kw))
        return [{"email": e, "status": "VALID"} for e in emails]


class Response:
    def __init__(self, status_code):
        self.status_code = status_code


class AutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.platform = Platform(self.store)
        self.user = str(uuid.uuid4())
        ws = self.store.create_workspace(self.user, "Auto", "auto-ws")
        self.ctx = Ctx(ws["id"], self.user, "owner")
        self.posts = []
        self.post_status = 200

        def post(url, **kw):
            self.posts.append((url, kw))
            return Response(self.post_status)

        public = [(2, 1, 6, "", ("93.184.216.34", 443))]
        self.engine = AutomationEngine(self.platform, http_post=post, resolver=lambda *a, **k: public)
        self.platform.override("automation", self.engine)
        self.platform.override("crm", StubCrm(self.platform))
        self.contacts = StubContacts()
        self.email = StubEmail()
        self.platform.override("contacts", self.contacts)
        self.platform.override("email", self.email)
        self.company = self.store.insert(self.ctx, "companies", {"name": "Acme", "industry": "Manufacturing",
                                                                 "account_score": 80})
        self.contact = self.store.insert(self.ctx, "contacts", {"company_id": self.company["id"],
                                                                "full_name": "Jo Lee", "email": "jo@acme.example"})

    def workflow(self, actions, conditions=None, trigger="new_company", enabled=True, ctx=None, **extra):
        values = {"name": "wf", "trigger": trigger, "conditions": conditions or [], "actions": actions, **extra}
        wf = self.engine.save_workflow(ctx or self.ctx, values)
        if enabled:
            wf = self.engine.save_workflow(ctx or self.ctx, {"enabled": True}, workflow_id=wf["id"])
        return wf

    def fire(self, key="evt-1", trigger="new_company", payload=None):
        runs = self.engine.emit(self.ctx, trigger, key, payload or {"company_id": self.company["id"]})
        results = []
        for run in runs:
            run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
            results.append(self.store.get(self.ctx, "workflow_runs", run["id"]))
        return results

    # --- conditions --------------------------------------------------------------------

    def test_condition_evaluation(self) -> None:
        data = {"company": {"industry": "Manufacturing", "account_score": 80, "technologies": ["SAP", "RPG"]},
                "payload": {"x": None}}
        self.assertTrue(evaluate({"all": [{"field": "company.industry", "op": "eq", "value": "manufacturing"},
                                          {"field": "company.account_score", "op": "gte", "value": 70}]}, data))
        self.assertFalse(evaluate([{"field": "company.account_score", "op": "gt", "value": 90}], data))
        self.assertTrue(evaluate({"any": [{"field": "company.account_score", "op": "gt", "value": 90},
                                          {"field": "company.technologies", "op": "contains", "value": "rpg"}]}, data))
        self.assertTrue(evaluate({"field": "company.technologies", "op": "in", "value": ["SAP"]}, data))
        self.assertFalse(evaluate({"field": "payload.x", "op": "exists"}, data))
        self.assertFalse(evaluate({"field": "company.missing", "op": "eq", "value": 1}, data))
        self.assertTrue(evaluate([], data))
        with self.assertRaises(ValidationError):
            self.engine.save_workflow(self.ctx, {"name": "bad", "trigger": "new_company",
                                                 "conditions": [{"field": "x", "op": "regex"}], "actions": []})
        with self.assertRaises(ValidationError):
            self.engine.save_workflow(self.ctx, {"name": "bad", "trigger": "nope", "actions": []})
        with self.assertRaises(ValidationError):
            self.engine.save_workflow(self.ctx, {"name": "bad", "trigger": "new_company",
                                                 "actions": [{"type": "send_email_now"}]})

    # --- emit ---------------------------------------------------------------------------------

    def test_new_workflows_are_disabled_and_ignored(self) -> None:
        wf = self.workflow([{"type": "create_task", "title": "x"}], enabled=False)
        self.assertFalse(wf["enabled"])
        self.assertEqual(self.engine.emit(self.ctx, "new_company", "e", {}), [])

    def test_emit_is_idempotent_per_event_key(self) -> None:
        self.workflow([{"type": "create_task", "title": "Research {company.name}"}])
        first = self.fire("evt-1")
        self.assertEqual(first[0]["status"], "succeeded")
        self.assertEqual(self.engine.emit(self.ctx, "new_company", "evt-1", {"company_id": self.company["id"]}), [])
        self.assertEqual(self.store.count(self.ctx, "crm_tasks"), 1)
        self.assertEqual(self.store.first(self.ctx, "crm_tasks", {})["title"], "Research Acme")
        self.fire("evt-2")
        self.assertEqual(self.store.count(self.ctx, "crm_tasks"), 2)

    def test_conditions_gate_actions(self) -> None:
        self.workflow([{"type": "create_task", "title": "x"}],
                      conditions={"all": [{"field": "company.account_score", "op": "gte", "value": 90}]})
        [run] = self.fire()
        self.assertEqual(run["status"], "skipped")
        self.assertEqual(self.store.count(self.ctx, "crm_tasks"), 0)

    def test_max_runs_per_day(self) -> None:
        self.workflow([{"type": "create_task", "title": "x"}], max_runs_per_day=1)
        self.fire("a")
        self.assertEqual(self.engine.emit(self.ctx, "new_company", "b", {"company_id": self.company["id"]}), [])

    # --- actions -----------------------------------------------------------------------------

    def test_crm_actions(self) -> None:
        lst = self.store.insert(self.ctx, "lists", {"name": "Hot", "entity_type": "companies"})
        self.platform.service("campaigns").ensure_defaults(self.ctx)
        campaign = self.store.first(self.ctx, "campaigns", {"key": "cox-little"})
        owner = str(uuid.uuid4())
        self.workflow([
            {"type": "assign_owner", "owner_id": owner},
            {"type": "add_to_list", "list_id": lst["id"]},
            {"type": "assign_campaign", "campaign_id": campaign["id"]},
            {"type": "create_opportunity", "title": "Acme ERP", "campaign_id": campaign["id"]},
        ])
        [run] = self.fire()
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        company = self.store.get(self.ctx, "companies", self.company["id"])
        self.assertEqual(company["owner_id"], owner)
        self.assertIn("campaign:cox-little", company["tags"])
        self.assertEqual(self.store.count(self.ctx, "list_members", {"list_id": lst["id"]}), 1)
        self.assertEqual(self.store.first(self.ctx, "opportunities", {})["campaign_id"], campaign["id"])
        self.assertEqual(len(run["steps"]), 4)
        self.assertTrue(self.store.count(self.ctx, "audit_log", {"action": "workflow.run"}))

    def test_enrichment_and_validation_never_pay_unless_admin_allowed(self) -> None:
        member = str(uuid.uuid4())
        self.store.add_member(self.ctx, member, "member")
        member_ctx = Ctx(self.ctx.workspace_id, member, "member")
        self.workflow([{"type": "find_contacts", "allow_paid": True},
                       {"type": "validate_email", "email": "jo@acme.example", "allow_paid": True},
                       {"type": "enrich_company", "allow_paid": True}], ctx=member_ctx, enabled=False)
        wf = self.store.first(self.ctx, "workflows", {})
        self.engine.save_workflow(member_ctx, {"enabled": True}, workflow_id=wf["id"])
        [run] = self.fire()
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        self.assertFalse(self.contacts.calls[0][1]["allow_paid"])
        self.assertFalse(self.email.calls[0][1]["allow_paid"])
        enrich = self.store.first(self.ctx, "platform_tasks", {"kind": "enrichment"})
        self.assertFalse(enrich["params"]["allow_paid"])

    def test_queue_sequence_only_creates_pending_approval(self) -> None:
        template = self.platform.service("sequences").create_template(self.ctx, name="t", subject="s", body="b")
        sequence = self.store.insert(self.ctx, "sequences", {"name": "S"})
        self.platform.service("sequences").add_step(self.ctx, sequence["id"], template_id=template["id"])
        self.workflow([{"type": "queue_sequence", "sequence_id": sequence["id"]}], trigger="new_contact")
        [run] = self.fire(trigger="new_contact", payload={"contact_id": self.contact["id"]})
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        enrollment = self.store.first(self.ctx, "sequence_enrollments", {})
        self.assertEqual(enrollment["status"], "pending_approval")
        self.assertIsNone(enrollment["approved_by"])

    def test_export_action_submits_a_task(self) -> None:
        self.workflow([{"type": "export", "entity_type": "companies", "format": "xlsx"}])
        [run] = self.fire()
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(self.store.count(self.ctx, "platform_tasks", {"kind": "export"}), 1)

    def test_webhook_refuses_private_addresses(self) -> None:
        self.workflow([{"type": "webhook", "url": "http://169.254.169.254/latest/meta-data"}])
        [run] = self.fire()
        self.assertEqual(run["status"], "failed")
        self.assertIn("public", run["error"])
        self.assertEqual(self.posts, [])

    def test_webhook_posts_signed_json_to_public_host(self) -> None:
        import os
        from unittest import mock

        self.workflow([{"type": "webhook", "url": "https://hooks.example.com/in"}])
        public = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with mock.patch.dict(os.environ, {"CAREERCLOUD_WEBHOOK_SIGNING_SECRET": "s3cret"}), \
                mock.patch("cloud.shared.urls.socket.getaddrinfo", return_value=public):
            [run] = self.fire()
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        url, kw = self.posts[0]
        self.assertEqual(url, "https://hooks.example.com/in")
        self.assertTrue(kw["headers"]["X-CareerCrawler-Signature"].startswith("sha256="))
        self.assertFalse(kw["allow_redirects"])

    def test_failure_is_recorded_and_retried_by_the_task_queue(self) -> None:
        self.post_status = 500
        self.workflow([{"type": "create_task", "title": "first"},
                       {"type": "webhook", "url": "https://hooks.example.com/in"}])
        [run] = self.fire()
        self.assertEqual(run["status"], "failed")
        self.assertEqual([s["status"] for s in run["steps"]], ["succeeded", "failed"])
        task = self.store.first(self.ctx, "platform_tasks", {"kind": "workflow"})
        self.assertEqual(task["status"], "retrying")
        # a retry resumes after the successful step: the CRM task is not duplicated
        self.post_status = 200
        self.engine.execute_run(self.ctx, run["id"])
        self.assertEqual(self.store.get(self.ctx, "workflow_runs", run["id"])["status"], "succeeded")
        self.assertEqual(self.store.count(self.ctx, "crm_tasks"), 1)

    def test_dry_run_changes_nothing(self) -> None:
        wf = self.workflow([{"type": "create_task", "title": "x"}])
        result = self.engine.dry_run(self.ctx, wf["id"], {"company_id": self.company["id"]})
        self.assertTrue(result["conditions_met"])
        self.assertEqual(self.store.count(self.ctx, "crm_tasks"), 0)
        self.assertEqual(self.store.count(self.ctx, "workflow_runs"), 0)


if __name__ == "__main__":
    unittest.main()
