"""The IT_KEYWORDS tab: an operator's keyword list, reloaded every run.

The point of this tab is that adding ``Salesforce | CRM Platforms | TRUE`` next
week must change what the crawler recognises, with no code change and no
deployment. Setting ``Enabled`` to ``FALSE`` must stop it being used the same
way.

So the tests below care about two things above all: that the sheet is read
fresh at the start of each run rather than baked in, and that a tab an operator
has already edited is never overwritten by the seeder.

Everything runs against a fake spreadsheet. Nothing reaches Google.
"""

from __future__ import annotations

import unittest

from crawler.keywords import (
    DEFAULT_MATCH_TYPE,
    Keyword,
    KeywordSet,
    load_keywords,
)
from sheets.client import SheetsClient
from sheets.init import initialise, seed_keywords
from sheets.schema import IT_KEYWORDS
from tests._fake_sheets import FakeSheetsService


def fixture(rows=None):
    """An initialised fake spreadsheet, and a client over it."""
    service = FakeSheetsService({"Sheet1": []})
    client = SheetsClient(service, "fake", sleep=lambda _s: None)
    initialise(client)
    if rows is not None:
        from sheets.schema import a1_range

        client.write(a1_range(IT_KEYWORDS.title, 2, 0), [list(row) for row in rows])
    return client, service


class TestTheTabIsDeclared(unittest.TestCase):
    """IT_KEYWORDS is part of the schema, like every other tab."""

    def test_the_tab_has_the_five_columns(self) -> None:
        self.assertEqual(
            [column.header for column in IT_KEYWORDS.columns],
            ["Keyword", "Category", "Enabled", "Match Type", "Notes"],
        )

    def test_the_keyword_identifies_a_row(self) -> None:
        """So an edit updates in place rather than appending a duplicate."""
        self.assertEqual(IT_KEYWORDS.identity_field, "keyword")

    def test_it_is_registered_among_the_tabs(self) -> None:
        from sheets.schema import ALL_TABS

        self.assertIn(IT_KEYWORDS, ALL_TABS)

    def test_initialise_creates_it(self) -> None:
        client, _service = fixture()
        titles = {tab["properties"]["title"] for tab in client.metadata()["sheets"]}
        self.assertIn("IT_KEYWORDS", titles)


class TestSeeding(unittest.TestCase):
    """The starter list is written once, and never over an operator's edits."""

    def test_seeding_writes_the_initial_list(self) -> None:
        client, _service = fixture()
        written = seed_keywords(client, IT_KEYWORDS.title, dry_run=False)
        self.assertGreater(written, 60)

    def test_every_seeded_row_is_enabled(self) -> None:
        client, _service = fixture()
        seed_keywords(client, IT_KEYWORDS.title, dry_run=False)
        for keyword in load_keywords(client, only_enabled=False):
            self.assertTrue(keyword.enabled, keyword.keyword)

    def test_the_seed_carries_the_supplied_categories(self) -> None:
        client, _service = fixture()
        seed_keywords(client, IT_KEYWORDS.title, dry_run=False)
        categories = {k.category for k in load_keywords(client)}
        self.assertEqual(categories, {"ERP Platforms"})

    def test_a_dry_run_seeds_nothing(self) -> None:
        client, service = fixture()
        before = len(service.mutating_calls())

        planned = seed_keywords(client, IT_KEYWORDS.title, dry_run=True)

        self.assertGreater(planned, 60)
        self.assertEqual(len(service.mutating_calls()), before)

    def test_a_tab_that_already_has_rows_is_never_reseeded(self) -> None:
        """The operator's own list must survive a re-run of init."""
        client, _service = fixture([["Salesforce", "CRM Platforms", "TRUE", "phrase", ""]])

        written = seed_keywords(client, IT_KEYWORDS.title, dry_run=False)

        self.assertEqual(written, 0)
        keywords = load_keywords(client)
        self.assertEqual([k.keyword for k in keywords], ["Salesforce"])

    def test_seeding_twice_writes_once(self) -> None:
        client, _service = fixture()
        seed_keywords(client, IT_KEYWORDS.title, dry_run=False)
        self.assertEqual(seed_keywords(client, IT_KEYWORDS.title, dry_run=False), 0)


class TestLoading(unittest.TestCase):
    """Reading the operator's current configuration."""

    def test_enabled_keywords_are_loaded(self) -> None:
        client, _service = fixture([
            ["SAP", "ERP Platforms", "TRUE", "phrase", ""],
            ["Oracle", "ERP Platforms", "TRUE", "phrase", ""],
        ])
        keywords = load_keywords(client)
        self.assertEqual({k.keyword for k in keywords}, {"SAP", "Oracle"})

    def test_a_disabled_keyword_is_dropped(self) -> None:
        """The whole point of the Enabled column."""
        client, _service = fixture([
            ["SAP", "ERP Platforms", "TRUE", "phrase", ""],
            ["Oracle", "ERP Platforms", "FALSE", "phrase", "paused"],
        ])
        keywords = load_keywords(client)
        self.assertEqual({k.keyword for k in keywords}, {"SAP"})

    def test_enabled_accepts_the_spellings_a_human_types(self) -> None:
        client, _service = fixture([
            ["A", "X", "TRUE", "phrase", ""],
            ["B", "X", "true", "phrase", ""],
            ["C", "X", "Yes", "phrase", ""],
            ["D", "X", "1", "phrase", ""],
            ["E", "X", "", "phrase", ""],
        ])
        # A blank Enabled means enabled: a keyword typed in a hurry should work.
        self.assertEqual({k.keyword for k in load_keywords(client)},
                         {"A", "B", "C", "D", "E"})

    def test_false_spellings_are_respected(self) -> None:
        client, _service = fixture([
            ["A", "X", "FALSE", "phrase", ""],
            ["B", "X", "false", "phrase", ""],
            ["C", "X", "No", "phrase", ""],
            ["D", "X", "0", "phrase", ""],
        ])
        self.assertEqual(load_keywords(client).all(), [])

    def test_a_blank_keyword_row_is_ignored(self) -> None:
        client, _service = fixture([
            ["SAP", "ERP Platforms", "TRUE", "phrase", ""],
            ["", "", "", "", ""],
        ])
        self.assertEqual(len(load_keywords(client)), 1)

    def test_a_missing_match_type_defaults(self) -> None:
        client, _service = fixture([["SAP", "ERP Platforms", "TRUE", "", ""]])
        self.assertEqual(load_keywords(client).all()[0].match_type, DEFAULT_MATCH_TYPE)

    def test_a_new_keyword_needs_no_code_change(self) -> None:
        """The requirement, stated as a test.

        An operator adds a row; the next load sees it. Nothing in Python names
        Salesforce anywhere.
        """
        client, _service = fixture([
            ["SAP", "ERP Platforms", "TRUE", "phrase", ""],
            ["Salesforce", "CRM Platforms", "TRUE", "phrase", "added this week"],
        ])
        keywords = load_keywords(client)

        self.assertIn("Salesforce", {k.keyword for k in keywords})
        self.assertEqual(keywords.category_of("salesforce"), "CRM Platforms")

    def test_an_absent_tab_yields_an_empty_set_rather_than_raising(self) -> None:
        """A crawl must still run against a spreadsheet nobody has set up."""
        service = FakeSheetsService({"Sheet1": []})
        client = SheetsClient(service, "fake", sleep=lambda _s: None)

        keywords = load_keywords(client)

        self.assertEqual(keywords.all(), [])
        self.assertFalse(keywords)


class TestKeywordSet(unittest.TestCase):
    """What the classifier is handed."""

    def build(self, *rows) -> KeywordSet:
        """A set from ``(keyword, category)`` pairs."""
        return KeywordSet([
            Keyword(keyword=k, category=c, enabled=True, match_type="phrase")
            for k, c in rows
        ])

    def test_lookup_is_case_insensitive(self) -> None:
        keywords = self.build(("SAP", "ERP Platforms"))
        self.assertEqual(keywords.category_of("sap"), "ERP Platforms")

    def test_phrases_are_exposed_for_matching(self) -> None:
        keywords = self.build(("SAP", "ERP"), ("JD Edwards", "ERP"))
        self.assertEqual(set(keywords.phrases()), {"sap", "jd edwards"})

    def test_an_empty_set_is_falsey(self) -> None:
        self.assertFalse(KeywordSet([]))

    def test_a_populated_set_is_truthy(self) -> None:
        self.assertTrue(self.build(("SAP", "ERP")))

    def test_duplicates_collapse_on_the_first_spelling(self) -> None:
        keywords = self.build(("SAP", "ERP Platforms"), ("sap", "Something Else"))
        self.assertEqual(len(keywords), 1)
        self.assertEqual(keywords.category_of("SAP"), "ERP Platforms")

    def test_a_fingerprint_identifies_the_configuration(self) -> None:
        """So a run can record which keyword set produced its verdicts."""
        first = self.build(("SAP", "ERP"))
        same = self.build(("SAP", "ERP"))
        different = self.build(("SAP", "ERP"), ("Oracle", "ERP"))

        self.assertEqual(first.fingerprint(), same.fingerprint())
        self.assertNotEqual(first.fingerprint(), different.fingerprint())


class TestReloadedEveryRun(unittest.TestCase):
    """Cached for one run, never across runs."""

    def test_a_second_load_sees_an_edit(self) -> None:
        from sheets.schema import a1_range

        client, _service = fixture([["SAP", "ERP Platforms", "TRUE", "phrase", ""]])
        self.assertEqual(len(load_keywords(client)), 1)

        client.write(
            a1_range(IT_KEYWORDS.title, 3, 0),
            [["Salesforce", "CRM Platforms", "TRUE", "phrase", ""]],
        )

        self.assertEqual(len(load_keywords(client)), 2)

    def test_a_second_load_sees_a_disable(self) -> None:
        from sheets.schema import a1_range

        client, _service = fixture([
            ["SAP", "ERP Platforms", "TRUE", "phrase", ""],
            ["Oracle", "ERP Platforms", "TRUE", "phrase", ""],
        ])
        self.assertEqual(len(load_keywords(client)), 2)

        client.write(
            a1_range(IT_KEYWORDS.title, 3, 0),
            [["Oracle", "ERP Platforms", "FALSE", "phrase", "paused"]],
        )

        self.assertEqual({k.keyword for k in load_keywords(client)}, {"SAP"})

    def test_loading_makes_no_writes(self) -> None:
        client, service = fixture([["SAP", "ERP Platforms", "TRUE", "phrase", ""]])
        before = len(service.mutating_calls())

        load_keywords(client)

        self.assertEqual(len(service.mutating_calls()), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
