"""Find the search controls a job board offers, and read their options.

Most boards let a visitor narrow the list — by department, by location, by
employment type — and those controls are worth reading for two reasons. They say
what the company organises its hiring around, which is information the crawler
otherwise has no way to learn. And where a technology department exists, its
filtered URL is a far cheaper way to reach the relevant postings than paging the
whole board::

    >>> from crawler.job_filters import detect_filters, technology_options
    >>> found = detect_filters(markup, "https://boards.greenhouse.io/acme")
    >>> [(f.label, f.filter_type.value, len(f.options)) for f in found.filters]
    [('Department', 'department', 7), ('Office', 'location', 4)]
    >>> [option.label for option in technology_options(found)]
    ['Information Technology', 'Software Engineering']

**Nothing here is vendor-specific.** There is no Workday branch and no
Greenhouse branch. Boards differ in markup, not in kind: they all express a
filter as a ``<select>``, a group of checkboxes, a set of links carrying a query
parameter, a row of tabs, or a facet list in the JSON they ship to their own
JavaScript. Six readers cover those shapes, and a vendor the crawler has never
seen is read by whichever one fits.

**A label is evidence; a type is an inference.** ``"Category"`` is recorded as
:attr:`FilterType.CATEGORY`, not as a department — plenty of boards use it for
job family, seniority, or something idiosyncratic. Only wording that actually
says department maps to :attr:`FilterType.DEPARTMENT`. Every filter keeps the
label exactly as published alongside whatever type was inferred, and carries a
confidence so a caller can decide how much to trust it.

**Relevance is decided by the existing classifier, never by a new word list.**
:func:`technology_options` asks :func:`crawler.tech_filter.is_tech_job` about
each option, which means ``"Information Technology"`` qualifies and
``"Engineering"`` does not — the latter being a generic role with no technical
qualifier, and the reason a manufacturer's engineering department must not be
mistaken for a software one.

**Bounded by construction.** Every list is capped, only a handful of filtered
URLs are ever produced, and combinations of filters are not generated at all:
one filter at a time, because the cross-product of a department list and a
location list is how a crawler ends up making four hundred requests to one
company.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Final, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from loguru import logger

from crawler.tech_filter import is_tech_job
from utils.html import absolute_url, clean_text, parse_html

__all__ = [
    "MAX_FILTERED_URLS",
    "MAX_FILTERS",
    "MAX_OPTIONS",
    "DetectionMethod",
    "FilterOption",
    "FilterSet",
    "FilterType",
    "JobFilter",
    "classify_label",
    "detect_filters",
    "detect_filters_from_payloads",
    "detect_filters_rendered",
    "technology_options",
    "technology_urls",
]

# ---------------------------------------------------------------------------
# Limits. Every one of these exists to stop a single company consuming a run.
# ---------------------------------------------------------------------------

#: Filters kept from one page. A board offering more than this is faceted
#: search rather than a job list, and reading all of it is not the point.
MAX_FILTERS: Final[int] = 12

#: Options kept per filter. Location lists run to thousands on large boards.
MAX_OPTIONS: Final[int] = 300

#: Filtered URLs produced for one company, across all filters. This is the
#: number that bounds extra requests, and it is deliberately small.
MAX_FILTERED_URLS: Final[int] = 5

#: Filters that may contribute URLs in one pass. One, always: combining a
#: department list with a location list multiplies requests without adding
#: postings that the unfiltered board would not also list.
MAX_FILTERS_COMBINED: Final[int] = 1

#: Options examined when looking for technology-relevant ones. Guards against a
#: pathological page that declares ten thousand options.
_MAX_OPTIONS_SCANNED: Final[int] = 1000

#: Anchors read when hunting for filter links.
_MAX_LINKS: Final[int] = 400


class FilterType(str, Enum):
    """What a filter appears to narrow by.

    The value is the label written to a report, so the enum can be used
    directly wherever a string is expected.
    """

    DEPARTMENT = "department"
    CATEGORY = "category"
    JOB_FAMILY = "job family"
    LOCATION = "location"
    COUNTRY = "country"
    STATE = "state"
    CITY = "city"
    WORKPLACE_TYPE = "workplace type"
    EMPLOYMENT_TYPE = "employment type"
    EXPERIENCE_LEVEL = "experience level"
    JOB_TYPE = "job type"
    KEYWORD = "keyword"

    #: Recognised as a filter, but its label matches nothing known. The label
    #: is still preserved; only the type is unknown.
    UNKNOWN = "unknown"


class DetectionMethod(str, Enum):
    """How a filter was found. Recorded so a low-confidence read is traceable."""

    SELECT = "select"
    CHECKBOX_GROUP = "checkbox group"
    LINK_PARAMETER = "link parameter"
    TAB = "tab"
    SEARCH_INPUT = "search input"
    EMBEDDED_JSON = "embedded json"
    RENDERED_DOM = "rendered dom"


#: Label wording that identifies a filter's type, and how much to trust it.
#: Ordered: the first entry whose phrase appears in the label wins, so the more
#: specific phrases come first.
#:
#: ``"category"`` maps to its own type rather than to department, and that is
#: the single most important line in this table. Boards use "Category" for job
#: family, for seniority, and for things with no equivalent elsewhere; treating
#: it as a department would put a confident wrong answer in the sheet.
_LABEL_TYPES: Final[Tuple[Tuple[str, FilterType, float], ...]] = (
    # Organisational unit.
    ("department", FilterType.DEPARTMENT, 1.0),
    ("dept", FilterType.DEPARTMENT, 0.9),
    ("division", FilterType.DEPARTMENT, 0.8),
    ("business unit", FilterType.DEPARTMENT, 0.8),
    ("business area", FilterType.DEPARTMENT, 0.8),
    ("function", FilterType.DEPARTMENT, 0.7),
    ("team", FilterType.DEPARTMENT, 0.6),
    ("job family", FilterType.JOB_FAMILY, 1.0),
    ("job field", FilterType.JOB_FAMILY, 0.9),
    ("family", FilterType.JOB_FAMILY, 0.7),
    ("discipline", FilterType.JOB_FAMILY, 0.7),
    ("practice", FilterType.JOB_FAMILY, 0.6),
    # Deliberately its own type. See the note above.
    ("job category", FilterType.CATEGORY, 1.0),
    ("category", FilterType.CATEGORY, 0.9),
    ("categories", FilterType.CATEGORY, 0.9),
    # Place. The narrow ones first, so "city" is not swallowed by "location".
    ("country", FilterType.COUNTRY, 1.0),
    ("state", FilterType.STATE, 0.9),
    ("province", FilterType.STATE, 0.9),
    ("region", FilterType.STATE, 0.6),
    ("city", FilterType.CITY, 0.9),
    ("location", FilterType.LOCATION, 1.0),
    ("office", FilterType.LOCATION, 0.8),
    ("site", FilterType.LOCATION, 0.6),
    ("where", FilterType.LOCATION, 0.5),
    # Working arrangement.
    ("remote", FilterType.WORKPLACE_TYPE, 0.9),
    ("workplace", FilterType.WORKPLACE_TYPE, 1.0),
    ("work type", FilterType.WORKPLACE_TYPE, 0.8),
    ("work model", FilterType.WORKPLACE_TYPE, 0.9),
    ("work arrangement", FilterType.WORKPLACE_TYPE, 0.9),
    ("hybrid", FilterType.WORKPLACE_TYPE, 0.8),
    ("onsite", FilterType.WORKPLACE_TYPE, 0.8),
    # Contract shape.
    ("employment type", FilterType.EMPLOYMENT_TYPE, 1.0),
    ("employment", FilterType.EMPLOYMENT_TYPE, 0.8),
    ("contract type", FilterType.EMPLOYMENT_TYPE, 0.9),
    ("schedule", FilterType.EMPLOYMENT_TYPE, 0.7),
    ("full time", FilterType.EMPLOYMENT_TYPE, 0.7),
    ("part time", FilterType.EMPLOYMENT_TYPE, 0.7),
    # Seniority.
    ("experience level", FilterType.EXPERIENCE_LEVEL, 1.0),
    ("experience", FilterType.EXPERIENCE_LEVEL, 0.8),
    ("seniority", FilterType.EXPERIENCE_LEVEL, 1.0),
    ("career level", FilterType.EXPERIENCE_LEVEL, 0.9),
    ("level", FilterType.EXPERIENCE_LEVEL, 0.6),
    # Kind of posting. Checked late: "job type" would otherwise capture labels
    # that are really about employment or category.
    ("job type", FilterType.JOB_TYPE, 0.9),
    ("position type", FilterType.JOB_TYPE, 0.8),
    ("opportunity type", FilterType.JOB_TYPE, 0.8),
    # Free-text search.
    ("keyword", FilterType.KEYWORD, 1.0),
    ("search", FilterType.KEYWORD, 0.8),
    ("job title", FilterType.KEYWORD, 0.7),
    ("query", FilterType.KEYWORD, 0.7),
)

#: Query parameters that mean a link is a filtered view. The value is the type
#: the parameter implies, and how much to trust it.
_PARAMETER_TYPES: Final[Tuple[Tuple[str, FilterType, float], ...]] = (
    ("department", FilterType.DEPARTMENT, 1.0),
    ("departmentid", FilterType.DEPARTMENT, 0.9),
    ("dept", FilterType.DEPARTMENT, 0.9),
    ("division", FilterType.DEPARTMENT, 0.8),
    ("function", FilterType.DEPARTMENT, 0.7),
    ("team", FilterType.DEPARTMENT, 0.6),
    ("jobfamily", FilterType.JOB_FAMILY, 1.0),
    ("family", FilterType.JOB_FAMILY, 0.7),
    ("category", FilterType.CATEGORY, 0.9),
    ("categories", FilterType.CATEGORY, 0.9),
    ("jobcategory", FilterType.CATEGORY, 1.0),
    ("cat", FilterType.CATEGORY, 0.6),
    ("country", FilterType.COUNTRY, 1.0),
    ("state", FilterType.STATE, 0.9),
    ("city", FilterType.CITY, 0.9),
    ("location", FilterType.LOCATION, 1.0),
    ("locationid", FilterType.LOCATION, 0.9),
    ("office", FilterType.LOCATION, 0.8),
    ("loc", FilterType.LOCATION, 0.7),
    ("remote", FilterType.WORKPLACE_TYPE, 0.8),
    ("workertype", FilterType.EMPLOYMENT_TYPE, 0.8),
    ("employmenttype", FilterType.EMPLOYMENT_TYPE, 1.0),
    ("jobtype", FilterType.JOB_TYPE, 0.9),
    ("level", FilterType.EXPERIENCE_LEVEL, 0.6),
    ("seniority", FilterType.EXPERIENCE_LEVEL, 1.0),
    ("keyword", FilterType.KEYWORD, 1.0),
    ("keywords", FilterType.KEYWORD, 1.0),
    ("q", FilterType.KEYWORD, 0.7),
    ("search", FilterType.KEYWORD, 0.8),
)

#: Keys in a page's own JSON that hold a facet list.
_JSON_FACET_KEYS: Final[Tuple[str, ...]] = (
    "facets", "filters", "refinements", "aggregations", "departments",
    "categories", "locations", "jobfamilies", "jobfamilygroups", "offices",
    "teams", "divisions", "employmenttypes", "worktypes",
)

#: Option labels that mean "no filter applied". Kept out of the option list so
#: a caller cannot mistake the placeholder for a real choice.
_PLACEHOLDER_OPTIONS: Final[frozenset] = frozenset(
    {
        "", "-", "--", "all", "any", "none", "select", "choose", "please select",
        "select one", "all departments", "all categories", "all locations",
        "all offices", "all teams", "all types", "show all", "any location",
        "any department", "all", "-- select --", "select...", "all job families",
    }
)

#: Anything that is not a letter or a digit, for comparing labels.
_NON_ALNUM: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: Confidence given to a filter whose label matches nothing known.
_UNKNOWN_CONFIDENCE: Final[float] = 0.3


def _fold(text: object) -> str:
    """Reduce a label to a comparable form.

    Args:
        text: A label, parameter name, or option.

    Returns:
        The text lowercased with punctuation collapsed to single spaces.
    """
    return _NON_ALNUM.sub(" ", str(text or "").strip().lower()).strip()


def classify_label(label: str) -> Tuple[FilterType, float]:
    """Infer what a filter narrows by, from the words it is labelled with.

    Args:
        label: The control's label, exactly as the board publishes it.

    Returns:
        ``(filter_type, confidence)``. An unrecognised label yields
        :attr:`FilterType.UNKNOWN` with low confidence rather than a guess —
        the label itself is preserved by the caller and remains the evidence.
    """
    folded = _fold(label)
    if not folded:
        return FilterType.UNKNOWN, 0.0

    for phrase, filter_type, confidence in _LABEL_TYPES:
        if phrase in folded:
            return filter_type, confidence

    return FilterType.UNKNOWN, _UNKNOWN_CONFIDENCE


def _classify_parameter(name: str) -> Tuple[FilterType, float]:
    """Infer a filter's type from the query parameter that carries it.

    Args:
        name: The parameter name.

    Returns:
        ``(filter_type, confidence)``, or ``UNKNOWN`` when it means nothing.
    """
    folded = _fold(name).replace(" ", "")
    if not folded:
        return FilterType.UNKNOWN, 0.0

    for candidate, filter_type, confidence in _PARAMETER_TYPES:
        if folded == candidate:
            return filter_type, confidence

    for candidate, filter_type, confidence in _PARAMETER_TYPES:
        if len(candidate) > 3 and candidate in folded:
            return filter_type, confidence * 0.8

    return FilterType.UNKNOWN, 0.0


@dataclass(frozen=True)
class FilterOption:
    """One choice a filter offers.

    Attributes:
        label: What the board calls it, verbatim.
        value: The value submitted when it is chosen, when one is published.
        url: A URL that applies this option, when the board expresses the
            filter as a link. Empty when it does not — which is what stops the
            crawler inventing filtered URLs it cannot know are valid.
        count: How many postings the board says match, when it says.
    """

    label: str
    value: str = ""
    url: str = ""
    count: Optional[int] = None

    @property
    def is_placeholder(self) -> bool:
        """Whether this is a "no filter" entry rather than a real choice.

        Returns:
            ``True`` for "All departments" and its many spellings.
        """
        return _fold(self.label) in _PLACEHOLDER_OPTIONS or not self.label.strip()


@dataclass
class JobFilter:
    """One search control on a board.

    Attributes:
        label: The control's label, exactly as published. Always kept, even
            when the type could not be inferred.
        filter_type: What it appears to narrow by.
        options: The choices it offers, placeholders removed.
        method: How it was found.
        confidence: How much the type inference is worth, ``0``–``1``.
        parameter: The query parameter it submits, when known.
    """

    label: str
    filter_type: FilterType = FilterType.UNKNOWN
    options: List[FilterOption] = field(default_factory=list)
    method: DetectionMethod = DetectionMethod.SELECT
    confidence: float = 0.0
    parameter: str = ""

    @property
    def value_labels(self) -> List[str]:
        """The option labels, for reporting.

        Returns:
            Every option's label, in order.
        """
        return [option.label for option in self.options]

    @property
    def can_build_urls(self) -> bool:
        """Whether a filtered URL can be produced for this control.

        Returns:
            ``True`` when the options carry URLs, or when the parameter name is
            known so one can be appended to the board's own URL. A control that
            offers neither is reported but never crawled, because guessing at
            how a board submits its form is how a crawler ends up requesting
            pages that do not exist.
        """
        return bool(self.parameter) or any(option.url for option in self.options)


@dataclass
class FilterSet:
    """Every filter found on one board.

    Attributes:
        filters: The controls found.
        source_url: The page they were read from.
        methods: Which readers contributed.
        blocked: Set when the page was a challenge or an error rather than a
            board, so a caller can tell "no filters" from "never saw the page".
    """

    filters: List[JobFilter] = field(default_factory=list)
    source_url: str = ""
    methods: List[DetectionMethod] = field(default_factory=list)
    blocked: str = ""

    def __bool__(self) -> bool:
        """Whether anything was found."""
        return bool(self.filters)

    @property
    def count(self) -> int:
        """How many filters were found.

        Returns:
            The count.
        """
        return len(self.filters)

    @property
    def types(self) -> List[str]:
        """The distinct filter types found, in order of appearance.

        Returns:
            Their labels.
        """
        seen: List[str] = []
        for item in self.filters:
            if item.filter_type.value not in seen:
                seen.append(item.filter_type.value)
        return seen

    @property
    def confidence(self) -> float:
        """How much the set as a whole is worth.

        Returns:
            The mean confidence of its filters, ``0`` when there are none.
        """
        if not self.filters:
            return 0.0
        return sum(item.confidence for item in self.filters) / len(self.filters)

    def by_type(self, *wanted: FilterType) -> List[JobFilter]:
        """The filters of given types, highest confidence first.

        Args:
            *wanted: Types to return.

        Returns:
            The matching filters.
        """
        chosen = [item for item in self.filters if item.filter_type in wanted]
        return sorted(chosen, key=lambda item: -item.confidence)

    def summary(self) -> Dict[str, object]:
        """Render as the metadata a report or a sheet cell would hold.

        Returns:
            A flat mapping: counts, types, labels and the method used.
        """
        return {
            "filters_detected": self.count > 0,
            "filter_count": self.count,
            "filter_types": ", ".join(self.types),
            "filter_labels": ", ".join(item.label for item in self.filters),
            "filter_values": "; ".join(
                f"{item.label}: {', '.join(item.value_labels[:8])}"
                for item in self.filters
                if item.options
            ),
            "filter_detection_method": ", ".join(
                sorted({method.value for method in self.methods})
            ),
            "filter_confidence": round(self.confidence, 2),
            "blocked": self.blocked,
        }


# ---------------------------------------------------------------------------
# Readers. One per shape a board can express a filter in.
# ---------------------------------------------------------------------------


def _label_for(element: Any, soup: Any) -> str:
    """Work out what a control is called.

    Boards label a control in five different ways and rarely more than one at a
    time, so all five are tried.

    Args:
        element: The control.
        soup: The parsed document, for finding a ``<label for=...>``.

    Returns:
        The label, or ``""``.
    """
    for attribute in ("aria-label", "data-label", "title", "placeholder"):
        value = clean_text(element.get(attribute))
        if value:
            return value

    identifier = element.get("id")
    if identifier:
        tag = soup.find("label", attrs={"for": identifier})
        if tag is not None:
            text = clean_text(tag.get_text(" "))
            if text:
                return text

    parent_label = element.find_parent("label")
    if parent_label is not None:
        text = clean_text(parent_label.get_text(" "))
        if text:
            return text

    # A legend describes the fieldset a group of inputs sits in.
    fieldset = element.find_parent("fieldset")
    if fieldset is not None:
        legend = fieldset.find("legend")
        if legend is not None:
            text = clean_text(legend.get_text(" "))
            if text:
                return text

    return clean_text(element.get("name")) or clean_text(element.get("id"))


def _form_action(element: Any, page_url: str) -> str:
    """The URL a control's form submits to.

    Args:
        element: The control.
        page_url: The page it is on, for resolving a relative action.

    Returns:
        The absolute action URL, or the page URL when the form names none.
    """
    form = element.find_parent("form")
    if form is None:
        return ""

    method = str(form.get("method") or "get").strip().lower()
    if method != "get":
        # A POST form cannot be turned into a URL, so no filtered URL is built.
        return ""

    return absolute_url(page_url, form.get("action")) or page_url


def _read_selects(soup: Any, page_url: str) -> List[JobFilter]:
    """Read ``<select>`` controls.

    Args:
        soup: The parsed document.
        page_url: The page's URL.

    Returns:
        One filter per select that offers real choices.
    """
    found: List[JobFilter] = []

    for element in soup.find_all("select")[:MAX_FILTERS * 2]:
        label = _label_for(element, soup)
        parameter = clean_text(element.get("name"))
        action = _form_action(element, page_url)

        options: List[FilterOption] = []
        for tag in element.find_all("option")[:MAX_OPTIONS]:
            text = clean_text(tag.get_text(" "))
            value = clean_text(tag.get("value"))
            option = FilterOption(
                label=text or value,
                value=value or text,
                url=(
                    _with_parameter(action, parameter, value or text)
                    if action and parameter
                    else ""
                ),
            )
            if not option.is_placeholder:
                options.append(option)

        if not options:
            continue

        filter_type, confidence = classify_label(label)
        if filter_type is FilterType.UNKNOWN and parameter:
            # The label said nothing; the parameter name may still.
            from_parameter, parameter_confidence = _classify_parameter(parameter)
            if from_parameter is not FilterType.UNKNOWN:
                filter_type, confidence = from_parameter, parameter_confidence

        found.append(
            JobFilter(
                label=label or parameter or "Filter",
                filter_type=filter_type,
                options=options,
                method=DetectionMethod.SELECT,
                confidence=confidence,
                parameter=parameter if action else "",
            )
        )

    return found


def _read_checkbox_groups(soup: Any, page_url: str) -> List[JobFilter]:
    """Read groups of checkboxes or radios that share a name.

    Args:
        soup: The parsed document.
        page_url: The page's URL.

    Returns:
        One filter per group offering more than one choice.
    """
    groups: Dict[str, List[Any]] = {}

    for element in soup.find_all("input"):
        kind = str(element.get("type") or "").strip().lower()
        if kind not in ("checkbox", "radio"):
            continue
        name = clean_text(element.get("name"))
        if name:
            groups.setdefault(name, []).append(element)

    found: List[JobFilter] = []

    for name, elements in list(groups.items())[:MAX_FILTERS * 2]:
        if len(elements) < 2:
            # A lone checkbox is a preference, not a filter with options.
            continue

        options: List[FilterOption] = []
        for element in elements[:MAX_OPTIONS]:
            label = _label_for(element, soup)
            value = clean_text(element.get("value"))
            option = FilterOption(label=label or value, value=value or label)
            if not option.is_placeholder:
                options.append(option)

        if not options:
            continue

        # The group's own label is the fieldset legend, not any one input's.
        legend = ""
        fieldset = elements[0].find_parent("fieldset")
        if fieldset is not None:
            tag = fieldset.find("legend")
            if tag is not None:
                legend = clean_text(tag.get_text(" "))

        label = legend or name
        filter_type, confidence = classify_label(label)
        if filter_type is FilterType.UNKNOWN:
            from_parameter, parameter_confidence = _classify_parameter(name)
            if from_parameter is not FilterType.UNKNOWN:
                filter_type, confidence = from_parameter, parameter_confidence

        action = _form_action(elements[0], page_url)
        found.append(
            JobFilter(
                label=label,
                filter_type=filter_type,
                options=options,
                method=DetectionMethod.CHECKBOX_GROUP,
                confidence=confidence,
                parameter=name if action else "",
            )
        )

    return found


def _read_link_parameters(soup: Any, page_url: str) -> List[JobFilter]:
    """Read filters expressed as links carrying a query parameter.

    The commonest shape on server-rendered boards: a sidebar of links, each
    applying one department or location.

    Args:
        soup: The parsed document.
        page_url: The page's URL.

    Returns:
        One filter per parameter that more than one link varies.
    """
    by_parameter: Dict[str, Dict[str, FilterOption]] = {}

    for anchor in soup.find_all("a", href=True)[:_MAX_LINKS]:
        target = absolute_url(page_url, anchor["href"])
        if not target:
            continue

        try:
            query = urlsplit(target).query
        except ValueError:
            continue
        if not query:
            continue

        text = clean_text(anchor.get_text(" "))

        for name, value in parse_qsl(query, keep_blank_values=False):
            filter_type, _ = _classify_parameter(name)
            if filter_type is FilterType.UNKNOWN:
                continue

            option = FilterOption(label=text or value, value=value, url=target)
            if option.is_placeholder:
                continue

            # Keyed on value, so a board linking one department from several
            # places contributes it once.
            by_parameter.setdefault(name, {}).setdefault(value, option)

    found: List[JobFilter] = []

    for name, options in by_parameter.items():
        if len(options) < 2:
            # One link is not a filter; it is a link.
            continue

        filter_type, confidence = _classify_parameter(name)
        found.append(
            JobFilter(
                label=name,
                filter_type=filter_type,
                options=list(options.values())[:MAX_OPTIONS],
                method=DetectionMethod.LINK_PARAMETER,
                # A parameter is weaker evidence than a written label.
                confidence=confidence * 0.9,
                parameter=name,
            )
        )

    return found


def _read_tabs(soup: Any, page_url: str) -> List[JobFilter]:
    """Read a row of tabs or filter buttons.

    Args:
        soup: The parsed document.
        page_url: The page's URL.

    Returns:
        At most one filter, since a board rarely has two tab strips.
    """
    tabs = soup.find_all(attrs={"role": "tab"})
    if len(tabs) < 2:
        return []

    options: List[FilterOption] = []
    for tab in tabs[:MAX_OPTIONS]:
        label = clean_text(tab.get_text(" ")) or clean_text(tab.get("aria-label"))
        target = absolute_url(page_url, tab.get("href")) if tab.get("href") else ""
        option = FilterOption(label=label, value=clean_text(tab.get("data-value")) or label, url=target)
        if not option.is_placeholder:
            options.append(option)

    if len(options) < 2:
        return []

    group = soup.find(attrs={"role": "tablist"})
    label = ""
    if group is not None:
        label = clean_text(group.get("aria-label")) or clean_text(group.get("title"))

    filter_type, confidence = classify_label(label) if label else (FilterType.UNKNOWN, _UNKNOWN_CONFIDENCE)

    return [
        JobFilter(
            label=label or "Tabs",
            filter_type=filter_type,
            options=options,
            method=DetectionMethod.TAB,
            confidence=confidence,
        )
    ]


def _read_search_inputs(soup: Any, page_url: str) -> List[JobFilter]:
    """Read free-text search boxes.

    These have no options; they are reported so a caller knows the board offers
    keyword search, which is a fact about the board worth recording.

    Args:
        soup: The parsed document.
        page_url: The page's URL.

    Returns:
        At most one keyword filter.
    """
    for element in soup.find_all("input"):
        kind = str(element.get("type") or "text").strip().lower()
        if kind not in ("text", "search", ""):
            continue

        label = _label_for(element, soup)
        name = clean_text(element.get("name"))

        filter_type, confidence = classify_label(label)
        if filter_type is not FilterType.KEYWORD:
            from_parameter, parameter_confidence = _classify_parameter(name)
            if from_parameter is not FilterType.KEYWORD:
                continue
            confidence = parameter_confidence

        action = _form_action(element, page_url)
        return [
            JobFilter(
                label=label or name or "Search",
                filter_type=FilterType.KEYWORD,
                options=[],
                method=DetectionMethod.SEARCH_INPUT,
                confidence=confidence,
                parameter=name if action else "",
            )
        ]

    return []


def _facet_options(node: Any) -> List[FilterOption]:
    """Read options out of a facet node in a board's own JSON.

    Args:
        node: A list or mapping from the page's JSON.

    Returns:
        The options it describes.
    """
    entries = node if isinstance(node, list) else [node]
    options: List[FilterOption] = []

    for entry in entries[:MAX_OPTIONS]:
        if isinstance(entry, str):
            option = FilterOption(label=entry, value=entry)
        elif isinstance(entry, dict):
            lowered = {str(key).lower(): value for key, value in entry.items()}
            label = ""
            for key in ("label", "name", "title", "displayname", "descriptor", "text", "value"):
                candidate = lowered.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    label = candidate.strip()
                    break
            if not label:
                continue

            value = lowered.get("id") or lowered.get("value") or label
            count = lowered.get("count")
            option = FilterOption(
                label=label,
                value=str(value),
                count=int(count) if isinstance(count, (int, float)) else None,
            )
        else:
            continue

        if not option.is_placeholder:
            options.append(option)

    return options


def detect_filters_from_payloads(payloads: Sequence[Any], source_url: str = "") -> List[JobFilter]:
    """Read facets out of the JSON a board ships to its own JavaScript.

    Single-page boards — Workday and Eightfold among them — render their filters
    client-side, so the markup has no ``<select>`` at all while the payload that
    built it names every facet. :func:`utils.discovery.embedded_json` already
    decodes those payloads for job extraction; this reads the other half.

    Args:
        payloads: Decoded JSON objects from the page.
        source_url: The page they came from, for logging.

    Returns:
        The filters found.
    """
    found: List[JobFilter] = []
    seen_labels: set = set()

    def walk(node: Any, depth: int = 0) -> None:
        """Search a payload for anything shaped like a facet list."""
        if depth > 8 or len(found) >= MAX_FILTERS:
            return

        if isinstance(node, list):
            for item in node[:200]:
                walk(item, depth + 1)
            return

        if not isinstance(node, dict):
            return

        for key, value in node.items():
            folded = _fold(key).replace(" ", "")

            if folded in _JSON_FACET_KEYS and isinstance(value, (list, dict)):
                # A facet container holds named groups; a facet list holds
                # options directly. Both shapes appear in the wild.
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    inner = {str(k).lower() for k in value[0]}
                    if inner & {"values", "options", "buckets", "children", "facetvalues"}:
                        for group in value[:MAX_FILTERS]:
                            lowered = {str(k).lower(): v for k, v in group.items()}
                            label = str(
                                lowered.get("label")
                                or lowered.get("name")
                                or lowered.get("descriptor")
                                or key
                            )
                            for inner_key in ("values", "options", "buckets", "children", "facetvalues"):
                                if inner_key in lowered:
                                    options = _facet_options(lowered[inner_key])
                                    if options and label not in seen_labels:
                                        seen_labels.add(label)
                                        filter_type, confidence = classify_label(label)
                                        found.append(
                                            JobFilter(
                                                label=label,
                                                filter_type=filter_type,
                                                options=options,
                                                method=DetectionMethod.EMBEDDED_JSON,
                                                confidence=confidence,
                                            )
                                        )
                                    break
                        continue

                options = _facet_options(value)
                if options and key not in seen_labels:
                    seen_labels.add(key)
                    filter_type, confidence = classify_label(key)
                    found.append(
                        JobFilter(
                            label=str(key),
                            filter_type=filter_type,
                            options=options,
                            method=DetectionMethod.EMBEDDED_JSON,
                            confidence=confidence,
                        )
                    )
                    continue

                # A container rather than a list: {"filters": {"departments":
                # [...], "locations": [...]}}. Matching the outer key and then
                # stopping would find nothing at all, so descend into it.
                if isinstance(value, dict):
                    walk(value, depth + 1)
                continue

            walk(value, depth + 1)

    for payload in payloads[:40]:
        walk(payload)

    if found:
        logger.debug("Read {} filter(s) from the JSON on {}", len(found), source_url)

    return found


def detect_filters(
    markup: str,
    page_url: str = "",
    payloads: Optional[Sequence[Any]] = None,
    include_embedded_json: bool = True,
) -> FilterSet:
    """Find every search control a board's page offers.

    Args:
        markup: The page's HTML — served, or rendered by the browser.
        page_url: Its URL, for resolving relative links.
        payloads: JSON the page shipped, when a caller already has it. The
            browser fallback collects these, and reusing them costs nothing.
        include_embedded_json: Whether to decode the page's inline JSON when
            no payloads were supplied. Worth it for a single-page board and
            wasted work for a server-rendered one, so it stops as soon as the
            markup itself has yielded filters.

    Returns:
        Everything found, capped at :data:`MAX_FILTERS`. Never raises: a page
        that cannot be parsed yields an empty set.
    """
    result = FilterSet(source_url=page_url)

    if not markup or not markup.strip():
        return result

    try:
        soup = parse_html(markup)
    except Exception:  # noqa: BLE001 - malformed markup is not fatal
        logger.opt(exception=True).debug("Could not parse {} for filters", page_url)
        return result

    collected: List[JobFilter] = []
    for reader in (_read_selects, _read_checkbox_groups, _read_link_parameters, _read_tabs):
        try:
            collected.extend(reader(soup, page_url))
        except Exception:  # noqa: BLE001 - one reader must not stop the rest
            logger.opt(exception=True).debug("Filter reader {} failed", reader.__name__)

    try:
        collected.extend(_read_search_inputs(soup, page_url))
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).debug("Search-input reader failed")

    # The page's own JSON, for boards that render their filters client-side.
    if payloads:
        collected.extend(detect_filters_from_payloads(payloads, page_url))
    elif include_embedded_json and not collected:
        try:
            from utils.discovery import embedded_json

            collected.extend(detect_filters_from_payloads(embedded_json(markup, soup), page_url))
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).debug("Could not read embedded JSON on {}", page_url)

    result.filters = _deduplicate(collected)[:MAX_FILTERS]
    result.methods = []
    for item in result.filters:
        if item.method not in result.methods:
            result.methods.append(item.method)

    if result.filters:
        logger.info(
            "{}: {} filter(s) detected ({})",
            page_url or "board",
            len(result.filters),
            ", ".join(result.types),
        )

    return result


def _deduplicate(filters: Sequence[JobFilter]) -> List[JobFilter]:
    """Drop filters that repeat one already found.

    A board commonly renders the same control twice — once for wide screens and
    once for narrow — and a select inside a GET form is also a set of links.

    Args:
        filters: Everything the readers produced.

    Returns:
        The distinct filters, richest first within each identity.
    """
    ranked = sorted(
        filters,
        key=lambda item: (-len(item.options), -item.confidence),
    )

    kept: List[JobFilter] = []
    seen: set = set()

    for item in ranked:
        identity = (
            item.filter_type.value,
            _fold(item.label),
            tuple(sorted(_fold(option.label) for option in item.options[:12])),
        )
        # A filter with the same type and the same options is the same filter,
        # whatever it is labelled and however it was found.
        loose = (item.filter_type.value, identity[2])

        if identity in seen or (item.options and loose in seen):
            continue

        seen.add(identity)
        if item.options:
            seen.add(loose)
        kept.append(item)

    return kept


def detect_filters_rendered(
    page_url: str,
    render: Optional[Any] = None,
) -> FilterSet:
    """Read a board's filters after its JavaScript has run.

    Measured against the boards in the reference sheet, this is not an edge
    case but the common one: ADP, UltiPro and Eightfold all serve markup with
    no controls in it at all and build every filter client-side. Static
    detection finds nothing on any of them.

    The browser is the version 2 fallback, unchanged — the same headless
    Chromium that already rescues client-side job boards. It is only worth its
    seconds when the static page has yielded nothing, so callers should try
    :func:`detect_filters` first.

    Args:
        page_url: The board to visit.
        render: Injected renderer, for tests. Defaults to
            :func:`utils.browser.render`.

    Returns:
        What was found. A page the browser could not read yields an empty set
        with :attr:`FilterSet.blocked` set, so a caller can tell "this board
        has no filters" from "this board was never seen".
    """
    result = FilterSet(source_url=page_url)

    if render is None:
        try:
            from utils.browser import render as browser_render

            render = browser_render
        except ImportError:  # pragma: no cover - playwright is optional
            result.blocked = "no browser available"
            return result

    try:
        page = render(page_url)
    except Exception as exc:  # noqa: BLE001 - a render failure is not fatal
        logger.opt(exception=True).debug("Could not render {} for filters", page_url)
        result.blocked = f"render failed: {exc}"[:120]
        return result

    if page is None:
        result.blocked = "browser unavailable"
        return result

    if getattr(page, "error", None):
        result.blocked = str(page.error)[:120]
        return result

    html = getattr(page, "html", "") or ""

    # A challenge renders perfectly well and is not a board. Classifying it
    # keeps a Cloudflare interstitial out of the filter tables, and lets the
    # caller report it as blocked rather than as "no filters found".
    from utils.blocking import Block, classify_response

    block = classify_response(
        getattr(page, "status", 200) or 200,
        getattr(page, "headers", {}) or {},
        html,
    )
    if block is not Block.NONE:
        result.blocked = block.value
        logger.info("{}: {} rather than a board", page_url, block.value)
        return result

    result = detect_filters(
        html,
        getattr(page, "url", "") or page_url,
        payloads=getattr(page, "payloads", None) or None,
    )

    # Whatever reader matched, the evidence came from a rendered DOM, and a
    # caller weighing confidence should know that.
    if result.filters and DetectionMethod.RENDERED_DOM not in result.methods:
        result.methods.append(DetectionMethod.RENDERED_DOM)

    return result


# ---------------------------------------------------------------------------
# Using what was found
# ---------------------------------------------------------------------------

#: Filter types that could plausibly separate technology roles from the rest.
#: Location and employment type cannot, so they are never used for this.
NARROWING_TYPES: Final[Tuple[FilterType, ...]] = (
    FilterType.DEPARTMENT,
    FilterType.JOB_FAMILY,
    FilterType.CATEGORY,
)


def technology_options(
    filters: FilterSet,
    extra_keywords: Iterable[str] = (),
) -> List[FilterOption]:
    """Pick the options that name technology work.

    The decision is delegated entirely to :func:`crawler.tech_filter.is_tech_job`,
    which is the same classifier that decides whether a posting belongs in
    ``CURRENT_JOBS``. That matters for one case in particular: a department
    called ``"Engineering"`` is **not** selected, because at a manufacturer it
    is a plant discipline, and the classifier already knows that a generic role
    word with no technical qualifier is not a technology role.

    Args:
        filters: What was detected on the board.
        extra_keywords: Additional phrases the operator counts as technical.

    Returns:
        The matching options, from the narrowing filters only, in the order
        their filters were found.
    """
    chosen: List[FilterOption] = []
    scanned = 0

    for item in filters.by_type(*NARROWING_TYPES):
        for option in item.options:
            scanned += 1
            if scanned > _MAX_OPTIONS_SCANNED:
                logger.debug("Stopped scanning options at {}", _MAX_OPTIONS_SCANNED)
                return chosen

            if is_tech_job(option.label, extra_keywords=extra_keywords):
                chosen.append(option)

    return chosen


def _with_parameter(url: str, name: str, value: str) -> str:
    """Return ``url`` with one query parameter set.

    Args:
        url: The base URL.
        name: Parameter name.
        value: Parameter value.

    Returns:
        The URL with the parameter applied, or ``""`` when it cannot be built.
    """
    if not url or not name or not value:
        return ""

    try:
        parts = urlsplit(url)
    except ValueError:
        return ""

    if parts.scheme not in ("http", "https"):
        return ""

    query = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True) if key != name]
    query.append((name, value))

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def technology_urls(
    filters: FilterSet,
    base_url: str = "",
    limit: int = MAX_FILTERED_URLS,
    extra_keywords: Iterable[str] = (),
) -> List[str]:
    """Build the filtered URLs worth crawling for technology roles.

    Args:
        filters: What was detected on the board.
        base_url: The board's URL, for filters expressed as a parameter rather
            than as links.
        limit: Ceiling on URLs returned. This is the number that bounds the
            extra requests one company can cost.
        extra_keywords: Additional phrases the operator counts as technical.

    Returns:
        Distinct URLs, at most ``limit`` of them, drawn from **one** filter —
        never a cross-product of two. An option that carries no URL and whose
        filter names no parameter contributes nothing, because guessing how a
        board submits its form produces requests for pages that do not exist.
    """
    candidates = [
        item
        for item in filters.by_type(*NARROWING_TYPES)
        if item.can_build_urls
    ][:MAX_FILTERS_COMBINED]

    urls: List[str] = []
    seen: set = set()

    for item in candidates:
        for option in item.options:
            if len(urls) >= max(0, limit):
                return urls

            if not is_tech_job(option.label, extra_keywords=extra_keywords):
                continue

            target = option.url or (
                _with_parameter(base_url, item.parameter, option.value or option.label)
                if item.parameter and base_url
                else ""
            )

            if not target or target in seen:
                continue

            seen.add(target)
            urls.append(target)

    return urls
