import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from requests.exceptions import SSLError
from upwork_scraper.exporters.sheets import SheetsBatchWriter
from upwork_scraper.exporters.sheets import _BufferedLead
from upwork_scraper.exporters.schema import SHEET_HEADERS
from upwork_scraper.pipeline.processor import ProcessedLead
from upwork_scraper.models import JobLead
from upwork_scraper.analyzer import LeadAnalysis


class SheetsTransportTests(unittest.TestCase):
    def writer(self):
        writer = SheetsBatchWriter(SimpleNamespace(
            google_sheet_id="test", sheets_batch_size=5,
            sheets_retry_attempts=2, sheets_min_write_interval=0,
        ), MagicMock())
        writer._sleep = MagicMock()
        writer._http_session = MagicMock()
        return writer

    def test_read_retries_after_dropping_failed_connection(self):
        writer = self.writer()
        request = MagicMock(side_effect=[SSLError("EOF"), "connected"])
        self.assertEqual(writer._write("open spreadsheet", request), "connected")
        writer._http_session.close.assert_called_once()
        self.assertEqual(request.call_count, 2)

    def test_uncertain_prepend_is_not_blindly_repeated(self):
        writer = self.writer()
        request = MagicMock(side_effect=SSLError("response lost"))
        with self.assertRaises(SSLError):
            writer._write("prepend rows", request, retry_transport=False)
        request.assert_called_once()

    def test_failed_batch_stays_pending_and_discards_cached_titles(self):
        writer = self.writer()
        batch = [object()]
        writer._buffers["tab"] = batch.copy()
        writer._states["tab"] = object()
        writer._append_to_tab = MagicMock(side_effect=SSLError("response lost"))
        writer._flush_sheet("tab")
        self.assertEqual(writer._buffers["tab"], batch)
        self.assertNotIn("tab", writer._states)
        writer.repository.mark_uploaded.assert_not_called()

    def prepared_batch(self):
        writer = self.writer()
        worksheet = MagicMock()
        worksheet.col_count = len(SHEET_HEADERS)
        worksheet.id = 123
        worksheet.get_all_values.return_value = [SHEET_HEADERS]
        writer._spreadsheet = MagicMock()
        writer._spreadsheet.worksheet.return_value = worksheet
        writer._format_rows = MagicMock()
        writer._buffers["tab"] = [_BufferedLead(
            ProcessedLead(JobLead(title="Fresh lead", platform="Upwork"), LeadAnalysis()),
            "lead-key",
        )]
        return writer, worksheet

    def test_layout_ssl_failure_does_not_prevent_row_upload(self):
        writer, worksheet = self.prepared_batch()
        writer._apply_fixed_layout = MagicMock(side_effect=SSLError("EOF"))
        writer._flush_sheet("tab")
        worksheet.spreadsheet.batch_update.assert_called_once()
        writer.repository.mark_uploaded.assert_called_once_with(["lead-key"])
        self.assertEqual(writer._buffers["tab"], [])
        self.assertEqual(writer.stats()["Upwork"]["saved"], 1)

    def test_header_ssl_failure_still_keeps_batch_pending(self):
        writer, worksheet = self.prepared_batch()
        worksheet.get_all_values.return_value = []
        worksheet.update.side_effect = SSLError("EOF")
        writer._apply_fixed_layout = MagicMock()
        writer._flush_sheet("tab")
        writer.repository.mark_uploaded.assert_not_called()
        worksheet.spreadsheet.batch_update.assert_not_called()
        writer._apply_fixed_layout.assert_not_called()
        self.assertEqual(len(writer._buffers["tab"]), 1)
