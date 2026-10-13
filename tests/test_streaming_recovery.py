import tempfile
import unittest
from pathlib import Path
from threading import local
from types import SimpleNamespace
from unittest.mock import Mock

from upwork_scraper.analyzer import LeadAnalysis
from upwork_scraper.config import ScraperConfig
from upwork_scraper.models import JobLead
from upwork_scraper.orchestration.engine import LeadEngine
from upwork_scraper.orchestration.output_worker import LeadOutputWorker
from upwork_scraper.orchestration.scheduler import PlatformScheduler
from upwork_scraper.platforms.base import PlatformAdapter
from upwork_scraper.orchestration.events import KeywordResult
from upwork_scraper.pipeline.deduplicator import RunDeduplicator
from upwork_scraper.pipeline.processor import ProcessedLead
from upwork_scraper.selenium_scraper import UpworkSeleniumScraper
from upwork_scraper.storage.sqlite_repository import SQLiteLeadRepository


class StreamingRecoveryTests(unittest.TestCase):
    def test_scheduler_stream_callback_contract_counts_each_lead_once(self):
        scraper = UpworkSeleniumScraper(ScraperConfig())
        leads = [JobLead(title="First"), JobLead(title="Second")]
        scraper._scrape_keyword = Mock(return_value=leads)
        scraper.enrich_attachments = Mock()
        scraper.enrich_client_locations = Mock()
        process = Mock(return_value=1)
        adapter = PlatformAdapter("test", scraper.search_keyword, stream_fn=scraper.search_keyword)
        scheduler = PlatformScheduler({"test": adapter}, ["keyword"], 1, 1, 10,
                                      process_leads=process, prepare_leads=lambda p, k, rows: rows)
        results = [event for event in scheduler.events() if isinstance(event, KeywordResult)]
        self.assertFalse(results[0].error)
        self.assertEqual(results[0].qualified_count, 2)
        self.assertEqual(process.call_count, 2)

    def test_committed_before_output_starts_and_replayed_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "leads.db")
            repository = SQLiteLeadRepository(path, "first")
            item = ProcessedLead(JobLead(title="Lead", posted_date="1 hour ago"), LeadAnalysis())
            worker = LeadOutputWorker("test", repository, Mock(), 10)
            worker.submit("keyword", item, "identity")
            repository.close()  # Output never started, simulating interruption.
            recovered = SQLiteLeadRepository(path, "second")
            pending = recovered.pending_uploads()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0][0].lead.title, "Lead")
            recovered.mark_uploaded([pending[0][1]])
            self.assertEqual(recovered.pending_uploads(), [])
            recovered.close()

    def test_streams_before_next_detail_and_preserves_failed_details(self):
        scraper = UpworkSeleniumScraper(ScraperConfig())
        leads = [JobLead(title=str(i)) for i in range(3)]
        scraper._scrape_keyword = Mock(return_value=leads)
        events = []
        def enrich(batch):
            events.append("check" + batch[0].title)
            if batch[0].title == "1":
                raise TimeoutError("detail transport failed")
            batch[0].attachment_status = "No"
        scraper.enrich_attachments = enrich
        scraper.enrich_client_locations = Mock()
        scraper.search_keyword("keyword", on_lead=lambda lead: events.append("save" + lead.title),
                               prepare_leads=lambda batch: batch)
        self.assertEqual(events, ["check0", "save0", "check1", "save1", "check2", "save2"])
        self.assertEqual(leads[1].attachment_status, "Not checked")
        scraper._scrape_keyword.assert_called_once()

    def test_filters_before_details_but_checkpoints_all_search_results(self):
        engine = LeadEngine.__new__(LeadEngine)
        engine._repository = Mock()
        engine._detail_deduplicator = RunDeduplicator()
        engine.run_policy = SimpleNamespace(recency_hours=72)
        first = JobLead(title="Same title", url="https://upwork.com/jobs/~001", posted_date="1 hour ago")
        second = first.model_copy(update={"url": "https://upwork.com/jobs/~002"})
        old = first.model_copy(update={"url": "https://upwork.com/jobs/~003", "posted_date": "25 hours ago"})
        candidates = engine._prepare_detail_leads("upwork_selenium", "keyword", [first, second, old, first])
        self.assertEqual(candidates, [first, second])
        engine._repository.checkpoint_search.assert_called_once()
        self.assertEqual(engine._prepare_detail_leads("upwork_selenium", "other", [first]), [])

    def test_upwork_identity_survives_title_and_tracking_changes(self):
        first = JobLead(title="Original", url="https://www.upwork.com/jobs/Original_~001/?source=a")
        renamed = JobLead(title="Renamed", url="https://upwork.com/jobs/~001?source=b")
        self.assertEqual(RunDeduplicator.key(first), RunDeduplicator.key(renamed))
