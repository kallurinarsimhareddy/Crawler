"""Account Builder on a real PostgreSQL: grouped counts, build + link, list provenance, the
database-size probe, and tenant isolation of the linked rows."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

from cloud.tests._pg import PostgresTestCase


class AccountBuilderPostgresTests(PostgresTestCase):
    def setUp(self) -> None:
        from cloud.intel.core.context import Ctx, utcnow
        from cloud.intel.platform import Platform, PlatformConfig
        from cloud.intel.store.postgres import PostgresStore

        self.store = PostgresStore.from_url(self.database_url, max_size=2)
        self.addCleanup(self.store.close)
        self.owner, self.stranger = str(uuid.uuid4()), str(uuid.uuid4())
        ws = self.store.create_workspace(self.owner, "A", f"ab-a-{uuid.uuid4().hex[:6]}")
        other = self.store.create_workspace(self.stranger, "B", f"ab-b-{uuid.uuid4().hex[:6]}")
        self.ctx = Ctx(ws["id"], self.owner, "owner")
        self.other = Ctx(other["id"], self.stranger, "owner")
        self.platform = Platform(self.store, config=PlatformConfig())
        self.builder = self.platform.service("account_builder")
        now = utcnow() - timedelta(days=1)

        def job(n, company, relevance="HIGH"):
            return {"title": f"ERP Analyst {n}", "job_url": f"https://jobs.example.com/{n}",
                    "url_key": f"https://jobs.example.com/{n}", "company_name": company, "status": "open",
                    "relevance_class": relevance, "matched_keywords": ["SAP", "ERP"], "source_kind": "scraper",
                    "source_name": "t", "first_seen_at": now, "last_seen_at": now}

        self.store.insert_many(self.ctx, "job_postings", [job(i, "Northwind Traders") for i in range(3)]
                               + [job(9, "NORTHWIND TRADERS", "REVIEW"), job(10, "Contoso Ltd")])
        self.store.insert(self.ctx, "hiring_signals", {
            "company_name": "Northwind Traders", "signal_type": "STACK_MIGRATION", "detected_at": now,
            "strength": 70.0, "confidence": 0.8, "fingerprint": f"t:{uuid.uuid4().hex}", "status": "active",
            "source": "job_signals"})

    def test_build_link_list_and_isolation(self) -> None:
        import psycopg

        page = self.builder.candidates(self.ctx, {}, limit=10)
        north = next(r for r in page["rows"] if r["display_name"].lower().startswith("northwind"))
        self.assertEqual((north["jobs"], north["high"], north["review"], north["signals"]),
                         (4, 3, 1, ["STACK_MIGRATION"]))
        self.assertEqual(sorted(north["technologies"]), ["ERP", "SAP"])
        self.assertEqual(self.store.group_tags(self.ctx, "job_postings", "company_name", "matched_keywords",
                                               {"company_name": ["Contoso Ltd"]}), {"Contoso Ltd": {"SAP": 1, "ERP": 1}})
        self.assertEqual(len(self.store.walk(self.ctx, "job_postings", {"status": "open"})), 5)
        slim = self.store.rows(self.ctx, "job_postings", {"company_name": ["Contoso Ltd"]}, columns=("company_name",))
        self.assertEqual([sorted(r) for r in slim], [["company_name", "id"]])
        self.assertEqual(len(self.store.walk(self.ctx, "job_postings", {"status": "open"}, columns=("status",))), 5)
        item = {k: north[k] for k in ("key", "names", "display_name", "signal_ids", "job_ids")}
        out = self.builder.build(self.ctx, [item], new_list="ERP migration targets")
        self.assertEqual((out["stats"]["created"], out["stats"]["jobs_linked"], out["stats"]["signals_linked"],
                          out["stats"]["listed"]), (1, 4, 1, 1))
        company = self.store.get(self.ctx, "companies", out["results"][0]["company_id"])
        self.assertEqual(company["hiring_count"], 4)
        member = self.store.first(self.ctx, "list_members", {"list_id": out["list"]["id"]})
        self.assertEqual(json.loads(member["added_reason"])["source"], "account_builder")
        self.assertGreater(self.store.database_size_bytes(), 1_000_000)
        self.assertIsNotNone(self.builder.capacity()["size_mb"])
        self.assertEqual(self.builder.candidates(self.other, {})["total"], 0)
        with psycopg.connect(self.database_url) as conn:
            with conn.transaction():
                conn.execute("select set_config('request.jwt.claims', %s, true)",
                             [json.dumps({"sub": self.stranger, "role": "authenticated"})])
                conn.execute("set local role authenticated")
                for table in ("companies", "list_members", "lists"):
                    self.assertEqual(conn.execute(f"select count(*) from careercloud.{table}").fetchone()[0], 0)

    def test_update_where_is_scoped_and_checked(self) -> None:
        from cloud.intel.core.context import Ctx, ForbiddenError, ValidationError

        with self.assertRaises(ValidationError):
            self.store.update_where(self.ctx, "job_postings", {}, {"company_match": "matched"})
        viewer_id = str(uuid.uuid4())
        self.store.add_member(self.ctx, viewer_id, "viewer")
        with self.assertRaises(ForbiddenError):
            self.store.update_where(Ctx(self.ctx.workspace_id, viewer_id, "viewer"), "job_postings",
                                    {"company_name": ["Contoso Ltd"]}, {"company_match": "matched"})
        # another workspace's owner matches nothing here (workspace scope + RLS)
        self.assertEqual(self.store.update_where(self.other, "job_postings", {"company_name": ["Contoso Ltd"]},
                                                 {"company_match": "matched"}), 0)
        self.assertEqual(self.store.update_where(self.ctx, "job_postings", {"company_name": ["Contoso Ltd"]},
                                                 {"company_match": "matched"}), 1)
        self.assertEqual(self.store.first(self.ctx, "job_postings", {"company_name": "Contoso Ltd"})["company_match"],
                         "matched")
