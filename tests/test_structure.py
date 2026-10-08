"""Offline tests for the refactored structural components."""

from __future__ import annotations

import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from requests.exceptions import SSLError

from upwork_scraper.analyzer import LeadAnalysis, LeadAnalyzer
from upwork_scraper.config import ScraperConfig
from upwork_scraper.exporters.sheets import SheetsBatchWriter
from upwork_scraper.exporters.rows import processed_lead_to_row
from upwork_scraper.models import JobLead
from upwork_scraper.orchestration.engine import LeadEngine
from upwork_scraper.orchestration.policy import DailyRunPolicy
from upwork_scraper.pipeline.deduplicator import RunDeduplicator
from upwork_scraper.pipeline.location_filter import LocationFilter
from upwork_scraper.pipeline.processor import LeadProcessor, ProcessedLead
from upwork_scraper.pipeline.recency_filter import RecencyFilter
from upwork_scraper.platforms.base import PlatformAdapter
from upwork_scraper.storage.sqlite_repository import SQLiteLeadRepository
from upwork_scraper.timezones import resolve_timezone


class _AnalyzerStub:
    def __init__(self, location: str = "Remote") -> None:
        self.location = location

    def analyze(self, **_: str) -> LeadAnalysis:
        return LeadAnalysis(
            location=self.location,
            lead_score=50,
            priority="GREEN",
        )


class _RepositoryStub:
    def mark_uploaded(self, _: list[str]) -> None:
        pass


class _SheetsWriterProbe(SheetsBatchWriter):
    def __init__(self) -> None:
        config = SimpleNamespace(google_sheet_id="test", sheets_batch_size=5)
        super().__init__(config, _RepositoryStub())
        self.flush_calls: list[tuple[str, int | None]] = []

    def _flush_sheet(self, sheet_name: str, limit: int | None = None) -> None:
        self.flush_calls.append((sheet_name, limit))

    def _sheet_name(self, item: ProcessedLead) -> str:
        return "Leads"


class _QuotaError(Exception):
    code = 429

    def __init__(self, retry_after: str | None = None) -> None:
        self.response = SimpleNamespace(
            status_code=429,
            headers={"Retry-After": retry_after} if retry_after else {},
        )


class _FailingSheetsWriter(SheetsBatchWriter):
    def __init__(self) -> None:
        config = SimpleNamespace(
            google_sheet_id="test",
            sheets_batch_size=10,
            sheets_retry_attempts=1,
            sheets_quota_cooldown=60,
        )
        super().__init__(config, _RepositoryStub())
        self.append_attempts = 0

    def _append_to_tab(self, tab_name: str, batch: list) -> None:
        self.append_attempts += 1
        raise _QuotaError()

    def _sheet_name(self, item: ProcessedLead) -> str:
        return "Leads"


class StructuralPipelineTests(unittest.TestCase):
    def test_decision_maker_and_description_are_ten_points_each(self) -> None:
        analyzer = LeadAnalyzer()
        analysis = LeadAnalysis(
            _found_decision_maker=True,
            _found_full_description=True,
        )

        analyzer._score(analysis)

        self.assertEqual(analysis.lead_score, 20)

    def test_priority_uses_strict_score_boundaries(self) -> None:
        analyzer = LeadAnalyzer()
        expectations = {
            29: "RED",
            30: "RED",
            49: "RED",
            50: "YELLOW",
            69: "YELLOW",
            70: "GREEN",
        }

        for score, expected in expectations.items():
            with self.subTest(score=score):
                analysis = LeadAnalysis(lead_score=score)
                analyzer._classify(analysis)
                self.assertEqual(analysis.priority, expected)

    def test_formats_whole_run_duration(self) -> None:
        self.assertEqual(
            LeadEngine._format_duration(3723.4),
            "01:02:03",
        )

    def test_daily_policy_uses_first_and_later_limits(self) -> None:
        config = ScraperConfig()

        first = DailyRunPolicy.from_config(config, 1)
        later = DailyRunPolicy.from_config(config, 2, 2.0)

        self.assertEqual(first.max_results_per_keyword, 150)
        self.assertEqual(first.page_limit, 3)
        self.assertEqual(first.recency_hours, 14.0)
        self.assertTrue(first.is_catch_up)
        self.assertEqual(later.max_results_per_keyword, 20)
        self.assertEqual(later.page_limit, 3)
        self.assertEqual(later.recency_hours, 2.0)
        self.assertFalse(later.is_catch_up)

    def test_daily_policy_catches_up_after_fourteen_hour_gap(self) -> None:
        config = ScraperConfig()

        catch_up = DailyRunPolicy.from_config(config, 7, 15.5)

        self.assertEqual(catch_up.max_results_per_keyword, 150)
        self.assertEqual(catch_up.page_limit, 3)
        self.assertEqual(catch_up.recency_hours, 14.0)
        self.assertTrue(catch_up.is_catch_up)

    def test_daily_policy_can_force_catch_up_for_testing(self) -> None:
        config = ScraperConfig(force_catch_up=True)

        catch_up = DailyRunPolicy.from_config(config, 8, 0.1)

        self.assertTrue(catch_up.is_catch_up)
        self.assertEqual(catch_up.recency_hours, 14.0)
        self.assertEqual(catch_up.page_limit, 3)

    def test_daily_run_number_is_persisted_by_local_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = SQLiteLeadRepository(
                str(Path(temp_dir) / "leads.db"),
                "run-1",
            )
            try:
                first = repository.claim_daily_run(
                    "2026-07-30", "2026-07-30T09:00:00+05:00"
                )
            finally:
                repository.close()

            repository = SQLiteLeadRepository(
                str(Path(temp_dir) / "leads.db"),
                "run-2",
            )
            try:
                second = repository.claim_daily_run(
                    "2026-07-30", "2026-07-30T12:00:00+05:00"
                )
            finally:
                repository.close()

            self.assertEqual(first, 1)
            self.assertEqual(second, 2)

    def test_only_completed_runs_set_the_catch_up_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = str(Path(temp_dir) / "leads.db")
            repository = SQLiteLeadRepository(database, "aborted-run")
            repository.claim_daily_run(
                "2026-07-30", "2026-07-30T08:00:00+05:00"
            )
            repository.close()

            repository = SQLiteLeadRepository(database, "completed-run")
            repository.claim_daily_run(
                "2026-07-30", "2026-07-30T09:00:00+05:00"
            )
            repository.mark_run_completed("2026-07-30T09:30:00+05:00")
            repository.close()

            repository = SQLiteLeadRepository(database, "next-run")
            try:
                self.assertEqual(
                    repository.latest_completed_at(),
                    "2026-07-30T09:30:00+05:00",
                )
            finally:
                repository.close()

    def test_first_run_recency_filter_keeps_only_last_14_hours(self) -> None:
        recency_filter = RecencyFilter(14, keep_unknown=True)
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)

        self.assertTrue(
            recency_filter.matches(
                JobLead(title="Recent", posted_date="14 hours ago"),
                now,
            )
        )
        self.assertFalse(
            recency_filter.matches(
                JobLead(title="Old", posted_date="15 hours ago"),
                now,
            )
        )
        self.assertTrue(
            recency_filter.matches(
                JobLead(title="Unknown", posted_date="6 days left"),
                now,
            )
        )

    def test_normal_run_recency_filter_enforces_two_hour_limit(self) -> None:
        recency_filter = RecencyFilter(
            2,
            keep_unknown=False,
            strict_date_only=True,
        )
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)

        self.assertTrue(
            recency_filter.matches(
                JobLead(
                    title="Recent ISO",
                    posted_date="2026-07-30T10:30:00+00:00",
                ),
                now,
            )
        )
        self.assertFalse(
            recency_filter.matches(
                JobLead(
                    title="Old ISO",
                    posted_date="2026-07-30T09:30:00+00:00",
                ),
                now,
            )
        )
        self.assertFalse(
            recency_filter.matches(
                JobLead(title="Old date", posted_date="2026-07-30"),
                now,
            )
        )
        self.assertFalse(
            recency_filter.matches(
                JobLead(title="Unknown", posted_date="6 days left"),
                now,
            )
        )

    def test_deduplicates_titles_case_insensitively(self) -> None:
        deduplicator = RunDeduplicator()
        first = JobLead(title="React Developer")
        duplicate = JobLead(title="  react   developer ")

        self.assertTrue(deduplicator.accept(first))
        self.assertFalse(deduplicator.accept(duplicate))

    def test_accepts_explicit_us_lead(self) -> None:
        processor = LeadProcessor(
            analyzer=_AnalyzerStub(),
            deduplicator=RunDeduplicator(),
        )

        result = processor.process(
            JobLead(
                title="Build a dashboard",
                location="United States",
                description="Relevant project",
            )
        )

        self.assertIsNotNone(result)

    def test_processor_does_not_reject_non_target_location(self) -> None:
        processor = LeadProcessor(
            analyzer=_AnalyzerStub(location="London, United Kingdom"),
            deduplicator=RunDeduplicator(),
        )

        result = processor.process(
            JobLead(
                title="Build an API",
                location="United Kingdom",
                description="Onsite in London",
            )
        )

        self.assertIsNotNone(result)

    def test_strict_location_rejects_non_client_location_signals(self) -> None:
        location_filter = LocationFilter(["United States", "Canada"])

        for location in ("Worldwide", "Remote", "India", ""):
            with self.subTest(location=location):
                self.assertFalse(
                    location_filter.matches(
                        JobLead(
                            title="Lead",
                            location=location,
                            description=(
                                "Serving customers in the United States "
                                "and working Eastern Time."
                            ),
                        )
                    )
                )

        self.assertTrue(
            location_filter.matches(
                JobLead(title="Canadian Lead", country="Canada")
            )
        )
        for region in ("North America", "America"):
            with self.subTest(region=region):
                self.assertTrue(
                    location_filter.matches(
                        JobLead(title="Regional Lead", country=region)
                    )
                )

    def test_sheets_upload_triggers_at_five_eligible_leads(self) -> None:
        writer = _SheetsWriterProbe()
        analysis = LeadAnalysis(priority="GREEN", lead_score=50)

        for index in range(4):
            writer.add(
                ProcessedLead(
                    JobLead(title=f"Lead {index}", platform="Upwork"),
                    analysis,
                ),
                f"lead-{index}",
            )
        self.assertEqual(writer.flush_calls, [])

        writer.add(
            ProcessedLead(
                JobLead(title="Lead 4", platform="Upwork"),
                analysis,
            ),
            "lead-4",
        )
        self.assertEqual(writer.flush_calls, [("Leads", 5)])

    def test_shared_sheet_batch_combines_platforms(self) -> None:
        writer = _SheetsWriterProbe()
        analysis = LeadAnalysis(priority="GREEN", lead_score=50)

        for index, platform in enumerate(
            ["Upwork", "Freelancer", "Guru", "Upwork (Vollna)", "Upwork"]
        ):
            writer.add(
                ProcessedLead(
                    JobLead(title=f"Mixed Lead {index}", platform=platform),
                    analysis,
                ),
                f"mixed-{index}",
            )

        self.assertEqual(writer.flush_calls, [("Leads", 5)])
        stats = writer.stats()
        self.assertEqual(stats["Upwork"]["eligible"], 2)
        self.assertEqual(stats["Freelancer"]["eligible"], 1)
        self.assertEqual(stats["Guru"]["eligible"], 1)
        self.assertEqual(stats["Vollna"]["eligible"], 1)

    def test_sheets_rejects_scores_below_thirty(self) -> None:
        writer = _SheetsWriterProbe()
        writer.config.sheets_min_lead_score = 30
        analysis = LeadAnalysis(priority="RED", lead_score=29)

        for index in range(5):
            writer.add(
                ProcessedLead(
                    JobLead(title=f"Low score {index}", platform="Upwork"),
                    analysis,
                ),
                f"low-{index}",
            )

        self.assertEqual(writer.flush_calls, [])
        self.assertEqual(writer._buffers["Leads"], [])

    def test_sheets_write_retries_429_with_exponential_backoff(self) -> None:
        writer = _SheetsWriterProbe()
        writer.config.sheets_retry_attempts = 3
        writer.config.sheets_retry_base_delay = 2
        writer.config.sheets_retry_max_delay = 10
        writer.config.sheets_min_write_interval = 0
        now = [0.0]
        sleeps = []
        attempts = [0]
        writer._monotonic = lambda: now[0]

        def fake_sleep(delay: float) -> None:
            sleeps.append(delay)
            now[0] += delay

        def flaky_write() -> str:
            attempts[0] += 1
            if attempts[0] < 3:
                raise _QuotaError()
            return "ok"

        writer._sleep = fake_sleep
        result = writer._write("test operation", flaky_write)

        self.assertEqual(result, "ok")
        self.assertEqual(attempts[0], 3)
        self.assertEqual(sleeps, [2, 4])

    def test_sheets_write_retries_transient_ssl_errors(self) -> None:
        writer = _SheetsWriterProbe()
        writer.config.sheets_retry_attempts = 2
        writer.config.sheets_retry_base_delay = 1
        writer.config.sheets_retry_max_delay = 1
        writer.config.sheets_min_write_interval = 0
        writer._sleep = lambda _: None
        attempts = [0]

        def flaky_write() -> str:
            attempts[0] += 1
            if attempts[0] == 1:
                raise SSLError("unexpected EOF")
            return "ok"

        self.assertEqual(writer._write("test SSL retry", flaky_write), "ok")
        self.assertEqual(attempts[0], 2)

    def test_failed_batch_enters_cooldown_without_losing_buffer(self) -> None:
        writer = _FailingSheetsWriter()
        now = [100.0]
        writer._monotonic = lambda: now[0]
        analysis = LeadAnalysis(priority="GREEN", lead_score=50)

        for index in range(10):
            writer.add(
                ProcessedLead(
                    JobLead(title=f"Quota Lead {index}", platform="Upwork"),
                    analysis,
                ),
                f"quota-{index}",
            )

        self.assertEqual(writer.append_attempts, 1)
        self.assertEqual(len(writer._buffers["Leads"]), 10)
        self.assertEqual(writer._retry_not_before["Leads"], 160.0)

        writer.add(
            ProcessedLead(
                JobLead(title="Quota Lead 10", platform="Upwork"),
                analysis,
            ),
            "quota-10",
        )
        self.assertEqual(writer.append_attempts, 1)
        self.assertEqual(len(writer._buffers["Leads"]), 11)

    def test_export_row_records_found_to_sheet_duration(self) -> None:
        item = ProcessedLead(
            JobLead(
                title="Timed lead",
                scraped_at="2026-07-29T10:00:00+00:00",
            ),
            LeadAnalysis(priority="GREEN", lead_score=50),
        )

        row = processed_lead_to_row(
            item,
            sheet_saved_at=datetime(
                2026, 7, 29, 10, 0, 12, 500000, tzinfo=timezone.utc
            ),
            display_timezone=resolve_timezone("Asia/Karachi"),
        )

        self.assertEqual(row["Lead Found At"], "2026-07-29 3:00 PM")
        self.assertEqual(row["Sheet Saved At"], "2026-07-29 3:00 PM")
        self.assertEqual(row["Found-to-Sheet Seconds"], "12.50")

    def test_engine_processes_keyword_results_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = ScraperConfig(
                keywords=["react", "shopify"],
                platforms=[],
                output_dir=temp_dir,
                database_path=str(Path(temp_dir) / "leads.db"),
                google_sheet_id="",
            )
            engine = LeadEngine(config)

            def scrape(keyword: str) -> list[JobLead]:
                return [
                    JobLead(
                        title=f"{keyword.title()} Developer",
                        platform="Test Platform",
                        location="United States",
                        description=(
                            "Location: Remote. We need web development services."
                        ),
                    )
                ]

            engine._adapters = {
                "test": PlatformAdapter("test", scrape)
            }
            try:
                leads = engine.run()
            finally:
                engine.close()

            self.assertEqual(len(leads), 2)
            exports = list(Path(temp_dir).glob("leads_*.csv"))
            self.assertEqual(len(exports), 1)
            self.assertTrue((Path(temp_dir) / "leads.db").exists())


if __name__ == "__main__":
    unittest.main()
