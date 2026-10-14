"""One write path for job_postings (audit 2026-10-14, group G1).

``JobIntelService.ingest_postings`` (crawler, external sources, scraper, API) used to key
postings by ``canonical_job_url`` while monitors and imports used ``job_url_key`` (site
profile aware), overwrite a monitor's status and source on every match, and never write
``job_posting_changes`` or keyword-set relevance. It now persists through
``JobMonitorService.upsert_batch``; these tests pin the shared rules.
"""

from __future__ import annotations

import unittest
import uuid
from datetime import timedelta

from cloud.intel.core.context import Ctx, utcnow
from cloud.intel.job_monitor.schema import job_url_key, normalize_job
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.store.memory import MemoryStore
from cloud.tests.test_job_relevance import workbook_bytes

WAD = "https://www.wearedevelopers.com/jobs/ext/3145489-senior-sap-consultant"
WAD_RENAMED = "https://www.wearedevelopers.com/jobs/ext/3145489-lead-sap-consultant?utm_source=x"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        owner = str(uuid.uuid4())
        ws = self.store.create_workspace(owner, "W", f"w-{uuid.uuid4().hex[:6]}")
        self.ctx = Ctx(ws["id"], owner, "owner")
        self.platform = Platform(self.store, config=PlatformConfig(environment="test"))
        self.jobs = self.platform.service("jobs")
        self.monitors = self.platform.service("job_monitors")
        self.acme = self.store.insert(self.ctx, "companies", {"name": "Acme", "domain": "acme.com"})

    def ingest(self, postings, *, source_name="careercrawler", source_kind="crawler", crawled=None, **kw):
        return self.jobs.ingest_postings(self.ctx, postings, source_kind=source_kind, source_name=source_name,
                                         crawled_company_ids=crawled, **kw)

    def posting(self, n, title="SAP FICO Consultant", **extra):
        return {"title": title, "job_url": f"https://acme.com/careers/{n}?utm_source=li", "location": "Tulsa, OK",
                "description": "SAP S/4HANA finance", **extra}

    def history(self, change=None):
        return self.store.all(self.ctx, "job_posting_changes", {"change": change} if change else {})

    def observe_by_monitor(self, url, title="Senior SAP Consultant", **values):
        record, problems = normalize_job({"job_url": url, "title": title, "company_name": "Acme",
                                          "location": "Remote", **values}, source="WeAreDevelopers")
        self.assertEqual(problems, [])
        return self.monitors.upsert_batch(self.ctx, [record], observed_at=utcnow(), source_kind="scraper",
                                          source_name="WeAreDevelopers US Jobs", run={"id": "jmr_test"},
                                          monitor={"id": "jmon_test"})

    def only_job(self):
        rows = self.store.all(self.ctx, "job_postings", {})
        self.assertEqual(len(rows), 1, [r["job_url"] for r in rows])
        return rows[0]

class WritePathTests(_Base):
    # --- identity ------------------------------------------------------------------------

    def test_one_url_identity_across_monitor_and_crawl(self) -> None:
        self.observe_by_monitor(WAD)
        stats = self.ingest([{"title": "Senior SAP Consultant", "job_url": WAD_RENAMED, "company": "Acme"}],
                            source_kind="external_source", source_name="ats_public")
        self.assertEqual((stats["inserted"], stats["updated"]), (0, 1))
        job = self.only_job()
        self.assertEqual(job["url_key"], job_url_key(WAD))
        self.assertEqual(job["url_key"], "https://wearedevelopers.com/jobs/ext/3145489")

    def test_repeated_ingest_and_in_batch_duplicates_store_one_row(self) -> None:
        first = self.ingest([self.posting(1), self.posting(1), {**self.posting(1), "job_url": "https://WWW.acme.com/careers/1/"}],
                            company_id=self.acme["id"])
        self.assertEqual(first["inserted"], 1)
        for _ in range(3):
            again = self.ingest([self.posting(1)], company_id=self.acme["id"])
            self.assertEqual((again["inserted"], again["updated"]), (0, 1))
        self.assertEqual(self.only_job()["observation_count"], 4)
        self.assertEqual(len(self.history("new")), 1)
        self.assertEqual(len(self.history("changed")), 0)

    # --- attribution and status ----------------------------------------------------------

    def test_a_crawl_never_rewrites_a_monitors_job(self) -> None:
        self.observe_by_monitor(WAD)
        job = self.only_job()
        self.store.update(self.ctx, "job_postings", job["id"], {"status": "stale", "stale_at": utcnow()})
        self.ingest([{"title": "Senior SAP Consultant", "job_url": WAD, "company": "Acme", "ats": "Greenhouse",
                      "department": "Finance"}], source_kind="external_source", source_name="ats_public")
        job = self.only_job()
        self.assertEqual(job["status"], "stale")                   # was forced back to "open" before
        self.assertEqual((job["source_kind"], job["source_name"]), ("scraper", "WeAreDevelopers US Jobs"))
        self.assertEqual(job["source_monitor_id"], "jmon_test")
        self.assertEqual(job["ats"], "Greenhouse")                 # a blank is filled
        self.assertNotEqual(job["department"], "Finance")          # an owned value is kept

    def test_status_transitions_and_their_history(self) -> None:
        cid = self.acme["id"]
        self.ingest([self.posting(1), self.posting(2)], company_id=cid, crawled={cid})
        self.assertEqual(self.store.count(self.ctx, "job_postings", {"status": "open"}), 2)
        closed = self.ingest([self.posting(1)], company_id=cid, crawled={cid})
        self.assertEqual(closed["closed"], 1)
        gone = self.store.first(self.ctx, "job_postings", {"status": "closed"})
        self.assertIsNotNone(gone["closed_at"])
        [row] = self.history("closed")
        self.assertEqual((row["job_posting_id"], row["after"]["reason"]), (gone["id"], "missing_from_board"))
        reopened = self.ingest([self.posting(1), self.posting(2)], company_id=cid, crawled={cid})
        self.assertEqual(reopened["reopened"], 1)
        job = self.store.get(self.ctx, "job_postings", gone["id"])
        self.assertEqual(job["status"], "open")
        self.assertIsNone(job["closed_at"])
        self.assertIsNotNone(job["reopened_at"])
        self.assertEqual(len(self.history("reopened")), 1)
        self.assertEqual(self.store.count(self.ctx, "job_postings"), 2)

    def test_an_expired_job_seen_again_is_reopened(self) -> None:
        self.ingest([self.posting(1)], company_id=self.acme["id"])
        job = self.only_job()
        self.store.update(self.ctx, "job_postings", job["id"], {"status": "expired", "expired_at": utcnow()})
        stats = self.ingest([self.posting(1)], company_id=self.acme["id"])
        self.assertEqual(stats["reopened"], 1)
        self.assertEqual(self.only_job()["status"], "open")

    def test_a_crawl_closes_only_its_own_jobs(self) -> None:
        cid = self.acme["id"]
        self.ingest([self.posting(1)], company_id=cid, source_kind="external_source", source_name="ats_public")
        stats = self.ingest([], company_id=cid, crawled={cid})
        self.assertEqual(stats["closed"], 0)
        self.assertEqual(self.only_job()["status"], "open")

    def test_a_failed_crawl_closes_nothing(self) -> None:
        cid = self.acme["id"]
        self.ingest([self.posting(1)], company_id=cid, crawled={cid})
        self.assertEqual(self.ingest([], company_id=cid, crawled=set())["closed"], 0)
        self.assertEqual(self.only_job()["status"], "open")

    # --- updates -------------------------------------------------------------------------

    def test_a_meaningful_change_is_recorded_once(self) -> None:
        self.ingest([self.posting(1)], company_id=self.acme["id"])
        self.ingest([self.posting(1, title="Senior SAP FICO Consultant")], company_id=self.acme["id"])
        self.ingest([self.posting(1, title="Senior SAP FICO Consultant")], company_id=self.acme["id"])
        job = self.only_job()
        self.assertEqual(job["title"], "Senior SAP FICO Consultant")
        [change] = self.history("changed")
        self.assertEqual(change["changed_fields"], ["title"])
        self.assertEqual(change["before"]["title"], "SAP FICO Consultant")

    def test_campaign_and_keyword_relevance_are_both_set(self) -> None:
        self.store.insert(self.ctx, "campaigns", {"key": "SAP", "name": "SAP", "focus_keywords": ["SAP"],
                                                  "status": "active"})
        self.monitors.upload_keyword_set(self.ctx, "kw.xlsx", workbook_bytes())
        stats = self.ingest([self.posting(1)], company_id=self.acme["id"])
        self.assertEqual(stats["relevant"], 1)
        job = self.only_job()
        self.assertTrue(job["is_relevant"])
        self.assertEqual(job["campaign_keys"], ["SAP"])
        self.assertIsNotNone(job["relevance_class"])               # was never set on this path
        self.assertTrue(job["relevance_version"])

    def test_first_seen_is_kept_and_last_seen_moves(self) -> None:
        self.ingest([self.posting(1)], company_id=self.acme["id"])
        job = self.only_job()
        earlier = job["first_seen_at"] - timedelta(days=3)
        self.store.update(self.ctx, "job_postings", job["id"], {"first_seen_at": earlier, "last_seen_at": earlier})
        self.ingest([self.posting(1)], company_id=self.acme["id"])
        job = self.only_job()
        self.assertEqual(job["first_seen_at"], earlier)
        self.assertGreater(job["last_seen_at"], earlier)


class HistoryRetentionTests(_Base):
    """prune_history: only new/stale rows older than the cutoff; dry run by default."""

    def age(self, change, days):
        for row in self.history(change):
            self.store.update(self.ctx.as_system(), "job_posting_changes", row["id"],
                              {"detected_at": utcnow() - timedelta(days=days)})

    def build_history(self):
        cid = self.acme["id"]
        self.ingest([self.posting(1), self.posting(2), self.posting(3)], company_id=cid, crawled={cid})
        self.ingest([self.posting(1, title="Lead SAP FICO Consultant"), self.posting(2)], company_id=cid,
                    crawled={cid})                                   # 1 changed, 3 closed
        job2 = self.store.first(self.ctx, "job_postings", {"url_key": "https://acme.com/careers/2"})
        self.store.update(self.ctx, "job_postings", job2["id"], {"status": "stale", "stale_at": utcnow()})
        self.store.insert(self.ctx.as_system(), "job_posting_changes", {
            "job_posting_id": job2["id"], "change": "stale", "changed_fields": ["status"],
            "before": {"status": "ACTIVE"}, "after": {"status": "STALE"}, "detected_at": utcnow()})
        for change in ("new", "stale", "changed", "closed"):
            self.age(change, 200)
        return job2

    def test_dry_run_counts_and_deletes_nothing(self) -> None:
        self.build_history()
        before = len(self.history())
        result = self.monitors.prune_history(self.ctx, older_than_days=90)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["matched"], {"new": 3, "stale": 1})
        self.assertEqual((result["deleted"], len(self.history())), (0, before))

    def test_prune_keeps_every_record_of_what_changed(self) -> None:
        job2 = self.build_history()
        result = self.monitors.prune_history(self.ctx, older_than_days=90, dry_run=False)
        self.assertEqual(result["deleted"], 4)
        self.assertEqual({h["change"] for h in self.history()}, {"changed", "closed"})
        self.assertEqual(self.store.count(self.ctx, "audit_log", {"action": "job_history.pruned"}), 1)
        # the timeline still shows when the job was first seen and when it went stale
        detail = self.monitors.job_detail(self.ctx, job2["id"])
        kinds = [h["change"] for h in detail["history"]]
        self.assertIn("new", kinds)
        self.assertIn("stale", kinds)
        self.assertTrue(all(h.get("synthesized") for h in detail["history"] if h["change"] in ("new", "stale")))

    def test_recent_rows_and_the_whole_table_below_the_cutoff_stay(self) -> None:
        self.build_history()
        self.ingest([self.posting(9)], company_id=self.acme["id"])     # a fresh 'new' row
        result = self.monitors.prune_history(self.ctx, older_than_days=90, dry_run=False)
        self.assertEqual(result["deleted"], 4)
        self.assertEqual(len(self.history("new")), 1)
        self.assertEqual(self.monitors.prune_history(self.ctx, older_than_days=90, dry_run=False)["deleted"], 0)

    def test_invalid_window_is_refused(self) -> None:
        from cloud.intel.core.context import ValidationError

        with self.assertRaises(ValidationError):
            self.monitors.prune_history(self.ctx, older_than_days=0)

    def test_detail_does_not_invent_entries_that_still_exist(self) -> None:
        self.ingest([self.posting(1)], company_id=self.acme["id"])
        job = self.only_job()
        history = self.monitors.job_detail(self.ctx, job["id"])["history"]
        self.assertEqual([h["change"] for h in history], ["new"])
        self.assertFalse(history[0].get("synthesized"))


if __name__ == "__main__":
    unittest.main()
