"""Shared fixtures for the intelligence-track tests (offline, MemoryStore)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional

from cloud.intel.core.context import Ctx, utcnow
from cloud.intel.platform import Platform
from cloud.intel.store.memory import MemoryStore


class RecordingAutomation:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def emit(self, ctx, trigger, event_key, payload):
        self.events.append((trigger, event_key, payload))
        return []


class NoResolver:
    """Forces the exact-key fallback so tests do not depend on another track's resolver."""

    def resolve(self, ctx, candidate):
        raise RuntimeError("resolver not used in these tests")


def make_platform() -> tuple:
    store = MemoryStore()
    user = str(uuid.uuid4())
    ws = store.create_workspace(user, "Intel", f"intel-{uuid.uuid4().hex[:8]}")
    ctx = Ctx(ws["id"], user, "owner")
    platform = Platform(store)
    automation = RecordingAutomation()
    platform.override("automation", automation)
    platform.override("dedupe", NoResolver())
    return platform, ctx, automation


def add_job(platform: Platform, ctx: Ctx, company_id: str, title: str, *, days_ago: float = 1,
            status: str = "open", description: str = "", relevant: bool = True, seniority: str = "mid",
            department: str = "erp", technologies: Optional[List[str]] = None, location: str = "Tulsa, OK",
            closed_days_ago: Optional[float] = None, posted_days_ago: Optional[float] = None) -> Dict[str, Any]:
    now = utcnow()
    url = f"https://jobs.example.com/{uuid.uuid4().hex}"
    return platform.store.insert(ctx, "job_postings", {
        "company_id": company_id, "company_name": "Acme", "title": title, "normalized_title": title.lower(),
        "job_url": url, "url_key": url, "description": description or None, "first_seen_at": now - timedelta(days=days_ago),
        "last_seen_at": now, "status": status, "is_relevant": relevant, "seniority": seniority,
        "department": department, "technologies": technologies or [], "location": location,
        "source_kind": "crawler", "source_name": "careercrawler",
        "closed_at": now - timedelta(days=closed_days_ago) if closed_days_ago is not None else None,
        "posted_at": now - timedelta(days=posted_days_ago) if posted_days_ago is not None else None,
    })
