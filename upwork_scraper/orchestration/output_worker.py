"""Per-platform persistence and Google Sheets output workers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from queue import Queue
from threading import Lock, Thread

from ..analyzer import LeadAnalysis
from ..exporters.sheets import SheetsBatchWriter
from ..models import JobLead
from ..pipeline.processor import ProcessedLead
from ..storage.sqlite_repository import SQLiteLeadRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SaveLead:
    platform_worker: str
    keyword: str
    item: ProcessedLead
    dedup_key: str


@dataclass(frozen=True)
class _FlushPlatform:
    platform_label: str


@dataclass(frozen=True)
class _ReplayUploads:
    entries: list


class _Stop:
    pass


class LeadOutputWorker:
    """Own one platform's SQLite and Sheets output queue."""

    def __init__(
        self,
        platform_worker: str,
        repository: SQLiteLeadRepository,
        sheets: SheetsBatchWriter,
        queue_size: int,
        processed: list[ProcessedLead] | None = None,
        all_leads: list[JobLead] | None = None,
        all_analyses: dict[str, LeadAnalysis] | None = None,
        collection_lock: Lock | None = None,
    ) -> None:
        self.platform_worker = platform_worker
        self.repository = repository
        self.sheets = sheets
        self.queue: Queue[object] = Queue(maxsize=max(1, queue_size))
        self.processed = processed if processed is not None else []
        self.all_leads = all_leads if all_leads is not None else []
        self.all_analyses = (
            all_analyses if all_analyses is not None else {}
        )
        self._collection_lock = collection_lock or Lock()
        self._errors: list[Exception] = []
        self._error_lock = Lock()
        self._started = False
        self._finished = False
        self._thread = Thread(
            target=self._run,
            name=f"{platform_worker}-output",
            daemon=True,
        )

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def submit(
        self,
        keyword: str,
        item: ProcessedLead,
        dedup_key: str,
    ) -> None:
        # Commit before queueing: a slow or unavailable Sheets connection must
        # not leave accepted leads existing only in a process's memory.
        upload_key = self.repository.save(self.platform_worker, keyword, item, dedup_key)
        with self._collection_lock:
            self.processed.append(item)
            self.all_leads.append(item.lead)
            self.all_analyses[item.lead.title] = item.analysis
        self.queue.put(
            _SaveLead(self.platform_worker, keyword, item, upload_key or dedup_key)
        )

    def flush(self) -> None:
        self.queue.put(_FlushPlatform(self.platform_worker))

    def finish(self) -> None:
        if not self._started or self._finished:
            return
        self.flush()
        self.queue.put(_Stop())
        self._thread.join()
        self._finished = True
        if self._errors:
            raise RuntimeError(
                f"Output worker encountered {len(self._errors)} error(s)"
            ) from self._errors[0]

    def close(self) -> None:
        if not self._started or self._finished:
            return
        self.finish()

    def _run(self) -> None:
        while True:
            command = self.queue.get()
            try:
                if isinstance(command, _Stop):
                    return
                if isinstance(command, _SaveLead):
                    self._save(command)
                elif isinstance(command, _ReplayUploads):
                    for item, key in command.entries:
                        self.sheets.add(item, key)
                elif isinstance(command, _FlushPlatform):
                    self.sheets.flush_platform(command.platform_label)
            except Exception as exc:
                logger.exception("Output worker command failed")
                with self._error_lock:
                    self._errors.append(exc)
            finally:
                self.queue.task_done()

    def _save(self, command: _SaveLead) -> None:
        self.sheets.add(command.item, command.dedup_key)


class PlatformOutputWorkers:
    """Route accepted leads to one bounded output worker per platform."""

    def __init__(
        self,
        platform_workers: list[str],
        repository: SQLiteLeadRepository,
        sheets: SheetsBatchWriter,
        queue_size: int,
    ) -> None:
        self._repository = repository
        self._sheets = sheets
        self._queue_size = queue_size
        self.processed: list[ProcessedLead] = []
        self.all_leads: list[JobLead] = []
        self.all_analyses: dict[str, LeadAnalysis] = {}
        self._collection_lock = Lock()
        self._workers_lock = Lock()
        self._workers: dict[str, LeadOutputWorker] = {}
        self._started = False
        self._finished = False
        for platform_worker in platform_workers:
            self._ensure_worker(platform_worker)

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    def start(self) -> None:
        if self._started:
            return
        pending = self._repository.pending_uploads()
        if pending and self._sheets.enabled:
            # A dedicated output queue replays durable records without holding
            # up browser workers or falsely adding them to this run's totals.
            recovery = self._ensure_worker("pending_uploads")
            recovery.start()
            recovery.queue.put(_ReplayUploads(pending))
        for worker in self._workers.values():
            worker.start()
        self._started = True

    def submit(
        self,
        platform_worker: str,
        keyword: str,
        item: ProcessedLead,
        dedup_key: str,
    ) -> None:
        self._ensure_worker(platform_worker).submit(
            keyword, item, dedup_key
        )

    def flush_platform(self, platform_worker: str) -> None:
        self._ensure_worker(platform_worker).flush()

    def finish(self) -> None:
        if not self._started or self._finished:
            return
        errors: list[Exception] = []
        for worker in self._workers.values():
            try:
                worker.finish()
            except Exception as exc:
                errors.append(exc)
        self._finished = True
        if errors:
            raise RuntimeError(
                f"{len(errors)} platform output worker(s) failed"
            ) from errors[0]

    def close(self) -> None:
        self.finish()

    def _ensure_worker(self, platform_worker: str) -> LeadOutputWorker:
        with self._workers_lock:
            worker = self._workers.get(platform_worker)
            if worker is not None:
                return worker
            worker = LeadOutputWorker(
                platform_worker,
                self._repository,
                self._sheets,
                self._queue_size,
                self.processed,
                self.all_leads,
                self.all_analyses,
                self._collection_lock,
            )
            self._workers[platform_worker] = worker
            if self._started:
                worker.start()
            return worker
