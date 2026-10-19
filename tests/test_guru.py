"""Regression tests for Guru job-detail URL extraction."""

import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone

from upwork_scraper.guru import GuruScraper
from upwork_scraper.models import JobLead


class GuruScraperTests(unittest.TestCase):
    def test_live_second_page_route_is_used(self):
        scraper = GuruScraper(target_locations=[])
        self.addCleanup(scraper.close)
        with (
            patch.object(scraper._session, "get", return_value=Mock(status_code=200, text="html")) as get,
            patch.object(scraper, "_parse_records", side_effect=[
                [JobLead(title="First", url="https://guru.com/jobs/one/1")],
                [JobLead(title="Second", url="https://guru.com/jobs/two/2")],
            ]),
        ):
            leads = scraper.scrape("python", page_limit=2)
        self.assertEqual(len(leads), 2)
        self.assertEqual(get.call_args_list[1].args[0], "https://www.guru.com/d/jobs/skill/python/pg/2/")

    def test_live_nested_posting_label_keeps_spaces_and_recency(self):
        from upwork_scraper.pipeline.recency_filter import RecencyFilter
        scraper = GuruScraper()
        self.addCleanup(scraper.close)
        leads = scraper._parse_records('''
        <div class="record jobRecord">
          <div class="jobRecord__meta">Posted <strong>11 hrs ago</strong>
            · <strong>114</strong> Quotes Received</div>
          <h2 class="jobRecord__title"><a href="/jobs/example/2121389">Example</a></h2>
          <p class="freelancerAvatar__subText"><strong>United States</strong></p>
        </div>''')
        self.assertTrue(leads[0].posted_date)
        self.assertTrue(RecencyFilter(14, keep_unknown=False).matches(leads[0]))

    def test_uses_title_detail_url_instead_of_category_url(self) -> None:
        html = """
        <article class="record jobRecord">
          <div class="jobRecord__meta">Posted 2 hrs ago · 3 Quotes Received</div>
          <h2 class="jobRecord__title">
            <a href="/jobs/front-end-developer/2119835&SearchUrl=search.aspx?">
              Front End Developer
            </a>
          </h2>
          <div class="jobRecord__budget">Fixed Price</div>
          <div class="jobRecord__body">Build a web application.</div>
          <p class="freelancerAvatar__subText">
            <strong>United States</strong>
          </p>
          <a href="/d/jobs/c/programming-development/">
            Programming &amp; Development
          </a>
        </article>
        """

        scraper = GuruScraper()
        try:
            leads = scraper._parse_records(html)
        finally:
            scraper._session.close()

        self.assertEqual(len(leads), 1)
        self.assertEqual(
            leads[0].url,
            "https://www.guru.com/jobs/front-end-developer/2119835",
        )
        self.assertEqual(leads[0].job_id, "2119835")
        self.assertEqual(leads[0].country, "United States")

    def test_converts_relative_posted_time_to_absolute_date(self) -> None:
        now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)

        posted = GuruScraper._parse_posted_date(
            "Posted 2 hrs ago \u00b7 3 Quotes Received",
            now,
        )

        self.assertEqual(posted, "2026-07-31T10:00:00+00:00")

    def test_preserves_absolute_posted_date_format(self) -> None:
        posted = GuruScraper._parse_posted_date(
            "Posted on Jul 17, 2026 \u00b7 26 Quotes Received"
        )

        self.assertEqual(posted, "Jul 17, 2026")

    def test_filters_returned_leads_to_us_and_canada(self) -> None:
        scraper = GuruScraper(["United States", "Canada"])
        try:
            leads = scraper._target_leads([
                JobLead(title="Toronto", country="Canada"),
                JobLead(title="London", country="United Kingdom"),
            ])
        finally:
            scraper.close()

        self.assertEqual([lead.title for lead in leads], ["Toronto"])


if __name__ == "__main__":
    unittest.main()
