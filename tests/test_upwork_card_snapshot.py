"""Keep Upwork page extraction bounded to one browser field-read request."""

import unittest
from unittest.mock import Mock

from upwork_scraper.config import ScraperConfig
from upwork_scraper.selenium_scraper import UpworkSeleniumScraper


class UpworkCardSnapshotTests(unittest.TestCase):
    def test_page_fields_are_read_with_one_browser_call(self):
        driver = Mock()
        cards = [object() for _ in range(50)]
        snapshots = [self.snapshot(index) for index in range(50)]
        driver.execute_script.return_value = snapshots
        scraper = UpworkSeleniumScraper(ScraperConfig())

        leads = [scraper._parse_card(row, "python")
                 for row in scraper._snapshot_cards(driver, cards)]

        driver.execute_script.assert_called_once()
        self.assertIs(driver.execute_script.call_args.args[1], cards)
        self.assertEqual(len(leads), 50)
        self.assertEqual(leads[0].title, "Python project 0")
        self.assertEqual(leads[0].url, "https://www.upwork.com/jobs/~0")
        self.assertEqual(leads[0].description, "Build an API")
        self.assertEqual(leads[0].budget, "Fixed-price $1500")
        self.assertEqual(leads[0].skills_required, "Python, Django")
        self.assertEqual(leads[0].posted_date, "5 minutes ago")
        self.assertEqual(leads[0].experience_level, "Intermediate")
        self.assertEqual(leads[0].country, "Canada")
        self.assertEqual(leads[0].keyword_searched, "python")

    def test_invalid_cards_are_skipped_without_losing_remaining_jobs(self):
        scraper = UpworkSeleniumScraper(ScraperConfig())
        for missing in ("title", "url"):
            row = self.snapshot(0)
            row[missing] = ""
            with self.subTest(missing=missing):
                self.assertIsNone(scraper._parse_card(row, "python"))
        self.assertIsNotNone(scraper._parse_card(self.snapshot(1), "python"))

    def test_search_switches_country_after_emitting_page_results(self):
        scraper = UpworkSeleniumScraper(ScraperConfig(
            recent_minutes=60, page_limit=1, max_results_per_keyword=2,
        ))
        driver = Mock()
        events = []
        scraper._get_driver = Mock(return_value=driver)
        scraper._ensure_logged_in = Mock(return_value=True)
        scraper.enrich_client_locations = Mock()
        scraper._load_jobs = Mock(side_effect=lambda *args, **kwargs: (
            events.append(args[1]) or [object()]
        ))
        driver.execute_script.side_effect = [[self.snapshot(1)], [self.snapshot(2)]]

        leads = scraper.search_keyword("python", on_lead=lambda lead: events.append(lead.title))

        self.assertEqual([lead.title for lead in leads], ["Python project 1", "Python project 2"])
        self.assertEqual(driver.execute_script.call_count, 2)
        self.assertIn("location=United+States", events[0])
        self.assertEqual(events[1], "Python project 1")
        self.assertIn("location=Canada", events[2])
        self.assertEqual(events[3], "Python project 2")

    @staticmethod
    def snapshot(index):
        return {
            "title": f"Python project {index}",
            "url": f"https://www.upwork.com/jobs/~{index}",
            "text": "Posted 5 minutes ago\nFixed-price $1500\nIntermediate",
            "description": "Build an API",
            "skills": ["Python", "Django"],
            "location": "Canada",
        }
