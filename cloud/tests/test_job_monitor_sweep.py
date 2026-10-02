"""Long-run safety for job monitor sweeps: retries on the same cursor, end-of-listing confirmation
in fresh sessions, cancellation, one crawler per site, the UNCHANGED filter, quiet zero-change runs,
streaming CSV import and malformed cards."""

from __future__ import annotations

import io
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest import mock

from cloud.intel.core.context import ConflictError, Ctx
from cloud.intel.core.http import SafeFetcher
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks.worker import run_task_inline
from cloud.shared.storage import LocalFileStorage
from cloud.tests.test_job_monitor import FAST, START, job, page_html, page_url, site
from cloud.tests.test_platform_ai_fakes import FakeResponse, fake_resolver


class ScriptedSession:
    """``pages[url]`` is a list of responses served in order (the last one repeats)."""

    def __init__(self, pages: Dict[str, List[Tuple[int, str]]]) -> None:
        self.pages, self.headers, self.calls = pages, {}, []

    def request(self, method, url, **kwargs):
        self.calls.append(url)
        queue = self.pages.get(url)
        if not queue:
            return FakeResponse(404, "not found")
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        return FakeResponse(status, body)


class SweepTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.store = MemoryStore()
        user = str(uuid.uuid4())
        ws = self.store.create_workspace(user, "W", f"w-{uuid.uuid4().hex[:6]}")
        self.ctx = Ctx(ws["id"], user, "owner")
        self.pages: Dict[str, List[Tuple[int, str]]] = {}
        self.sessions: List[ScriptedSession] = []

        def factory():
            session = ScriptedSession(self.pages)
            self.sessions.append(session)
            return SafeFetcher(session=session, resolver=fake_resolver(), per_host_delay=0)

        self.platform = Platform(self.store, storage=LocalFileStorage(Path(scratch.name)),
                                 config=PlatformConfig(extra={"fetcher_factory": factory, **FAST}))
        self.svc = self.platform.service("job_monitors")

    def serve(self, catalog: List[Dict[str, Any]], per_page: int = 3) -> None:
        self.pages.clear()
        for url, (status, body) in site(catalog, per_page).items():
            self.pages[url] = [(status, body)]

    def calls(self) -> List[str]:
        return [c for s in self.sessions for c in s.calls if not c.endswith("robots.txt")]

    def run_monitor(self, monitor: Dict[str, Any], mode: str = "incremental") -> Dict[str, Any]:
        run = self.svc.start_run(self.ctx, monitor["id"], mode=mode)
        run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
        return self.store.get(self.ctx, "job_monitor_runs", run["id"])

    def monitor(self, **kw: Any) -> Dict[str, Any]:
        return self.svc.create_monitor(self.ctx, {"source_url": START, "name": "WAD", **kw})

    def test_transient_errors_are_retried_on_the_same_cursor(self) -> None:
        self.serve([job(n) for n in range(1, 10)])
        good = self.pages[page_url(2)][0]
        self.pages[page_url(2)] = [(503, "busy"), (500, "oops"), (502, "bad gateway"), (500, "again"), good]
        run = self.run_monitor(self.monitor(), "full")
        self.assertEqual((run["status"], run["pages"], run["found"]), ("completed", 3, 9))
        self.assertGreaterEqual(run["warning_count"], 1)
        self.assertTrue(any("retry 1/5" in n for n in run["notes"]))
        self.assertGreater(run["request_count"], 3)
        self.assertIsNotNone(run["duration_seconds"])

    def test_retries_are_bounded_and_refusals_are_not_retried(self) -> None:
        self.serve([job(n) for n in range(1, 10)])
        self.pages[page_url(2)] = [(500, "down")]
        run = self.run_monitor(self.monitor(), "full")
        self.assertEqual(run["status"], "partial")
        self.assertEqual(self.calls().count(page_url(2)), 6 * 3)     # 6 cursor attempts x (1 + 2 HTTP retries)
        self.pages[page_url(2)] = [(403, "<html>Access denied</html>")]
        self.sessions.clear()
        run = self.run_monitor(self.store.first(self.ctx, "job_source_monitors", {}), "full")
        self.assertEqual(run["status"], "partial")
        self.assertEqual(self.calls().count(page_url(2)), 1)         # a refusal is never retried

    def test_end_of_listing_needs_confirmation_from_fresh_sessions(self) -> None:
        catalog = [job(n) for n in range(1, 10)]
        self.serve(catalog)
        # Page 2 first comes back wedged (no "Load more"), then a fresh session sees the cursor.
        self.pages[page_url(2)] = [(200, page_html(catalog[3:6], None)), (200, page_html(catalog[3:6], "C3"))]
        run = self.run_monitor(self.monitor(), "full")
        self.assertEqual((run["status"], run["found"]), ("completed", 9))
        self.assertIn("fresh session found one", " ".join(run["notes"]))
        self.assertIn("confirmed by 3 fresh-session re-reads", run["stop_reason"])
        # the real last page was re-read 3 more times in new sessions before the run believed it
        self.assertEqual(self.calls().count(page_url(3)), 4)

    def test_cancelled_sweep_closes_nothing(self) -> None:
        catalog = [job(n) for n in range(1, 7)]
        self.serve(catalog)
        monitor = self.monitor()
        self.run_monitor(monitor, "full")
        self.serve(catalog[:3])
        for _ in range(2):
            run = self.svc.start_run(self.ctx, monitor["id"], mode="full")
            with mock.patch("cloud.intel.tasks.service.TaskReporter.is_cancelled", return_value=True):
                run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
            self.assertEqual(self.store.get(self.ctx, "job_monitor_runs", run["id"])["status"], "cancelled")
        self.assertEqual(self.store.count(self.ctx, "job_postings", {"status": "closed"}), 0)
        self.assertEqual({j["missed_full_sweeps"] for j in self.store.all(self.ctx, "job_postings", {})}, {0})

    def test_one_crawler_per_site(self) -> None:
        self.serve([job(1)])
        first, second = self.monitor(), self.monitor(name="WAD duplicate")
        self.svc.start_run(self.ctx, first["id"])
        with self.assertRaises(ConflictError):
            self.svc.start_run(self.ctx, first["id"])                # the same monitor twice
        with self.assertRaises(ConflictError):
            self.svc.start_run(self.ctx, second["id"])               # another monitor, same site

    def test_unchanged_filter_and_quiet_zero_change_runs(self) -> None:
        catalog = [job(n) for n in range(1, 10)]
        self.serve(catalog)
        monitor = self.monitor()
        self.run_monitor(monitor)
        before = self.store.count(self.ctx, "notifications", {})
        catalog[4] = job(5, salary="$1k")
        self.serve(catalog)
        second = self.run_monitor(self.store.get(self.ctx, "job_source_monitors", monitor["id"]))
        self.assertEqual((second["changed_count"], second["unchanged_count"]), (1, 8))
        q = self.svc.search_jobs
        self.assertEqual(q(self.ctx, {"monitor": monitor["id"], "change": "unchanged"})["total"], 8)
        self.assertEqual(q(self.ctx, {"monitor": monitor["id"], "run": second["id"], "change": "unchanged"})["total"], 8)
        self.assertEqual(self.store.count(self.ctx, "notifications", {}), before + 1)   # "1 job changed" only
        quiet = self.run_monitor(self.store.get(self.ctx, "job_source_monitors", monitor["id"]))
        self.assertEqual((quiet["new_count"], quiet["changed_count"]), (0, 0))
        self.assertEqual(self.store.count(self.ctx, "notifications", {}), before + 1)   # nothing for a quiet run

    def test_live_counts_during_a_run(self) -> None:
        self.serve([job(n) for n in range(1, 10)])
        monitor = self.monitor()
        seen: List[int] = []
        original = self.svc.run_counts

        def spy(ctx, run_id):
            counts = original(ctx, run_id)
            seen.append(counts["new_count"])
            return counts

        with mock.patch("cloud.intel.job_monitor.runner.LIVE_COUNTS_EVERY", 1),                 mock.patch.object(self.svc, "run_counts", side_effect=spy):
            run = self.run_monitor(monitor)
        self.assertEqual(seen[:3], [3, 6, 9])                       # refreshed after every page
        self.assertEqual(run["new_count"], 9)

    def test_malformed_card_does_not_lose_the_page(self) -> None:
        catalog = [job(n) for n in range(1, 4)]
        self.serve(catalog)
        from cloud.intel.job_monitor.profiles.wearedevelopers import WeAreDevelopersProfile

        original = WeAreDevelopersProfile._card

        def flaky(self, card, page_url):
            if "Engineer 2" in card.get_text():
                raise ValueError("bad markup")
            return original(self, card, page_url)

        with mock.patch.object(WeAreDevelopersProfile, "_card", flaky):
            run = self.run_monitor(self.monitor())
        self.assertEqual((run["status"], run["found"]), ("completed", 2))
        self.assertIn("could not be read", " ".join(run["notes"]))


class StreamingImportTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.store = MemoryStore()
        user = str(uuid.uuid4())
        ws = self.store.create_workspace(user, "W", "w-stream")
        self.ctx = Ctx(ws["id"], user, "owner")
        self.platform = Platform(self.store, storage=LocalFileStorage(Path(scratch.name)), config=PlatformConfig())
        self.imports = self.platform.service("job_imports")

    def do(self, name: str, data: bytes) -> Dict[str, Any]:
        row = self.imports.upload(self.ctx, name, data)
        self.imports.validate(self.ctx, row["id"], row["mapping"])
        task = self.imports.start(self.ctx, row["id"])["task_id"]
        run_task_inline(self.platform, self.ctx.workspace_id, task)
        return self.store.get(self.ctx, "job_imports", row["id"])

    def test_streamed_csv_with_quotes_newlines_and_bom(self) -> None:
        text = ("﻿Job URL,Job Title,Company Name,Location\n"
                'https://x.io/j/1,"Engineer, Senior","Acme, Inc.","Austin, TX"\n'
                'https://x.io/j/2,"Multi\nline title",Beta,\n\n'
                "https://x.io/j/1,Duplicate,Acme,\n")
        done = self.do("a.csv", text.encode("utf-8"))
        self.assertEqual((done["stats"]["rows"], done["stats"]["new"], done["stats"]["duplicates"]), (3, 2, 1))
        titles = {j["title"]: j for j in self.store.all(self.ctx, "job_postings", {})}
        self.assertEqual(titles["Engineer, Senior"]["company_name"], "Acme, Inc.")
        self.assertIn("Multi line title", titles)

    def test_legacy_encoding_falls_back(self) -> None:
        data = "Job URL,Job Title,Company Name\nhttps://x.io/j/1,Développeur,Société\n".encode("cp1252")
        done = self.do("legacy.csv", data)
        self.assertEqual(done["stats"]["new"], 1)
        self.assertEqual(self.store.all(self.ctx, "job_postings", {})[0]["company_name"], "Société")


if __name__ == "__main__":
    unittest.main()
