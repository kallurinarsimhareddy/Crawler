"""The 0013 lifecycle on a real PostgreSQL: window-aware close phase, gone checks, the daily
evaluation, STALE / EXPIRED queries, signal + outcome tables and their workspace isolation."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

from cloud.tests._pg import PostgresTestCase


class _GoneHTTP:
    def __init__(self, statuses):
        self.statuses, self.calls = statuses, []

    def fetch(self, url):
        self.calls.append(url)
        return SimpleNamespace(status=self.statuses.get(url, 200), blocked=False, error=None)


class LifecyclePostgresTests(PostgresTestCase):
    def setUp(self) -> None:
        from cloud.intel.core.context import Ctx, utcnow
        from cloud.intel.platform import Platform, PlatformConfig
        from cloud.intel.store.postgres import PostgresStore

        self.store = PostgresStore.from_url(self.database_url, max_size=2)
        self.addCleanup(self.store.close)
        self.owner, self.stranger = str(uuid.uuid4()), str(uuid.uuid4())
        ws = self.store.create_workspace(self.owner, "A", f"lc-a-{uuid.uuid4().hex[:6]}")
        other = self.store.create_workspace(self.stranger, "B", f"lc-b-{uuid.uuid4().hex[:6]}")
        self.ctx = Ctx(ws["id"], self.owner, "owner")
        self.other = Ctx(other["id"], self.stranger, "owner")
        self.platform = Platform(self.store, config=PlatformConfig())
        self.svc = self.platform.service("job_monitors")
        self.now = utcnow()
        self.monitor = self.svc.create_monitor(self.ctx, {"source_url": "https://www.wearedevelopers.com/jobs?country=US",
                                                          "name": "WAD", "schedule": "daily"})
        started = self.now - timedelta(hours=2)
        self.run = self.store.insert(self.ctx, "job_monitor_runs", {"monitor_id": self.monitor["id"], "mode": "full",
                                                                    "status": "running", "started_at": started})
        old = started - timedelta(days=3)

        def job(n, listed_days, status="unknown"):
            return {"title": f"Job {n}", "job_url": f"https://www.wearedevelopers.com/jobs/ext/{n}-job",
                    "url_key": f"https://wearedevelopers.com/jobs/ext/{n}", "source_kind": "import", "source_name": "t",
                    "source": "WeAreDevelopers", "status": status, "first_seen_at": old, "last_seen_at": old,
                    "listing_date": (self.now - timedelta(days=listed_days)).date()}

        self.store.insert_many(self.ctx, "job_postings", [job(1, 10), job(2, 50), job(3, 120), job(4, 200)])

    def rows(self):
        return {r["url_key"].rsplit("/", 1)[1]: r for r in self.store.all(self.ctx, "job_postings", {})}

    def test_close_phase_gone_checks_and_queries(self) -> None:
        stats = self.svc.close_missing(self.ctx, self.monitor, self.run)
        self.assertEqual((stats["missed"], stats["expired"], stats["closed"]), (2, 2, 0))
        rows = self.rows()
        self.assertEqual([rows[k]["status"] for k in "1234"], ["unknown", "unknown", "expired", "expired"])
        self.assertEqual([rows[k]["missed_full_sweeps"] for k in "1234"], [1, 1, 0, 0])
        http = _GoneHTTP({rows["2"]["job_url"]: 410, rows["4"]["job_url"]: 410})
        gone = self.svc.check_gone(self.ctx, self.monitor, self.run, http=http)
        self.assertEqual((gone["checked"], gone["closed"], gone["live"]), (4, 2, 2))
        rows = self.rows()
        self.assertEqual([rows[k]["status"] for k in "1234"], ["unknown", "closed", "expired", "closed"])
        self.assertEqual(rows["4"]["closure_reason"], "source_gone")
        self.assertEqual(self.svc.check_gone(self.ctx, self.monitor, self.run, http=http)["checked"], 0)  # rechecked later
        kinds = sorted(c["change"] for c in self.store.all(self.ctx, "job_posting_changes", {}))
        self.assertEqual(kinds, ["closed", "closed", "expired", "expired"])
        self.assertEqual(self.svc.search_jobs(self.ctx, {"change": "expired"})["total"], 1)
        self.assertEqual(self.svc.search_jobs(self.ctx, {"change": "closed"})["total"], 2)
        self.assertEqual(self.svc.search_jobs(self.ctx, {"status": "EXPIRED,CLOSED"})["total"], 3)
        self.assertEqual(self.svc.search_jobs(self.other, {"change": "closed"})["total"], 0)   # isolation

    def test_daily_evaluation_and_scheduling(self) -> None:
        system = self.ctx.as_system()
        for row in self.store.all(self.ctx, "job_postings", {}):
            self.store.update(system, "job_postings", row["id"], {"status": "open"})
        self.assertEqual(self.svc.schedule_lifecycle(self.ctx), 1)
        self.assertEqual(self.svc.schedule_lifecycle(self.ctx), 0)
        result = self.svc.evaluate_lifecycle(self.ctx, self.monitor["id"])
        self.assertEqual(result["became_stale"], 3)                     # listed 50/120/200 days ago
        self.assertEqual(self.svc.search_jobs(self.ctx, {"change": "stale"})["total"], 3)
        self.assertEqual(self.store.get(self.ctx, "job_source_monitors", self.monitor["id"])
                         ["last_lifecycle_result"]["by_status"], {"ACTIVE": 1, "STALE": 3})

    def test_signal_outcome_tables_are_tenant_isolated(self) -> None:
        import psycopg

        outcomes = self.platform.service("signal_outcomes")
        signal = self.store.insert(self.ctx, "hiring_signals", {
            "company_name": "Acme", "signal_type": "HIRING_CLUSTER", "detected_at": self.now,
            "fingerprint": f"t:{uuid.uuid4().hex}", "status": "active", "technologies": ["SAP"]})
        self.assertIsNone(signal["company_id"])                          # 0013: no CRM company needed
        outcomes.record(self.ctx, signal["id"], "meeting", note="intro")
        contact = self.store.insert(self.ctx, "contacts", {"full_name": "Pat"})
        self.platform.service("signals").record_contact_snapshot(self.ctx, contact, provider="zoominfo",
                                                                 person={"zoominfo_id": "z", "company_name": "Acme"})
        with psycopg.connect(self.database_url) as conn:
            with conn.transaction():
                conn.execute("select set_config('request.jwt.claims', %s, true)",
                             [json.dumps({"sub": self.stranger, "role": "authenticated"})])
                conn.execute("set local role authenticated")
                for table in ("signal_outcomes", "contact_snapshots"):
                    self.assertEqual(conn.execute(f"select count(*) from careercloud.{table}").fetchone()[0], 0)
        self.assertEqual(len(outcomes.outcomes(self.ctx, signal["id"])), 1)
        self.assertEqual(self.store.get(self.ctx, "hiring_signals", signal["id"])["outcome"], "meeting")


class DashboardSeriesPostgresTests(LifecyclePostgresTests):
    """The dashboard time series counts per day in SQL (it used to load up to 20k full rows)."""

    def test_count_by_day_and_series(self) -> None:
        from datetime import timedelta as td

        today = str(self.now.date())
        counts = self.store.count_by_day(self.ctx, "job_postings", "created_at")
        self.assertEqual(counts, {today: 4})
        self.assertEqual(self.store.count_by_day(self.other, "job_postings", "created_at"), {})   # isolation
        series = self.platform.service("analytics").timeseries(self.ctx, "job_postings", 7)
        self.assertEqual(sum(p["count"] for p in series["points"]), 4)
        with self.assertRaises(Exception):
            self.store.count_by_day(self.ctx, "job_postings", "title")
        self.assertEqual(self.store.count_by_day(self.ctx, "job_postings", "created_at",
                                                 {"created_at__gte": self.now + td(days=1)}), {})
