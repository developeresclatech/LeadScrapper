"""Concurrent lead-generation orchestration."""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from threading import local

from rich.console import Console
from rich.table import Table

from ..analyzer import LeadAnalysis, LeadAnalyzer
from ..config import ScraperConfig
from ..exporters.local import LocalExporter
from ..exporters.schema import PLATFORM_SHEET_MAP
from ..exporters.sheets import SheetsBatchWriter
from ..models import JobLead
from ..pipeline.deduplicator import RunDeduplicator
from ..pipeline.processor import LeadProcessor, ProcessedLead
from ..pipeline.recency_filter import RecencyFilter
from ..platforms.registry import build_platform_adapters
from ..storage.sqlite_repository import SQLiteLeadRepository
from ..timezones import resolve_timezone
from .events import KeywordResult, PlatformFinished
from .output_worker import PlatformOutputWorkers
from .policy import DailyRunPolicy
from .scheduler import PlatformScheduler

logger = logging.getLogger(__name__)
console = Console()


class LeadEngine:
    """Coordinate platform workers, processing, persistence, and output."""

    def __init__(self, config: ScraperConfig | None = None, *, upwork_session=None) -> None:
        self.config = config or ScraperConfig()
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        self.all_leads: list[JobLead] = []
        self.all_analyses: dict[str, LeadAnalysis] = {}
        self._processed: list[ProcessedLead] = []
        self._scraped_counts: Counter = Counter()
        self._closed = False
        self.last_run_duration_seconds: float | None = None

        local_timezone = resolve_timezone(self.config.local_timezone)
        started_local = datetime.now(local_timezone)
        self._repository = SQLiteLeadRepository(
            self.config.database_path, self.run_id
        )
        latest_completed_at = self._repository.latest_completed_at()
        run_number = self._repository.claim_daily_run(
            started_local.date().isoformat(),
            started_local.isoformat(),
        )
        hours_since_last_completed = self._hours_since(
            latest_completed_at,
            started_local,
        )
        self.run_policy = DailyRunPolicy.from_config(
            self.config,
            run_number,
            hours_since_last_completed,
        )
        self.config.max_results_per_keyword = (
            self.run_policy.max_results_per_keyword
        )
        self.config.page_limit = self.run_policy.page_limit
        self.config.collection_recency_hours = self.run_policy.recency_hours

        self._adapters = build_platform_adapters(self.config, upwork_session=upwork_session)
        self._deduplicator = RunDeduplicator()
        self._detail_deduplicator = RunDeduplicator()
        self._processor_local = local()
        self._sheets = SheetsBatchWriter(self.config, self._repository)
        self._output = PlatformOutputWorkers(
            list(self._adapters),
            self._repository,
            self._sheets,
            self.config.output_queue_size,
        )
        self._processed = self._output.processed
        self.all_leads = self._output.all_leads
        self.all_analyses = self._output.all_analyses
        self._local_exporter = LocalExporter(
            self.config.output_dir, self.config.output_format
        )

    def run(self) -> list[JobLead]:
        run_started_at = time.monotonic()
        platforms = list(self._adapters)
        console.rule("[bold green]Multi-Platform Lead Generator")
        console.print(f"Platforms: [cyan]{', '.join(platforms)}[/cyan]")
        if self.run_policy.is_first_run:
            policy_label = "first daily run"
        elif self.run_policy.is_catch_up:
            policy_label = (
                f"catch-up run (daily run #{self.run_policy.run_number})"
            )
        else:
            policy_label = f"daily run #{self.run_policy.run_number}"
        recency = (
            f"last {self.run_policy.recency_hours:g} hours"
            if self.run_policy.recency_hours is not None
            else "newest-first platform results"
        )
        console.print(
            f"Run policy: [cyan]{policy_label}; "
            f"up to {self.config.max_results_per_keyword} jobs/keyword; "
            f"up to {self.config.page_limit} pages; "
            f"{recency}[/cyan]"
        )
        console.print(
            f"Execution: [cyan]{self.config.max_platform_workers} "
            "platform workers; "
            f"{self.config.max_browser_workers} browser slots; "
            f"{self.config.upwork_keyword_workers} Upwork keyword workers; "
            f"{self.config.http_keyword_workers} HTTP keyword workers/platform"
            "[/cyan]"
        )
        console.print(
            f"Sheets batch: [cyan]{self.config.sheets_batch_size} "
            "qualified leads; "
            f"{self._output.worker_count} platform output workers[/cyan]"
        )

        self._output.start()
        scheduler = PlatformScheduler(
            adapters=self._adapters,
            keywords=self.config.keywords,
            max_workers=self.config.max_platform_workers,
            max_browser_workers=self.config.max_browser_workers,
            queue_size=self.config.event_queue_size,
            process_leads=self._process_scraped_leads,
            prepare_leads=self._prepare_detail_leads,
        )

        for event in scheduler.events():
            if isinstance(event, KeywordResult):
                self._handle_keyword_result(event)
            elif isinstance(event, PlatformFinished):
                self._output.flush_platform(event.platform_worker)
                console.print(
                    f"[green]Platform completed:[/green] "
                    f"{event.platform_worker}"
                )

        self._output.finish()
        path = self._local_exporter.export(self._processed, self.run_id)
        self._repository.mark_run_completed()
        self.last_run_duration_seconds = time.monotonic() - run_started_at
        self._print_summary()
        self._print_sheets_summary()
        console.print(
            "[bold cyan]Total scraper run time:[/bold cyan] "
            f"{self._format_duration(self.last_run_duration_seconds)} "
            f"({self.last_run_duration_seconds:.2f} seconds)"
        )
        console.print(
            f"\n[bold green]Exported {len(self._processed)} qualified "
            f"leads to {path}[/bold green]"
        )
        return self.all_leads

    @staticmethod
    def _format_duration(total_seconds: float) -> str:
        whole_seconds = max(0, int(round(total_seconds)))
        hours, remainder = divmod(whole_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _hours_since(
        earlier: str | None,
        later: datetime,
    ) -> float | None:
        if not earlier:
            return None
        try:
            earlier_time = datetime.fromisoformat(earlier)
        except ValueError:
            return None
        if earlier_time.tzinfo is None:
            earlier_time = earlier_time.replace(tzinfo=timezone.utc)
        if later.tzinfo is None:
            later = later.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (later.astimezone(timezone.utc) - earlier_time.astimezone(timezone.utc))
            .total_seconds()
            / 3600,
        )

    def _handle_keyword_result(
        self,
        event: KeywordResult,
    ) -> None:
        if event.error:
            logger.warning(
                "%s failed for '%s': %s",
                event.platform_worker,
                event.keyword,
                event.error,
            )
            return

        platform_label = self._event_platform_label(event)
        scraped_count = event.scraped_count or len(event.leads)
        self._scraped_counts[platform_label] += scraped_count

        console.print(
            f"  {event.platform_worker} | {event.keyword}: "
            f"{scraped_count} scraped, "
            f"[green]{event.qualified_count} qualified/new[/green]"
        )

    def _prepare_detail_leads(self, platform_worker, keyword, leads):
        self._repository.checkpoint_search(keyword, leads)
        recency = RecencyFilter(
            min(24.0, self.run_policy.recency_hours)
            if self.run_policy.recency_hours is not None else 24.0,
            keep_unknown=False, strict_date_only=True, anchor_to_scraped_at=True,
        )
        return [lead for lead in leads
                if recency.matches(lead) and self._detail_deduplicator.accept(lead)]

    def _process_scraped_leads(
        self,
        platform_worker: str,
        keyword: str,
        leads: list[JobLead],
    ) -> int:
        """Run analysis in the keyword worker, then queue accepted output."""
        processor = getattr(self._processor_local, "processor", None)
        if processor is None:
            processor = LeadProcessor(
                LeadAnalyzer(),
                self._deduplicator,
                RecencyFilter(
                    min(24.0, self.run_policy.recency_hours)
                    if self.run_policy.recency_hours is not None else 24.0,
                    keep_unknown=False,
                    strict_date_only=True,
                    anchor_to_scraped_at=True,
                ),
            )
            self._processor_local.processor = processor

        accepted = 0
        for lead in leads:
            lead.keyword_searched = keyword
            item = processor.process(lead)
            if item is None:
                continue
            self._output.submit(
                platform_worker,
                keyword,
                item,
                self._deduplicator.key(lead),
            )
            accepted += 1
        return accepted

    def _print_summary(self) -> None:
        table = Table(title="Lead Collection Summary", show_lines=True)
        table.add_column("Platform", style="cyan")
        table.add_column("Scraped", justify="right")
        table.add_column("Qualified", justify="right")
        table.add_column("GREEN", justify="right", style="green")
        table.add_column("YELLOW", justify="right", style="yellow")
        table.add_column("RED", justify="right", style="red")

        summary: dict[str, Counter] = defaultdict(Counter)
        for item in self._processed:
            platform = PLATFORM_SHEET_MAP.get(
                item.lead.platform, item.lead.platform
            )
            summary[platform]["total"] += 1
            summary[platform][item.analysis.priority] += 1

        platforms = sorted(set(self._scraped_counts) | set(summary))
        for platform in platforms:
            counts = summary[platform]
            table.add_row(
                platform,
                str(self._scraped_counts[platform]),
                str(counts["total"]),
                str(counts["GREEN"]),
                str(counts["YELLOW"]),
                str(counts["RED"]),
            )
        table.add_section()
        table.add_row(
            "TOTAL",
            str(sum(self._scraped_counts.values())),
            str(sum(counts["total"] for counts in summary.values())),
            str(sum(counts["GREEN"] for counts in summary.values())),
            str(sum(counts["YELLOW"] for counts in summary.values())),
            str(sum(counts["RED"] for counts in summary.values())),
        )
        console.print(table)
         
    def _print_sheets_summary(self) -> None:
        if not self._sheets.enabled:
            console.print("[yellow]Google Sheets: disabled[/yellow]")
            return

        stats = self._sheets.stats()
        table = Table(title="Google Sheets Save Summary", show_lines=True)
        table.add_column("Platform", style="cyan")
        table.add_column("Eligible", justify="right")
        table.add_column("Saved", justify="right", style="green")
        table.add_column("Duplicates", justify="right", style="yellow")
        table.add_column("Below Score", justify="right")
        table.add_column("Pending", justify="right", style="red")

        for platform, counts in sorted(stats.items()):
            table.add_row(
                platform,
                str(counts["eligible"]),
                str(counts["saved"]),
                str(counts["duplicates"]),
                str(counts["below_score"]),
                str(counts["pending"]),
            )

        table.add_section()
        table.add_row(
            "TOTAL",
            str(sum(row["eligible"] for row in stats.values())),
            str(sum(row["saved"] for row in stats.values())),
            str(sum(row["duplicates"] for row in stats.values())),
            str(sum(row["below_score"] for row in stats.values())),
            str(sum(row["pending"] for row in stats.values())),
        )
        console.print(table)

    @staticmethod
    def _event_platform_label(event: KeywordResult) -> str:
        if event.source_platform or event.leads:
            platform = (
                event.source_platform
                or event.leads[0].platform
            )
            return PLATFORM_SHEET_MAP.get(platform, platform)
        return {
            "upwork": "Upwork",
            "upwork_selenium": "Upwork",
            "vollna": "Vollna",
            "freelancer": "Freelancer",
            "guru": "Guru",
            "bark": "Bark.com",
        }.get(event.platform_worker, event.platform_worker)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._output.close()
        except Exception as exc:
            logger.warning("Failed to close output worker: %s", exc)
        for adapter in self._adapters.values():
            try:
                adapter.close()
            except Exception as exc:
                logger.warning("Failed to close %s: %s", adapter.name, exc)
        self._repository.close()
        self._closed = True
