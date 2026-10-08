#!/usr/bin/env python3
"""Upload existing CSV leads to Google Sheets."""
import csv
import os
import logging
import time
from pathlib import Path
from datetime import date, datetime

import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
from upwork_scraper.exporters.schema import (
    COLUMN_WIDTHS,
    LEGACY_SHEET_HEADERS,
    SHEET_HEADERS,
    TIMING_HEADERS,
    TIMING_INSERT_INDEX,
)
from upwork_scraper.exporters.sheet_operations import prepend_rows_requests
from upwork_scraper.exporters.schema import attachment_column_requests

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(message)s")

SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
CREDS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "service-account.json")
CSV_PATH = "output/leads_20260716_211534.csv"
MIN_LEAD_SCORE = 0

# Google Sheets does not allow "/" in worksheet titles, so date tabs use the
# equivalent unambiguous format (for example, "Leads 03-08-2026").
TAB_DATE_FORMAT = "%d-%m-%Y"

PLATFORM_SHEET_MAP = {
    "Bark.com": "Bark.com",
    "Freelancer": "Freelancer",
    "Guru": "Guru",
    "Upwork": "Upwork",
    "Upwork (Vollna)": "Vollna",
    "Upwork (Selenium)": "Upwork",
}

COLOR_GREEN = {"red": 0.85, "green": 0.92, "blue": 0.83}
COLOR_YELLOW = {"red": 1.0, "green": 0.95, "blue": 0.8}
COLOR_RED = {"red": 1.0, "green": 0.85, "blue": 0.85}
COLOR_HEADER = {"red": 0.2, "green": 0.2, "blue": 0.2}


def _ensure_headers(worksheet) -> list[list[str]]:
    """Create or repair row 1 without overwriting existing lead data."""
    if worksheet.col_count < len(SHEET_HEADERS):
        worksheet.add_cols(len(SHEET_HEADERS) - worksheet.col_count)

    values = worksheet.get_all_values()
    expected = [header.casefold().strip() for header in SHEET_HEADERS]
    current = (
        [value.casefold().strip() for value in values[0][:len(SHEET_HEADERS)]]
        if values
        else []
    )
    legacy = [header.casefold().strip() for header in LEGACY_SHEET_HEADERS]
    header_range = f"A1:{rowcol_to_a1(1, len(SHEET_HEADERS))}"

    if not values:
        worksheet.update(range_name=header_range, values=[SHEET_HEADERS])
    elif current[:len(legacy)] == legacy and len(current) == len(legacy):
        worksheet.spreadsheet.batch_update({
            "requests": [{
                "insertDimension": {
                    "range": {
                        "sheetId": worksheet.id,
                        "dimension": "COLUMNS",
                        "startIndex": TIMING_INSERT_INDEX,
                        "endIndex": TIMING_INSERT_INDEX + len(TIMING_HEADERS),
                    },
                    "inheritFromBefore": False,
                }
            }]
        })
        worksheet.spreadsheet.batch_update({"requests": attachment_column_requests(worksheet.id,
            LEGACY_SHEET_HEADERS[:4] + TIMING_HEADERS + LEGACY_SHEET_HEADERS[4:])})
        worksheet.update(range_name=header_range, values=[SHEET_HEADERS])
    elif attachment_column_requests(worksheet.id, current) is not None:
        worksheet.spreadsheet.batch_update({"requests": attachment_column_requests(worksheet.id, current)})
        worksheet.update(range_name=header_range, values=[SHEET_HEADERS])
    elif current != expected:
        worksheet.insert_row(
            SHEET_HEADERS,
            index=1,
            value_input_option="RAW",
        )
    else:
        worksheet.update(range_name=header_range, values=[SHEET_HEADERS])

    return worksheet.get_all_values()


def _apply_fixed_layout(worksheet) -> None:
    """Apply deterministic dimensions so long values cannot resize cells."""
    sheet_id = worksheet.id
    requests = []

    for column_index, width in enumerate(COLUMN_WIDTHS):
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": column_index,
                    "endIndex": column_index + 1,
                },
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })

    requests.extend([
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": 0,
                    "endIndex": 1,
                },
                "properties": {"pixelSize": 42},
                "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": 1,
                    "endIndex": worksheet.row_count,
                },
                "properties": {"pixelSize": 36},
                "fields": "pixelSize",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": worksheet.row_count,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(SHEET_HEADERS),
                },
                "cell": {
                    "userEnteredFormat": {
                        "wrapStrategy": "CLIP",
                        "verticalAlignment": "TOP",
                    }
                },
                "fields": (
                    "userEnteredFormat.wrapStrategy,"
                    "userEnteredFormat.verticalAlignment"
                ),
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
    ])

    worksheet.spreadsheet.batch_update({"requests": requests})
    header_range = f"A1:{rowcol_to_a1(1, len(SHEET_HEADERS))}"
    worksheet.format(header_range, {
        "backgroundColor": COLOR_HEADER,
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE",
        "wrapStrategy": "CLIP",
        "textFormat": {
            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
            "bold": True,
        },
    })


def _is_sheet_eligible(row: dict) -> bool:
    try:
        lead_score = float(row.get("Lead Score", "0") or 0)
    except (TypeError, ValueError):
        return False
    return lead_score >= MIN_LEAD_SCORE


def _lead_date(value: str, fallback: date) -> date:
    """Return the calendar date stored in a Lead Found At value."""
    value = (value or "").strip()
    if not value:
        return fallback

    # Current exports are ISO 8601; older exports used a display timestamp.
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass

    for timestamp_format in ("%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, timestamp_format).date()
        except ValueError:
            continue

    logging.warning(
        "Could not parse Lead Found At %r; using the upload date instead.",
        value,
    )
    return fallback


def main():
    if not SHEET_ID:
        print("GOOGLE_SHEET_ID not set in .env")
        return

    rows = []
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    print(f"Loaded {len(rows)} leads from {CSV_PATH}")

    creds = Credentials.from_service_account_file(
        CREDS_PATH,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"],
    )
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(SHEET_ID)

    # Group leads by the day they were found, creating one worksheet per day.
    upload_date = datetime.now().astimezone().date()
    grouped = {}
    for row in rows:
        found_date = _lead_date(row.get("Lead Found At", ""), upload_date)
        tab_name = f"Leads {found_date.strftime(TAB_DATE_FORMAT)}"
        grouped.setdefault(tab_name, []).append(row)

    for sheet_name, leads in grouped.items():
        print(f"\n{sheet_name}: {len(leads)} leads")
        try:
            worksheet = sheet.worksheet(sheet_name)
        except Exception:
            worksheet = sheet.add_worksheet(title=sheet_name, rows=1000, cols=25)

        try:
            all_values = _ensure_headers(worksheet)
        except Exception as exc:
            logging.error("  Could not create headers for %s: %s", sheet_name, exc)
            continue

        headers = [header.casefold().strip() for header in all_values[0]]
        title_col = headers.index("job title")
        existing_titles = {
            row[title_col]
            for row in all_values[1:]
            if len(row) > title_col and row[title_col]
        }

        new_rows = []
        newest_first = sorted(
            leads,
            key=lambda row: row.get("Lead Found At", ""),
            reverse=True,
        )
        for row in newest_first:
            if not _is_sheet_eligible(row):
                continue
            if row.get("Job Title", "") in existing_titles:
                continue
            new_rows.append([row.get(h, "") for h in SHEET_HEADERS])

        if not new_rows:
            try:
                _apply_fixed_layout(worksheet)
                worksheet.set_basic_filter()
            except Exception as exc:
                logging.warning("  Layout formatting failed: %s", exc)
            print("  No new leads to upload.")
            continue

        worksheet.spreadsheet.batch_update({
            "requests": prepend_rows_requests(
                worksheet.id,
                new_rows,
                start_row_index=1,
            )
        })
        try:
            _apply_fixed_layout(worksheet)
            worksheet.set_basic_filter()
        except Exception as exc:
            logging.warning("  Leads uploaded, but layout formatting failed: %s", exc)
        print(f"  Uploaded {len(new_rows)} new leads.")
        time.sleep(2)

    print("\nDone!")

if __name__ == "__main__":
    main()
