"""Composable processing stages applied to each scraped lead."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from ..analyzer import LeadAnalysis, LeadAnalyzer
from ..models import JobLead
from .deduplicator import RunDeduplicator
from .recency_filter import RecencyFilter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessedLead:
    lead: JobLead
    analysis: LeadAnalysis


class LeadProcessor:
    def __init__(
        self,
        analyzer: LeadAnalyzer,
        deduplicator: RunDeduplicator,
        recency_filter: RecencyFilter | None = None,
    ) -> None:
        self._analyzer = analyzer
        self._deduplicator = deduplicator
        self._recency_filter = recency_filter or RecencyFilter(None)

    def process(self, lead: JobLead) -> ProcessedLead | None:
        if not self._recency_filter.matches(lead):
            logger.debug("Rejected by recency: %s (posted %s)", lead.title, lead.posted_date)
            return None
        if not self._deduplicator.accept(lead):
            logger.debug("Rejected duplicate: %s", lead.title)
            return None

        analysis = self._analyzer.analyze(
            title=lead.title,
            description=lead.description,
            budget=lead.budget,
            url=lead.url,
            platform=lead.platform,
        )
        return ProcessedLead(lead=lead, analysis=analysis)
