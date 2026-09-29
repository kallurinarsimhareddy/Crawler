"""The ``scraper`` background task: one run, URL by URL, then the output files.

    for each input URL (cancel / pause checked between URLs):
        Fetching    SafeFetcher; a refusal is recorded as its outcome and not retried around
        Extracting  JSON-LD ─► official ATS API ─► links/headings ─► regex
                    job requests with no jobs on the page follow its ATS board / careers link (≤ 2 pages)
                    AI only for requested fields still empty (and only if the workspace allows it)
        Saving      one scrape_results row per input URL (updated in place on retry)
    then:
        Normalizing normalise ─► validate ─► filters ─► dedupe
        Saving      CSV / XLSX / JSON

Restart recovery and idempotency: a result row is keyed by the input's index, so
a task that is re-delivered after a crash (or retried, or resumed after a pause)
skips every URL that already has a row. ``retry`` re-does only URLs whose
outcome may be transient (timeout, network failure, rate limit).

Live progress is kept in ``scrape_runs.stats.progress`` (and mirrored to the
task): URLs done/total, pages fetched, records, completed, failed, current URL
and stage.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from cloud.intel.core.context import Ctx
from cloud.intel.scraper.dedupe import dedupe_records
from cloud.intel.scraper.exports import write_outputs
from cloud.intel.scraper.extractor import (PageFacts, ai_extract_jobs, ai_fill, build_records, careers_candidates,
                                           extract_page)
from cloud.intel.scraper.fetcher import PageFetcher, browser_renderer
from cloud.intel.scraper.models import FieldValue, Outcome, PageExtraction, TRANSIENT, store_status
from cloud.intel.scraper.normalizer import normalize_record
from cloud.intel.scraper.validator import apply_filters, validate_record

__all__ = ["AIBudget", "run_scrape_task", "scrape_one"]

log = logging.getLogger(__name__)

DEFAULT_MAX_AI_CALLS = 25
MAX_FOLLOW = 2


class AIBudget:
    """The run's AI: at most ``max_calls`` calls, switched off for the rest of the run
    as soon as the provider is unavailable (free quota used up, not allowed…).

    A call that fails (e.g. the provider answers 503) is noted on its page and also
    counted here, so the run's summary says AI did not help; the call is not retried."""

    def __init__(self, provider: Any, max_calls: int, note: Optional[str] = None) -> None:
        self.provider = provider
        self.max_calls = max_calls
        self.calls = 0
        self.note = note
        self.failures = 0
        self.last_failure: Optional[str] = None

    @property
    def summary(self) -> Optional[str]:
        """What the run reports about AI: why it stopped, and any failed calls."""
        parts = [self.note] if self.note else []
        if self.failures:
            parts.append(f"{self.failures} AI call{'s' if self.failures > 1 else ''} failed "
                         f"(last: {self.last_failure}); the fields asked for were left empty")
        return "; ".join(parts) or None

    @property
    def available(self) -> bool:
        if self.provider is not None and self.calls >= self.max_calls and not self.note:
            self.note = f"AI call limit for this run ({self.max_calls}) reached; the rest used rules only"
        return self.provider is not None and self.calls < self.max_calls

    def call(self, fn: Callable[[Any], bool], problems: List[str]) -> bool:
        """Run ``fn(provider)``; returns whether AI was actually used."""
        from cloud.intel.ai.base import AIError, AIUnavailable

        if self.provider is None:
            return False
        if self.calls >= self.max_calls:
            if not self.note:
                self.note = f"AI call limit for this run ({self.max_calls}) reached; the rest used rules only"
            return False
        self.calls += 1
        try:
            return bool(fn(self.provider))
        except AIUnavailable as error:
            self.provider = None
            self.note = f"{error} — the rest of the run used rules only"
            problems.append(f"AI not used: {error}")
        except AIError as error:
            self.failures += 1
            self.last_failure = str(error)[:200]
            problems.append(f"AI extraction skipped: {str(error)[:200]}")
        return False


def _merge_company(base: Dict[str, FieldValue], other: Mapping[str, FieldValue], *, prefer_other: tuple = ()) -> None:
    for name, fv in other.items():
        if name == "website":
            base.setdefault(name, fv)
        elif name not in base or name in prefer_other or fv.confidence > base[name].confidence:
            base[name] = fv


def scrape_one(url: str, schema: Mapping[str, Any], fetcher: PageFetcher, ai: Optional[AIBudget] = None,
               on_stage: Callable[[str], None] = lambda stage: None) -> PageExtraction:
    """Fetch one input URL (and at most :data:`MAX_FOLLOW` careers pages it links to) and extract."""
    fields = list(schema["fields"])
    requested = {f["name"] for f in fields}
    want_jobs = schema.get("entity") == "job"
    now = datetime.now(timezone.utc)
    on_stage("Fetching")
    page = fetcher.fetch(url)
    pages = [{"url": url, "final_url": page.final_url, "outcome": page.outcome, "http_status": page.http_status,
              "rendered": page.rendered}]
    if page.outcome != Outcome.OK:
        return PageExtraction(url, page.final_url, page.outcome, pages=pages, fetched_at=now,
                              problems=[f"{page.outcome}: {page.reason or 'refused'}"
                                        + ("; not bypassed" if page.outcome not in (Outcome.TIMEOUT, Outcome.FAILED,
                                                                                    Outcome.NOT_FOUND) else "")])
    problems: List[str] = []
    if page.truncated:
        problems.append("page was larger than the size limit and was cut")
    on_stage("Extracting")
    facts = extract_page(page.html, page.final_url, fetch_json=fetcher.fetch_json, want_jobs=want_jobs)
    company = dict(facts.company)
    job_facts: PageFacts = facts
    ai_target: Optional[PageFacts] = facts if facts.is_careers_page else None
    follow = want_jobs and not facts.jobs
    follow = follow or (not want_jobs and "ats" in requested and "ats" not in company and bool(facts.careers_links))
    if follow:
        for link in careers_candidates(facts, MAX_FOLLOW):
            on_stage("Fetching")
            sub = fetcher.fetch(link)
            pages.append({"url": link, "final_url": sub.final_url, "outcome": sub.outcome,
                          "http_status": sub.http_status, "rendered": sub.rendered})
            if sub.outcome != Outcome.OK:
                problems.append(f"careers page {link[:120]}: {sub.outcome} ({sub.reason or 'refused'})")
                continue
            on_stage("Extracting")
            sub_facts = extract_page(sub.html, sub.final_url, fetch_json=fetcher.fetch_json, want_jobs=want_jobs)
            _merge_company(company, sub_facts.company,
                           prefer_other=("careers_url", "ats") if sub_facts.is_careers_page else ())
            if sub_facts.is_careers_page and ai_target is None:
                ai_target = sub_facts
            if not want_jobs and "ats" in company:
                break
            if sub_facts.jobs:
                job_facts = sub_facts
                break
    ai_used = False
    if want_jobs and not job_facts.jobs and ai is not None and ai.available:
        target = ai_target or facts
        ai_used = ai.call(lambda provider: ai_extract_jobs(target, fields, provider, problems), problems) or ai_used
        if target.jobs:
            job_facts = target
            if "careers_url" in requested and target is not facts:
                company["careers_url"] = FieldValue(target.url, "url", 0.85, "job postings are listed on this page",
                                                    target.url)
    # Page-level fields still missing (industry, location, custom fields…): one AI call on the input page.
    page_fields = [f for f in fields if f.get("level") != "job" and f["name"] not in company
                   and f["name"] not in ("domain",)]
    if page_fields and ai is not None and ai.available:
        record: Dict[str, FieldValue] = {}
        ai_used = ai.call(lambda provider: ai_fill(facts, record, page_fields, provider, problems), problems) or ai_used
        company.update(record)
    problems.extend(note for f in {id(facts): facts, id(job_facts): job_facts}.values() for note in f.notes)
    if want_jobs and not job_facts.jobs:
        problems.append("no job postings found on the page"
                        + (" or the careers pages it links to" if len(pages) > 1 else ""))
    records = build_records(job_facts, schema, company=company)
    if want_jobs and not records:
        records = [{name: fv for name, fv in company.items() if name in requested}]
    has_value = any(record for record in records)
    return PageExtraction(url, page.final_url, Outcome.OK if has_value else Outcome.EMPTY, records=records,
                          pages=pages, problems=problems, ai_used=ai_used, fetched_at=now,
                          page_text=job_facts.text if want_jobs else facts.text)


def flatten(record: Mapping[str, FieldValue], schema: Mapping[str, Any], *, source_url: str,
            extracted_at: datetime) -> Dict[str, Any]:
    """Normalise, validate and flatten one record into an output row."""
    fields = list(schema["fields"])
    normal = normalize_record(record, fields, now=extracted_at)
    clean, problems = validate_record({name: fv.value for name, fv in normal.items()}, fields)
    kept = {name: fv for name, fv in normal.items() if clean.get(name) is not None}
    confidences = [fv.confidence for fv in kept.values()]
    sources = [fv.source_url for fv in kept.values() if fv.source_url]
    row: Dict[str, Any] = dict(clean)
    row.update({
        "source_url": next((fv.source_url for name, fv in kept.items() if fv.source_url and name.startswith("job_")),
                           None) or (sources[0] if sources else source_url),
        "extraction_method": "+".join(sorted({fv.method for fv in kept.values()})) or None,
        "confidence": round(sum(confidences) / len(confidences), 2) if confidences else None,
        "extracted_at": extracted_at.isoformat(timespec="seconds"),
        "_evidence": {name: {k: v for k, v in fv.as_dict().items() if k != "value"} for name, fv in kept.items()},
        "_problems": problems,
    })
    return row


# --- the task ---------------------------------------------------------------------------------------


def _http(platform: Any) -> Any:
    from cloud.intel.core.http import SafeFetcher

    factory = platform.config.extra.get("fetcher_factory")
    return factory() if factory else SafeFetcher()


def _ai_budget(platform: Any, ctx: Ctx, run: Mapping[str, Any]) -> AIBudget:
    options = run["stats"].get("options") or {}
    if options.get("use_ai") is False:
        return AIBudget(None, 0, "AI turned off for this run")
    try:
        provider = platform.service("ai").for_ctx(ctx, "extraction", run_id=run["id"])
    except Exception as error:  # noqa: BLE001 - AI is optional; rules still work
        return AIBudget(None, 0, f"AI unavailable: {error}")
    if not getattr(provider, "external", False):
        reason = (provider.describe() or {}).get("reason") if hasattr(provider, "describe") else None
        return AIBudget(None, 0, f"rules only: {reason or 'no AI provider'}")
    limit = int(options.get("max_ai_calls") or platform.config.extra.get("scraper_max_ai_calls") or DEFAULT_MAX_AI_CALLS)
    return AIBudget(provider, limit)


class _Progress:
    def __init__(self, platform: Any, ctx: Ctx, run: Dict[str, Any], reporter: Any) -> None:
        self.platform, self.ctx, self.run, self.reporter = platform, ctx, run, reporter
        self.state: Dict[str, Any] = dict(run["stats"].get("progress") or {})

    def set(self, **changes: Any) -> None:
        self.state.update(changes)
        stats = {**self.run["stats"], "progress": dict(self.state)}
        try:
            self.run = self.platform.store.update(self.ctx, "scrape_runs", self.run["id"], {"stats": stats})
        except Exception:  # noqa: BLE001 - progress must never break the work
            log.exception("could not save scrape progress for %s", self.run["id"])
        message = f"{self.state.get('processed', 0)}/{self.state.get('total', 0)} URLs · {self.state.get('stage', '')}"
        self.reporter.progress(message, done=self.state.get("processed", 0), total=self.state.get("total", 0),
                               stage=self.state.get("stage"), current_url=self.state.get("current_url"),
                               records=self.state.get("records", 0), pages=self.state.get("pages", 0))


def run_scrape_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    from cloud.intel.tasks.worker import PermanentTaskError, TaskCancelled, TaskPaused

    store = platform.store
    run = store.find(ctx, "scrape_runs", task["params"].get("run_id", ""))
    if run is None:
        raise PermanentTaskError("scrape run not found")
    if run["status"] == "cancelled":
        raise TaskCancelled()
    schema = run["schema"]
    inputs: List[Dict[str, Any]] = list(run["stats"].get("inputs") or
                                        [{"url": u, "row": i + 1, "batch": "-", "source": "list"}
                                         for i, u in enumerate(run["stats"].get("urls") or [])])
    run = store.update(ctx, "scrape_runs", run["id"], {"status": "running", "error": None})
    existing = {int(r["data"].get("index", -1)): r for r in store.all(ctx, "scrape_results", {"run_id": run["id"]},
                                                                      order="created_at")}
    redo = set()
    if task["params"].get("retry"):
        redo = {i for i, r in existing.items() if r["data"].get("outcome") in TRANSIENT}
    progress = _Progress(platform, ctx, run, reporter)
    progress.set(total=len(inputs), processed=len([i for i in existing if i not in redo]),
                 completed=sum(1 for i, r in existing.items() if i not in redo and r["status"] in ("ok", "empty")),
                 failed=sum(1 for i, r in existing.items() if i not in redo and r["status"] not in ("ok", "empty")),
                 pages=sum(len(r["data"].get("pages") or []) for i, r in existing.items() if i not in redo),
                 records=sum(len(r["data"].get("records") or []) for i, r in existing.items() if i not in redo),
                 stage="Fetching", current_url=None, started_at=progress.state.get("started_at")
                 or datetime.now(timezone.utc).isoformat(timespec="seconds"))
    ai = _ai_budget(platform, ctx, run)
    fetcher = PageFetcher(_http(platform), browser_renderer(bool(platform.config.extra.get("scraper_browser_enabled"))))
    try:
        for index, item in enumerate(inputs):
            if index in existing and index not in redo:
                continue
            if reporter.is_cancelled():
                _finish(platform, ctx, progress, schema, inputs, ai, status="cancelled")
                raise TaskCancelled()
            if reporter.should_pause():
                progress.set(stage="Paused", current_url=None)
                store.update(ctx, "scrape_runs", run["id"], {"status": "queued"})
                raise TaskPaused({"next": index})
            url = item["url"]
            progress.set(current_url=url, stage="Fetching")
            try:
                page = scrape_one(url, schema, fetcher, ai,
                                  on_stage=lambda stage: stage != progress.state.get("stage") and progress.set(stage=stage))
            except Exception as error:  # noqa: BLE001 - one bad page must not sink the run
                log.exception("scraping %s failed", url)
                page = PageExtraction(url, url, Outcome.FAILED, problems=[f"{type(error).__name__}: {error}"[:300]],
                                      fetched_at=datetime.now(timezone.utc))
            progress.set(stage="Saving")
            extracted_at = page.fetched_at or datetime.now(timezone.utc)
            rows = [flatten(r, schema, source_url=page.final_url or url, extracted_at=extracted_at) for r in page.records]
            for row in rows:
                row["input_url"], row["input_row"] = url, item.get("row")
            data = {"index": index, "input": item, "outcome": page.outcome, "records": rows, "pages": page.pages,
                    "ai_used": page.ai_used, "reason": page.problems[0] if page.problems and page.outcome not in
                    (Outcome.OK, Outcome.EMPTY) else None}
            values = {"run_id": run["id"], "url": url[:2048], "final_url": (page.final_url or url)[:2048],
                      "status": store_status(page.outcome), "method": page.method, "data": data,
                      "field_sources": {k: v["method"] for k, v in (rows[0]["_evidence"] if rows else {}).items()},
                      "problems": page.problems[:50], "fetched_at": extracted_at}
            if index in existing:
                store.update(ctx, "scrape_results", existing[index]["id"], values)
            else:
                existing[index] = store.insert(ctx, "scrape_results", values)
            ok = page.outcome in (Outcome.OK, Outcome.EMPTY)
            progress.set(processed=progress.state["processed"] + 1,
                         completed=progress.state["completed"] + (1 if ok else 0),
                         failed=progress.state["failed"] + (0 if ok else 1),
                         pages=progress.state["pages"] + len(page.pages),
                         records=progress.state["records"] + len(rows),
                         ai_calls=ai.calls, ai_failures=ai.failures, ai_note=ai.summary)
        return _finish(platform, ctx, progress, schema, inputs, ai, status="completed")
    except (TaskCancelled, TaskPaused):
        raise
    except Exception as error:
        store.update(ctx, "scrape_runs", run["id"], {"status": "failed", "error": f"{type(error).__name__}: {error}"[:4000]})
        raise


def _finish(platform: Any, ctx: Ctx, progress: _Progress, schema: Mapping[str, Any], inputs: List[Dict[str, Any]],
            ai: AIBudget, *, status: str) -> Dict[str, Any]:
    """Assemble every result row (including ones from before a pause/crash) into the output files."""
    store = platform.store
    run = progress.run
    progress.set(stage="Normalizing", current_url=None)
    results = sorted(store.all(ctx, "scrape_results", {"run_id": run["id"]}, order="created_at"),
                     key=lambda r: int(r["data"].get("index", 0)))
    records: List[Dict[str, Any]] = []
    pages: List[Dict[str, Any]] = []
    outcomes: Dict[str, int] = {}
    problems = 0
    for row in results:
        data = row["data"]
        outcome = data.get("outcome") or row["status"].upper()
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        item = data.get("input") or {}
        pages.append({"row": item.get("row"), "batch": item.get("batch"), "source": item.get("source"),
                      "url": row["url"], "final_url": row["final_url"], "outcome": outcome,
                      "reason": data.get("reason") or "; ".join(row["problems"][:2]) or None,
                      "records": len(data.get("records") or []), "pages_fetched": len(data.get("pages") or []),
                      "ai_used": bool(data.get("ai_used")), "extraction_method": row["method"]})
        for record in data.get("records") or []:
            problems += len(record.get("_problems") or [])
            records.append(dict(record))
    fields = [f["name"] for f in schema["fields"]]
    records, filtered_out = apply_filters(records, schema.get("filters") or [])
    records, duplicates = dedupe_records(records, fields, schema.get("entity", "company"))
    progress.set(stage="Saving")
    summary = {"inputs": len(inputs), "processed": len(results), "outcomes": outcomes, "records": len(records),
               "duplicates_removed": duplicates, "filtered_out": filtered_out, "validation_problems": problems,
               "ai_calls": ai.calls, "ai_failures": ai.failures, "ai_note": ai.summary,
               "status": status}
    files = write_outputs(platform.storage, f"platform/{ctx.workspace_id}/scrape/{run['id']}", run["id"], schema,
                          records, pages, summary)
    counts = {"ok": 0, "blocked": 0, "error": 0, "empty": 0}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    stats = {**progress.run["stats"], "counts": counts, "outcomes": outcomes, "records": len(records),
             "duplicates_removed": duplicates, "filtered_out": filtered_out, "validation_problems": problems,
             "files": files, "ai_calls": ai.calls, "ai_failures": ai.failures, "ai_note": ai.summary,
             "progress": {**progress.state, "stage": "Done" if status == "completed" else "Cancelled",
                          "records": len(records), "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}}
    store.update(ctx, "scrape_runs", run["id"], {"status": status, "stats": stats})
    return {"run_id": run["id"], "records": len(records), "counts": counts, "outcomes": outcomes,
            "duplicates_removed": duplicates, "filtered_out": filtered_out, "ai_calls": ai.calls}
