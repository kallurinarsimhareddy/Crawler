"""The unified job write path and history retention on a real PostgreSQL (RLS, the unique
``(workspace_id, url_key)`` index, batched inserts and deletes)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from cloud.tests._pg import PostgresTestCase


class WritePathPostgresTests(PostgresTestCase):
    def setUp(self) -> None:
        from cloud.intel.core.context import Ctx
        from cloud.intel.platform import Platform, PlatformConfig
        from cloud.intel.store.postgres import PostgresStore

        self.store = PostgresStore.from_url(self.database_url, max_size=2)
        self.addCleanup(self.store.close)
        self.owner, self.stranger = str(uuid.uuid4()), str(uuid.uuid4())
        ws = self.store.create_workspace(self.owner, "A", f"wp-a-{uuid.uuid4().hex[:6]}")
        other = self.store.create_workspace(self.stranger, "B", f"wp-b-{uuid.uuid4().hex[:6]}")
        self.ctx = Ctx(ws["id"], self.owner, "owner")
        self.other = Ctx(other["id"], self.stranger, "owner")
        self.platform = Platform(self.store, config=PlatformConfig())
        self.jobs = self.platform.service("jobs")
        self.monitors = self.platform.service("job_monitors")
        self.acme = self.store.insert(self.ctx, "companies", {"name": "Acme", "domain": "acme.com"})

    def posting(self, n, title="SAP Basis Engineer"):
        return {"title": title, "job_url": f"https://acme.com/careers/{n}", "location": "Tulsa, OK"}

    def test_one_row_per_url_history_and_close_on_postgres(self) -> None:
        from cloud.intel.job_monitor.schema import normalize_job
        from cloud.intel.core.context import utcnow

        cid = self.acme["id"]
        wad = "https://www.wearedevelopers.com/jobs/ext/77-sap"
        record, _ = normalize_job({"job_url": wad, "title": "SAP Dev", "company_name": "Acme"}, source="WAD")
        self.monitors.upsert_batch(self.ctx, [record], observed_at=utcnow(), source_kind="scraper",
                                   source_name="WAD", run={"id": "jmr_pg"}, monitor={"id": "jmon_pg"})
        self.jobs.ingest_postings(self.ctx, [{"title": "SAP Dev", "job_url": wad + "-renamed", "company": "Acme"}],
                                  source_kind="external_source", source_name="ats_public")
        self.jobs.ingest_postings(self.ctx, [self.posting(1), self.posting(2), self.posting(2)],
                                  source_kind="crawler", source_name="careercrawler", company_id=cid,
                                  crawled_company_ids={cid})
        stats = self.jobs.ingest_postings(self.ctx, [self.posting(1)], source_kind="crawler",
                                          source_name="careercrawler", company_id=cid, crawled_company_ids={cid})
        self.assertEqual(stats["closed"], 1)
        self.assertEqual(self.store.count(self.ctx, "job_postings"), 3)
        changes = {c["change"] for c in self.store.all(self.ctx, "job_posting_changes", {})}
        self.assertEqual(changes, {"new", "closed"})
        self.assertEqual(self.store.count(self.other, "job_postings"), 0)       # isolation holds

    def test_prune_history_deletes_on_postgres_and_only_in_its_workspace(self) -> None:
        from cloud.intel.core.context import utcnow

        cid = self.acme["id"]
        self.jobs.ingest_postings(self.ctx, [self.posting(1), self.posting(2)], source_kind="crawler",
                                  source_name="careercrawler", company_id=cid, crawled_company_ids={cid})
        self.jobs.ingest_postings(self.ctx, [self.posting(1)], source_kind="crawler", source_name="careercrawler",
                                  company_id=cid, crawled_company_ids={cid})
        old = utcnow() - timedelta(days=400)
        system = self.ctx.as_system()
        self.store.update_many(system, "job_posting_changes",
                               [(r["id"], {"detected_at": old}) for r in self.store.all(system, "job_posting_changes", {})])
        other_job = self.store.insert(self.other, "job_postings", {
            "title": "X", "job_url": "https://b.example/1", "url_key": "https://b.example/1", "company_name": "B",
            "source_kind": "crawler", "source_name": "t", "status": "open", "first_seen_at": old, "last_seen_at": old})
        self.store.insert(self.other.as_system(), "job_posting_changes", {
            "job_posting_id": other_job["id"], "change": "new", "changed_fields": [], "before": {}, "after": {},
            "detected_at": old})
        dry = self.monitors.prune_history(self.ctx, older_than_days=365)
        self.assertEqual(dry["matched"], {"new": 2, "stale": 0})
        done = self.monitors.prune_history(self.ctx, older_than_days=365, dry_run=False)
        self.assertEqual(done["deleted"], 2)
        self.assertEqual([c["change"] for c in self.store.all(system, "job_posting_changes", {})], ["closed"])
        self.assertEqual(self.store.count(self.other.as_system(), "job_posting_changes"), 1)
