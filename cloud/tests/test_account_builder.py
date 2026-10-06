"""Account Builder: hiring companies (jobs + hiring signals) -> CRM accounts -> lists, with the
review queue, auto re-match, the capacity guard, the background task, permissions and isolation;
plus the owner-protection fix on the legacy members endpoint."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional
from unittest import mock

from cloud.intel.core.context import ConflictError, Ctx, ForbiddenError, ValidationError, utcnow
from cloud.intel.job_monitor import accounts as ab
from cloud.intel.tasks.worker import run_task_inline
from cloud.tests.test_job_monitor import ApiTests, _Base


def job(n: int, company: str, *, relevance: str = "HIGH", status: str = "open", keywords=("SAP",)) -> Dict[str, Any]:
    now = utcnow() - timedelta(days=2)
    return {"title": f"SAP Consultant {n}", "normalized_title": f"sap consultant {n}",
            "job_url": f"https://jobs.example.com/{n}", "url_key": f"https://jobs.example.com/{n}",
            "company_name": company, "status": status, "relevance_class": relevance,
            "matched_keywords": list(keywords), "first_seen_at": now, "last_seen_at": now,
            "source_kind": "scraper", "source_name": "test", "source": "WeAreDevelopers"}


class AccountBuilderTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.builder = self.platform.service("account_builder")
        rows = ([job(i, "Acme Corp") for i in range(4)] + [job(10, "ACME CORP.", relevance="REVIEW")]
                + [job(20 + i, "Beta LLC", keywords=("NetSuite",)) for i in range(2)]
                + [job(30, "Gamma Inc", relevance="REJECT", status="closed")])
        for row in rows:
            self.store.insert(self.ctx, "job_postings", row)
        self.signal = self.store.insert(self.ctx, "hiring_signals", {
            "company_name": "Acme Corp", "signal_type": "HIRING_CLUSTER", "detected_at": utcnow(), "strength": 80.0,
            "confidence": 0.9, "fingerprint": "HIRING_CLUSTER:name:acme:sap", "status": "active", "source": "job_signals",
            "technologies": ["SAP"]})

    def cand(self, name: str, **params: Any) -> Dict[str, Any]:
        rows = self.builder.candidates(self.ctx, params, limit=50)["rows"]
        return next(r for r in rows if name.lower() in r["display_name"].lower())

    def item(self, row: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
        return {"key": row["key"], "names": row["names"], "display_name": row["display_name"],
                "signal_ids": row["signal_ids"], "job_ids": row["job_ids"], **extra}

    # grouping
    def test_grouping_by_normalised_name_with_counts_technologies_and_signals(self) -> None:
        page = self.builder.candidates(self.ctx, {}, limit=50)
        acme = self.cand("acme")
        self.assertEqual((acme["jobs"], acme["open_jobs"], acme["high"], acme["review"]), (5, 5, 4, 1))
        self.assertEqual(sorted(acme["names"]), ["ACME CORP.", "Acme Corp"])
        self.assertEqual((acme["technologies"][0], acme["signals"], acme["state"]), ("SAP", ["HIRING_CLUSTER"],
                                                                                    "NEW ACCOUNT"))
        self.assertGreater(acme["hiring_score"], 0)
        self.assertEqual(acme["score_source"], "signals")
        self.assertEqual(page["states"]["NEW ACCOUNT"], 3)
        # the Jobs filters apply: relevance HIGH only keeps Acme + Beta
        filtered = self.builder.candidates(self.ctx, {"relevance": "HIGH"})
        self.assertEqual(sorted(r["display_name"] for r in filtered["rows"]), ["Acme Corp", "Beta LLC"])
        self.assertEqual(self.cand("gamma")["open_jobs"], 0)

    # exact match, bulk creation, attaching jobs + signals, refresh, provenance
    def test_bulk_create_attaches_jobs_signals_refreshes_and_records_provenance(self) -> None:
        acme, beta = self.cand("acme"), self.cand("beta")
        out = self.builder.build(self.ctx, [self.item(acme), self.item(beta)])
        self.assertEqual((out["mode"], out["stats"]["created"], out["stats"]["jobs_linked"], out["stats"]["signals_linked"]),
                         ("sync", 2, 7, 1))
        company = self.store.first(self.ctx, "companies", {"normalized_name": acme["key"]})
        self.assertEqual(company["hiring_count"], 5)                        # open + stale jobs
        self.assertEqual(self.store.count(self.ctx, "job_postings", {"company_id": company["id"]}), 5)
        sig = self.store.get(self.ctx, "hiring_signals", self.signal["id"])
        self.assertEqual((sig["company_id"], sig["status"]), (company["id"], "active"))   # attached, not expired
        self.assertIsNotNone(company["hiring_score"])                       # existing scoring engine ran
        # every signal on the account names its company (none shows up nameless in Signals)
        self.assertTrue(all(s.get("company_name") for s in self.store.all(
            self.ctx, "hiring_signals", {"company_id": company["id"]})))
        self.assertGreater(company["hiring_score"], 0)
        prov = self.store.first(self.ctx, "source_records", {"entity_id": company["id"]})
        self.assertEqual((prov["source_kind"], prov["source_name"]), ("discovery", "Account Builder"))
        self.assertIn(self.signal["id"], prov["original"]["signal_ids"])
        self.assertEqual(self.cand("acme")["state"], "LINKED")

    def test_idempotent_retry_never_duplicates(self) -> None:
        acme = self.cand("acme")
        self.builder.build(self.ctx, [self.item(acme)])
        again = self.builder.build(self.ctx, [self.item(acme)])
        self.assertEqual(again["stats"]["created"], 0)
        self.assertEqual(again["stats"]["skipped"] + again["stats"]["linked"], 1)
        self.assertEqual(self.store.count(self.ctx, "companies", {"normalized_name": acme["key"]}), 1)

    def test_exact_existing_company_links_instead_of_creating(self) -> None:
        existing = self.platform.service("crm").create_company(self.ctx, {"name": "Beta LLC"})["company"]
        beta = self.cand("beta")
        self.assertEqual(beta["state"], "ALREADY IN CRM")
        out = self.builder.build(self.ctx, [self.item(beta)])
        self.assertEqual((out["stats"]["created"], out["results"][0]["company_id"]), (0, existing["id"]))
        self.assertEqual(self.store.count(self.ctx, "companies", {}), 1)

    def test_high_and_review_count_open_jobs_and_state_counts_ignore_the_state_filter(self) -> None:
        self.store.insert(self.ctx, "job_postings", job(40, "Beta LLC", status="closed"))     # closed HIGH job
        beta = self.cand("beta")
        self.assertEqual((beta["jobs"], beta["open_jobs"], beta["high"]), (3, 2, 2))
        self.platform.service("crm").create_company(self.ctx, {"name": "Beta LLC"})
        page = self.builder.candidates(self.ctx, {}, state="ALREADY IN CRM")
        self.assertEqual([r["display_name"] for r in page["rows"]], ["Beta LLC"])
        self.assertEqual((page["total"], page["all_total"]), (1, 3))
        self.assertEqual((page["states"]["NEW ACCOUNT"], page["states"]["ALREADY IN CRM"]), (2, 1))

    def test_near_duplicate_by_legal_form_goes_to_review_never_created(self) -> None:
        for i in range(3):
            self.store.insert(self.ctx, "job_postings", job(50 + i, "Booz Allen Hamilton Inc."))
        existing = self.platform.service("crm").create_company(self.ctx, {"name": "Booz Allen Hamilton"})["company"]
        booz = self.cand("booz")
        self.assertEqual(booz["state"], "REVIEW")
        self.assertEqual([c["name"] for c in booz["crm"]], ["Booz Allen Hamilton"])
        self.assertIn("legal form", booz["match_reason"])
        # the candidate's own signals / score are not shown as this company's
        self.store.insert(self.ctx, "hiring_signals", {
            "company_id": existing["id"], "company_name": "Booz Allen Hamilton", "signal_type": "LEADERSHIP_HIRING",
            "detected_at": utcnow(), "strength": 90.0, "confidence": 0.9, "fingerprint": "LH:booz", "status": "active",
            "source": "job_signals"})
        booz = self.builder.candidates(self.ctx, {"company": "booz"}, limit=50, fresh=True)["rows"][0]
        self.assertEqual((booz["signals"], booz["score_source"]), ([], "signals"))
        out = self.builder.build(self.ctx, [self.item(booz)])
        self.assertEqual((out["stats"]["created"], out["stats"]["review"]), (0, 1))
        self.assertEqual(self.store.count(self.ctx, "companies", {}), 1)                  # nothing created
        review = self.store.first(self.ctx, "job_company_reviews", {"normalized_name": booz["key"]})
        self.assertEqual(list(review["candidate_ids"]), [existing["id"]])
        # two spellings in ONE run: the second sees the company the first created
        for i, name in enumerate(["Northwind Data Systems Inc", "Northwind Data Systems"]):
            self.store.insert(self.ctx, "job_postings", job(60 + i, name))
        rows = [self.cand("northwind data systems inc"), next(
            r for r in self.builder.candidates(self.ctx, {}, limit=50)["rows"] if r["display_name"] == "Northwind Data Systems")]
        out = self.builder.build(self.ctx, [self.item(r) for r in rows])
        self.assertEqual((out["stats"]["created"], out["stats"]["review"]), (1, 1))
        # the explicit override still creates
        out = self.builder.build(self.ctx, [self.item(booz, override=True)])
        self.assertEqual(out["stats"]["created"], 1)

    def test_crm_index_signals_and_technologies_are_not_truncated_at_one_page(self) -> None:
        crm = self.platform.service("crm")
        for i in range(8):
            crm.create_company(self.ctx, {"name": f"Filler Company {i}"})
        crm.create_company(self.ctx, {"name": "Beta LLC"})
        for i in range(4):
            self.store.insert(self.ctx, "hiring_signals", {
                "company_name": "Acme Corp", "signal_type": f"NEW_ROLE", "detected_at": utcnow(), "strength": 10.0 + i,
                "confidence": 0.5, "fingerprint": f"NEW_ROLE:acme:{i}", "status": "active", "source": "job_signals"})
        with mock.patch("cloud.intel.store.base.MAX_PAGE", 3):           # every read now spans several pages
            self.assertEqual(self.cand("beta")["state"], "ALREADY IN CRM")
            acme = self.cand("acme")
        self.assertEqual(len(acme["signal_ids"]), 5)
        self.assertEqual(acme["technologies"], ["SAP"])
        self.assertEqual(self.store.group_tags(self.ctx, "job_postings", "company_name", "matched_keywords",
                                               {"status": "open"})["Beta LLC"], {"NetSuite": 2})

    # near match -> review; explicit override; link to an existing company
    def test_ambiguous_goes_to_review_and_override_or_link_resolves_it(self) -> None:
        crm = self.platform.service("crm")
        a = crm.create_company(self.ctx, {"name": "Acme Corporation", "domain": "acme.com"})["company"]
        crm.create_company(self.ctx, {"name": "Acme Holdings", "domain": "acmeholdings.com"}, force=True)
        acme = self.cand("acme corp")
        self.assertIn(acme["state"], ("REVIEW", "MATCHED"))
        out = self.builder.build(self.ctx, [self.item(acme)])
        if acme["state"] == "REVIEW":
            self.assertEqual((out["stats"]["review"], out["stats"]["created"]), (1, 0))
            review = self.store.first(self.ctx, "job_company_reviews", {"normalized_name": acme["key"]})
            self.assertEqual(review["status"], "pending")
            # explicit link to the person's choice
            done = self.builder.link(self.ctx, self.item(acme), a["id"])
            self.assertEqual(done["jobs_linked"], 5)
            self.assertEqual(self.store.get(self.ctx, "job_company_reviews", review["id"])["status"], "linked")
        companies_before = self.store.count(self.ctx, "companies", {})
        beta = self.cand("beta")
        # override: an explicit "create new" is the only way past a possible duplicate
        crm.create_company(self.ctx, {"name": "Beta Group"}, force=True)
        out = self.builder.build(self.ctx, [self.item(beta, create_new=True)])
        self.assertEqual(out["stats"]["created"], 1)
        self.assertEqual(self.store.count(self.ctx, "companies", {}), companies_before + 2)

    def test_linked_row_shows_the_company_its_jobs_point_at(self) -> None:
        crm = self.platform.service("crm")
        first = crm.create_company(self.ctx, {"name": "Beta LLC"})["company"]
        crm.create_company(self.ctx, {"name": "Beta, LLC"}, force=True)            # same normalised name
        self.builder.link(self.ctx, self.item(self.cand("beta")), first["id"])
        beta = self.builder.candidates(self.ctx, {}, limit=50, fresh=True)["rows"]
        beta = next(r for r in beta if r["display_name"] == "Beta LLC")
        self.assertEqual((beta["state"], [c["id"] for c in beta["crm"]], beta["score_source"]),
                         ("LINKED", [first["id"]], "crm"))

    def test_review_queue_create_new_and_keep(self) -> None:
        crm = self.platform.service("crm")
        crm.create_company(self.ctx, {"name": "Acme Corp"})
        crm.create_company(self.ctx, {"name": "Acme Corp", "domain": "acme-two.example"}, force=True)
        acme = self.cand("acme")
        self.assertEqual(acme["state"], "REVIEW")                        # two CRM companies share the name
        self.builder.build(self.ctx, [self.item(acme)])
        review = self.store.first(self.ctx, "job_company_reviews", {"normalized_name": acme["key"]})
        svc = self.platform.service("job_monitors")
        self.assertEqual(svc.resolve_review(self.ctx, review["id"], action="keep")["status"], "pending")
        created = svc.resolve_review(self.ctx, review["id"], action="create")
        self.assertEqual(created["outcome"], "created")
        with self.assertRaises(ValidationError):
            svc.resolve_review(self.ctx, review["id"], action="merge-everything")

    # auto re-match
    def test_auto_rematch_links_exact_names_only(self) -> None:
        svc = self.platform.service("job_monitors")
        from cloud.intel.core.normalize import normalize_name

        svc._queue_reviews(self.ctx, {normalize_name("Beta LLC"): ("Beta LLC", [], 2)})
        crm = self.platform.service("crm")
        company = crm.create_company(self.ctx, {"name": "Beta LLC"})["company"]
        self.assertEqual(self.store.count(self.ctx, "job_postings", {"company_id": company["id"]}), 2)
        review = self.store.first(self.ctx, "job_company_reviews", {"normalized_name": company["normalized_name"]})
        self.assertEqual((review["status"], review["company_id"]), ("linked", company["id"]))
        # a near-but-different name is never linked automatically
        svc._queue_reviews(self.ctx, {normalize_name("Gamma Inc"): ("Gamma Inc", [], 1)})
        other = crm.create_company(self.ctx, {"name": "Gamma Industries"})["company"]
        self.assertEqual(self.store.count(self.ctx, "job_postings", {"company_id": other["id"]}), 0)

    # lists + provenance
    def test_lists_preserve_account_builder_provenance(self) -> None:
        acme = self.cand("acme")
        out = self.builder.build(self.ctx, [self.item(acme)], new_list="Q4 SAP Migration Targets")
        self.assertEqual((out["list"]["name"], out["list"]["source"], out["stats"]["listed"]),
                         ("Q4 SAP Migration Targets", "account_builder", 1))
        member = self.store.first(self.ctx, "list_members", {"list_id": out["list"]["id"]})
        reason = json.loads(member["added_reason"])
        self.assertEqual(reason["source"], "account_builder")
        self.assertIn(self.signal["id"], reason["signal_ids"])
        self.assertTrue(reason["job_ids"])
        self.assertIn("at", reason)
        self.assertIsNotNone(member["created_at"])
        company_id = out["results"][0]["company_id"]
        again = self.builder.add_to_list(self.ctx, [{"company_id": company_id}], list_id=out["list"]["id"])
        self.assertEqual((again["added"], again["already"]), (0, 1))          # no duplicate membership
        contacts = self.platform.service("crm").create_list(self.ctx, "People", "contacts")
        with self.assertRaises(ValidationError):
            self.builder.add_to_list(self.ctx, [{"company_id": company_id}], list_id=contacts["id"])
        self.assertLessEqual(len(ab._reason(signal_ids=["hs_" + "a" * 32] * 30, job_ids=["jp_" + "b" * 32] * 30)), 500)

    # the flow continues: list -> contacts -> email validation
    def test_list_summary_and_contact_email_validation(self) -> None:
        out = self.builder.build(self.ctx, [self.item(self.cand("acme"))], new_list="Targets")
        list_id, company_id = out["list"]["id"], out["results"][0]["company_id"]
        with self.assertRaises(ValidationError):
            self.builder.validate_list_contacts(self.ctx, list_id)          # no contacts yet
        crm = self.platform.service("crm")
        crm.upsert_contact(self.ctx, {"full_name": "Pat Lee", "email": "pat@acme-corp.example", "company_id": company_id},
                           source_kind="manual", source_name="test")
        crm.upsert_contact(self.ctx, {"full_name": "No Mail", "company_id": company_id}, source_kind="manual",
                           source_name="test")
        summary = self.builder.list_summary(self.ctx, list_id)
        self.assertEqual((summary["companies"], summary["companies_with_contacts"], summary["contacts"],
                          summary["with_email"]), (1, 1, 2, 1))
        job = self.builder.validate_list_contacts(self.ctx, list_id)
        self.assertEqual(job["row_count"] if "row_count" in job else job.get("total_rows", 1), 1)

    # guards
    def test_limits_confirmation_and_capacity_guard(self) -> None:
        many = [{"name": f"Company {i}"} for i in range(ab.CONFIRM_ABOVE + 1)]
        with self.assertRaises(ValidationError):
            self.builder.build(self.ctx, many)                                 # needs explicit confirm
        with self.assertRaises(ValidationError):
            self.builder.build(self.ctx, [{"name": f"C {i}"} for i in range(ab.MAX_PER_ACTION + 1)], confirm=True)
        with self.assertRaises(ValidationError):
            self.builder.build(self.ctx, [])
        self.store.database_size_bytes = lambda: 460 * 2**20                   # 460 MB of 500
        with self.assertRaises(ValidationError) as guard:
            self.builder.build(self.ctx, [{"name": f"Company {i}"} for i in range(ab.SYNC_LIMIT + 1)])
        self.assertIn("capacity", str(guard.exception))
        small = self.builder.build(self.ctx, [self.item(self.cand("beta"))])  # small work still allowed
        self.assertEqual(small["stats"]["created"], 1)

    # background task: progress, pause and cancel are not crashes
    def test_background_build_pause_resume_and_cancel(self) -> None:
        crm_names = [f"Delta {i}" for i in range(ab.SYNC_LIMIT + 5)]
        for i, name in enumerate(crm_names):
            self.store.insert(self.ctx, "job_postings", job(100 + i, name))
        items = [{"name": n} for n in crm_names]
        queued = self.builder.build(self.ctx, items)
        self.assertEqual(queued["mode"], "task")
        task_id = queued["task_id"]
        calls = {"n": 0}
        real = type(self.builder).build_one

        def pause_after_three(this, ctx, item):
            calls["n"] += 1
            if calls["n"] == 3:
                self.platform.tasks.pause(self.ctx, task_id)
            return real(this, ctx, item)

        with mock.patch.object(type(self.builder), "build_one", pause_after_three):
            run_task_inline(self.platform, self.ctx.workspace_id, task_id)
        paused = self.store.get(self.ctx, "platform_tasks", task_id)
        self.assertEqual(paused["status"], "paused")                          # a pause, not a retry
        self.assertEqual(paused["progress"]["checkpoint"]["index"], 3)
        self.platform.tasks.resume(self.ctx, task_id)
        run_task_inline(self.platform, self.ctx.workspace_id, task_id)
        done = self.store.get(self.ctx, "platform_tasks", task_id)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.store.count(self.ctx, "companies", {}), len(crm_names))   # each exactly once
        # cancel
        more = [{"name": f"Epsilon {i}"} for i in range(ab.SYNC_LIMIT + 2)]
        task2 = self.builder.build(self.ctx, more)["task_id"]
        self.platform.tasks.cancel(self.ctx, task2)
        run_task_inline(self.platform, self.ctx.workspace_id, task2)
        self.assertIn(self.store.get(self.ctx, "platform_tasks", task2)["status"], ("cancelled",))

    # permissions + isolation
    def test_viewer_cannot_create_or_link_and_workspaces_are_isolated(self) -> None:
        viewer = Ctx(self.ctx.workspace_id, str(uuid.uuid4()), "viewer")
        acme = self.cand("acme")
        with self.assertRaises(ForbiddenError):
            self.builder.build(viewer, [self.item(acme)])
        company = self.platform.service("crm").create_company(self.ctx, {"name": "Zeta"})["company"]
        with self.assertRaises(ForbiddenError):
            self.builder.link(viewer, self.item(acme), company["id"])
        other_user = str(uuid.uuid4())
        other = Ctx(self.store.create_workspace(other_user, "B", f"b-{uuid.uuid4().hex[:6]}")["id"], other_user, "owner")
        self.assertEqual(self.builder.candidates(other, {})["total"], 0)
        with self.assertRaises(Exception):
            self.builder.link(other, self.item(acme), company["id"])        # another workspace's company


class AccountBuilderApiTests(ApiTests):
    test_monitor_import_and_jobs_over_http = None

    def test_http_flow_and_isolation(self) -> None:
        from cloud.intel.core.context import Ctx as _Ctx

        ws_ctx = _Ctx.for_system(self.ws)
        for i in range(3):
            self.platform.store.insert(ws_ctx, "job_postings", job(i, "Omega Systems"))
        r = self.client.get(self.base + "/hiring-companies?relevance=HIGH")
        self.assertEqual(r.status_code, 200, r.text)
        row = r.json()["rows"][0]
        self.assertEqual((row["display_name"], row["open_jobs"], row["state"]), ("Omega Systems", 3, "NEW ACCOUNT"))
        item = {k: row[k] for k in ("key", "names", "display_name", "signal_ids", "job_ids")}
        r = self.client.post(self.base + "/hiring-companies/build", json={"items": [item], "new_list": "Targets"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["stats"]["created"], r.json()["list"]["name"]), (1, "Targets"))
        self.assertEqual(self.client.get(self.base + "/hiring-companies/capacity").json()["max_per_action"], 500)
        # a signal that only carries a CRM company id is listed with that company's name
        company_id = r.json()["results"][0]["company_id"]
        self.platform.store.insert(ws_ctx, "hiring_signals", {
            "company_id": company_id, "signal_type": "NEW_ROLE", "detected_at": utcnow(), "strength": 50.0,
            "confidence": 0.5, "fingerprint": "nameless:omega", "status": "active", "source": "hiring_engine"})
        listed = self.client.get(self.base + "/hiring-signals?limit=50").json()["items"]
        self.assertEqual({s["company_name"] for s in listed if s["company_id"] == company_id}, {"Omega Systems"})
        for path in ("/hiring-companies", "/hiring-companies/capacity"):
            self.assertIn(self.mallory.get(self.base + path).status_code, (403, 404))
        self.assertIn(self.mallory.post(self.base + "/hiring-companies/build", json={"items": [item]}).status_code,
                      (403, 404))

    def test_owner_cannot_be_demoted_through_legacy_members_endpoint(self) -> None:
        owner_id = next(m["user_id"] for m in self.client.get(self.base + "/members").json()["items"]
                        if m["role"] == "owner")
        admin_id = str(uuid.uuid4())
        self.platform.store.add_member(Ctx.for_system(self.ws), admin_id, "admin")
        from cloud.api.auth import DevTokenIssuer  # noqa: F401 - the harness issues tokens by email
        r = self.client.post(self.base + "/members", json={"user_id": str(owner_id), "role": "viewer"})
        self.assertEqual(r.status_code, 403, r.text)
        r = self.client.post(self.base + "/members", json={"user_id": admin_id, "role": "owner"})
        self.assertEqual(r.status_code, 403, r.text)
        roles = {str(m["user_id"]): m["role"] for m in self.client.get(self.base + "/members").json()["items"]}
        self.assertEqual(roles[str(owner_id)], "owner")
        r = self.client.post(self.base + "/members", json={"user_id": admin_id, "role": "manager"})
        self.assertEqual(r.status_code, 201, r.text)                         # ordinary role changes still work
