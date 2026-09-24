# VENDORED from CareerCrawler utils/names.py (branch feature/careercloud-mvp, e21045c) on 2026-09-24 (pure logic, imports rewritten to cloud.intel.vendor).
# Keep behaviour identical to the original; its tests there remain the reference.
"""Decide when two spellings name the same company.

Deduplication depends entirely on this. A company reaches the crawler from the
master sheet as ``"Acme Corp."``, from a discovery hit as ``"Acme Corporation"``
and from its own careers page as ``"ACME"``, and all three must resolve to one
record or the master list grows a duplicate every week::

    >>> from cloud.intel.vendor.names import company_key, company_slug, registrable_domain
    >>> company_slug("Acme Corporation")
    'acme'
    >>> company_slug("ACME Corp.")
    'acme'
    >>> registrable_domain("https://careers.acme.co.uk/jobs")
    'acme.co.uk'

Identity is decided in two steps, strongest evidence first.

**A registrable domain is proof.** Two records that resolve to ``acme.com`` are
the same company however they are spelled, and the domain survives rebrands,
punctuation and translation. This is the primary key wherever a website or board
URL exists.

**A name slug is inference.** Used only when no domain is available. Accents are
folded, case and punctuation are discarded, and a trailing legal form —
``Inc``, ``GmbH``, ``Pty Ltd`` — is removed, because it varies by
jurisdiction and by who typed the row.

The name rules are deliberately narrow. ``Group``, ``Holdings``,
``International`` and ``Technologies`` are *not* stripped, even though they look
like noise, because ``Bosch`` and ``Bosch Group`` are frequently different legal
entities with different boards. Merging two real companies loses one of them
permanently; keeping a near-duplicate is visible and fixable, so the ambiguous
cases are left alone.
"""

from __future__ import annotations

import re
from typing import Final, FrozenSet, Tuple
from urllib.parse import urlsplit

from cloud.intel.vendor.text import strip_accents

__all__ = [
    "LEGAL_SUFFIXES",
    "company_key",
    "company_slug",
    "domains_match",
    "normalise_name",
    "registrable_domain",
    "same_company",
]

#: Trailing tokens that state a company's legal form rather than its identity.
#: Stripped from the end of a name, repeatedly, so ``"Acme Holdings Pvt Ltd"``
#: loses ``Pvt`` and ``Ltd`` but keeps ``Holdings``.
LEGAL_SUFFIXES: Final[FrozenSet[str]] = frozenset(
    {
        # English-speaking jurisdictions.
        "inc",
        "incorporated",
        "llc",
        "lllp",
        "llp",
        "lp",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "company",
        "plc",
        "pc",
        "pllc",
        "pty",
        "pvt",
        "private",
        # Continental Europe.
        "gmbh",
        "mbh",
        "ag",
        "kg",
        "kgaa",
        "ohg",
        "gbr",
        "ug",
        "sa",
        "sas",
        "sarl",
        "sl",
        "slu",
        "srl",
        "spa",
        "bv",
        "nv",
        "cv",
        "ab",
        "asa",
        "oy",
        "oyj",
        "aps",
        "sp",
        "zoo",
        "dooel",
        "doo",
        "as",
        # Asia-Pacific.
        "kk",
        "kabushiki",
        "kaisha",
        "sdn",
        "bhd",
        "berhad",
        "tbk",
        "pte",
    }
)

#: Leading article, which some sheets include and others do not.
_LEADING_ARTICLE: Final[re.Pattern[str]] = re.compile(r"^the\s+")

#: Ampersand, spelled both ways in the same sheet.
_AMPERSAND: Final[re.Pattern[str]] = re.compile(r"\s*&\s*")

#: Anything that is not a letter or a digit, for slugging.
_NON_ALNUM: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: Runs of whitespace, including the tabs a pasted cell brings with it.
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")

#: Second-level labels that are part of a country's registry rather than a
#: company's name, so ``acme.co.uk`` is one company and ``co.uk`` is not a
#: company at all. The heuristic in :func:`registrable_domain` generalises this
#: to unlisted country codes; this set covers the ones that break the heuristic.
_MULTIPART_SUFFIXES: Final[FrozenSet[str]] = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "me.uk",
        "net.uk",
        "sch.uk",
        "ltd.uk",
        "plc.uk",
        "com.au",
        "net.au",
        "org.au",
        "edu.au",
        "gov.au",
        "asn.au",
        "id.au",
        "co.nz",
        "net.nz",
        "org.nz",
        "ac.nz",
        "govt.nz",
        "geek.nz",
        "co.za",
        "org.za",
        "net.za",
        "web.za",
        "co.jp",
        "ne.jp",
        "or.jp",
        "ac.jp",
        "go.jp",
        "co.kr",
        "or.kr",
        "co.in",
        "net.in",
        "org.in",
        "gen.in",
        "ind.in",
        "firm.in",
        "ac.in",
        "edu.in",
        "res.in",
        "co.il",
        "co.id",
        "co.th",
        "in.th",
        "com.br",
        "com.mx",
        "com.ar",
        "com.co",
        "com.pe",
        "com.ve",
        "com.uy",
        "com.ec",
        "com.sg",
        "com.my",
        "com.hk",
        "com.tw",
        "com.cn",
        "net.cn",
        "org.cn",
        "com.tr",
        "com.ua",
        "com.pl",
        "com.ph",
        "com.vn",
        "com.sa",
        "com.eg",
        "com.ng",
        "com.pk",
        "com.bd",
        "com.cy",
        "com.mt",
        "com.gr",
        "org.il",
        "gov.il",
        "ac.il",
        "ac.at",
        "co.at",
        "or.at",
        "gv.at",
        "co.ke",
        "co.tz",
        "co.ug",
    }
)

#: Labels that, in front of a two-letter country code, almost always belong to
#: the registry rather than to a company. Lets an unlisted ccTLD such as
#: ``com.xy`` be handled correctly instead of collapsing every company on it
#: onto a single key.
_REGISTRY_LABELS: Final[FrozenSet[str]] = frozenset(
    {
        "com",
        "co",
        "net",
        "org",
        "edu",
        "ac",
        "gov",
        "govt",
        "gob",
        "gv",
        "go",
        "mil",
        "int",
        "biz",
        "info",
        "name",
        "web",
        "ne",
        "or",
        "in",
        "id",
        "sch",
        "firm",
        "gen",
        "ind",
        "res",
        "asn",
        "nom",
        "principe",
        "ltd",
        "plc",
        "me",
    }
)

#: Hosts that serve every tenant of an applicant tracking system. A company is
#: never identified by one of these, because they are shared infrastructure:
#: ``boards.greenhouse.io/acme`` and ``boards.greenhouse.io/other`` are two
#: companies on one host, and keying on the host would merge the entire vendor.
_SHARED_ATS_HOSTS: Final[Tuple[str, ...]] = (
    "myworkdayjobs.com",
    "myworkdaysite.com",
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "icims.com",
    "ultipro.com",
    "smartrecruiters.com",
    "successfactors.com",
    "sapsf.com",
    "oraclecloud.com",
    "taleo.net",
    "jobvite.com",
    "teamtailor.com",
    "bamboohr.com",
    "recruitee.com",
    "workable.com",
    "dayforcehcm.com",
    "adp.com",
    "csod.com",
    "eightfold.ai",
    "phenompeople.com",
    "paylocity.com",
    "paycomonline.net",
    "paycor.com",
    "ukg.com",
    "ukgpro.com",
    "kronos.com",
    "myisolved.com",
    "asuresoftware.com",
    "applytojob.com",
    "rippling.com",
    "personio.de",
    "avature.net",
    "bullhornstaffing.com",
    "breezy.hr",
    "pinpointhq.com",
    "comeet.co",
    "fountain.com",
    "neogov.com",
    "oleeo.com",
    "jobscore.com",
    "gohire.io",
    "homerun.co",
    "join.com",
    "zoho.com",
    "manatal.com",
    "gem.com",
    "jobappnetwork.com",
    "applicantpro.com",
    "applicantstack.com",
    "clearcompany.com",
    "careerplug.com",
    "hireology.com",
    "hrmdirect.com",
    "silkroad.com",
    "recruiterbox.com",
    "trakstar.com",
    "radancy.com",
    "indeed.com",
    "linkedin.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "monster.com",
    "appone.com",
)


def normalise_name(name: str) -> str:
    """Fold a company name to a comparable, still-readable form.

    Args:
        name: The name as written anywhere.

    Returns:
        The name lowercased, accent-folded, with ``&`` spelled ``and``, a
        leading article removed and whitespace collapsed.
    """
    text = strip_accents(str(name or "")).lower().strip()
    text = _AMPERSAND.sub(" and ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return _LEADING_ARTICLE.sub("", text).strip()


def company_slug(name: str) -> str:
    """Reduce a company name to a single comparison token.

    Args:
        name: The name as written anywhere.

    Returns:
        The slug, e.g. ``"acmecorporation"`` becomes ``"acme"``. Empty when the
        name is blank or consists only of a legal form.
    """
    normalised = normalise_name(name)
    if not normalised:
        return ""

    # Strip trailing legal forms repeatedly: "Acme Pvt Ltd" sheds both.
    words = [_NON_ALNUM.sub("", word) for word in normalised.split(" ")]
    words = [word for word in words if word]

    while len(words) > 1 and words[-1] in LEGAL_SUFFIXES:
        words.pop()

    return "".join(words)


def _is_registry_suffix(labels: Tuple[str, ...]) -> bool:
    """Whether the final two labels belong to a registry rather than a company.

    Args:
        labels: The host split on dots.

    Returns:
        ``True`` when the host needs three labels to name a company.
    """
    if len(labels) < 3:
        return False

    candidate = ".".join(labels[-2:])
    if candidate in _MULTIPART_SUFFIXES:
        return True

    # Generalise to country codes not listed above: a two-letter final label
    # preceded by a registry word is a registry suffix, so `acme.com.xy` keys
    # on `acme.com.xy` rather than collapsing onto `com.xy`.
    second_level, top_level = labels[-2], labels[-1]
    return len(top_level) == 2 and second_level in _REGISTRY_LABELS


def registrable_domain(url_or_host: str) -> str:
    """Extract the domain a company actually owns.

    Args:
        url_or_host: A URL, or a bare hostname, in any case, with or without a
            scheme, a ``www.`` prefix, a port or a path.

    Returns:
        The registrable domain, e.g. ``"acme.co.uk"``. Empty when the input
        names no host, or names a host shared by every tenant of an ATS — those
        identify a vendor, never a company.
    """
    candidate = str(url_or_host or "").strip()
    if not candidate:
        return ""

    # urlsplit only finds a netloc when a scheme is present; the sheet's
    # `Website` column routinely holds a bare "www.acme.com".
    if "://" not in candidate:
        candidate = f"//{candidate}"

    try:
        host = (urlsplit(candidate).hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return ""

    if not host or "." not in host:
        return ""

    labels = tuple(label for label in host.split(".") if label)
    if len(labels) < 2:
        return ""

    take = 3 if _is_registry_suffix(labels) else 2
    domain = ".".join(labels[-take:])

    # A shared board host names the vendor, not the company on it.
    if any(domain == shared or domain.endswith(f".{shared}") for shared in _SHARED_ATS_HOSTS):
        return ""

    return domain


def domains_match(left: str, right: str) -> bool:
    """Whether two URLs or hosts belong to the same company.

    Args:
        left: A URL or hostname.
        right: A URL or hostname.

    Returns:
        ``True`` when both resolve to the same registrable domain. Two blanks
        are not a match: absence of evidence is not evidence of identity.
    """
    first = registrable_domain(left)
    return bool(first) and first == registrable_domain(right)


def company_key(name: str = "", website: str = "", career_url: str = "") -> str:
    """Produce the identity a company is stored and deduplicated under.

    Args:
        name: Company name, used when no usable domain is available.
        website: The company's own website, the strongest signal.
        career_url: Its careers page, used when the website is missing. Often
            an ATS host, which :func:`registrable_domain` rejects on purpose.

    Returns:
        ``"domain:acme.com"`` when a domain is known, otherwise ``"name:acme"``,
        or ``""`` when the record identifies nothing at all.
    """
    for source in (website, career_url):
        domain = registrable_domain(source)
        if domain:
            return f"domain:{domain}"

    slug = company_slug(name)
    return f"name:{slug}" if slug else ""


def same_company(
    left: Tuple[str, str, str],
    right: Tuple[str, str, str],
) -> bool:
    """Whether two ``(name, website, career_url)`` triples are one company.

    Domains are checked first and settle the question outright. Names are only
    consulted when neither record offers a domain, since two companies can
    share a name and one company can own several domains.

    Args:
        left: ``(name, website, career_url)``.
        right: ``(name, website, career_url)``.

    Returns:
        ``True`` when they identify the same company.
    """
    left_key = company_key(*left)
    right_key = company_key(*right)
    return bool(left_key) and left_key == right_key
