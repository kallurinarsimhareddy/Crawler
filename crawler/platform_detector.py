"""Identify which applicant tracking system (ATS) hosts a careers page.

Every ATS exposes its jobs differently, so the crawler must know which platform
it is looking at before it can extract anything. The detected platform is also
written to the ``Platform`` column of ``output/jobs.xlsx``.

Detection here is **URL-only** — nothing is fetched. Two signals are used, in
this order:

1. **Hostname** — the strongest signal, because most tenants are handed a
   subdomain on the vendor's own domain (``boards.greenhouse.io``,
   ``jobs.lever.co``, ``<tenant>.icims.com``, ...).
2. **Path and query** — the fallback for boards served from the company's own
   domain, where only the vendor's URL layout gives it away
   (``/careersection/`` for Taleo, ``/wday/`` for Workday, ...).

Usage::

    >>> from crawler.platform_detector import detect_platform
    >>> detect_platform("https://boards.greenhouse.io/acme")
    <Platform.GREENHOUSE: 'Greenhouse'>
    >>> detect_platform("https://acme.com/about/careers").value
    'Generic HTML'

A valid ``http(s)`` URL that matches no vendor is :attr:`Platform.GENERIC_HTML`
— the generic adapter can still try it. :attr:`Platform.UNKNOWN` means the input
was not a usable URL at all, so there is nothing to crawl.

Every function is pure: same URL in, same platform out, no I/O and no shared
state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from loguru import logger

__all__ = [
    "Platform",
    "detect_platform",
    "match_host",
    "match_path",
    "normalise_url",
    "split_url",
]


class Platform(str, Enum):
    """A supported applicant tracking system.

    The value is the human-readable label written to the ``Platform`` column,
    so the enum can be used directly wherever a string is expected.
    """

    WORKDAY = "Workday"
    GREENHOUSE = "Greenhouse"
    LEVER = "Lever"
    ASHBY = "Ashby"
    ICIMS = "iCIMS"
    ULTIPRO = "UltiPro"
    SMARTRECRUITERS = "SmartRecruiters"
    SUCCESSFACTORS = "SAP SuccessFactors"
    ORACLE = "Oracle"
    TALEO = "Taleo"
    JOBVITE = "Jobvite"
    TEAMTAILOR = "Teamtailor"
    BAMBOOHR = "BambooHR"
    RECRUITEE = "Recruitee"
    WORKABLE = "Workable"
    DAYFORCE = "Dayforce"
    UKG = "UKG"
    ADP = "ADP"
    CORNERSTONE = "Cornerstone"
    EIGHTFOLD = "Eightfold"
    PHENOM = "Phenom"

    # --- Added in version 2 -------------------------------------------------
    # Payroll suites with a recruiting module. Between them these account for
    # more of a typical US mid-market sheet than any single dedicated ATS.
    PAYLOCITY = "Paylocity"
    PAYCOM = "Paycom"
    PAYCOR = "Paycor"
    UKG_READY = "UKG Ready"
    ISOLVED = "isolved"
    ASURE = "Asure"

    # Dedicated applicant tracking systems.
    PEOPLEADMIN = "PeopleAdmin"
    JAZZHR = "JazzHR"
    RIPPLING = "Rippling"
    PERSONIO = "Personio"
    AVATURE = "Avature"
    BULLHORN = "Bullhorn"
    BREEZYHR = "BreezyHR"
    PINPOINT = "Pinpoint"
    COMEET = "Comeet"
    FOUNTAIN = "Fountain"
    NEOGOV = "NeoGov"
    OLEEO = "Oleeo"
    JOBSCORE = "JobScore"
    GOHIRE = "GoHire"
    HOMERUN = "Homerun"
    JOIN = "Join.com"
    ZOHO_RECRUIT = "Zoho Recruit"
    MANATAL = "Manatal"
    GEM = "Gem"
    TALENTREEF = "TalentReef"
    APPLICANTPRO = "ApplicantPro"
    APPLICANTSTACK = "ApplicantStack"
    CLEARCOMPANY = "ClearCompany"
    CAREERPLUG = "CareerPlug"
    HIREOLOGY = "Hireology"
    HRMDIRECT = "HRMDirect"
    SILKROAD = "SilkRoad"
    RECRUITERBOX = "Trakstar Hire"
    RADANCY = "Radancy"
    ADP_RM = "ADP Recruiting Management"
    INDEED = "Indeed"

    #: A reachable page with no recognised vendor behind it.
    GENERIC_HTML = "Generic HTML"

    #: The input was not a usable ``http(s)`` URL.
    UNKNOWN = "Unknown"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class _Rule:
    """One platform's URL fingerprints.

    Attributes:
        platform: The platform these fingerprints identify.
        hosts: Registrable domains owned by the vendor. A URL matches when its
            hostname equals one of these or is a subdomain of it, so
            ``boards.greenhouse.io`` matches ``greenhouse.io`` while
            ``notgreenhouse.io`` does not.
        paths: Substrings of the lowercased ``path?query`` that only this
            vendor produces. Used when the board is served from the company's
            own domain, where the hostname reveals nothing.
    """

    platform: Platform
    hosts: Tuple[str, ...] = ()
    paths: Tuple[str, ...] = ()


#: Fingerprints in evaluation order. All hostnames are tried before any path, so
#: a vendor-owned domain always wins over a path heuristic. Within each pass the
#: first match wins, so more specific rules are listed first (UltiPro before UKG,
#: Taleo before Oracle — both pairs share a corporate parent but not a URL shape).
_RULES: Final[Tuple[_Rule, ...]] = (
    _Rule(
        Platform.WORKDAY,
        hosts=("myworkdayjobs.com", "myworkdaysite.com", "myworkday.com", "workday.com"),
        paths=("/wday/",),
    ),
    _Rule(
        Platform.GREENHOUSE,
        hosts=("greenhouse.io", "grnh.se"),
        paths=("/embed/job_board", "/embed/job_app"),
    ),
    _Rule(Platform.LEVER, hosts=("lever.co",)),
    _Rule(Platform.ASHBY, hosts=("ashbyhq.com",), paths=("/ashby_embed",)),
    _Rule(Platform.ICIMS, hosts=("icims.com",), paths=("/icims/",)),
    # UltiPro before UKG: UKG owns both, but only the ultipro.com estate serves
    # the /JobBoard/ recruiting app the ultipro adapter targets.
    _Rule(Platform.ULTIPRO, hosts=("ultipro.com", "ultipro.ca"), paths=("/jobboard/",)),
    _Rule(Platform.UKG, hosts=("ukg.com", "ukgpro.com", "ukgpro.ca", "ukg.net")),
    _Rule(Platform.SMARTRECRUITERS, hosts=("smartrecruiters.com",)),
    _Rule(
        Platform.SUCCESSFACTORS,
        hosts=("successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu", "jobs.sap.com"),
        paths=("/sfcareer/", "/career?company=", "/careers?company="),
    ),
    # Taleo before Oracle: Oracle acquired Taleo, but taleo.net boards use a
    # completely different URL layout from Oracle Cloud Recruiting.
    _Rule(Platform.TALEO, hosts=("taleo.net", "taleo.com"), paths=("/careersection/",)),
    _Rule(
        Platform.ORACLE,
        hosts=("oraclecloud.com", "oracle.com"),
        paths=("/hcmui/candidateexperience", "/recruitingce"),
    ),
    _Rule(Platform.JOBVITE, hosts=("jobvite.com", "jobvite.co")),
    _Rule(Platform.TEAMTAILOR, hosts=("teamtailor.com",)),
    _Rule(Platform.BAMBOOHR, hosts=("bamboohr.com", "bamboohr.co.uk"), paths=("/jobs/embed2.php",)),
    _Rule(Platform.RECRUITEE, hosts=("recruitee.com",)),
    _Rule(Platform.WORKABLE, hosts=("workable.com",)),
    _Rule(
        Platform.DAYFORCE,
        hosts=("dayforcehcm.com", "dayforce.com", "ceridian.com"),
        paths=("/candidateportal",),
    ),
    # ADP Recruiting Management before ADP: both are adp.com, but myjobs.adp.com
    # is a different product from the WorkforceNow board and needs its own
    # adapter, so the more specific host has to be tested first.
    _Rule(Platform.ADP_RM, hosts=("myjobs.adp.com",)),
    _Rule(
        Platform.ADP,
        hosts=("adp.com",),
        paths=("/mascsr/default/mdf/recruitment/",),
    ),
    _Rule(
        Platform.CORNERSTONE,
        hosts=("csod.com", "cornerstoneondemand.com"),
        paths=("/ux/ats/careersite",),
    ),
    _Rule(Platform.EIGHTFOLD, hosts=("eightfold.ai",), paths=("/careers?pid=",)),
    _Rule(Platform.PHENOM, hosts=("phenompeople.com", "phenomapps.com", "phenom.com")),
    # --- Added in version 2 -------------------------------------------------
    # No path rule: Paylocity's own "/recruiting/jobs/" layout is too plain to
    # claim from a company-hosted URL, and the host is unambiguous anyway.
    _Rule(Platform.PAYLOCITY, hosts=("paylocity.com",)),
    _Rule(
        Platform.PAYCOM,
        hosts=("paycomonline.net", "paycomonline.com"),
        paths=("/v4/ats/",),
    ),
    _Rule(
        Platform.PAYCOR,
        hosts=("recruitingbypaycor.com", "paycor.com", "newtonsoftware.com"),
        paths=("/career/careerhome.action", "/career/jobintroduction.action"),
    ),
    #: UKG Ready is a different product from the UKG Pro rule above, and lives
    #: on a different registrable domain, so the two never compete.
    _Rule(Platform.UKG_READY, hosts=("saashr.com",)),
    _Rule(Platform.ISOLVED, hosts=("isolvedhire.com", "myisolved.com", "isolvedhcm.com")),
    _Rule(Platform.ASURE, hosts=("entertimeonline.com", "asuresoftware.com")),
    _Rule(Platform.JAZZHR, hosts=("applytojob.com", "jazzhr.com", "jazz.co")),
    _Rule(Platform.RIPPLING, hosts=("rippling.com", "rippling-ats.com")),
    _Rule(Platform.PERSONIO, hosts=("personio.de", "personio.com")),
    _Rule(Platform.AVATURE, hosts=("avature.net",), paths=("/careers/searchjobs",)),
    _Rule(
        Platform.BULLHORN,
        hosts=("bullhornstaffing.com", "bullhorn.com", "bullhorncareerportal.com"),
    ),
    _Rule(Platform.BREEZYHR, hosts=("breezy.hr",)),
    _Rule(Platform.PINPOINT, hosts=("pinpointhq.com",)),
    _Rule(Platform.COMEET, hosts=("comeet.co", "comeet.com"), paths=("/careers-api/",)),
    _Rule(Platform.FOUNTAIN, hosts=("fountain.com",)),
    _Rule(Platform.NEOGOV, hosts=("governmentjobs.com", "neogov.com", "schooljobs.com")),
    _Rule(Platform.OLEEO, hosts=("oleeo.com", "tal.net")),
    _Rule(Platform.JOBSCORE, hosts=("jobscore.com",)),
    _Rule(Platform.GOHIRE, hosts=("gohire.io",)),
    _Rule(Platform.HOMERUN, hosts=("homerun.co",)),
    _Rule(Platform.JOIN, hosts=("join.com",)),
    _Rule(
        Platform.ZOHO_RECRUIT,
        hosts=("zohorecruit.com", "zohorecruit.eu", "zohorecruit.in", "zohorecruit.com.au"),
        paths=("/recruit/portal/",),
    ),
    _Rule(Platform.MANATAL, hosts=("manatal.com",)),
    _Rule(Platform.GEM, hosts=("jobs.gem.com",)),
    _Rule(Platform.TALENTREEF, hosts=("talentreef.com", "jobappnetwork.com")),
    _Rule(Platform.APPLICANTPRO, hosts=("applicantpro.com", "applicantpool.com")),
    _Rule(Platform.APPLICANTSTACK, hosts=("applicantstack.com",)),
    _Rule(Platform.CLEARCOMPANY, hosts=("clearcompany.com",)),
    _Rule(Platform.CAREERPLUG, hosts=("careerplug.com",)),
    _Rule(Platform.HIREOLOGY, hosts=("hireology.com",)),
    _Rule(Platform.HRMDIRECT, hosts=("hrmdirect.com",)),
    _Rule(Platform.SILKROAD, hosts=("silkroad.com", "silkroad-eng.com")),
    _Rule(Platform.RECRUITERBOX, hosts=("recruiterbox.com", "trakstar.com", "trakstarhire.com")),
    _Rule(Platform.RADANCY, hosts=("radancy.com", "talentbrew.com", "tmpwebeng.com")),
    _Rule(Platform.INDEED, hosts=("indeed.com", "indeed.jobs")),
    # PeopleAdmin last, and the only rule here whose *path* does the real work.
    # Institutions front it with their own domain -- jobs.montana.edu,
    # employment.plu.edu -- so the hostname says nothing about the vendor and
    # the search view is the only thing in the URL that does. Every other rule
    # is evaluated before it, and hosts are tried before any path at all, so a
    # vendor with its own domain can never be claimed by this.
    #
    # The trade is deliberate and the same one Taleo's "/careersection/" rule
    # makes: an unrelated site serving "/postings/search" is classified
    # PeopleAdmin. Detection never fetches, so the URL is all the evidence
    # there is. adapters.peopleadmin checks the markup and fails with a clear
    # message rather than inventing postings, which is where that is caught.
    _Rule(Platform.PEOPLEADMIN, hosts=("peopleadmin.com",), paths=("/postings/search",)),
)

#: Schemes worth crawling. Anything else (mailto:, javascript:, ftp:) is not a page.
_CRAWLABLE_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

#: Two or more dot-separated labels, none containing URL punctuation or spaces.
#: Input sheets carry filler in the career URL column ("N/A", "TBD", "none
#: found"); prepending a scheme to those yields a syntactically valid URL with a
#: nonsense host, which would otherwise be reported as a crawlable generic page.
_VALID_HOST: Final[re.Pattern[str]] = re.compile(r"^[^\s./\\?#@:]+(\.[^\s./\\?#@:]+)+$")


def normalise_url(url: str) -> str:
    """Tidy a raw URL cell into something :func:`urllib.parse.urlsplit` can read.

    Input sheets carry URLs in whatever shape a human pasted them, most often
    scheme-less (``www.acme.com/careers``). Without a scheme ``urlsplit`` reads
    the whole string as a path and the hostname is lost, so one is added.

    Args:
        url: Raw URL, possibly padded, quoted or scheme-less.

    Returns:
        The cleaned URL, or ``""`` if there was nothing usable in ``url``.
    """
    if not url:
        return ""

    cleaned = str(url).strip().strip('"').strip("'")
    if not cleaned:
        return ""

    # "//host/path" is protocol-relative; "host/path" has no scheme at all.
    if cleaned.startswith("//"):
        return f"https:{cleaned}"
    if "://" not in cleaned:
        # A scheme-less string is only a URL if it has no other scheme's marker.
        if ":" in cleaned.split("/", 1)[0]:
            return cleaned
        return f"https://{cleaned}"

    return cleaned


def split_url(url: str) -> Optional[Tuple[str, str]]:
    """Split a URL into the two pieces detection cares about.

    Args:
        url: Raw or normalised URL.

    Returns:
        ``(hostname, "path?query")`` — both lowercased, with any ``www.``
        prefix, port and trailing dot stripped from the hostname — or ``None``
        if ``url`` is not a crawlable ``http(s)`` URL with a plausible hostname.
    """
    normalised = normalise_url(url)
    if not normalised:
        return None

    try:
        parts = urlsplit(normalised)
    except ValueError as exc:
        logger.debug("Unparsable URL {!r}: {}", url, exc)
        return None

    if parts.scheme.lower() not in _CRAWLABLE_SCHEMES:
        logger.debug("Ignoring non-crawlable scheme {!r} in {!r}", parts.scheme, url)
        return None

    try:
        hostname = parts.hostname or ""
    except ValueError as exc:  # malformed IPv6 literal / bad port
        logger.debug("Unparsable host in {!r}: {}", url, exc)
        return None

    host = hostname.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not _VALID_HOST.match(host):
        logger.debug("Not a hostname: {!r} (from {!r})", host, url)
        return None

    path = parts.path.lower()
    if parts.query:
        path = f"{path}?{parts.query.lower()}"

    return host, path


def match_host(host: str, domains: Sequence[str]) -> bool:
    """Report whether ``host`` is one of ``domains`` or a subdomain of one.

    Matching is on label boundaries, so ``greenhouse.io`` matches
    ``boards.greenhouse.io`` but never ``notgreenhouse.io``.

    Args:
        host: Lowercased hostname, as returned by :func:`split_url`.
        domains: Registrable domains to test against.

    Returns:
        ``True`` on a match.
    """
    if not host:
        return False

    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def match_path(path: str, patterns: Sequence[str]) -> bool:
    """Report whether ``path`` contains any of ``patterns``.

    Args:
        path: Lowercased ``"path?query"``, as returned by :func:`split_url`.
        patterns: Vendor-specific substrings to look for.

    Returns:
        ``True`` on a match.
    """
    if not path:
        return False

    return any(pattern in path for pattern in patterns)


def detect_by_host(host: str) -> Optional[Platform]:
    """Identify the platform from the hostname alone.

    Args:
        host: Lowercased hostname, as returned by :func:`split_url`.

    Returns:
        The matching platform, or ``None`` if the host belongs to no known
        vendor (the usual case for a company-hosted board).
    """
    for rule in _RULES:
        if match_host(host, rule.hosts):
            return rule.platform

    return None


def detect_by_path(path: str) -> Optional[Platform]:
    """Identify the platform from the URL path and query alone.

    This is what catches boards served from a company's own domain, where the
    hostname gives nothing away but the vendor's URL layout still shows through.

    Args:
        path: Lowercased ``"path?query"``, as returned by :func:`split_url`.

    Returns:
        The matching platform, or ``None`` if no vendor pattern is present.
    """
    for rule in _RULES:
        if match_path(path, rule.paths):
            return rule.platform

    return None


def detect_platform(url: str) -> Platform:
    """Identify which ATS serves ``url``.

    Hostname is tried first, then path and query. Nothing is fetched, so this is
    cheap enough to call on every row of the input sheet.

    Args:
        url: A careers page or job board URL, in any shape the input sheet
            might hold it (scheme-less and padded values are handled).

    Returns:
        The detected :class:`Platform`. :attr:`Platform.GENERIC_HTML` when the
        URL is crawlable but matches no vendor; :attr:`Platform.UNKNOWN` when
        the input is empty or is not an ``http(s)`` URL with a hostname.
    """
    parts = split_url(url)
    if parts is None:
        logger.debug("No platform for {!r}: not a crawlable URL", url)
        return Platform.UNKNOWN

    host, path = parts

    platform = detect_by_host(host)
    if platform is not None:
        logger.debug("Detected {} from host {!r} ({})", platform.value, host, url)
        return platform

    platform = detect_by_path(path)
    if platform is not None:
        logger.debug("Detected {} from path {!r} ({})", platform.value, path, url)
        return platform

    logger.debug("No known ATS for host {!r}, falling back to generic ({})", host, url)
    return Platform.GENERIC_HTML
