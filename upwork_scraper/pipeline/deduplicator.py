"""Thread-safe run-level lead deduplication."""

from __future__ import annotations

import re
from threading import Lock
from urllib.parse import urlsplit, urlunsplit

from ..models import JobLead


class RunDeduplicator:
    """Deduplicate by stable job identity, with a title fallback for missing URLs."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = Lock()

    @staticmethod
    def key(lead: JobLead) -> str:
        url = urlsplit(lead.url.strip())
        if url.hostname and "upwork.com" == url.hostname.removeprefix("www."):
            match = re.search(r"~[A-Za-z0-9]+", url.path)
            if match:
                return "upwork:" + match.group(0)
        if url.netloc:
            return urlunsplit(("https", url.netloc.casefold().removeprefix("www."),
                              url.path.rstrip("/"), "", ""))
        if lead.job_id:
            return lead.platform.casefold() + ":" + lead.job_id
        return re.sub(r"\s+", " ", lead.title).strip().casefold()

    def accept(self, lead: JobLead) -> bool:
        key = self.key(lead)
        if not key:
            return False
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True
