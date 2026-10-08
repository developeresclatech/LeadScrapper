"""Offline tests for deterministic Google Sheets headers and layout."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from upload_sheets import (
    COLUMN_WIDTHS,
    LEGACY_SHEET_HEADERS,
    SHEET_HEADERS,
    TIMING_INSERT_INDEX,
    _apply_fixed_layout,
    _ensure_headers,
    _is_sheet_eligible,
)
from upwork_scraper.analyzer import LeadAnalysis
from upwork_scraper.exporters.sheets import (
    SheetsBatchWriter,
    _WorksheetState,
)
from upwork_scraper.exporters.sheet_operations import prepend_rows_requests
from upwork_scraper.models import JobLead
from upwork_scraper.pipeline.processor import ProcessedLead


class _SpreadsheetFake:
    def __init__(self, worksheet=None) -> None:
        self.requests = []
        self.worksheet = worksheet

    def batch_update(self, body: dict) -> None:
        self.requests.extend(body["requests"])
        if self.worksheet is None:
            return
        for request in body["requests"]:
            insertion = request.get("insertDimension")
            if not insertion:
                continue
            dimension_range = insertion["range"]
            if dimension_range["dimension"] != "COLUMNS":
                continue
            start = dimension_range["startIndex"]
            count = dimension_range["endIndex"] - start
            for row in self.worksheet.values:
                row[start:start] = [""] * count


class _RepositoryFake:
    def mark_uploaded(self, dedup_keys: list[str]) -> None:
        pass


class _WorksheetFake:
    def __init__(self, values: list[list[str]]) -> None:
        self.values = [list(row) for row in values]
        self.col_count = 25
        self.row_count = 1000
        self.id = 123
        self.title = "Test"
        self.spreadsheet = _SpreadsheetFake(self)
        self.formatted_ranges = []

    def get_all_values(self) -> list[list[str]]:
        return [list(row) for row in self.values]

    def add_cols(self, count: int) -> None:
        self.col_count += count

    def update(self, *, range_name: str, values: list[list[str]]) -> None:
        if self.values:
            self.values[0] = list(values[0])
        else:
            self.values = [list(values[0])]

    def insert_row(
        self,
        values: list[str],
        index: int,
        value_input_option: str,
    ) -> None:
        self.values.insert(index - 1, list(values))

    def format(self, range_name: str, cell_format: dict) -> None:
        self.formatted_ranges.append((range_name, cell_format))


class UploadSheetLayoutTests(unittest.TestCase):
    def test_standalone_upload_includes_red_leads(self) -> None:
        self.assertTrue(_is_sheet_eligible({"Lead Score": "0"}))
        self.assertTrue(_is_sheet_eligible({"Lead Score": "29"}))
        self.assertTrue(_is_sheet_eligible({"Lead Score": "30"}))
        self.assertTrue(_is_sheet_eligible({"Lead Score": "49"}))
        self.assertFalse(_is_sheet_eligible({"Lead Score": "invalid"}))

    def test_empty_worksheet_receives_headers(self) -> None:
        worksheet = _WorksheetFake([])

        values = _ensure_headers(worksheet)

        self.assertEqual(values[0], SHEET_HEADERS)

    def test_attachment_headers_extend_existing_sheet_without_extra_row(self) -> None:
        existing_lead = ["Existing lead", "https://example.com"]
        old_headers = [h for h in SHEET_HEADERS if not h.startswith("Attachment")]
        existing_lead += [""] * (len(old_headers) - len(existing_lead))
        worksheet = _WorksheetFake([old_headers, existing_lead])
        values = _ensure_headers(worksheet)
        self.assertEqual(values[0], SHEET_HEADERS)
        self.assertEqual(values[1], existing_lead[:7] + ["", ""] + existing_lead[7:])
        self.assertEqual(len(values), 2)

    def test_headerless_data_is_preserved_below_inserted_header(self) -> None:
        existing_lead = ["Existing lead", "https://example.com"]
        worksheet = _WorksheetFake([existing_lead])

        values = _ensure_headers(worksheet)

        self.assertEqual(values[0], SHEET_HEADERS)
        self.assertEqual(values[1], existing_lead)

    def test_legacy_sheet_inserts_timing_columns_without_shifting_data(self) -> None:
        legacy_row = [f"value-{index}" for index in range(len(LEGACY_SHEET_HEADERS))]
        worksheet = _WorksheetFake([LEGACY_SHEET_HEADERS, legacy_row])

        values = _ensure_headers(worksheet)

        self.assertEqual(values[0], SHEET_HEADERS)
        self.assertEqual(
            values[1][TIMING_INSERT_INDEX:TIMING_INSERT_INDEX + 3],
            ["", "", ""],
        )
        self.assertEqual(
            values[1][TIMING_INSERT_INDEX + 5],
            legacy_row[TIMING_INSERT_INDEX],
        )

    def test_layout_uses_fixed_columns_rows_and_clipped_text(self) -> None:
        worksheet = _WorksheetFake([SHEET_HEADERS])

        _apply_fixed_layout(worksheet)

        requests = worksheet.spreadsheet.requests
        column_requests = [
            request
            for request in requests
            if request.get("updateDimensionProperties", {})
            .get("range", {})
            .get("dimension") == "COLUMNS"
        ]
        self.assertEqual(len(column_requests), len(COLUMN_WIDTHS))

        repeat_cell = next(
            request["repeatCell"]
            for request in requests
            if "repeatCell" in request
        )
        self.assertEqual(
            repeat_cell["cell"]["userEnteredFormat"]["wrapStrategy"],
            "CLIP",
        )

    def test_runtime_writer_uses_the_same_fixed_layout(self) -> None:
        worksheet = _WorksheetFake([SHEET_HEADERS])
        writer = SheetsBatchWriter(
            SimpleNamespace(
                google_sheet_id="test",
                sheets_batch_size=10,
                sheets_min_write_interval=0,
            ),
            _RepositoryFake(),
        )

        writer._apply_fixed_layout(worksheet)

        column_requests = [
            request
            for request in worksheet.spreadsheet.requests
            if request.get("updateDimensionProperties", {})
            .get("range", {})
            .get("dimension") == "COLUMNS"
        ]
        self.assertEqual(len(column_requests), len(COLUMN_WIDTHS))

    def test_runtime_writer_groups_leads_by_local_found_date(self) -> None:
        writer = SheetsBatchWriter(
            SimpleNamespace(
                google_sheet_id="test",
                sheets_batch_size=10,
                sheets_min_lead_score=30,
                sheets_min_write_interval=0,
                local_timezone="Asia/Karachi",
            ),
            _RepositoryFake(),
        )
        analysis = LeadAnalysis(priority="YELLOW", lead_score=50)

        writer.add(
            ProcessedLead(
                JobLead(
                    title="August 3 lead",
                    platform="Upwork",
                    scraped_at="2026-08-02T20:30:00+00:00",
                ),
                analysis,
            ),
            "august-3",
        )
        writer.add(
            ProcessedLead(
                JobLead(
                    title="August 4 lead",
                    platform="Upwork",
                    scraped_at="2026-08-03T20:30:00+00:00",
                ),
                analysis,
            ),
            "august-4",
        )

        self.assertEqual(len(writer._buffers["Leads 03-08-2026"]), 1)
        self.assertEqual(len(writer._buffers["Leads 04-08-2026"]), 1)

    def test_prepend_request_inserts_below_header_in_given_order(self) -> None:
        requests = prepend_rows_requests(
            123,
            [["Newest lead"], ["Older lead"]],
        )

        insertion = requests[0]["insertDimension"]["range"]
        self.assertEqual(insertion["startIndex"], 1)
        self.assertEqual(insertion["endIndex"], 3)
        inserted_rows = requests[1]["updateCells"]["rows"]
        self.assertEqual(
            inserted_rows[0]["values"][0]["userEnteredValue"]["stringValue"],
            "Newest lead",
        )
        self.assertEqual(
            inserted_rows[1]["values"][0]["userEnteredValue"]["stringValue"],
            "Older lead",
        )

    def test_runtime_writer_counts_saved_duplicates_and_below_score(
        self,
    ) -> None:
        writer = SheetsBatchWriter(
            SimpleNamespace(
                google_sheet_id="test",
                sheets_batch_size=10,
                sheets_min_lead_score=30,
                sheets_min_write_interval=0,
                local_timezone="Asia/Karachi",
            ),
            _RepositoryFake(),
        )
        worksheet = _WorksheetFake([SHEET_HEADERS, ["Existing Lead"]])
        date_tab = "Leads 03-08-2026"
        writer._states[date_tab] = _WorksheetState(
            worksheet=worksheet,
            titles={"existing lead"},
        )
        writer._format_rows = lambda *_: None

        eligible = LeadAnalysis(priority="YELLOW", lead_score=50)
        below_score = LeadAnalysis(priority="RED", lead_score=29)
        writer.add(
            ProcessedLead(
                JobLead(
                    title="Existing Lead",
                    platform="Upwork",
                    scraped_at="2026-08-03T12:00:00+00:00",
                ),
                eligible,
            ),
            "existing",
        )
        writer.add(
            ProcessedLead(
                JobLead(
                    title="New Lead",
                    platform="Upwork",
                    scraped_at="2026-08-03T12:00:00+00:00",
                ),
                eligible,
            ),
            "new",
        )
        writer.add(
            ProcessedLead(
                JobLead(
                    title="Below Score",
                    platform="Upwork",
                    scraped_at="2026-08-03T12:00:00+00:00",
                ),
                below_score,
            ),
            "below",
        )

        result = writer._append_to_tab(
            date_tab,
            writer._buffers[date_tab],
        )
        writer._record_primary_result(date_tab, result)
        stats = writer.stats()["Upwork"]

        self.assertEqual(stats["eligible"], 2)
        self.assertEqual(stats["saved"], 1)
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(stats["below_score"], 1)


if __name__ == "__main__":
    unittest.main()
