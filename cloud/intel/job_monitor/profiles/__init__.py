"""Deterministic site profiles, picked by host.

A profile knows one job site's listing markup: which element is a job card, how
each of the 14 fields is read from it, how the next page is reached, and the
stable identity of a job URL. Adding a site means adding a profile here — the
monitor, change detection, notifications and UI stay the same.
"""

from __future__ import annotations

from typing import Dict, Optional, Type
from urllib.parse import urlsplit

from cloud.intel.job_monitor.profiles.base import ListingPage, SiteProfile
from cloud.intel.job_monitor.profiles.wearedevelopers import WeAreDevelopersProfile

__all__ = ["PROFILES", "ListingPage", "SiteProfile", "get_profile", "profile_for_url"]

PROFILES: Dict[str, Type[SiteProfile]] = {
    WeAreDevelopersProfile.name: WeAreDevelopersProfile,
}


def get_profile(name: Optional[str]) -> Optional[SiteProfile]:
    cls = PROFILES.get(name or "")
    return cls() if cls else None


def profile_for_url(url: str) -> Optional[SiteProfile]:
    host = (urlsplit(url or "").hostname or "").lower()
    for cls in PROFILES.values():
        if any(host == h or host.endswith("." + h) for h in cls.hosts):
            return cls()
    return None
