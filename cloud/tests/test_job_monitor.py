"""Job source monitors: 14-field schema, WeAreDevelopers profile, historical import, change
detection (NEW / CHANGED / UNCHANGED / REOPENED / delayed CLOSED), scheduling, resume,
notifications, SANA chat, queries, company links, API, permissions and isolation.

The site is a scripted WeAreDevelopers lookalike served by a fake HTTP session — the
markup follows the live listing (article cards, colour-coded pills, the turbo-frame
"Load more jobs" cursor link), so the profile is exercised exactly as in production.
"""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from cloud.intel.core.context import ConflictError, Ctx, ForbiddenError, ValidationError
from cloud.intel.job_monitor import diff as jdiff
from cloud.intel.job_monitor.importer import suggest_mapping
from cloud.intel.job_monitor.profiles import get_profile, profile_for_url
from cloud.intel.job_monitor.schema import (JOB_FIELDS, content_hash, job_url_key, keywords_of, normalize_job,
                                            parse_date, remote_of)
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks.worker import run_task_inline
from cloud.shared.storage import LocalFileStorage
from cloud.tests.test_platform_ai_fakes import FakeSession, fake_resolver

HOST = "https://www.wearedevelopers.com"
#: Long-run safety settings with no waiting (retries and end-of-listing confirmations still happen).
FAST = {"job_monitor_retry_delays": [0], "job_monitor_end_delay": 0, "job_monitor_sleep": lambda _s: None}
START = HOST + "/jobs?q=&country=US"


def job(n: int, **over: Any) -> Dict[str, Any]:
    base = {"id": 9000 - n, "title": f"Engineer {n}", "company": f"Company {n % 7}",
            "location": "Austin, TX, United States", "exp": "Expert", "salary": f"${100 + n}k–{150 + n}k",
            "remote": n % 3 == 0, "kws": ["Python", "AWS", "PostgreSQL"]}
    base.update(over)
    return base


def card(j: Dict[str, Any]) -> str:
    slug = j["title"].lower().replace(" ", "-")
    company = (f'<div class="truncate">{j["company"]}</div>' if j.get("company") else "")
    pills = []
    if j.get("exp"):
        pills.append(f'<span class="inline-flex items-center rounded-full bg-primary/10 px-3 text-primary">'
                     f'<span aria-hidden="true" class="mr-1"><span class="block h-0.5 w-3 rounded bg-primary"></span>'
                     f'</span>{j["exp"]}</span>')
    if j.get("salary"):
        pills.append(f'<span class="inline-flex rounded-full bg-secondary/10 text-secondary">{j["salary"]}</span>')
    if j.get("remote"):
        pills.append('<span class="inline-flex rounded-full bg-amber-500/10 text-amber-700">Remote</span>')
    pills += [f'<span class="inline-flex rounded-full bg-base-200 text-base-content/80">{k}</span>'
              for k in j.get("kws", [])]
    return (f'<article class="group relative"><a data-turbo-frame="_top" href="/jobs/ext/{j["id"]}-{slug}">'
            f'<h3 class="mb-3 font-semibold">{j["title"]}</h3><div class="mb-4"><div class="min-w-0 flex-1">{company}'
            f'<div class="truncate text-base-content/60">{j["location"]}</div></div></div>'
            f'<div class="flex flex-wrap">{"".join(pills)}</div></a></article>')


def page_html(jobs: List[Dict[str, Any]], next_cursor: Optional[str]) -> str:
    more = ""
    if next_cursor:
        more = (f'<turbo-frame class="group" id="jobs_pagination"><div><a class="btn" data-turbo-frame='
                f'"jobs_pagination" href="{HOST}/jobs.turbo_stream?country=US&amp;page={next_cursor}">Load more jobs'
                f'</a></div></turbo-frame>')
    return f'<html><body><main><div class="grid">{"".join(card(j) for j in jobs)}</div>{more}</main></body></html>'


def site(jobs: List[Dict[str, Any]], per_page: int = 3) -> Dict[str, Tuple]:
    pages: Dict[str, Tuple] = {}
    chunks = [jobs[i:i + per_page] for i in range(0, len(jobs), per_page)] or [[]]
    for index, chunk in enumerate(chunks):
        url = START if index == 0 else f"{HOST}/jobs?country=US&page=C{index + 1}"
        nxt = f"C{index + 2}" if index + 1 < len(chunks) else None
        pages[url] = (200, page_html(chunk, nxt))
    return pages


def page_url(n: int) -> str:
    return START if n == 1 else f"{HOST}/jobs?country=US&page=C{n}"


class SchemaTests(unittest.TestCase):
    def test_field_order_is_the_mandatory_schema(self) -> None:
        self.assertEqual(JOB_FIELDS, ("Job URL", "Job Title", "Company Name", "Location", "Experience Level",
                                      "Salary Budget", "Keyword 1", "Keyword 2", "Keyword 3", "Keyword 4",
                                      "Keyword 5", "Remote", "Source", "Scraped Date"))

    def test_url_normalization_and_wad_identity(self) -> None:
        a = job_url_key("https://www.wearedevelopers.com/jobs/ext/3145489-application-programmer?utm_source=x#top")
        b = job_url_key("https://wearedevelopers.com/jobs/ext/3145489-application-programmer-renamed/")
        self.assertEqual(a, "https://wearedevelopers.com/jobs/ext/3145489")
        self.assertEqual(a, b)
        self.assertEqual(job_url_key("HTTPS://Jobs.Example.com/a/b/?gh_jid=5&utm_medium=y"),
                         "https://jobs.example.com/a/b?gh_jid=5")
        self.assertIsNone(job_url_key("javascript:alert(1)"))
        self.assertIsNone(job_url_key("  "))

    def test_content_hash_ignores_case_whitespace_and_bookkeeping(self) -> None:
        a, _ = normalize_job({"job_url": "https://x.io/j/1", "title": "Data  Engineer", "salary_budget": "$1k"},
                             source="A", scraped_date=date(2026, 9, 1))
        b, _ = normalize_job({"job_url": "https://x.io/j/1", "title": "data engineer ", "salary_budget": "$1K"},
                             source="B", scraped_date=date(2026, 10, 1))
        c, _ = normalize_job({"job_url": "https://x.io/j/1", "title": "Data Engineer", "salary_budget": "$2k"})
        self.assertEqual(a["content_hash"], b["content_hash"])
        self.assertNotEqual(a["content_hash"], c["content_hash"])
        self.assertEqual(len(content_hash({})), 64)

    def test_missing_fields_stay_blank_and_nothing_is_invented(self) -> None:
        values, problems = normalize_job({"job_url": "https://x.io/j/2", "title": "Engineer"})
        self.assertEqual(problems, [])
        for column in ("company_name", "location", "experience_level", "salary_budget", "keyword_1", "keyword_5",
                       "remote", "source", "scraped_date"):
            self.assertIsNone(values[column], column)

    def test_rejects_rows_without_url_or_title(self) -> None:
        self.assertEqual(normalize_job({"title": "X"})[0], None)
        self.assertIn("missing Job Title", normalize_job({"job_url": "https://x.io/1"})[1])
        self.assertIsNone(normalize_job({"job_url": "not a url", "title": "X"})[0])

    def test_keywords_first_five_distinct_in_source_order(self) -> None:
        self.assertEqual(keywords_of(["Go", "go", "AWS", "", None, "K8s", "SQL", "Rust", "Java"]),
                         ["Go", "AWS", "K8s", "SQL", "Rust"])
        self.assertEqual(keywords_of(["Go"]), ["Go", None, None, None, None])

    def test_remote_values(self) -> None:
        self.assertEqual(remote_of("yes"), "Remote")
        self.assertEqual(remote_of("Remote"), "Remote")
        self.assertEqual(remote_of("on-site"), "On-site")
        self.assertEqual(remote_of("Hybrid"), "Hybrid")
        self.assertIsNone(remote_of(""))
        self.assertEqual(remote_of("Remote (US only)"), "Remote (US only)")

    def test_dates(self) -> None:
        for raw in ("2026-09-04", "09/04/2026", "2026-09-04T08:00:00Z", datetime(2026, 9, 4, 3), 46269, "46269"):
            self.assertEqual(parse_date(raw), date(2026, 9, 4), raw)
        self.assertIsNone(parse_date("yesterday"))
        values, problems = normalize_job({"job_url": "https://x.io/3", "title": "T", "scraped_date": "soon"},
                                         scraped_date=date(2026, 1, 1))
        self.assertIsNone(values["scraped_date"])  # an unreadable date is not replaced by a guess
        self.assertTrue(problems)


class ProfileTests(unittest.TestCase):
    def test_wearedevelopers_cards(self) -> None:
        profile = get_profile("wearedevelopers")
        jobs = [job(1, remote=True), job(2, company=None, salary=None, exp=None, kws=[]),
                job(3, kws=["Go", "Go", "AWS", "K8s", "SQL", "Rust", "Java"])]
        listing = profile.parse_listing(page_html(jobs, "CUR2"), START)
        self.assertEqual(listing.cards, 3)
        first, second, third = listing.records
        self.assertEqual(first["job_url"], HOST + "/jobs/ext/8999-engineer-1")
        self.assertEqual((first["title"], first["company_name"], first["location"]),
                         ("Engineer 1", "Company 1", "Austin, TX, United States"))
        self.assertEqual((first["experience_level"], first["salary_budget"], first["remote"]),
                         ("Expert", "$101k–151k", "Remote"))
        self.assertEqual(first["keywords"], ["Python", "AWS", "PostgreSQL"])
        self.assertIsNone(second["company_name"])                # no company on the card -> blank, not the city
        self.assertEqual(second["location"], "Austin, TX, United States")
        self.assertEqual((second["salary_budget"], second["experience_level"], second["remote"]), (None, None, None))
        self.assertEqual(listing.next_url, HOST + "/jobs?country=US&page=CUR2")
        values, _ = normalize_job(third, source="WeAreDevelopers")
        self.assertEqual([values[f"keyword_{i}"] for i in range(1, 6)], ["Go", "AWS", "K8s", "SQL", "Rust"])
        self.assertIsNone(profile.parse_listing(page_html(jobs, None), START).next_url)

    def test_profile_lookup(self) -> None:
        self.assertEqual(profile_for_url(START).name, "wearedevelopers")
        self.assertIsNone(profile_for_url("https://example.com/jobs"))


class MappingTests(unittest.TestCase):
    def test_header_variants(self) -> None:
        mapping = suggest_mapping(["﻿URL", "Position", "Employer", "Job Location", "Experience", "Compensation",
                                   "Keyword 1", "Keyword 2", "Remote Type", "Source", "Scraped Date", "Notes"])
        self.assertEqual(mapping["Job URL"], "﻿URL")
        self.assertEqual(mapping["Job Title"], "Position")
        self.assertEqual(mapping["Company Name"], "Employer")
        self.assertEqual(mapping["Location"], "Job Location")
        self.assertEqual(mapping["Experience Level"], "Experience")
        self.assertEqual(mapping["Salary Budget"], "Compensation")
        self.assertEqual(mapping["Remote"], "Remote Type")
        self.assertIsNone(mapping["Keyword 3"])
        self.assertEqual(suggest_mapping(["Job Link", "Title", "Company"])["Job URL"], "Job Link")


class DiffTests(unittest.TestCase):
    def test_classify(self) -> None:
        values, _ = normalize_job({"job_url": "https://x.io/1", "title": "A", "salary_budget": "$1"})
        self.assertEqual(jdiff.classify(None, values).kind, "new")
        same = {**values, "status": "open"}
        self.assertEqual(jdiff.classify(same, values).kind, "unchanged")
        changed = jdiff.classify({**same, "salary_budget": "$2", "content_hash": "old"}, values)
        self.assertEqual((changed.kind, changed.changed_fields, changed.before), ("changed", ["salary_budget"],
                                                                                 {"salary_budget": "$2"}))
        self.assertEqual(jdiff.classify({**same, "status": "closed"}, values).kind, "reopened")


class GoneHTTP:
    """Stands in for the safe fetcher in gone checks: ``owner.gone`` maps job URL -> status."""

    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def fetch(self, url: str) -> Any:
        from types import SimpleNamespace

        self.owner.gone_calls.append(url)
        return SimpleNamespace(status=self.owner.gone.get(url, 200), blocked=False, error=None)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.store = MemoryStore()
        self.user = str(uuid.uuid4())
        ws = self.store.create_workspace(self.user, "W", f"w-{uuid.uuid4().hex[:6]}")
        self.ctx = Ctx(ws["id"], self.user, "owner")
        self.pages: Dict[str, Tuple] = {}
        self.sessions: List[FakeSession] = []

        def factory():
            from cloud.intel.core.http import SafeFetcher

            session = FakeSession(self.pages)
            self.sessions.append(session)
            return SafeFetcher(session=session, resolver=fake_resolver(), per_host_delay=0)

        # Gone checks: a job's own page is live unless a test lists it in self.gone (url -> status).
        self.gone: Dict[str, int] = {}
        self.gone_calls: List[str] = []
        self.platform = Platform(self.store, storage=LocalFileStorage(Path(scratch.name)),
                                 config=PlatformConfig(extra={"fetcher_factory": factory, **FAST,
                                                              "job_monitor_gone_http": GoneHTTP(self)}))
        self.svc = self.platform.service("job_monitors")
        self.imports = self.platform.service("job_imports")

    def calls(self) -> List[str]:
        return [c for s in self.sessions for c in s.calls if not c.endswith("/robots.txt")]

    def monitor(self, **kw: Any) -> Dict[str, Any]:
        return self.svc.create_monitor(self.ctx, {"source_url": START, "name": "WeAreDevelopers US Jobs", **kw})

    def run_monitor(self, monitor: Dict[str, Any], mode: str = "incremental") -> Dict[str, Any]:
        run = self.svc.start_run(self.ctx, monitor["id"], mode=mode)
        run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
        return self.store.get(self.ctx, "job_monitor_runs", run["id"])

    def jobs(self, **filters: Any) -> List[Dict[str, Any]]:
        return self.store.all(self.ctx, "job_postings", filters)


class ImportTests(_Base):
    def csv_bytes(self, rows: List[List[Any]], header=None) -> bytes:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(header or list(JOB_FIELDS))
        writer.writerows(rows)
        return ("﻿" + out.getvalue()).encode("utf-8")

    def row(self, n: int, **over: Any) -> List[Any]:
        values = {"Job URL": f"{HOST}/jobs/ext/{9000 - n}-engineer-{n}", "Job Title": f"Engineer {n}",
                  "Company Name": f"Company {n % 7}", "Location": "Austin, TX, United States", "Experience Level": "",
                  "Salary Budget": "", "Keyword 1": "Python", "Keyword 2": "", "Keyword 3": "", "Keyword 4": "",
                  "Keyword 5": "", "Remote": "", "Source": "WeAreDevelopers", "Scraped Date": "2026-09-04"}
        values.update(over)
        return [values[f] for f in JOB_FIELDS]

    def do_import(self, data: bytes, name: str = "jobs.csv", **validate: Any) -> Dict[str, Any]:
        row = self.imports.upload(self.ctx, name, data)
        row = self.imports.validate(self.ctx, row["id"], row["mapping"], **validate)
        started = self.imports.start(self.ctx, row["id"])
        run_task_inline(self.platform, self.ctx.workspace_id, started["task_id"])
        return self.store.get(self.ctx, "job_imports", row["id"])

    def test_historical_import_baseline(self) -> None:
        rows = [self.row(n) for n in range(1, 11)]
        rows.append(self.row(1))                                   # duplicate in file
        rows.append(self.row(99, **{"Job URL": ""}))               # rejected: no URL
        rows.append(self.row(98, **{"Scraped Date": "", "Source": ""}))
        upload = self.imports.upload(self.ctx, "baseline.csv", self.csv_bytes(rows))
        self.assertEqual(upload["mapping"]["Job URL"], "Job URL")
        self.assertEqual(upload["row_count"], 13)
        report = self.imports.validate(self.ctx, upload["id"], upload["mapping"])["validation"]
        self.assertEqual((report["valid"], report["rejected"], report["duplicates_in_file"]), (11, 1, 1))
        self.assertEqual(report["filled"]["Salary Budget"], 0)
        self.imports.start(self.ctx, upload["id"])
        done = self.store.get(self.ctx, "job_imports", upload["id"])
        run_task_inline(self.platform, self.ctx.workspace_id, done["task_id"])
        done = self.store.get(self.ctx, "job_imports", upload["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual((done["stats"]["new"], done["stats"]["duplicates"], done["stats"]["rejected"]), (11, 1, 1))
        jobs = self.jobs()
        self.assertEqual(len(jobs), 11)
        one = next(j for j in jobs if j["title"] == "Engineer 1")
        self.assertEqual((one["status"], one["source"], one["scraped_date"]), ("unknown", "WeAreDevelopers",
                                                                               date(2026, 9, 4)))
        self.assertEqual(one["first_seen_at"].date(), date(2026, 9, 4))
        self.assertIsNone(one["salary_budget"])
        blank = next(j for j in jobs if j["title"] == "Engineer 98")
        self.assertIsNone(blank["scraped_date"])                   # no date in the file -> blank, never "today"
        self.assertIsNone(blank["source"])
        notes = self.store.all(self.ctx, "notifications", {"kind": "job_import"})
        self.assertEqual(notes[0]["link"], f"/jobs?import={upload['id']}")
        self.assertEqual(self.svc.search_jobs(self.ctx, {"import": upload["id"]})["total"], 11)

    def test_reimport_fills_blanks_but_never_overwrites(self) -> None:
        self.do_import(self.csv_bytes([self.row(1, **{"Salary Budget": ""})]))
        self.store.update(self.ctx, "job_postings", self.jobs()[0]["id"], {"title": "Engineer 1 (observed)"})
        stats = self.do_import(self.csv_bytes([self.row(1, **{"Job Title": "Old title", "Salary Budget": "$5k"})]),
                               name="again.csv")["stats"]
        self.assertEqual((stats["new"], stats["duplicates"], stats["filled"]), (0, 1, 1))
        stored = self.jobs()[0]
        self.assertEqual((stored["title"], stored["salary_budget"]), ("Engineer 1 (observed)", "$5k"))

    def test_mapping_must_be_corrected_to_real_columns(self) -> None:
        upload = self.imports.upload(self.ctx, "x.csv", self.csv_bytes([["u", "t"]], header=["Link", "Name"]))
        self.assertIsNone(upload["mapping"]["Job Title"])
        with self.assertRaises(ValidationError):
            self.imports.validate(self.ctx, upload["id"], upload["mapping"])
        with self.assertRaises(ValidationError):
            self.imports.validate(self.ctx, upload["id"], {"Job URL": "Link", "Job Title": "Nope"})
        ok = self.imports.validate(self.ctx, upload["id"], {"Job URL": "Link", "Job Title": "Name"})
        self.assertEqual(ok["status"], "validated")

    def test_large_import_in_batches(self) -> None:
        rows = [self.row(n, **{"Job URL": f"https://example.com/jobs/{n}"}) for n in range(1, 1301)]
        done = self.do_import(self.csv_bytes(rows))
        self.assertEqual((done["status"], done["stats"]["new"], done["checkpoint"]["row"]), ("completed", 1300, 1300))
        self.assertEqual(self.store.count(self.ctx, "job_postings", {}), 1300)

    def test_xlsx_import(self) -> None:
        from openpyxl import Workbook

        book = Workbook()
        sheet = book.active
        sheet.append(["Job Link", "Title", "Company", "Scraped Date"])
        sheet.append([f"{HOST}/jobs/ext/1-a", "A", "Acme", datetime(2026, 9, 4)])
        buf = io.BytesIO()
        book.save(buf)
        done = self.do_import(buf.getvalue(), name="jobs.xlsx")
        self.assertEqual(done["stats"]["new"], 1)
        self.assertEqual(self.jobs()[0]["scraped_date"], date(2026, 9, 4))


class MonitorTests(_Base):
    def test_create_detects_profile_and_filters(self) -> None:
        monitor = self.monitor()
        self.assertEqual((monitor["strategy"], monitor["profile"], monitor["source_name"]),
                         ("wad_turbo", "wearedevelopers", "WeAreDevelopers"))
        self.assertEqual(monitor["filters"], {"country": "US"})
        self.assertEqual(monitor["schedule"], "daily")
        self.assertGreater(monitor["next_run_at"], monitor["created_at"])
        with self.assertRaises(ConflictError):
            self.monitor()
        generic = self.svc.create_monitor(self.ctx, {"source_url": "https://careers.example.com/jobs", "name": "G"})
        self.assertEqual(generic["strategy"], "ai_scraper")

    def test_new_changed_unchanged_and_incremental_stop(self) -> None:
        catalog = [job(n) for n in range(1, 13)]                   # 12 jobs, 4 pages
        self.pages.update(site(catalog))
        monitor = self.monitor()
        first = self.run_monitor(monitor)
        self.assertEqual((first["status"], first["pages"], first["found"], first["new_count"]), ("completed", 4, 12, 12))
        self.assertEqual(first["stop_reason"], "end of the listing")
        stored = self.jobs()
        self.assertTrue(all(j["status"] == "open" and j["source"] == "WeAreDevelopers" for j in stored))
        self.assertTrue(all(j["scraped_date"] == datetime.now(timezone.utc).date() for j in stored))

        # Tomorrow: 2 new jobs on top, job 2's salary changed; the old tail is unchanged.
        tomorrow = [job(101, title="Senior Backend Engineer", company="Acme Corp"), job(102)] + catalog
        tomorrow[3] = job(2, salary="$999k")
        self.pages.clear()
        self.pages.update(site(tomorrow))
        self.sessions.clear()
        second = self.run_monitor(self.store.get(self.ctx, "job_source_monitors", monitor["id"]))
        self.assertEqual((second["new_count"], second["changed_count"], second["closed_count"]), (2, 1, 0))
        # stops after 3 pages in a row with only known jobs (pages 2, 3, 4 of 5)
        self.assertEqual(second["pages"], 4)
        self.assertIn("already-known", second["stop_reason"])
        self.assertEqual(second["unchanged_count"], second["found"] - 3)
        self.assertNotIn(page_url(5), self.calls())
        changed = self.jobs(url_key="https://wearedevelopers.com/jobs/ext/8998")[0]
        self.assertEqual((changed["salary_budget"], changed["last_changed_run_id"]), ("$999k", second["id"]))
        history = self.store.all(self.ctx, "job_posting_changes", {"job_posting_id": changed["id"]})
        kinds = sorted(h["change"] for h in history)
        self.assertEqual(kinds, ["changed", "new"])
        before = next(h for h in history if h["change"] == "changed")["before"]
        self.assertEqual(before, {"salary_budget": "$102k–152k"})
        self.assertEqual(self.store.count(self.ctx, "job_postings", {}), 14)   # no duplicates

        # "NEW since last run" = exactly the 2 jobs, through the deep link the notification carries.
        new = self.svc.search_jobs(self.ctx, {"monitor": monitor["id"], "change": "new"})
        self.assertEqual(sorted(r["title"] for r in new["rows"]), ["Engineer 102", "Senior Backend Engineer"])
        self.assertTrue(all(r["change_badge"] == "New" for r in new["rows"]))
        notes = self.store.all(self.ctx, "notifications", {"kind": "job_monitor"}, order="created_at")
        titles = [n["title"] for n in notes]
        self.assertIn("2 new jobs found from WeAreDevelopers", titles)
        self.assertIn("1 job changed", titles)
        link = next(n["link"] for n in notes if n["title"].startswith("2 new"))
        self.assertEqual(link, f"/jobs?monitor={monitor['id']}&run={second['id']}&change=new")
        params = dict(p.split("=") for p in link.split("?")[1].split("&"))
        self.assertEqual(self.svc.search_jobs(self.ctx, params)["total"], 2)

        # SANA chat: compact list of the newest jobs with their real source URLs.
        message = self.store.all(self.ctx, "agent_messages", {})[-1]
        self.assertIn("2 new jobs found.", message["content"])
        self.assertEqual(message["data"]["kind"], "job_monitor_update")
        urls = {j["job_url"] for j in message["data"]["jobs"]}
        self.assertIn(HOST + "/jobs/ext/8899-senior-backend-engineer", urls)
        self.assertEqual(message["data"]["jobs"][0]["keywords"], ["Python", "AWS", "PostgreSQL"])

    def test_delayed_close_and_reopen(self) -> None:
        catalog = [job(n) for n in range(1, 7)]
        self.pages.update(site(catalog))
        monitor = self.monitor()
        self.assertEqual(self.run_monitor(monitor, "full")["status"], "completed")
        gone = catalog.pop(2)
        self.pages.clear()
        self.pages.update(site(catalog))
        sweep1 = self.run_monitor(monitor, "full")
        key = f"https://wearedevelopers.com/jobs/ext/{gone['id']}"
        row = self.jobs(url_key=key)[0]
        self.assertEqual((sweep1["status"], sweep1["closed_count"], row["status"], row["missed_full_sweeps"]),
                         ("completed", 0, "open", 1))
        incremental = self.run_monitor(monitor)                            # daily runs never close
        self.assertEqual(incremental["closed_count"], 0)
        sweep2 = self.run_monitor(monitor, "full")
        row = self.jobs(url_key=key)[0]
        self.assertEqual((sweep2["closed_count"], row["status"], row["closed_run_id"]), (1, "closed", sweep2["id"]))
        self.assertIsNotNone(row["closed_at"])
        closed_note = [n for n in self.store.all(self.ctx, "notifications", {}) if n["title"] == "1 job was closed"]
        self.assertEqual(len(closed_note), 1)
        self.assertEqual(self.svc.search_jobs(self.ctx, {"monitor": monitor["id"], "run": sweep2["id"],
                                                         "change": "closed"})["total"], 1)
        # it comes back: REOPENED, history kept, never deleted
        catalog.insert(2, gone)
        self.pages.clear()
        self.pages.update(site(catalog))
        back = self.run_monitor(monitor)
        row = self.jobs(url_key=key)[0]
        self.assertEqual((back["reopened_count"], row["status"], row["closed_at"]), (1, "open", None))
        kinds = [h["change"] for h in self.store.all(self.ctx, "job_posting_changes", {"job_posting_id": row["id"]},
                                                      order="detected_at")]
        self.assertEqual(kinds, ["new", "closed", "reopened"])

    def test_missing_in_one_sweep_then_seen_resets_the_count(self) -> None:
        catalog = [job(n) for n in range(1, 4)]
        self.pages.update(site(catalog))
        monitor = self.monitor()
        self.run_monitor(monitor, "full")
        self.pages.clear()
        self.pages.update(site(catalog[:2]))
        self.run_monitor(monitor, "full")
        self.pages.clear()
        self.pages.update(site(catalog))
        self.run_monitor(monitor, "full")
        self.pages.clear()
        self.pages.update(site(catalog[:2]))
        self.run_monitor(monitor, "full")
        row = self.jobs(url_key="https://wearedevelopers.com/jobs/ext/8997")[0]
        self.assertEqual((row["status"], row["missed_full_sweeps"]), ("open", 1))   # not 2 consecutive misses

    def test_failed_or_partial_sweeps_never_close(self) -> None:
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
            self.assertIn("page 2", run["stop_reason"])
        self.assertEqual(self.store.count(self.ctx, "job_postings", {"status": "closed"}), 0)
        self.assertEqual({j["missed_full_sweeps"] for j in self.jobs()}, {0})
        note = self.store.all(self.ctx, "notifications", {}, order="-created_at")[0]
        self.assertEqual(note["title"], "Monitor finished as partial: WeAreDevelopers US Jobs")
        # a capped sweep (page limit) is not complete either
        self.svc.update_monitor(self.ctx, monitor["id"], {"max_pages_full": 1})
        self.pages.clear()
        self.pages.update(site(catalog[:6]))                        # 2 pages, only 1 allowed
        capped = self.run_monitor(monitor, "full")
        self.assertEqual((capped["status"], capped["pages"]), ("partial", 1))
        self.assertEqual(self.store.count(self.ctx, "job_postings", {"status": "closed"}), 0)

    def test_a_suspiciously_small_sweep_closes_nothing(self) -> None:
        catalog = [job(n) for n in range(1, 160)]
        self.pages.update(site(catalog, per_page=50))
        monitor = self.monitor()
        self.run_monitor(monitor, "full")
        self.pages.clear()
        self.pages.update(site(catalog[:10], per_page=50))           # layout broke: only 10 visible
        for _ in range(2):
            run = self.run_monitor(monitor, "full")
            self.assertEqual(run["status"], "partial")
        self.assertEqual(self.store.count(self.ctx, "job_postings", {"status": "closed"}), 0)

    def test_first_page_without_jobs_is_not_success(self) -> None:
        self.pages[START] = (200, "<html><body>Maintenance</body></html>")
        run = self.run_monitor(self.monitor())
        self.assertEqual(run["status"], "partial")
        self.assertIn("layout", run["stop_reason"])

    def test_blocked_page_is_reported_not_bypassed(self) -> None:
        self.pages[START] = (403, "<html>Access denied</html>")
        run = self.run_monitor(self.monitor())
        self.assertEqual(run["status"], "partial")
        self.assertIn("BLOCKED", run["stop_reason"])
        self.assertEqual(self.calls().count(START), 1)

    def test_robots_disallow_is_respected(self) -> None:
        self.pages[HOST + "/robots.txt"] = (200, "User-agent: *\nDisallow: /jobs\n")
        self.pages.update(site([job(1)]))
        run = self.run_monitor(self.monitor())
        self.assertEqual((run["status"], run["found"]), ("partial", 0))
        self.assertIn("ROBOTS", run["stop_reason"])
        self.assertNotIn(START, self.calls())

    def test_imported_baseline_then_monitor_only_new_are_new(self) -> None:
        catalog = [job(n) for n in range(1, 10)]
        importer = ImportTests.csv_bytes
        rows = []
        for j in catalog[2:]:
            values, _ = normalize_job(get_profile("wearedevelopers").parse_listing(page_html([j], None), START)
                                      .records[0], source="WeAreDevelopers")
            rows.append([{"job_url": values["job_url"], "title": values["title"], "company_name": values["company_name"],
                          "location": values["location"], "experience_level": values["experience_level"],
                          "salary_budget": values["salary_budget"], "keyword_1": values["keyword_1"],
                          "keyword_2": values["keyword_2"], "keyword_3": values["keyword_3"], "keyword_4": None,
                          "keyword_5": None, "remote": values["remote"], "source": "WeAreDevelopers",
                          "scraped_date": "2026-09-04"}[c] for c in ("job_url", "title", "company_name", "location",
                                                                     "experience_level", "salary_budget", "keyword_1",
                                                                     "keyword_2", "keyword_3", "keyword_4", "keyword_5",
                                                                     "remote", "source", "scraped_date")])
        upload = self.imports.upload(self.ctx, "baseline.csv", importer(self, rows))
        self.imports.validate(self.ctx, upload["id"], upload["mapping"])
        task = self.imports.start(self.ctx, upload["id"])["task_id"]
        run_task_inline(self.platform, self.ctx.workspace_id, task)
        self.assertEqual(self.store.count(self.ctx, "job_postings", {"status": "unknown"}), 7)
        self.pages.update(site(catalog))
        run = self.run_monitor(self.monitor())
        self.assertEqual((run["new_count"], run["changed_count"], run["found"]), (2, 0, 9))
        self.assertEqual(self.store.count(self.ctx, "job_postings", {}), 9)
        self.assertEqual(self.store.count(self.ctx, "job_postings", {"status": "open"}), 9)  # observed -> ACTIVE

    def test_restart_resumes_at_the_next_page(self) -> None:
        self.pages.update(site([job(n) for n in range(1, 13)]))
        monitor = self.monitor()
        run = self.svc.start_run(self.ctx, monitor["id"])
        original = self.svc.upsert_batch
        calls = {"n": 0}

        def crash_on_third(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("worker killed")
            return original(*args, **kwargs)

        with mock.patch.object(self.svc, "upsert_batch", side_effect=crash_on_third):
            task = run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
        self.assertIn(task["status"], ("retrying", "queued"))
        mid = self.store.get(self.ctx, "job_monitor_runs", run["id"])
        self.assertEqual((mid["status"], mid["pages"]), ("running", 2))
        self.assertEqual(mid["checkpoint"]["next_url"], page_url(3))
        self.sessions.clear()
        self.store.update(self.ctx.as_system(), "platform_tasks", run["task_id"], {"run_after": None})
        run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
        done = self.store.get(self.ctx, "job_monitor_runs", run["id"])
        self.assertEqual((done["status"], done["pages"], done["found"], done["new_count"]), ("completed", 4, 12, 12))
        self.assertNotIn(page_url(1), self.calls())                 # pages 1-2 were not read again
        self.assertEqual(self.store.count(self.ctx, "job_postings", {}), 12)

    def test_cancel_marks_the_run(self) -> None:
        self.pages.update(site([job(n) for n in range(1, 7)]))
        monitor = self.monitor()
        run = self.svc.start_run(self.ctx, monitor["id"])
        with mock.patch("cloud.intel.tasks.service.TaskReporter.is_cancelled", return_value=True):
            run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
        self.assertEqual(self.store.get(self.ctx, "job_monitor_runs", run["id"])["status"], "cancelled")

    def test_scheduling(self) -> None:
        self.pages.update(site([job(1)]))
        monitor = self.monitor()
        self.assertEqual(self.svc.tick(self.ctx), 0)                # not due yet
        now = datetime.now(timezone.utc)
        self.store.update(self.ctx, "job_source_monitors", monitor["id"], {"next_run_at": now - timedelta(minutes=1)})
        self.assertEqual(self.svc.tick(self.ctx), 1)
        runs = self.store.all(self.ctx, "job_monitor_runs", {"monitor_id": monitor["id"]})
        self.assertEqual((len(runs), runs[0]["mode"], runs[0]["trigger"]), (1, "incremental", "schedule"))
        nxt = self.store.get(self.ctx, "job_source_monitors", monitor["id"])["next_run_at"]
        self.assertAlmostEqual((nxt - now).total_seconds(), 86400 - 60, delta=5)
        # an active run is never doubled; a due full sweep replaces the incremental run
        self.store.update(self.ctx, "job_source_monitors", monitor["id"], {"next_run_at": now - timedelta(minutes=1)})
        self.assertEqual(self.svc.tick(self.ctx), 0)
        with self.assertRaises(ConflictError):
            self.svc.start_run(self.ctx, monitor["id"])
        run_task_inline(self.platform, self.ctx.workspace_id, runs[0]["task_id"])
        self.store.update(self.ctx, "job_source_monitors", monitor["id"], {
            "next_run_at": now - timedelta(minutes=1), "next_full_sweep_at": now - timedelta(minutes=1)})
        self.assertEqual(self.svc.tick(self.ctx), 1)
        latest = self.store.all(self.ctx, "job_monitor_runs", {"monitor_id": monitor["id"]}, order="-created_at")[0]
        self.assertEqual(latest["mode"], "full")
        run_task_inline(self.platform, self.ctx.workspace_id, latest["task_id"])
        after = self.store.get(self.ctx, "job_source_monitors", monitor["id"])
        self.assertGreater(after["next_full_sweep_at"], now + timedelta(days=6))
        # paused and manual monitors are not scheduled
        self.svc.update_monitor(self.ctx, monitor["id"], {"enabled": False})
        self.store.update(self.ctx, "job_source_monitors", monitor["id"], {"next_run_at": now - timedelta(minutes=1)})
        self.assertEqual(self.svc.tick(self.ctx), 0)

    def test_worker_maintenance_runs_the_tick(self) -> None:
        from cloud.intel.tasks.worker import PERIODIC_SERVICES

        self.assertIn("job_monitors", PERIODIC_SERVICES)
        self.assertTrue(callable(getattr(self.svc, "tick")))

    def test_failed_run_notifies(self) -> None:
        self.pages.update(site([job(1)]))
        monitor = self.monitor()
        run = self.svc.start_run(self.ctx, monitor["id"])
        self.store.update(self.ctx.as_system(), "platform_tasks", run["task_id"], {"max_attempts": 1})
        with mock.patch.object(self.svc, "upsert_batch", side_effect=RuntimeError("database unavailable")):
            run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
        row = self.store.get(self.ctx, "job_monitor_runs", run["id"])
        self.assertEqual(row["status"], "failed")
        self.assertIn("database unavailable", row["error"])
        note = self.store.all(self.ctx, "notifications", {"kind": "job_monitor"})[0]
        self.assertEqual((note["title"], note["severity"], note["link"]),
                         ("Monitor failed: WeAreDevelopers US Jobs", "error", f"/monitors/{monitor['id']}"))

    def test_preview_plan_reads_one_page(self) -> None:
        self.pages.update(site([job(n) for n in range(1, 7)]))
        plan = self.svc.plan(self.ctx, START, preview=True)
        self.assertEqual((plan["name"], plan["profile"], plan["filters"]), ("WeAreDevelopers US Jobs",
                                                                            "wearedevelopers", {"country": "US"}))
        self.assertEqual((plan["preview"]["jobs_on_page"], len(plan["preview"]["sample"])), (3, 3))
        self.assertTrue(plan["preview"]["has_next_page"])
        self.assertEqual(self.calls(), [START])
        self.assertEqual(self.store.count(self.ctx, "job_postings", {}), 0)   # nothing saved


class CompanyAndQueryTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        crm = self.platform.service("crm")
        src = {"source_kind": "manual", "source_name": "test"}
        self.acme = crm.upsert_company(self.ctx, {"name": "Acme Corp", "domain": "acme.com"}, **src)["company"]
        crm.upsert_company(self.ctx, {"name": "Twin Co", "domain": "twin-a.com"}, **src)
        from cloud.intel.core.normalize import normalize_name

        # A second CRM company with the same name (the CRM itself would queue it for review).
        self.store.insert(self.ctx, "companies", {"name": "Twin Co", "normalized_name": normalize_name("Twin Co"),
                                                  "domain": "twin-b.com"})
        catalog = [job(1, company="Acme Corp", kws=["Python", "Kafka"], remote=True),
                   job(2, company="ACME  corp", kws=["Java"]), job(3, company="Twin Co", kws=["Go"]),
                   job(4, company="Unknown LLC", kws=[]), job(5, company=None)]
        self.pages.update(site(catalog))
        self.monitor_row = self.monitor()
        self.first = self.run_monitor(self.monitor_row)

    def test_exact_name_links_and_review_queue(self) -> None:
        companies_before = self.store.count(self.ctx, "companies", {})
        linked = self.jobs(company_id=self.acme["id"])
        self.assertEqual(sorted(j["title"] for j in linked), ["Engineer 1", "Engineer 2"])
        self.assertEqual(self.store.count(self.ctx, "companies", {}), companies_before)   # nothing created
        reviews = {r["company_name"]: r for r in self.store.all(self.ctx, "job_company_reviews", {})}
        self.assertEqual(set(reviews), {"Twin Co", "Unknown LLC"})
        self.assertEqual(len(reviews["Twin Co"]["candidate_ids"]), 2)
        self.assertEqual(self.jobs(title="Engineer 5")[0]["company_match"], "unmatched")
        done = self.svc.resolve_review(self.ctx, reviews["Unknown LLC"]["id"], action="link",
                                       company_id=self.acme["id"])
        self.assertEqual(done["jobs_linked"], 1)
        self.assertEqual(self.jobs(title="Engineer 4")[0]["company_id"], self.acme["id"])
        summary = self.svc.company_jobs(self.ctx, self.acme["id"])
        self.assertEqual((summary["counts"]["open"], summary["counts"]["new"]), (3, 3))
        self.assertEqual(sum(w["new"] for w in summary["activity"]), 3)

    def test_filters_and_conditions(self) -> None:
        q = self.svc.search_jobs
        self.assertEqual(q(self.ctx, {"keyword": "kafka"})["total"], 1)
        self.assertEqual(q(self.ctx, {"remote": "Remote"})["total"], 2)          # jobs 1 and 3 (n % 3 == 0)
        self.assertEqual(q(self.ctx, {"company": "acme"})["total"], 2)
        self.assertEqual(q(self.ctx, {"status": "ACTIVE"})["total"], 5)
        self.assertEqual(q(self.ctx, {"status": "CLOSED"})["total"], 0)
        either = {"any": [{"field": "keyword", "op": "eq", "value": "Go"},
                          {"field": "company", "op": "contains", "value": "unknown"}]}
        self.assertEqual(q(self.ctx, {"conditions": either})["total"], 2)
        both = {"all": [{"field": "company", "op": "contains", "value": "acme"},
                        {"any": [{"field": "keyword", "op": "eq", "value": "Java"},
                                 {"field": "remote", "op": "eq", "value": "Remote"}]}]}
        self.assertEqual(sorted(r["title"] for r in q(self.ctx, {"conditions": both})["rows"]),
                         ["Engineer 1", "Engineer 2"])
        self.assertEqual(q(self.ctx, {"conditions": {"all": [{"field": "salary", "op": "empty"}]}})["total"], 0)
        self.assertEqual(q(self.ctx, {"monitor": self.monitor_row["id"], "since_last_run": "1"})["total"], 5)
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(q(self.ctx, {"first_seen_from": today, "scraped_from": today})["total"], 5)
        with self.assertRaises(ValidationError):
            q(self.ctx, {"conditions": {"all": [{"field": "password", "op": "eq", "value": "x"}]}})
        with self.assertRaises(ValidationError):
            q(self.ctx, {"order": "secret"})

    def test_job_detail_and_original_url(self) -> None:
        row = self.jobs(title="Engineer 1")[0]
        detail = self.svc.job_detail(self.ctx, row["id"])
        job_view = detail["job"]
        self.assertEqual(job_view["job_url"], HOST + "/jobs/ext/8999-engineer-1")   # the real source URL, as read
        self.assertEqual(job_view["fields"]["Job URL"], job_view["job_url"])
        self.assertEqual(set(job_view["fields"]), set(JOB_FIELDS))
        self.assertEqual((job_view["status_label"], job_view["change_badge"]), ("ACTIVE", "New"))
        self.assertEqual(detail["monitor"]["name"], "WeAreDevelopers US Jobs")
        self.assertEqual(detail["company"]["id"], self.acme["id"])
        self.assertEqual(detail["history"][0]["change"], "new")


class SecurityTests(_Base):
    def test_workspace_isolation(self) -> None:
        self.pages.update(site([job(1), job(2)]))
        self.run_monitor(self.monitor())
        other_user = str(uuid.uuid4())
        other = self.store.create_workspace(other_user, "Other", "other-ws")
        octx = Ctx(other["id"], other_user, "owner")
        self.assertEqual(self.svc.search_jobs(octx, {})["total"], 0)
        self.assertEqual(self.store.count(octx, "job_source_monitors", {}), 0)
        row = self.jobs()[0]
        from cloud.intel.core.context import NotFoundError

        with self.assertRaises(NotFoundError):
            self.svc.job_detail(octx, row["id"])

    def test_viewer_cannot_change_anything(self) -> None:
        viewer = Ctx(self.ctx.workspace_id, str(uuid.uuid4()), "viewer")
        with self.assertRaises(ForbiddenError):
            self.svc.create_monitor(viewer, {"source_url": START})
        monitor = self.monitor()
        with self.assertRaises(ForbiddenError):
            self.svc.start_run(viewer, monitor["id"])
        with self.assertRaises(ForbiddenError):
            self.imports.upload(viewer, "a.csv", b"Job URL,Job Title\nhttps://x.io/1,A\n")
        self.assertEqual(self.svc.search_jobs(viewer, {})["total"], 0)     # reading is allowed

    def test_unsafe_source_urls_are_refused(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.create_monitor(self.ctx, {"source_url": "file:///etc/passwd"})
        monitor = self.svc.create_monitor(self.ctx, {"source_url": "http://internal.example/jobs", "name": "Inside"})
        run = self.run_monitor(monitor)
        self.assertEqual(run["status"], "partial")
        self.assertIn("UNSAFE", run["stop_reason"])


class MigrationTests(unittest.TestCase):
    def test_0011_is_generated_and_applied_files_unchanged(self) -> None:
        from cloud.intel.store import ddl

        for version, path in ddl.GENERATED.items():
            self.assertEqual(ddl.generate(version), path.read_text(encoding="utf-8"), version)
        sql = ddl.generate("0011")
        self.assertIn("check (status in ('open', 'closed', 'unknown'))", sql)
        self.assertIn("alter table careercloud.job_postings alter column company_name drop not null;", sql)
        self.assertIn("'job_monitor', 'job_import'", sql)
        for table in ("job_source_monitors", "job_monitor_runs", "job_posting_changes", "job_imports",
                      "job_company_reviews"):
            self.assertIn(f"alter table careercloud.{table} enable row level security;", sql)
            self.assertIn(f"create policy {table}_select on careercloud.{table} for select to authenticated", sql)
        self.assertNotIn("create policy job_posting_changes_update", sql)       # history is append-only
        # 0007 keeps the task kinds it had; 0003 keeps company_name NOT NULL
        self.assertNotIn("job_monitor", ddl.generate("0007"))
        self.assertIn("company_name           text not null", ddl.generate("0003"))


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from cloud.api.auth import DevTokenIssuer
        from cloud.api.main import create_app
        from cloud.api.settings import Settings
        from cloud.worker.dispatcher import NullDispatcher

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.pages = site([job(n) for n in range(1, 5)])
        from cloud.intel.core.http import SafeFetcher

        self.platform = Platform(MemoryStore(), storage=LocalFileStorage(root / "platform"), config=PlatformConfig(
            files_dir=root / "platform", extra={**FAST, "fetcher_factory": lambda: SafeFetcher(
                session=FakeSession(self.pages), resolver=fake_resolver(), per_host_delay=0)}))
        self.issuer = DevTokenIssuer("job-monitor-test-secret-0123456789abcdef")
        app = create_app(Settings(auth_mode="dev", results_dir=root / "results"),
                         storage=LocalFileStorage(root / "results"), token_verifier=self.issuer,
                         dispatcher=NullDispatcher(), platform=self.platform)

        def client(email):
            c = TestClient(app)
            c.headers["Authorization"] = f"Bearer {self.issuer.issue(email)['access_token']}"
            c.__enter__()
            self.addCleanup(c.__exit__, None, None, None)
            return c

        self.client, self.mallory = client("alice@example.com"), client("mallory@example.com")
        ws = self.client.post("/api/v1/workspaces", json={"name": "Alice", "seed": False}).json()["id"]
        self.ws, self.base = ws, f"/api/v1/w/{ws}"

    def test_monitor_import_and_jobs_over_http(self) -> None:
        r = self.client.post(self.base + "/job-monitors/plan", json={"source_url": START, "preview": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["preview"]["jobs_on_page"], 3)
        r = self.client.post(self.base + "/job-monitors", json={"source_url": START, "run_now": True})
        self.assertEqual(r.status_code, 201, r.text)
        monitor = r.json()
        # "Run now" runs once now; the daily schedule starts a day later (no immediate second run)
        from datetime import datetime as _dt
        self.assertGreater(_dt.fromisoformat(monitor["next_run_at"]) - _dt.fromisoformat(monitor["created_at"]),
                           timedelta(hours=23))
        run = self.client.get(self.base + f"/job-monitor-runs?monitor_id={monitor['id']}").json()["items"][0]
        run_task_inline(self.platform, self.ws, run["task_id"])
        detail = self.client.get(self.base + f"/job-monitors/{monitor['id']}").json()
        self.assertEqual((detail["runs"][0]["status"], detail["runs"][0]["new_count"]), ("completed", 4))
        feed = self.client.get(self.base + f"/job-feed?monitor={monitor['id']}&change=new").json()
        self.assertEqual(feed["total"], 4)
        job_id = feed["rows"][0]["id"]
        one = self.client.get(self.base + f"/job-feed/{job_id}").json()
        self.assertTrue(one["job"]["job_url"].startswith(HOST + "/jobs/ext/"))
        search = self.client.post(self.base + "/job-feed/search", json={
            "conditions": {"any": [{"field": "title", "op": "contains", "value": "Engineer 1"}]}})
        self.assertEqual(search.json()["total"], 1)
        self.assertEqual(self.client.post(self.base + f"/job-monitors/{monitor['id']}/run",
                                          json={"mode": "full"}).status_code, 201)
        self.assertEqual(self.client.post(self.base + f"/job-monitors/{monitor['id']}/run",
                                          json={"mode": "full"}).status_code, 409)
        data = "Job URL,Job Title\nhttps://example.com/j/1,Imported\n".encode()
        up = self.client.post(self.base + "/job-imports", files={"file": ("old.csv", data, "text/csv")})
        self.assertEqual(up.status_code, 201, up.text)
        val = self.client.post(self.base + f"/job-imports/{up.json()['id']}/validate",
                               json={"mapping": up.json()["mapping"]})
        self.assertEqual(val.json()["validation"]["valid"], 1)
        started = self.client.post(self.base + f"/job-imports/{up.json()['id']}/start").json()
        run_task_inline(self.platform, self.ws, started["task_id"])
        self.assertEqual(self.client.get(self.base + "/job-feed?status=UNKNOWN").json()["total"], 1)
        # another account cannot read this workspace
        self.assertIn(self.mallory.get(self.base + "/job-feed").status_code, (403, 404))
        self.assertIn(self.mallory.post(self.base + "/job-monitors", json={"source_url": START}).status_code,
                      (403, 404))


if __name__ == "__main__":
    unittest.main()
