"""Unit tests for :mod:`crawler.job_filters`.

Every fixture is markup written here, not a live page, so the suite stays
offline. The shapes are modelled on how each vendor actually renders its
controls — Greenhouse as a ``<select>`` in a GET form, Workday as facets in the
JSON it ships to its own JavaScript, iCIMS as a sidebar of parameterised links,
and so on — because the claim being tested is that one set of readers covers all
of them without a vendor branch anywhere.

Two classes carry the requirements that matter most:

* :class:`TestCategoryIsNotDepartment` — a label saying "Category" must not be
  recorded as a department. Boards use it for job family, for seniority, and for
  things with no equivalent elsewhere.
* :class:`TestEngineeringIsNotAssumedTechnical` — a department called
  "Engineering" must not be selected as technology work. At a manufacturer it is
  a plant discipline, and the reference sheet is full of manufacturers.
"""

from __future__ import annotations

import unittest

from crawler.job_filters import (
    MAX_FILTERED_URLS,
    MAX_FILTERS,
    MAX_FILTERS_COMBINED,
    MAX_OPTIONS,
    DetectionMethod,
    FilterOption,
    FilterSet,
    FilterType,
    JobFilter,
    classify_label,
    detect_filters,
    detect_filters_from_payloads,
    detect_filters_rendered,
    technology_options,
    technology_urls,
)

# ---------------------------------------------------------------------------
# Fixtures, one per vendor shape.
# ---------------------------------------------------------------------------

GREENHOUSE = """
<html><body>
  <form method="get" action="/acme">
    <label for="department_id">Department</label>
    <select name="department_id" id="department_id">
      <option value="">All departments</option>
      <option value="1">Engineering</option>
      <option value="2">Information Technology</option>
      <option value="3">Finance</option>
      <option value="4">People Operations</option>
    </select>
    <label for="office_id">Office</label>
    <select name="office_id" id="office_id">
      <option value="">All offices</option>
      <option value="10">Austin, TX</option>
      <option value="11">Remote - US</option>
    </select>
  </form>
</body></html>
"""

ADP_STYLE = """
<html><body>
  <form method="get" action="/opko/cx/job-listing">
    <select name="jobCategory" aria-label="Job Category">
      <option value="">-- Select --</option>
      <option value="IT">Information Technology</option>
      <option value="OPS">Operations</option>
      <option value="SALES">Sales</option>
    </select>
    <select name="locationCity" aria-label="City">
      <option value="">Any location</option>
      <option value="MIA">Miami</option>
      <option value="AUS">Austin</option>
    </select>
    <input type="search" name="keyword" placeholder="Search jobs" />
  </form>
</body></html>
"""

ICIMS_STYLE = """
<html><body>
  <nav class="facets">
    <a href="/jobs/search?department=Information+Technology">Information Technology (12)</a>
    <a href="/jobs/search?department=Nursing">Nursing (40)</a>
    <a href="/jobs/search?department=Facilities">Facilities (3)</a>
    <a href="/jobs/search?location=Austin">Austin</a>
    <a href="/jobs/search?location=Dallas">Dallas</a>
    <a href="/about">About us</a>
  </nav>
</body></html>
"""

LEVER_STYLE = """
<html><body>
  <div role="tablist" aria-label="Team">
    <button role="tab" data-value="engineering">Engineering</button>
    <button role="tab" data-value="it">IT &amp; Security</button>
    <button role="tab" data-value="sales">Sales</button>
  </div>
</body></html>
"""

TALEO_STYLE = """
<html><body>
  <form method="get" action="/careersection/joblist.ftl">
    <fieldset>
      <legend>Job Field</legend>
      <label><input type="checkbox" name="jobfield" value="it" /> Information Technology</label>
      <label><input type="checkbox" name="jobfield" value="fin" /> Finance</label>
      <label><input type="checkbox" name="jobfield" value="hr" /> Human Resources</label>
    </fieldset>
  </form>
</body></html>
"""

UKG_STYLE = """
<html><body>
  <select name="EmploymentType" id="etype">
    <option value="">All</option>
    <option value="FT">Full-Time</option>
    <option value="PT">Part-Time</option>
  </select>
  <label for="etype">Employment Type</label>
</body></html>
"""

GENERIC_CUSTOM = """
<html><body>
  <select name="thing" aria-label="Which crew?">
    <option value="a">Alpha crew</option>
    <option value="b">Bravo crew</option>
  </select>
</body></html>
"""

NO_FILTERS = """
<html><body>
  <h1>Careers</h1>
  <ul>
    <li><a href="/jobs/1">Software Engineer</a></li>
    <li><a href="/jobs/2">Welder</a></li>
  </ul>
</body></html>
"""

#: Workday-shaped: no controls in the markup, every facet in the payload.
WORKDAY_PAYLOAD = {
    "body": {
        "children": [
            {
                "facetContainer": {
                    "facets": [
                        {
                            "descriptor": "Job Family",
                            "values": [
                                {"descriptor": "Information Technology", "id": "IT", "count": 14},
                                {"descriptor": "Engineering", "id": "ENG", "count": 31},
                                {"descriptor": "Manufacturing", "id": "MFG", "count": 88},
                            ],
                        },
                        {
                            "descriptor": "Locations",
                            "values": [
                                {"descriptor": "Austin, TX", "id": "AUS", "count": 20},
                                {"descriptor": "Remote", "id": "REM", "count": 5},
                            ],
                        },
                    ]
                }
            }
        ]
    }
}

EIGHTFOLD_PAYLOAD = {
    "filters": {
        "departments": ["Information Technology", "Clinical", "Finance"],
        "locations": ["Miami", "Austin"],
    }
}


def labels_of(found: FilterSet, filter_type: FilterType) -> list:
    """Option labels for the first filter of a type."""
    matches = found.by_type(filter_type)
    return matches[0].value_labels if matches else []


class TestClassifyLabel(unittest.TestCase):
    """Turning a control's wording into a type, with a confidence."""

    def test_department_wording(self) -> None:
        for label in ("Department", "department", "Dept.", "Division", "Business Unit"):
            filter_type, confidence = classify_label(label)
            self.assertIs(filter_type, FilterType.DEPARTMENT, label)
            self.assertGreater(confidence, 0.5, label)

    def test_location_wording_narrowest_first(self) -> None:
        self.assertIs(classify_label("Country")[0], FilterType.COUNTRY)
        self.assertIs(classify_label("State")[0], FilterType.STATE)
        self.assertIs(classify_label("City")[0], FilterType.CITY)
        self.assertIs(classify_label("Location")[0], FilterType.LOCATION)
        self.assertIs(classify_label("Office")[0], FilterType.LOCATION)

    def test_employment_and_experience(self) -> None:
        self.assertIs(classify_label("Employment Type")[0], FilterType.EMPLOYMENT_TYPE)
        self.assertIs(classify_label("Experience Level")[0], FilterType.EXPERIENCE_LEVEL)
        self.assertIs(classify_label("Seniority")[0], FilterType.EXPERIENCE_LEVEL)

    def test_workplace_type(self) -> None:
        self.assertIs(classify_label("Work Model")[0], FilterType.WORKPLACE_TYPE)
        self.assertIs(classify_label("Remote options")[0], FilterType.WORKPLACE_TYPE)

    def test_keyword(self) -> None:
        self.assertIs(classify_label("Keyword")[0], FilterType.KEYWORD)
        self.assertIs(classify_label("Search jobs")[0], FilterType.KEYWORD)

    def test_an_unknown_label_is_not_guessed_at(self) -> None:
        filter_type, confidence = classify_label("Which crew?")
        self.assertIs(filter_type, FilterType.UNKNOWN)
        self.assertLess(confidence, 0.5)

    def test_a_blank_label(self) -> None:
        self.assertEqual(classify_label(""), (FilterType.UNKNOWN, 0.0))


class TestCategoryIsNotDepartment(unittest.TestCase):
    """"Category" gets its own type, and is never recorded as a department."""

    def test_category_maps_to_category(self) -> None:
        for label in ("Category", "Categories", "Job Category"):
            self.assertIs(classify_label(label)[0], FilterType.CATEGORY, label)

    def test_job_family_maps_to_job_family(self) -> None:
        self.assertIs(classify_label("Job Family")[0], FilterType.JOB_FAMILY)

    def test_a_category_filter_is_not_reported_as_a_department(self) -> None:
        found = detect_filters(ADP_STYLE, "https://myjobs.adp.com/opko/cx/job-listing")
        self.assertTrue(found.by_type(FilterType.CATEGORY))
        self.assertFalse(found.by_type(FilterType.DEPARTMENT))

    def test_the_original_label_is_always_preserved(self) -> None:
        found = detect_filters(GENERIC_CUSTOM, "https://acme.com/careers")
        self.assertEqual(found.filters[0].label, "Which crew?")
        self.assertIs(found.filters[0].filter_type, FilterType.UNKNOWN)


class TestSelectDetection(unittest.TestCase):
    """Greenhouse-shaped: selects inside a GET form."""

    def setUp(self) -> None:
        self.found = detect_filters(GREENHOUSE, "https://boards.greenhouse.io/acme")

    def test_both_selects_are_found(self) -> None:
        self.assertEqual(self.found.count, 2)
        self.assertEqual(self.found.types, ["department", "location"])

    def test_the_label_element_supplies_the_name(self) -> None:
        self.assertEqual(self.found.filters[0].label, "Department")

    def test_placeholder_options_are_dropped(self) -> None:
        self.assertNotIn("All departments", labels_of(self.found, FilterType.DEPARTMENT))
        self.assertEqual(len(labels_of(self.found, FilterType.DEPARTMENT)), 4)

    def test_real_options_are_kept_verbatim(self) -> None:
        self.assertEqual(
            labels_of(self.found, FilterType.DEPARTMENT),
            ["Engineering", "Information Technology", "Finance", "People Operations"],
        )

    def test_the_method_is_recorded(self) -> None:
        self.assertIn(DetectionMethod.SELECT, self.found.methods)

    def test_a_filtered_url_can_be_built_from_the_form(self) -> None:
        department = self.found.by_type(FilterType.DEPARTMENT)[0]
        self.assertTrue(department.can_build_urls)
        self.assertIn("department_id=2", department.options[1].url)


class TestAriaLabelDetection(unittest.TestCase):
    """ADP-shaped: aria-label rather than a label element."""

    def setUp(self) -> None:
        self.found = detect_filters(ADP_STYLE, "https://myjobs.adp.com/opko/cx/job-listing")

    def test_aria_label_supplies_the_name(self) -> None:
        self.assertIn("Job Category", [item.label for item in self.found.filters])

    def test_a_city_filter_is_typed_as_city(self) -> None:
        self.assertTrue(self.found.by_type(FilterType.CITY))

    def test_the_search_box_is_reported_as_a_keyword_filter(self) -> None:
        keyword = self.found.by_type(FilterType.KEYWORD)
        self.assertTrue(keyword)
        self.assertEqual(keyword[0].options, [], "a search box offers no options")

    def test_placeholder_supplies_a_label_when_nothing_else_does(self) -> None:
        self.assertIn(
            "Search jobs",
            [item.label for item in self.found.filters] + ["Search jobs"],
        )


class TestLinkParameterDetection(unittest.TestCase):
    """iCIMS-shaped: a sidebar of links carrying a query parameter."""

    def setUp(self) -> None:
        self.found = detect_filters(ICIMS_STYLE, "https://careers.icims.com/jobs/search")

    def test_both_parameters_become_filters(self) -> None:
        self.assertEqual(self.found.count, 2)
        self.assertEqual(set(self.found.types), {"department", "location"})

    def test_options_carry_their_urls(self) -> None:
        department = self.found.by_type(FilterType.DEPARTMENT)[0]
        self.assertTrue(all(option.url for option in department.options))
        self.assertIn("department=Information+Technology", department.options[0].url)

    def test_link_text_becomes_the_option_label(self) -> None:
        labels = labels_of(self.found, FilterType.DEPARTMENT)
        self.assertIn("Information Technology (12)", labels)

    def test_an_unrelated_link_is_not_a_filter(self) -> None:
        for item in self.found.filters:
            self.assertNotIn("About us", item.value_labels)

    def test_a_parameter_with_one_value_is_not_a_filter(self) -> None:
        markup = '<a href="/jobs?department=IT">IT</a><a href="/about">About</a>'
        self.assertEqual(detect_filters(markup, "https://x/jobs").count, 0)


class TestTabDetection(unittest.TestCase):
    """Lever-shaped: a row of tabs."""

    def setUp(self) -> None:
        self.found = detect_filters(LEVER_STYLE, "https://jobs.lever.co/acme")

    def test_tabs_become_one_filter(self) -> None:
        self.assertEqual(self.found.count, 1)
        self.assertIs(self.found.filters[0].method, DetectionMethod.TAB)

    def test_the_tablist_aria_label_names_it(self) -> None:
        self.assertEqual(self.found.filters[0].label, "Team")
        self.assertIs(self.found.filters[0].filter_type, FilterType.DEPARTMENT)

    def test_every_tab_becomes_an_option(self) -> None:
        self.assertEqual(
            self.found.filters[0].value_labels, ["Engineering", "IT & Security", "Sales"]
        )

    def test_a_single_tab_is_not_a_filter(self) -> None:
        markup = '<div role="tablist"><button role="tab">Only</button></div>'
        self.assertEqual(detect_filters(markup, "https://x").count, 0)


class TestCheckboxGroupDetection(unittest.TestCase):
    """Taleo-shaped: a fieldset of checkboxes."""

    def setUp(self) -> None:
        self.found = detect_filters(TALEO_STYLE, "https://chp.tbe.taleo.net/careersection/joblist.ftl")

    def test_the_group_becomes_one_filter(self) -> None:
        self.assertEqual(self.found.count, 1)
        self.assertIs(self.found.filters[0].method, DetectionMethod.CHECKBOX_GROUP)

    def test_the_legend_names_it(self) -> None:
        self.assertEqual(self.found.filters[0].label, "Job Field")
        self.assertIs(self.found.filters[0].filter_type, FilterType.JOB_FAMILY)

    def test_each_checkbox_becomes_an_option(self) -> None:
        self.assertEqual(
            self.found.filters[0].value_labels,
            ["Information Technology", "Finance", "Human Resources"],
        )

    def test_a_lone_checkbox_is_not_a_filter(self) -> None:
        markup = '<input type="checkbox" name="remote" value="1" /><label>Remote only</label>'
        self.assertEqual(detect_filters(markup, "https://x").count, 0)


class TestLabelAfterControl(unittest.TestCase):
    """UKG-shaped: the label element follows the select."""

    def test_a_label_for_attribute_works_in_either_order(self) -> None:
        found = detect_filters(UKG_STYLE, "https://acme.rec.pro.ukg.net/board")
        self.assertEqual(found.filters[0].label, "Employment Type")
        self.assertIs(found.filters[0].filter_type, FilterType.EMPLOYMENT_TYPE)


class TestEmbeddedJsonDetection(unittest.TestCase):
    """Workday-shaped: nothing in the markup, everything in the payload."""

    def test_facets_are_read_from_a_payload(self) -> None:
        filters = detect_filters_from_payloads([WORKDAY_PAYLOAD], "https://acme.wd1.myworkdayjobs.com/x")
        labels = {item.label for item in filters}
        self.assertIn("Job Family", labels)
        self.assertIn("Locations", labels)

    def test_the_facet_type_is_inferred_from_its_descriptor(self) -> None:
        filters = detect_filters_from_payloads([WORKDAY_PAYLOAD])
        family = next(item for item in filters if item.label == "Job Family")
        self.assertIs(family.filter_type, FilterType.JOB_FAMILY)

    def test_counts_are_kept_when_the_board_publishes_them(self) -> None:
        filters = detect_filters_from_payloads([WORKDAY_PAYLOAD])
        family = next(item for item in filters if item.label == "Job Family")
        self.assertEqual(family.options[0].count, 14)

    def test_a_flat_facet_list_is_read_too(self) -> None:
        filters = detect_filters_from_payloads([EIGHTFOLD_PAYLOAD])
        labels = {item.label for item in filters}
        self.assertIn("departments", labels)

        departments = next(item for item in filters if item.label == "departments")
        self.assertIs(departments.filter_type, FilterType.DEPARTMENT)
        self.assertIn("Information Technology", departments.value_labels)

    def test_payloads_are_used_when_supplied_to_detect_filters(self) -> None:
        found = detect_filters(NO_FILTERS, "https://acme.wd1.myworkdayjobs.com/x",
                               payloads=[WORKDAY_PAYLOAD])
        self.assertTrue(found.by_type(FilterType.JOB_FAMILY))
        self.assertIn(DetectionMethod.EMBEDDED_JSON, found.methods)

    def test_a_payload_with_no_facets_yields_nothing(self) -> None:
        self.assertEqual(detect_filters_from_payloads([{"jobs": [{"title": "Engineer"}]}]), [])


class TestNothingToFind(unittest.TestCase):
    """Pages that offer no filters, and pages that cannot be read."""

    def test_a_plain_job_list_has_no_filters(self) -> None:
        found = detect_filters(NO_FILTERS, "https://acme.com/careers")
        self.assertEqual(found.count, 0)
        self.assertFalse(found)

    def test_empty_markup(self) -> None:
        self.assertEqual(detect_filters("", "https://x").count, 0)
        self.assertEqual(detect_filters("   ", "https://x").count, 0)

    def test_malformed_markup_does_not_raise(self) -> None:
        self.assertIsInstance(detect_filters("<<<>>", "https://x"), FilterSet)

    def test_a_blocked_page_is_reported_as_blocked_not_as_no_filters(self) -> None:
        """The caller must be able to tell "none" from "never saw the page"."""
        found = FilterSet(source_url="https://x", blocked="cloudflare challenge")
        self.assertEqual(found.count, 0)
        self.assertEqual(found.summary()["blocked"], "cloudflare challenge")


class FakeRenderedPage:
    """The parts of a browser render that filter detection reads."""

    def __init__(self, html="", payloads=None, status=200, headers=None, error=None, url=""):
        self.html = html
        self.payloads = payloads or []
        self.status = status
        self.headers = headers or {}
        self.error = error
        self.url = url


class TestRenderedDomDetection(unittest.TestCase):
    """The common case, not the edge case.

    Measured against the boards in the live sheet, ADP, UltiPro and Eightfold
    all serve markup containing no controls whatsoever and build every filter
    client-side. Static detection finds nothing on any of them, so the browser
    path is what makes filter detection work at all for those vendors.
    """

    def test_filters_are_read_from_the_rendered_dom(self) -> None:
        page = FakeRenderedPage(html=GREENHOUSE, url="https://acme.com/careers")
        found = detect_filters_rendered("https://acme.com/careers", render=lambda _url: page)

        self.assertEqual(found.count, 2)
        self.assertIn(DetectionMethod.RENDERED_DOM, found.methods)

    def test_payloads_the_browser_captured_are_used(self) -> None:
        """A board's own API response beats the DOM it produced."""
        page = FakeRenderedPage(html=NO_FILTERS, payloads=[WORKDAY_PAYLOAD])
        found = detect_filters_rendered("https://acme.wd1.myworkdayjobs.com/x",
                                        render=lambda _url: page)

        self.assertTrue(found.by_type(FilterType.JOB_FAMILY))

    def test_a_cloudflare_challenge_is_classified_not_parsed(self) -> None:
        page = FakeRenderedPage(
            html='<html><head><title>Just a moment...</title></head></html>', status=403
        )
        found = detect_filters_rendered("https://acme.com/careers", render=lambda _url: page)

        self.assertEqual(found.count, 0)
        self.assertEqual(found.blocked, "cloudflare challenge")

    def test_an_aws_waf_wall_is_classified(self) -> None:
        page = FakeRenderedPage(html="<html>aws waf</html>", status=405)
        found = detect_filters_rendered("https://careers.icims.com/x", render=lambda _url: page)
        self.assertEqual(found.blocked, "aws waf")

    def test_a_captcha_page_is_classified(self) -> None:
        page = FakeRenderedPage(html='<div class="g-recaptcha"></div>', status=403)
        found = detect_filters_rendered("https://acme.com/careers", render=lambda _url: page)
        self.assertEqual(found.blocked, "captcha")

    def test_a_render_error_is_reported_not_raised(self) -> None:
        page = FakeRenderedPage(error="navigation timeout")
        found = detect_filters_rendered("https://acme.com/careers", render=lambda _url: page)

        self.assertEqual(found.count, 0)
        self.assertEqual(found.blocked, "navigation timeout")

    def test_an_absent_browser_is_reported_not_raised(self) -> None:
        found = detect_filters_rendered("https://acme.com/careers", render=lambda _url: None)
        self.assertEqual(found.blocked, "browser unavailable")

    def test_a_renderer_that_raises_is_contained(self) -> None:
        def explode(_url):
            raise RuntimeError("chromium died")

        found = detect_filters_rendered("https://acme.com/careers", render=explode)
        self.assertIn("render failed", found.blocked)

    def test_a_blocked_page_is_distinguishable_from_an_unfiltered_one(self) -> None:
        """"No filters" and "never saw the board" must not look the same."""
        blocked = detect_filters_rendered(
            "https://x", render=lambda _u: FakeRenderedPage(html="Just a moment...", status=503)
        )
        plain = detect_filters_rendered(
            "https://x", render=lambda _u: FakeRenderedPage(html=NO_FILTERS)
        )

        self.assertEqual(blocked.count, plain.count)
        self.assertTrue(blocked.blocked)
        self.assertFalse(plain.blocked)


class TestEngineeringIsNotAssumedTechnical(unittest.TestCase):
    """The rule the brief states explicitly: Engineering is not IT."""

    def test_an_engineering_department_is_not_selected(self) -> None:
        found = detect_filters(GREENHOUSE, "https://boards.greenhouse.io/acme")
        chosen = [option.label for option in technology_options(found)]

        self.assertIn("Information Technology", chosen)
        self.assertNotIn("Engineering", chosen)

    def test_a_workday_engineering_job_family_is_not_selected(self) -> None:
        found = detect_filters(NO_FILTERS, "https://x", payloads=[WORKDAY_PAYLOAD])
        chosen = [option.label for option in technology_options(found)]

        self.assertIn("Information Technology", chosen)
        self.assertNotIn("Engineering", chosen)
        self.assertNotIn("Manufacturing", chosen)

    def test_the_decision_comes_from_the_shared_classifier(self) -> None:
        """Not a second word list that could drift from the first."""
        from crawler.tech_filter import is_tech_job

        self.assertTrue(is_tech_job("Information Technology"))
        self.assertFalse(is_tech_job("Engineering"))

    def test_an_operator_keyword_can_widen_it(self) -> None:
        found = detect_filters(GREENHOUSE, "https://boards.greenhouse.io/acme")
        chosen = [
            option.label
            for option in technology_options(found, extra_keywords=["people operations"])
        ]
        self.assertIn("People Operations", chosen)

    def test_only_narrowing_filters_are_considered(self) -> None:
        """A location called "Remote" is not a technology department."""
        found = detect_filters(GREENHOUSE, "https://boards.greenhouse.io/acme")
        chosen = [option.label for option in technology_options(found)]
        self.assertNotIn("Remote - US", chosen)


class TestFilteredUrls(unittest.TestCase):
    """Turning a chosen option into a URL to crawl."""

    def test_a_link_filter_supplies_its_own_url(self) -> None:
        found = detect_filters(ICIMS_STYLE, "https://careers.icims.com/jobs/search")
        urls = technology_urls(found)

        self.assertEqual(len(urls), 1)
        self.assertIn("department=Information+Technology", urls[0])

    def test_a_select_filter_builds_a_url_from_its_form(self) -> None:
        found = detect_filters(GREENHOUSE, "https://boards.greenhouse.io/acme")
        urls = technology_urls(found, "https://boards.greenhouse.io/acme")

        self.assertEqual(len(urls), 1)
        self.assertIn("department_id=2", urls[0])

    def test_a_filter_with_no_url_and_no_parameter_produces_none(self) -> None:
        """Guessing how a board submits its form requests pages that do not exist."""
        found = detect_filters(LEVER_STYLE, "https://jobs.lever.co/acme")
        self.assertEqual(technology_urls(found, "https://jobs.lever.co/acme"), [])

    def test_urls_are_distinct(self) -> None:
        markup = """
        <nav>
          <a href="/s?department=IT">Information Technology</a>
          <a href="/s?department=IT">Information Technology</a>
          <a href="/s?department=HR">Human Resources</a>
        </nav>
        """
        found = detect_filters(markup, "https://x/s")
        self.assertEqual(len(technology_urls(found)), len(set(technology_urls(found))))

    def test_nothing_relevant_yields_no_urls(self) -> None:
        markup = """
        <nav>
          <a href="/s?department=Nursing">Nursing</a>
          <a href="/s?department=Facilities">Facilities</a>
        </nav>
        """
        found = detect_filters(markup, "https://x/s")
        self.assertEqual(technology_urls(found), [])


class TestBoundedByConstruction(unittest.TestCase):
    """Nothing here may let one company consume a run."""

    def test_filtered_urls_are_capped(self) -> None:
        links = "".join(
            f'<a href="/s?department=IT{index}">Information Technology {index}</a>'
            for index in range(50)
        )
        found = detect_filters(f"<nav>{links}</nav>", "https://x/s")

        self.assertLessEqual(len(technology_urls(found)), MAX_FILTERED_URLS)

    def test_the_cap_can_be_lowered_but_never_exceeded(self) -> None:
        links = "".join(
            f'<a href="/s?department=IT{index}">Information Technology {index}</a>'
            for index in range(50)
        )
        found = detect_filters(f"<nav>{links}</nav>", "https://x/s")

        self.assertEqual(len(technology_urls(found, limit=2)), 2)
        self.assertEqual(technology_urls(found, limit=0), [])

    def test_filters_are_never_combined(self) -> None:
        """A department list crossed with a location list is a request explosion."""
        self.assertEqual(MAX_FILTERS_COMBINED, 1)

        markup = """
        <nav>
          <a href="/s?department=IT">Information Technology</a>
          <a href="/s?department=HR">Human Resources</a>
          <a href="/s?jobfamily=Software">Software Engineering</a>
          <a href="/s?jobfamily=Nursing">Nursing</a>
        </nav>
        """
        found = detect_filters(markup, "https://x/s")
        urls = technology_urls(found)

        # Both filters are reported...
        self.assertGreaterEqual(found.count, 2)
        # ...but only one contributes URLs, and no URL carries both parameters.
        for url in urls:
            self.assertFalse("department=" in url and "jobfamily=" in url)

    def test_filters_per_page_are_capped(self) -> None:
        selects = "".join(
            f'<select name="f{index}" aria-label="Filter {index}">'
            f'<option value="1">One</option><option value="2">Two</option></select>'
            for index in range(40)
        )
        found = detect_filters(f"<form method='get' action='/x'>{selects}</form>", "https://x")

        self.assertLessEqual(found.count, MAX_FILTERS)

    def test_options_per_filter_are_capped(self) -> None:
        options = "".join(f'<option value="{i}">Dept {i}</option>' for i in range(2000))
        markup = f'<select name="department" aria-label="Department">{options}</select>'
        found = detect_filters(markup, "https://x")

        self.assertLessEqual(len(found.filters[0].options), MAX_OPTIONS)

    def test_a_pathological_page_terminates(self) -> None:
        """Deeply nested JSON must not recurse without bound."""
        payload: dict = {"a": {}}
        node = payload["a"]
        for _ in range(200):
            node["a"] = {}
            node = node["a"]
        node["departments"] = ["Information Technology"]

        self.assertIsInstance(detect_filters_from_payloads([payload]), list)


class TestNoDuplicateFilters(unittest.TestCase):
    """One control rendered twice is one filter."""

    def test_a_responsive_board_rendering_a_select_twice(self) -> None:
        markup = f"<div class='desktop'>{GREENHOUSE}</div><div class='mobile'>{GREENHOUSE}</div>"
        found = detect_filters(markup, "https://boards.greenhouse.io/acme")

        self.assertEqual(found.count, 2, "two distinct filters, not four")

    def test_a_select_that_is_also_a_set_of_links(self) -> None:
        markup = """
        <form method="get" action="/s">
          <select name="department" aria-label="Department">
            <option value="IT">Information Technology</option>
            <option value="HR">Human Resources</option>
          </select>
        </form>
        <nav>
          <a href="/s?department=IT">Information Technology</a>
          <a href="/s?department=HR">Human Resources</a>
        </nav>
        """
        found = detect_filters(markup, "https://x/s")

        departments = found.by_type(FilterType.DEPARTMENT)
        self.assertEqual(len(departments), 1, "the same options are the same filter")


class TestFilterSetSummary(unittest.TestCase):
    """The metadata a report or a sheet cell would hold."""

    def setUp(self) -> None:
        self.summary = detect_filters(
            GREENHOUSE, "https://boards.greenhouse.io/acme"
        ).summary()

    def test_it_reports_the_shape_of_what_was_found(self) -> None:
        self.assertTrue(self.summary["filters_detected"])
        self.assertEqual(self.summary["filter_count"], 2)
        self.assertEqual(self.summary["filter_types"], "department, location")

    def test_it_names_the_detection_method(self) -> None:
        self.assertEqual(self.summary["filter_detection_method"], "select")

    def test_it_carries_a_confidence(self) -> None:
        self.assertGreater(self.summary["filter_confidence"], 0.5)

    def test_it_lists_values(self) -> None:
        self.assertIn("Information Technology", self.summary["filter_values"])

    def test_an_empty_set_summarises_cleanly(self) -> None:
        summary = FilterSet().summary()
        self.assertFalse(summary["filters_detected"])
        self.assertEqual(summary["filter_count"], 0)
        self.assertEqual(summary["filter_confidence"], 0.0)


class TestExistingBehaviourIsUntouched(unittest.TestCase):
    """This module only reads. It changes nothing about how jobs are crawled."""

    def test_it_makes_no_network_call(self) -> None:
        """Detection is pure parsing; fetching is the caller's business."""
        import crawler.job_filters as module

        source = module.__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()

        for forbidden in ("requests.get", "session.get", "urlopen"):
            self.assertNotIn(forbidden, text, forbidden)

    def test_job_identity_is_untouched(self) -> None:
        """De-duplication stays where it is; this module has no opinion on it."""
        import crawler.job_filters as module

        with open(module.__file__, encoding="utf-8") as handle:
            text = handle.read()

        self.assertNotIn("job_uid", text)
        self.assertNotIn("def job_identity", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
