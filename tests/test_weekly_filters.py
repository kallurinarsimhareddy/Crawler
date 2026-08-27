"""Unit tests for wiring :mod:`crawler.job_filters` into the weekly run.

The module itself is tested in :mod:`tests.test_job_filters`; what is tested
here is the *integration*, and the requirement that carries it is a negative
one: a run that does not ask for filters must behave exactly as it did before
this existed. So the first class asserts absence — no detector call, no request,
no cell touched — and everything after it asserts what switching it on adds.

Everything runs offline. The spreadsheet is the in-memory fake, the crawl engine
returns canned results, and the detector is injected, so no test reaches the
network, the browser, or a real board.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from config.settings import SETTINGS, configure
from crawler.crawler_engine import CrawlResult
from crawler.job_filters import (
    DetectionMethod,
    FilterOption,
    FilterSet,
    FilterType,
    JobFilter,
)
from crawler.platform_detector import Platform
from crawler.weekly_run import WeeklyRun
from models.job import Job
from sheets.client import SheetsClient
from sheets.companies import CompanyRepository
from sheets.init import initialise
from sheets.schema import MASTER_COMPANIES
from tests._fake_sheets import FakeSheetsService

BOARD = "https://boards.greenhouse.io/acme"


def posting(title: str = "Software Engineer") -> Job:
    """A version 2 job record."""
    return Job(
        company_name="Acme Corporation",
        job_title=title,
        job_url="https://boards.greenhouse.io/acme/jobs/1",
        location="Austin, TX",
        country="United States",
        career_page_url=BOARD,
        platform="Greenhouse",
    )


def result(
    jobs: Sequence[Job] = (),
    error: Optional[str] = None,
    seed_url: str = BOARD,
) -> CrawlResult:
    """A version 2 crawl result."""
    return CrawlResult(
        company="Acme Corporation",
        platform=Platform.GREENHOUSE,
        seed_url=seed_url,
        seed_field="career_url",
        jobs=list(jobs),
        error=error,
    )


def department_filter(*labels: str) -> FilterSet:
    """A board offering one department control with the given options."""
    return FilterSet(
        filters=[
            JobFilter(
                label="Department",
                filter_type=FilterType.DEPARTMENT,
                options=[FilterOption(label=label, value=label) for label in labels],
                method=DetectionMethod.SELECT,
                confidence=0.9,
                parameter="department",
            )
        ],
        source_url=BOARD,
        methods=[DetectionMethod.SELECT],
    )


class FakeEngine:
    """Returns canned results and records what it was asked for.

    Args:
        by_company: Company key to the result to return.
    """

    def __init__(self, by_company: Optional[Dict[str, CrawlResult]] = None) -> None:
        self.by_company = dict(by_company or {})
        self.crawled: List[str] = []

    def crawl_all(
        self,
        records: Sequence[Any],
        max_workers: int = 0,
    ) -> List[CrawlResult]:
        """Return one result per record."""
        produced: List[CrawlResult] = []
        for record in records:
            key = str(record.get("company_key") or "")
            self.crawled.append(key)
            produced.append(self.by_company.get(key) or result(jobs=[]))
        return produced


class RecordingDetector:
    """A filter detector that answers from a table and records every call.

    Args:
        by_url: Board URL to what should be found there.
        raises: URLs that should raise instead of answering.
    """

    def __init__(
        self,
        by_url: Optional[Dict[str, FilterSet]] = None,
        raises: Iterable[str] = (),
    ) -> None:
        self.by_url = dict(by_url or {})
        self.raises = set(raises)
        self.calls: List[str] = []
        self._lock = threading.Lock()

    def __call__(self, company_key: str, board_url: str) -> FilterSet:
        """Answer for one board."""
        with self._lock:
            self.calls.append(board_url)
        if board_url in self.raises:
            raise RuntimeError("detector exploded")
        # Looked up rather than `or`-ed: a FilterSet is falsy when it holds no
        # filters, so `by_url.get(url) or FilterSet(...)` would silently
        # discard a blocked one — which is exactly the case being tested.
        if board_url in self.by_url:
            return self.by_url[board_url]
        return FilterSet(source_url=board_url)


class FilterRunTest(unittest.TestCase):
    """Base class: an initialised fake spreadsheet and pristine settings."""

    companies = (("Acme Corporation", "https://acme.com"),)

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.checkpoint_path = Path(self._directory.name) / "checkpoint.json"

        self.service = FakeSheetsService({"Sheet1": []})
        self.client = SheetsClient(self.service, "fake", sleep=lambda _seconds: None)
        initialise(self.client)

        CompanyRepository(self.client).import_rows(
            [{"company": name, "website": site} for name, site in self.companies]
        )
        self.key = "domain:acme.com"

        # SETTINGS is process-wide, so every test puts it back as it found it.
        before = {
            "detect_filters": SETTINGS.detect_filters,
            "filter_render_budget": SETTINGS.filter_render_budget,
        }
        self.addCleanup(lambda: configure(**before))

    def run_with(
        self,
        engine: FakeEngine,
        detector: Any = None,
        filters_on: bool = True,
        render_budget: int = 0,
        **kwargs: Any,
    ) -> Any:
        """Execute a run with resolution and detection both kept offline."""
        configure(detect_filters=filters_on, filter_render_budget=render_budget)
        runner = WeeklyRun(
            self.client,
            engine=engine,
            checkpoint_path=self.checkpoint_path,
            batch_size=5,
            session_factory=lambda: None,
            filter_detector=detector,
        )
        kwargs.setdefault("run_id", "run-1")
        return runner.execute(**kwargs)

    def stored(self) -> Any:
        """The company row as the sheet now holds it."""
        return CompanyRepository(self.client).store.read_index("company_key")[self.key]


class TestOffByDefault(FilterRunTest):
    """A run that did not ask for filters must be the run it always was."""

    def test_the_detector_is_never_called(self) -> None:
        detector = RecordingDetector({BOARD: department_filter("Information Technology")})
        self.run_with(
            FakeEngine({self.key: result(jobs=[posting()])}),
            detector=detector,
            filters_on=False,
        )
        self.assertEqual(detector.calls, [])

    def test_no_filter_cell_is_written(self) -> None:
        self.run_with(FakeEngine({self.key: result(jobs=[posting()])}), filters_on=False)

        row = self.stored()
        for field in (
            "filters_detected",
            "filter_count",
            "filter_types",
            "filter_labels",
            "filter_values",
            "filter_detection_method",
            "filter_confidence",
            "filter_blocked",
        ):
            self.assertEqual(row.get(field), "", field)

    def test_the_dashboard_gains_no_filter_section(self) -> None:
        summary = self.run_with(
            FakeEngine({self.key: result(jobs=[posting()])}), filters_on=False
        )
        titles = [section for section, _metrics in summary.dashboard_sections()]
        self.assertNotIn("Board filters", titles)

    def test_the_crawl_itself_is_unchanged(self) -> None:
        engine = FakeEngine({self.key: result(jobs=[posting()])})
        summary = self.run_with(engine, filters_on=False)

        self.assertEqual(engine.crawled, [self.key])
        self.assertEqual(summary.observations, 1)
        self.assertEqual(summary.companies_succeeded, 1)


class TestWhatDetectionRecords(FilterRunTest):
    """Switched on, what a board's controls put in MASTER_COMPANIES."""

    def test_the_controls_are_written_to_their_own_columns(self) -> None:
        detector = RecordingDetector(
            {BOARD: department_filter("Information Technology", "Marketing")}
        )
        self.run_with(FakeEngine({self.key: result(jobs=[posting()])}), detector=detector)

        row = self.stored()
        self.assertEqual(row.get("filters_detected"), "TRUE")
        self.assertEqual(row.get("filter_count"), "1")
        self.assertEqual(row.get("filter_types"), "department")
        self.assertEqual(row.get("filter_labels"), "Department")
        self.assertIn("Information Technology", row.get("filter_values"))
        self.assertEqual(row.get("filter_detection_method"), "select")
        self.assertEqual(row.get("filter_confidence"), "0.9")
        self.assertEqual(row.get("filter_blocked"), "")

    def test_a_board_with_no_controls_records_false_rather_than_nothing(self) -> None:
        detector = RecordingDetector({BOARD: FilterSet(source_url=BOARD)})
        self.run_with(FakeEngine({self.key: result(jobs=[posting()])}), detector=detector)

        row = self.stored()
        self.assertEqual(row.get("filters_detected"), "FALSE")
        self.assertEqual(row.get("filter_count"), "0")

    def test_a_blocked_board_says_so(self) -> None:
        """Never seen and has-no-filters are different facts."""
        blocked = FilterSet(source_url=BOARD, blocked="cloudflare challenge")
        self.run_with(
            FakeEngine({self.key: result(jobs=[posting()])}),
            detector=RecordingDetector({BOARD: blocked}),
        )

        row = self.stored()
        self.assertEqual(row.get("filters_detected"), "FALSE")
        self.assertEqual(row.get("filter_blocked"), "cloudflare challenge")

    def test_the_url_read_is_the_one_the_engine_crawled(self) -> None:
        """Resolution only predicts a board; the engine settles it."""
        crawled = "https://boards.greenhouse.io/acme-inc"
        detector = RecordingDetector()
        self.run_with(
            FakeEngine({self.key: result(jobs=[posting()], seed_url=crawled)}),
            detector=detector,
        )
        self.assertEqual(detector.calls, [crawled])

    def test_the_counters_add_up(self) -> None:
        detector = RecordingDetector(
            {BOARD: department_filter("Information Technology", "Marketing")}
        )
        summary = self.run_with(
            FakeEngine({self.key: result(jobs=[posting()])}), detector=detector
        )

        self.assertEqual(summary.filters_checked, 1)
        self.assertEqual(summary.filters_with_controls, 1)
        self.assertEqual(summary.filters_found, 1)
        self.assertEqual(summary.filters_blocked, 0)
        # The classifier picks the technology option and leaves Marketing.
        self.assertEqual(summary.filters_tech_options, 1)

    def test_the_dashboard_reports_the_section(self) -> None:
        summary = self.run_with(
            FakeEngine({self.key: result(jobs=[posting()])}),
            detector=RecordingDetector({BOARD: department_filter("Information Technology")}),
        )
        sections = dict(summary.dashboard_sections())
        self.assertIn("Board filters", sections)
        self.assertIn(("Boards offering filters", 1), sections["Board filters"])


class TestDetectionCostsNothingItNeedNot(FilterRunTest):
    """The requests filter detection must not spend."""

    def test_a_company_that_could_not_be_crawled_is_not_fetched_again(self) -> None:
        detector = RecordingDetector()
        summary = self.run_with(
            FakeEngine({self.key: result(error="AdapterHttpError: 403 forbidden")}),
            detector=detector,
        )

        self.assertEqual(detector.calls, [])
        self.assertEqual(summary.filters_checked, 0)

    def test_a_company_with_no_url_is_not_fetched(self) -> None:
        detector = RecordingDetector()
        self.run_with(
            FakeEngine({self.key: result(jobs=[], seed_url="")}),
            detector=detector,
        )
        self.assertEqual(detector.calls, [])

    def test_a_board_that_was_read_but_advertised_nothing_is_still_examined(self) -> None:
        """No open jobs is not a failure, and its filters are worth knowing."""
        detector = RecordingDetector()
        self.run_with(FakeEngine({self.key: result(jobs=[])}), detector=detector)
        self.assertEqual(detector.calls, [BOARD])

    def test_each_board_is_read_once(self) -> None:
        detector = RecordingDetector()
        self.run_with(FakeEngine({self.key: result(jobs=[posting()])}), detector=detector)
        self.assertEqual(len(detector.calls), 1)


class TestFailureIsolation(FilterRunTest):
    """One board's filters must never cost the run."""

    def test_a_detector_that_raises_does_not_end_the_run(self) -> None:
        detector = RecordingDetector(raises=(BOARD,))
        summary = self.run_with(
            FakeEngine({self.key: result(jobs=[posting()])}), detector=detector
        )

        self.assertEqual(summary.companies_succeeded, 1)
        self.assertEqual(summary.observations, 1)
        self.assertEqual(summary.filters_blocked, 1)
        self.assertEqual(self.stored().get("filter_blocked"), "detection raised")

    def test_the_postings_are_still_written(self) -> None:
        from sheets.jobs import JobRepository

        self.run_with(
            FakeEngine({self.key: result(jobs=[posting()])}),
            detector=RecordingDetector(raises=(BOARD,)),
        )
        self.assertEqual(len(JobRepository(self.client).current.read()), 1)


class TestWhenTheBrowserIsWorthAVisit(FilterRunTest):
    """A search box is not a filter, and must not stand in for one.

    Every company careers page carries a site-wide search box, which is read as
    a keyword control. Treating that as "this board has filters" is what would
    stop the browser ever visiting an ADP board — whose department list exists
    only once its JavaScript has run.
    """

    def runner(self, rendered: FilterSet, budget: int = 1) -> tuple:
        """A runner whose browser path returns ``rendered``, and the call log."""
        visits: List[str] = []

        def render(url: str, _render: Any = None) -> FilterSet:
            visits.append(url)
            return rendered

        run = WeeklyRun(
            self.client,
            engine=FakeEngine(),
            checkpoint_path=self.checkpoint_path,
            session_factory=lambda: None,
        )
        run._renders_left = budget
        return run, visits, render

    def keyword_only(self) -> FilterSet:
        """What a WordPress careers page yields: one search box, no options."""
        return FilterSet(
            filters=[
                JobFilter(
                    label="Search",
                    filter_type=FilterType.KEYWORD,
                    options=[],
                    method=DetectionMethod.SEARCH_INPUT,
                    confidence=0.8,
                    parameter="s",
                )
            ],
            source_url=BOARD,
            methods=[DetectionMethod.SEARCH_INPUT],
        )

    def read_with(self, run: Any, render: Any, static: FilterSet) -> FilterSet:
        """Run ``_read_filters`` with both the fetch and the browser stubbed."""
        import crawler.weekly_run as module

        original_get, original_render = module.get_text, module.detect_filters_rendered
        original_detect = module.detect_filters
        module.get_text = lambda _session, _url, **_kwargs: "<html></html>"
        module.detect_filters = lambda _markup, _url=None, **_kwargs: static
        module.detect_filters_rendered = render
        try:
            return run._read_filters("key", BOARD)
        finally:
            module.get_text = original_get
            module.detect_filters = original_detect
            module.detect_filters_rendered = original_render

    def test_a_search_box_alone_does_not_stop_the_browser_visiting(self) -> None:
        real = department_filter("Information Technology")
        run, visits, render = self.runner(real)

        found = self.read_with(run, render, self.keyword_only())

        self.assertEqual(visits, [BOARD])
        self.assertEqual([item.label for item in found.filters], ["Department"])

    def test_a_department_control_is_enough_to_skip_the_browser(self) -> None:
        run, visits, render = self.runner(FilterSet(source_url=BOARD))

        self.read_with(run, render, department_filter("Information Technology"))

        self.assertEqual(visits, [])

    def test_the_static_search_box_survives_a_browser_that_found_nothing(self) -> None:
        run, visits, render = self.runner(FilterSet(source_url=BOARD))

        found = self.read_with(run, render, self.keyword_only())

        self.assertEqual(visits, [BOARD])
        self.assertEqual([item.label for item in found.filters], ["Search"])

    def test_an_exhausted_budget_leaves_the_static_answer_alone(self) -> None:
        run, visits, render = self.runner(department_filter("IT"), budget=0)

        found = self.read_with(run, render, self.keyword_only())

        self.assertEqual(visits, [])
        self.assertEqual([item.label for item in found.filters], ["Search"])

    def test_only_narrowing_boards_are_counted_as_such(self) -> None:
        summary = self.run_with(
            FakeEngine({self.key: result(jobs=[posting()])}),
            detector=RecordingDetector({BOARD: self.keyword_only()}),
        )
        self.assertEqual(summary.filters_with_controls, 1)
        self.assertEqual(summary.filters_narrowing, 0)


class TestRenderBudget(FilterRunTest):
    """The browser is the expensive path, so it is claimed, not assumed."""

    def test_the_budget_is_spent_at_most_once_per_visit(self) -> None:
        run = WeeklyRun(
            self.client,
            engine=FakeEngine(),
            checkpoint_path=self.checkpoint_path,
            session_factory=lambda: None,
        )
        run._renders_left = 2
        self.assertEqual([run._claim_render() for _ in range(4)], [True, True, False, False])

    def test_a_second_execute_starts_with_a_fresh_budget(self) -> None:
        configure(detect_filters=True, filter_render_budget=2)
        run = WeeklyRun(
            self.client,
            engine=FakeEngine(),
            checkpoint_path=self.checkpoint_path,
            batch_size=5,
            session_factory=lambda: None,
            filter_detector=RecordingDetector(),
        )
        run.execute(run_id="run-1")
        run._renders_left = 0
        run.execute(run_id="run-2", resume=False)
        self.assertEqual(run._renders_left, 2)


class TestDryRunWritesNothing(FilterRunTest):
    """A dry run with filters on is still a dry run."""

    def test_no_filter_cell_reaches_the_sheet(self) -> None:
        summary = self.run_with(
            FakeEngine({self.key: result(jobs=[posting()])}),
            detector=RecordingDetector({BOARD: department_filter("Information Technology")}),
            dry_run=True,
        )

        self.assertEqual(summary.filters_with_controls, 1)
        self.assertEqual(self.stored().get("filters_detected"), "")

    def test_the_checkpoint_is_not_written(self) -> None:
        self.run_with(
            FakeEngine({self.key: result(jobs=[posting()])}),
            detector=RecordingDetector(),
            dry_run=True,
        )
        self.assertFalse(self.checkpoint_path.is_file())


class TestTheSchemaCarriesTheColumns(unittest.TestCase):
    """The columns have to exist before anything can be written to them."""

    expected = (
        "filters_detected",
        "filter_count",
        "filter_types",
        "filter_labels",
        "filter_values",
        "filter_detection_method",
        "filter_confidence",
        "filter_blocked",
    )

    def test_master_companies_declares_every_filter_column(self) -> None:
        for field in self.expected:
            self.assertIn(field, MASTER_COMPANIES.fields, field)

    def test_the_identity_column_is_still_last(self) -> None:
        self.assertEqual(MASTER_COMPANIES.fields[-1], "company_key")

    def test_the_headers_are_distinct(self) -> None:
        headers = MASTER_COMPANIES.headers
        self.assertEqual(len(headers), len(set(headers)))

    def test_no_filter_column_collides_with_an_existing_one(self) -> None:
        """Two columns sharing a normalised spelling would adopt each other."""
        seen: Dict[str, str] = {}
        for column in MASTER_COMPANIES.columns:
            for key in column.keys():
                self.assertNotIn(key, seen, f"{column.field} clashes with {seen.get(key)}")
                seen[key] = column.field

    def test_initialisation_appends_them_to_a_tab_that_predates_them(self) -> None:
        """The live sheet has the sixteen original columns and no more."""
        original = [
            column.header
            for column in MASTER_COMPANIES.columns
            if not column.field.startswith("filter")
        ]
        service = FakeSheetsService({"MASTER_COMPANIES": [original]})
        client = SheetsClient(service, "fake", sleep=lambda _seconds: None)
        initialise(client)

        headers = service.headers_of("MASTER_COMPANIES")
        self.assertEqual(headers[: len(original)], original)
        self.assertEqual(sorted(headers), sorted(MASTER_COMPANIES.headers))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
