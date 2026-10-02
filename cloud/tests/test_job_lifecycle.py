"""Job lifecycle (STALE / EXPIRED / CLOSED / REOPENED), gone checks, the daily scheduled
evaluation, job-derived signals (cluster, stack migration, provider-evidenced departure) and
the signal -> campaign -> outcome loop (migration 0013)."""

from __future__ import annotations

import base64
import json
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from cloud.intel.core.context import ConflictError, Ctx, utcnow
from cloud.intel.job_monitor import diff as jdiff
from cloud.intel.job_monitor.profiles import get_profile
from cloud.intel.job_monitor.schema import content_hash, normalize_job
from cloud.intel.signals import jobsignals
from cloud.intel.tasks.worker import run_task_inline
from cloud.tests.test_job_monitor import HOST, START, _Base, job, page_html, page_url, site


def cursor(day: date, ident: int) -> str:
    return base64.urlsafe_b64encode(json.dumps([day.isoformat(), ident], separators=(",", ":")).encode()) \
        .decode().rstrip("=")


def dated_site(pages_of_jobs: List[Tuple[date, List[Dict[str, Any]]]]) -> Dict[str, Tuple]:
    """A listing whose cursors carry real [listing date, id] positions (as WeAreDevelopers does)."""
    pages: Dict[str, Tuple] = {}
    url = START
    for index, (day, jobs) in enumerate(pages_of_jobs):
        last = index + 1 == len(pages_of_jobs)
        nxt = None if last else cursor(day, jobs[-1]["id"])
        pages[url] = (200, page_html(jobs, nxt))
        if nxt:
            url = f"{HOST}/jobs?country=US&page={nxt}"
    return pages


def key(j: Dict[str, Any]) -> str:
    return f"https://wearedevelopers.com/jobs/ext/{j['id']}"


class LifecycleTests(_Base):
    def history(self, row: Dict[str, Any]) -> List[str]:
        return [h["change"] for h in self.store.all(self.ctx, "job_posting_changes", {"job_posting_id": row["id"]},
                                                    order="detected_at")]

    def age(self, url_key: str, days: int) -> None:
        row = self.jobs(url_key=url_key)[0]
        self.store.update(self.ctx.as_system(), "job_postings", row["id"],
                          {"first_seen_at": utcnow() - timedelta(days=days)})

    # 1. stale after 30 days
    def test_stale_after_30_days(self) -> None:
        catalog = [job(1), job(2)]
        self.pages.update(site(catalog))
        monitor = self.monitor()
        self.run_monitor(monitor)
        self.age(key(catalog[0]), 31)
        self.age(key(catalog[1]), 29)
        result = self.svc.evaluate_lifecycle(self.ctx, monitor["id"])
        old, young = self.jobs(url_key=key(catalog[0]))[0], self.jobs(url_key=key(catalog[1]))[0]
        self.assertEqual((old["status"], young["status"]), ("stale", "open"))
        self.assertIsNotNone(old["stale_at"])
        self.assertEqual(result["became_stale"], 1)
        self.assertEqual(self.history(old), ["new", "stale"])
        self.assertEqual([n["title"] for n in self.store.all(self.ctx, "notifications", {})
                          if "stale" in n["title"]], ["1 job became stale"])
        # a STALE job seen again stays STALE (still active, still old) and is not re-flagged
        self.run_monitor(monitor)
        self.assertEqual(self.jobs(url_key=key(catalog[0]))[0]["status"], "stale")
        self.assertEqual(self.svc.evaluate_lifecycle(self.ctx, monitor["id"])["became_stale"], 0)
        self.assertEqual(self.svc.search_jobs(self.ctx, {"change": "stale"})["total"], 1)
        self.assertEqual(self.svc.search_jobs(self.ctx, {"status": "STALE"})["total"], 1)

    def test_stale_uses_listing_date_and_reopen_restarts_the_clock(self) -> None:
        row = {"title": "X", "first_seen_at": utcnow() - timedelta(days=5),
               "listing_date": (utcnow() - timedelta(days=40)).date()}
        self.assertLess(self.svc.active_since(row), utcnow() - timedelta(days=39))
        row["reopened_at"] = utcnow() - timedelta(days=2)
        self.assertGreater(self.svc.active_since(row), utcnow() - timedelta(days=3))

    # 2. fingerprint changes
    def test_fingerprint_changes_only_on_meaningful_fields(self) -> None:
        a, _ = normalize_job({"job_url": f"{HOST}/jobs/ext/1-a", "title": "SAP Analyst", "salary_budget": "$1k"})
        b, _ = normalize_job({"job_url": f"{HOST}/jobs/ext/1-a-renamed?utm_source=x", "title": " sap analyst ",
                              "salary_budget": "$1K", "listing_date": "2026-09-01"})
        c, _ = normalize_job({"job_url": f"{HOST}/jobs/ext/1-a", "title": "SAP Analyst", "salary_budget": "$2k"})
        self.assertEqual(a["content_hash"], b["content_hash"])          # case/space/slug/listing date: same
        self.assertNotEqual(a["content_hash"], c["content_hash"])       # salary: different
        self.assertEqual(b["listing_date"], date(2026, 9, 1))
        self.assertEqual(a["content_hash"], content_hash(a))

    # 3. CHANGED
    def test_changed(self) -> None:
        catalog = [job(1)]
        self.pages.update(site(catalog))
        monitor = self.monitor()
        self.run_monitor(monitor)
        catalog[0] = job(1, salary="$999k–1000k")
        self.pages.clear()
        self.pages.update(site(catalog))
        run = self.run_monitor(monitor)
        row = self.jobs(url_key=key(catalog[0]))[0]
        self.assertEqual((run["changed_count"], row["salary_budget"]), (1, "$999k–1000k"))
        change = self.store.first(self.ctx, "job_posting_changes", {"job_posting_id": row["id"], "change": "changed"})
        self.assertEqual(change["changed_fields"], ["salary_budget"])
        self.assertEqual(change["before"]["salary_budget"], "$101k–151k")
        self.assertEqual(row["last_changed_run_id"], run["id"])

    # 4. REOPENED (from CLOSED and from EXPIRED)
    def test_reopened_from_closed_and_expired(self) -> None:
        catalog = [job(1), job(2)]
        self.pages.update(site(catalog))
        monitor = self.monitor()
        self.run_monitor(monitor)
        closed, expired = self.jobs(url_key=key(catalog[0]))[0], self.jobs(url_key=key(catalog[1]))[0]
        system = self.ctx.as_system()
        self.store.update(system, "job_postings", closed["id"], {"status": "closed", "closed_at": utcnow(),
                                                                 "closure_reason": "source_gone", "gone_status": 410})
        self.store.update(system, "job_postings", expired["id"], {"status": "expired", "expired_at": utcnow()})
        run = self.run_monitor(monitor)
        a, b = self.jobs(url_key=key(catalog[0]))[0], self.jobs(url_key=key(catalog[1]))[0]
        self.assertEqual(run["reopened_count"], 2)
        for row in (a, b):
            self.assertEqual(row["status"], "open")
            self.assertIsNotNone(row["reopened_at"])
            self.assertIsNone(row["closure_reason"])
            self.assertIsNone(row["expired_at"])
        before = self.store.first(self.ctx, "job_posting_changes", {"job_posting_id": a["id"], "change": "reopened"})
        self.assertEqual((before["before"]["status"], before["before"]["closure_reason"]), ("CLOSED", "source_gone"))
        self.assertEqual(jdiff.classify({"status": "expired", "content_hash": "x"}, {"content_hash": "x"}).kind,
                         "reopened")

    # 5 + 6. first missed sweep keeps the job; the second consecutive one closes it
    def test_first_and_second_missed_full_sweep(self) -> None:
        catalog = [job(n) for n in range(1, 4)]
        self.pages.update(site(catalog))
        monitor = self.monitor()
        self.run_monitor(monitor, "full")
        self.pages.clear()
        self.pages.update(site(catalog[:2]))
        first = self.run_monitor(monitor, "full")
        row = self.jobs(url_key=key(catalog[2]))[0]
        self.assertEqual((first["status"], first["closed_count"], row["status"], row["missed_full_sweeps"]),
                         ("completed", 0, "open", 1))
        second = self.run_monitor(monitor, "full")
        row = self.jobs(url_key=key(catalog[2]))[0]
        self.assertEqual((second["closed_count"], row["status"], row["closure_reason"]),
                         (1, "closed", "missed_full_sweeps"))
        self.assertEqual(self.history(row), ["new", "closed"])

    # 7. a partial sweep never closes
    def test_partial_sweep_does_not_close_or_count(self) -> None:
        catalog = [job(n) for n in range(1, 10)]
        self.pages.update(site(catalog))
        monitor = self.monitor()
        self.run_monitor(monitor, "full")
        broken = site(catalog[:6])
        broken[page_url(2)] = (500, "upstream error")
        for _ in range(3):
            self.pages.clear()
            self.pages.update(broken)
            run = self.run_monitor(monitor, "full")
            self.assertEqual(run["status"], "partial")
        rows = self.jobs()
        self.assertTrue(all(r["status"] == "open" and r["missed_full_sweeps"] == 0 for r in rows))
        self.assertEqual(self.gone_calls, [])     # no gone checks after a partial sweep

    # 8. 410 Gone handling
    def test_gone_410_closes_on_first_miss_and_live_page_keeps(self) -> None:
        catalog = [job(n) for n in range(1, 5)]
        self.pages.update(site(catalog))
        monitor = self.monitor()
        self.run_monitor(monitor, "full")
        gone, live = self.jobs(url_key=key(catalog[2]))[0], self.jobs(url_key=key(catalog[3]))[0]
        self.gone[gone["job_url"]] = 410
        self.pages.clear()
        self.pages.update(site(catalog[:2]))
        run = self.run_monitor(monitor, "full")
        gone, live = self.jobs(url_key=key(catalog[2]))[0], self.jobs(url_key=key(catalog[3]))[0]
        self.assertEqual((gone["status"], gone["closure_reason"], gone["gone_status"]), ("closed", "source_gone", 410))
        self.assertEqual((live["status"], live["gone_status"], live["missed_full_sweeps"]), ("open", 200, 1))
        self.assertEqual((run["closed_count"], run["gone_checked_count"], run["gone_closed_count"]), (1, 2, 1))
        note = [n for n in self.store.all(self.ctx, "notifications", {}) if n["title"] == "1 job was closed"][0]
        self.assertIn("confirmed removed at the source", note["body"])
        # checked recently: not fetched again by the next sweep
        calls = len(self.gone_calls)
        self.run_monitor(monitor, "full")
        self.assertEqual(len(self.gone_calls), calls)

    def test_gone_checks_stop_on_refusal(self) -> None:
        catalog = [job(n) for n in range(1, 6)]
        self.pages.update(site(catalog))
        monitor = self.monitor()
        self.run_monitor(monitor, "full")
        for j in catalog[1:]:
            self.gone[f"{HOST}/jobs/ext/{j['id']}-{j['title'].lower().replace(' ', '-')}"] = 429
        self.pages.clear()
        self.pages.update(site(catalog[:1]))
        run = self.run_monitor(monitor, "full")
        self.assertEqual(len(self.gone_calls), 1)
        self.assertEqual(run["gone_checked_count"], 0)
        self.assertTrue(any("stopped" in n for n in run["notes"]))
        self.assertTrue(all(r["status"] == "open" for r in self.jobs()))

    # 9. 90-day source visibility boundary
    def test_90_day_window_expires_instead_of_closing(self) -> None:
        today = utcnow().date()
        fresh = [job(n) for n in range(1, 4)]
        old = [job(n) for n in range(4, 7)]
        tail = [job(7)]     # the last page has no next cursor: its own cursor (95 days) dates it
        self.pages.update(dated_site([(today - timedelta(days=1), fresh[:2]), (today - timedelta(days=60), fresh[2:]),
                                      (today - timedelta(days=95), old), (today - timedelta(days=96), tail)]))
        old = old + tail
        monitor = self.monitor()
        self.assertEqual(monitor["visible_window_days"], 90)
        self.run_monitor(monitor, "full")
        listed = {r["url_key"]: r["listing_date"] for r in self.jobs()}
        self.assertEqual(listed[key(fresh[0])], today - timedelta(days=1))
        self.assertEqual(listed[key(fresh[2])], today - timedelta(days=60))
        # the source drops everything: the 60-day job misses a sweep, the 95-day ones expire
        self.pages.clear()
        self.pages.update(dated_site([(today - timedelta(days=1), fresh[:2])]))
        run = self.run_monitor(monitor, "full")
        by_key = {r["url_key"]: r for r in self.jobs()}
        self.assertEqual((by_key[key(fresh[2])]["status"], by_key[key(fresh[2])]["missed_full_sweeps"]), ("open", 1))
        for j in old:
            row = by_key[key(j)]
            self.assertEqual((row["status"], row["missed_full_sweeps"], row["closed_at"]), ("expired", 0, None))
            self.assertIsNotNone(row["expired_at"])
        self.assertEqual((run["expired_count"], run["closed_count"]), (4, 0))
        # another sweep never closes the expired ones (absence outside the window proves nothing)
        self.run_monitor(monitor, "full")
        self.assertTrue(all(by_key[key(j)]["status"] == "expired" for j in old))
        self.assertEqual(self.svc.search_jobs(self.ctx, {"change": "expired"})["total"], 4)
        self.assertEqual(get_profile("wearedevelopers").cursor_date(
            f"{HOST}/jobs?country=US&page={cursor(date(2026, 7, 3), 5)}"), date(2026, 7, 3))
        self.assertIsNone(get_profile("wearedevelopers").cursor_date(f"{HOST}/jobs?country=US&page=garbage"))

    # 10. the daily scheduled stale evaluation
    def test_daily_scheduled_lifecycle_task(self) -> None:
        catalog = [job(1)]
        self.pages.update(site(catalog))
        monitor = self.monitor()
        self.run_monitor(monitor)
        self.age(key(catalog[0]), 45)
        self.assertEqual(self.svc.tick(self.ctx), 0)              # no run due; the lifecycle task is queued
        tasks = self.store.all(self.ctx, "platform_tasks", {"kind": "job_lifecycle"})
        self.assertEqual(len(tasks), 1)
        self.svc.tick(self.ctx)                                   # same day: not queued twice
        self.assertEqual(len(self.store.all(self.ctx, "platform_tasks", {"kind": "job_lifecycle"})), 1)
        run_task_inline(self.platform, self.ctx.workspace_id, tasks[0]["id"])
        self.assertEqual(self.jobs(url_key=key(catalog[0]))[0]["status"], "stale")
        stored = self.store.get(self.ctx, "job_source_monitors", monitor["id"])
        self.assertEqual(stored["last_lifecycle_result"]["became_stale"], 1)
        self.assertGreater(stored["next_lifecycle_at"], utcnow() + timedelta(hours=23))
        later = utcnow() + timedelta(days=1, minutes=1)
        self.svc.tick(self.ctx, now=later)                        # the next day: queued again
        self.assertEqual(len(self.store.all(self.ctx, "platform_tasks", {"kind": "job_lifecycle"})), 2)
        self.svc.update_monitor(self.ctx, monitor["id"], {"enabled": False})
        self.svc.tick(self.ctx, now=later + timedelta(days=1))    # paused monitors are not evaluated
        self.assertEqual(len(self.store.all(self.ctx, "platform_tasks", {"kind": "job_lifecycle"})), 2)

    # 11. no duplicate normalized URLs
    def test_no_duplicate_normalized_urls(self) -> None:
        a = job(1)
        twin = dict(a, title="Engineer 1 renamed")
        self.pages.update(site([a, twin]))
        monitor = self.monitor()
        self.run_monitor(monitor)
        self.assertEqual(len(self.jobs(url_key=key(a))), 1)
        row = self.jobs(url_key=key(a))[0]
        with self.assertRaises(ConflictError):
            self.store.insert(self.ctx, "job_postings", {k: row[k] for k in (
                "title", "job_url", "url_key", "normalized_title", "first_seen_at", "last_seen_at", "source_kind",
                "source_name")})

    # 12. restart / resume in the close + gone-check phase
    def test_resume_after_crash_in_gone_checks_does_not_double_count(self) -> None:
        catalog = [job(n) for n in range(1, 4)]
        self.pages.update(site(catalog))
        monitor = self.monitor()
        self.run_monitor(monitor, "full")
        self.pages.clear()
        self.pages.update(site(catalog[:2]))
        run = self.svc.start_run(self.ctx, monitor["id"], mode="full")
        real = self.svc.check_gone
        with mock.patch.object(type(self.svc), "check_gone", side_effect=RuntimeError("worker killed")):
            run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
        stored = self.store.get(self.ctx, "job_monitor_runs", run["id"])
        self.assertEqual(stored["checkpoint"]["phase"], "checking")
        self.assertEqual(self.jobs(url_key=key(catalog[2]))[0]["missed_full_sweeps"], 1)
        self.store.update(self.ctx.as_system(), "platform_tasks", run["task_id"], {"run_after": None})
        run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
        done = self.store.get(self.ctx, "job_monitor_runs", run["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.jobs(url_key=key(catalog[2]))[0]["missed_full_sweeps"], 1)   # counted once
        self.assertTrue(callable(real))

    # 13. notification counts from real results; none for a zero-change run
    def test_notification_counts(self) -> None:
        catalog = [job(n) for n in range(1, 5)]
        self.pages.update(site(catalog))
        monitor = self.monitor()
        self.run_monitor(monitor)
        titles = [n["title"] for n in self.store.all(self.ctx, "notifications", {})]
        self.assertIn("4 new jobs found from WeAreDevelopers", titles)
        before = len(titles)
        self.run_monitor(monitor)                                  # nothing changed
        self.assertEqual(len(self.store.all(self.ctx, "notifications", {})), before)
        catalog[0] = job(1, title="Engineer 1 (Lead)")
        catalog[1] = job(2, salary="$1k")
        self.pages.clear()
        self.pages.update(site(catalog))
        self.run_monitor(monitor)
        titles = [n["title"] for n in self.store.all(self.ctx, "notifications", {})]
        self.assertIn("2 jobs changed", titles)
        link = [n for n in self.store.all(self.ctx, "notifications", {}) if n["title"] == "2 jobs changed"][0]["link"]
        self.assertTrue(link.startswith(f"/jobs?monitor={monitor['id']}&run=") and link.endswith("&change=changed"))

    def test_automatic_full_sweep_can_be_switched_off(self) -> None:
        self.pages.update(site([job(1), job(2)]))
        monitor = self.monitor(auto_full_sweep=False)
        self.assertIsNone(monitor["next_full_sweep_at"])
        later = utcnow() + timedelta(days=30)
        self.svc.tick(self.ctx, now=later)                        # due daily run: incremental, never full
        modes = [r["mode"] for r in self.store.all(self.ctx, "job_monitor_runs", {"monitor_id": monitor["id"]})]
        self.assertEqual(modes, ["incremental"])
        queued = self.store.first(self.ctx, "job_monitor_runs", {"monitor_id": monitor["id"]})
        run_task_inline(self.platform, self.ctx.workspace_id, queued["task_id"])
        self.run_monitor(monitor, "full")                         # a manual sweep still works...
        stored = self.store.get(self.ctx, "job_source_monitors", monitor["id"])
        self.assertIsNone(stored["next_full_sweep_at"])          # ...and does not switch automatic sweeps on
        self.assertFalse(self.svc.monitor_detail(self.ctx, monitor["id"])["monitor"]["auto_full_sweep"])
        on = self.svc.update_monitor(self.ctx, monitor["id"], {"auto_full_sweep": True})
        self.assertGreater(on["next_full_sweep_at"], utcnow() + timedelta(days=6))
        off = self.svc.update_monitor(self.ctx, monitor["id"], {"auto_full_sweep": False})
        self.assertIsNone(off["next_full_sweep_at"])

    def test_first_incremental_run_adds_only_todays_listings(self) -> None:
        today = utcnow().date()
        new_today = [job(n) for n in range(1, 4)]
        backlog = [job(n) for n in range(4, 10)]
        self.pages.update(dated_site([(today, new_today), (today - timedelta(days=1), backlog[:3]),
                                      (today - timedelta(days=2), backlog[3:])]))
        monitor = self.monitor()
        run = self.run_monitor(monitor)                           # empty monitor: since = today
        self.assertEqual(run["checkpoint"]["since"], today.isoformat())
        self.assertEqual(sorted(r["url_key"] for r in self.jobs()), sorted(key(j) for j in new_today))
        self.assertEqual((run["new_count"], run["pages"]), (3, 2))
        self.assertIn("listed before", run["stop_reason"])
        # tomorrow: one more posting at the top; only it is new, nothing is re-added
        fresh = job(20)
        self.pages.clear()
        self.pages.update(dated_site([(today, [fresh] + new_today[:2]), (today, new_today[2:] + backlog[:2]),
                                      (today - timedelta(days=1), backlog[2:5]), (today - timedelta(days=2), backlog[5:]),
                                      (today - timedelta(days=3), [job(30)])]))
        nxt = self.run_monitor(monitor)
        floor = (today - timedelta(days=1)).isoformat()           # the day before the previous run
        self.assertEqual(nxt["checkpoint"]["since"], floor)
        self.assertEqual(nxt["new_count"], 6)                     # listed today/yesterday and never seen
        self.assertNotIn(key(backlog[5]), {r["url_key"] for r in self.jobs()})   # older backlog: never added
        self.assertEqual(len(self.jobs()), 9)                     # each URL stored once
        explicit = self.svc.start_run(self.ctx, monitor["id"], mode="incremental", since=today.isoformat())
        self.assertEqual(explicit["checkpoint"]["since"], today.isoformat())
        self.store.update(self.ctx.as_system(), "job_monitor_runs", explicit["id"], {"status": "cancelled"})
        with self.assertRaises(Exception):
            self.svc.start_run(self.ctx, monitor["id"], mode="full", since=today.isoformat())

    def test_floor_follows_the_sources_dates_not_our_clock(self) -> None:
        # WeAreDevelopers dates a fresh posting a day or two back: the newest batch must still count
        lag = utcnow().date() - timedelta(days=2)
        newest = [job(n) for n in range(1, 4)]
        older = [job(n) for n in range(4, 7)]
        self.pages.update(dated_site([(lag, newest), (lag - timedelta(days=3), older), (lag - timedelta(days=4),
                                                                                       [job(9)])]))
        run = self.run_monitor(self.monitor())
        self.assertEqual((run["new_count"], run["checkpoint"]["since"], run["checkpoint"]["newest_listing"]),
                         (3, lag.isoformat(), lag.isoformat()))
        self.assertEqual(sorted(r["url_key"] for r in self.jobs()), sorted(key(j) for j in newest))


def _job_row(n: int, *, company: str, title: str, keywords: List[str], relevance: str = "HIGH",
             status: str = "open", seen: Optional[datetime] = None, description: str = "") -> Dict[str, Any]:
    seen = seen or utcnow() - timedelta(days=3)
    return {"id": f"jp_{n:032x}", "source_kind": "scraper", "source_name": "test", "title": title, "normalized_title": title.lower(), "job_url": f"https://jobs.example.com/{n}",
            "url_key": f"https://jobs.example.com/{n}", "company_name": company, "status": status,
            "relevance_class": relevance, "matched_keywords": keywords, "first_seen_at": seen, "last_seen_at": seen,
            "description": description, "source": "WeAreDevelopers"}


class SignalTests(_Base):
    def add_jobs(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [self.store.insert(self.ctx, "job_postings", {k: v for k, v in r.items() if k != "id"})
                for r in rows]

    # 14. cluster signal >= 3
    def test_hiring_cluster_needs_three_related_open_jobs(self) -> None:
        self.add_jobs([_job_row(i, company="Acme Corp", title=f"SAP Consultant {i}", keywords=["SAP", "ABAP"])
                       for i in range(3)])
        self.add_jobs([_job_row(10 + i, company="Beta LLC", title=f"NetSuite Admin {i}", keywords=["NetSuite"])
                       for i in range(2)])
        self.add_jobs([_job_row(20, company="Acme Corp", title="Office Manager", keywords=["SAP"],
                                relevance="REJECT")])
        signals = self.platform.service("signals")
        stats = signals.detect_job_signals(self.ctx)
        rows = self.store.all(self.ctx, "hiring_signals", {"signal_type": "HIRING_CLUSTER"})
        techs = sorted(r["technologies"][0] for r in rows)
        self.assertEqual(techs, ["ABAP", "SAP"])                  # Beta has only 2; the REJECT job is not counted
        sap = [r for r in rows if r["technologies"] == ["SAP"]][0]
        self.assertEqual((sap["company_name"], len(sap["job_posting_ids"]), sap["company_id"]), ("Acme Corp", 3, None))
        self.assertEqual(stats["HIRING_CLUSTER"], 2)
        signals.detect_job_signals(self.ctx)                       # idempotent
        self.assertEqual(len(self.store.all(self.ctx, "hiring_signals", {"signal_type": "HIRING_CLUSTER"})), 2)
        one = self.store.first(self.ctx, "job_postings", {"title": "SAP Consultant 0"})
        self.store.update(self.ctx, "job_postings", one["id"], {"status": "closed"})
        signals.detect_job_signals(self.ctx)                       # below 3 again: expires, never deleted
        self.assertEqual({r["status"] for r in self.store.all(self.ctx, "hiring_signals",
                                                              {"signal_type": "HIRING_CLUSTER"})}, {"expired"})

    # 15. stack migration needs real evidence
    def test_stack_migration_evidence(self) -> None:
        now = utcnow()
        lone = [_job_row(1, company="Solo Inc", title="SAP ECC Basis Admin", keywords=["SAP"])]
        both_no_words = [_job_row(2, company="Mix Inc", title="SAP ECC and S/4HANA support", keywords=["SAP"])]
        self.assertEqual(jobsignals.detect_migrations(lone + both_no_words, now=now), [])
        pair = [_job_row(3, company="Acme", title="SAP ECC FI Analyst", keywords=["SAP"]),
                _job_row(4, company="Acme", title="S/4HANA Finance Lead", keywords=["SAP"])]
        worded = [_job_row(5, company="Zed", title="ERP Analyst", keywords=["SAP"],
                           description="Lead the migration from SAP ECC to S/4HANA.")]
        old = [_job_row(6, company="Old Co", title="Oracle EBS Developer", keywords=["Oracle"],
                        seen=now - timedelta(days=400)),
               _job_row(7, company="Old Co", title="Oracle Fusion Architect", keywords=["Oracle"])]
        found = {s.company_name: s for s in jobsignals.detect_migrations(pair + worded + old, now=now)}
        self.assertEqual(sorted(found), ["Acme", "Zed"])          # Old Co's legacy job is outside the window
        acme = found["Acme"]
        self.assertIn("legacy_and_modern_jobs", acme.reason_codes)
        self.assertEqual({e["rule"] for e in acme.evidence}, {"legacy job", "modern job"})
        self.assertEqual(found["Zed"].reason_codes[1], "migration_wording_in_job")
        self.assertTrue(any("ecc" in t.lower() for t in acme.technologies))
        self.assertEqual(jobsignals.detect_migrations([_job_row(8, company="AWS shop", title="AWS EBS volumes",
                                                                 keywords=[])], now=now), [])

    # 16. departures only with authorized provider evidence
    def test_departure_only_with_provider_evidence(self) -> None:
        signals = self.platform.service("signals")
        company = self.store.insert(self.ctx, "companies", {"name": "Acme Corp", "normalized_name": "acme corp"})
        mgr = self.store.insert(self.ctx, "contacts", {"full_name": "Pat Lee", "title": "ERP Manager",
                                                       "company_id": company["id"]})
        sales = self.store.insert(self.ctx, "contacts", {"full_name": "Sam Roe", "title": "Sales Manager",
                                                         "company_id": company["id"]})
        quiet = self.store.insert(self.ctx, "contacts", {"full_name": "Kim Poe", "title": "IT Director",
                                                         "company_id": company["id"]})
        for contact, pid in ((mgr, "z1"), (sales, "z2"), (quiet, "z3")):
            signals.record_contact_snapshot(self.ctx, contact, provider="zoominfo",
                                            person={"zoominfo_id": pid, "title": contact["title"],
                                                    "company_name": "Acme Corp"}, company=company,
                                            observed_at=utcnow() - timedelta(days=30))
        self.assertEqual(signals.detect_departures(self.ctx), [])          # one snapshot each: nothing
        later = {"title": "ERP Manager", "company_name": "Other Co"}
        signals.record_contact_snapshot(self.ctx, mgr, provider="zoominfo", person={"zoominfo_id": "z1", **later})
        signals.record_contact_snapshot(self.ctx, sales, provider="zoominfo",
                                        person={"zoominfo_id": "z2", "title": "Sales Manager",
                                                "company_name": "Other Co"})
        # quiet: the provider returned nothing new -> no snapshot -> never a departure
        signals.record_contact_snapshot(self.ctx, quiet, provider="manual",
                                        person={"title": "IT Director", "employment_status": "left_company"})
        found = signals.detect_departures(self.ctx)
        self.assertEqual([s.contact_id for s in found], [mgr["id"]])        # tech manager only, provider only
        self.assertIn("provider_reports_new_company", found[0].reason_codes)
        self.assertEqual(len(found[0].evidence), 2)
        signals.detect_job_signals(self.ctx)
        stored = self.store.all(self.ctx, "hiring_signals", {"signal_type": "DEPARTURE"})
        self.assertEqual([(s["contact_id"], s["company_id"]) for s in stored], [(mgr["id"], company["id"])])

    # 17. signal -> campaign -> outcome linkage
    def test_signal_to_campaign_outcome_linkage(self) -> None:
        outcomes = self.platform.service("signal_outcomes")
        company = self.store.insert(self.ctx, "companies", {"name": "Acme Corp", "normalized_name": "acme corp"})
        contact = self.store.insert(self.ctx, "contacts", {"full_name": "Pat Lee", "company_id": company["id"]})
        signal = self.store.insert(self.ctx, "hiring_signals", {
            "company_id": company["id"], "signal_type": "HIRING_CLUSTER", "detected_at": utcnow(),
            "fingerprint": f"t:{uuid.uuid4().hex}", "status": "active"})
        campaign = self.store.insert(self.ctx, "campaigns", {"key": "erp", "name": "ERP"})
        sequence = self.store.insert(self.ctx, "sequences", {"name": "ERP seq", "campaign_id": campaign["id"]})
        enrollment = self.store.insert(self.ctx, "sequence_enrollments", {
            "sequence_id": sequence["id"], "contact_id": contact["id"], "campaign_id": campaign["id"],
            "status": "active"})
        outcomes.link_enrollment(self.ctx, enrollment["id"], signal["id"])
        self.assertEqual(self.store.get(self.ctx, "sequence_enrollments", enrollment["id"])["signal_id"], signal["id"])
        sent = {"id": "me_x", "event": "sent", "enrollment_id": enrollment["id"], "occurred_at": utcnow()}
        outcomes.on_message_event(self.ctx, sent)
        outcomes.on_message_event(self.ctx, sent)                 # a resent event is not doubled
        self.assertEqual(outcomes.on_reply(self.ctx, contact["id"]), 1)
        outcomes.record(self.ctx, signal["id"], "meeting", enrollment_id=enrollment["id"], note="intro call")
        rows = outcomes.outcomes(self.ctx, signal["id"])
        self.assertEqual(sorted(r["outcome"] for r in rows), ["contacted", "meeting", "replied"])
        for row in rows:
            self.assertEqual((row["signal_id"], row["company_id"], row["contact_id"], row["campaign_id"],
                              row["enrollment_id"]), (signal["id"], company["id"], contact["id"], campaign["id"],
                                                      enrollment["id"]))
            self.assertIsNotNone(row["occurred_at"])
        stored = self.store.get(self.ctx, "hiring_signals", signal["id"])
        self.assertEqual(stored["outcome"], "meeting")
        self.assertEqual(stored["outcome_counts"], {"contacted": 1, "replied": 1, "meeting": 1})
        opp = {"id": "op_1", "signal_ids": [signal["id"]], "campaign_id": campaign["id"]}
        self.assertEqual(outcomes.on_opportunity(self.ctx, opp), 1)
        done = self.store.update(self.ctx, "sequence_enrollments", enrollment["id"], {"status": "completed"})
        outcomes.on_enrollment_finished(self.ctx, done)
        self.assertEqual(self.store.get(self.ctx, "hiring_signals", signal["id"])["outcome_counts"]["no_response"], 1)
        with self.assertRaises(Exception):
            outcomes.record(self.ctx, signal["id"], "won-the-lottery")
        # an enrollment without a signal never writes an outcome
        other = self.store.insert(self.ctx, "sequence_enrollments", {
            "sequence_id": sequence["id"], "contact_id": self.store.insert(
                self.ctx, "contacts", {"full_name": "No Signal"})["id"], "status": "active"})
        self.assertIsNone(outcomes.on_message_event(self.ctx, {"event": "sent", "enrollment_id": other["id"]}))


if __name__ == "__main__":
    unittest.main()
