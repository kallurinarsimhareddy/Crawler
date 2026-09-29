"""De-duplicating result rows, keeping every source URL.

The key, first that applies:

1. ``job_url`` — canonical (no tracking parameters, fragment or trailing slash);
2. a job without a URL — company/domain + title + location;
3. a company row — its domain (from ``website``/``domain``), else its normalised name;
4. anything else — a fingerprint of all requested field values.

When two rows share a key, the first is kept, empty fields in it are filled
from the duplicate, and the duplicate's ``source_url`` is added to
``source_urls``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from cloud.intel.core.normalize import company_name_key, domain_of
from cloud.intel.scraper.normalizer import canonical_url

__all__ = ["dedupe_records", "record_key"]


def _company(record: Dict[str, Any]) -> str:
    domain = record.get("domain") or (domain_of(record["website"]) if record.get("website") else None)
    return str(domain or company_name_key(record.get("company_name")) or "").lower()


def record_key(record: Dict[str, Any], fields: Sequence[str], entity: str) -> Optional[Tuple]:
    if record.get("job_url"):
        return ("job_url", (canonical_url(record["job_url"]) or str(record["job_url"])).lower())
    if record.get("job_title"):
        return ("job", _company(record), str(record["job_title"]).strip().lower(),
                str(record.get("location") or "").strip().lower())
    if entity != "job":
        company = _company(record)
        if company:
            return ("company", company)
    values = {name: record.get(name) for name in fields if record.get(name) not in (None, "", [])}
    if not values:
        return None
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()[:24]
    return ("fields", digest)


def dedupe_records(records: Iterable[Dict[str, Any]], fields: Sequence[str] = (), entity: str = "company"
                   ) -> Tuple[List[Dict[str, Any]], int]:
    """Returns ``(unique_records, duplicates_removed)``."""
    kept: Dict[Tuple, Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []
    dupes = 0
    for record in records:
        record.setdefault("source_urls", [record["source_url"]] if record.get("source_url") else [])
        key = record_key(record, fields, entity)
        if key is None:
            out.append(record)
            continue
        first = kept.get(key)
        if first is None:
            kept[key] = record
            out.append(record)
            continue
        dupes += 1
        for url in record["source_urls"]:
            if url not in first["source_urls"]:
                first["source_urls"].append(url)
        for name in fields:
            if first.get(name) in (None, "", []) and record.get(name) not in (None, "", []):
                first[name] = record[name]
                if name in (record.get("_evidence") or {}):
                    first.setdefault("_evidence", {})[name] = record["_evidence"][name]
    return out, dupes
