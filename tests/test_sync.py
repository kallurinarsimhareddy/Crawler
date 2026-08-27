"""Sheet ↔ SQLite synchronisation, and the separation of the two queues.

Every test runs against a fake spreadsheet and an in-memory database. Nothing
reaches Google.

Two rules dominate: a sync must never lose what an operator curated, and
running it twice with nothing changed must write nothing at all.
"""

from __future__ import annotations

import unittest

from crawler.sync import SheetSync, SyncPlan
from sheets.client import SheetsClient
from sheets.companies import CompanyRepository as SheetCompanies
from sheets.init import initialise
from store import Database, migrate
from store.queue import CrawlQueue, DiscoveryQueue, QueueState
from store.repositories import CompanyRepository, JobRepository
from tests._fake_sheets import FakeSheetsService


def fixture(rows=()):
    """An initialised fake sheet, a client over it, and a fresh database."""
    service = FakeSheetsService({"Sheet1": []})
    client = SheetsClient(service, "fake", sleep=lambda _s: None)
    initialise(client)
    if rows:
        SheetCompanies(client).import_rows(list(rows))
    database = Database(":memory:")
    migrate(database)
    return client, service, database


class TestSheetToDatabase(unittest.TestCase):
    """Pulling the operator's list into the crawler's own store."""

    def setUp(self) -> None:
        self.client, self.service, self.database = fixture(
            [
                {"company": "Acme", "website": "https://acme.com"},
                {"company": "Bravo", "website": "https://bravo.com"},
            ]
        )
        self.sync = SheetSync(self.client, self.database)
        # Initialising the tabs and importing rows are legitimate setup writes.
        # What matters is that the sync itself adds none.
        self.baseline = len(self.service.mutating_calls())

    def test_companies_are_imported(self) -> None:
        self.sync.pull()
        self.assertEqual(CompanyRepository(self.database).count(), 2)

    def test_the_company_key_is_carried_across_unchanged(self) -> None:
        self.sync.pull()
        stored = CompanyRepository(self.database).get("domain:acme.com")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["company_key"], "domain:acme.com")

    def test_pulling_twice_changes_nothing(self) -> None:
        self.sync.pull()
        second = self.sync.pull()
        self.assertEqual(second.written, 0)

    def test_a_stored_board_survives_a_pull_that_omits_it(self) -> None:
        """The sheet is authoritative for companies, not for what we learned."""
        self.sync.pull()
        CompanyRepository(self.database).set_board(
            "domain:acme.com", "https://boards.greenhouse.io/acme", "Greenhouse"
        )
        self.sync.pull()
        self.assertEqual(
            CompanyRepository(self.database).get("domain:acme.com")["it_link"],
            "https://boards.greenhouse.io/acme",
        )

    def test_the_sheet_row_is_remembered_for_writing_back(self) -> None:
        self.sync.pull()
        self.assertGreater(
            CompanyRepository(self.database).get("domain:acme.com")["sheet_row"], 0
        )

    def test_a_pull_makes_no_sheet_writes(self) -> None:
        self.sync.pull()
        self.assertEqual(len(self.service.mutating_calls()), self.baseline)


class TestDatabaseToSheet(unittest.TestCase):
    """Pushing discovered boards back, under the existing safety rules."""

    def setUp(self) -> None:
        self.client, self.service, self.database = fixture(
            [{"company": "Acme", "website": "https://acme.com"}]
        )
        self.sync = SheetSync(self.client, self.database)
        self.sync.pull()
        self.companies = CompanyRepository(self.database)
        self.baseline = len(self.service.mutating_calls())

    def stored_row(self):
        """The company as the sheet now holds it."""
        return SheetCompanies(self.client).store.read_index("company_key")[
            "domain:acme.com"
        ]

    def test_a_dry_run_writes_nothing(self) -> None:
        self.companies.set_board(
            "domain:acme.com", "https://boards.greenhouse.io/acme", "Greenhouse"
        )
        plan = self.sync.push(dry_run=True)

        self.assertEqual(len(plan.updates), 1)
        self.assertEqual(len(self.service.mutating_calls()), self.baseline)

    def test_apply_writes_the_board(self) -> None:
        self.companies.set_board(
            "domain:acme.com", "https://boards.greenhouse.io/acme", "Greenhouse"
        )
        self.sync.push(dry_run=False)

        self.assertEqual(
            self.stored_row().get("it_link"), "https://boards.greenhouse.io/acme"
        )

    def test_only_the_two_approved_fields_are_written(self) -> None:
        self.companies.set_board(
            "domain:acme.com", "https://boards.greenhouse.io/acme", "Greenhouse"
        )
        plan = self.sync.push(dry_run=True)

        for update in plan.updates:
            self.assertEqual(set(update["fields"]), {"it_link", "platform"})

    def test_an_existing_sheet_link_is_never_overwritten(self) -> None:
        """The operator's entry wins over anything discovery found."""
        sheet = SheetCompanies(self.client)
        row = sheet.store.read_index("company_key")["domain:acme.com"].row
        sheet.store.update_rows(
            {row: {"it_link": "https://jobs.lever.co/acme", "platform": "Lever"}}
        )

        self.companies.set_board(
            "domain:acme.com", "https://boards.greenhouse.io/acme", "Greenhouse"
        )
        plan = self.sync.push(dry_run=False)

        self.assertEqual(self.stored_row().get("it_link"), "https://jobs.lever.co/acme")
        self.assertEqual(len(plan.updates), 0)

    def test_pushing_twice_is_idempotent(self) -> None:
        self.companies.set_board(
            "domain:acme.com", "https://boards.greenhouse.io/acme", "Greenhouse"
        )
        self.sync.push(dry_run=False)
        before = len(self.service.mutating_calls())

        plan = self.sync.push(dry_run=False)

        self.assertEqual(len(plan.updates), 0)
        self.assertEqual(len(self.service.mutating_calls()), before)

    def test_nothing_to_push_makes_no_calls(self) -> None:
        plan = self.sync.push(dry_run=False)
        self.assertEqual(len(plan.updates), 0)
        self.assertEqual(len(self.service.mutating_calls()), self.baseline)

    def test_a_company_key_is_never_written(self) -> None:
        self.companies.set_board(
            "domain:acme.com", "https://boards.greenhouse.io/acme", "Greenhouse"
        )
        plan = self.sync.push(dry_run=True)
        for update in plan.updates:
            self.assertNotIn("company_key", update["fields"])

    def test_no_destructive_request_is_ever_made(self) -> None:
        self.companies.set_board(
            "domain:acme.com", "https://boards.greenhouse.io/acme", "Greenhouse"
        )
        self.sync.push(dry_run=False)
        self.assertEqual(self.service.destructive_requests(), [])


class TestQueuesAreSeparate(unittest.TestCase):
    """A weekly crawl must never silently trigger discovery.

    Discovery renders pages in a browser and costs minutes per company. Folding
    it into the crawl is how a six-minute run becomes a twenty-hour one without
    anybody choosing that.
    """

    def setUp(self) -> None:
        self.database = Database(":memory:")
        migrate(self.database)
        CompanyRepository(self.database).upsert_many(
            [
                {"company_key": "domain:a.com", "company_name": "A",
                 "website": "https://a.com", "it_link": "https://boards.greenhouse.io/a"},
                {"company_key": "domain:b.com", "company_name": "B",
                 "website": "https://b.com", "it_link": ""},
            ]
        )

    def test_they_are_different_tables(self) -> None:
        CrawlQueue(self.database).enqueue_all()
        self.assertEqual(sum(DiscoveryQueue(self.database).stats().values()), 0)

    def test_the_crawl_queue_holds_every_company(self) -> None:
        """Including ones with no board: the engine can still try the website."""
        self.assertEqual(CrawlQueue(self.database).enqueue_all(), 2)

    def test_discovery_queues_only_companies_lacking_a_board(self) -> None:
        added = DiscoveryQueue(self.database).enqueue_all(only_missing_board=True)
        self.assertEqual(added, 1)

    def test_claiming_crawl_work_does_not_touch_discovery(self) -> None:
        crawl = CrawlQueue(self.database)
        discovery = DiscoveryQueue(self.database)
        crawl.enqueue_all()
        discovery.enqueue_all(only_missing_board=True)

        crawl.claim("worker-1", limit=5)

        self.assertEqual(discovery.stats()[QueueState.RUNNING], 0)
        self.assertEqual(discovery.stats()[QueueState.PENDING], 1)

    def test_finishing_discovery_does_not_advance_the_crawl(self) -> None:
        crawl = CrawlQueue(self.database)
        discovery = DiscoveryQueue(self.database)
        crawl.enqueue_all()
        discovery.enqueue_all(only_missing_board=True)
        discovery.claim("worker-1", limit=1)
        discovery.resolve("domain:b.com", "https://jobs.lever.co/b", "Lever")

        self.assertEqual(crawl.stats()[QueueState.PENDING], 2)

    def test_discovery_records_a_refusal_distinctly(self) -> None:
        discovery = DiscoveryQueue(self.database)
        discovery.enqueue_all(only_missing_board=True)
        discovery.claim("worker-1", limit=1)
        discovery.refuse("domain:b.com", "Indeed is an aggregator",
                         "https://indeed.com/cmp/b")

        row = discovery.get("domain:b.com")
        self.assertEqual(row["state"], "refused")
        self.assertIn("aggregator", row["reason"])

    def test_a_refusal_is_not_retried(self) -> None:
        discovery = DiscoveryQueue(self.database)
        discovery.enqueue_all(only_missing_board=True)
        discovery.claim("worker-1", limit=1)
        discovery.refuse("domain:b.com", "aggregator")

        self.assertEqual(discovery.claim("worker-2", limit=5), [])

    def test_the_weekly_crawl_module_does_not_import_discovery(self) -> None:
        """The structural guarantee, asserted rather than assumed."""
        from pathlib import Path

        import crawler.weekly_run as weekly

        source = Path(weekly.__file__).read_text(encoding="utf-8")
        self.assertNotIn("ats_discovery", source)
        self.assertNotIn("DiscoveryQueue", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
