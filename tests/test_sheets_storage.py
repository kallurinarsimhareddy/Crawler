"""Unit tests for the Google Sheets storage layer.

Every test runs against :class:`tests._fake_sheets.FakeSheetsService`, so the
suite needs no credentials, no network and no real spreadsheet.

The properties worth reading first, because each guards a failure that would be
expensive and quiet:

* :class:`TestIdempotence` — every operation run twice must issue no second
  write. A spreadsheet has no transactions, so "safe to rerun" is the only
  recovery mechanism an interrupted weekly run has.
* :class:`TestManualFieldsArePreserved` — a hand-typed Industry, a hand-added
  ``Notes`` column and an existing Website must all survive an import that does
  not mention them.
* :class:`TestClosureRequiresEvidence` — a company the crawler could not read
  has none of its postings closed.
* :class:`TestIdentityIsStable` — the same company and the same posting resolve
  to the same key across spellings, encodings and URL decoration.
"""

from __future__ import annotations

import codecs
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List

from sheets.client import SheetsClient
from sheets.companies import MANUAL_FIELDS, CompanyRepository, company_record
from sheets.init import initialise
from sheets.jobs import CHANGE_CLOSED, CHANGE_NEW, CHANGE_REOPENED, JobRepository, observation
from sheets.runs import (
    STATUS_DONE,
    STATUS_RUNNING,
    DashboardRepository,
    FailureRepository,
    RunRepository,
    failure_record,
)
from sheets.schema import MASTER_COMPANIES, normalise_header
from sheets.storage import TabStore
from tests._fake_sheets import FakeSheetsService

ACME = "domain:acme.com"


def fixture(extra_tabs: Dict[str, List[List[str]]] = None) -> tuple:
    """An initialised in-memory spreadsheet and a client over it.

    Args:
        extra_tabs: Additional starting content.

    Returns:
        ``(client, service)``.
    """
    tabs = {"Sheet1": []}
    tabs.update(extra_tabs or {})
    service = FakeSheetsService(tabs)
    client = SheetsClient(service, "fake", sleep=lambda _seconds: None)
    initialise(client)
    return client, service


def company(name: str = "Acme Corporation", **extra) -> Dict[str, str]:
    """A company record ready for upsert."""
    record = company_record({"company": name, "website": "acme.com", **extra})
    assert record is not None
    return record


def job(
    key: str,
    title: str = "Engineer",
    url: str = "",
    company_key: str = ACME,
    is_tech: bool = True,
) -> Dict[str, str]:
    """An observation ready for apply.

    ``is_tech`` defaults true because these tests are about the lifecycle, not
    the filter; the filter has its own class below.
    """
    link = url or f"https://acme.com/jobs/{key}"
    return observation(
        job_key=key,
        company_key=company_key,
        company_name="Acme Corporation",
        job_title=title,
        job_url=link,
        url_key=link,
        content_key=f"content:{title.lower()}",
        platform="Greenhouse",
        location="Austin, TX",
        country="United States",
        is_tech=is_tech,
    )


class TestTabStore(unittest.TestCase):
    """The core read/write machinery."""

    def setUp(self) -> None:
        self.client, self.service = fixture()
        self.store = TabStore(self.client, MASTER_COMPANIES)

    def test_an_empty_tab_reads_as_no_records(self) -> None:
        self.assertEqual(self.store.read(), [])
        self.assertEqual(self.store.count(), 0)

    def test_insert_then_read_back(self) -> None:
        self.store.upsert([company()], key_field="company_key")

        records = self.store.read()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].get("company_name"), "Acme Corporation")
        self.assertEqual(records[0].row, 2)

    def test_update_keeps_the_row_in_place(self) -> None:
        """Row positions carry the operator's filters and formatting."""
        self.store.upsert([company("A", website="a.com"), company("B", website="b.com")],
                          key_field="company_key")
        self.store.upsert([company("A renamed", website="a.com")], key_field="company_key")

        records = self.store.read()
        self.assertEqual(records[0].get("company_name"), "A renamed")
        self.assertEqual(records[0].row, 2)
        self.assertEqual(records[1].get("company_name"), "B")

    def test_a_record_with_no_key_is_skipped(self) -> None:
        result = self.store.upsert([{"company_name": "No key"}], key_field="company_key")
        self.assertEqual(result.skipped, 1)
        self.assertEqual(self.store.count(), 0)

    def test_two_incoming_records_with_one_key_insert_once(self) -> None:
        result = self.store.upsert(
            [company("First", website="acme.com"), company("Second", website="acme.com")],
            key_field="company_key",
        )
        self.assertEqual(result.inserted, 1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(self.store.count(), 1)

    def test_read_index_keeps_the_first_of_a_duplicated_key(self) -> None:
        # Built from the specification rather than counted out by hand, so
        # adding a column to MASTER_COMPANIES does not silently move the key
        # out from under this test.
        fields = MASTER_COMPANIES.fields
        name_at = fields.index("company_name")
        key_at = fields.index("company_key")

        def row(name: str) -> list:
            cells = [""] * len(fields)
            cells[name_at] = name
            cells[key_at] = ACME
            return cells

        self.service.tabs["MASTER_COMPANIES"].append(row("Original"))
        self.service.tabs["MASTER_COMPANIES"].append(row("Later"))
        index = self.store.read_index("company_key")
        self.assertEqual(index[ACME].get("company_name"), "Original")

    def test_blank_rows_are_not_records(self) -> None:
        self.store.upsert([company()], key_field="company_key")
        self.service.tabs["MASTER_COMPANIES"].append(["", "", ""])
        self.assertEqual(self.store.count(), 1)

    def test_replace_blanks_the_surplus(self) -> None:
        self.store.replace([company("A", website="a.com"), company("B", website="b.com")])
        self.store.replace([company("A", website="a.com")])

        self.assertEqual(self.store.count(), 1)
        # The row still exists in the grid; it is empty, not deleted.
        self.assertEqual(self.service.destructive_requests(), [])

    def test_append_adds_without_checking_for_duplicates(self) -> None:
        self.store.append([company()])
        self.store.append([company()])
        self.assertEqual(self.store.count(), 2)

    def test_a_missing_tab_names_the_fix(self) -> None:
        service = FakeSheetsService({"Sheet1": []})
        client = SheetsClient(service, "fake", sleep=lambda _s: None)
        with self.assertRaises(KeyError) as caught:
            TabStore(client, MASTER_COMPANIES).read()
        self.assertIn("sheets.init", str(caught.exception))


class TestUnmanagedColumnsSurvive(unittest.TestCase):
    """A column the operator added is never written through."""

    def setUp(self) -> None:
        headers = list(MASTER_COMPANIES.headers)
        headers.insert(2, "Notes")
        self.client, self.service = fixture({"MASTER_COMPANIES": [headers]})
        self.store = TabStore(self.client, MASTER_COMPANIES)

    def test_the_column_is_recognised_as_foreign(self) -> None:
        self.assertEqual(self.store.plan.unknown_headers, ["Notes"])

    def test_writing_a_row_does_not_touch_it(self) -> None:
        self.store.upsert([company()], key_field="company_key")
        self.service.tabs["MASTER_COMPANIES"][1][2] = "call back Tuesday"

        self.store.upsert([company("Acme Renamed")], key_field="company_key")

        self.assertEqual(self.service.tabs["MASTER_COMPANIES"][1][2], "call back Tuesday")

    def test_the_write_is_split_around_it(self) -> None:
        """One block per unbroken stretch of managed columns."""
        runs = self.store.plan.contiguous_runs()
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0][0], 0)
        self.assertEqual(runs[1][0], 3)

    def test_replace_leaves_it_alone_even_when_blanking(self) -> None:
        self.store.replace([company("A", website="a.com"), company("B", website="b.com")])
        self.service.tabs["MASTER_COMPANIES"][2][2] = "keep me"

        self.store.replace([company("A", website="a.com")])

        self.assertEqual(self.service.tabs["MASTER_COMPANIES"][2][2], "keep me")


class TestIdempotence(unittest.TestCase):
    """Every operation, run twice, must issue no second write."""

    def assert_second_run_is_silent(self, operation) -> None:
        """Run an operation twice and assert the second wrote nothing.

        Args:
            operation: Callable taking the client.
        """
        client, service = fixture()
        operation(client)

        service.calls.clear()
        service.structural_kinds.clear()
        operation(client)

        self.assertEqual(service.mutating_calls(), [])

    def test_company_upsert(self) -> None:
        self.assert_second_run_is_silent(
            lambda client: CompanyRepository(client).upsert([company()])
        )

    def test_company_import(self) -> None:
        rows = [{"company": "Acme", "website": "acme.com", "career_url": ""}]
        self.assert_second_run_is_silent(
            lambda client: CompanyRepository(client).import_rows(rows)
        )

    def test_job_apply(self) -> None:
        client, service = fixture()
        jobs = JobRepository(client)
        jobs.apply([job("a")], {ACME}, run_id="run-1")

        service.calls.clear()
        jobs.apply([job("a")], {ACME}, run_id="run-1")

        self.assertEqual(service.mutating_calls(), [])

    def test_dashboard_write(self) -> None:
        sections = [("Totals", [("companies", 10), ("jobs", 42)])]
        client, service = fixture()
        DashboardRepository(client).write(sections, week_start="2026-08-24")

        service.calls.clear()
        DashboardRepository(client).write(sections, week_start="2026-08-24")

        self.assertEqual(service.mutating_calls(), [])

    def test_failure_upsert(self) -> None:
        record = failure_record(ACME, "Acme", error="HTTP 403", run_id="run-1")
        self.assert_second_run_is_silent(
            lambda client: FailureRepository(client).upsert([record])
        )

    def test_run_start_does_not_create_two_rows(self) -> None:
        client, _ = fixture()
        runs = RunRepository(client)
        runs.start(companies_total=5, run_id="run-1")
        runs.start(companies_total=5, run_id="run-1")

        self.assertEqual(len(runs.all()), 1)

    def test_repeating_a_whole_weekly_cycle_changes_nothing(self) -> None:
        client, service = fixture()

        def cycle() -> None:
            CompanyRepository(client).upsert([company()])
            JobRepository(client).apply([job("a"), job("b")], {ACME}, run_id="run-1")
            RunRepository(client).start(companies_total=1, run_id="run-1")
            RunRepository(client).finish("run-1", STATUS_DONE, {"jobs_new": 2})

        cycle()
        snapshot = {name: [list(row) for row in rows] for name, rows in service.tabs.items()}

        cycle()

        for name, rows in snapshot.items():
            self.assertEqual(service.tabs[name], rows, name)


class TestCompanyImport(unittest.TestCase):
    """Importing rows into ``MASTER_COMPANIES``."""

    def setUp(self) -> None:
        self.client, self.service = fixture()
        self.companies = CompanyRepository(self.client)

    def test_rows_become_companies(self) -> None:
        result, _ = self.companies.import_rows(
            [
                {"company": "Acme Corporation", "website": "acme.com", "career_url": ""},
                {"company": "Other Inc", "website": "other.com", "career_url": ""},
            ]
        )
        self.assertEqual(result.inserted, 2)
        self.assertEqual(self.companies.count(), 2)

    def test_two_rows_naming_one_company_collapse(self) -> None:
        result, collapsed = self.companies.import_rows(
            [
                {"company": "Acme Corp.", "website": "acme.com", "career_url": ""},
                {"company": "Acme Corporation", "website": "www.acme.com", "career_url": ""},
            ]
        )
        self.assertEqual(collapsed, 1)
        self.assertEqual(result.inserted, 1)
        self.assertEqual(self.companies.count(), 1)

    def test_the_fuller_of_two_merged_rows_wins(self) -> None:
        self.companies.import_rows(
            [
                {"company": "Acme", "website": "acme.com", "career_url": ""},
                {"company": "Acme", "website": "acme.com", "career_url": "https://acme.com/jobs"},
            ]
        )
        stored = self.companies.store.read_index("company_key")[ACME]
        self.assertEqual(stored.get("career_url"), "https://acme.com/jobs")

    def test_a_row_with_no_name_is_skipped(self) -> None:
        result, _ = self.companies.import_rows([{"company": "", "website": "acme.com"}])
        self.assertEqual(result.inserted, 0)

    def test_reimporting_after_adding_a_row_adds_only_that_row(self) -> None:
        rows = [{"company": "Acme", "website": "acme.com", "career_url": ""}]
        self.companies.import_rows(rows)

        rows.append({"company": "New Co", "website": "newco.com", "career_url": ""})
        result, _ = self.companies.import_rows(rows)

        self.assertEqual(result.inserted, 1)
        self.assertEqual(result.unchanged, 1)
        self.assertEqual(self.companies.count(), 2)

    def test_crawl_records_project_to_the_version_2_shape(self) -> None:
        """The stored list must crawl through the existing engine unchanged."""
        self.companies.import_rows(
            [{"company": "Acme", "website": "acme.com", "career_url": "https://acme.com/jobs"}]
        )
        records = self.companies.crawl_records()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["company"], "Acme")
        self.assertEqual(records[0]["career_url"], "https://acme.com/jobs")
        for required in ("company", "website", "career_url", "it_link"):
            self.assertIn(required, records[0])

    def test_a_paused_company_is_not_offered_for_crawling(self) -> None:
        self.companies.import_rows([{"company": "Acme", "website": "acme.com"}])
        self.companies.upsert([{"company_key": ACME, "status": "paused"}])
        self.assertEqual(self.companies.crawl_records(), [])

    def test_dry_run_writes_nothing(self) -> None:
        result, _ = self.companies.import_rows(
            [{"company": "Acme", "website": "acme.com"}], dry_run=True
        )
        self.assertEqual(result.inserted, 1)
        self.assertTrue(result.dry_run)
        self.assertEqual(self.companies.count(), 0)


class TestManualFieldsArePreserved(unittest.TestCase):
    """What the operator typed outranks what the import brought."""

    def setUp(self) -> None:
        self.client, self.service = fixture()
        self.companies = CompanyRepository(self.client)
        self.companies.import_rows(
            [{"company": "Acme", "website": "acme.com", "career_url": "https://acme.com/jobs"}]
        )

    def test_a_hand_typed_industry_survives_reimport(self) -> None:
        self.companies.upsert([{"company_key": ACME, "industry": "Cybersecurity"}])
        self.companies.import_rows(
            [{"company": "Acme", "website": "acme.com", "industry": "Something Else"}]
        )

        self.assertEqual(
            self.companies.store.read_index("company_key")[ACME].get("industry"), "Cybersecurity"
        )

    def test_a_hand_typed_department_survives_reimport(self) -> None:
        self.companies.upsert([{"company_key": ACME, "department": "Engineering"}])
        self.companies.import_rows([{"company": "Acme", "website": "acme.com", "department": "X"}])

        self.assertEqual(
            self.companies.store.read_index("company_key")[ACME].get("department"), "Engineering"
        )

    def test_an_empty_manual_field_is_filled_by_an_import(self) -> None:
        """Preserving is not refusing: a blank cell may be populated."""
        self.companies.import_rows(
            [{"company": "Acme", "website": "acme.com", "industry": "SaaS"}]
        )
        self.assertEqual(
            self.companies.store.read_index("company_key")[ACME].get("industry"), "SaaS"
        )

    def test_a_blank_import_value_does_not_erase_a_stored_one(self) -> None:
        """The failure that would blank 8,000 websites in one run."""
        self.companies.upsert([{"company_key": ACME, "website": ""}])

        self.assertEqual(
            self.companies.store.read_index("company_key")[ACME].get("website"),
            "https://acme.com",
        )

    def test_a_reimport_missing_a_column_does_not_erase_it(self) -> None:
        self.companies.import_rows([{"company": "Acme", "website": "acme.com"}])
        self.assertEqual(
            self.companies.store.read_index("company_key")[ACME].get("career_url"),
            "https://acme.com/jobs",
        )

    def test_manual_fields_are_declared(self) -> None:
        self.assertEqual(MANUAL_FIELDS, frozenset({"department", "industry"}))

    def test_a_non_manual_field_is_updated_by_an_import(self) -> None:
        self.companies.import_rows(
            [{"company": "Acme Renamed", "website": "acme.com"}]
        )
        self.assertEqual(
            self.companies.store.read_index("company_key")[ACME].get("company_name"),
            "Acme Renamed",
        )


class TestIdentityIsStable(unittest.TestCase):
    """One company, and one posting, however they are spelled."""

    def setUp(self) -> None:
        self.client, _ = fixture()
        self.companies = CompanyRepository(self.client)

    def test_name_variants_are_one_company(self) -> None:
        self.companies.import_rows(
            [
                {"company": "Acme Corporation", "website": "acme.com"},
                {"company": "ACME Corp.", "website": "https://www.acme.com/"},
                {"company": "Acme, Inc.", "website": "http://acme.com/about"},
            ]
        )
        self.assertEqual(self.companies.count(), 1)

    def test_accented_and_unaccented_names_agree(self) -> None:
        self.companies.import_rows(
            [
                {"company": "Nestlé S.A.", "website": ""},
                {"company": "Nestle SA", "website": ""},
            ]
        )
        self.assertEqual(self.companies.count(), 1)

    def test_a_unicode_name_round_trips_intact(self) -> None:
        self.companies.import_rows(
            [{"company": "Schrödinger Analyse GmbH", "website": "schrodinger.example"}]
        )
        stored = self.companies.all()[0]
        self.assertEqual(stored.get("company_name"), "Schrödinger Analyse GmbH")

    def test_a_non_breaking_space_in_a_name_does_not_split_a_company(self) -> None:
        """The cp1252 0xA0 that used to stop a run, arriving as text."""
        self.companies.import_rows(
            [
                {"company": "Acme Corp", "website": "acme.com"},
                {"company": "Acme Corp", "website": "acme.com"},
            ]
        )
        self.assertEqual(self.companies.count(), 1)

    def test_a_shared_ats_host_does_not_merge_two_companies(self) -> None:
        self.companies.import_rows(
            [
                {"company": "Acme", "website": "", "career_url": "https://boards.greenhouse.io/acme"},
                {"company": "Other", "website": "", "career_url": "https://boards.greenhouse.io/other"},
            ]
        )
        self.assertEqual(self.companies.count(), 2)

    def test_urls_are_normalised_on_the_way_in(self) -> None:
        self.companies.import_rows(
            [
                {
                    "company": "Acme",
                    "website": "WWW.Acme.com",
                    "career_url": "https://acme.com/jobs/?utm_source=news",
                }
            ]
        )
        stored = self.companies.all()[0]
        self.assertEqual(stored.get("website"), "https://acme.com")
        self.assertEqual(stored.get("career_url"), "https://acme.com/jobs")

    def test_filler_in_a_url_column_is_left_visible(self) -> None:
        """Rewriting it would hide a bad cell from the operator."""
        self.companies.import_rows(
            [{"company": "Acme", "website": "acme.com", "career_url": "N/A"}]
        )
        self.assertEqual(self.companies.all()[0].get("career_url"), "N/A")


class TestJobLifecycle(unittest.TestCase):
    """A posting through new, unchanged, closed and reopened."""

    def setUp(self) -> None:
        self.client, self.service = fixture()
        self.jobs = JobRepository(self.client)

    def test_first_run_reports_everything_new(self) -> None:
        applied = self.jobs.apply([job("a"), job("b")], {ACME}, run_id="run-1")

        self.assertEqual(len(applied.changes.new_jobs), 2)
        self.assertEqual(self.jobs.history.count(), 2)
        self.assertEqual(self.jobs.current.count(), 2)

    def test_a_second_sighting_is_not_new(self) -> None:
        self.jobs.apply([job("a")], {ACME}, run_id="run-1")
        applied = self.jobs.apply([job("a")], {ACME}, run_id="run-2")

        self.assertEqual(applied.changes.new_jobs, [])
        self.assertEqual(len(applied.changes.still_active), 1)

    def test_first_seen_never_moves(self) -> None:
        self.jobs.apply([job("a")], {ACME}, run_id="run-1")
        original = self.jobs.history.read_index("job_key")["a"].get("first_seen")

        self.jobs.apply([job("a")], {ACME}, run_id="run-2")
        stored = self.jobs.history.read_index("job_key")["a"]

        self.assertEqual(stored.get("first_seen"), original)
        self.assertEqual(stored.get("first_run_id"), "run-1")
        self.assertEqual(stored.get("last_run_id"), "run-2")

    def test_a_vanished_posting_is_closed_not_removed(self) -> None:
        self.jobs.apply([job("a")], {ACME}, run_id="run-1")
        self.jobs.apply([], {ACME}, run_id="run-2")

        stored = self.jobs.history.read_index("job_key")["a"]
        self.assertEqual(stored.get("status"), "closed")
        self.assertTrue(stored.get("closed_at"))
        self.assertEqual(self.jobs.history.count(), 1)
        self.assertEqual(self.jobs.current.count(), 0)

    def test_closed_at_is_stamped_once(self) -> None:
        self.jobs.apply([job("a")], {ACME}, run_id="run-1")
        self.jobs.apply([], {ACME}, run_id="run-2")
        first = self.jobs.history.read_index("job_key")["a"].get("closed_at")

        self.jobs.apply([], {ACME}, run_id="run-3")
        self.assertEqual(self.jobs.history.read_index("job_key")["a"].get("closed_at"), first)

    def test_a_reopened_posting_keeps_its_original_first_seen(self) -> None:
        self.jobs.apply([job("a")], {ACME}, run_id="run-1")
        original = self.jobs.history.read_index("job_key")["a"].get("first_seen")
        self.jobs.apply([], {ACME}, run_id="run-2")

        applied = self.jobs.apply([job("a")], {ACME}, run_id="run-3")

        self.assertEqual(len(applied.changes.reopened_jobs), 1)
        stored = self.jobs.history.read_index("job_key")["a"]
        self.assertEqual(stored.get("status"), "active")
        self.assertEqual(stored.get("closed_at"), "")
        self.assertEqual(stored.get("first_seen"), original)

    def test_the_current_view_is_replaced_not_appended(self) -> None:
        self.jobs.apply([job("a"), job("b")], {ACME}, run_id="run-1")
        self.jobs.apply([job("a")], {ACME}, run_id="run-2")

        self.assertEqual(self.jobs.current.count(), 1)
        self.assertEqual(self.jobs.current.read()[0].get("job_key"), "a")

    def test_a_duplicate_observation_yields_one_row(self) -> None:
        self.jobs.apply([job("a"), job("a")], {ACME}, run_id="run-1")
        self.assertEqual(self.jobs.history.count(), 1)
        self.assertEqual(self.jobs.current.count(), 1)

    def test_history_never_grows_a_second_row_for_one_posting(self) -> None:
        for index in range(4):
            self.jobs.apply([job("a")], {ACME}, run_id=f"run-{index}")
        self.assertEqual(self.jobs.history.count(), 1)


class TestTechFilterAppliesToCurrentJobsOnly(unittest.TestCase):
    """The narrowing that keeps ``CURRENT_JOBS`` readable, and its limit."""

    def setUp(self) -> None:
        self.client, _ = fixture()
        self.jobs = JobRepository(self.client)

    def test_non_technology_postings_are_kept_out_of_the_current_view(self) -> None:
        self.jobs.apply(
            [job("a", "Software Engineer"), job("b", "Welder", is_tech=False)],
            {ACME},
            run_id="run-1",
        )
        self.assertEqual(self.jobs.current.count(), 1)
        self.assertEqual(self.jobs.current.read()[0].get("job_title"), "Software Engineer")

    def test_the_ledger_keeps_everything(self) -> None:
        """Nothing is lost, so the filter can change without re-crawling."""
        self.jobs.apply(
            [job("a", "Software Engineer"), job("b", "Welder", is_tech=False)],
            {ACME},
            run_id="run-1",
        )
        self.assertEqual(self.jobs.history.count(), 2)

    def test_the_weekly_diff_sees_every_posting(self) -> None:
        applied = self.jobs.apply(
            [job("a", "Software Engineer"), job("b", "Welder", is_tech=False)],
            {ACME},
            run_id="run-1",
        )
        self.assertEqual(len(applied.changes.new_jobs), 2)

    def test_the_filter_can_be_turned_off(self) -> None:
        self.jobs.apply(
            [job("a", "Software Engineer"), job("b", "Welder", is_tech=False)],
            {ACME},
            run_id="run-1",
            tech_only=False,
        )
        self.assertEqual(self.jobs.current.count(), 2)

    def test_a_posting_that_stops_being_technical_leaves_the_current_view(self) -> None:
        self.jobs.apply([job("a", "Software Engineer")], {ACME}, run_id="run-1")
        self.assertEqual(self.jobs.current.count(), 1)

        self.jobs.apply([job("a", "Welder", is_tech=False)], {ACME}, run_id="run-2")
        self.assertEqual(self.jobs.current.count(), 0)
        self.assertEqual(self.jobs.history.count(), 1)


class TestClosureRequiresEvidence(unittest.TestCase):
    """A company that could not be read has nothing concluded about it."""

    def setUp(self) -> None:
        self.client, _ = fixture()
        self.jobs = JobRepository(self.client)
        self.jobs.apply([job("a"), job("b")], {ACME}, run_id="run-1")

    def test_a_blocked_company_closes_nothing(self) -> None:
        applied = self.jobs.apply([], set(), run_id="run-2")

        self.assertEqual(applied.changes.closed_jobs, [])
        self.assertEqual(applied.changes.skipped_closures, 2)
        self.assertEqual(self.jobs.active_count(), 2)

    def test_an_empty_board_that_was_read_does_close(self) -> None:
        applied = self.jobs.apply([], {ACME}, run_id="run-2")
        self.assertEqual(len(applied.changes.closed_jobs), 2)

    def test_a_blocked_week_leaves_no_scar(self) -> None:
        self.jobs.apply([], set(), run_id="run-2")
        applied = self.jobs.apply([job("a"), job("b")], {ACME}, run_id="run-3")

        self.assertEqual(applied.changes.new_jobs, [])
        self.assertEqual(applied.changes.closed_jobs, [])
        self.assertEqual(len(applied.changes.still_active), 2)

    def test_another_companys_jobs_are_unaffected(self) -> None:
        other = "domain:other.com"
        self.jobs.apply(
            [job("a"), job("b"), job("c", company_key=other)], {ACME, other}, run_id="run-2"
        )
        applied = self.jobs.apply([job("c", company_key=other)], {other}, run_id="run-3")

        self.assertEqual(applied.changes.closed_jobs, [])
        self.assertEqual(applied.changes.skipped_closures, 2)


class TestRelinking(unittest.TestCase):
    """A posting whose identity moved is the posting it already was."""

    def setUp(self) -> None:
        self.client, _ = fixture()
        self.jobs = JobRepository(self.client)

    def test_a_rewritten_url_does_not_churn(self) -> None:
        self.jobs.apply([job("a", "Engineer")], {ACME}, run_id="run-1")
        original = self.jobs.history.read_index("job_key")["a"].get("first_seen")

        moved = job("a-new", "Engineer", url="https://acme.com/careers/engineer")
        applied = self.jobs.apply([moved], {ACME}, run_id="run-2")

        self.assertEqual(len(applied.changes.relinked), 1)
        self.assertEqual(applied.changes.new_jobs, [])
        self.assertEqual(applied.changes.closed_jobs, [])

        stored = self.jobs.history.read_index("job_key")
        self.assertIn("a-new", stored)
        self.assertEqual(stored["a-new"].get("first_seen"), original)

    def test_the_old_identity_does_not_stay_active(self) -> None:
        self.jobs.apply([job("a", "Engineer")], {ACME}, run_id="run-1")
        moved = job("a-new", "Engineer", url="https://acme.com/careers/engineer")
        self.jobs.apply([moved], {ACME}, run_id="run-2")

        self.assertEqual(self.jobs.active_count(), 1)


class TestWeeklyChangeLog(unittest.TestCase):
    """``NEW_LAST_WEEK`` accumulates, but never twice for one run."""

    def setUp(self) -> None:
        self.client, _ = fixture()
        self.jobs = JobRepository(self.client)

    def test_new_jobs_are_logged(self) -> None:
        self.jobs.apply([job("a"), job("b")], {ACME}, run_id="run-1")

        rows = self.jobs.weekly.read()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.get("change") for row in rows}, {CHANGE_NEW})

    def test_closures_and_reopenings_are_logged(self) -> None:
        self.jobs.apply([job("a")], {ACME}, run_id="run-1")
        self.jobs.apply([], {ACME}, run_id="run-2")
        self.jobs.apply([job("a")], {ACME}, run_id="run-3")

        changes = [row.get("change") for row in self.jobs.weekly.read()]
        self.assertEqual(changes, [CHANGE_NEW, CHANGE_CLOSED, CHANGE_REOPENED])

    def test_repeating_a_run_logs_once(self) -> None:
        self.jobs.apply([job("a")], {ACME}, run_id="run-1")
        applied = self.jobs.apply([job("a")], {ACME}, run_id="run-1")

        self.assertTrue(applied.already_logged)
        self.assertEqual(len(self.jobs.weekly.read()), 1)

    def test_a_closed_posting_logs_its_title(self) -> None:
        """The observation is gone by then, so the title comes from history."""
        self.jobs.apply([job("a", "Platform Engineer")], {ACME}, run_id="run-1")
        self.jobs.apply([], {ACME}, run_id="run-2")

        closed = [row for row in self.jobs.weekly.read() if row.get("change") == CHANGE_CLOSED]
        self.assertEqual(closed[0].get("job_title"), "Platform Engineer")

    def test_a_quiet_week_logs_nothing(self) -> None:
        self.jobs.apply([job("a")], {ACME}, run_id="run-1")
        self.jobs.apply([job("a")], {ACME}, run_id="run-2")
        self.assertEqual(len(self.jobs.weekly.read()), 1)


class TestRunRepository(unittest.TestCase):
    """``WEEKLY_RUNS``."""

    def setUp(self) -> None:
        self.client, _ = fixture()
        self.runs = RunRepository(self.client)

    def test_start_and_finish(self) -> None:
        run = self.runs.start(companies_total=10, run_id="run-1")
        self.assertEqual(run.status, STATUS_RUNNING)

        self.runs.finish("run-1", STATUS_DONE, {"jobs_new": 5, "companies_checked": 10})
        stored = self.runs.get("run-1")

        self.assertTrue(stored.is_finished)
        self.assertEqual(stored.counts["jobs_new"], 5)
        self.assertTrue(stored.finished_at)

    def test_duration_is_recorded(self) -> None:
        self.runs.start(companies_total=1, run_id="run-1")
        self.runs.finish("run-1", STATUS_DONE)
        row = self.runs.store.read_index("run_id")["run-1"]
        self.assertTrue(row.get("duration"))

    def test_success_rate_is_computed(self) -> None:
        self.runs.start(companies_total=10, run_id="run-1")
        self.runs.finish("run-1", STATUS_DONE, {"companies_checked": 10, "companies_succeeded": 8})
        self.assertEqual(self.runs.store.read_index("run_id")["run-1"].get("success_rate"), "80.0%")

    def test_counts_accumulate_across_updates(self) -> None:
        self.runs.start(companies_total=10, run_id="run-1")
        self.runs.update_counts("run-1", {"companies_checked": 4})
        self.runs.finish("run-1", STATUS_DONE, {"jobs_new": 2})

        stored = self.runs.get("run-1")
        self.assertEqual(stored.counts["companies_checked"], 4)
        self.assertEqual(stored.counts["jobs_new"], 2)

    def test_restarting_a_run_keeps_its_original_start_time(self) -> None:
        first = self.runs.start(companies_total=10, run_id="run-1")
        second = self.runs.start(companies_total=10, run_id="run-1")
        self.assertEqual(second.started_at, first.started_at)
        self.assertEqual(len(self.runs.all()), 1)

    def test_an_interrupted_run_is_resumed_rather_than_replaced(self) -> None:
        first = self.runs.start(companies_total=10, run_id="run-1")
        self.runs.finish("run-1", "interrupted")

        resumed = self.runs.start(companies_total=10, run_id="run-1")

        self.assertEqual(resumed.started_at, first.started_at)
        self.assertEqual(self.runs.get("run-1").status, STATUS_RUNNING)
        self.assertEqual(len(self.runs.all()), 1)

    def test_a_completed_run_is_not_reopened(self) -> None:
        """Replaying a finished run converges; it does not re-time it."""
        self.runs.start(companies_total=10, run_id="run-1")
        self.runs.finish("run-1", STATUS_DONE, {"jobs_new": 5})
        finished_at = self.runs.get("run-1").finished_at

        replayed = self.runs.start(companies_total=10, run_id="run-1")

        self.assertEqual(replayed.status, STATUS_DONE)
        self.assertEqual(self.runs.get("run-1").finished_at, finished_at)
        self.assertEqual(self.runs.get("run-1").counts["jobs_new"], 5)

    def test_last_completed_ignores_an_unfinished_run(self) -> None:
        self.runs.start(companies_total=1, run_id="run-1")
        self.runs.finish("run-1", STATUS_DONE)
        self.runs.start(companies_total=1, run_id="run-2")

        self.assertEqual(self.runs.last_completed().run_id, "run-1")

    def test_an_unfinished_run_this_week_is_resumable(self) -> None:
        self.runs.start(companies_total=1, run_id="run-1")
        self.assertEqual(self.runs.resumable().run_id, "run-1")

    def test_a_finished_run_is_not_resumable(self) -> None:
        self.runs.start(companies_total=1, run_id="run-1")
        self.runs.finish("run-1", STATUS_DONE)
        self.assertIsNone(self.runs.resumable())


class TestFailureRepository(unittest.TestCase):
    """``FAILURES``."""

    def setUp(self) -> None:
        self.client, _ = fixture()
        self.failures = FailureRepository(self.client)

    def test_a_cloudflare_403_is_classified(self) -> None:
        record = failure_record(
            ACME, "Acme",
            error="AdapterHttpError: GET https://x returned HTTP 403: 'Just a moment...'",
            run_id="run-1",
        )
        self.assertEqual(record["failure_type"], "cloudflare challenge")
        self.assertEqual(record["retryable"], "TRUE")

    def test_a_bad_sheet_url_is_not_a_block(self) -> None:
        record = failure_record(
            ACME, "Acme", error="AdapterUrlError: No Asure company id in 'https://x'", run_id="r"
        )
        self.assertEqual(record["failure_type"], "unusable board url")
        self.assertEqual(record["retryable"], "FALSE")

    def test_the_detail_is_trimmed(self) -> None:
        record = failure_record(ACME, error="x" * 5000)
        self.assertLessEqual(len(record["detail"]), 500)

    def test_one_company_failing_twice_occupies_one_row(self) -> None:
        self.failures.upsert([failure_record(ACME, "Acme", error="HTTP 403", run_id="run-1")])
        self.failures.upsert([failure_record(ACME, "Acme", error="HTTP 429", run_id="run-1")])

        self.assertEqual(self.failures.count(), 1)
        self.assertEqual(
            self.failures.store.read_index("company_key")[ACME].get("failure_type"),
            "429 rate limited",
        )

    def test_replace_clears_the_previous_run(self) -> None:
        self.failures.replace(
            [
                failure_record(ACME, "Acme", error="HTTP 403", run_id="run-1"),
                failure_record("domain:b.com", "B", error="HTTP 404", run_id="run-1"),
            ]
        )
        self.failures.replace([failure_record(ACME, "Acme", error="HTTP 403", run_id="run-2")])

        self.assertEqual(self.failures.count(), 1)

    def test_counts_by_type(self) -> None:
        self.failures.replace(
            [
                failure_record(ACME, error="HTTP 403", run_id="r"),
                failure_record("domain:b.com", error="HTTP 403", run_id="r"),
                failure_record("domain:c.com", error="HTTP 404", run_id="r"),
            ]
        )
        counts = self.failures.counts_by_type()
        self.assertEqual(counts["403 forbidden"], 2)
        self.assertEqual(counts["404 not found"], 1)


class TestDashboardRepository(unittest.TestCase):
    """``DASHBOARD`` — the data layer only."""

    def setUp(self) -> None:
        self.client, _ = fixture()
        self.dashboard = DashboardRepository(self.client)

    def test_metrics_are_written_and_read_back(self) -> None:
        self.dashboard.write(
            [
                ("Companies", [("total", 8275), ("checked", 8000)]),
                ("Jobs", [("active", 42000), ("new this week", 143)]),
            ],
            week_start="2026-08-24",
        )

        metrics = self.dashboard.read_metrics()
        self.assertEqual(metrics["total"], "8275")
        self.assertEqual(metrics["new this week"], "143")

    def test_a_rewrite_replaces_rather_than_appends(self) -> None:
        self.dashboard.write([("A", [("x", 1), ("y", 2)])])
        self.dashboard.write([("A", [("x", 9)])])

        self.assertEqual(self.dashboard.store.count(), 1)
        self.assertEqual(self.dashboard.read_metrics()["x"], "9")

    def test_none_becomes_blank_not_the_word_none(self) -> None:
        self.dashboard.write([("A", [("x", None)])])
        self.assertEqual(self.dashboard.store.read()[0].get("value"), "")


class TestCsvImportIntegration(unittest.TestCase):
    """The real CSV reader feeding the real repository."""

    def setUp(self) -> None:
        self.client, _ = fixture()
        self.companies = CompanyRepository(self.client)
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def write_csv(self, body: str, encoding: str = "utf-8") -> Path:
        """Write a CSV fixture in a given encoding."""
        path = self.directory / "companies.csv"
        path.write_bytes(body.encode(encoding))
        return path

    def test_a_utf8_csv_imports(self) -> None:
        path = self.write_csv(
            "Company Name,Website,Career Page URL\n"
            "Acme Corporation,acme.com,https://acme.com/careers\n"
        )
        result, _, encoding = self.companies.import_csv(path)

        self.assertEqual(result.inserted, 1)
        self.assertIn("utf-8", encoding)

    def test_a_windows_1252_csv_imports(self) -> None:
        """The 0xA0 that used to stop a run, all the way into the sheet."""
        body = (
            "Company Name,Website,Career Page URL\n"
            "Acme Corp,acme.com,https://acme.com/careers\n"
        )
        path = self.write_csv(body, encoding="cp1252")

        with self.assertRaises(UnicodeDecodeError):
            path.read_bytes().decode("utf-8")

        result, _, encoding = self.companies.import_csv(path)

        self.assertEqual(result.inserted, 1)
        self.assertEqual(encoding, "cp1252")
        self.assertEqual(self.companies.all()[0].get("company_name"), "Acme Corp")

    def test_a_csv_with_a_byte_order_mark_imports(self) -> None:
        path = self.directory / "companies.csv"
        path.write_bytes(
            codecs.BOM_UTF8
            + b"Company Name,Website,Career Page URL\nAcme,acme.com,https://acme.com/careers\n"
        )
        result, _, _ = self.companies.import_csv(path)
        self.assertEqual(result.inserted, 1)

    def test_importing_the_same_csv_twice_changes_nothing(self) -> None:
        path = self.write_csv(
            "Company Name,Website,Career Page URL\n"
            "Acme Corporation,acme.com,https://acme.com/careers\n"
            "Other Inc,other.com,https://other.com/jobs\n"
        )
        self.companies.import_csv(path)
        result, _, _ = self.companies.import_csv(path)

        self.assertEqual(result.inserted, 0)
        self.assertEqual(result.unchanged, 2)
        self.assertEqual(self.companies.count(), 2)

    def test_the_source_file_is_not_modified(self) -> None:
        body = "Company Name,Website,Career Page URL\nAcme Corp,acme.com,x\n"
        path = self.write_csv(body, encoding="cp1252")
        original = path.read_bytes()

        self.companies.import_csv(path)

        self.assertEqual(path.read_bytes(), original)

    def test_a_missing_column_raises_a_clear_error(self) -> None:
        path = self.write_csv("Company Name,Website\nAcme,acme.com\n")
        with self.assertRaises(ValueError) as caught:
            self.companies.import_csv(path)
        self.assertIn("Career Page URL", str(caught.exception))


class TestNothingIsDestroyed(unittest.TestCase):
    """No storage operation may emit a deletion."""

    def test_a_full_cycle_emits_no_destructive_request(self) -> None:
        client, service = fixture({"Quarterly Plan": [["Q1"], ["revenue"]]})

        CompanyRepository(client).import_rows([{"company": "Acme", "website": "acme.com"}])
        jobs = JobRepository(client)
        jobs.apply([job("a"), job("b")], {ACME}, run_id="run-1")
        jobs.apply([job("a")], {ACME}, run_id="run-2")
        RunRepository(client).start(companies_total=1, run_id="run-1")
        RunRepository(client).finish("run-1", STATUS_DONE)
        FailureRepository(client).replace([failure_record(ACME, error="HTTP 403", run_id="run-1")])
        DashboardRepository(client).write([("A", [("x", 1)])])

        self.assertEqual(service.destructive_requests(), [])
        self.assertEqual(service.tabs["Quarterly Plan"], [["Q1"], ["revenue"]])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
