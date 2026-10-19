import unittest
from unittest.mock import Mock, patch

from selenium.common.exceptions import SessionNotCreatedException

from upwork_scraper.config import ScraperConfig
from upwork_scraper.selenium_scraper import UpworkSeleniumScraper
from upwork_scraper.orchestration.policy import DailyRunPolicy
from upwork_scraper.pipeline.recency_filter import RecencyFilter
from upwork_scraper.models import JobLead
from upwork_scraper.guru import GuruScraper
from datetime import datetime, timezone


class BrowserLaunchRecoveryTests(unittest.TestCase):
    def test_failed_constructor_cleans_partial_driver_and_cools_down(self):
        instances = []
        class FailedChrome:
            def __init__(self, **kwargs):
                instances.append(self)
                self.browser_pid = 123
                raise SessionNotCreatedException("cannot connect to chrome")
        scraper = UpworkSeleniumScraper()
        with (
            patch("upwork_scraper.selenium_scraper.uc.Chrome", FailedChrome),
            patch("upwork_scraper.selenium_scraper.chrome_launch_options", return_value={}),
            patch("upwork_scraper.selenium_scraper.discard_chrome_safely") as discard,
        ):
            with self.assertRaises(SessionNotCreatedException):
                scraper.search_keyword("first")
            discard.assert_called_once_with(instances[0])
            with self.assertRaisesRegex(RuntimeError, "cooling down"):
                scraper.search_keyword("second")
        self.assertEqual(len(instances), 1)
        self.assertIsNone(scraper._driver)

    def test_location_failure_does_not_launch_for_every_lead(self):
        scraper = UpworkSeleniumScraper()
        scraper._get_driver = Mock(side_effect=SessionNotCreatedException("cannot connect"))
        leads = [JobLead(title=str(i), url=f"https://upwork.com/jobs/~{i:09d}")
                 for i in range(10)]
        scraper.enrich_client_locations(leads)
        scraper._get_driver.assert_called_once()
        self.assertTrue(all(not lead.country for lead in leads))

    def test_guru_recent_time_survives_evening_filter(self):
        now = datetime(2026, 10, 16, 14, 47, tzinfo=timezone.utc)
        posted = GuruScraper._parse_posted_date("Posted 2 hrs ago · 3 Quotes Received", now)
        self.assertTrue(RecencyFilter(14, keep_unknown=False, strict_date_only=True).matches(
            JobLead(title="Recent", posted_date=posted), now,
        ))

    def test_fourteen_hour_window_uses_catchup_search_depth(self):
        policy = DailyRunPolicy.from_config(ScraperConfig(recent_minutes=840), 8)
        self.assertEqual(policy.recency_hours, 14)
        self.assertEqual(policy.max_results_per_keyword, 150)
        self.assertEqual(policy.page_limit, 3)
