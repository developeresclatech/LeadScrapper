"""Checks for early RSS delivery and bounded parallel keyword searches."""

import unittest
from datetime import datetime, timedelta, timezone
from threading import Barrier
from unittest.mock import Mock, patch

import main
from upwork_scraper.config import ScraperConfig
from upwork_scraper.models import JobLead
from upwork_scraper.orchestration.events import KeywordResult
from upwork_scraper.orchestration.scheduler import PlatformScheduler
from upwork_scraper.platforms.base import PlatformAdapter
from upwork_scraper.selenium_scraper import UpworkSeleniumScraper
from upwork_scraper.vollna import VollnaScraper
from upwork_scraper.analyzer import LeadAnalysis
from upwork_scraper.pipeline.processor import ProcessedLead
from upwork_scraper.exporters.sheets import SheetsBatchWriter, _WorksheetState
from upwork_scraper.exporters.schema import SHEET_HEADERS, COLOR_GREEN


class FastSearchTests(unittest.TestCase):
    def test_recent_lead_insert_and_colour_use_one_sheet_request(self):
        writer = SheetsBatchWriter(ScraperConfig(
            recent_minutes=30, google_sheet_id="test", sheets_batch_size=1,
            sheets_min_write_interval=0,
        ), Mock())
        worksheet = Mock(id=7)
        lead = JobLead(title="Fresh Upwork job", url="https://www.upwork.com/jobs/~999",
                       platform="Upwork", posted_date="1 minute ago")
        item = ProcessedLead(lead, LeadAnalysis(priority="GREEN", lead_score=90))
        writer._states[writer._sheet_name(item)] = _WorksheetState(worksheet, set())
        writer.add(item, "fresh-key")
        worksheet.spreadsheet.batch_update.assert_called_once()
        requests = worksheet.spreadsheet.batch_update.call_args.args[0]["requests"]
        self.assertEqual(list(requests[0]), ["insertDimension"])
        self.assertEqual(list(requests[1]), ["updateCells"])
        formatting = requests[2]["repeatCell"]
        self.assertEqual(formatting["range"]["startRowIndex"], 1)
        self.assertEqual(formatting["cell"]["userEnteredFormat"]["backgroundColor"], COLOR_GREEN)
        writer.repository.mark_uploaded.assert_called_once_with(["fresh-key"])

    def test_ready_sheet_does_not_repeat_layout_or_read_all_rows_twice(self):
        writer = SheetsBatchWriter(ScraperConfig(recent_minutes=30, google_sheet_id="test"), Mock())
        worksheet = Mock(col_count=len(SHEET_HEADERS))
        worksheet.get_all_values.return_value = [SHEET_HEADERS]
        writer._spreadsheet = Mock()
        writer._spreadsheet.worksheet.return_value = worksheet
        writer._connect = Mock()
        writer._apply_fixed_layout = Mock()
        writer._write = Mock()
        state = writer._worksheet_state("Leads test")
        self.assertIs(state.worksheet, worksheet)
        worksheet.get_all_values.assert_called_once()
        writer._apply_fixed_layout.assert_not_called()
        writer._write.assert_not_called()

    def test_all_six_http_keywords_can_start_before_any_finishes(self):
        config = main.build_config(main.parse_args(["--recent-minutes", "30"]))
        keywords = config.keywords[:6]
        barrier = Barrier(len(keywords), timeout=5)

        def scrape(keyword):
            barrier.wait()
            return [JobLead(title=keyword)]

        scheduler = PlatformScheduler(
            {"http": PlatformAdapter("http", scrape, keyword_workers=config.http_keyword_workers)},
            keywords, 1, 2, 100,
        )
        results = [event for event in scheduler.events() if isinstance(event, KeywordResult)]
        self.assertEqual(len(results), 6)
        self.assertTrue(all(not result.error for result in results))

    def test_rss_stream_releases_ready_jobs_before_resolving_other_countries(self):
        config = ScraperConfig(recent_minutes=30, collection_recency_hours=0.5)
        scraper = VollnaScraper(config)
        self.addCleanup(scraper.close)
        now = datetime.now(timezone.utc)
        ready = JobLead(title="Ready", url="https://www.upwork.com/jobs/~001",
                        posted_date=now.isoformat(), country="Canada", keyword_searched="SEO")
        pending = JobLead(title="Pending", url="https://www.upwork.com/jobs/~002",
                          posted_date=now.isoformat(), keyword_searched="SEO")
        scraper._fetch = Mock(return_value="rss")
        scraper._parse_jobs = Mock(return_value=[pending, ready])
        emitted = []

        def enrich(leads):
            self.assertEqual(emitted, [ready])
            leads[0].country = "United States"

        scraper._enrich_locations = Mock(side_effect=enrich)
        processed = []

        def process(platform, keyword, leads):
            emitted.extend(leads)
            processed.extend(leads)
            return len(leads)

        adapter = PlatformAdapter("vollna", scraper.search_keyword,
                                  scrape_many_fn=scraper.search_keywords,
                                  stream_many_fn=scraper.search_keywords)
        scheduler = PlatformScheduler({"vollna": adapter}, ["SEO"], 1, 2, 10,
                                      process_leads=process)
        results = [event for event in scheduler.events() if isinstance(event, KeywordResult)]
        self.assertEqual(processed, [ready, pending])
        self.assertEqual(results[0].qualified_count, 2)
        self.assertEqual(results[0].scraped_count, 2)
        self.assertFalse(results[0].error)
        scraper._fetch.assert_called_once()

    def test_rss_filters_and_sorts_before_applying_result_limit(self):
        scraper = VollnaScraper(ScraperConfig(
            target_locations=[], collection_recency_hours=0.5, max_results_per_keyword=1,
        ))
        self.addCleanup(scraper.close)
        now = datetime.now(timezone.utc)
        old, recent, newest = [JobLead(
            title=str(minutes), url=f"https://example.com/{minutes}",
            posted_date=(now - timedelta(minutes=minutes)).isoformat(),
        ) for minutes in (90, 10, 1)]
        scraper._fetch = Mock(return_value="rss")
        scraper._parse_jobs = Mock(return_value=[old, recent, newest])
        self.assertEqual(scraper.search_keywords(["SEO"])["SEO"], [newest])

    def test_recent_login_cooldown_does_not_block_every_keyword(self):
        scraper = UpworkSeleniumScraper(ScraperConfig(
            recent_minutes=30, upwork_manual_login=False,
        ))
        scraper._login_retry_at = float("inf")
        with patch.object(scraper, "_is_logged_in", return_value=False), \
             patch("upwork_scraper.selenium_scraper.WebDriverWait") as wait, \
             patch.object(scraper, "_attempt_login") as login:
            with self.assertRaisesRegex(RuntimeError, "cooling down"):
                scraper._ensure_logged_in(Mock())
        wait.assert_not_called()
        login.assert_not_called()

    def test_login_lock_covers_verification_instead_of_losing_keyword_at_45s(self):
        scraper = UpworkSeleniumScraper(ScraperConfig(upwork_manual_login=False))
        with patch("upwork_scraper.selenium_scraper._LOGIN_LOCK") as lock, \
             patch.object(scraper, "_attempt_login", return_value=True):
            self.assertTrue(scraper._ensure_logged_in(Mock()))
        self.assertGreaterEqual(lock.acquire.call_args.kwargs["timeout"], 360)
        lock.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
