"""Common interface for existing platform scrapers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..models import JobLead


@dataclass
class PlatformAdapter:
    """Adapt a platform-specific scraper to one orchestration interface."""

    name: str
    scrape_fn: Callable[[str], list[JobLead]]
    close_fn: Callable[[], None] | None = None
    resource_group: str = "http"
    keyword_workers: int = 1
    scrape_many_fn: (
        Callable[[list[str]], dict[str, list[JobLead]]] | None
    ) = None
    stream_fn: Callable | None = None
    stream_many_fn: Callable | None = None

    def scrape(self, keyword: str) -> list[JobLead]:
        return self.scrape_fn(keyword)

    def scrape_many(self, keywords: list[str]) -> dict[str, list[JobLead]]:
        if self.scrape_many_fn is None:
            return {keyword: self.scrape(keyword) for keyword in keywords}
        return self.scrape_many_fn(keywords)

    def close(self) -> None:
        if self.close_fn is not None:
            self.close_fn()
