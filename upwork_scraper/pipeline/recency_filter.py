"""Posted-date parsing and first-run recency filtering."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from ..models import JobLead


class RecencyFilter:
    DATE_ONLY_FORMATS = (
        "%b %d, %Y",
        "%B %d, %Y",
        "%Y-%m-%d",
        "%d %b %Y",
    )

    def __init__(
        self,
        max_age_hours: float | None,
        keep_unknown: bool = True,
        strict_date_only: bool = False,
    ) -> None:
        self.max_age_hours = max_age_hours
        self.keep_unknown = keep_unknown
        self.strict_date_only = strict_date_only

    def matches(self, lead: JobLead, now: datetime | None = None) -> bool:
        if self.max_age_hours is None:
            return True
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        posted_at = self.parse_posted_at(lead.posted_date, reference)
        if posted_at is None:
            return self.keep_unknown
        if self._is_date_only(lead.posted_date) and not self.strict_date_only:
            cutoff_date = (
                reference - timedelta(hours=self.max_age_hours)
            ).date()
            return posted_at.date() >= cutoff_date
        age = reference - posted_at
        return timedelta(0) <= age <= timedelta(hours=self.max_age_hours)

    @classmethod
    def page_reaches_boundary(
        cls,
        leads: list[JobLead],
        max_age_hours: float | None,
        now: datetime | None = None,
    ) -> bool:
        """Whether a newest-first page contains a definitely old lead."""
        if max_age_hours is None:
            return False
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        cutoff = reference - timedelta(hours=max_age_hours)
        for lead in leads:
            posted_at = cls.parse_posted_at(lead.posted_date, reference)
            if posted_at is None:
                continue
            if cls._is_date_only(lead.posted_date):
                if posted_at.date() < cutoff.date():
                    return True
            elif posted_at < cutoff:
                return True
        return False

    @classmethod
    def _is_date_only(cls, value: str) -> bool:
        text = (value or "").strip()
        for date_format in cls.DATE_ONLY_FORMATS:
            try:
                datetime.strptime(text, date_format)
                return True
            except ValueError:
                continue
        return False

    @staticmethod
    def parse_posted_at(
        value: str,
        now: datetime | None = None,
    ) -> datetime | None:
        text = (value or "").strip()
        if not text:
            return None
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)

        # Upwork combines the posting label and proposal count on one line.
        text = re.split(r"[·•]|\bProposals\s*:", text, maxsplit=1, flags=re.IGNORECASE)[0].strip(" \t|Â")
        lowered = re.sub(r"^posted\s+", "", text, flags=re.IGNORECASE).casefold().strip()
        if lowered in {"just now", "now"}:
            return reference
        if lowered == "yesterday":
            return reference - timedelta(days=1)

        named_period = re.fullmatch(r"last\s+(week|month|quarter|year)", lowered)
        if named_period:
            # These coarse labels cannot qualify for a <=24-hour window.
            days = {"week": 7, "month": 30, "quarter": 90, "year": 365}
            return reference - timedelta(days=days[named_period.group(1)])

        relative = re.search(
            r"(\d+|an?|one)\s*"
            r"(second|minute|hour|day|week|month|quarter|year)s?\s+ago",
            lowered,
        )
        if relative:
            amount = 1 if relative.group(1) in {"a", "an", "one"} else int(relative.group(1))
            unit = relative.group(2)
            multipliers = {
                "second": timedelta(seconds=amount),
                "minute": timedelta(minutes=amount),
                "hour": timedelta(hours=amount),
                "day": timedelta(days=amount),
                "week": timedelta(weeks=amount),
                "month": timedelta(days=30 * amount),
                "quarter": timedelta(days=90 * amount),
                "year": timedelta(days=365 * amount),
            }
            return reference - multipliers[unit]

        # Bark sometimes abbreviates relative units.
        abbreviated = re.search(r"(\d+)\s*([smhdw])\s*ago", lowered)
        if abbreviated:
            amount = int(abbreviated.group(1))
            unit = abbreviated.group(2)
            deltas = {
                "s": timedelta(seconds=amount),
                "m": timedelta(minutes=amount),
                "h": timedelta(hours=amount),
                "d": timedelta(days=amount),
                "w": timedelta(weeks=amount),
            }
            return reference - deltas[unit]

        # Freelancer's "days left" is a deadline, not a posted time.
        if "left" in lowered:
            return None

        # Preserve the timezone embedded in ISO-8601 platform timestamps.
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass

        try:
            parsed = parsedate_to_datetime(text)
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass

        for date_format in RecencyFilter.DATE_ONLY_FORMATS:
            try:
                parsed = datetime.strptime(text, date_format)
                return parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None
