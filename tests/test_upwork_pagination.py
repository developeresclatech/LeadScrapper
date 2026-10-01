from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlparse

from upwork_scraper.config import ScraperConfig
from upwork_scraper.models import JobLead
from upwork_scraper.selenium_scraper import UpworkSeleniumScraper


class UpworkPaginationTests(unittest.TestCase):
    def test_america_aliases_search_united_states_and_canada(self) -> None:
        for alias in ("North America", "America"):
            with self.subTest(alias=alias):
                scraper = UpworkSeleniumScraper(
                    ScraperConfig(target_locations=[alias])
                )
                self.assertEqual(
                    scraper._client_search_locations(),
                    ["United States", "Canada"],
                )

    def test_detects_cloudflare_verification_page(self) -> None:
        class _Body:
            text = "Cloudflare Ray ID: abc123"

        class _Driver:
            title = "Just a moment..."

            @staticmethod
            def find_element(*_args):
                return _Body()

        self.assertTrue(
            UpworkSeleniumScraper._is_verification_page(_Driver())
        )

    def test_detail_location_keeps_only_country_line(self) -> None:
        self.assertEqual(
            UpworkSeleniumScraper._clean_client_location(
                "United States\nSan Jose2:16 PM"
            ),
            "United States",
        )
        self.assertEqual(
            UpworkSeleniumScraper._clean_client_location(
                "Canada\nToronto 5:16 PM"
            ),
            "Canada",
        )

    def test_search_enriches_missing_client_location(self) -> None:
        scraper = UpworkSeleniumScraper(ScraperConfig())
        lead = JobLead(
            title="Needs location",
            url="https://www.upwork.com/jobs/~012345",
        )
        calls: list[tuple[list[JobLead], bool]] = []
        scraper._scrape_keyword = lambda _: [lead]
        scraper.enrich_client_locations = (
            lambda leads, ensure_logged_in=True: calls.append(
                (leads, ensure_logged_in)
            )
        )

        result = scraper.search_keyword("react")

        self.assertEqual(result, [lead])
        self.assertEqual(calls, [([lead], False)])

    def test_enrichment_caches_country_by_upwork_job_id(self) -> None:
        scraper = UpworkSeleniumScraper(ScraperConfig())
        first = JobLead(
            title="First",
            url="https://www.upwork.com/jobs/title_~099999/",
        )
        second = JobLead(
            title="Second",
            url="https://www.upwork.com/jobs/~099999?source=rss",
        )
        scraper._get_driver = lambda: object()
        scraper._resolve_client_location = (
            lambda _driver, _url: "United States"
        )

        scraper.enrich_client_locations(
            [first],
            ensure_logged_in=False,
        )
        scraper._resolve_client_location = (
            lambda *_: self.fail("cached location should be reused")
        )
        scraper.enrich_client_locations(
            [second],
            ensure_logged_in=False,
        )

        self.assertEqual(first.country, "United States")
        self.assertEqual(second.country, "United States")

    def test_catch_up_requests_pages_until_lookback_boundary(self) -> None:
        config = ScraperConfig(
            max_results_per_keyword=1000,
            page_limit=100,
            collection_recency_hours=14,
        )
        scraper = UpworkSeleniumScraper(config)
        scraper._get_driver = lambda: object()
        scraper._ensure_logged_in = lambda _: True

        requested_pages: list[tuple[str, int]] = []
        pages = {
            1: [
                JobLead(
                    title="Recent 1",
                    url="https://www.upwork.com/jobs/1",
                    posted_date="1 hour ago",
                )
            ],
            2: [
                JobLead(
                    title="Recent 2",
                    url="https://www.upwork.com/jobs/2",
                    posted_date="13 hours ago",
                )
            ],
            3: [
                JobLead(
                    title="Boundary",
                    url="https://www.upwork.com/jobs/3",
                    posted_date="15 hours ago",
                )
            ],
        }

        def load_jobs(_, url: str, max_scrolls: int):
            query = parse_qs(urlparse(url).query)
            page = int(query["page"][0])
            self.assertEqual(query["per_page"], ["50"])
            location = query["location"][0]
            requested_pages.append((location, page))
            return [
                lead.model_copy(deep=True)
                for lead in pages.get(page, [])
            ]

        scraper._load_jobs = load_jobs
        scraper._parse_card = lambda card, _: card

        leads = scraper._scrape_keyword("web development")

        self.assertEqual(
            requested_pages,
            [
                ("United States", 1),
                ("United States", 2),
                ("United States", 3),
                ("Canada", 1),
                ("Canada", 2),
                ("Canada", 3),
            ],
        )
        self.assertEqual(len(leads), 3)
        self.assertTrue(
            all(lead.country == "United States" for lead in leads)
        )

    def test_date_only_values_are_not_rejected_when_day_overlaps_window(
        self,
    ) -> None:
        from datetime import datetime, timezone

        from upwork_scraper.pipeline.recency_filter import RecencyFilter

        now = datetime(2026, 7, 30, 14, 30, tzinfo=timezone.utc)
        recency_filter = RecencyFilter(14)

        self.assertTrue(
            recency_filter.matches(
                JobLead(title="Same day", posted_date="Jul 30, 2026"),
                now,
            )
        )
        self.assertFalse(
            recency_filter.matches(
                JobLead(title="Previous day", posted_date="Jul 29, 2026"),
                now,
            )
        )

    def test_stops_when_upwork_repeats_the_same_page(self) -> None:
        config = ScraperConfig(
            max_results_per_keyword=1000,
            page_limit=100,
            collection_recency_hours=14,
        )
        scraper = UpworkSeleniumScraper(config)
        scraper._get_driver = lambda: object()
        scraper._ensure_logged_in = lambda _: True
        card = JobLead(
            title="Repeated",
            url="https://www.upwork.com/jobs/repeated",
            posted_date="1 hour ago",
        )
        calls = [0]

        def load_jobs(*_, **__):
            calls[0] += 1
            return [card]

        scraper._load_jobs = load_jobs
        scraper._parse_card = lambda value, _: value

        leads = scraper._scrape_keyword("web development")

        self.assertEqual(calls[0], 4)
        self.assertEqual(len(leads), 1)

    def test_restarts_browser_once_after_local_driver_timeout(self) -> None:
        scraper = UpworkSeleniumScraper(ScraperConfig())
        calls = [0]
        resets = [0]

        def scrape(_: str) -> list[JobLead]:
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError(
                    "HTTPConnectionPool(host='localhost', port=51781): "
                    "Read timed out"
                )
            return [JobLead(title="Recovered")]

        scraper._scrape_keyword = scrape
        scraper._discard_driver = lambda: resets.__setitem__(
            0,
            resets[0] + 1,
        )

        leads = scraper.search_keyword("marketing")

        self.assertEqual(len(leads), 1)
        self.assertEqual(calls[0], 2)
        self.assertEqual(resets[0], 1)


if __name__ == "__main__":
    unittest.main()
