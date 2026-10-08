import unittest
from unittest.mock import MagicMock

from upwork_scraper.models import JobLead
from upwork_scraper.selenium_scraper import UpworkSeleniumScraper
from upwork_scraper.pipeline.processor import ProcessedLead
from upwork_scraper.analyzer import LeadAnalysis
from upwork_scraper.exporters.rows import processed_lead_to_row


class AttachmentTests(unittest.TestCase):
    def test_extracts_and_deduplicates_attachment_links(self):
        driver = MagicMock()
        file = MagicMock()
        file.is_displayed.return_value = True
        file.text = "Requirements.pdf"
        file.get_attribute.return_value = "https://www.upwork.com/att/download/123"
        driver.find_elements.return_value = [file, file]
        lead = JobLead(title="Test")
        UpworkSeleniumScraper._read_attachments(driver, lead)
        row = processed_lead_to_row(ProcessedLead(lead, LeadAnalysis()))
        self.assertEqual(row["Attachment Status"], "Yes")
        self.assertEqual(row["Attachment Count"], "1")
        self.assertEqual(row["Attachment Names"], "Requirements.pdf")

    def test_checked_page_without_files_is_no(self):
        driver = MagicMock()
        driver.find_elements.return_value = []
        lead = JobLead(title="Test")
        UpworkSeleniumScraper._read_attachments(driver, lead)
        self.assertEqual(lead.attachment_status, "No")
        self.assertEqual(lead.attachment_count, 0)

    def test_unchecked_lead_exports_unknown_count(self):
        row = processed_lead_to_row(ProcessedLead(JobLead(title="Test"), LeadAnalysis()))
        self.assertEqual(row["Attachment Status"], "Not checked")
        self.assertEqual(row["Attachment Count"], "")
