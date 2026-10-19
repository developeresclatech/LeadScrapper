import unittest
from threading import Event
from unittest.mock import Mock, patch

from upwork_scraper.config import ScraperConfig
from upwork_scraper.freelancer import FreelancerScraper
from upwork_scraper.guru import GuruScraper
from upwork_scraper.models import JobLead
from upwork_scraper.selenium_scraper import UpworkSeleniumScraper


class EarlyDeliveryTests(unittest.TestCase):
    def test_upwork_emits_before_next_card_and_does_not_emit_twice(self):
        scraper = UpworkSeleniumScraper(ScraperConfig(
            recent_minutes=10, page_limit=1,
        ))
        events = []
        cards = [JobLead(title=str(i), url=f"https://upwork.com/jobs/~{i}",
                         posted_date="1 minute ago") for i in range(2)]
        scraper._load_jobs = Mock(return_value=cards)
        def parse(card, keyword):
            events.append("parse" + card.title)
            return card
        scraper._parse_card = parse
        def collect(keyword):
            results = scraper._scrape_keyword_location(object(), keyword, "Canada", 2)
            events.append("search-finished")
            return results
        scraper._scrape_keyword = collect
        scraper.search_keyword("python", on_lead=lambda lead: events.append("emit" + lead.title))
        self.assertEqual(events, ["parse0", "emit0", "parse1", "emit1", "search-finished"])

    def test_freelancer_emits_without_waiting_for_slow_detail(self):
        scraper = FreelancerScraper()
        self.addCleanup(scraper.close)
        release = Event()
        finished = Event()
        leads = [JobLead(title="Fast", url="fast"), JobLead(title="Slow", url="slow")]
        def metadata(url):
            if url == "slow":
                if not release.wait(2):
                    raise AssertionError("Fast lead was held until all details completed")
                finished.set()
            return "1 minute ago", "Canada"
        received = []
        def emit(lead):
            received.append(lead.title)
            if lead.title == "Fast":
                self.assertFalse(finished.is_set())
                release.set()
        with patch.object(scraper, "_fetch_detail_metadata", side_effect=metadata):
            scraper._populate_posted_dates(leads, on_lead=emit)
        self.assertEqual(received, ["Fast", "Slow"])

    def test_guru_emits_before_next_page_request(self):
        scraper = GuruScraper(target_locations=[])
        self.addCleanup(scraper.close)
        events = []
        def get(url, **kwargs):
            events.append("page2" if "pg/2/" in url else "page1")
            return Mock(status_code=200, text="html")
        with (
            patch.object(scraper._session, "get", side_effect=get),
            patch.object(scraper, "_parse_records", side_effect=[
                [JobLead(title="First", url="https://guru.com/jobs/1")],
                [JobLead(title="Second", url="https://guru.com/jobs/2")],
            ]),
        ):
            scraper.scrape("python", page_limit=2, on_lead=lambda lead: events.append(lead.title))
        self.assertEqual(events, ["page1", "First", "page2", "Second"])
