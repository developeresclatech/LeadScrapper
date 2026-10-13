import unittest
from datetime import datetime, timedelta, timezone
from threading import local
from types import SimpleNamespace
from unittest.mock import MagicMock

from upwork_scraper.models import JobLead
from upwork_scraper.pipeline.recency_filter import RecencyFilter
from upwork_scraper.pipeline.deduplicator import RunDeduplicator
from upwork_scraper.orchestration.engine import LeadEngine
from upwork_scraper.selenium_scraper import UpworkSeleniumScraper


class RecencyRegressionTests(unittest.TestCase):
    def test_upwork_coarse_old_labels_are_rejected_even_when_unknown_allowed(self):
        now = datetime(2026, 10, 13, tzinfo=timezone.utc)
        for value in ("last week·Proposals: 20 to 50", "last month·Proposals: Fewer than 5",
                      "last quarterÂ·Proposals: 20 to 50", "Posted a month ago", "1 year ago"):
            with self.subTest(value=value):
                self.assertIsNotNone(RecencyFilter.parse_posted_at(value, now))
                self.assertFalse(RecencyFilter(24, keep_unknown=True).matches(JobLead(title="Old", posted_date=value), now))

    def test_recent_dates_and_boundary(self):
        now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
        check = RecencyFilter(24, keep_unknown=False, strict_date_only=True)
        for hours, expected in ((1, True), (23, True), (24, True), (25, False)):
            self.assertEqual(check.matches(JobLead(title="Job", posted_date=(now-timedelta(hours=hours)).isoformat()), now), expected)
        self.assertTrue(check.matches(JobLead(title="Job", posted_date="Posted an hour ago·Proposals: 5 to 10"), now))
        self.assertFalse(check.matches(JobLead(title="Job", posted_date="unknown"), now))
        self.assertFalse(check.matches(JobLead(title="Job", posted_date="2026-10-12"), now))

    def test_posted_label_does_not_include_proposal_counts(self):
        card = SimpleNamespace(text="UI Designer\nPosted last month·Proposals: Fewer than 5\nDescription")
        self.assertEqual(UpworkSeleniumScraper._extract_posted_str(card), "last month")

    def test_engine_enforces_ceiling_and_rejects_unknown_during_catchup(self):
        engine = LeadEngine.__new__(LeadEngine)
        engine._processor_local = local()
        engine._deduplicator = RunDeduplicator()
        engine.run_policy = SimpleNamespace(recency_hours=72, is_catch_up=True)
        engine.config = SimpleNamespace(keep_unknown_posted_dates=True)
        engine._output = MagicMock()
        leads = [JobLead(title="Old", posted_date="25 hours ago"),
                 JobLead(title="Unknown", posted_date="unrecognized")]
        self.assertEqual(engine._process_scraped_leads("upwork_selenium", "design", leads), 0)
        engine._output.submit.assert_not_called()
