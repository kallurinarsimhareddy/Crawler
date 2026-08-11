"""Find job postings hidden in a page's JavaScript state rather than its markup.

Modern career pages ship the job list to the browser as JSON and let JavaScript
render it. The markup a plain ``requests`` GET sees therefore contains every
posting — just not in any ``<a>`` tag. This module reads those payloads:

* ``<script id="__NEXT_DATA__">`` — Next.js
* ``window.__NUXT__`` — Nuxt
* ``window.__APOLLO_STATE__`` — Apollo GraphQL's normalised cache
* ``window.__INITIAL_STATE__`` and its many spellings — Redux/Vuex/Pinia
* ``<script type="application/json">`` — Inertia, Ember, Remix, hand-rolled
* ``data-page`` / ``data-props`` attributes — Inertia and friends

Whatever the wrapper, the payload is decoded to plain Python and then searched
for objects that *look like* a posting. The search is shape-based rather than
schema-based, because every vendor names its fields differently: an object
qualifies when it carries a plausible title plus at least one corroborating
signal (a URL, a location, a requisition id, a posted date, ...).

False positives are the real risk — a navigation menu is also a list of objects
with a title and a URL. Two things hold them off. Candidates are grouped by
their exact key set and only the largest group is used, because a job list is
the page's biggest run of identically-shaped objects. And a group is rejected
unless its shape carries a job-specific field, not merely title and link.

Nothing here performs I/O and nothing raises: a page this module cannot read
yields an empty list and the caller falls through to its next strategy.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Dict, Final, List, Optional, Sequence, Tuple

from bs4 import BeautifulSoup
from loguru import logger

from models.job import Job
from utils.html import absolute_url, clean_text, parse_html
from utils.jobs import build_job

__all__ = [
    "embedded_json",
    "job_like_objects",
    "jobs_from_payload",
    "jobs_from_state",
    "script_endpoints",
    "text_of",
]

#: Global variables that single-page career sites hydrate themselves from. Each
#: is matched as ``<name> = <json>`` inside an inline script.
_STATE_VARIABLES: Final[Tuple[str, ...]] = (
    "__NUXT__",
    "__APOLLO_STATE__",
    "__INITIAL_STATE__",
    "__PRELOADED_STATE__",
    "__INITIAL_DATA__",
    "__INITIAL_PROPS__",
    "__NEXT_DATA__",
    "__REDUX_STATE__",
    "__SERVER_DATA__",
    "__STATE__",
    "__DATA__",
    "__remixContext",
    "__sveltekit_data",
    "APP_STATE",
    "PAGE_DATA",
    "pageData",
    "initialState",
    "serverData",
    "jobsData",
    "jobData",
)

#: ``window.__NUXT__ = {...}`` / ``var __INITIAL_STATE__={...}`` / ``self.X = [...]``.
#: The body is located by name and then balanced-brace scanned, because a job
#: description routinely contains braces, quotes and semicolons of its own.
_STATE_ASSIGNMENT: Final[re.Pattern[str]] = re.compile(
    r"(?:window|self|globalThis|var|let|const)?\s*\.?\s*\b(%s)\b\s*=\s*(?=[\[{])"
    % "|".join(re.escape(name) for name in _STATE_VARIABLES)
)

#: HTML attributes carrying a JSON payload, used by Inertia and similar bridges.
_JSON_ATTRIBUTES: Final[Tuple[str, ...]] = (
    "data-page",
    "data-props",
    "data-state",
    "data-jobs",
    "data-initial-state",
    "data-component-props",
)

#: Keys whose value is the posting's title.
_TITLE_KEYS: Final[Tuple[str, ...]] = (
    "title", "jobtitle", "job_title", "name", "positiontitle", "position_title",
    "postingtitle", "posting_title", "positionname", "position_name", "jobname",
    "job_name", "displayname", "display_name", "roletitle", "role_title",
    "requisitiontitle", "vacancytitle", "advertisedtitle", "headline", "label",
)

#: Keys whose value is the posting's own URL.
_URL_KEYS: Final[Tuple[str, ...]] = (
    "url", "absolute_url", "absoluteurl", "joburl", "job_url", "applyurl",
    "apply_url", "hostedurl", "hosted_url", "canonicalurl", "canonical_url",
    "permalink", "link", "href", "detailurl", "detail_url", "jobpostingurl",
    "postingurl", "posting_url", "externalpath", "external_path", "applylink",
    "landingpageurl", "joblink", "job_link", "seourl", "path", "slug",
)

#: Keys whose value names where the job is. Ordered best-first, because the
#: first one present wins: a pre-formatted location beats one that has to be
#: assembled, and a posting-specific location beats the employer's address.
_LOCATION_KEYS: Final[Tuple[str, ...]] = (
    "location", "locations", "locationname", "location_name", "joblocation",
    "job_location", "joblocations", "postinglocations", "posting_locations",
    "requisitionlocations", "worklocations", "work_locations",
    "primarylocation", "primary_location", "city", "cityname",
    "locationtext", "location_text", "formattedlocation", "workplace",
    "locationsstext", "officelocation", "office", "region", "worklocation",
    "locationdescription", "geo", "address", "locationsstring",
)

#: Keys whose value names the country directly.
_COUNTRY_KEYS: Final[Tuple[str, ...]] = (
    "country", "countryname", "country_name", "countrycode", "locationcountry",
)

#: Keys that only a job posting carries. A candidate group must show at least
#: one of these, which is what keeps navigation menus and breadcrumb trails out.
_JOB_SIGNAL_KEYS: Final[frozenset] = frozenset(
    _LOCATION_KEYS
    + _COUNTRY_KEYS
    + (
        "department", "departments", "departmentname", "team", "teamname",
        "employmenttype", "employment_type", "jobtype", "job_type", "worktype",
        "contracttype", "schedule", "shift", "category", "categories",
        "jobcategory", "function", "jobfunction", "discipline",
        "requisitionid", "requisition_id", "reqid", "req_id", "jobid", "job_id",
        "jobcode", "job_code", "shortcode", "postingid", "posting_id",
        "externalid", "vacancyid", "opportunityid", "positionid",
        "dateposted", "date_posted", "postedon", "posted_on", "posteddate",
        "publisheddate", "published_at", "publishedat", "createdat",
        "created_at", "updatedat", "firstpublished", "livedate",
        "salary", "compensation", "payrange", "pay_range", "remote",
        "isremote", "is_remote", "workplacetype", "seniority", "experience",
        "applyurl", "apply_url", "applylink", "brand", "businessunit",
    )
)

#: Bounds on a plausible job title, in characters. Matches adapters.generic.
_MIN_TITLE: Final[int] = 3
_MAX_TITLE: Final[int] = 180

#: Titles that are page furniture rather than a posting.
_NON_TITLES: Final[frozenset] = frozenset(
    {
        "careers", "career", "jobs", "job", "search", "search jobs", "home",
        "all jobs", "open positions", "apply", "apply now", "view all",
        "view all jobs", "login", "sign in", "register", "about", "about us",
        "contact", "contact us", "privacy", "terms", "cookies", "help",
        "benefits", "culture", "our team", "join us", "join our team", "more",
        "next", "previous", "back", "english", "united states", "remote",
    }
)

#: A candidate group must have at least this many members to outvote the rest.
#: One is allowed because plenty of small boards advertise a single opening.
_MIN_GROUP: Final[int] = 1

#: Depth ceiling on the payload walk, so a self-referential structure cannot
#: spin. Apollo caches nest deeply but never anywhere near this far.
_MAX_DEPTH: Final[int] = 24

#: Ceiling on candidate objects collected from one payload.
_MAX_CANDIDATES: Final[int] = 20000

#: Script ``src`` values worth reporting in an unknown-platform diagnostic.
_BUNDLE_SUFFIXES: Final[Tuple[str, ...]] = (".js", ".mjs", ".jsx", ".ts")

#: Absolute or root-relative API-ish paths appearing anywhere in a page's
#: scripts. Reported for diagnostics so an unsupported board can be adapted.
#: The optional query tail matters: a bare path tells you where to look, but
#: the parameters are what tell you how to page and filter it.
_ENDPOINT: Final[re.Pattern[str]] = re.compile(
    r"""["'`](/(?:[\w.\-]+/)*(?:api|graphql|jobs|search|positions|openings|"""
    r"""postings|vacancies|careers|widgets|services|rest)[\w./\-]*"""
    r"""(?:\?[\w=&%.\-+]*)?)["'`]""",
    re.IGNORECASE,
)


#: The three parts of an address, each with every spelling the vendors use, in
#: the order they should be joined. Looked up before any single-key read, so a
#: structured address is never flattened to just its city.
_ADDRESS_PARTS: Final[Tuple[Tuple[str, ...], ...]] = (
    ("addressLocality", "cityName", "city", "locality", "town", "municipality"),
    (
        "addressRegion",
        "countrySubdivisionLevel1",
        "stateProvince",
        "state",
        "province",
        "region",
        "stateCode",
    ),
    ("addressCountry", "country", "countryName", "countryCode"),
)

#: Keys to read a dictionary's text from, best first. ``address`` is included
#: so a posting whose location nests one level deeper is still readable.
_TEXT_KEYS: Final[Tuple[str, ...]] = (
    "name",
    "longName",
    "label",
    "text",
    "value",
    "title",
    "displayName",
    "formattedAddress",
    "address",
    "city",
    "cityName",
    "locality",
    "description",
    "fullName",
    "shortName",
    "codeValue",
)


def _first_present(value: Dict[str, Any], keys: Sequence[str]) -> str:
    """Read the first of ``keys`` present on ``value`` that yields text.

    Args:
        value: A decoded JSON object, with its keys as published.
        keys: Field aliases to try, best first.

    Returns:
        The text found, or ``""``.
    """
    for key in keys:
        if key in value:
            found = text_of(value[key])
            if found:
                return found
    return ""


def text_of(value: Any) -> str:
    """Reduce any decoded JSON value to a single readable string.

    Vendors publish the same field as a string, a ``{"name": ...}`` object, or a
    list of either, so every read of a payload field goes through here.

    Args:
        value: Any decoded JSON value.

    Returns:
        The text it represents, with lists joined by ``", "``. ``""`` when the
        value carries no readable text.
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return clean_text(value)

    if isinstance(value, dict):
        # An address is checked first: its parts are individually readable, so
        # a single-key lookup would find "Austin" and silently drop the state
        # and country beside it. Each part is looked up under every spelling
        # the vendors use, because there is no agreement whatsoever — one
        # writes ``addressRegion``, another ``countrySubdivisionLevel1``.
        assembled = ", ".join(
            part
            for part in (_first_present(value, group) for group in _ADDRESS_PARTS)
            if part
        )
        if assembled:
            return assembled

        for key in _TEXT_KEYS:
            if key in value:
                found = text_of(value[key])
                if found:
                    return found
        return ""

    if isinstance(value, (list, tuple)):
        seen: List[str] = []
        for item in value:
            found = text_of(item)
            if found and found not in seen:
                seen.append(found)
        return ", ".join(seen[:6])

    return ""


def _first(obj: Dict[str, Any], keys: Sequence[str]) -> str:
    """Read the first of ``keys`` present on ``obj`` that yields text.

    Args:
        obj: A candidate posting object, whose keys have been lowercased.
        keys: Field aliases to try, best first.

    Returns:
        The text found, or ``""``.
    """
    for key in keys:
        if key in obj:
            found = text_of(obj[key])
            if found:
                return found
    return ""


def _plausible_title(text: str) -> bool:
    """Report whether a string could be a job title.

    Args:
        text: Candidate title.

    Returns:
        ``True`` when it is the right length, contains letters, and is not
        obvious page furniture.
    """
    if not (_MIN_TITLE <= len(text) <= _MAX_TITLE):
        return False
    if text.lower().strip(" .:*>|-") in _NON_TITLES:
        return False
    if not any(character.isalpha() for character in text):
        return False
    # A sentence is a description, not a title.
    return text.count(".") <= 3 and "\n" not in text


def _lowered(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Re-key an object so field lookups are case- and separator-insensitive.

    ``jobTitle``, ``job_title`` and ``JOBTITLE`` all become ``jobtitle``.

    Args:
        obj: One decoded JSON object.

    Returns:
        The same values under normalised keys. The first spelling of a key
        wins, so an explicit ``title`` is not shadowed by a later ``Title``.
    """
    flat: Dict[str, Any] = {}
    for key, value in obj.items():
        if not isinstance(key, str):
            continue
        normalised = key.lower()
        if normalised not in flat:
            flat[normalised] = value
        stripped = normalised.replace("_", "").replace("-", "")
        if stripped not in flat:
            flat[stripped] = value
    return flat


def _balanced_json(source: str, start: int) -> str:
    """Return the JSON literal beginning at ``start``, respecting nesting.

    A regex cannot do this: job descriptions contain braces, brackets and
    escaped quotes, so the literal has to be scanned with a string-aware depth
    counter.

    Args:
        source: The script text.
        start: Index of the opening ``{`` or ``[``.

    Returns:
        The complete literal, or ``""`` if it is never closed.
    """
    opener = source[start]
    closer = {"{": "}", "[": "]"}.get(opener, "")
    if not closer:
        return ""

    depth = 0
    in_string = False
    quote = ""
    escaped = False

    for index in range(start, min(len(source), start + 8 * 1024 * 1024)):
        character = source[index]

        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                in_string = False
            continue

        if character in "\"'":
            in_string = True
            quote = character
        elif character == opener:
            depth += 1
        elif character == closer:
            depth -= 1
            if depth == 0:
                return source[start : index + 1]

    return ""


def _decode(raw: str) -> Optional[Any]:
    """Decode a JSON literal, tolerating the dialects inline scripts use.

    Args:
        raw: The literal.

    Returns:
        The decoded value, or ``None`` if it is not readable as JSON.
    """
    text = raw.strip().rstrip(";").strip()
    if not text:
        return None

    try:
        return json.loads(text)
    except ValueError:
        pass

    # Server-rendered state is often HTML-escaped into the script body, and
    # occasionally single-quoted. Both are cheap to undo; anything more
    # exotic (functions, `undefined`, dates) is genuinely not JSON.
    repaired = (
        text.replace("&quot;", '"')
        .replace("&#34;", '"')
        .replace("&amp;", "&")
        .replace("&#39;", "'")
        .replace("\\u002F", "/")
        .replace("\\/", "/")
    )
    try:
        return json.loads(repaired)
    except ValueError:
        return None


def embedded_json(markup: str, soup: Optional[BeautifulSoup] = None) -> List[Any]:
    """Decode every JSON payload the page ships to its own JavaScript.

    Args:
        markup: Raw HTML.
        soup: Pre-parsed document, to avoid parsing twice. Parsed here when
            omitted.

    Returns:
        Every payload decoded, largest first — the job list is almost always
        the biggest thing on the page. Empty when the page embeds no JSON.
    """
    if not markup:
        return []

    document = soup if soup is not None else parse_html(markup)
    payloads: List[Any] = []

    # 1. Declared JSON script blocks: __NEXT_DATA__, Inertia, Remix, Ember.
    for script in document.find_all("script"):
        script_type = str(script.get("type") or "").lower()
        if script_type and script_type not in {
            "application/json",
            "application/ld+json",
            "text/json",
            "application/x-json",
        }:
            continue
        if script_type == "application/ld+json":
            # utils.html.json_ld_job_postings already owns this shape.
            continue
        if not script_type and not script.get("id"):
            continue

        decoded = _decode(script.string or script.get_text() or "")
        if decoded is not None:
            payloads.append(decoded)

    # 2. State assigned to a global inside an inline script.
    for script in document.find_all("script"):
        if script.get("src"):
            continue
        body = script.string or script.get_text() or ""
        if not body or "=" not in body:
            continue

        for match in _STATE_ASSIGNMENT.finditer(body):
            literal = _balanced_json(body, match.end())
            if not literal:
                continue
            decoded = _decode(literal)
            if decoded is not None:
                payloads.append(decoded)

    # 3. JSON parked in an attribute.
    for attribute in _JSON_ATTRIBUTES:
        for node in document.find_all(attrs={attribute: True}):
            decoded = _decode(str(node.get(attribute) or ""))
            if decoded is not None:
                payloads.append(decoded)

    payloads.sort(key=lambda payload: -len(json.dumps(payload, default=str)[:2_000_000]))
    logger.debug("Discovery: {} embedded JSON payload(s)", len(payloads))
    return payloads


def job_like_objects(payload: Any) -> List[Dict[str, Any]]:
    """Find the objects inside a payload that describe job postings.

    Every dictionary anywhere in the payload is considered, then grouped by its
    exact key set. The largest group whose shape carries a job-specific field
    wins — a job list is a page's longest run of identically-shaped objects,
    and requiring a job-specific field is what stops a navigation menu (title
    plus link, nothing more) from being mistaken for one.

    Args:
        payload: Any decoded JSON value.

    Returns:
        The posting objects, key-normalised by :func:`_lowered`, in the order
        they appeared. Empty when nothing in the payload looks like a posting.
    """
    candidates: List[Dict[str, Any]] = []

    def walk(node: Any, depth: int) -> None:
        if depth > _MAX_DEPTH or len(candidates) >= _MAX_CANDIDATES:
            return

        if isinstance(node, dict):
            flat = _lowered(node)
            title = _first(flat, _TITLE_KEYS)
            if _plausible_title(title):
                candidates.append(flat)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)

    walk(payload, 0)
    if not candidates:
        return []

    groups: Dict[frozenset, List[Dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        groups[frozenset(candidate)].append(candidate)

    scored: List[Tuple[int, int, List[Dict[str, Any]]]] = []
    for shape, members in groups.items():
        if len(members) < _MIN_GROUP:
            continue
        signals = len(shape & _JOB_SIGNAL_KEYS)
        if not signals:
            # Title and a link alone describe a menu just as well as a board.
            continue
        scored.append((len(members), signals, members))

    if not scored:
        return []

    # Most members wins; a tie goes to the shape that looks most job-like.
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best = scored[0][2]

    logger.debug(
        "Discovery: {} candidate object(s) in {} shape(s); using {}",
        len(candidates),
        len(groups),
        len(best),
    )
    return best


def _job_url(obj: Dict[str, Any], page_url: str) -> str:
    """Work out the posting's own URL from a candidate object.

    Args:
        obj: A key-normalised candidate object.
        page_url: URL the payload came from, for resolving relative links.

    Returns:
        An absolute URL, or ``""`` when the object names none. A bare slug is
        only accepted when it looks like a path, since ``slug`` is also used
        for departments and other taxonomy.
    """
    for key in _URL_KEYS:
        if key not in obj:
            continue
        raw = obj[key]
        if isinstance(raw, dict):
            raw = raw.get("href") or raw.get("url") or raw.get("value")
        if not isinstance(raw, str):
            continue

        candidate = raw.strip()
        if not candidate:
            continue
        if key == "slug" and "/" not in candidate:
            continue

        resolved = absolute_url(page_url, candidate)
        if resolved:
            return resolved

    return ""


def jobs_from_payload(
    payload: Any,
    page_url: str,
    company_name: str,
    platform: str,
    career_page_url: str = "",
    url_builder: Optional[Any] = None,
) -> List[Optional[Job]]:
    """Turn one decoded payload into postings.

    Args:
        payload: Any decoded JSON value.
        page_url: URL the payload came from, for resolving relative links.
        company_name: Company as named in the input sheet.
        platform: Label for the ``Platform`` column.
        career_page_url: Board URL recorded on each job. Defaults to
            ``page_url``.
        url_builder: ``(object, page_url) -> str``, called when the object
            names no URL of its own. Lets an adapter that knows its vendor's
            URL layout rebuild one from an id, without this module having to
            know about it.

    Returns:
        One entry per posting found; unusable postings are ``None``, which
        :func:`utils.jobs.dedupe` drops.
    """
    collected: List[Optional[Job]] = []

    for obj in job_like_objects(payload):
        title = _first(obj, _TITLE_KEYS)
        url = _job_url(obj, page_url)

        if not url and url_builder is not None:
            try:
                url = str(url_builder(obj, page_url) or "")
            except Exception:  # noqa: BLE001 - a bad builder must not end the crawl
                logger.opt(exception=True).debug("Discovery: url_builder failed")
                url = ""

        collected.append(
            build_job(
                company_name=company_name,
                title=title,
                job_url=url,
                location=_first(obj, _LOCATION_KEYS),
                country=_first(obj, _COUNTRY_KEYS),
                career_page_url=career_page_url or page_url,
                platform=platform,
            )
        )

    return collected


def jobs_from_state(
    markup: str,
    page_url: str,
    company_name: str,
    platform: str,
    career_page_url: str = "",
    soup: Optional[BeautifulSoup] = None,
    url_builder: Optional[Any] = None,
) -> List[Job]:
    """Extract postings from a page's embedded JavaScript state.

    Tries each embedded payload, largest first, and returns the first one that
    yields postings. Never raises.

    Args:
        markup: Raw HTML.
        page_url: URL the markup came from.
        company_name: Company as named in the input sheet.
        platform: Label for the ``Platform`` column.
        career_page_url: Board URL recorded on each job.
        soup: Pre-parsed document, to avoid parsing twice.
        url_builder: Passed through to :func:`jobs_from_payload`.

    Returns:
        The postings found, deduplicated. Empty when the page embeds no usable
        state — which is the common case and not an error.
    """
    from utils.jobs import dedupe  # local: utils.jobs imports nothing from here

    try:
        for payload in embedded_json(markup, soup=soup):
            jobs = dedupe(
                jobs_from_payload(
                    payload,
                    page_url,
                    company_name,
                    platform,
                    career_page_url=career_page_url,
                    url_builder=url_builder,
                )
            )
            if jobs:
                logger.debug("Discovery: {} job(s) from embedded state on {}", len(jobs), page_url)
                return jobs
    except Exception:  # noqa: BLE001 - discovery is best effort
        logger.opt(exception=True).debug("Discovery: state extraction failed on {}", page_url)

    return []


def script_endpoints(markup: str, page_url: str, limit: int = 40) -> Tuple[List[str], List[str]]:
    """List the JavaScript bundles and API paths a page references.

    Used only for the unknown-platform diagnostics: when a board cannot be
    read, the bundle URLs and the API-looking paths inside them are what a
    human needs to write the next adapter.

    Args:
        markup: Raw HTML.
        page_url: URL the markup came from, for resolving relative links.
        limit: Ceiling on each list.

    Returns:
        ``(bundle_urls, endpoint_paths)``, both deduplicated and sorted.
    """
    if not markup:
        return [], []

    soup = parse_html(markup)

    bundles = {
        absolute_url(page_url, str(script.get("src") or ""))
        for script in soup.find_all("script", src=True)
    }
    bundles = {
        url
        for url in bundles
        if url and any(suffix in url.lower() for suffix in _BUNDLE_SUFFIXES)
    }

    endpoints = {
        match.group(1)
        for match in _ENDPOINT.finditer(markup)
        if 4 <= len(match.group(1)) <= 200
    }

    return sorted(bundles)[:limit], sorted(endpoints)[:limit]
