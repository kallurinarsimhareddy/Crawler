"""Reduce a URL to the identity of the thing it points at.

Week-over-week comparison lives or dies on this. The same posting is served
under many spellings — the board links it one way, an email campaign appends
``?utm_source=newsletter``, an aggregator appends ``?src=indeed``, and the host
answers to both ``acme.com`` and ``www.acme.com``. Compared literally, next
Friday's crawl reports every one of them as a brand new job::

    >>> from utils.urlkey import url_key
    >>> url_key("https://WWW.Acme.com/jobs/42/?utm_source=x&gh_src=y#apply")
    'https://acme.com/jobs/42'
    >>> url_key("https://acme.com/jobs/42")
    'https://acme.com/jobs/42'

Two decisions here are deliberately conservative, because collapsing two
distinct postings into one loses a job, which is worse than reporting one job
twice.

**Query parameters are removed by denylist, never by allowlist.** Only
parameters known to be campaign or attribution tracking are dropped. Everything
else is kept and sorted, because ATS vendors routinely put the posting's own
identity in the query string: ``?gh_jid=8130725`` *is* the Greenhouse job,
``?jobId=...&cid=...`` *is* the ADP requisition. An allowlist would delete them.

**Fragments are removed only when they are anchors.** A fragment that looks like
routing — ``#/jobdetail?jobId=123``, as SAP SuccessFactors and several
single-page portals emit — carries the posting's identity, and dropping it would
map an entire board onto one key. A plain ``#apply`` anchor is discarded.
"""

from __future__ import annotations

import re
from typing import Final, FrozenSet, List, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = [
    "TRACKING_PARAMETERS",
    "TRACKING_PREFIXES",
    "canonical_url",
    "is_tracking_parameter",
    "url_key",
]

#: Query parameters that identify *how a visitor arrived* rather than *what
#: they are looking at*. Dropping these is what stops a campaign link from
#: registering as a new posting every week.
TRACKING_PARAMETERS: Final[FrozenSet[str]] = frozenset(
    {
        # Google Analytics / Urchin, and the ad-click ids that travel with it.
        "gclid",
        "gclsrc",
        "dclid",
        "wbraid",
        "gbraid",
        "gad_source",
        "_ga",
        "_gl",
        # Other advertising networks.
        "fbclid",
        "msclkid",
        "twclid",
        "ttclid",
        "yclid",
        "igshid",
        "epik",
        "s_kwcid",
        "ef_id",
        # Email service providers.
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "hsctatracking",
        "vero_id",
        "oly_anon_id",
        "oly_enc_id",
        # Applicant tracking systems' own source attribution. These name the
        # job board that referred the candidate; the posting is the same one.
        "gh_src",  # Greenhouse. NB: gh_jid is the job id and is kept.
        "lever-source",
        "lever-origin",
        "lever-via",
        "iis",  # iCIMS: referring source, e.g. "Job Board"
        "iisn",  # iCIMS: referring source name, e.g. "Indeed"
        "jobpipeline",
        "codes",
        "sourceid",
        "source_id",
        "src_trk",
        "srcid",
        # Generic attribution. Ambiguous in the abstract, but on a job posting
        # these are attribution in every case observed on the reference sheet.
        "source",
        "src",
        "ref",
        "referer",
        "referrer",
        "trk",
        "trkcampaign",
        "utm_referrer",
    }
)

#: Prefixes whose every parameter is tracking. ``utm_*`` has a long tail
#: (``utm_id``, ``utm_content``, ``utm_reader``, vendor-specific extensions)
#: that is not worth enumerating.
TRACKING_PREFIXES: Final[Tuple[str, ...]] = ("utm_", "pk_", "piwik_", "matomo_", "rx_", "_hs")

#: Schemes worth keying at all. Anything else is returned untouched, because a
#: ``mailto:`` or ``javascript:`` link is not a posting and normalising it would
#: only invent a false identity for it.
_KEYABLE_SCHEMES: Final[FrozenSet[str]] = frozenset({"http", "https"})

#: Default ports, which carry no meaning and must not distinguish two URLs.
_DEFAULT_PORTS: Final[dict] = {"http": 80, "https": 443}

#: A fragment carrying routing rather than an anchor position. Single-page
#: portals put the posting's identity here, so these must survive.
_ROUTING_FRAGMENT: Final[re.Pattern[str]] = re.compile(r"^[!/]|[=?&]")

#: Repeated slashes in a path, which servers collapse and comparisons do not.
_REPEATED_SLASHES: Final[re.Pattern[str]] = re.compile(r"/{2,}")


def is_tracking_parameter(name: str) -> bool:
    """Whether a query parameter describes the referral rather than the target.

    Args:
        name: The parameter name, in any case.

    Returns:
        ``True`` if the parameter should be dropped when keying a URL.
    """
    lowered = name.strip().lower()
    if lowered in TRACKING_PARAMETERS:
        return True
    return any(lowered.startswith(prefix) for prefix in TRACKING_PREFIXES)


def _clean_query(query: str) -> str:
    """Drop tracking parameters and put the rest in a stable order.

    Sorting matters as much as filtering: a board that emits ``?a=1&b=2`` on one
    page and ``?b=2&a=1`` on another is describing one posting.

    Args:
        query: The raw query string, without its leading ``?``.

    Returns:
        The normalised query string, possibly empty.
    """
    if not query:
        return ""

    # keep_blank_values so ``?q=`` — which UltiPro boards emit — round-trips
    # rather than silently changing the URL's meaning.
    pairs: List[Tuple[str, str]] = [
        (name, value)
        for name, value in parse_qsl(query, keep_blank_values=True)
        if not is_tracking_parameter(name)
    ]

    if not pairs:
        return ""

    return urlencode(sorted(pairs), doseq=True)


def _clean_fragment(fragment: str) -> str:
    """Keep a fragment only when it routes to a posting.

    Args:
        fragment: The raw fragment, without its leading ``#``.

    Returns:
        The fragment to keep, or ``""`` for a plain anchor.
    """
    candidate = fragment.strip()
    if not candidate:
        return ""
    return candidate if _ROUTING_FRAGMENT.search(candidate) else ""


def _clean_host(host: str, scheme: str, port: str) -> str:
    """Normalise the network location.

    Args:
        host: Hostname as parsed, in any case.
        scheme: URL scheme, lowercased, used to recognise a default port.
        port: Port as parsed, or ``""``.

    Returns:
        The lowercased host, without ``www.`` and without a default port.
    """
    cleaned = host.strip().lower().rstrip(".")

    # ``www`` is a convention, not a distinct service: every board that answers
    # on one answers on the other, and the sheet uses both spellings.
    if cleaned.startswith("www."):
        cleaned = cleaned[4:]

    if port and _DEFAULT_PORTS.get(scheme) != int(port):
        return f"{cleaned}:{port}"

    return cleaned


def _clean_path(path: str) -> str:
    """Normalise the path, without changing its case.

    Paths are case-sensitive by specification, and several ATS tenants really do
    serve different postings from paths differing only in case. Only structural
    noise is removed.

    Args:
        path: The raw path.

    Returns:
        The normalised path.
    """
    collapsed = _REPEATED_SLASHES.sub("/", path)

    # One trailing slash is presentational; the root's slash is structural.
    if len(collapsed) > 1 and collapsed.endswith("/"):
        collapsed = collapsed.rstrip("/") or "/"

    return collapsed


def canonical_url(url: str) -> str:
    """Rewrite a URL into the one canonical spelling of what it points at.

    Args:
        url: Any URL, absolute or otherwise.

    Returns:
        The canonical form. Input that is blank, relative, or not ``http(s)``
        is returned stripped and otherwise unchanged, because there is nothing
        here that can safely be normalised about it.
    """
    candidate = (url or "").strip()
    if not candidate:
        return ""

    try:
        parts = urlsplit(candidate)
    except ValueError:
        # A malformed URL — an invalid IPv6 literal, say. Nothing to normalise,
        # and inventing a key for it would be worse than keeping it verbatim.
        return candidate

    scheme = parts.scheme.lower()
    if scheme not in _KEYABLE_SCHEMES or not parts.hostname:
        return candidate

    try:
        port = str(parts.port) if parts.port else ""
    except ValueError:
        port = ""

    return urlunsplit(
        (
            scheme,
            _clean_host(parts.hostname, scheme, port),
            _clean_path(parts.path),
            _clean_query(parts.query),
            _clean_fragment(parts.fragment),
        )
    )


def url_key(url: str) -> str:
    """Produce the comparison key for a URL.

    This is what week-over-week diffing compares, and what job identity falls
    back to when a platform exposes no requisition id of its own.

    Args:
        url: Any URL.

    Returns:
        The canonical URL, or ``""`` when the input was blank.
    """
    return canonical_url(url)
