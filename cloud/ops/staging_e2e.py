"""End-to-end staging tests against a running CareerCloud API.

    python -m cloud.ops.staging_e2e --api https://api-staging.<domain> --auth supabase \\
        --worker-kill "ssh stg sudo systemctl kill -s KILL careercloud-staging-worker" \\
        --worker-start "ssh stg sudo systemctl start careercloud-staging-worker" \\
        --worker-restart "ssh stg sudo systemctl restart careercloud-staging-worker" \\
        --api-restart "ssh stg sudo systemctl restart careercloud-staging-api" \\
        --tests 1,2,3,4,5,6 --json-report staging-e2e.json

Credentials come from the environment, never the command line:

* ``--auth supabase``: ``CAREERCLOUD_E2E_SUPABASE_URL``, ``CAREERCLOUD_E2E_SUPABASE_ANON_KEY``,
  ``CAREERCLOUD_E2E_USER_A_EMAIL``/``_PASSWORD``, ``CAREERCLOUD_E2E_USER_B_EMAIL``/``_PASSWORD``
* ``--auth dev``: users are signed in through ``/auth/dev-session`` (local rehearsal only)

The tests crawl real public websites through the real worker. Run
``python -m cloud.ops.safety_report`` first; ``--require-safety-report``
refuses to start unless it passes.

TEST 1  single-company real crawl: discovery, jobs found, persisted, progress, CSV download
TEST 2  bulk crawl of 5 companies: all finish, failures isolated, results downloadable
TEST 3  worker crash mid-job: lease expiry, requeue, completion, no duplicate result records
TEST 4  multi-user isolation: B cannot see, cancel or download A's job
TEST 5  cancellation: running job stops between companies and ends cancelled
TEST 6  infrastructure restart: API and worker restarts; jobs recover
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import requests

TERMINAL = {"completed", "failed", "cancelled"}

# Public company websites. The runner discovers each careers page itself.
SINGLE_CANDIDATES = ["https://supabase.com", "https://linear.app", "https://vercel.com", "https://www.figma.com"]
BULK = ["https://posthog.com", "https://linear.app", "https://vercel.com", "https://supabase.com", "https://sentry.io"]
EIGHT = [
    "https://www.figma.com", "https://gitlab.com", "https://posthog.com", "https://linear.app",
    "https://vercel.com", "https://supabase.com", "https://sentry.io", "https://www.cloudflare.com",
]
#: Crash, cancel and restart need a job long enough to interrupt. Each site is
#: listed twice (16 companies); the crawl stays small and polite.
LONG = EIGHT * 2


class TestFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise TestFailure(message)


@dataclass
class Result:
    number: int
    name: str
    passed: bool = False
    seconds: float = 0.0
    evidence: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class Client:
    def __init__(self, api: str, token: str, *, verify: bool = True) -> None:
        self.api = api.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.verify = verify

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        return self.session.request(method, f"{self.api}{path}", timeout=60, **kwargs)

    def create(self, companies: List[str]) -> str:
        if len(companies) == 1:
            payload: Dict[str, Any] = {"type": "single_company", "website": companies[0]}
        else:
            payload = {"type": "bulk_companies", "companies": [{"website": w} for w in companies]}
        response = self.request("POST", "/api/v1/jobs", json=payload)
        check(response.status_code == 201, f"create job: {response.status_code} {response.text[:300]}")
        return response.json()["job_id"]

    def job(self, job_id: str) -> Dict[str, Any]:
        response = self.request("GET", f"/api/v1/jobs/{job_id}")
        check(response.status_code == 200, f"get job: {response.status_code}")
        return response.json()

    def get(self, path: str) -> Any:
        response = self.request("GET", path)
        check(response.status_code == 200, f"GET {path}: {response.status_code}")
        return response.json()

    poll = 1.0

    def wait(
        self, job_id: str, *, until: Callable[[Dict[str, Any]], bool], timeout: float, log: List,
        abort_if_terminal: bool = False,
    ) -> Dict[str, Any]:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            job = self.job(job_id)
            snapshot = (job["status"], job["progress"]["completed"], job["progress"]["total"], job["progress"]["current_phase"], job["attempts"])
            if snapshot != last:
                log.append({"t": round(time.time(), 1), "status": snapshot[0], "completed": snapshot[1], "total": snapshot[2], "phase": snapshot[3], "attempt": snapshot[4]})
                last = snapshot
            if until(job):
                return job
            if abort_if_terminal and job["status"] in TERMINAL:
                raise TestFailure(f"{job_id} finished ({job['status']}) before the test could act; use a longer job")
            time.sleep(self.poll)
        raise TestFailure(f"timed out after {timeout:.0f}s waiting on {job_id} (last: {last})")

    def download(self, job_id: str, kind: str) -> bytes:
        results = self.get(f"/api/v1/jobs/{job_id}/results")["results"]
        match = [r for r in results if r["kind"] == kind]
        check(len(match) == 1, f"expected exactly one {kind} result, found {len(match)}")
        response = self.request("GET", match[0]["download_url"])
        check(response.status_code == 200, f"download {kind}: {response.status_code}")
        check(len(response.content) == match[0]["size_bytes"], "downloaded size differs from recorded size")
        return response.content


def sign_in(args, which: str) -> str:
    if args.auth == "dev":
        email = f"e2e-user-{which.lower()}@careercloud-staging.test"
        response = requests.post(f"{args.api.rstrip('/')}/api/v1/auth/dev-session", json={"email": email}, timeout=30)
        check(response.status_code == 200, f"dev session for {which}: {response.status_code}")
        return response.json()["access_token"]
    url = os.environ["CAREERCLOUD_E2E_SUPABASE_URL"].rstrip("/")
    anon = os.environ["CAREERCLOUD_E2E_SUPABASE_ANON_KEY"]
    email = os.environ[f"CAREERCLOUD_E2E_USER_{which}_EMAIL"]
    password = os.environ[f"CAREERCLOUD_E2E_USER_{which}_PASSWORD"]
    response = requests.post(
        f"{url}/auth/v1/token?grant_type=password",
        headers={"apikey": anon, "Content-Type": "application/json"},
        json={"email": email, "password": password},
        timeout=30,
    )
    check(response.status_code == 200, f"Supabase sign-in for user {which}: {response.status_code}")
    return response.json()["access_token"]


def run_command(label: str, command: Optional[str], evidence: Dict[str, Any]) -> None:
    check(bool(command), f"--{label} command is required for this test")
    started = time.time()
    completed = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=600)
    evidence.setdefault("commands", []).append({"label": label, "exit": completed.returncode, "seconds": round(time.time() - started, 1)})
    check(completed.returncode == 0, f"{label} failed ({completed.returncode}): {completed.stderr[-300:]}")


# --- tests -----------------------------------------------------------------------


def test_single(a: Client, args, ev: Dict[str, Any]) -> None:
    attempts = []
    for website in SINGLE_CANDIDATES:
        job_id = a.create([website])
        progress: List = []
        job = a.wait(job_id, until=lambda j: j["status"] in TERMINAL, timeout=args.job_timeout, log=progress)
        targets = a.get(f"/api/v1/jobs/{job_id}/targets")["targets"]
        attempts.append({"website": website, "job_id": job_id, "status": job["status"], "jobs_found": job["progress"]["jobs_found"],
                         "platform": targets[0]["platform"], "outcome": targets[0]["outcome"], "error": targets[0]["error"], "progress": progress})
        # A real careers board: a recognised ATS or a discovered careers page, with
        # several postings. One "job" scraped from a homepage is not evidence.
        genuine = targets[0]["platform"] not in (None, "Generic HTML") or targets[0]["jobs_found"] >= 5
        attempts[-1]["genuine_board"] = genuine
        if job["status"] == "completed" and job["progress"]["jobs_found"] >= 3 and genuine:
            summary = json.loads(a.download(job_id, "summary_json"))
            company = summary["companies"][0]
            rows = list(csv.DictReader(io.StringIO(a.download(job_id, "jobs_csv").decode("utf-8-sig"))))
            ev.update(job_id=job_id, website=website, jobs_found=job["progress"]["jobs_found"], csv_rows=len(rows),
                      platform=company["platform"], seed_url=company["seed_url"], discovered=company["discovered"],
                      progress_snapshots=len(progress), sample_titles=[r["job_title"] for r in rows[:3]])
            events = [e["kind"] for e in a.get(f"/api/v1/jobs/{job_id}/events")["events"]]
            target = targets[0]
            ev.update(events=events, observed_running=any(p["status"] == "running" for p in progress),
                      started_at=job["started_at"], completed_at=job["completed_at"], elapsed_seconds=job["elapsed_seconds"])
            check(len(rows) > 0, "CSV has no rows although jobs were found")
            check(job["progress"]["completed"] == job["progress"]["total"] == 1, "progress counters were not persisted")
            check(job["started_at"] and target["started_at"] and target["completed_at"], "progress timestamps missing")
            check(events[:2] == ["created", "claimed"] and events[-1] == "completed", f"unexpected timeline {events}")
            check(company["seed_url"], "no careers page was discovered/used")
            ev["attempts"] = attempts
            return
    ev["attempts"] = attempts
    raise TestFailure("no candidate company produced jobs; see attempts")


def test_bulk(a: Client, args, ev: Dict[str, Any]) -> None:
    job_id = a.create(BULK)
    progress: List = []
    job = a.wait(job_id, until=lambda j: j["status"] in TERMINAL, timeout=args.job_timeout * 3, log=progress)
    targets = a.get(f"/api/v1/jobs/{job_id}/targets")["targets"]
    ev.update(job_id=job_id, status=job["status"], jobs_found=job["progress"]["jobs_found"], failed=job["progress"]["failed"],
              targets=[{k: t[k] for k in ("website", "status", "platform", "jobs_found", "error")} for t in targets])
    check(job["status"] == "completed", f"bulk job ended {job['status']}: {job['error']}")
    check(all(t["status"] in ("completed", "failed", "skipped") for t in targets), "a company did not finish")
    check(job["progress"]["completed"] == len(BULK), "not every company was processed")
    xlsx = a.download(job_id, "jobs_xlsx")
    csv_bytes = a.download(job_id, "jobs_csv")
    ev.update(xlsx_bytes=len(xlsx), csv_bytes=len(csv_bytes))
    check(xlsx[:2] == b"PK", "XLSX download is not a workbook")


def test_crash(a: Client, args, ev: Dict[str, Any]) -> None:
    job_id = a.create(LONG)
    progress: List = []
    before = a.wait(job_id, until=lambda j: j["status"] == "running" and j["progress"]["completed"] >= 2,
                    timeout=args.job_timeout * 2, log=progress, abort_if_terminal=True)
    ev["killed_at_progress"] = f"{before['progress']['completed']}/{before['progress']['total']}"
    run_command("worker-kill", args.worker_kill, ev)
    killed_at = time.time()
    run_command("worker-start", args.worker_start, ev)
    job = a.wait(job_id, until=lambda j: j["status"] in TERMINAL, timeout=args.job_timeout * 6, log=progress)
    events = [e["kind"] for e in a.get(f"/api/v1/jobs/{job_id}/events")["events"]]
    results = a.get(f"/api/v1/jobs/{job_id}/results")["results"]
    kinds = [r["kind"] for r in results]
    ev.update(job_id=job_id, status=job["status"], attempts=job["attempts"], events=events, result_kinds=kinds,
              recovered_seconds=round(time.time() - killed_at), progress=progress)
    check(job["status"] == "completed", f"job ended {job['status']}")
    check(job["attempts"] >= 2, "the job was not re-run after the crash")
    check("reaped_requeued" in events, "no lease-expiry requeue recorded")
    check(len(kinds) == len(set(kinds)), f"duplicate result records: {kinds}")


def test_isolation(a: Client, b: Client, args, ev: Dict[str, Any]) -> None:
    job_id = a.create([BULK[0]])
    a.wait(job_id, until=lambda j: j["status"] in TERMINAL, timeout=args.job_timeout, log=[])
    results = a.get(f"/api/v1/jobs/{job_id}/results")["results"]
    check(results, "user A's job has no results to probe")
    probes = {
        "B get job": b.request("GET", f"/api/v1/jobs/{job_id}").status_code,
        "B targets": b.request("GET", f"/api/v1/jobs/{job_id}/targets").status_code,
        "B events": b.request("GET", f"/api/v1/jobs/{job_id}/events").status_code,
        "B results": b.request("GET", f"/api/v1/jobs/{job_id}/results").status_code,
        "B cancel": b.request("POST", f"/api/v1/jobs/{job_id}/cancel").status_code,
        "B download": b.request("GET", results[0]["download_url"]).status_code,
        "anonymous download": requests.get(f"{a.api}{results[0]['download_url']}", timeout=30).status_code,
    }
    listed = [j["job_id"] for j in b.get("/api/v1/jobs?limit=200")["jobs"]]
    ev.update(job_id=job_id, probes=probes, visible_in_b_list=job_id in listed)
    for name, code in probes.items():
        expected = 401 if name.startswith("anonymous") else 404
        check(code == expected, f"{name}: expected {expected}, got {code}")
    check(job_id not in listed, "A's job appears in B's job list")
    check(a.job(job_id)["status"] in TERMINAL, "A's job changed")


def test_cancel(a: Client, args, ev: Dict[str, Any]) -> None:
    companies = LONG
    job_id = a.create(companies)
    progress: List = []
    before = a.wait(job_id, until=lambda j: j["status"] == "running", timeout=args.job_timeout * 2, log=progress,
                    abort_if_terminal=True)
    ev["cancelled_at_progress"] = f"{before['progress']['completed']}/{before['progress']['total']}"
    response = a.request("POST", f"/api/v1/jobs/{job_id}/cancel")
    check(response.status_code == 200, f"cancel: {response.status_code}")
    requested_at = time.time()
    job = a.wait(job_id, until=lambda j: j["status"] in TERMINAL, timeout=args.job_timeout * 2, log=progress)
    targets = a.get(f"/api/v1/jobs/{job_id}/targets")["targets"]
    events = [e["kind"] for e in a.get(f"/api/v1/jobs/{job_id}/events")["events"]]
    ev.update(job_id=job_id, status=job["status"], completed=job["progress"]["completed"], total=job["progress"]["total"],
              stopped_seconds=round(time.time() - requested_at), events=events,
              target_statuses=[t["status"] for t in targets])
    check(job["status"] == "cancelled", f"job ended {job['status']}")
    check(job["progress"]["completed"] < len(companies), "every company was crawled; cancellation did not stop the run")
    check("cancel_requested" in events and events[-1] == "cancelled", f"unexpected timeline {events}")
    check(a.request("POST", f"/api/v1/jobs/{job_id}/cancel").status_code == 409, "cancelling a finished job should be 409")


def test_restart(a: Client, args, ev: Dict[str, Any]) -> None:
    running = a.create(LONG)
    progress: List = []
    a.wait(running, until=lambda j: j["status"] == "running" and j["progress"]["completed"] >= 1,
           timeout=args.job_timeout * 2, log=progress, abort_if_terminal=True)
    queued = a.create([EIGHT[4]])
    run_command("api-restart", args.api_restart, ev)
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            if requests.get(f"{a.api}/api/v1/health", timeout=10).status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(2)
    run_command("worker-restart", args.worker_restart, ev)
    first = a.wait(running, until=lambda j: j["status"] in TERMINAL, timeout=args.job_timeout * 6, log=progress)
    second = a.wait(queued, until=lambda j: j["status"] in TERMINAL, timeout=args.job_timeout * 4, log=[])
    events = [e["kind"] for e in a.get(f"/api/v1/jobs/{running}/events")["events"]]
    ev.update(running_job=running, running_status=first["status"], running_attempts=first["attempts"], running_events=events,
              queued_job=queued, queued_status=second["status"])
    check(first["status"] == "completed", f"job running during the restart ended {first['status']}")
    check(second["status"] == "completed", f"job queued during the restart ended {second['status']}")
    check("released_on_shutdown" in events or "reaped_requeued" in events or first["attempts"] == 1,
          "no release or recovery recorded for the interrupted job")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloud.ops.staging_e2e")
    parser.add_argument("--api", required=True)
    parser.add_argument("--auth", choices=["supabase", "dev"], default="supabase")
    parser.add_argument("--tests", default="1,2,3,4,5,6")
    parser.add_argument("--worker-kill")
    parser.add_argument("--worker-start")
    parser.add_argument("--worker-restart")
    parser.add_argument("--api-restart")
    parser.add_argument("--job-timeout", type=float, default=600)
    parser.add_argument("--poll", type=float, default=1.0, help="seconds between status polls")
    parser.add_argument("--json-report")
    parser.add_argument("--require-safety-report", metavar="CMD", help="command that must exit 0 before any crawl")
    args = parser.parse_args(argv)

    if args.auth == "dev" and not any(h in args.api for h in ("127.0.0.1", "localhost")):
        print("refusing: --auth dev is only for a local rehearsal API", file=sys.stderr)
        return 2
    if args.require_safety_report:
        check_result = subprocess.run(shlex.split(args.require_safety_report, posix=os.name != "nt"))
        if check_result.returncode != 0:
            print("refusing: the safety report did not pass", file=sys.stderr)
            return 2

    health = requests.get(f"{args.api.rstrip('/')}/api/v1/health", timeout=30).json()
    print(f"API {args.api}: {health}")
    Client.poll = args.poll
    a = Client(args.api, sign_in(args, "A"))
    b = Client(args.api, sign_in(args, "B"))

    plan = {
        1: ("single-company real crawl", lambda ev: test_single(a, args, ev)),
        2: ("bulk crawl (5 companies)", lambda ev: test_bulk(a, args, ev)),
        3: ("worker crash and recovery", lambda ev: test_crash(a, args, ev)),
        4: ("multi-user isolation", lambda ev: test_isolation(a, b, args, ev)),
        5: ("cancellation", lambda ev: test_cancel(a, args, ev)),
        6: ("API and worker restart", lambda ev: test_restart(a, args, ev)),
    }
    results: List[Result] = []
    for number in [int(n) for n in args.tests.split(",") if n.strip()]:
        name, fn = plan[number]
        result = Result(number, name)
        started = time.time()
        print(f"\nTEST {number}: {name} ...", flush=True)
        try:
            fn(result.evidence)
            result.passed = True
        except Exception as error:
            result.error = f"{type(error).__name__}: {error}"
        result.seconds = round(time.time() - started, 1)
        print(f"TEST {number}: {'PASS' if result.passed else 'FAIL'} in {result.seconds}s" + (f" - {result.error}" if result.error else ""))
        results.append(result)

    if args.json_report:
        with open(args.json_report, "w", encoding="utf-8") as handle:
            json.dump({"api": args.api, "health": health, "results": [r.__dict__ for r in results]}, handle, indent=2, default=str)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
