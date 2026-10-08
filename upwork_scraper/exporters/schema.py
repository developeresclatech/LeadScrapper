"""Shared output schema and platform names."""

TIMING_HEADERS = [
    "Lead Found At",
    "Sheet Saved At",
    "Found-to-Sheet Seconds",
]
TIMING_INSERT_INDEX = 4

LEGACY_SHEET_HEADERS = [
    "Job Title",
    "Job URL",
    "Job Platform",
    "Date Posted",
    "Priority",
    "Lead Score",
    "Company Name",
    "Company Website",
    "Company Domain",
    "Email",
    "Business Email",
    "Phone",
    "LinkedIn URL",
    "Decision-Maker Name",
    "Decision-Maker Title",
    "Budget",
    "Timeline",
    "Location",
    "Industry",
    "Technologies",
    "Services Required",
    "Qualification Reason",
    "Full Job Description",
]

SHEET_HEADERS = (
    LEGACY_SHEET_HEADERS[:TIMING_INSERT_INDEX]
    + TIMING_HEADERS
    + ["Attachment Status", "Attachment Count"]
    + LEGACY_SHEET_HEADERS[TIMING_INSERT_INDEX:]
    + ["Attachment Names", "Attachment URLs"]
)

PLATFORM_SHEET_MAP = {
    "Upwork": "Upwork",
    "Upwork (Vollna)": "Vollna",
    "Freelancer": "Freelancer",
    "Guru": "Guru",
    "Upwork (Selenium)": "Upwork",
    "Bark.com": "Bark.com",
}

COLOR_GREEN = {"red": 0.85, "green": 0.92, "blue": 0.83}
COLOR_YELLOW = {"red": 1.0, "green": 0.95, "blue": 0.8}
COLOR_RED = {"red": 1.0, "green": 0.85, "blue": 0.85}
COLOR_HEADER = {"red": 0.2, "green": 0.2, "blue": 0.2}

# Fixed pixel widths, in the same order as SHEET_HEADERS.
COLUMN_WIDTHS = [
    280, 260, 110, 115, 165, 165, 145, 90, 85, 160, 220, 160, 210,
    210, 140, 220, 170, 160, 120, 110, 150, 130, 220, 220, 300, 420,
    130, 110, 260, 320,
]
COLUMN_WIDTHS = COLUMN_WIDTHS[:7] + COLUMN_WIDTHS[26:28] + COLUMN_WIDTHS[7:26] + COLUMN_WIDTHS[28:]


def attachment_column_requests(sheet_id, headers):
    """Move attachment cells with their headers, or insert empty columns."""
    current = [value.casefold().strip() for value in headers]
    base = [value.casefold() for value in
            LEGACY_SHEET_HEADERS[:4] + TIMING_HEADERS + LEGACY_SHEET_HEADERS[4:]]
    if current == base:
        return [{"insertDimension": {"range": {
            "sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 7, "endIndex": 9,
        }, "inheritFromBefore": False}}]
    if current == base + ["attachment status", "attachment count", "attachment names", "attachment urls"]:
        return [{"moveDimension": {"source": {
            "sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 26, "endIndex": 28,
        }, "destinationIndex": 7}}]
    return None
