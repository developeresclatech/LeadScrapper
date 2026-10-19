import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import get_ident
from unittest.mock import Mock

import main
from upwork_scraper.analyzer import LeadAnalysis
from upwork_scraper.config import ScraperConfig
from upwork_scraper.exporters.sheets import SheetsBatchWriter
from upwork_scraper.models import JobLead
from upwork_scraper.orchestration.policy import DailyRunPolicy
from upwork_scraper.pipeline.processor import ProcessedLead
from upwork_scraper.pipeline.recency_filter import RecencyFilter
from upwork_scraper.platforms.registry import build_platform_adapters
from upwork_scraper.platforms.upwork_session import UpworkBrowserSession
from upwork_scraper.storage.sqlite_repository import SQLiteLeadRepository
from upwork_scraper.selenium_scraper import UpworkSeleniumScraper


class RecentModeTests(unittest.TestCase):
    def test_cli_defaults_apply_to_first_and_later_runs(self):
        config = main.build_config(main.parse_args([]))
        self.assertEqual(config.sheets_batch_size, 1)
        for number in (1, 5):
            policy = DailyRunPolicy.from_config(config, number, 72)
            self.assertEqual(policy.recency_hours, 1)
            self.assertEqual(policy.page_limit, 1)
            self.assertFalse(policy.is_catch_up)
        config = main.build_config(main.parse_args(["--recent-minutes", "5"]))
        self.assertEqual(DailyRunPolicy.from_config(config, 1).recency_hours, 5 / 60)

    def test_relative_dates_do_not_become_fresh_again_on_replay(self):
        now = datetime(2026, 10, 16, 15, tzinfo=timezone.utc)
        check = RecencyFilter(0.5, keep_unknown=False, anchor_to_scraped_at=True)
        lead = JobLead(title="Pending", posted_date="5 minutes ago",
                       scraped_at=(now - timedelta(hours=3)).isoformat())
        self.assertFalse(check.matches(lead, now))
        lead.scraped_at = now.isoformat()
        self.assertTrue(check.matches(lead, now))
        lead.posted_date = "1 hour ago"
        self.assertFalse(check.matches(lead, now))
        lead.posted_date = "unknown"
        self.assertFalse(check.matches(lead, now))

    def test_recent_mode_streams_without_attachment_visits(self):
        scraper = UpworkSeleniumScraper(ScraperConfig(recent_minutes=30))
        lead = JobLead(title="Fresh", country="Canada", posted_date="2 minutes ago")
        scraper._scrape_keyword = Mock(return_value=[lead])
        scraper.enrich_attachments = Mock()
        scraper.enrich_client_locations = Mock()
        emitted = []
        scraper.search_keyword("python", on_lead=emitted.append)
        self.assertEqual(emitted, [lead])
        scraper.enrich_attachments.assert_not_called()
        self.assertEqual(lead.attachment_status, "Not checked")

    def test_expired_pending_upload_is_retired_and_local_record_retained(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        repo = SQLiteLeadRepository(str(Path(directory) / "leads.db"), "old")
        self.addCleanup(repo.close)
        item = ProcessedLead(JobLead(
            title="Old pending", posted_date="5 minutes ago",
            scraped_at=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
        ), LeadAnalysis())
        key = repo.save("freelancer", "python", item, "old-key")
        writer = SheetsBatchWriter(ScraperConfig(
            recent_minutes=30, google_sheet_id="test", sheets_batch_size=1,
        ), repo)
        writer._append_to_tab = Mock()
        writer.add(item, key)
        writer._append_to_tab.assert_not_called()
        self.assertEqual(repo.pending_uploads(), [])
        self.assertEqual(repo._connection.execute(
            "SELECT uploaded FROM processed_leads"
        ).fetchall(), [(0,)])

    def test_expired_buffer_is_rechecked_before_retry(self):
        writer = SheetsBatchWriter(ScraperConfig(
            recent_minutes=30, google_sheet_id="test", sheets_batch_size=5,
        ), Mock())
        item = ProcessedLead(JobLead(title="Fresh", posted_date="1 minute ago"), LeadAnalysis())
        writer.add(item, "key")
        item.lead.scraped_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        writer._append_to_tab = Mock()
        writer.flush_all()
        writer._append_to_tab.assert_not_called()
        writer.repository.expire_pending_uploads.assert_called_once_with(["key"])

    def test_country_resolvers_reuse_browser_owner_threads(self):
        created = []
        class Scraper:
            def __init__(self, config, profile_worker):
                self.owner = get_ident()
                created.append(self)
            def search_keyword(self, keyword):
                return []
            def enrich_client_locations(self, leads):
                self_test.assertEqual(get_ident(), self.owner)
                for lead in leads:
                    lead.country = "Canada"
            def close(self):
                pass
        self_test = self
        session = UpworkBrowserSession(factory=Scraper)
        self.addCleanup(session.close)
        config = ScraperConfig(platforms=["upwork", "upwork_selenium", "vollna"])
        adapters = build_platform_adapters(config, upwork_session=session)
        self.addCleanup(lambda: [adapter.close() for adapter in adapters.values()])
        direct = adapters["upwork"].scrape_fn.__self__
        vollna = adapters["vollna"].scrape_many_fn.__self__
        leads = [JobLead(title="Example", url="https://www.upwork.com/jobs/~001")]
        direct._with_client_locations(leads)
        vollna._enrich_locations(leads)
        self.assertEqual(leads[0].country, "Canada")
        self.assertIs(vollna._location_resolver, session)
        self.assertEqual(len(created), 2)
