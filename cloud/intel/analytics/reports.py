"""Analytics reports: date-ranged, filterable tables and chart series, exportable.

``ReportService`` ("reports") answers ``run(ctx, report, start=, end=, filters=)``
for every report in :data:`REPORTS` with one shape::

    {"report", "title", "description", "range": {"start", "end"}, "filters",
     "columns": [{"key", "label"}], "rows": [...], "series": [{"name", "points": [{"x", "y"}]}],
     "chart": "bar" | "line" | "funnel", "totals": {...}, "notes": [...],
     "empty": bool, "truncated": bool, "generated_at"}

Rules:

* Everything is read through the store with the caller's context, so the
  workspace isolation (RLS in PostgreSQL) of any other read applies.
* **No invented numbers.** Rates are ``None`` (shown as "—") when there is no
  denominator. Money appears only as the sum of deal ``amount`` values that
  were actually recorded, labelled as such; nothing is estimated. AI cost is the
  per-call estimate stored in ``ai_usage`` and is labelled an estimate.
* Opens/clicks appear only if a provider recorded those events.
* Row scans are capped (:data:`SCAN_CAP`); a capped report says ``truncated``.

Saved views live in ``saved_reports`` (report key + filters + date range).
"""

from __future__ import annotations

import csv
import io
from collections import Counter, OrderedDict, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, ValidationError, utcnow

__all__ = ["ReportService", "REPORTS", "SCAN_CAP", "parse_range"]

SCAN_CAP = 20_000
MAX_DAYS = 731


def _day(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValidationError(f"not a date: {value!r} (use YYYY-MM-DD)") from None


def parse_range(start: Any = None, end: Any = None, *, now: Optional[datetime] = None) -> Tuple[date, date]:
    """Inclusive day range; defaults to the last 30 days."""
    today = (now or utcnow()).date()
    end_day = _day(end) or today
    start_day = _day(start) or end_day - timedelta(days=29)
    if start_day > end_day:
        raise ValidationError("start must be on or before end")
    if (end_day - start_day).days + 1 > MAX_DAYS:
        raise ValidationError(f"a report covers at most {MAX_DAYS} days")
    return start_day, end_day


def _bounds(start: date, end: date) -> Tuple[datetime, datetime]:
    return (datetime.combine(start, time.min, tzinfo=timezone.utc),
            datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc))


def _rate(part: float, whole: float) -> Optional[float]:
    return round(part / whole, 4) if whole else None


def _bucket(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return str((value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date())
    if isinstance(value, date):
        return str(value)
    return None


def _days(start: date, end: date) -> List[str]:
    return [str(start + timedelta(days=i)) for i in range((end - start).days + 1)]


class _Scan:
    """Row reads for one report run, remembering whether any read hit the cap."""

    def __init__(self, store: Any, ctx: Ctx, lo: datetime, hi: datetime) -> None:
        self.store, self.ctx, self.lo, self.hi = store, ctx, lo, hi
        self.truncated = False

    def rows(self, entity: str, column: Optional[str] = "created_at", filters: Optional[Mapping[str, Any]] = None
             ) -> List[Dict[str, Any]]:
        f: Dict[str, Any] = dict(filters or {})
        if column:
            f[f"{column}__gte"] = self.lo
            f[f"{column}__lt"] = self.hi
        rows = self.store.all(self.ctx, entity, f, cap=SCAN_CAP)
        if len(rows) >= SCAN_CAP:
            self.truncated = True
        return rows


# --- the report catalogue -----------------------------------------------------------

REPORTS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


def report(key: str, title: str, description: str, *, family: str, filters: Iterable[str] = (),
           chart: str = "bar") -> Callable:
    def register(fn: Callable) -> Callable:
        REPORTS[key] = {"key": key, "title": title, "description": description, "family": family,
                        "filters": list(filters), "chart": chart, "fn": fn}
        return fn

    return register


def _cols(*pairs: Tuple[str, str]) -> List[Dict[str, str]]:
    return [{"key": k, "label": l} for k, l in pairs]


@report("funnel", "GTM funnel", "Records created in the range at each step, from company to won deal.",
        family="pipeline", chart="funnel", filters=("campaign_id",))
def _funnel(svc: "ReportService", scan: _Scan, filters: Mapping[str, Any]) -> Dict[str, Any]:
    campaign = filters.get("campaign_id")
    cf = {"campaign_id": campaign} if campaign else {}
    contacts = scan.rows("contacts")
    events = Counter(e["event"] for e in scan.rows("message_events", "occurred_at", cf))
    opps = scan.rows("opportunities", "created_at", cf)
    steps = [
        ("Companies added", len(scan.rows("companies")) if not campaign else None),
        ("Contacts added", len(contacts) if not campaign else None),
        ("Contacts with a valid email", sum(1 for c in contacts if c.get("email_status") == "VALID")
         if not campaign else None),
        ("Enrolled in sequences", len(scan.rows("sequence_enrollments", "created_at", cf))),
        ("Emails sent", events.get("sent", 0)),
        ("Replies", events.get("replied", 0)),
        ("Deals created", len(opps)),
        ("Deals won", sum(1 for o in opps if o["status"] == "won")),
    ]
    rows, previous = [], None
    for i, (label, count) in enumerate(steps):
        if count is None:
            continue
        rows.append({"id": str(i), "step": label, "count": count,
                     "conversion": _rate(count, previous) if previous is not None else None})
        previous = count
    return {"columns": _cols(("step", "Step"), ("count", "Count"), ("conversion", "From previous step")),
            "rows": rows, "series": [{"name": "Count", "points": [{"x": r["step"], "y": r["count"]} for r in rows]}],
            "notes": ["Campaign filter limits the funnel to campaign-linked steps."] if campaign else []}


def _event_counts(events: Iterable[Mapping[str, Any]], key: str) -> Dict[Any, Counter]:
    out: Dict[Any, Counter] = defaultdict(Counter)
    for e in events:
        out[e.get(key)][e["event"]] += 1
    return out


def _won_value(opps: Iterable[Mapping[str, Any]]) -> Optional[float]:
    amounts = [float(o["amount"]) for o in opps if o["status"] == "won" and o.get("amount") is not None]
    return round(sum(amounts), 2) if amounts else None


@report("campaign_attribution", "Campaign attribution",
        "Per campaign: enrollments, messages and outcomes, and the deals attributed to it.",
        family="campaigns", filters=("campaign_id",))
def _campaigns(svc: "ReportService", scan: _Scan, filters: Mapping[str, Any]) -> Dict[str, Any]:
    campaigns = svc.store.all(scan.ctx, "campaigns", {"id": filters["campaign_id"]} if filters.get("campaign_id")
                              else None, cap=500)
    events = _event_counts(scan.rows("message_events", "occurred_at"), "campaign_id")
    enrollments = Counter(e.get("campaign_id") for e in scan.rows("sequence_enrollments"))
    opps: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for o in scan.rows("opportunities"):
        opps[o.get("campaign_id")].append(o)
    rows = []
    tracked = Counter()
    for c in campaigns:
        ev = events.get(c["id"], Counter())
        tracked.update(ev)
        sent = ev.get("sent", 0)
        won = [o for o in opps.get(c["id"], []) if o["status"] == "won"]
        rows.append({"id": c["id"], "campaign": c["name"], "status": c["status"],
                     "enrollments": enrollments.get(c["id"], 0), "sent": sent, "delivered": ev.get("delivered", 0),
                     "opened": ev.get("opened", 0), "clicked": ev.get("clicked", 0), "replied": ev.get("replied", 0),
                     "bounced": ev.get("bounced", 0), "unsubscribed": ev.get("unsubscribed", 0),
                     "blocked": ev.get("blocked", 0), "reply_rate": _rate(ev.get("replied", 0), sent),
                     "bounce_rate": _rate(ev.get("bounced", 0), sent), "deals": len(opps.get(c["id"], [])),
                     "won": len(won), "won_value": _won_value(won)})
    notes = ["Won value is the sum of deal amounts actually recorded; blank when none were entered."]
    if not tracked.get("opened") and not tracked.get("clicked"):
        notes.append("Opens and clicks show 0 unless a connected provider reports them.")
    return {"columns": _cols(("campaign", "Campaign"), ("status", "Status"), ("enrollments", "Enrolled"),
                             ("sent", "Sent"), ("delivered", "Delivered"), ("opened", "Opened"),
                             ("clicked", "Clicked"), ("replied", "Replied"), ("reply_rate", "Reply rate"),
                             ("bounced", "Bounced"), ("bounce_rate", "Bounce rate"), ("unsubscribed", "Unsubscribed"),
                             ("blocked", "Blocked"), ("deals", "Deals"), ("won", "Won"), ("won_value", "Won value")),
            "rows": rows, "series": [{"name": "Sent", "points": [{"x": r["campaign"], "y": r["sent"]} for r in rows]},
                                     {"name": "Replied", "points": [{"x": r["campaign"], "y": r["replied"]}
                                                                    for r in rows]}],
            "notes": notes, "has_data": any(r["enrollments"] or r["sent"] or r["deals"] for r in rows)}


@report("sequence_performance", "Sequence performance",
        "Per sequence: enrollments by state, messages sent and replies.", family="campaigns",
        filters=("sequence_id",))
def _sequences(svc: "ReportService", scan: _Scan, filters: Mapping[str, Any]) -> Dict[str, Any]:
    seqs = svc.store.all(scan.ctx, "sequences", {"id": filters["sequence_id"]} if filters.get("sequence_id") else None,
                         cap=500)
    enrollments = scan.rows("sequence_enrollments")
    by_seq: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    seq_of: Dict[str, str] = {}
    for e in enrollments:
        by_seq[e["sequence_id"]].append(e)
        seq_of[e["id"]] = e["sequence_id"]
    events: Dict[str, Counter] = defaultdict(Counter)
    for ev in scan.rows("message_events", "occurred_at"):
        if ev.get("enrollment_id") in seq_of:
            events[seq_of[ev["enrollment_id"]]][ev["event"]] += 1
    rows = []
    for s in seqs:
        states = Counter(e["status"] for e in by_seq.get(s["id"], []))
        ev = events.get(s["id"], Counter())
        rows.append({"id": s["id"], "sequence": s["name"], "status": s["status"],
                     "steps": svc.store.count(scan.ctx, "sequence_steps", {"sequence_id": s["id"]}),
                     "enrolled": sum(states.values()), "active": states.get("active", 0),
                     "pending_approval": states.get("pending_approval", 0), "completed": states.get("completed", 0),
                     "replied_state": states.get("replied", 0), "stopped": states.get("stopped", 0)
                     + states.get("suppressed", 0) + states.get("unsubscribed", 0) + states.get("bounced", 0),
                     "sent": ev.get("sent", 0), "replied": ev.get("replied", 0),
                     "reply_rate": _rate(ev.get("replied", 0), ev.get("sent", 0))})
    return {"columns": _cols(("sequence", "Sequence"), ("status", "Status"), ("steps", "Steps"), ("enrolled", "Enrolled"),
                             ("pending_approval", "Awaiting approval"), ("active", "Active"),
                             ("completed", "Completed"), ("stopped", "Stopped"), ("sent", "Sent"),
                             ("replied", "Replies"), ("reply_rate", "Reply rate")),
            "rows": rows, "series": [{"name": "Enrolled", "points": [{"x": r["sequence"], "y": r["enrolled"]}
                                                                     for r in rows]}],
            "has_data": any(r["enrolled"] or r["sent"] for r in rows)}


@report("hiring_trends", "Hiring signal trends", "Hiring signals detected and new job postings, by day and type.",
        family="intelligence", chart="line", filters=("signal_type",))
def _hiring(svc: "ReportService", scan: _Scan, filters: Mapping[str, Any]) -> Dict[str, Any]:
    sf = {"signal_type": filters["signal_type"]} if filters.get("signal_type") else {}
    signals = scan.rows("hiring_signals", "detected_at", sf)
    jobs = scan.rows("job_postings", "first_seen_at")
    days = _days(scan.lo.date(), (scan.hi - timedelta(days=1)).date())
    sig_days = Counter(_bucket(s["detected_at"]) for s in signals)
    job_days = Counter(_bucket(j["first_seen_at"]) for j in jobs)
    by_type = Counter(s["signal_type"] for s in signals)
    companies = defaultdict(set)
    for s in signals:
        companies[s["signal_type"]].add(s["company_id"])
    rows = [{"id": t, "signal_type": t, "signals": n, "companies": len(companies[t])} for t, n in by_type.most_common()]
    return {"columns": _cols(("signal_type", "Signal"), ("signals", "Signals"), ("companies", "Companies")),
            "rows": rows, "series": [{"name": "Signals", "points": [{"x": d, "y": sig_days.get(d, 0)} for d in days]},
                                     {"name": "New jobs", "points": [{"x": d, "y": job_days.get(d, 0)} for d in days]}],
            "totals": {"signals": len(signals), "new_jobs": len(jobs)}, "has_data": bool(signals or jobs)}


@report("prospect_conversion", "Prospect conversion",
        "Companies added in the range by lifecycle stage, and discovery candidates by outcome.", family="pipeline")
def _conversion(svc: "ReportService", scan: _Scan, filters: Mapping[str, Any]) -> Dict[str, Any]:
    companies = [c for c in scan.rows("companies") if c["status"] != "merged"]
    lifecycle = Counter(c["lifecycle"] for c in companies)
    order = ("prospect", "account", "customer", "partner", "disqualified")
    rows = [{"id": k, "stage": k, "companies": lifecycle.get(k, 0), "share": _rate(lifecycle.get(k, 0), len(companies))}
            for k in order]
    with_opps = {o["company_id"] for o in scan.rows("opportunities")}
    candidates = Counter(c["status"] for c in scan.rows("discovery_candidates"))
    return {"columns": _cols(("stage", "Lifecycle"), ("companies", "Companies"), ("share", "Share")),
            "rows": rows, "series": [{"name": "Companies", "points": [{"x": r["stage"], "y": r["companies"]}
                                                                      for r in rows]}],
            "totals": {"companies": len(companies),
                       "companies_with_deals": len(with_opps & {c["id"] for c in companies}),
                       "deal_conversion": _rate(len(with_opps & {c["id"] for c in companies}), len(companies)),
                       "discovery_candidates": dict(candidates)},
            "has_data": bool(companies or candidates)}


@report("opportunity_attribution", "Opportunity attribution",
        "Deals created in the range by source, campaign and triggering signal.", family="pipeline",
        filters=("dimension",))
def _opps(svc: "ReportService", scan: _Scan, filters: Mapping[str, Any]) -> Dict[str, Any]:
    dimension = filters.get("dimension") or "source"
    if dimension not in ("source", "campaign_id", "signal_types", "owner_id"):
        raise ValidationError("dimension must be source, campaign_id, signal_types or owner_id")
    opps = scan.rows("opportunities")
    names = {c["id"]: c["name"] for c in svc.store.all(scan.ctx, "campaigns", cap=500)} \
        if dimension == "campaign_id" else {}
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for o in opps:
        keys = o.get(dimension) if dimension == "signal_types" else [o.get(dimension)]
        for k in keys or [None]:
            label = names.get(k, k) if k else "(none)"
            groups[str(label)].append(o)
    rows = []
    for label, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        won = [o for o in items if o["status"] == "won"]
        lost = sum(1 for o in items if o["status"] == "lost")
        rows.append({"id": label, "group": label, "deals": len(items),
                     "open": sum(1 for o in items if o["status"] == "open"), "won": len(won), "lost": lost,
                     "win_rate": _rate(len(won), len(won) + lost), "won_value": _won_value(won)})
    return {"columns": _cols(("group", dimension.replace("_id", "").replace("_", " ").title()), ("deals", "Deals"),
                             ("open", "Open"), ("won", "Won"), ("lost", "Lost"), ("win_rate", "Win rate"),
                             ("won_value", "Won value (recorded)")),
            "rows": rows, "series": [{"name": "Deals", "points": [{"x": r["group"], "y": r["deals"]} for r in rows]}],
            "notes": ["Win rate = won ÷ (won + lost). Won value sums recorded deal amounts only."]}


@report("source_attribution", "Source attribution",
        "Where companies and contacts came from (provenance), and which deals trace back to each source.",
        family="data")
def _sources(svc: "ReportService", scan: _Scan, filters: Mapping[str, Any]) -> Dict[str, Any]:
    records = scan.rows("source_records", "observed_at")
    first: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in sorted(records, key=lambda r: r["observed_at"]):
        first.setdefault((r["entity_type"], r["entity_id"]), r)
    counts: Dict[str, Counter] = defaultdict(Counter)
    company_source: Dict[str, str] = {}
    for (entity_type, entity_id), r in first.items():
        counts[r["source_kind"]][entity_type] += 1
        if entity_type == "companies":
            company_source[entity_id] = r["source_kind"]
    for o in scan.rows("opportunities"):
        counts[company_source.get(o["company_id"], "(unknown)")]["deals"] += 1
    rows = [{"id": k, "source": k, "companies": v.get("companies", 0), "contacts": v.get("contacts", 0),
             "deals": v.get("deals", 0)} for k, v in sorted(counts.items(), key=lambda kv: -sum(kv[1].values()))]
    return {"columns": _cols(("source", "Source"), ("companies", "Companies"), ("contacts", "Contacts"),
                             ("deals", "Deals")),
            "rows": rows, "series": [{"name": "Companies", "points": [{"x": r["source"], "y": r["companies"]}
                                                                      for r in rows]}],
            "totals": {"jobs_by_source": dict(Counter(j["source_name"] for j in scan.rows("job_postings",
                                                                                           "first_seen_at")))},
            "notes": ["A record is attributed to the first source that provided it within the range."]}


@report("activity_performance", "Activity performance", "Activities logged, by kind and by day.",
        family="team", chart="line", filters=("kind",))
def _activities(svc: "ReportService", scan: _Scan, filters: Mapping[str, Any]) -> Dict[str, Any]:
    acts = scan.rows("activities", "occurred_at", {"kind": filters["kind"]} if filters.get("kind") else None)
    days = _days(scan.lo.date(), (scan.hi - timedelta(days=1)).date())
    per_day = Counter(_bucket(a["occurred_at"]) for a in acts)
    kinds = Counter(a["kind"] for a in acts)
    rows = [{"id": k, "kind": k, "activities": n, "share": _rate(n, len(acts))} for k, n in kinds.most_common()]
    return {"columns": _cols(("kind", "Kind"), ("activities", "Activities"), ("share", "Share")),
            "rows": rows, "series": [{"name": "Activities", "points": [{"x": d, "y": per_day.get(d, 0)} for d in days]}],
            "totals": {"activities": len(acts)}}


@report("owner_performance", "Rep / owner performance",
        "Per user: owned companies and deals, activities logged and tasks completed in the range.", family="team")
def _owners(svc: "ReportService", scan: _Scan, filters: Mapping[str, Any]) -> Dict[str, Any]:
    stats: Dict[str, Counter] = defaultdict(Counter)
    for c in svc.store.all(scan.ctx, "companies", {"owner_id__isnull": False}, cap=SCAN_CAP):
        stats[c["owner_id"]]["companies_owned"] += 1
    for o in scan.rows("opportunities"):
        if o.get("owner_id"):
            stats[o["owner_id"]]["deals"] += 1
            stats[o["owner_id"]][f"deals_{o['status']}"] += 1
    for a in scan.rows("activities", "occurred_at"):
        if a.get("actor_id"):
            stats[a["actor_id"]]["activities"] += 1
    for t in scan.rows("crm_tasks", "updated_at", {"status": "done"}):
        if t.get("assignee_id"):
            stats[t["assignee_id"]]["tasks_done"] += 1
    members = {}
    try:
        members = {m["user_id"]: m.get("role") for m in svc.store.list_members(scan.ctx)}
    except Exception:  # noqa: BLE001 - membership listing is best-effort decoration
        members = {}
    rows = [{"id": uid, "user_id": uid, "role": members.get(uid), "companies_owned": s["companies_owned"],
             "deals": s["deals"], "deals_won": s["deals_won"], "deals_open": s["deals_open"],
             "win_rate": _rate(s["deals_won"], s["deals_won"] + s["deals_lost"]), "activities": s["activities"],
             "tasks_done": s["tasks_done"]} for uid, s in sorted(stats.items(), key=lambda kv: -kv[1]["deals"])]
    return {"columns": _cols(("user_id", "User"), ("role", "Role"), ("companies_owned", "Companies owned"),
                             ("deals", "Deals"), ("deals_open", "Open"), ("deals_won", "Won"), ("win_rate", "Win rate"),
                             ("activities", "Activities"), ("tasks_done", "Tasks done")),
            "rows": rows, "series": [{"name": "Activities", "points": [{"x": r["user_id"][:8], "y": r["activities"]}
                                                                       for r in rows]}],
            "notes": ["Only records with an owner, actor or assignee are counted."]}


@report("list_performance", "List performance",
        "Per list: members, email quality, enrollments, replies and deals among its members.", family="campaigns",
        filters=("list_id",))
def _lists(svc: "ReportService", scan: _Scan, filters: Mapping[str, Any]) -> Dict[str, Any]:
    lists = svc.store.all(scan.ctx, "lists", {"id": filters["list_id"]} if filters.get("list_id") else None, cap=200)
    replied_contacts = {e["contact_id"] for e in scan.rows("message_events", "occurred_at", {"event": "replied"})}
    enrolled_contacts = {e["contact_id"] for e in scan.rows("sequence_enrollments")}
    deal_companies = {o["company_id"] for o in scan.rows("opportunities")}
    rows = []
    for lst in lists:
        members = svc.store.all(scan.ctx, "list_members", {"list_id": lst["id"]}, cap=SCAN_CAP)
        ids = [m["entity_id"] for m in members]
        valid = enrolled = replied = deals = 0
        if lst["entity_type"] == "contacts" and ids:
            contacts = svc.store.all(scan.ctx, "contacts", {"id__in": ids}, cap=SCAN_CAP)
            valid = sum(1 for c in contacts if c.get("email_status") == "VALID")
            enrolled = sum(1 for c in contacts if c["id"] in enrolled_contacts)
            replied = sum(1 for c in contacts if c["id"] in replied_contacts)
            deals = len({c.get("company_id") for c in contacts} & deal_companies)
        elif lst["entity_type"] == "companies":
            deals = len(set(ids) & deal_companies)
        rows.append({"id": lst["id"], "list": lst["name"], "entity_type": lst["entity_type"], "members": len(ids),
                     "valid_emails": valid if lst["entity_type"] == "contacts" else None,
                     "valid_share": _rate(valid, len(ids)) if lst["entity_type"] == "contacts" else None,
                     "enrolled": enrolled, "replied": replied, "deals": deals})
    return {"columns": _cols(("list", "List"), ("entity_type", "Of"), ("members", "Members"),
                             ("valid_emails", "Valid emails"), ("valid_share", "Valid share"), ("enrolled", "Enrolled"),
                             ("replied", "Replied"), ("deals", "Deals")),
            "rows": rows, "series": [{"name": "Members", "points": [{"x": r["list"], "y": r["members"]} for r in rows]}],
            "notes": ["Enrollments, replies and deals are those in the selected range."],
            "has_data": any(r["members"] for r in rows)}


@report("validation_quality", "Email validation quality",
        "Validation results in the range by status and provider, and each validation job.", family="data",
        filters=("provider",))
def _validation(svc: "ReportService", scan: _Scan, filters: Mapping[str, Any]) -> Dict[str, Any]:
    pf = {"provider": filters["provider"]} if filters.get("provider") else None
    results = scan.rows("email_validations", "validated_at", pf)
    statuses = Counter(r["status"] for r in results)
    providers = Counter(r["provider"] for r in results)
    rows = [{"id": s, "status": s, "emails": n, "share": _rate(n, len(results))} for s, n in statuses.most_common()]
    jobs = []
    for j in scan.rows("email_validation_jobs"):
        counts = j.get("counts") or {}
        jobs.append({"id": j["id"], "name": j["name"], "status": j["status"], "rows": j["row_count"],
                     "processed": j["processed"], "valid": counts.get("VALID", 0), "invalid": counts.get("INVALID", 0)})
    return {"columns": _cols(("status", "Status"), ("emails", "Emails"), ("share", "Share")),
            "rows": rows, "series": [{"name": "Emails", "points": [{"x": r["status"], "y": r["emails"]} for r in rows]}],
            "totals": {"validated": len(results), "deliverable_share": _rate(statuses.get("VALID", 0), len(results)),
                       "by_provider": dict(providers), "jobs": jobs},
            "notes": ["Local checks never return VALID (no SMTP probing); VALID requires a configured paid provider."],
            "has_data": bool(results or jobs)}


@report("scraper_usage", "AI Scraper usage", "Scrape runs and pages in the range, by status and page outcome.",
        family="usage", chart="line")
def _scraper(svc: "ReportService", scan: _Scan, filters: Mapping[str, Any]) -> Dict[str, Any]:
    runs = scan.rows("scrape_runs")
    pages = scan.rows("scrape_pages")
    days = _days(scan.lo.date(), (scan.hi - timedelta(days=1)).date())
    per_day = Counter(_bucket(r["created_at"]) for r in runs)
    outcomes = Counter(p["outcome"] for p in pages)
    rows = [{"id": o, "outcome": o, "pages": n, "share": _rate(n, len(pages))} for o, n in outcomes.most_common()]
    return {"columns": _cols(("outcome", "Page outcome"), ("pages", "Pages"), ("share", "Share")),
            "rows": rows, "series": [{"name": "Runs", "points": [{"x": d, "y": per_day.get(d, 0)} for d in days]}],
            "totals": {"runs": len(runs), "runs_by_status": dict(Counter(r["status"] for r in runs)),
                       "pages": len(pages), "records": sum(int(p.get("records") or 0) for p in pages),
                       "browser_pages": sum(1 for p in pages if p.get("browser_used"))},
            "has_data": bool(runs or pages)}


@report("ai_usage", "AI usage", "External AI calls in the range by provider, model and purpose.",
        family="usage", filters=("provider", "purpose"))
def _ai(svc: "ReportService", scan: _Scan, filters: Mapping[str, Any]) -> Dict[str, Any]:
    f = {k: filters[k] for k in ("provider", "purpose") if filters.get(k)}
    calls = scan.rows("ai_usage", "created_at", f)
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for c in calls:
        groups[(c["provider"], c["model"], c["purpose"])].append(c)
    rows = []
    for (provider, model, purpose), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        costs = [float(c["estimated_cost_usd"]) for c in items if c.get("estimated_cost_usd") is not None]
        rows.append({"id": f"{provider}:{model}:{purpose}", "provider": provider, "model": model, "purpose": purpose,
                     "calls": len(items), "succeeded": sum(1 for c in items if c["success"]),
                     "success_rate": _rate(sum(1 for c in items if c["success"]), len(items)),
                     "tokens": sum(int(c.get("total_tokens") or 0) for c in items),
                     "estimated_cost_usd": round(sum(costs), 4) if costs else None})
    days = _days(scan.lo.date(), (scan.hi - timedelta(days=1)).date())
    per_day = Counter(_bucket(c["created_at"]) for c in calls)
    return {"columns": _cols(("provider", "Provider"), ("model", "Model"), ("purpose", "Purpose"), ("calls", "Calls"),
                             ("success_rate", "Success rate"), ("tokens", "Tokens"),
                             ("estimated_cost_usd", "Estimated cost (USD)")),
            "rows": rows, "series": [{"name": "Calls", "points": [{"x": d, "y": per_day.get(d, 0)} for d in days]}],
            "notes": ["Cost is the per-call estimate recorded at call time, not an invoice."]}


# --- the service ----------------------------------------------------------------------

_PERCENT_KEYS = ("rate", "share", "conversion")


def _cell(key: str, value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float) and any(p in key for p in _PERCENT_KEYS):
        return f"{value * 100:.1f}%"
    return value


class ReportService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    def catalog(self) -> List[Dict[str, Any]]:
        return [{k: v for k, v in info.items() if k != "fn"} for info in REPORTS.values()]

    def run(self, ctx: Ctx, key: str, *, start: Any = None, end: Any = None,
            filters: Optional[Mapping[str, Any]] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
        info = REPORTS.get(key)
        if info is None:
            raise ValidationError(f"unknown report {key!r}; one of {', '.join(REPORTS)}")
        allowed = set(info["filters"])
        filters = {k: v for k, v in (filters or {}).items() if v not in (None, "")}
        unknown = set(filters) - allowed
        if unknown:
            raise ValidationError(f"{key} does not filter on {', '.join(sorted(unknown))}")
        start_day, end_day = parse_range(start, end, now=now)
        lo, hi = _bounds(start_day, end_day)
        scan = _Scan(self.store, ctx, lo, hi)
        body = info["fn"](self, scan, filters)
        rows = body.get("rows", [])
        has_data = body.pop("has_data", None)
        empty = not rows if has_data is None else not has_data
        return {"report": key, "title": info["title"], "description": info["description"], "family": info["family"],
                "chart": info["chart"], "range": {"start": str(start_day), "end": str(end_day)}, "filters": filters,
                "columns": body.get("columns", []), "rows": rows, "series": body.get("series", []),
                "totals": body.get("totals", {}), "notes": body.get("notes", []), "empty": empty,
                "truncated": scan.truncated, "generated_at": utcnow().isoformat()}

    def export(self, ctx: Ctx, key: str, fmt: str, *, start: Any = None, end: Any = None,
               filters: Optional[Mapping[str, Any]] = None) -> Tuple[bytes, str, str]:
        from cloud.intel.exports.service import _CONTENT_TYPES, _flat, neutralise_cell

        if fmt not in ("csv", "xlsx"):
            raise ValidationError("format must be csv or xlsx")
        result = self.run(ctx, key, start=start, end=end, filters=filters)
        columns = result["columns"]
        header = [c["label"] for c in columns]
        lines = [[neutralise_cell(_flat(_cell(c["key"], row.get(c["key"])))) for c in columns] for row in result["rows"]]
        stem = f"{key}_{result['range']['start']}_{result['range']['end']}"
        if fmt == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(header)
            writer.writerows(lines)
            data = buffer.getvalue().encode("utf-8-sig")
        else:
            from openpyxl import Workbook

            workbook = Workbook()
            sheet = workbook.active
            sheet.title = key[:31]
            sheet.append(header)
            for line in lines:
                sheet.append(line)
            about = workbook.create_sheet("about")
            about.append(["Report", result["title"]])
            about.append(["Range", f"{result['range']['start']} to {result['range']['end']}"])
            about.append(["Filters", _flat(result["filters"]) or "none"])
            about.append(["Generated", result["generated_at"]])
            for note in result["notes"]:
                about.append(["Note", note])
            out = io.BytesIO()
            workbook.save(out)
            data = out.getvalue()
        audit(self.store, ctx, "report.export", entity_type="saved_reports", summary=f"{key} as {fmt}",
              changes={"report": key, "range": result["range"], "filters": result["filters"], "rows": len(lines)})
        return data, f"{stem}.{fmt}", _CONTENT_TYPES[fmt]

    # --- saved views ------------------------------------------------------------------

    def save_view(self, ctx: Ctx, values: Mapping[str, Any]) -> Dict[str, Any]:
        ctx.require_write()
        key = str(values.get("report") or "")
        if key not in REPORTS:
            raise ValidationError(f"unknown report {key!r}")
        name = str(values.get("name") or "").strip()
        if not name:
            raise ValidationError("name is required")
        date_range = dict(values.get("date_range") or {})
        if date_range:
            parse_range(date_range.get("start"), date_range.get("end"))
        row = self.store.insert(ctx, "saved_reports", {"name": name, "report": key,
                                                       "filters": dict(values.get("filters") or {}),
                                                       "date_range": date_range,
                                                       "shared": bool(values.get("shared", True))})
        audit(self.store, ctx, "report.save_view", entity_type="saved_reports", entity_id=row["id"], summary=name)
        return row
