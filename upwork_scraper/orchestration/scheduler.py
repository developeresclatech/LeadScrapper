"""Bounded platform-level concurrency."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from threading import BoundedSemaphore
from typing import Callable, Iterator

from ..models import JobLead
from ..platforms.base import PlatformAdapter
from .events import KeywordResult, PlatformFinished

SchedulerEvent = KeywordResult | PlatformFinished


class PlatformScheduler:
    """Run platforms concurrently with bounded keyword workers."""

    def __init__(
        self,
        adapters: dict[str, PlatformAdapter],
        keywords: list[str],
        max_workers: int,
        max_browser_workers: int,
        queue_size: int,
        process_leads: (
            Callable[[str, str, list[JobLead]], int] | None
        ) = None,
        prepare_leads: Callable | None = None,
    ) -> None:
        self.adapters = adapters
        self.keywords = keywords
        self.max_workers = max(1, min(max_workers, len(adapters) or 1))
        self.max_browser_workers = max(1, max_browser_workers)
        self.queue_size = max(1, queue_size)
        self.process_leads = process_leads
        self.prepare_leads = prepare_leads

    def events(self) -> Iterator[SchedulerEvent]:
        queue: Queue[SchedulerEvent] = Queue(maxsize=self.queue_size)
        browser_slots = BoundedSemaphore(self.max_browser_workers)

        def scrape_keyword(
            adapter: PlatformAdapter,
            keyword: str,
        ) -> KeywordResult:
            try:
                qualified_count = 0

                def emit(lead):
                    nonlocal qualified_count
                    qualified_count += self.process_leads(adapter.name, keyword, [lead])

                streaming = adapter.stream_fn is not None and self.process_leads is not None

                def collect():
                    if streaming:
                        return adapter.stream_fn(
                            keyword,
                            on_lead=emit,
                            prepare_leads=(
                                lambda leads: self.prepare_leads(adapter.name, keyword, leads)
                            ) if self.prepare_leads else None,
                        )
                    return adapter.scrape(keyword)

                if adapter.resource_group == "browser":
                    with browser_slots:
                        leads = collect()
                else:
                    leads = collect()
                qualified_count += (
                    self.process_leads(adapter.name, keyword, leads)
                    if self.process_leads is not None and not streaming
                    else 0
                )
                return KeywordResult(
                    platform_worker=adapter.name,
                    keyword=keyword,
                    leads=[] if self.process_leads is not None else leads,
                    scraped_count=len(leads),
                    qualified_count=qualified_count,
                    source_platform=(
                        leads[0].platform if leads else ""
                    ),
                )
            except Exception as exc:
                return KeywordResult(
                    platform_worker=adapter.name,
                    keyword=keyword,
                    error=str(exc),
                )

        def run_platform(adapter: PlatformAdapter) -> None:
            try:
                if adapter.scrape_many_fn is not None:
                    try:
                        if adapter.resource_group == "browser":
                            with browser_slots:
                                results = adapter.scrape_many(self.keywords)
                        else:
                            results = adapter.scrape_many(self.keywords)
                        for keyword in self.keywords:
                            leads = results.get(keyword, [])
                            qualified_count = (
                                self.process_leads(
                                    adapter.name,
                                    keyword,
                                    leads,
                                )
                                if self.process_leads is not None
                                else 0
                            )
                            queue.put(
                                KeywordResult(
                                    platform_worker=adapter.name,
                                    keyword=keyword,
                                    leads=(
                                        []
                                        if self.process_leads is not None
                                        else leads
                                    ),
                                    scraped_count=len(leads),
                                    qualified_count=qualified_count,
                                    source_platform=(
                                        leads[0].platform if leads else ""
                                    ),
                                )
                            )
                    except Exception as exc:
                        for keyword in self.keywords:
                            queue.put(
                                KeywordResult(
                                    platform_worker=adapter.name,
                                    keyword=keyword,
                                    error=str(exc),
                                )
                            )
                    return

                workers = max(
                    1,
                    min(adapter.keyword_workers, len(self.keywords) or 1),
                )
                if workers == 1:
                    for keyword in self.keywords:
                        queue.put(scrape_keyword(adapter, keyword))
                    return

                with ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix=f"{adapter.name}-keyword",
                ) as keyword_executor:
                    futures = {
                        keyword_executor.submit(
                            scrape_keyword,
                            adapter,
                            keyword,
                        ): keyword
                        for keyword in self.keywords
                    }
                    for future in as_completed(futures):
                        queue.put(future.result())
            finally:
                queue.put(PlatformFinished(adapter.name))

        with ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="platform",
        ) as executor:
            futures = [
                executor.submit(run_platform, adapter)
                for adapter in self.adapters.values()
            ]
            finished = 0
            while finished < len(futures):
                event = queue.get()
                if isinstance(event, PlatformFinished):
                    finished += 1
                yield event

            for future in futures:
                future.result()
