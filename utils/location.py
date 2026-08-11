"""Derive the ``Country`` column from a platform's free-text location string.

Applicant tracking systems publish locations as prose, not structured data:
``"Austin, TX"``, ``"London, United Kingdom"``, ``"Remote - US"``,
``"2 Locations"``. This module turns those into a country name, or an empty
string when the input does not support a confident answer.

    >>> from utils.location import derive_country
    >>> derive_country("Austin, TX")
    'United States'
    >>> derive_country("Bengaluru, Karnataka, India")
    'India'
    >>> derive_country("2 Locations")
    ''

Guessing is deliberately avoided: a bare city with no country or state marker
returns ``""`` rather than defaulting to the United States, so the export never
asserts a country the source did not support. The one deliberate exception is
the ``"CA"`` collision, documented in :func:`_country_from_single`.
"""

from __future__ import annotations

import re
from typing import Dict, Final, List

__all__ = ["derive_country", "split_locations"]

_UNITED_STATES: Final[str] = "United States"
_CANADA: Final[str] = "Canada"

#: US states and territories, plus DC. A trailing two-letter code from this set
#: is the single strongest US signal in ATS location strings.
_US_STATES: Final[frozenset[str]] = frozenset(
    """
    al ak az ar ca co ct de dc fl ga hi id il in ia ks ky la me md ma mi mn ms mo
    mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy
    as gu mp pr vi
    """.split()
)

#: Canadian provinces and territories.
_CA_PROVINCES: Final[frozenset[str]] = frozenset(
    "ab bc mb nb nl ns nt nu on pe qc sk yt".split()
)

#: Full state names, for boards that spell them out ("Austin, Texas").
_US_STATE_NAMES: Final[frozenset[str]] = frozenset(
    """
    alabama alaska arizona arkansas california colorado connecticut delaware
    florida georgia hawaii idaho illinois indiana iowa kansas kentucky louisiana
    maine maryland massachusetts michigan minnesota mississippi missouri montana
    nebraska nevada ohio oklahoma oregon pennsylvania tennessee texas utah
    vermont virginia washington wisconsin wyoming
    """.split()
    + [
        "new hampshire",
        "new jersey",
        "new mexico",
        "new york",
        "north carolina",
        "north dakota",
        "rhode island",
        "south carolina",
        "south dakota",
        "west virginia",
        "district of columbia",
        "puerto rico",
    ]
)

#: Lowercased alias -> canonical country name. Covers the spellings ATS boards
#: actually emit, including the abbreviations Workday and iCIMS favour.
_COUNTRY_ALIASES: Final[Dict[str, str]] = {
    # United States
    "united states": _UNITED_STATES,
    "united states of america": _UNITED_STATES,
    "usa": _UNITED_STATES,
    "u.s.a.": _UNITED_STATES,
    "u.s.": _UNITED_STATES,
    "us": _UNITED_STATES,
    "america": _UNITED_STATES,
    # United Kingdom
    "united kingdom": "United Kingdom",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "great britain": "United Kingdom",
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "wales": "United Kingdom",
    "northern ireland": "United Kingdom",
    "gb": "United Kingdom",
    # Rest of world, by frequency in job boards. Note the absence of "ca" as a
    # Canada alias — see _country_from_single for why.
    "canada": _CANADA,
    "mexico": "Mexico",
    "brazil": "Brazil",
    "brasil": "Brazil",
    "argentina": "Argentina",
    "chile": "Chile",
    "colombia": "Colombia",
    "costa rica": "Costa Rica",
    "india": "India",
    "china": "China",
    "hong kong": "Hong Kong",
    "taiwan": "Taiwan",
    "japan": "Japan",
    "south korea": "South Korea",
    "korea": "South Korea",
    "singapore": "Singapore",
    "malaysia": "Malaysia",
    "indonesia": "Indonesia",
    "thailand": "Thailand",
    "vietnam": "Vietnam",
    "viet nam": "Vietnam",
    "philippines": "Philippines",
    "australia": "Australia",
    "new zealand": "New Zealand",
    "ireland": "Ireland",
    "france": "France",
    "germany": "Germany",
    "deutschland": "Germany",
    "spain": "Spain",
    "espana": "Spain",
    "portugal": "Portugal",
    "italy": "Italy",
    "netherlands": "Netherlands",
    "the netherlands": "Netherlands",
    "holland": "Netherlands",
    "belgium": "Belgium",
    "luxembourg": "Luxembourg",
    "switzerland": "Switzerland",
    "austria": "Austria",
    "sweden": "Sweden",
    "norway": "Norway",
    "denmark": "Denmark",
    "finland": "Finland",
    "iceland": "Iceland",
    "poland": "Poland",
    "czech republic": "Czech Republic",
    "czechia": "Czech Republic",
    "slovakia": "Slovakia",
    "hungary": "Hungary",
    "romania": "Romania",
    "bulgaria": "Bulgaria",
    "greece": "Greece",
    "turkey": "Turkey",
    "ukraine": "Ukraine",
    "israel": "Israel",
    "united arab emirates": "United Arab Emirates",
    "uae": "United Arab Emirates",
    "saudi arabia": "Saudi Arabia",
    "qatar": "Qatar",
    "egypt": "Egypt",
    "south africa": "South Africa",
    "nigeria": "Nigeria",
    "kenya": "Kenya",
    "morocco": "Morocco",
}

#: Strings that name no place at all. Workday in particular reports
#: ``"3 Locations"`` when a posting spans several sites.
_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {
        "remote",
        "various",
        "various locations",
        "multiple",
        "multiple locations",
        "worldwide",
        "global",
        "anywhere",
        "flexible",
        "tbd",
        "n/a",
        "na",
        "none",
    }
)

#: Workday's "2 Locations" / "12 Locations" summary form.
_LOCATION_COUNT: Final[re.Pattern[str]] = re.compile(r"^\d+\s+locations?$")

#: Separators boards use between several locations in one string.
_MULTI_SEPARATOR: Final[re.Pattern[str]] = re.compile(r"\s*(?:;|\||\bor\b|/)\s*", re.IGNORECASE)

#: Leading qualifiers to drop: "Remote - Austin, TX", "Hybrid: London, UK".
_QUALIFIER: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:fully\s+)?(?:remote|hybrid|on[\s-]?site|virtual|field|home[\s-]?based|telecommute)"
    r"\s*(?:[-–—:,]\s*|\s+in\s+)",
    re.IGNORECASE,
)

_PUNCTUATION: Final[re.Pattern[str]] = re.compile(r"[\.\s]+")

#: Trailing site or building qualifier: "High Point, NC (EAS Premier)". Workday
#: tenants in particular append these, and they hide the state behind them.
_PARENTHETICAL: Final[re.Pattern[str]] = re.compile(r"\s*\([^)]*\)")


def _normalise(text: str) -> str:
    """Lowercase and collapse whitespace for comparison.

    Args:
        text: A location string or one of its segments.

    Returns:
        The comparable form.
    """
    return " ".join(str(text).lower().split())


def split_locations(location: str) -> List[str]:
    """Split a multi-location string into its individual locations.

    Boards join locations with semicolons, pipes, slashes or the word "or".
    Commas are *not* separators — they divide city from state from country
    inside a single location.

    Args:
        location: Raw location string as published.

    Returns:
        The individual location strings, stripped and non-empty. An empty list
        if there is nothing usable.
    """
    if not location:
        return []

    return [part.strip() for part in _MULTI_SEPARATOR.split(str(location)) if part.strip()]


def _country_from_single(location: str) -> str:
    """Derive the country from one location string.

    Args:
        location: A single location, e.g. ``"Austin, TX"``.

    Returns:
        The canonical country name, or ``""``.
    """
    text = _PARENTHETICAL.sub("", _QUALIFIER.sub("", location)).strip()
    normalised = _normalise(text)

    if not normalised or normalised in _PLACEHOLDERS or _LOCATION_COUNT.match(normalised):
        return ""

    # The whole string may itself be a country ("United States", "Remote - India").
    direct = _COUNTRY_ALIASES.get(normalised)
    if direct:
        return direct

    segments = [segment.strip() for segment in normalised.split(",") if segment.strip()]
    if not segments:
        return ""

    # Read right to left: boards order locations least- to most-specific
    # reversed, so the country (when present) is last.
    for segment in reversed(segments):
        collapsed = _PUNCTUATION.sub(" ", segment).strip()

        country = _COUNTRY_ALIASES.get(collapsed) or _COUNTRY_ALIASES.get(segment)
        if country:
            return country

        # A trailing two-letter code is only meaningful alongside a city; alone
        # it is as likely to be a country code as a state.
        #
        # "CA" collides: California and Canada. It is resolved as California,
        # because "City, CA" with no country is the US postal form, whereas
        # Canadian boards name the country ("Toronto, ON, Canada") or use a
        # province code that does not collide ("Vancouver, BC"). This mislabels
        # the rare board that writes "Montreal, CA"; every alternative mislabels
        # far more.
        if len(segments) > 1:
            token = collapsed.replace(" ", "")
            if token in _US_STATES:
                return _UNITED_STATES
            if token in _CA_PROVINCES:
                return _CANADA
            if collapsed in _US_STATE_NAMES:
                return _UNITED_STATES

        # A ZIP-style suffix ("Austin, TX 78701") still carries the state.
        match = re.match(r"^([a-z]{2})\s+\d{5}(?:-\d{4})?$", collapsed)
        if match and match.group(1) in _US_STATES:
            return _UNITED_STATES

    return ""


def derive_country(location: str) -> str:
    """Derive a country name from a platform's location string.

    Handles the shapes ATS boards emit: ``"Austin, TX"``, ``"London, UK"``,
    ``"Remote - Canada"``, ``"Austin, TX | Boston, MA"``, ``"3 Locations"``.

    Args:
        location: Location exactly as published by the platform.

    Returns:
        The canonical country name, or ``""`` when the string names no country,
        names several different ones, or is a placeholder such as
        ``"Multiple Locations"``. Ambiguity yields ``""`` rather than a guess,
        so the ``Country`` column never asserts more than the source did.
    """
    if not location:
        return ""

    countries = []
    for part in split_locations(location):
        country = _country_from_single(part)
        if country and country not in countries:
            countries.append(country)

    if len(countries) == 1:
        return countries[0]

    return ""
