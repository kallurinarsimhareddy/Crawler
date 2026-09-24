"""The concrete sources.

=====================  ==================  ========================================================
Adapter                Access              Status without credentials
=====================  ==================  ========================================================
ats_public             public_api          ok — the ATS vendors' own public job-board APIs
                                           (Greenhouse, Lever, Ashby, SmartRecruiters, Workable,
                                           Recruitee, Teamtailor, Workday CXS) found by the vendored
                                           CareerAutomation ``ats.detect``/``api_endpoints``
careercrawler          public_api          ok — runs the existing CareerCrawler engine as a
                                           ``crawl`` task (60+ adapters, browser fallback)
usajobs                official_api        not_configured — needs an API key + registered email
adzuna                 official_api        not_configured — needs app_id + app_key
ziprecruiter           partner             not_configured — needs a ZipRecruiter publisher API key
linkedin               partner             not_configured — LinkedIn partner program only
indeed                 partner             not_configured — Indeed partner program only
dice                   authorized_account  not_configured — a licensed data feed from Dice
wellfound              authorized_account  not_configured — a licensed data feed from Wellfound
builtin                authorized_account  not_configured — a licensed data feed from Built In
=====================  ==================  ========================================================

None of these scrape a site that requires authorisation. The earlier
job-board crawler's LinkedIn guest-endpoint, Indeed stealth-browser and Monster
DataDome code was audited as NOT reusable (proxy rotation, CAPTCHA/Cloudflare
bypass) and is deliberately absent. Keyed sources whose HTTP shape is
implemented but has never been exercised with a real key report
``configured_unverified`` until :meth:`health` succeeds against the provider.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlencode

from cloud.intel.sources.base import SourceAdapter, SourceError, SourceQuery, SourceUnavailable

__all__ = ["ADAPTERS", "adapter_class"]


def _ts(value: Any) -> Optional[str]:
    """Epoch millis/seconds or ISO text -> ISO 8601 UTC, else None."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _workplace(remote: Any = None, text: str = "") -> str:
    lowered = (text or "").lower()
    if "hybrid" in lowered:
        return "hybrid"
    if remote is True or "remote" in lowered:
        return "remote"
    if lowered in ("onsite", "on-site", "on_site", "in office"):
        return "onsite"
    return "unknown"


def _clean_html(markup: Optional[str], limit: int = 20000) -> Optional[str]:
    if not markup:
        return None
    from bs4 import BeautifulSoup

    text = BeautifulSoup(str(markup), "html.parser").get_text(" ", strip=True)
    return text[:limit] or None


class _FetchMixin:
    def _get_json(self, url: str, *, method: str = "GET", body: Any = None,
                  headers: Optional[Dict[str, str]] = None) -> Any:
        self.calls += 1
        result = self.fetcher.fetch(url, method=method, json_body=body, headers=headers,
                                    accept="application/json")
        if result.blocked:
            self.errors += 1
            raise SourceError(f"{self.label} refused the request (HTTP {result.status or result.error}); "
                              "not retried around the block")
        if not result.ok:
            self.errors += 1
            raise SourceError(f"{self.label} request failed: {result.error or 'HTTP ' + str(result.status)}")
        try:
            return result.json()
        except ValueError as error:
            self.errors += 1
            raise SourceError(f"{self.label} returned a non-JSON body") from error


# ---------------------------------------------------------------------------
# ATS public job-board APIs
# ---------------------------------------------------------------------------


class ATSPublicAdapter(_FetchMixin, SourceAdapter):
    """The ATS vendors' own, documented, unauthenticated job-board APIs."""

    name = "ats_public"
    label = "ATS public job boards"
    kind = "ats"
    access_method = "public_api"
    requirement = "a careers/ATS board URL (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee…)"

    def health(self) -> Dict[str, Any]:
        return {"status": "ok", "detail": "public vendor APIs; no credentials needed"}

    def detect(self, board_url: str) -> Optional[Dict[str, Any]]:
        from cloud.intel.vendor import ats_detect

        return ats_detect.detect(board_url)

    def search(self, query: SourceQuery) -> List[Dict[str, Any]]:
        from cloud.intel.vendor import ats_detect

        if not query.board_url:
            raise SourceError("the ATS source needs a board_url")
        detected = self.detect(query.board_url)
        if not detected:
            raise SourceError(f"no supported ATS recognised at {query.board_url}")
        endpoints = ats_detect.api_endpoints(detected)
        if not endpoints:
            raise SourceError(f"{detected['platform']} has no public job-board API; use the careercrawler source")
        company = query.company or detected.get("token") or ""
        out: List[Dict[str, Any]] = []
        for method, url, body, extractor in endpoints:
            payload = self._get_json(url, method=method, body=body)
            for item in self._items(extractor, payload):
                out.append({"extractor": extractor, "item": item, "company": company, "platform": detected["platform"],
                            "host": detected.get("host"), "token": detected.get("token"), "board_url": query.board_url})
        keywords = [k for k in query.keywords.lower().split() if k]
        if keywords:
            out = [r for r in out if all(k in json.dumps(r["item"]).lower() for k in keywords)]
        return out[: query.limit]

    @staticmethod
    def _items(extractor: str, payload: Any) -> List[Mapping[str, Any]]:
        if extractor == "lever":
            return payload if isinstance(payload, list) else []
        if not isinstance(payload, Mapping):
            return []
        for key in {"greenhouse": "jobs", "ashby": "jobs", "smartrecruiters": "content", "workable": "jobs",
                    "recruitee": "offers", "workday": "jobPostings", "teamtailor": "data"}.get(extractor, "jobs"),:
            items = payload.get(key)
            if isinstance(items, list):
                return items
        return []

    def normalize(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        item, ex = raw["item"], raw["extractor"]
        base = {"company_name": raw.get("company") or "", "ats": raw.get("platform"), "source_kind": "external_source",
                "source_name": f"ats:{ex}"}
        if ex == "greenhouse":
            depts = item.get("departments") or []
            return {**base, "title": item.get("title"), "job_url": item.get("absolute_url"),
                    "external_id": str(item.get("id") or ""), "location": (item.get("location") or {}).get("name"),
                    "posted_at": _ts(item.get("first_published") or item.get("updated_at")),
                    "department": depts[0].get("name") if depts and isinstance(depts[0], Mapping) else None,
                    "description": _clean_html(item.get("content")),
                    "workplace_type": _workplace(text=(item.get("location") or {}).get("name", ""))}
        if ex == "lever":
            cats = item.get("categories") or {}
            return {**base, "title": item.get("text"), "job_url": item.get("hostedUrl"),
                    "external_id": str(item.get("id") or ""), "location": cats.get("location"),
                    "posted_at": _ts(item.get("createdAt")), "department": cats.get("department") or cats.get("team"),
                    "employment_type": cats.get("commitment"), "description": item.get("descriptionPlain"),
                    "workplace_type": _workplace(text=str(item.get("workplaceType") or cats.get("location") or ""))}
        if ex == "ashby":
            return {**base, "title": item.get("title"), "job_url": item.get("jobUrl"),
                    "external_id": str(item.get("id") or ""), "location": item.get("location"),
                    "posted_at": _ts(item.get("publishedAt")), "department": item.get("department") or item.get("team"),
                    "employment_type": item.get("employmentType"), "description": item.get("descriptionPlain"),
                    "workplace_type": _workplace(item.get("isRemote"), str(item.get("workplaceType") or ""))}
        if ex == "smartrecruiters":
            loc = item.get("location") or {}
            place = ", ".join(p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p)
            token = raw.get("token") or ""
            return {**base, "title": item.get("name"), "job_url": item.get("ref") and
                    f"https://jobs.smartrecruiters.com/{token}/{item.get('id')}",
                    "external_id": str(item.get("id") or ""), "location": place,
                    "posted_at": _ts(item.get("releasedDate")),
                    "department": (item.get("department") or {}).get("label"),
                    "employment_type": (item.get("typeOfEmployment") or {}).get("label"),
                    "workplace_type": _workplace(loc.get("remote"), place)}
        if ex == "workable":
            place = ", ".join(p for p in (item.get("city"), item.get("state"), item.get("country")) if p)
            return {**base, "title": item.get("title"), "job_url": item.get("url") or item.get("shortlink"),
                    "external_id": str(item.get("shortcode") or ""), "location": place,
                    "posted_at": _ts(item.get("published_on") or item.get("created_at")),
                    "department": item.get("department"), "employment_type": item.get("employment_type"),
                    "workplace_type": _workplace(item.get("telecommuting"), place)}
        if ex == "recruitee":
            return {**base, "title": item.get("title"), "job_url": item.get("careers_url"),
                    "external_id": str(item.get("id") or ""), "location": item.get("location"),
                    "posted_at": _ts(item.get("published_at")), "department": item.get("department"),
                    "employment_type": item.get("employment_type_code"),
                    "description": _clean_html(item.get("description")),
                    "workplace_type": _workplace(item.get("remote"), str(item.get("location") or ""))}
        if ex == "workday":
            path = item.get("externalPath") or ""
            token = str(raw.get("token") or "")
            board = token.split("/", 1)[1] if "/" in token else ""
            return {**base, "title": item.get("title"),
                    "job_url": f"https://{raw.get('host')}/{board}{path}" if path else None,
                    "location": item.get("locationsText"), "workplace_type": _workplace(text=item.get("locationsText") or "")}
        return {**base, "title": item.get("title") or item.get("name"),
                "job_url": item.get("url") or item.get("absolute_url") or item.get("jobUrl"),
                "location": item.get("location") if isinstance(item.get("location"), str) else None}


# ---------------------------------------------------------------------------
# CareerCrawler (the existing engine, run as a task)
# ---------------------------------------------------------------------------


class CareerCrawlerAdapter(SourceAdapter):
    """The production crawler's engine, run by the platform worker as a ``crawl`` task.

    It is not called inline: :class:`~cloud.intel.sources.service.SourceService`
    submits a task and Track C's crawl handler calls the engine through its
    sanctioned bridge. This adapter only describes the source.
    """

    name = "careercrawler"
    label = "CareerCrawler (company careers sites)"
    kind = "ats"
    access_method = "public_api"
    task_kind = "crawl"
    requirement = "company websites or careers URLs"

    def health(self) -> Dict[str, Any]:
        return {"status": "ok", "detail": "runs as a crawl task on the platform worker"}

    def search(self, query: SourceQuery) -> List[Dict[str, Any]]:
        raise SourceError("the CareerCrawler source runs as a background crawl task")

    def normalize(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        return dict(raw)


# ---------------------------------------------------------------------------
# Keyed official APIs
# ---------------------------------------------------------------------------


class USAJobsAdapter(_FetchMixin, SourceAdapter):
    """USAJOBS Search API (data.usajobs.gov). Official; needs a free API key and the
    email address it was registered with (sent as the ``User-Agent``)."""

    name = "usajobs"
    label = "USAJOBS"
    access_method = "official_api"
    requires = ("api_key", "email")
    requirement = "a USAJOBS developer API key and the email it was registered to (developer.usajobs.gov)"

    def search(self, query: SourceQuery) -> List[Dict[str, Any]]:
        self.require_configured()
        params = {"Keyword": query.keywords, "LocationName": query.location,
                  "ResultsPerPage": min(query.limit, 500), "Page": query.page}
        if query.posted_within_days:
            params["DatePosted"] = query.posted_within_days
        payload = self._get_json("https://data.usajobs.gov/api/search?" + urlencode({k: v for k, v in params.items() if v}),
                                 headers={"Authorization-Key": self.credentials["api_key"],
                                          "User-Agent": self.credentials["email"]})
        items = ((payload or {}).get("SearchResult") or {}).get("SearchResultItems") or []
        return [i.get("MatchedObjectDescriptor") or {} for i in items]

    def normalize(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        locations = raw.get("PositionLocation") or []
        return {"company_name": raw.get("OrganizationName") or raw.get("DepartmentName") or "",
                "title": raw.get("PositionTitle"), "job_url": raw.get("PositionURI"),
                "external_id": str(raw.get("PositionID") or ""),
                "location": raw.get("PositionLocationDisplay") or (locations[0].get("LocationName") if locations else None),
                "posted_at": _ts(raw.get("PublicationStartDate")), "country": "United States",
                "description": ((raw.get("UserArea") or {}).get("Details") or {}).get("JobSummary"),
                "source_kind": "external_source", "source_name": "usajobs", "workplace_type": "unknown"}


class AdzunaAdapter(_FetchMixin, SourceAdapter):
    """Adzuna Jobs API (developer.adzuna.com). Official; needs app_id + app_key."""

    name = "adzuna"
    label = "Adzuna"
    access_method = "official_api"
    requires = ("app_id", "app_key")
    requirement = "an Adzuna developer app_id and app_key (developer.adzuna.com)"

    def search(self, query: SourceQuery) -> List[Dict[str, Any]]:
        self.require_configured()
        country = str(self.settings.get("country") or "us")
        params = {"app_id": self.credentials["app_id"], "app_key": self.credentials["app_key"],
                  "what": query.keywords, "where": query.location, "results_per_page": min(query.limit, 50),
                  "content-type": "application/json"}
        if query.posted_within_days:
            params["max_days_old"] = query.posted_within_days
        payload = self._get_json(f"https://api.adzuna.com/v1/api/jobs/{country}/search/{max(1, query.page)}?"
                                 + urlencode({k: v for k, v in params.items() if v}))
        return list((payload or {}).get("results") or [])

    def normalize(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        return {"company_name": (raw.get("company") or {}).get("display_name") or "",
                "title": raw.get("title"), "job_url": raw.get("redirect_url"), "external_id": str(raw.get("id") or ""),
                "location": (raw.get("location") or {}).get("display_name"), "posted_at": _ts(raw.get("created")),
                "description": raw.get("description"), "employment_type": raw.get("contract_time"),
                "source_kind": "external_source", "source_name": "adzuna", "workplace_type": "unknown"}


class ZipRecruiterAdapter(_FetchMixin, SourceAdapter):
    """ZipRecruiter publisher Job Search API. Requires a publisher/partner API key
    issued by ZipRecruiter. The request shape follows the publisher API
    (``api.ziprecruiter.com/jobs/v1``); it has not been exercised with a real key,
    so a configured connection stays ``configured_unverified`` until verified."""

    name = "ziprecruiter"
    label = "ZipRecruiter"
    access_method = "partner"
    requires = ("api_key",)
    requirement = "a ZipRecruiter publisher/partner Job Search API key issued by ZipRecruiter"

    def search(self, query: SourceQuery) -> List[Dict[str, Any]]:
        self.require_configured()
        params = {"search": query.keywords, "location": query.location, "api_key": self.credentials["api_key"],
                  "jobs_per_page": min(query.limit, 100), "page": query.page}
        if query.posted_within_days:
            params["days_ago"] = query.posted_within_days
        payload = self._get_json("https://api.ziprecruiter.com/jobs/v1?" + urlencode({k: v for k, v in params.items() if v}))
        return list((payload or {}).get("jobs") or [])

    def normalize(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        return {"company_name": (raw.get("hiring_company") or {}).get("name") or raw.get("source") or "",
                "title": raw.get("name"), "job_url": raw.get("url"), "external_id": str(raw.get("id") or ""),
                "location": raw.get("location"), "posted_at": _ts(raw.get("posted_time")),
                "description": raw.get("snippet"), "source_kind": "external_source", "source_name": "ziprecruiter",
                "workplace_type": "unknown"}


class _PartnerOnlyAdapter(SourceAdapter):
    """A source whose job data is available only under a partner agreement.

    Storing a partner token marks it ``configured_unverified``; search still
    refuses until the partner agreement's documented endpoint is implemented,
    because there is no public API to call and scraping is not an option.
    """

    access_method = "partner"
    requires = ("partner_api_token",)

    def search(self, query: SourceQuery) -> List[Dict[str, Any]]:
        self.require_configured()
        raise SourceUnavailable(f"{self.label}: the partner endpoint is defined by your partner agreement and is "
                                "not implemented yet; supply its documentation to enable it")

    def normalize(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        return dict(raw)


class LinkedInAdapter(_PartnerOnlyAdapter):
    name = "linkedin"
    label = "LinkedIn Jobs"
    requirement = ("LinkedIn partner API access (LinkedIn Talent Solutions / Job Posting API partner program); "
                   "LinkedIn offers no public job-search API and guest-endpoint scraping is not permitted")


class IndeedAdapter(_PartnerOnlyAdapter):
    name = "indeed"
    label = "Indeed"
    requirement = ("Indeed partner API access (Indeed's public Job Search/Publisher API is closed to new users); "
                   "Indeed's site is protected by bot controls that this platform will not bypass")


class _LicensedFeedAdapter(_FetchMixin, SourceAdapter):
    """A source reachable only through a licensed/authorised data feed.

    Configure ``feed_url`` (a JSON feed the provider supplies under contract) and,
    if the provider issued one, ``feed_token`` (sent as a Bearer token). The feed
    is expected to be a JSON list of jobs, or an object with a ``jobs`` list.
    """

    access_method = "authorized_account"
    requires = ("feed_url",)

    def search(self, query: SourceQuery) -> List[Dict[str, Any]]:
        self.require_configured()
        headers = {"Authorization": f"Bearer {self.credentials['feed_token']}"} if self.credentials.get("feed_token") else None
        payload = self._get_json(self.credentials["feed_url"], headers=headers)
        items = payload if isinstance(payload, list) else (payload or {}).get("jobs") or []
        keywords = [k for k in query.keywords.lower().split() if k]
        if keywords:
            items = [i for i in items if all(k in json.dumps(i).lower() for k in keywords)]
        return list(items)[: query.limit]

    def normalize(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        return {"company_name": raw.get("company") or raw.get("company_name") or "",
                "title": raw.get("title"), "job_url": raw.get("url") or raw.get("job_url"),
                "external_id": str(raw.get("id") or ""), "location": raw.get("location"),
                "posted_at": _ts(raw.get("posted_at") or raw.get("date_posted")),
                "description": raw.get("description"), "source_kind": "external_source", "source_name": self.name,
                "workplace_type": _workplace(raw.get("remote"), str(raw.get("location") or ""))}


class DiceAdapter(_LicensedFeedAdapter):
    name = "dice"
    label = "Dice"
    requirement = ("a licensed Dice data feed or written authorisation from Dice; Dice has no public job API and "
                   "its site terms do not permit scraping")


class WellfoundAdapter(_LicensedFeedAdapter):
    name = "wellfound"
    label = "Wellfound"
    requirement = "a licensed Wellfound (AngelList Talent) data feed or partner agreement; no public job API"


class BuiltInAdapter(_LicensedFeedAdapter):
    name = "builtin"
    label = "Built In"
    requirement = "a licensed Built In data feed or partner agreement; no public job API"


ADAPTERS: Dict[str, type] = {cls.name: cls for cls in (
    ATSPublicAdapter, CareerCrawlerAdapter, USAJobsAdapter, AdzunaAdapter, ZipRecruiterAdapter, LinkedInAdapter,
    IndeedAdapter, DiceAdapter, WellfoundAdapter, BuiltInAdapter)}


def adapter_class(name: str) -> type:
    try:
        return ADAPTERS[name]
    except KeyError:
        raise SourceError(f"unknown source {name!r}") from None
