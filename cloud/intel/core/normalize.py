"""Normalisation shared by every track: domains, names, emails, titles, locations.

Built on the vendored CareerCrawler/discovery primitives so the platform and the
crawler agree on what "the same domain" and "the same company name" mean.
Nothing here guesses meaning: a value that cannot be normalised is returned as
``None`` and the original is kept by the caller in ``source_records``.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from cloud.intel.vendor import identity as _identity
from cloud.intel.vendor.names import company_slug, normalise_name, registrable_domain

__all__ = [
    "FREE_EMAIL_DOMAINS",
    "canonical_linkedin",
    "company_name_key",
    "domain_of",
    "email_domain",
    "normalize_country",
    "normalize_email",
    "normalize_name",
    "normalize_website",
    "split_full_name",
]

FREE_EMAIL_DOMAINS = _identity.FREE_EMAIL_DOMAINS
canonical_linkedin = _identity.canonical_linkedin
email_domain = _identity.email_domain

_EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
_PLACEHOLDERS = frozenset({"", "n/a", "na", "none", "null", "-", "--", "unknown", "tbd", "0", "#n/a"})


def blank(value: object) -> bool:
    return value is None or str(value).strip().lower() in _PLACEHOLDERS


def normalize_website(value: object) -> Optional[str]:
    """``acme.com/about`` -> ``https://acme.com/about``; placeholders -> None."""
    if blank(value):
        return None
    text = str(value).strip()
    if " " in text or "." not in text:
        return None
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return text


def domain_of(value: object) -> Optional[str]:
    """The registrable domain of a website, URL, bare host or email address."""
    if blank(value):
        return None
    text = str(value).strip().lower()
    if "@" in text and "://" not in text:
        text = text.rpartition("@")[2]
    domain = registrable_domain(text)
    return domain or None


def normalize_name(value: object) -> Optional[str]:
    """Lower-cased, accent-folded, punctuation-collapsed name, legal form KEPT."""
    if blank(value):
        return None
    full, _slug = _identity.normalised_name(str(value))
    return full or None


def company_name_key(value: object) -> Optional[str]:
    """Legal-form-stripped slug. A similarity key for candidate generation only —
    never proof of identity (``Acme Inc`` and ``Acme LLC`` share it)."""
    if blank(value):
        return None
    return company_slug(str(value)) or None


def normalize_email(value: object) -> Optional[str]:
    if blank(value):
        return None
    text = str(value).strip().strip("<>;,").lower()
    if text.startswith("mailto:"):
        text = text[7:]
    return text if _EMAIL.match(text) and len(text) <= 320 else None


_COUNTRY = {
    "us": "United States", "usa": "United States", "u.s.": "United States", "u.s.a.": "United States",
    "united states": "United States", "united states of america": "United States", "america": "United States",
    "uk": "United Kingdom", "u.k.": "United Kingdom", "gb": "United Kingdom", "united kingdom": "United Kingdom",
    "great britain": "United Kingdom", "england": "United Kingdom", "ca": "Canada", "canada": "Canada",
    "in": "India", "india": "India", "de": "Germany", "germany": "Germany", "mx": "Mexico", "mexico": "Mexico",
    "au": "Australia", "australia": "Australia", "fr": "France", "france": "France",
}


def normalize_country(value: object) -> Optional[str]:
    if blank(value):
        return None
    text = str(value).strip()
    return _COUNTRY.get(text.lower(), text)


def split_full_name(full_name: str) -> Tuple[str, str]:
    parts = [p for p in re.split(r"\s+", (full_name or "").strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def unique(values: Iterable[Optional[str]]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out
