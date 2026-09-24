"""Monitoring and change detection.

Two sources of ``change_events``:

* **Live events** recorded by the code that observes a change —
  :meth:`MonitoringService.record_change` is called by job ingest (new_job,
  job_closed, ats_changed) and technology recording (technology_added/removed).
* **Snapshot diffs.** A monitor (company, list or segment; daily, weekly or
  monthly) runs as a ``monitor`` task: it snapshots each company into
  ``company_snapshots`` and diffs against the previous snapshot to catch what no
  live path saw — hiring spikes, new or changed contacts, leadership changes,
  careers URL, ATS and status changes. Per-job events are left to live ingest,
  so a posting is never reported twice.

Scheduling is idempotent: the worker's maintenance loop calls
:meth:`schedule_due` every 30 s, and each monitor period gets one task at most
(``idempotency_key = monitor:<id>:<period start>``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional

from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, NotFoundError, ValidationError, utcnow
from cloud.intel.jobs.classify import is_leadership

__all__ = ["MonitoringService", "run_monitor_task", "diff_snapshots", "FREQUENCIES"]

log = logging.getLogger(__name__)

FREQUENCIES = {"daily": timedelta(days=1), "weekly": timedelta(days=7), "monthly": timedelta(days=30)}

#: A hiring spike between snapshots: open roles grew by at least this many AND this ratio.
SPIKE_MIN_NEW = 3
SPIKE_MIN_RATIO = 1.5

_LEADER_SENIORITY = {"vp", "c_level", "c-level", "c-suite", "c suite", "executive", "chief", "owner", "founder",
                     "president"}


def _is_leader(contact: Mapping[str, Any]) -> bool:
    return (str(contact.get("seniority") or "").lower() in _LEADER_SENIORITY
            or is_leadership(str(contact.get("title") or "")))


def diff_snapshots(before: Optional[Mapping[str, Any]], after: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Changes between two company snapshots, as ``change_events`` values (minus company_id)."""
    if not before:
        return []
    changes: List[Dict[str, Any]] = []

    b_open, a_open = int(before.get("open_jobs", 0)), int(after.get("open_jobs", 0))
    if a_open - b_open >= SPIKE_MIN_NEW and a_open >= max(1, b_open) * SPIKE_MIN_RATIO:
        changes.append({"change_type": "hiring_spike", "before": {"open_jobs": b_open}, "after": {"open_jobs": a_open},
                        "summary": f"Open roles rose from {b_open} to {a_open}"})

    b_tech, a_tech = set(before.get("technologies") or []), set(after.get("technologies") or [])
    for tech in sorted(a_tech - b_tech):
        changes.append({"change_type": "technology_added", "before": {}, "after": {"technology": tech},
                        "summary": f"{tech} now observed"})
    for tech in sorted(b_tech - a_tech):
        changes.append({"change_type": "technology_removed", "before": {"technology": tech}, "after": {},
                        "summary": f"{tech} no longer observed"})

    b_contacts: Mapping[str, Any] = before.get("contacts") or {}
    a_contacts: Mapping[str, Any] = after.get("contacts") or {}
    for cid, contact in a_contacts.items():
        if cid not in b_contacts:
            kind = "leadership_change" if _is_leader(contact) else "new_contact"
            changes.append({"change_type": kind, "before": {}, "after": {"contact_id": cid, **contact},
                            "summary": f"{'New leader' if kind == 'leadership_change' else 'New contact'}: "
                                       f"{contact.get('name')} ({contact.get('title') or 'no title'})"})
        else:
            old = b_contacts[cid]
            if (old.get("title"), old.get("status")) != (contact.get("title"), contact.get("status")):
                kind = "leadership_change" if (_is_leader(contact) or _is_leader(old)) else "contact_changed"
                changes.append({"change_type": kind, "before": {"contact_id": cid, **old},
                                "after": {"contact_id": cid, **contact},
                                "summary": f"{contact.get('name')}: {old.get('title')} → {contact.get('title')}"
                                           + (f" ({contact.get('status')})" if contact.get("status") != old.get(
                                               "status") else "")})
    for field, change_type in (("careers_url", "careers_url_changed"), ("ats", "ats_changed"),
                               ("status", "status_changed")):
        if before.get(field) != after.get(field) and (before.get(field) or after.get(field)):
            changes.append({"change_type": change_type, "before": {field: before.get(field)},
                            "after": {field: after.get(field)},
                            "summary": f"{field.replace('_', ' ')}: {before.get(field)} → {after.get(field)}"})
    return changes


class MonitoringService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    # --- change events -----------------------------------------------------------

    def record_change(self, ctx: Ctx, company_id: str, change_type: str, *, before: Optional[Mapping] = None,
                      after: Optional[Mapping] = None, summary: Optional[str] = None,
                      source: Optional[str] = None) -> Dict[str, Any]:
        return self.store.insert(ctx, "change_events", {
            "company_id": company_id, "change_type": change_type, "detected_at": utcnow(),
            "before": dict(before or {}), "after": dict(after or {}), "summary": (summary or "")[:1000] or None,
            "source": (source or "")[:100] or None})

    # --- monitors -------------------------------------------------------------------

    def create_monitor(self, ctx: Ctx, *, name: str, target_type: str, target_id: str, frequency: str = "weekly",
                       watch: Optional[List[str]] = None, enabled: bool = True) -> Dict[str, Any]:
        if frequency not in FREQUENCIES:
            raise ValidationError("frequency must be daily, weekly or monthly")
        entity = {"company": "companies", "list": "lists", "segment": "segments"}.get(target_type)
        if entity is None:
            raise ValidationError("target_type must be company, list or segment")
        self.store.get(ctx, entity, target_id)  # 404 if it is not in this workspace
        row = self.store.insert(ctx, "monitors", {"name": name, "target_type": target_type, "target_id": target_id,
                                                  "frequency": frequency, "watch": watch or [], "enabled": enabled,
                                                  "next_run_at": utcnow()})
        audit(self.store, ctx, "monitor.create", entity_type="monitors", entity_id=row["id"], summary=name)
        return row

    def schedule_due(self, ctx: Ctx, *, now: Optional[datetime] = None) -> int:
        now = now or utcnow()
        count = 0
        for monitor in self.store.all(ctx, "monitors", {"enabled": True}, cap=5000):
            due = monitor.get("next_run_at")
            if due is not None and due > now:
                continue
            period = (due or now).strftime("%Y%m%dT%H%M")
            self.platform.tasks.submit(ctx, "monitor", {"monitor_id": monitor["id"]},
                                       idempotency_key=f"monitor:{monitor['id']}:{period}",
                                       entity_type="monitors", entity_id=monitor["id"])
            step = FREQUENCIES.get(monitor["frequency"], FREQUENCIES["weekly"])
            nxt = (due or now) + step
            while nxt <= now:  # a monitor that was off for a while runs once, not once per missed period
                nxt += step
            self.store.update(ctx, "monitors", monitor["id"], {"next_run_at": nxt})
            count += 1
        return count

    def targets(self, ctx: Ctx, monitor: Mapping[str, Any]) -> List[str]:
        kind, target = monitor["target_type"], monitor["target_id"]
        if kind == "company":
            return [target]
        if kind == "list":
            return [m["entity_id"] for m in self.store.all(ctx, "list_members", {"list_id": target}, cap=10000)
                    if m["entity_type"] == "companies"]
        segment = self.store.get(ctx, "segments", target)
        if segment["entity_type"] != "companies":
            return []
        return [c["id"] for c in self.store.all(ctx, "companies", segment.get("filters") or {}, cap=10000)]

    # --- snapshots ------------------------------------------------------------------

    def snapshot(self, ctx: Ctx, company_id: str) -> Dict[str, Any]:
        company = self.store.get(ctx, "companies", company_id)
        open_jobs = self.store.count(ctx, "job_postings", {"company_id": company_id, "status": "open"})
        techs = sorted({t["technology"] for t in self.store.all(ctx, "company_technologies",
                                                                  {"company_id": company_id, "status": "active"})})
        contacts = {c["id"]: {"name": c["full_name"], "title": c.get("title"), "seniority": c.get("seniority"),
                              "status": c.get("status")}
                    for c in self.store.all(ctx, "contacts", {"company_id": company_id}, cap=2000)}
        return {"open_jobs": open_jobs, "technologies": techs, "contacts": contacts,
                "careers_url": company.get("careers_url"), "ats": company.get("ats"), "status": company.get("status"),
                "hiring_count": company.get("hiring_count"), "lifecycle": company.get("lifecycle")}

    def check_company(self, ctx: Ctx, company_id: str, *, source: str = "monitor") -> List[Dict[str, Any]]:
        """Snapshot a company, diff with its previous snapshot, record the changes."""
        previous = self.store.first(ctx, "company_snapshots", {"company_id": company_id}, order="-taken_at")
        current = self.snapshot(ctx, company_id)
        system = ctx if ctx.system else ctx.as_system()  # snapshots are platform-written
        self.store.insert(system, "company_snapshots", {"company_id": company_id, "taken_at": utcnow(),
                                                        "data": current})
        recorded = []
        for change in diff_snapshots(previous["data"] if previous else None, current):
            recorded.append(self.record_change(ctx, company_id, change["change_type"], before=change["before"],
                                               after=change["after"], summary=change["summary"], source=source))
            if change["change_type"] == "leadership_change":
                from cloud.intel.technology.service import emit_best_effort

                emit_best_effort(self.platform, ctx, "leadership_change",
                                 f"{company_id}:{change['after'].get('contact_id')}:{change['after'].get('title')}",
                                 {"company_id": company_id, **change["after"]})
        return recorded

    def run_monitor(self, ctx: Ctx, monitor_id: str, reporter: Any = None) -> Dict[str, Any]:
        monitor = self.store.get(ctx, "monitors", monitor_id)
        company_ids = self.targets(ctx, monitor)
        watch = set(monitor.get("watch") or [])
        totals = {"companies": len(company_ids), "changes": 0, "signals": 0, "errors": 0}
        for index, company_id in enumerate(company_ids):
            if reporter is not None:
                if reporter.is_cancelled():
                    break
                if reporter.should_pause():
                    from cloud.intel.tasks.worker import TaskPaused

                    raise TaskPaused({"index": index})
                reporter.progress(f"Checking {index + 1}/{len(company_ids)}", done=index, total=len(company_ids))
            try:
                totals["changes"] += len(self.check_company(ctx, company_id))
                if not watch or "signals" in watch:
                    totals["signals"] += len(self.platform.service("signals").detect_for_company(ctx, company_id))
            except NotFoundError:
                continue
            except Exception:  # noqa: BLE001 - one company must not stop the monitor
                log.exception("monitor %s failed on %s", monitor_id, company_id)
                totals["errors"] += 1
        self.store.update(ctx, "monitors", monitor_id, {"last_run_at": utcnow()})
        return totals


def run_monitor_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    service: MonitoringService = platform.service("monitoring")
    params = task.get("params") or {}
    if params.get("monitor_id"):
        return service.run_monitor(ctx, params["monitor_id"], reporter)
    totals = {"companies": 0, "changes": 0}
    for company_id in params.get("company_ids") or []:
        if reporter.is_cancelled():
            break
        totals["companies"] += 1
        totals["changes"] += len(service.check_company(ctx, company_id))
    return totals
