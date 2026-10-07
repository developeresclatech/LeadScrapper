"""Single-writer, batched Google Sheets integration."""

from __future__ import annotations

import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Callable, TypeVar

from gspread.exceptions import WorksheetNotFound
from gspread.utils import rowcol_to_a1
from requests.exceptions import ConnectionError, SSLError, Timeout

from ..config import ScraperConfig
from ..pipeline.processor import ProcessedLead
from ..storage.sqlite_repository import SQLiteLeadRepository
from ..timezones import resolve_timezone
from .rows import processed_lead_to_row
from .sheet_operations import prepend_rows_requests
from .schema import (
    COLUMN_WIDTHS,
    COLOR_GREEN,
    COLOR_HEADER,
    COLOR_RED,
    COLOR_YELLOW,
    LEGACY_SHEET_HEADERS,
    PLATFORM_SHEET_MAP,
    SHEET_HEADERS,
    TIMING_HEADERS,
    TIMING_INSERT_INDEX,
)

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


@dataclass
class _BufferedLead:
    item: ProcessedLead
    dedup_key: str


@dataclass
class _WorksheetState:
    worksheet: object
    titles: set[str]


@dataclass
class _AppendResult:
    saved: list[_BufferedLead]
    duplicates: list[_BufferedLead]


class SheetsBatchWriter:
    """Buffer qualified leads and upload them through one Sheets client."""

    def __init__(
        self,
        config: ScraperConfig,
        repository: SQLiteLeadRepository,
    ) -> None:
        self.config = config
        self.repository = repository
        self.batch_size = max(1, config.sheets_batch_size)
        self.enabled = bool(config.google_sheet_id)
        self._buffers: dict[str, list[_BufferedLead]] = defaultdict(list)
        self._states: dict[str, _WorksheetState] = {}
        self._spreadsheet = None
        self._next_write_at = 0.0
        self._retry_not_before: dict[str, float] = defaultdict(float)
        self._stats: dict[str, Counter] = defaultdict(Counter)
        self._recorded_primary_outcomes: set[tuple[str, str]] = set()
        self._sleep = time.sleep
        self._monotonic = time.monotonic
        self._lock = RLock()

    def add(self, item: ProcessedLead, dedup_key: str) -> None:
        with self._lock:
            self._add(item, dedup_key)

    def _add(self, item: ProcessedLead, dedup_key: str) -> None:
        if not self.enabled:
            return
        platform_label = self._platform_label(item)
        minimum_score = getattr(
            self.config, "sheets_min_lead_score", 0
        )
        if item.analysis.lead_score < minimum_score:
            self._stats[platform_label]["below_score"] += 1
            logger.debug(
                "Skipping Sheets upload for '%s': score %d is below %d.",
                item.lead.title,
                item.analysis.lead_score,
                minimum_score,
            )
            return
        sheet_name = self._sheet_name(item)
        self._stats[platform_label]["eligible"] += 1
        self._buffers[sheet_name].append(_BufferedLead(item, dedup_key))
        if (
            len(self._buffers[sheet_name]) >= self.batch_size
            and self._monotonic() >= self._retry_not_before[sheet_name]
        ):
            self._flush_sheet(sheet_name, self.batch_size)

    def flush_platform(self, platform_label: str) -> None:
        with self._lock:
            if not self.enabled:
                return
            # Leads may span more than one local calendar date, so flush every
            # date buffer. Empty buffers are ignored by _flush_sheet().
            for sheet_name in list(self._buffers):
                self._flush_sheet(sheet_name, force=True)

    def flush_all(self) -> None:
        with self._lock:
            if not self.enabled:
                return
            for sheet_name in list(self._buffers):
                self._flush_sheet(sheet_name, force=True)

    def stats(self) -> dict[str, dict[str, int]]:
        """Return per-platform run outcomes for console reporting."""
        with self._lock:
            return self._stats_snapshot()

    def _stats_snapshot(self) -> dict[str, dict[str, int]]:
        pending: Counter = Counter()
        for buffer in self._buffers.values():
            for entry in buffer:
                pending[self._platform_label(entry.item)] += 1
        platform_names = set(self._stats) | set(pending)
        result: dict[str, dict[str, int]] = {}
        for platform_name in platform_names:
            counts = self._stats[platform_name]
            result[platform_name] = {
                "eligible": counts["eligible"],
                "saved": counts["saved"],
                "duplicates": counts["duplicates"],
                "below_score": counts["below_score"],
                "pending": pending[platform_name],
            }
        return result

    def _connect(self) -> None:
        if self._spreadsheet is not None:
            return
        import gspread
        from google.oauth2.service_account import Credentials

        credentials = Credentials.from_service_account_file(
            self.config.google_credentials_path,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        client = gspread.authorize(credentials)
        self._spreadsheet = self._write(
            "open spreadsheet",
            lambda: client.open_by_key(self.config.google_sheet_id),
        )

    def _write(self, operation: str, call: Callable[[], _T]) -> _T:
        """Run one Sheets write with throttling and bounded retries."""
        attempts = max(1, getattr(self.config, "sheets_retry_attempts", 5))
        base_delay = max(
            0.1, getattr(self.config, "sheets_retry_base_delay", 5.0)
        )
        max_delay = max(
            base_delay,
            getattr(self.config, "sheets_retry_max_delay", 60.0),
        )
        interval = max(
            0.0, getattr(self.config, "sheets_min_write_interval", 1.1)
        )

        for attempt in range(attempts):
            wait = self._next_write_at - self._monotonic()
            if wait > 0:
                self._sleep(wait)

            try:
                result = call()
            except Exception as exc:
                self._next_write_at = self._monotonic() + interval
                if not self._is_retryable(exc) or attempt + 1 >= attempts:
                    raise

                retry_after = self._retry_after(exc)
                delay = retry_after or min(
                    max_delay, base_delay * (2 ** attempt)
                )
                logger.warning(
                    "Sheets %s was rate-limited/temporarily unavailable; "
                    "retrying in %.1fs (attempt %d/%d).",
                    operation,
                    delay,
                    attempt + 2,
                    attempts,
                )
                self._sleep(delay)
                continue

            self._next_write_at = self._monotonic() + interval
            return result

        raise RuntimeError(f"Sheets write failed: {operation}")

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(exc, (ConnectionError, SSLError, Timeout)):
            return True
        code = getattr(exc, "code", None)
        if code is None:
            response = getattr(exc, "response", None)
            code = getattr(response, "status_code", None)
        return code == 429 or (isinstance(code, int) and code >= 500)

    @staticmethod
    def _retry_after(exc: Exception) -> float | None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", {}) or {}
        value = headers.get("Retry-After") or headers.get("retry-after")
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None

    def _worksheet_state(self, name: str) -> _WorksheetState:
        if name in self._states:
            return self._states[name]
        self._connect()
        try:
            worksheet = self._spreadsheet.worksheet(name)
        except WorksheetNotFound:
            worksheet = self._write(
                f"create worksheet {name}",
                lambda: self._spreadsheet.add_worksheet(
                    title=name, rows=1000, cols=len(SHEET_HEADERS)
                ),
            )

        if worksheet.col_count < len(SHEET_HEADERS):
            missing_columns = len(SHEET_HEADERS) - worksheet.col_count
            self._write(
                f"expand worksheet {name}",
                lambda: worksheet.add_cols(missing_columns),
            )

        values = worksheet.get_all_values()
        expected = [header.casefold().strip() for header in SHEET_HEADERS]
        current = (
            [
                value.casefold().strip()
                for value in values[0][:len(SHEET_HEADERS)]
            ]
            if values
            else []
        )
        legacy = [
            header.casefold().strip() for header in LEGACY_SHEET_HEADERS
        ]
        header_range = f"A1:{rowcol_to_a1(1, len(SHEET_HEADERS))}"
        if not values:
            self._write(
                f"write header {name}",
                lambda: worksheet.update(
                    range_name=header_range, values=[SHEET_HEADERS]
                ),
            )
        elif current[:len(legacy)] == legacy and len(current) == len(legacy):
            self._write(
                f"insert timing columns {name}",
                lambda: worksheet.spreadsheet.batch_update({
                    "requests": [{
                        "insertDimension": {
                            "range": {
                                "sheetId": worksheet.id,
                                "dimension": "COLUMNS",
                                "startIndex": TIMING_INSERT_INDEX,
                                "endIndex": (
                                    TIMING_INSERT_INDEX + len(TIMING_HEADERS)
                                ),
                            },
                            "inheritFromBefore": False,
                        }
                    }]
                }),
            )
            self._write(
                f"write expanded header {name}",
                lambda: worksheet.update(
                    range_name=header_range, values=[SHEET_HEADERS]
                ),
            )
        elif current != expected:
            self._write(
                f"insert header {name}",
                lambda: worksheet.insert_row(
                    SHEET_HEADERS,
                    index=1,
                    value_input_option="RAW",
                ),
            )
        else:
            # The canonical header is already present; avoid a redundant write.
            pass
        values = worksheet.get_all_values()

        try:
            self._apply_fixed_layout(worksheet)
            self._write(
                f"enable filter {name}",
                worksheet.set_basic_filter,
            )
        except Exception as exc:
            logger.warning("Worksheet layout failed for %s: %s", name, exc)
            if self._is_retryable(exc):
                raise

        headers = [value.casefold().strip() for value in values[0]]
        title_index = headers.index("job title") if "job title" in headers else 0
        titles = {
            self._title_key(row[title_index])
            for row in values[1:]
            if len(row) > title_index and row[title_index]
        }
        state = _WorksheetState(
            worksheet=worksheet,
            titles=titles,
        )
        self._states[name] = state
        return state

    def _apply_fixed_layout(self, worksheet: object) -> None:
        requests = []
        for column_index, width in enumerate(COLUMN_WIDTHS):
            requests.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": worksheet.id,
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
                        "sheetId": worksheet.id,
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
                        "sheetId": worksheet.id,
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
                        "sheetId": worksheet.id,
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
                        "sheetId": worksheet.id,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            },
        ])
        self._write(
            f"apply layout {worksheet.title}",
            lambda: worksheet.spreadsheet.batch_update({"requests": requests}),
        )
        header_range = f"A1:{rowcol_to_a1(1, len(SHEET_HEADERS))}"
        self._write(
            f"format header {worksheet.title}",
            lambda: worksheet.format(header_range, {
                "backgroundColor": COLOR_HEADER,
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "CLIP",
                "textFormat": {
                    "foregroundColor": {
                        "red": 1,
                        "green": 1,
                        "blue": 1,
                    },
                    "bold": True,
                },
            }),
        )

    def _flush_sheet(
        self,
        sheet_name: str,
        limit: int | None = None,
        force: bool = False,
    ) -> None:
        buffer = self._buffers[sheet_name]
        if not buffer:
            return

        cooldown_remaining = (
            self._retry_not_before[sheet_name] - self._monotonic()
        )
        if cooldown_remaining > 0:
            if not force:
                return
            logger.info(
                "Waiting %.1fs before retrying the %s Sheets buffer.",
                cooldown_remaining,
                sheet_name,
            )
            self._sleep(cooldown_remaining)

        count = min(limit or len(buffer), len(buffer))
        batch = buffer[:count]

        try:
            primary_result = self._append_to_tab(sheet_name, batch)
            newly_saved, newly_duplicated = self._record_primary_result(
                sheet_name,
                primary_result,
            )
        except Exception as exc:
            logger.error(
                "Sheets batch upload failed for %s (%d leads): %s",
                sheet_name,
                len(batch),
                exc,
            )
            cooldown = max(
                1.0, getattr(self.config, "sheets_quota_cooldown", 60.0)
            )
            self._retry_not_before[sheet_name] = (
                self._monotonic() + cooldown
            )
            return

        self._retry_not_before[sheet_name] = 0.0
        del buffer[:count]
        self.repository.mark_uploaded([entry.dedup_key for entry in batch])
        logger.info(
            "Google Sheets tab %s: %d newly saved, %d duplicates "
            "(%d eligible processed).",
            sheet_name,
            newly_saved,
            newly_duplicated,
            len(batch),
        )

    def _append_to_tab(
        self, tab_name: str, batch: list[_BufferedLead]
    ) -> _AppendResult:
        state = self._worksheet_state(tab_name)
        new_entries = [
            entry
            for entry in batch
            if self._title_key(entry.item.lead.title) not in state.titles
        ]
        new_ids = {id(entry) for entry in new_entries}
        duplicates = [
            entry for entry in batch if id(entry) not in new_ids
        ]
        if not new_entries:
            return _AppendResult([], duplicates)
        new_entries.sort(
            key=lambda entry: entry.item.lead.scraped_at,
            reverse=True,
        )

        sheet_saved_at = datetime.now(timezone.utc)
        display_timezone = resolve_timezone(
            getattr(self.config, "local_timezone", "Asia/Karachi")
        )
        rows = [
            [
                processed_lead_to_row(
                    entry.item,
                    sheet_saved_at=sheet_saved_at,
                    display_timezone=display_timezone,
                )[header]
                for header in SHEET_HEADERS
            ]
            for entry in new_entries
        ]
        start_row = 2
        requests = prepend_rows_requests(
            state.worksheet.id,
            rows,
            start_row_index=1,
        )
        self._write(
            f"prepend {len(rows)} rows to {tab_name}",
            lambda: state.worksheet.spreadsheet.batch_update(
                {"requests": requests}
            ),
        )
        state.titles.update(
            self._title_key(entry.item.lead.title)
            for entry in new_entries
        )
        self._format_rows(state.worksheet, start_row, new_entries)
        return _AppendResult(new_entries, duplicates)

    def _record_primary_result(
        self,
        sheet_name: str,
        result: _AppendResult,
    ) -> tuple[int, int]:
        newly_saved = 0
        newly_duplicated = 0
        for outcome, entries in (
            ("saved", result.saved),
            ("duplicates", result.duplicates),
        ):
            for entry in entries:
                key = (sheet_name, entry.dedup_key)
                if key in self._recorded_primary_outcomes:
                    continue
                self._recorded_primary_outcomes.add(key)
                platform_label = self._platform_label(entry.item)
                self._stats[platform_label][outcome] += 1
                if outcome == "saved":
                    newly_saved += 1
                else:
                    newly_duplicated += 1
        return newly_saved, newly_duplicated

    @staticmethod
    def _title_key(title: str) -> str:
        return re.sub(r"\s+", " ", title).strip().casefold()

    @staticmethod
    def _platform_label(item: ProcessedLead) -> str:
        return PLATFORM_SHEET_MAP.get(
            item.lead.platform,
            item.lead.platform or "Other",
        )

    def _sheet_name(self, item: ProcessedLead) -> str:
        """Return the lead's local date as a valid Google Sheets tab name."""
        value = (item.lead.scraped_at or "").strip()
        try:
            found_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if found_at.tzinfo is None:
                found_at = found_at.replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning(
                "Invalid scraped_at timestamp %r for '%s'; using current time.",
                value,
                item.lead.title,
            )
            found_at = datetime.now(timezone.utc)

        local_timezone = resolve_timezone(
            getattr(self.config, "local_timezone", "Asia/Karachi")
        )
        # A slash is not valid in a worksheet title, hence dd-mm-yyyy.
        date_label = found_at.astimezone(local_timezone).strftime("%d-%m-%Y")
        return f"Leads {date_label}"

    def _format_rows(
        self,
        worksheet: object,
        start_row: int,
        entries: list[_BufferedLead],
    ) -> None:
        requests = []
        for offset, entry in enumerate(entries):
            analysis = entry.item.analysis
            color = (
                COLOR_GREEN
                if analysis.priority == "GREEN"
                else COLOR_YELLOW
                if analysis.priority == "YELLOW"
                else COLOR_RED
            )
            cell_format = {"backgroundColor": color}
            if analysis.priority == "GREEN":
                cell_format["textFormat"] = {"bold": True}
            row_index = start_row + offset - 1
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": worksheet.id,
                            "startRowIndex": row_index,
                            "endRowIndex": row_index + 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(SHEET_HEADERS),
                        },
                        "cell": {"userEnteredFormat": cell_format},
                        "fields": (
                            "userEnteredFormat(backgroundColor,textFormat)"
                        ),
                    }
                }
            )
        if requests:
            try:
                self._write(
                    f"format {len(entries)} rows in {worksheet.title}",
                    lambda: worksheet.spreadsheet.batch_update(
                        {"requests": requests}
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "Row formatting failed for %s: %s", worksheet.title, exc
                )
