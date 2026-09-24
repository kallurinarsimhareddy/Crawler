# VENDORED from CareerCrawler-seamless discovery/identity.py (uncommitted work on seamless-integration) on 2026-09-24 (pure logic, imports rewritten to cloud.intel.vendor).
# Keep behaviour identical to the original; its tests there remain the reference.
"""Deciding whether two records name the same company, and how sure we are.

The existing crawler answers this with one boolean built on one signal: a
registrable domain if there is one, otherwise a name slug. That is right for the
crawler, whose records come from a curated sheet, and wrong for discovery, whose
records arrive from anywhere with any subset of the evidence present. Measured
on the current primitives:

    Acme Inc          vs Acme LLC              -> same    (both slug to "acme")
    Acme Corp +site   vs Acme Corp (no site)   -> not same
    acme.com          vs acme.co.uk            -> not same

The first is a false merge waiting to happen -- two unrelated companies sharing
a name and no website become one row. The others are false negatives, and they
are the *normal* case for a discovered record, which often has a name and an
email domain and nothing else.

So this module keeps five signals and reports what it actually found:

=================  ===========================================================
company name       Normalised twice: once keeping the legal form, once without
website domain     The registrable domain, ATS hosts rejected
email domain       The part after ``@``, free providers rejected
linkedin           Canonicalised to ``linkedin.com/company/<slug>``
location           City, region and country, compared as a triple
=================  ===========================================================

**The name is deliberately compared with its legal form intact.** "Acme Inc" and
"Acme LLC" normalise to different names and do *not* match; they match only on
the suffix-stripped slug, which this module treats as a *similarity*, never as
identity. That is the whole difference between "probably the same" and "the same".

**No single weak signal merges anything.** A domain or a LinkedIn page is strong
evidence. A name alone, an email domain alone, a location alone are not, and
each of those on its own yields :data:`AMBIGUOUS` or :data:`NONE` rather than a
match. Combinations do the work: name and email domain together are strong, name
and location together are probable.

**A conflict always wins.** Two records whose domains disagree, or whose
LinkedIn pages disagree, are never merged automatically however much else
agrees -- that is the parent/subsidiary case, and guessing it wrong merges two
real companies into one and loses one of them. :data:`AMBIGUOUS` is the answer,
and a human decides.

Every result carries the signals that produced it, so a decision can be argued
with rather than taken on trust.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Final, FrozenSet, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

__all__ = [
    "AMBIGUOUS",
    "EXACT",
    "NONE",
    "OUTCOMES",
    "PROBABLE",
    "STRONG",
    "CandidateEvidence",
    "MatchResult",
    "canonical_linkedin",
    "email_domain",
    "evidence_from",
    "match",
    "normalise_location",
    "normalised_name",
]

#: The record is the same company beyond reasonable doubt: two strong signals
#: agree, or one strong signal agrees and the full name agrees with it.
EXACT: Final[str] = "EXACT"

#: One strong signal agrees, or two supporting signals do. Safe to merge.
STRONG: Final[str] = "STRONG"

#: Supporting evidence points the same way but nothing strong confirms it.
#: Worth a human's attention; not merged automatically.
PROBABLE: Final[str] = "PROBABLE"

#: The evidence cannot separate two candidates, or it actively disagrees.
#: Never merged automatically.
AMBIGUOUS: Final[str] = "AMBIGUOUS"

#: Nothing links the records.
NONE: Final[str] = "NONE"

#: Every outcome, strongest first.
OUTCOMES: Final[Tuple[str, ...]] = (EXACT, STRONG, PROBABLE, AMBIGUOUS, NONE)

#: Signals that can carry a match on their own.
STRONG_SIGNALS: Final[FrozenSet[str]] = frozenset({"website_domain", "linkedin"})

#: Signals that corroborate but never decide alone.
SUPPORTING_SIGNALS: Final[FrozenSet[str]] = frozenset({"company_name", "email_domain"})

#: Signals too weak to suggest identity by themselves.
WEAK_SIGNALS: Final[FrozenSet[str]] = frozenset({"location"})

#: A disagreement in one of these is never overridden by agreement elsewhere.
HARD_CONFLICTS: Final[FrozenSet[str]] = frozenset({"website_domain", "linkedin"})

#: Mailbox providers. An address at one of these belongs to a person, not to a
#: company, so its domain says nothing about which company they work for.
FREE_EMAIL_DOMAINS: Final[FrozenSet[str]] = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "outlook.co.uk", "live.com",
    "msn.com", "aol.com", "icloud.com", "me.com", "mac.com", "proton.me",
    "protonmail.com", "gmx.com", "gmx.de", "mail.com", "zoho.com", "yandex.ru",
    "qq.com", "163.com", "126.com", "naver.com", "web.de", "t-online.de",
    "comcast.net", "verizon.net", "sbcglobal.net", "att.net", "cox.net",
    "btinternet.com", "rediffmail.com", "hushmail.com", "fastmail.com",
})

#: Region names to their abbreviations, so "Oklahoma" and "OK" compare equal.
#: US states plus Canadian provinces, which is what these rosters contain.
_REGIONS: Final[Dict[str, str]] = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
    "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne",
    "nevada": "nv", "new hampshire": "nh", "new jersey": "nj",
    "new mexico": "nm", "new york": "ny", "north carolina": "nc",
    "north dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or",
    "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa",
    "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "district of columbia": "dc", "puerto rico": "pr",
    "ontario": "on", "quebec": "qc", "british columbia": "bc",
    "alberta": "ab", "manitoba": "mb", "saskatchewan": "sk",
    "nova scotia": "ns", "new brunswick": "nb", "newfoundland": "nl",
}

#: Country spellings that mean the same country.
_COUNTRIES: Final[Dict[str, str]] = {
    "usa": "us", "u.s.a.": "us", "u.s.": "us", "united states": "us",
    "united states of america": "us", "america": "us",
    "uk": "gb", "u.k.": "gb", "united kingdom": "gb", "great britain": "gb",
    "england": "gb", "scotland": "gb", "wales": "gb",
    "canada": "ca", "deutschland": "de", "germany": "de",
    "india": "in", "australia": "au", "france": "fr", "mexico": "mx",
}

_NON_ALNUM: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")
_LINKEDIN: Final[re.Pattern[str]] = re.compile(
    r"linkedin\.com/(?:company|school|showcase)/([A-Za-z0-9\-_%.]+)", re.I
)


def normalised_name(name: str) -> Tuple[str, str]:
    """Normalise a company name twice: with its legal form, and without.

    Both are needed, and conflating them is the bug this module exists to avoid.
    The full form distinguishes "Acme Inc" from "Acme LLC"; the slug groups them
    so they can be *considered* together. Identity uses the full form; the slug
    only ever raises a candidate for comparison.

    Args:
        name: The name as written anywhere.

    Returns:
        ``(full, slug)``. ``full`` keeps the legal suffix, lowercased,
        accent-folded, punctuation removed, whitespace collapsed. ``slug``
        additionally strips trailing legal forms.

    Examples:
        >>> normalised_name("Acme Inc.")
        ('acme inc', 'acme')
        >>> normalised_name("ACME  Corporation")
        ('acme corporation', 'acme')
    """
    from cloud.intel.vendor.names import company_slug, normalise_name

    text = normalise_name(name)
    if not text:
        return "", ""

    words = [_NON_ALNUM.sub("", word) for word in text.split(" ")]
    full = " ".join(word for word in words if word)
    return full, company_slug(name)


def email_domain(value: str) -> str:
    """The company domain an address implies, if it implies one.

    Args:
        value: An email address, or a bare domain.

    Returns:
        The registrable domain after the ``@``, or ``""`` when the address is
        at a mailbox provider -- a personal account says nothing about an
        employer -- or names no domain at all.

    Examples:
        >>> email_domain("jane.doe@acme.com")
        'acme.com'
        >>> email_domain("jane@gmail.com")
        ''
    """
    from cloud.intel.vendor.names import registrable_domain

    text = str(value or "").strip().lower()
    if not text:
        return ""

    if "@" in text:
        local, _, host = text.rpartition("@")
        # "@acme.com" is a fragment, not an address, and the domain of a
        # fragment is not evidence that anybody works there.
        if not local.strip():
            return ""
        host = host.strip().strip("<>,;")
    else:
        host = text.strip().strip("<>,;")

    if not host or "." not in host:
        return ""
    if host in FREE_EMAIL_DOMAINS:
        return ""

    domain = registrable_domain(host)
    return "" if domain in FREE_EMAIL_DOMAINS else domain


def canonical_linkedin(url: str) -> str:
    """Reduce a LinkedIn company URL to the identity it names.

    Args:
        url: Any LinkedIn company, school or showcase URL.

    Returns:
        ``"linkedin.com/company/<slug>"``, or ``""``. Locale hosts
        (``uk.linkedin.com``), ``www.``, trailing paths (``/about``), query
        strings, tracking parameters, fragments and trailing slashes are all
        removed; a personal ``/in/`` profile is not a company and yields
        nothing.

    Examples:
        >>> canonical_linkedin("https://www.linkedin.com/company/Acme-Corp/about/?trk=x")
        'linkedin.com/company/acme-corp'
        >>> canonical_linkedin("https://www.linkedin.com/in/jane-doe")
        ''
    """
    text = str(url or "").strip()
    if not text:
        return ""

    match = _LINKEDIN.search(text)
    if not match:
        return ""

    slug = match.group(1).strip().strip("/").lower()
    slug = slug.split("?", 1)[0].split("#", 1)[0]
    return f"linkedin.com/company/{slug}" if slug else ""


def normalise_location(city: str = "", region: str = "",
                       country: str = "") -> Tuple[str, str, str]:
    """Fold a location into a comparable triple.

    Args:
        city: Locality.
        region: State or province, spelled out or abbreviated.
        country: Country, spelled out or as a code.

    Returns:
        ``(city, region, country)``, lowercased and accent-folded, with the
        region reduced to its abbreviation and the country to its code so
        "Oklahoma"/"OK" and "United States"/"USA"/"US" compare equal.
    """
    from cloud.intel.vendor.text import strip_accents

    def fold(value: str) -> str:
        """Lowercase, fold accents and collapse punctuation.

        Args:
            value: Any location part.

        Returns:
            The comparable form.
        """
        text = strip_accents(str(value or "")).lower().strip()
        return re.sub(r"[^a-z0-9 ]+", " ", text).strip()

    folded_city = re.sub(r"\s+", " ", fold(city))
    folded_region = re.sub(r"\s+", " ", fold(region))
    folded_country = re.sub(r"\s+", " ", fold(country))

    # Look the country up before *and* after punctuation is folded away:
    # "U.S.A." becomes "u s a", which is in no table, while "u.s.a." is.
    raw_country = str(country or "").strip().lower()
    country_code = (
        _COUNTRIES.get(raw_country)
        or _COUNTRIES.get(folded_country)
        or folded_country
    )

    return (folded_city, _REGIONS.get(folded_region, folded_region), country_code)


@dataclass(frozen=True)
class CandidateEvidence:
    """What is known about one company record, raw and normalised.

    The originals are kept beside the normalised forms because normalisation is
    lossy and the original is the evidence. A reviewer looking at a decision
    needs to see the URL as published, not the domain it was reduced to.

    Attributes:
        name: The company name as given.
        website: The website as given.
        email: The address or domain as given.
        linkedin: The LinkedIn URL as given.
        city: The locality as given.
        region: The state or province as given.
        country: The country as given.
        source: Where this record came from.
        name_full: Normalised name, legal form kept.
        name_slug: Normalised name, legal form stripped.
        domain: Registrable website domain.
        mail_domain: Registrable email domain.
        linkedin_id: Canonical LinkedIn identity.
        location: Normalised ``(city, region, country)``.
    """

    name: str = ""
    website: str = ""
    email: str = ""
    linkedin: str = ""
    city: str = ""
    region: str = ""
    country: str = ""
    source: str = ""

    name_full: str = ""
    name_slug: str = ""
    domain: str = ""
    mail_domain: str = ""
    linkedin_id: str = ""
    location: Tuple[str, str, str] = ("", "", "")

    @property
    def has_identity(self) -> bool:
        """Whether anything here could identify a company at all.

        Returns:
            Whether at least one non-location signal is present.
        """
        return bool(self.name_full or self.domain or self.mail_domain
                    or self.linkedin_id)

    def as_dict(self) -> Dict[str, str]:
        """The evidence flattened for storage.

        Returns:
            Original and normalised values, as strings.
        """
        city, region, country = self.location
        return {
            "name": self.name, "website": self.website, "email": self.email,
            "linkedin": self.linkedin, "city": self.city, "region": self.region,
            "country": self.country, "source": self.source,
            "name_full": self.name_full, "name_slug": self.name_slug,
            "domain": self.domain, "mail_domain": self.mail_domain,
            "linkedin_id": self.linkedin_id,
            "location_city": city, "location_region": region,
            "location_country": country,
        }


def evidence_from(
    name: str = "",
    website: str = "",
    email: str = "",
    linkedin: str = "",
    city: str = "",
    region: str = "",
    country: str = "",
    location: str = "",
    source: str = "",
) -> CandidateEvidence:
    """Build the evidence for one record, normalising every signal.

    Args:
        name: Company name.
        website: Website or bare host.
        email: A work address, or a bare domain.
        linkedin: A LinkedIn company URL.
        city: Locality.
        region: State or province.
        country: Country.
        location: A free-text location, used only when ``city``/``region``/
            ``country`` are not given separately.
        source: Where the record came from.

    Returns:
        The evidence, originals preserved.
    """
    from cloud.intel.vendor.names import registrable_domain

    if location and not (city or region or country):
        parts = [part.strip() for part in str(location).split(",") if part.strip()]
        city = parts[0] if parts else ""
        region = parts[1] if len(parts) > 1 else ""
        country = parts[2] if len(parts) > 2 else ""

    full, slug = normalised_name(name)

    return CandidateEvidence(
        name=str(name or ""), website=str(website or ""), email=str(email or ""),
        linkedin=str(linkedin or ""), city=str(city or ""),
        region=str(region or ""), country=str(country or ""),
        source=str(source or ""),
        name_full=full, name_slug=slug,
        domain=registrable_domain(website),
        mail_domain=email_domain(email),
        linkedin_id=canonical_linkedin(linkedin),
        location=normalise_location(city, region, country),
    )


@dataclass
class MatchResult:
    """The decision, and the evidence that produced it.

    Attributes:
        outcome: One of :data:`OUTCOMES`.
        matched_signals: Signals that agreed.
        conflicting_signals: Signals that disagreed.
        near_signals: Signals that nearly agreed -- a shared name slug where
            the full names differ, which is a reason to look, not to merge.
        reasons: Human-readable statements behind the decision.
    """

    outcome: str = NONE
    matched_signals: List[str] = field(default_factory=list)
    conflicting_signals: List[str] = field(default_factory=list)
    near_signals: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    @property
    def mergeable(self) -> bool:
        """Whether this decision may merge records without a human.

        Returns:
            Whether the outcome is :data:`EXACT` or :data:`STRONG`. Anything
            weaker is a question, and a question is not an answer.
        """
        return self.outcome in (EXACT, STRONG)

    def describe(self) -> str:
        """A one-line summary.

        Returns:
            The outcome with its signals.
        """
        parts = [self.outcome]
        if self.matched_signals:
            parts.append("matched=" + ",".join(sorted(self.matched_signals)))
        if self.conflicting_signals:
            parts.append("conflict=" + ",".join(sorted(self.conflicting_signals)))
        if self.near_signals:
            parts.append("near=" + ",".join(sorted(self.near_signals)))
        return " ".join(parts)


def _compare_signals(
    left: CandidateEvidence, right: CandidateEvidence
) -> Tuple[List[str], List[str], List[str], List[str]]:
    """Work out which signals agree, disagree, nearly agree, or are absent.

    A signal only participates when *both* records carry it. Absence is never
    agreement: two records with no website do not have matching websites.

    Args:
        left: One record's evidence.
        right: The other's.

    Returns:
        ``(matched, conflicting, near, reasons)``.
    """
    matched: List[str] = []
    conflicting: List[str] = []
    near: List[str] = []
    reasons: List[str] = []

    if left.domain and right.domain:
        if left.domain == right.domain:
            matched.append("website_domain")
            reasons.append(f"same website domain ({left.domain})")
        else:
            conflicting.append("website_domain")
            reasons.append(
                f"different website domains ({left.domain} vs {right.domain})")

    if left.linkedin_id and right.linkedin_id:
        if left.linkedin_id == right.linkedin_id:
            matched.append("linkedin")
            reasons.append(f"same LinkedIn page ({left.linkedin_id})")
        else:
            conflicting.append("linkedin")
            reasons.append(
                f"different LinkedIn pages ({left.linkedin_id} vs "
                f"{right.linkedin_id})")

    if left.name_full and right.name_full:
        if left.name_full == right.name_full:
            matched.append("company_name")
            reasons.append(f"same company name ({left.name_full})")
        elif left.name_slug and left.name_slug == right.name_slug:
            # "Acme Inc" and "Acme LLC": the same name wearing different legal
            # forms, which is exactly as likely to be two companies as one.
            near.append("company_name")
            reasons.append(
                f"names share a slug but differ in legal form "
                f"({left.name_full} vs {right.name_full})")
        else:
            reasons.append(
                f"different company names ({left.name_full} vs {right.name_full})")

    if left.mail_domain and right.mail_domain:
        if left.mail_domain == right.mail_domain:
            matched.append("email_domain")
            reasons.append(f"same work email domain ({left.mail_domain})")
        else:
            reasons.append(
                f"different work email domains ({left.mail_domain} vs "
                f"{right.mail_domain})")

    # One record's website and the other's work email resolving to the same
    # registrable domain is a *domain* agreement, not a lesser one -- it is the
    # combination the specification names as preferred. Recording it as
    # "website_domain" also means it collapses with a direct website match
    # rather than being counted a second time, so acme.com seen twice on each
    # side stays one piece of evidence.
    for one, other in ((left, right), (right, left)):
        if one.domain and other.mail_domain and one.domain == other.mail_domain:
            if "website_domain" not in matched:
                matched.append("website_domain")
                reasons.append(
                    f"one record's website and the other's work email domain "
                    f"agree ({one.domain})")

    if any(left.location) and any(right.location):
        if left.location == right.location:
            matched.append("location")
            reasons.append("same location")
        elif (left.location[2] and right.location[2]
              and left.location[2] != right.location[2]):
            conflicting.append("location")
            reasons.append(
                f"different countries ({left.location[2]} vs {right.location[2]})")

    return matched, conflicting, near, reasons


def match(left: CandidateEvidence, right: CandidateEvidence) -> MatchResult:
    """Decide whether two records name the same company.

    The rules, in the order they are applied:

    1. **A hard conflict ends it.** Disagreeing websites or disagreeing
       LinkedIn pages yield :data:`AMBIGUOUS` whatever else agrees. This is the
       parent/subsidiary case and the two-companies-one-name case, and merging
       either loses a real company.
    2. **Two strong signals, or one strong plus the full name, is EXACT.**
    3. **One strong signal is STRONG.** A shared registrable domain, or a
       shared LinkedIn page, is enough on its own.
    4. **Name and email domain together are STRONG.** Neither would do alone.
    5. **Name and location together are PROBABLE.** Worth review, not a merge.
    6. **One supporting signal alone is AMBIGUOUS.** A name on its own, or an
       email domain on its own, cannot separate two companies.
    7. **Everything else is NONE.** Location alone included.

    Args:
        left: One record's evidence.
        right: The other's.

    Returns:
        The decision with its evidence.
    """
    if not left.has_identity or not right.has_identity:
        return MatchResult(NONE, reasons=["one record carries no identifying evidence"])

    matched, conflicting, near, reasons = _compare_signals(left, right)

    hard = [signal for signal in conflicting if signal in HARD_CONFLICTS]
    if hard:
        return MatchResult(
            AMBIGUOUS, matched, conflicting, near,
            reasons + [f"refusing to merge: {', '.join(hard)} disagree"],
        )

    strong = [s for s in matched if s in STRONG_SIGNALS]
    support = [s for s in matched if s in SUPPORTING_SIGNALS]
    weak = [s for s in matched if s in WEAK_SIGNALS]

    if len(strong) >= 2:
        outcome = EXACT
    elif strong and "company_name" in support:
        outcome = EXACT
    elif strong:
        outcome = STRONG
    elif len(support) >= 2:
        outcome = STRONG
    elif "company_name" in support and weak:
        outcome = PROBABLE
    elif support or near:
        outcome = AMBIGUOUS
    else:
        outcome = NONE

    if outcome in (EXACT, STRONG) and near and not strong:
        # The names only agree on their slug; that cannot carry a merge.
        outcome = PROBABLE
        reasons.append("downgraded: name agreement is only on the stripped slug")

    return MatchResult(outcome, matched, conflicting, near, reasons)
