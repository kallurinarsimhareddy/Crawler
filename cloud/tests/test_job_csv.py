"""Jobs CSV upload (streamed, mapped, validated, resumable, deduplicated, reported) and download
(current page / every match, filters, large background exports, CSV safety, privacy, retry)."""

from __future__ import annotations

import csv
import io
import tempfile
import tracemalloc
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from cloud.intel.core.context import Ctx, ForbiddenError, ValidationError, utcnow
from cloud.intel.job_monitor import exports as jexports
from cloud.intel.job_monitor.importer import IMPORT_FIELDS, import_report_csv, suggest_mapping
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks.worker import run_task_inline
from cloud.shared.storage import LocalFileStorage
from cloud.tests.test_job_monitor import FAST, HOST, ApiTests, _Base

HEADER = ["job_url", "job_title", "company_name", "location", "experience_level", "salary_budget", "keyword_1",
          "keyword_2", "keyword_3", "keyword_4", "keyword_5", "remote", "source", "source_board", "search_term",
          "scraped_date"]


def row(n: int, **over: Any) -> Dict[str, Any]:
    base = {"job_url": f"{HOST}/jobs/ext/{700000 + n}-engineer-{n}", "job_title": f"SAP Engineer {n}",
            "company_name": f"Company {n % 5}", "location": "Austin, TX", "experience_level": "Senior",
            "salary_budget": f"${100 + n}k", "keyword_1": "SAP", "keyword_2": "ABAP", "keyword_3": "", "keyword_4": "",
            "keyword_5": "", "remote": "Remote" if n % 2 else "", "source": "Partner feed", "source_board": "Board X",
            "search_term": "sap abap", "scraped_date": "2026-10-01"}
    base.update(over)
    return base


def csv_bytes(rows: List[Dict[str, Any]], header: Optional[List[str]] = None) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=header or HEADER, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


class UploadTests(_Base):
    def do_import(self, data: bytes, name: str = "jobs.csv", mapping: Optional[Dict[str, Any]] = None):
        up = self.imports.upload(self.ctx, name, data)
        checked = self.imports.validate(self.ctx, up["id"], mapping or up["mapping"])
        started = self.imports.start(self.ctx, up["id"])
        run_task_inline(self.platform, self.ctx.workspace_id, started["task_id"])
        return up, checked, self.store.get(self.ctx, "job_imports", up["id"])

    def test_auto_mapping_of_common_names(self) -> None:
        mapping = suggest_mapping(HEADER)
        self.assertEqual(mapping["Job URL"], "job_url")
        self.assertEqual(mapping["Job Title"], "job_title")
        self.assertEqual(mapping["Company Name"], "company_name")
        self.assertEqual(mapping["Source Board"], "source_board")
        self.assertEqual(mapping["Search Term"], "search_term")
        self.assertEqual(mapping["Keyword 5"], "keyword_5")
        other = suggest_mapping(["URL", "Title", "Company", "Experience", "Salary", "source", "scraped_date"])
        self.assertEqual((other["Job URL"], other["Job Title"], other["Company Name"], other["Experience Level"],
                          other["Salary Budget"]), ("URL", "Title", "Company", "Experience", "Salary"))
        self.assertEqual(set(mapping), set(IMPORT_FIELDS))

    def test_valid_csv_preview_validation_import_and_counts(self) -> None:
        rows = [row(n) for n in range(1, 6)] + [row(1)]                     # one duplicate in the file
        rows.append(row(9, job_url=""))                                      # rejected: no URL
        rows.append(row(10, job_url="javascript:alert(1)"))                  # rejected: not http(s)
        up, checked, done = self.do_import(csv_bytes(rows))
        self.assertEqual(up["row_count"], 8)
        report = checked["validation"]
        self.assertEqual((report["valid"], report["rejected"], report["duplicates_in_file"]), (5, 2, 1))
        self.assertEqual(report["preview"][0]["Source Board"], "Board X")
        stats = done["stats"]
        self.assertEqual((done["status"], stats["rows"], stats["new"], stats["duplicates"], stats["rejected"],
                          stats["percent"]), ("completed", 8, 5, 1, 2, 100.0))
        job = self.store.first(self.ctx, "job_postings", {"url_key": "https://wearedevelopers.com/jobs/ext/700001"})
        self.assertEqual((job["source"], job["source_board"], job["search_term"], job["status"]),
                         ("Partner feed", "Board X", "sap abap", "unknown"))
        self.assertIsNone(job["keyword_3"])                                  # never invented
        self.assertEqual(job["last_import_id"], up["id"])
        text = import_report_csv(done)
        self.assertIn("New jobs,5", text)
        self.assertIn("missing or invalid Job URL", text)
        self.assertEqual(self.store.count(self.ctx, "companies", {}), 0)      # CRM companies never auto-created

    def test_mixed_case_and_slug_urls_are_one_job(self) -> None:
        rows = [row(1), row(1, job_url=f"{HOST.upper().replace('HTTPS', 'https')}/jobs/ext/700001-renamed?utm_source=x"),
                row(1, job_url=f"{HOST}/jobs/ext/700001-engineer-1/")]
        _, _, done = self.do_import(csv_bytes(rows))
        self.assertEqual((done["stats"]["new"], done["stats"]["duplicates"]), (1, 2))
        self.assertEqual(self.store.count(self.ctx, "job_postings", {}), 1)

    def test_empty_malformed_and_missing_columns(self) -> None:
        with self.assertRaises(ValidationError):
            self.imports.upload(self.ctx, "empty.csv", b"")
        with self.assertRaises(ValidationError):
            self.imports.upload(self.ctx, "header-only.csv", b"job_url,job_title\n")
        with self.assertRaises(ValidationError):
            self.imports.upload(self.ctx, "broken.csv", b'job_url,job_title\n"https://x.io/1,unterminated\n' * 1
                                + b"x" * 10)
        with self.assertRaises(ValidationError):
            self.imports.upload(self.ctx, "notes.txt", b"hello")
        up = self.imports.upload(self.ctx, "no-title.csv", csv_bytes([row(1)], header=["job_url", "company_name"]))
        self.assertIsNone(up["mapping"]["Job Title"])
        with self.assertRaises(ValidationError):                             # Job URL + Job Title are required
            self.imports.validate(self.ctx, up["id"], up["mapping"])

    def test_reimport_updates_meaningful_changes_with_history(self) -> None:
        first, _, _ = self.do_import(csv_bytes([row(1), row(2)]))
        second, _, done = self.do_import(csv_bytes([row(1, salary_budget="$999k"), row(2), row(3)]), name="b.csv")
        stats = done["stats"]
        self.assertEqual((stats["new"], stats["updated"], stats["unchanged"], stats["duplicates"]), (1, 1, 1, 0))
        job = self.store.first(self.ctx, "job_postings", {"url_key": "https://wearedevelopers.com/jobs/ext/700001"})
        self.assertEqual(job["salary_budget"], "$999k")
        change = self.store.first(self.ctx, "job_posting_changes", {"job_posting_id": job["id"], "change": "changed"})
        self.assertEqual((change["import_id"], change["changed_fields"], change["before"]["salary_budget"]),
                         (second["id"], ["salary_budget"], "$101k"))
        # blank cells never erase stored values; both imports stay in the history
        _, _, third = self.do_import(csv_bytes([row(1, salary_budget="", location="")]), name="c.csv")
        self.assertEqual(third["stats"]["unchanged"], 1)
        job = self.store.get(self.ctx, "job_postings", job["id"])
        self.assertEqual((job["salary_budget"], job["location"]), ("$999k", "Austin, TX"))
        self.assertEqual(len(self.store.all(self.ctx, "job_imports", {})), 3)
        self.assertEqual(self.store.get(self.ctx, "job_imports", first["id"])["filename"], "jobs.csv")

    def test_large_csv_streams_with_bounded_memory(self) -> None:
        # Streaming is what keeps memory bounded: the upload is scanned from disk and the import
        # reads 500-row batches. (A 200k-row benchmark on PostgreSQL is in the deployment report.)
        rows = [row(n) for n in range(1, 5_001)]
        data = csv_bytes(rows)
        self.assertGreater(len(data), 800_000)
        with mock.patch("cloud.intel.imports.parse.parse_file", side_effect=AssertionError("CSV parsed in memory")):
            up = self.imports.upload(self.ctx, "big.csv", data)                # scanned, never fully parsed
        self.assertEqual(up["row_count"], 5_000)
        self.imports.validate(self.ctx, up["id"], up["mapping"])
        started = self.imports.start(self.ctx, up["id"])
        tracemalloc.start()
        run_task_inline(self.platform, self.ctx.workspace_id, started["task_id"])
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        done = self.store.get(self.ctx, "job_imports", up["id"])
        self.assertEqual(done["stats"]["new"], 5_000)
        self.assertLess(peak, 60 * 1024 * 1024)

    def test_restart_resumes_from_the_checkpoint_without_duplicates(self) -> None:
        rows = [row(n) for n in range(1, 2_201)]
        up = self.imports.upload(self.ctx, "resume.csv", csv_bytes(rows))
        self.imports.validate(self.ctx, up["id"], up["mapping"])
        started = self.imports.start(self.ctx, up["id"])
        svc = self.platform.service("job_monitors")
        original, calls = svc.upsert_batch, {"n": 0}

        def crash_on_third(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("worker killed")
            return original(*args, **kwargs)

        with mock.patch.object(svc, "upsert_batch", side_effect=crash_on_third):
            run_task_inline(self.platform, self.ctx.workspace_id, started["task_id"])
        mid = self.store.get(self.ctx, "job_imports", up["id"])
        self.assertEqual((mid["status"], mid["checkpoint"]["row"]), ("importing", 1000))
        self.store.update(self.ctx.as_system(), "platform_tasks", started["task_id"], {"run_after": None})
        run_task_inline(self.platform, self.ctx.workspace_id, started["task_id"])
        done = self.store.get(self.ctx, "job_imports", up["id"])
        self.assertEqual((done["status"], done["stats"]["new"], done["stats"]["rows"]), ("completed", 2_200, 2_200))
        self.assertEqual(self.store.count(self.ctx, "job_postings", {}), 2_200)

    def test_workspace_isolation(self) -> None:
        up, _, _ = self.do_import(csv_bytes([row(1)]))
        other_user = str(uuid.uuid4())
        other = Ctx(self.store.create_workspace(other_user, "B", f"b-{uuid.uuid4().hex[:6]}")["id"], other_user, "owner")
        self.assertEqual(self.store.count(other, "job_postings", {}), 0)
        with self.assertRaises(Exception):
            self.store.get(other, "job_imports", up["id"])
        viewer = Ctx(self.ctx.workspace_id, str(uuid.uuid4()), "viewer")
        with self.assertRaises(ForbiddenError):
            self.imports.upload(viewer, "x.csv", csv_bytes([row(2)]))


class DownloadTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.exports = self.platform.service("job_exports")
        importer = UploadTests.do_import
        rows = [row(n) for n in range(1, 21)]
        rows[0]["job_title"] = '=HYPERLINK("http://evil.example","x")'
        rows[1]["company_name"] = 'Comma, "Quoted"\nNewline Inc.'
        rows[2]["location"] = "+1 Main St"
        rows[3]["salary_budget"] = "-5000"
        rows[4]["keyword_1"] = "@cmd"
        rows[5]["company_name"] = "Zürich Ünïcødé GmbH"
        rows[6]["source"] = "Other feed"
        importer(self, csv_bytes(rows))

    def read(self, record: Dict[str, Any]) -> List[List[str]]:
        with self.platform.storage.open(record["storage_key"]) as handle:
            text = handle.read().decode("utf-8-sig")
        return list(csv.reader(io.StringIO(text)))

    def test_all_matching_respects_filters_and_columns(self) -> None:
        record = self.exports.create(self.ctx, scope="all", params={"source": "Partner feed"})
        self.assertEqual(record["status"], "completed")
        table = self.read(record)
        header, body = table[0], table[1:]
        self.assertEqual(header, [h for h, _ in jexports.EXPORT_COLUMNS])
        self.assertEqual(len(body), 19)                                      # "Other feed" excluded
        self.assertEqual(record["row_count"], 19)
        self.assertEqual(self.exports.estimate(self.ctx, {"source": "Partner feed"})["count"], 19)
        self.assertIn("Monitor ID", header)
        self.assertNotIn("content_hash", header)

    def test_no_filters_exports_the_whole_workspace_and_empty_result_has_header(self) -> None:
        self.assertEqual(self.exports.create(self.ctx, scope="all", params={})["row_count"], 20)
        empty = self.exports.create(self.ctx, scope="all", params={"company": "nobody-matches"})
        self.assertEqual((empty["status"], empty["row_count"], len(self.read(empty))), ("completed", 0, 1))

    def test_current_results_are_the_shown_page(self) -> None:
        page = self.exports.create(self.ctx, scope="current", params={"source": "Partner feed"},
                                   page={"limit": 5, "offset": 5, "order": "title"})
        rows = self.read(page)[1:]
        shown = self.platform.service("job_monitors").search_jobs(
            self.ctx, {"source": "Partner feed", "limit": 5, "offset": 5, "order": "title"})["rows"]
        self.assertEqual([r[0] for r in rows], [j["job_url"] for j in shown])
        self.assertEqual(page["scope"], "current")

    def test_csv_safety_quoting_and_formula_injection(self) -> None:
        # import collapses whitespace in names; a stored newline must still survive the export
        two = self.store.first(self.ctx, "job_postings", {"url_key": "https://wearedevelopers.com/jobs/ext/700002"})
        self.store.update(self.ctx, "job_postings", two["id"], {"company_name": 'Comma, "Quoted"\nNewline Inc.'})
        table = self.read(self.exports.create(self.ctx, scope="all", params={}))
        cells = [c for r in table for c in r]
        self.assertIn("'=HYPERLINK(\"http://evil.example\",\"x\")", cells)
        self.assertIn("'+1 Main St", cells)
        self.assertIn("'-5000", cells)
        self.assertIn("'@cmd", cells)
        self.assertIn('Comma, "Quoted"\nNewline Inc.', cells)               # commas, quotes, newline preserved
        self.assertIn("Zürich Ünïcødé GmbH", cells)
        self.assertFalse(any(c.startswith(("=", "+", "-", "@")) for c in cells))

    def test_large_export_runs_in_the_background_with_progress_and_retry(self) -> None:
        with mock.patch.object(jexports, "SYNC_ROWS", 5), mock.patch.object(jexports, "CHUNK", 4):
            record = self.exports.create(self.ctx, scope="all", params={})
            self.assertEqual((record["status"], record["total_rows"]), ("queued", 20))
            again = self.exports.create(self.ctx, scope="all", params={})          # no accidental duplicate
            self.assertEqual((again["id"], again.get("deduplicated")), (record["id"], True))
            real_put = self.platform.storage.put_file
            with mock.patch.object(self.platform.storage, "put_file", side_effect=OSError("disk full")):
                run_task_inline(self.platform, self.ctx.workspace_id, record["task_id"])
            failed = self.store.get(self.ctx, "exports", record["id"])
            self.assertEqual(failed["status"], "failed")
            self.store.update(self.ctx.as_system(), "platform_tasks", record["task_id"], {"run_after": None})
            run_task_inline(self.platform, self.ctx.workspace_id, record["task_id"])
            self.assertTrue(callable(real_put))
        done = self.exports.get(self.ctx, record["id"])
        self.assertEqual((done["status"], done["row_count"], done["progress_rows"], done["available"]),
                         ("completed", 20, 20, True))
        self.assertEqual(len(self.read(done)), 21)                            # rewritten once, not appended

    def test_privacy_and_workspace_isolation(self) -> None:
        record = self.exports.create(self.ctx, scope="all", params={})
        colleague = Ctx(self.ctx.workspace_id, str(uuid.uuid4()), "member")
        self.store.add_member(self.ctx, colleague.user_id, "member")
        with self.assertRaises(ForbiddenError):
            self.exports.get(colleague, record["id"])
        self.assertEqual(self.exports.history(colleague), [])
        other_user = str(uuid.uuid4())
        other = Ctx(self.store.create_workspace(other_user, "B", f"b-{uuid.uuid4().hex[:6]}")["id"], other_user, "owner")
        with self.assertRaises(Exception):
            self.exports.get(other, record["id"])
        self.assertEqual(self.exports.create(other, scope="all", params={})["row_count"], 0)
        with self.assertRaises(ValidationError):
            self.exports.create(self.ctx, scope="everything", params={})
        with self.assertRaises(ValidationError):
            self.exports.create(self.ctx, scope="all", params={"change": "bogus"})


class CsvApiTests(ApiTests):
    test_monitor_import_and_jobs_over_http = None     # inherited harness only; that test runs in its own module

    def test_upload_and_download_over_http_with_isolation(self) -> None:
        data = csv_bytes([row(n) for n in range(1, 4)] + [row(1)])
        r = self.client.post(self.base + "/job-imports", files={"file": ("jobs.csv", data, "text/csv")})
        self.assertEqual(r.status_code, 201, r.text)
        up = r.json()
        self.assertEqual((up["row_count"], up["mapping"]["Search Term"]), (4, "search_term"))
        r = self.client.post(self.base + f"/job-imports/{up['id']}/validate", json={"mapping": up["mapping"]})
        self.assertEqual(r.status_code, 200, r.text)
        started = self.client.post(self.base + f"/job-imports/{up['id']}/start").json()
        run_task_inline(self.platform, self.ws, started["task_id"])
        done = self.client.get(self.base + f"/job-imports/{up['id']}").json()
        self.assertEqual((done["stats"]["new"], done["stats"]["duplicates"]), (3, 1))
        report = self.client.get(self.base + f"/job-imports/{up['id']}/report")
        self.assertEqual(report.status_code, 200)
        self.assertIn("New jobs,3", report.text)
        self.assertEqual(self.client.get(self.base + "/job-exports/estimate?source=Partner%20feed").json()["count"], 3)
        r = self.client.post(self.base + "/job-exports", json={"scope": "all", "params": {"source": "Partner feed"}})
        self.assertEqual(r.status_code, 201, r.text)
        export = r.json()
        file = self.client.get(self.base + f"/job-exports/{export['id']}/download")
        self.assertEqual(file.status_code, 200)
        self.assertIn("attachment", file.headers["content-disposition"])
        lines = list(csv.reader(io.StringIO(file.content.decode("utf-8-sig"))))
        self.assertEqual((lines[0][0], len(lines)), ("Job URL", 4))
        # another workspace's user: no import, no report, no export, no upload into this workspace
        for path in (f"/job-imports/{up['id']}", f"/job-imports/{up['id']}/report", f"/job-exports/{export['id']}",
                     f"/job-exports/{export['id']}/download", f"/exports/{export['id']}/download"):
            self.assertIn(self.mallory.get(self.base + path).status_code, (403, 404), path)
        r = self.mallory.post(self.base + "/job-imports", files={"file": ("x.csv", data, "text/csv")})
        self.assertIn(r.status_code, (403, 404))


    def test_upload_guards_size_and_disk_space(self) -> None:
        from collections import namedtuple

        from cloud.intel.api import routes_job_monitor as routes

        data = csv_bytes([row(n) for n in range(1, 50)])
        with mock.patch.object(routes, "MAX_IMPORT_BYTES", 1024):
            r = self.client.post(self.base + "/job-imports", files={"file": ("big.csv", data, "text/csv")})
        self.assertEqual(r.status_code, 413, r.text)
        usage = namedtuple("usage", "total used free")
        with mock.patch("shutil.disk_usage", return_value=usage(10, 10, 1024)):
            r = self.client.post(self.base + "/job-imports", files={"file": ("x.csv", data, "text/csv")})
        self.assertEqual(r.status_code, 507, r.text)
        self.assertEqual(self.client.get(self.base + "/job-imports").json()["total"], 0)   # nothing half-stored

if __name__ == "__main__":
    unittest.main()
