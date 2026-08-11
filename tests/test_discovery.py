"""Tests for reading job listings out of a page's JavaScript state.

The risk this module carries is not missing a board — the caller has other
strategies for that — but inventing one. A navigation menu is also a list of
objects with a title and a URL, and reporting those as jobs would be worse than
reporting nothing. Most of what follows is therefore about what must *not* be
extracted.
"""

from __future__ import annotations

import json
import unittest

from utils.discovery import (
    embedded_json,
    job_like_objects,
    jobs_from_state,
    script_endpoints,
    text_of,
)


def _page(script: str, script_type: str = "", script_id: str = "") -> str:
    """Wrap a script body in a minimal page.

    Args:
        script: The script body.
        script_type: Value for the ``type`` attribute.
        script_id: Value for the ``id`` attribute.

    Returns:
        The markup.
    """
    attributes = ""
    if script_type:
        attributes += f' type="{script_type}"'
    if script_id:
        attributes += f' id="{script_id}"'
    return f"<html><body><script{attributes}>{script}</script></body></html>"


class TestTextOf(unittest.TestCase):
    """Vendors publish the same field as a string, an object or a list."""

    def test_reads_a_plain_string(self) -> None:
        self.assertEqual(text_of("  Austin,  TX "), "Austin, TX")

    def test_reads_a_named_object(self) -> None:
        self.assertEqual(text_of({"name": "Austin, TX"}), "Austin, TX")

    def test_assembles_a_schema_org_address(self) -> None:
        self.assertEqual(
            text_of({"addressLocality": "Austin", "addressRegion": "TX", "addressCountry": "US"}),
            "Austin, TX, US",
        )

    def test_assembles_an_address_under_a_vendors_own_field_names(self) -> None:
        # ADP Recruiting Management's shape, nested two levels deep.
        node = {
            "itemID": "500",
            "address": {
                "cityName": "Pine Brook",
                "countrySubdivisionLevel1": {"codeValue": "NJ", "longName": "New Jersey"},
                "country": {"codeValue": "USA", "longName": "United States"},
            },
        }
        self.assertEqual(text_of([node]), "Pine Brook, New Jersey, United States")

    def test_a_partial_address_keeps_the_parts_it_has(self) -> None:
        self.assertEqual(text_of({"cityName": "Leeds", "country": "United Kingdom"}),
                         "Leeds, United Kingdom")

    def test_joins_a_list_without_repeats(self) -> None:
        self.assertEqual(text_of(["Austin", "Austin", "Boston"]), "Austin, Boston")

    def test_numbers_become_text_and_booleans_do_not(self) -> None:
        self.assertEqual(text_of(12), "12")
        self.assertEqual(text_of(True), "")

    def test_nothing_readable_is_empty(self) -> None:
        for value in (None, {}, [], {"unrelated": {"deep": 1}}):
            with self.subTest(value=value):
                self.assertEqual(text_of(value), "")


class TestEmbeddedJson(unittest.TestCase):
    """Every wrapper a single-page career site ships its state in."""

    def test_reads_next_data(self) -> None:
        payload = {"props": {"jobs": [{"title": "Engineer"}]}}
        found = embedded_json(_page(json.dumps(payload), "application/json", "__NEXT_DATA__"))
        self.assertEqual(found, [payload])

    def test_reads_a_window_assignment(self) -> None:
        payload = {"jobs": [{"title": "Engineer"}]}
        found = embedded_json(_page(f"window.__NUXT__ = {json.dumps(payload)};"))
        self.assertEqual(found, [payload])

    def test_reads_an_apollo_cache(self) -> None:
        payload = {"Job:1": {"title": "Engineer"}}
        found = embedded_json(_page(f"window.__APOLLO_STATE__={json.dumps(payload)}"))
        self.assertEqual(found, [payload])

    def test_reads_a_data_page_attribute(self) -> None:
        payload = {"props": {"positions": []}}
        markup = f"<div data-page='{json.dumps(payload)}'></div>"
        self.assertEqual(embedded_json(markup), [payload])

    def test_braces_inside_a_string_do_not_end_the_literal(self) -> None:
        payload = {"jobs": [{"title": "Engineer", "description": "Use {braces} and \"quotes\""}]}
        found = embedded_json(_page(f"window.__INITIAL_STATE__ = {json.dumps(payload)};"))
        self.assertEqual(found, [payload])

    def test_html_escaped_state_is_repaired(self) -> None:
        markup = _page("window.__INITIAL_STATE__ = {&quot;jobs&quot;: []};")
        self.assertEqual(embedded_json(markup), [{"jobs": []}])

    def test_json_ld_is_left_to_the_structured_data_reader(self) -> None:
        markup = _page('{"@type": "JobPosting"}', "application/ld+json")
        self.assertEqual(embedded_json(markup), [])

    def test_a_page_with_no_state_yields_nothing(self) -> None:
        for markup in ("", "<html></html>", _page("var x = 1;"), _page("{not json,,}")):
            with self.subTest(markup=markup[:20]):
                self.assertEqual(embedded_json(markup), [])

    def test_the_largest_payload_comes_first(self) -> None:
        small = {"a": 1}
        large = {"jobs": [{"title": f"Engineer {index}"} for index in range(40)]}
        markup = (
            "<html><body>"
            f'<script id="one" type="application/json">{json.dumps(small)}</script>'
            f'<script id="two" type="application/json">{json.dumps(large)}</script>'
            "</body></html>"
        )
        self.assertEqual(embedded_json(markup)[0], large)


class TestJobLikeObjects(unittest.TestCase):
    """Shape-based detection, and above all its false positives."""

    def test_finds_the_repeating_posting_shape(self) -> None:
        payload = {
            "data": {
                "jobs": [
                    {"title": "Engineer", "url": "/j/1", "location": "Austin", "department": "R&D"},
                    {"title": "Analyst", "url": "/j/2", "location": "Boston", "department": "Ops"},
                ]
            }
        }
        found = job_like_objects(payload)
        self.assertEqual([item["title"] for item in found], ["Engineer", "Analyst"])

    def test_a_navigation_menu_is_not_a_job_list(self) -> None:
        payload = {
            "nav": [
                {"title": "About us", "url": "/about"},
                {"title": "Our products", "url": "/products"},
                {"title": "Contact sales", "url": "/contact"},
            ]
        }
        self.assertEqual(job_like_objects(payload), [])

    def test_the_larger_shape_wins_over_a_smaller_one(self) -> None:
        payload = {
            "featured": [{"title": "Highlighted role", "location": "Austin"}],
            "jobs": [
                {"title": f"Engineer {index}", "url": f"/j/{index}", "location": "Austin",
                 "employmentType": "Full-time"}
                for index in range(5)
            ],
        }
        found = job_like_objects(payload)
        self.assertEqual(len(found), 5)

    def test_page_furniture_is_never_a_title(self) -> None:
        payload = {"items": [{"title": "Careers", "location": "x"}, {"title": "Jobs", "location": "y"}]}
        self.assertEqual(job_like_objects(payload), [])

    def test_a_description_is_not_a_title(self) -> None:
        sentence = "We are looking for someone. They will do things. It will be great. Apply now."
        payload = {"items": [{"title": sentence, "location": "Austin"}]}
        self.assertEqual(job_like_objects(payload), [])

    def test_key_spelling_does_not_matter(self) -> None:
        payload = {"Positions": [{"JobTitle": "Engineer", "job_url": "/j/1", "Location": "Austin"}]}
        found = job_like_objects(payload)
        self.assertEqual(len(found), 1)

    def test_a_cycle_cannot_hang_the_walk(self) -> None:
        node: dict = {"title": "Engineer", "location": "Austin"}
        node["self"] = node
        self.assertIsInstance(job_like_objects(node), list)

    def test_non_json_input_is_tolerated(self) -> None:
        for payload in (None, 3, "text", []):
            with self.subTest(payload=payload):
                self.assertEqual(job_like_objects(payload), [])


class TestJobsFromState(unittest.TestCase):
    """End to end: markup in, Job records out."""

    def test_builds_jobs_with_absolute_urls(self) -> None:
        payload = {
            "jobs": [
                {"title": "Field Engineer", "url": "/careers/1", "location": "Denver, CO",
                 "department": "Service"},
                {"title": "Field Technician", "url": "/careers/2", "location": "Denver, CO",
                 "department": "Service"},
            ]
        }
        markup = _page(json.dumps(payload), "application/json", "__NEXT_DATA__")

        jobs = jobs_from_state(markup, "https://acme.com/careers", "Acme", "Generic HTML")

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].job_url, "https://acme.com/careers/1")
        self.assertEqual(jobs[0].country, "United States")
        self.assertEqual(jobs[0].platform, "Generic HTML")

    def test_a_url_builder_rescues_postings_that_name_no_url(self) -> None:
        payload = {"jobs": [{"title": "Engineer", "jobId": "77", "location": "Austin"}]}
        markup = _page(json.dumps(payload), "application/json", "__NEXT_DATA__")

        jobs = jobs_from_state(
            markup,
            "https://acme.com/careers",
            "Acme",
            "Generic HTML",
            url_builder=lambda obj, page: f"https://acme.com/j/{obj['jobid']}",
        )

        self.assertEqual(jobs[0].job_url, "https://acme.com/j/77")

    def test_a_failing_url_builder_does_not_raise(self) -> None:
        payload = {"jobs": [{"title": "Engineer", "jobId": "77", "location": "Austin"}]}
        markup = _page(json.dumps(payload), "application/json", "__NEXT_DATA__")

        def explode(obj: dict, page: str) -> str:
            raise RuntimeError("boom")

        self.assertEqual(
            jobs_from_state(markup, "https://acme.com/", "Acme", "X", url_builder=explode), []
        )

    def test_malformed_input_never_raises(self) -> None:
        for markup in ("", "<<<", _page("window.__NUXT__ = {"), "\x00"):
            with self.subTest(markup=markup[:12]):
                self.assertEqual(jobs_from_state(markup, "https://acme.com/", "Acme", "X"), [])


class TestScriptEndpoints(unittest.TestCase):
    """The diagnostics need the bundles and API paths a page references."""

    def test_collects_bundles_and_api_paths(self) -> None:
        markup = (
            '<script src="/static/app.abc123.js"></script>'
            '<script>fetch("/api/v2/jobs?page=1")</script>'
        )
        bundles, endpoints = script_endpoints(markup, "https://acme.com/careers")

        self.assertIn("https://acme.com/static/app.abc123.js", bundles)
        self.assertIn("/api/v2/jobs?page=1", endpoints)

    def test_an_empty_page_yields_two_empty_lists(self) -> None:
        self.assertEqual(script_endpoints("", "https://acme.com/"), ([], []))


if __name__ == "__main__":
    unittest.main()
