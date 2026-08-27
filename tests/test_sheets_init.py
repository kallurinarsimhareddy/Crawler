"""Unit tests for the Google Sheets storage and initialisation layer.

Every test runs against :class:`tests._fake_sheets.FakeSheetsService`, an
in-memory simulator, so the suite needs no credentials, no network and no real
spreadsheet.

Three properties matter more than the rest, and each has its own class:

* :class:`TestIdempotence` — running initialisation twice must issue no second
  round of changes. Not "no harmful changes": no mutating request at all.
* :class:`TestNothingIsDestroyed` — the crawler cannot delete a tab, a column or
  a row, and cannot overwrite a column it does not manage. This is enforced in
  :mod:`sheets.client` and asserted here.
* :class:`TestExistingSpreadsheet` — a spreadsheet that already has tabs and
  columns is adopted, not replaced.
"""

from __future__ import annotations

import unittest
from typing import List

from sheets.client import DestructiveRequestError, SheetsClient, SheetsError
from sheets.init import DEFAULT_DISCOVERY_CONFIG, initialise
from sheets.schema import (
    ALL_TABS,
    CURRENT_JOBS,
    DISCOVERY_CONFIG,
    JOB_HISTORY,
    MASTER_COMPANIES,
    a1_range,
    column_letter,
    match_title,
    normalise_header,
    plan_columns,
)
from tests._fake_sheets import FakeHttpError, FakeSheetsService, FlakyFakeSheetsService

#: The ten titles version 3 creates, in creation order. IT_KEYWORDS joined
#: them when keyword configuration moved out of Python and into the sheet.
EXPECTED_TITLES: List[str] = [
    "MASTER_COMPANIES",
    "CURRENT_JOBS",
    "JOB_HISTORY",
    "NEW_LAST_WEEK",
    "NEW_COMPANY_DISCOVERY",
    "DISCOVERY_CONFIG",
    "IT_KEYWORDS",
    "WEEKLY_RUNS",
    "DASHBOARD",
    "FAILURES",
]


def fresh_service() -> FakeSheetsService:
    """A spreadsheet as Google creates one: a single empty ``Sheet1``."""
    return FakeSheetsService({"Sheet1": []})


def client_for(service: FakeSheetsService) -> SheetsClient:
    """A client over a fake, with the backoff sleeper disabled."""
    return SheetsClient(service, "fake-spreadsheet-id", sleep=lambda _seconds: None)


class TestSchemaHelpers(unittest.TestCase):
    """The pure parts: header folding, A1 notation, column mapping."""

    def test_normalising_ignores_case_punctuation_and_spacing(self) -> None:
        for spelling in ("Career Page URL", "career page url", "  Career-Page  URL ", "CareerPageURL"):
            self.assertEqual(normalise_header(spelling), "careerpageurl", spelling)

    def test_distinct_wordings_normalise_distinctly(self) -> None:
        """Folding is not a synonym table: it only removes noise."""
        self.assertNotEqual(normalise_header("career_url"), normalise_header("Career Page URL"))

    def test_every_spelling_of_a_column_resolves_to_it(self) -> None:
        """Uniting genuinely different wordings is the alias table's job."""
        keys = MASTER_COMPANIES.columns[2].keys()
        for spelling in ("Career Page URL", "career_url", "Careers / Jobs URL", "Careers URL"):
            self.assertIn(normalise_header(spelling), keys, spelling)

    def test_column_letters(self) -> None:
        self.assertEqual(column_letter(0), "A")
        self.assertEqual(column_letter(25), "Z")
        self.assertEqual(column_letter(26), "AA")
        self.assertEqual(column_letter(51), "AZ")

    def test_negative_column_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            column_letter(-1)

    def test_a1_quotes_the_title(self) -> None:
        self.assertEqual(a1_range("MASTER_COMPANIES", 1, 0, 1, 3), "'MASTER_COMPANIES'!A1:D1")

    def test_a1_escapes_an_apostrophe(self) -> None:
        """A title with an apostrophe would otherwise address the wrong tab."""
        self.assertEqual(a1_range("Bob's Tab", 2, 0), "'Bob''s Tab'!A2")

    def test_a1_with_no_ends_is_a_single_cell_not_a_whole_tab(self) -> None:
        """The distinction that made seeding read one cell and call it a tab."""
        self.assertEqual(a1_range("DASHBOARD", 2, 0), "'DASHBOARD'!A2")

    def test_a1_open_ended_downwards(self) -> None:
        self.assertEqual(a1_range("DISCOVERY_CONFIG", 2, 0, None, 3), "'DISCOVERY_CONFIG'!A2:D")

    def test_every_tab_has_unique_fields_and_headers(self) -> None:
        for spec in ALL_TABS:
            self.assertEqual(len(set(spec.fields)), len(spec.fields), f"{spec.key} fields")
            keys = [normalise_header(header) for header in spec.headers]
            self.assertEqual(len(set(keys)), len(keys), f"{spec.key} headers")

    def test_every_tab_title_is_unique(self) -> None:
        titles = [normalise_header(spec.title) for spec in ALL_TABS]
        self.assertEqual(len(set(titles)), len(titles))

    def test_no_two_tabs_claim_the_same_alias(self) -> None:
        """An overlapping alias would make one tab adopt another's."""
        seen = {}
        for spec in ALL_TABS:
            for key in spec.title_keys():
                self.assertNotIn(key, seen, f"{spec.key} and {seen.get(key)} share alias {key!r}")
                seen[key] = spec.key

    def test_identity_columns_exist(self) -> None:
        for spec in ALL_TABS:
            if spec.identity_field:
                self.assertIn(spec.identity_field, spec.fields, spec.key)


class TestColumnPlanning(unittest.TestCase):
    """Fitting a specification onto columns that already exist."""

    def test_an_empty_tab_gets_every_column(self) -> None:
        plan = plan_columns(MASTER_COMPANIES, [])
        self.assertEqual(plan.header_row, list(MASTER_COMPANIES.headers))
        self.assertFalse(plan.is_satisfied)

    def test_existing_columns_keep_their_positions(self) -> None:
        plan = plan_columns(MASTER_COMPANIES, ["Company Name", "Notes", "Website"])
        self.assertEqual(plan.index_of("company_name"), 0)
        self.assertEqual(plan.index_of("website"), 2)

    def test_an_unmanaged_column_is_preserved(self) -> None:
        plan = plan_columns(MASTER_COMPANIES, ["Company Name", "Notes", "Website"])
        self.assertEqual(plan.unknown_headers, ["Notes"])
        self.assertEqual(plan.header_row[1], "Notes")

    def test_missing_columns_are_appended_on_the_right(self) -> None:
        plan = plan_columns(MASTER_COMPANIES, ["Company Name", "Notes", "Website"])
        self.assertEqual(plan.header_row[:3], ["Company Name", "Notes", "Website"])
        self.assertIn("Career Page URL", plan.appended_headers)

    def test_an_operators_own_spelling_is_adopted(self) -> None:
        """The alias table is what stops a near-duplicate column appearing."""
        plan = plan_columns(MASTER_COMPANIES, ["Company", "Careers / Jobs URL"])
        self.assertEqual(plan.index_of("company_name"), 0)
        self.assertEqual(plan.index_of("career_url"), 1)
        self.assertNotIn("Career Page URL", plan.appended_headers)

    def test_a_satisfied_tab_needs_no_write(self) -> None:
        plan = plan_columns(MASTER_COMPANIES, list(MASTER_COMPANIES.headers))
        self.assertTrue(plan.is_satisfied)
        self.assertEqual(plan.appended_headers, [])

    def test_trailing_blank_headers_are_ignored(self) -> None:
        """Otherwise appends land past a gap and the header row breaks."""
        plan = plan_columns(MASTER_COMPANIES, ["Company Name", "", "", ""])
        self.assertEqual(plan.existing_headers, ["Company Name"])
        self.assertEqual(plan.index_of("website"), 1)

    def test_a_duplicated_header_does_not_shadow_the_original(self) -> None:
        plan = plan_columns(MASTER_COMPANIES, ["Company Name", "Company Name"])
        self.assertEqual(plan.index_of("company_name"), 0)

    def test_row_for_leaves_unmanaged_columns_alone(self) -> None:
        """None means 'do not change this cell'."""
        plan = plan_columns(MASTER_COMPANIES, ["Company Name", "Notes", "Website"])
        row = plan.row_for({"company_name": "Acme", "website": "acme.com"})
        self.assertEqual(row[0], "Acme")
        self.assertIsNone(row[1])
        self.assertEqual(row[2], "acme.com")

    def test_match_title_finds_an_alias(self) -> None:
        self.assertEqual(match_title(MASTER_COMPANIES, ["Companies"]), "Companies")

    def test_match_title_is_case_insensitive(self) -> None:
        self.assertEqual(match_title(CURRENT_JOBS, ["current jobs"]), "current jobs")

    def test_match_title_returns_none_when_absent(self) -> None:
        self.assertIsNone(match_title(JOB_HISTORY, ["Something Else"]))


class TestFreshSpreadsheet(unittest.TestCase):
    """A brand-new spreadsheet with one empty ``Sheet1``."""

    def setUp(self) -> None:
        self.service = fresh_service()
        self.report = initialise(client_for(self.service))

    def test_every_tab_exists(self) -> None:
        self.assertEqual(list(self.service.tabs), EXPECTED_TITLES)

    def test_no_sheet1_remains(self) -> None:
        """The empty default is reused, not left as a tenth empty tab."""
        self.assertNotIn("Sheet1", self.service.tabs)

    def test_sheet1_was_renamed_rather_than_deleted(self) -> None:
        self.assertEqual(self.report.outcomes[0].action, "renamed")
        self.assertIn("Sheet1", self.report.outcomes[0].detail)
        self.assertEqual(self.service.destructive_requests(), [])

    def test_every_tab_has_its_full_header_row(self) -> None:
        for spec in ALL_TABS:
            self.assertEqual(
                self.service.headers_of(spec.title), list(spec.headers), spec.title
            )

    def test_headers_are_frozen_and_formatted(self) -> None:
        for title in EXPECTED_TITLES:
            self.assertEqual(self.service.frozen[title], 1, title)
            self.assertIn(title, self.service.formatted, title)

    def test_grids_are_wide_enough_for_their_headers(self) -> None:
        for spec in ALL_TABS:
            _rows, columns = self.service.grid[spec.title]
            self.assertGreaterEqual(columns, len(spec.headers), spec.title)

    def test_the_report_lists_what_it_did(self) -> None:
        self.assertTrue(self.report.changed)
        self.assertEqual(len(self.report.created), len(EXPECTED_TITLES))
        self.assertEqual(self.report.untouched_tabs, [])


class TestIdempotence(unittest.TestCase):
    """Running initialisation twice must change nothing the second time."""

    def test_a_second_run_issues_no_mutating_request(self) -> None:
        service = fresh_service()
        initialise(client_for(service))

        before = dict(service.tabs)
        service.calls.clear()
        service.structural_kinds.clear()

        report = initialise(client_for(service))

        self.assertEqual(service.mutating_calls(), [])
        self.assertEqual(service.structural_kinds, [])
        self.assertFalse(report.changed)
        self.assertEqual(service.tabs, before)

    def test_a_second_run_reports_every_tab_unchanged(self) -> None:
        service = fresh_service()
        initialise(client_for(service))
        report = initialise(client_for(service))

        self.assertEqual({item.action for item in report.outcomes}, {"unchanged"})

    def test_a_third_run_is_still_a_no_op(self) -> None:
        service = fresh_service()
        for _ in range(3):
            initialise(client_for(service))

        self.assertEqual(list(service.tabs), EXPECTED_TITLES)
        for spec in ALL_TABS:
            self.assertEqual(service.headers_of(spec.title), list(spec.headers), spec.title)

    def test_tab_count_does_not_grow(self) -> None:
        service = fresh_service()
        for _ in range(4):
            initialise(client_for(service))
        self.assertEqual(len(service.tabs), len(EXPECTED_TITLES))

    def test_headers_are_not_duplicated(self) -> None:
        service = fresh_service()
        initialise(client_for(service))
        initialise(client_for(service))

        headers = service.headers_of("MASTER_COMPANIES")
        self.assertEqual(len(headers), len(set(headers)))


class TestNothingIsDestroyed(unittest.TestCase):
    """The promise that a weekly job cannot damage the operator's spreadsheet."""

    def test_initialisation_never_emits_a_destructive_request(self) -> None:
        service = FakeSheetsService(
            {
                "Sheet1": [],
                "My Notes": [["Keep"], ["This"]],
                "MASTER_COMPANIES": [["Company Name", "Notes"], ["Acme", "important"]],
            }
        )
        initialise(client_for(service))
        self.assertEqual(service.destructive_requests(), [])

    def test_the_client_refuses_a_delete_before_sending_it(self) -> None:
        service = fresh_service()
        client = client_for(service)

        with self.assertRaises(DestructiveRequestError):
            client.batch_update([{"deleteSheet": {"sheetId": 0}}])

        self.assertEqual(service.calls, [])

    def test_a_batch_containing_one_delete_performs_none_of_it(self) -> None:
        service = fresh_service()
        client = client_for(service)

        with self.assertRaises(DestructiveRequestError):
            client.batch_update(
                [
                    {"addSheet": {"properties": {"title": "Harmless"}}},
                    {"deleteDimension": {"range": {"sheetId": 0}}},
                ]
            )

        self.assertNotIn("Harmless", service.tabs)

    def test_every_deletion_kind_is_refused(self) -> None:
        client = client_for(fresh_service())
        for kind in ("deleteSheet", "deleteDimension", "deleteRange", "cutPaste", "sortRange"):
            with self.assertRaises(DestructiveRequestError, msg=kind):
                client.batch_update([{kind: {}}])

    def test_an_unrelated_tab_is_left_exactly_as_it_was(self) -> None:
        service = FakeSheetsService({"Sheet1": [], "Quarterly Plan": [["Q1"], ["revenue"]]})
        initialise(client_for(service))

        self.assertEqual(service.tabs["Quarterly Plan"], [["Q1"], ["revenue"]])

    def test_an_unrelated_tab_is_reported_as_untouched(self) -> None:
        service = FakeSheetsService({"Sheet1": [], "Quarterly Plan": [["Q1"]]})
        report = initialise(client_for(service))
        self.assertIn("Quarterly Plan", report.untouched_tabs)

    def test_a_sheet1_with_data_is_not_reused(self) -> None:
        """Only a genuinely empty factory tab may be renamed."""
        service = FakeSheetsService({"Sheet1": [["something the operator typed"]]})
        initialise(client_for(service))

        self.assertIn("Sheet1", service.tabs)
        self.assertEqual(service.tabs["Sheet1"], [["something the operator typed"]])
        # Sheet1 survives alongside every created tab, rather than being reused.
        self.assertEqual(len(service.tabs), len(EXPECTED_TITLES) + 1)

    def test_existing_row_data_survives_initialisation(self) -> None:
        service = FakeSheetsService(
            {
                "Sheet1": [],
                "MASTER_COMPANIES": [
                    ["Company Name", "Website"],
                    ["Acme", "acme.com"],
                    ["Other", "other.com"],
                ],
            }
        )
        initialise(client_for(service))

        rows = service.tabs["MASTER_COMPANIES"]
        self.assertEqual(rows[1][:2], ["Acme", "acme.com"])
        self.assertEqual(rows[2][:2], ["Other", "other.com"])


class TestExistingSpreadsheet(unittest.TestCase):
    """A spreadsheet that already has tabs and columns of its own."""

    def test_a_tab_matching_by_alias_is_adopted(self) -> None:
        service = FakeSheetsService({"Companies": [["Company Name", "Website"]]})
        report = initialise(client_for(service))

        self.assertIn("Companies", service.tabs)
        self.assertNotIn("MASTER_COMPANIES", service.tabs)
        master = next(item for item in report.outcomes if item.key == MASTER_COMPANIES.key)
        self.assertEqual(master.title, "Companies")
        self.assertEqual(master.action, "extended")

    def test_adopting_appends_only_the_missing_columns(self) -> None:
        service = FakeSheetsService({"MASTER_COMPANIES": [["Company Name", "Website"]]})
        initialise(client_for(service))

        headers = service.headers_of("MASTER_COMPANIES")
        self.assertEqual(headers[:2], ["Company Name", "Website"])
        self.assertEqual(len(headers), len(MASTER_COMPANIES.headers))

    def test_a_hand_added_column_keeps_its_position_and_contents(self) -> None:
        service = FakeSheetsService(
            {"MASTER_COMPANIES": [["Company Name", "Notes", "Website"], ["Acme", "call back", "acme.com"]]}
        )
        report = initialise(client_for(service))

        headers = service.headers_of("MASTER_COMPANIES")
        self.assertEqual(headers[1], "Notes")
        self.assertEqual(service.tabs["MASTER_COMPANIES"][1][1], "call back")

        master = next(item for item in report.outcomes if item.key == MASTER_COMPANIES.key)
        self.assertEqual(master.preserved_headers, ["Notes"])

    def test_an_already_complete_tab_is_left_alone(self) -> None:
        service = FakeSheetsService(
            {"MASTER_COMPANIES": [list(MASTER_COMPANIES.headers)], "Sheet1": []}
        )
        report = initialise(client_for(service))

        master = next(item for item in report.outcomes if item.key == MASTER_COMPANIES.key)
        self.assertEqual(master.action, "unchanged")

    def test_existing_row_counts_are_reported(self) -> None:
        service = FakeSheetsService(
            {"MASTER_COMPANIES": [list(MASTER_COMPANIES.headers), ["Acme"], ["Other"]]}
        )
        report = initialise(client_for(service))

        master = next(item for item in report.outcomes if item.key == MASTER_COMPANIES.key)
        self.assertEqual(master.rows, 2)


class TestDryRun(unittest.TestCase):
    """``--dry-run`` must report without changing anything."""

    def test_nothing_is_written(self) -> None:
        service = fresh_service()
        report = initialise(client_for(service), dry_run=True)

        self.assertEqual(service.mutating_calls(), [])
        self.assertEqual(list(service.tabs), ["Sheet1"])
        self.assertTrue(report.dry_run)

    def test_it_still_reports_what_would_happen(self) -> None:
        service = fresh_service()
        report = initialise(client_for(service), dry_run=True)

        self.assertTrue(report.changed)
        self.assertEqual(len(report.outcomes), len(EXPECTED_TITLES))
        self.assertEqual(report.outcomes[0].action, "renamed")

    def test_a_dry_run_after_a_real_one_reports_no_work(self) -> None:
        service = fresh_service()
        initialise(client_for(service))
        report = initialise(client_for(service), dry_run=True)
        self.assertFalse(report.changed)


class TestDiscoveryConfigSeeding(unittest.TestCase):
    """The starter industry list."""

    def test_seeding_writes_the_default_industries(self) -> None:
        service = fresh_service()
        report = initialise(client_for(service), seed_config=True)

        self.assertEqual(report.seeded_config_rows, len(DEFAULT_DISCOVERY_CONFIG))
        rows = service.tabs["DISCOVERY_CONFIG"]
        self.assertEqual(rows[0], list(DISCOVERY_CONFIG.headers))
        self.assertEqual(rows[1][0], "Information Technology")

    def test_seeding_is_skipped_when_the_tab_already_has_rows(self) -> None:
        service = fresh_service()
        initialise(client_for(service), seed_config=True)
        report = initialise(client_for(service), seed_config=True)

        self.assertEqual(report.seeded_config_rows, 0)
        self.assertEqual(
            len(service.tabs["DISCOVERY_CONFIG"]), len(DEFAULT_DISCOVERY_CONFIG) + 1
        )

    def test_an_operators_own_config_is_never_overwritten(self) -> None:
        service = FakeSheetsService(
            {
                "Sheet1": [],
                "DISCOVERY_CONFIG": [
                    list(DISCOVERY_CONFIG.headers),
                    ["Quantum", "Quantum Computing", "USA", "TRUE"],
                ],
            }
        )
        initialise(client_for(service), seed_config=True)

        self.assertEqual(service.tabs["DISCOVERY_CONFIG"][1][0], "Quantum")
        self.assertEqual(len(service.tabs["DISCOVERY_CONFIG"]), 2)

    def test_a_config_whose_first_cell_is_blank_is_still_not_overwritten(self) -> None:
        """Seeding once read a single anchor cell, so a blank A2 looked empty."""
        service = FakeSheetsService(
            {
                "Sheet1": [],
                "DISCOVERY_CONFIG": [
                    list(DISCOVERY_CONFIG.headers),
                    ["", "Quantum Computing, QPU", "USA", "TRUE"],
                ],
            }
        )
        report = initialise(client_for(service), seed_config=True)

        self.assertEqual(report.seeded_config_rows, 0)
        self.assertEqual(service.tabs["DISCOVERY_CONFIG"][1][1], "Quantum Computing, QPU")

    def test_not_seeding_leaves_the_tab_empty(self) -> None:
        service = fresh_service()
        initialise(client_for(service))
        self.assertEqual(len(service.tabs["DISCOVERY_CONFIG"]), 1)

    def test_the_defaults_are_well_formed(self) -> None:
        for row in DEFAULT_DISCOVERY_CONFIG:
            self.assertEqual(len(row), len(DISCOVERY_CONFIG.headers))
            self.assertIn(row[3], ("TRUE", "FALSE"))


class TestClientBehaviour(unittest.TestCase):
    """Batching, chunking and waiting out a rate limit."""

    def test_rate_limiting_is_retried(self) -> None:
        service = FlakyFakeSheetsService(failures=2, tabs={"Sheet1": []})
        client = client_for(service)

        titles = client.tab_titles()

        self.assertEqual(titles, ["Sheet1"])
        self.assertEqual(client.stats.retries, 2)

    def test_retry_after_is_honoured(self) -> None:
        service = FlakyFakeSheetsService(failures=1, retry_after="7", tabs={"Sheet1": []})
        waits: List[float] = []
        client = SheetsClient(service, "id", sleep=waits.append)

        client.tab_titles()

        self.assertEqual(waits, [7.0])

    def test_a_permanent_error_is_not_retried(self) -> None:
        """Retrying a malformed request just repeats the mistake."""
        service = FlakyFakeSheetsService(failures=99, status=400, tabs={"Sheet1": []})
        client = client_for(service)

        with self.assertRaises(SheetsError):
            client.tab_titles()

        self.assertEqual(client.stats.retries, 0)

    def test_giving_up_after_the_retry_budget(self) -> None:
        service = FlakyFakeSheetsService(failures=99, tabs={"Sheet1": []})
        client = SheetsClient(service, "id", retries=3, sleep=lambda _s: None)

        with self.assertRaises(SheetsError):
            client.tab_titles()

        self.assertEqual(client.stats.retries, 2)

    def test_a_large_write_is_split_into_chunks(self) -> None:
        service = FakeSheetsService({"CURRENT_JOBS": []})
        client = client_for(service)

        rows = [[f"row {index}", "b", "c"] for index in range(12_000)]
        client.write(a1_range("CURRENT_JOBS", 1, 0), rows)

        writes = [kind for kind, _ in service.calls if kind == "values.update"]
        self.assertGreater(len(writes), 1)
        self.assertEqual(len(service.tabs["CURRENT_JOBS"]), 12_000)
        self.assertEqual(service.tabs["CURRENT_JOBS"][11_999][0], "row 11999")

    def test_a_batch_read_is_one_call(self) -> None:
        service = FakeSheetsService({"A": [["1"]], "B": [["2"]], "C": [["3"]]})
        client = client_for(service)

        results = client.batch_read(
            [a1_range("A", 1, 0), a1_range("B", 1, 0), a1_range("C", 1, 0)]
        )

        self.assertEqual(results, [[["1"]], [["2"]], [["3"]]])
        self.assertEqual(client.stats.reads, 1)

    def test_writing_none_leaves_a_cell_alone(self) -> None:
        service = FakeSheetsService({"T": [["a", "b", "c"]]})
        client = client_for(service)

        client.write(a1_range("T", 1, 0), [["x", None, "z"]])

        self.assertEqual(service.tabs["T"][0], ["x", "b", "z"])

    def test_empty_writes_cost_nothing(self) -> None:
        service = FakeSheetsService({"T": []})
        client = client_for(service)

        self.assertEqual(client.write(a1_range("T", 1, 0), []), 0)
        self.assertEqual(client.batch_write([]), 0)
        self.assertEqual(service.calls, [])

    def test_the_client_counts_its_traffic(self) -> None:
        service = fresh_service()
        client = client_for(service)
        initialise(client)

        self.assertGreater(client.stats.reads, 0)
        self.assertGreater(client.stats.writes, 0)
        self.assertGreater(client.stats.cells_written, 0)
        self.assertIn("read(s)", client.stats.describe())


class TestFakeService(unittest.TestCase):
    """The simulator itself, since every other test rests on it."""

    def test_a_write_then_a_read_round_trips(self) -> None:
        service = FakeSheetsService({"T": []})
        client = client_for(service)

        client.write(a1_range("T", 1, 0), [["a", "b"], ["c", "d"]])

        self.assertEqual(client.read(a1_range("T", 1, 0)), [["a", "b"], ["c", "d"]])

    def test_an_unknown_tab_is_rejected_as_the_api_rejects_it(self) -> None:
        service = FakeSheetsService({"T": []})
        client = client_for(service)

        with self.assertRaises(SheetsError):
            client.read(a1_range("Nonexistent", 1, 0))

    def test_renaming_preserves_content_and_order(self) -> None:
        service = FakeSheetsService({"First": [["x"]], "Second": [["y"]]})
        client = client_for(service)

        client.rename_tab(service.sheet_ids["First"], "Renamed")

        self.assertEqual(list(service.tabs), ["Renamed", "Second"])
        self.assertEqual(service.tabs["Renamed"], [["x"]])

    def test_the_grid_only_grows(self) -> None:
        service = FakeSheetsService({"T": []})
        client = client_for(service)

        client.ensure_size(service.sheet_ids["T"], 5000, 40)
        client.ensure_size(service.sheet_ids["T"], 10, 4)

        self.assertEqual(service.grid["T"], (5000, 40))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
