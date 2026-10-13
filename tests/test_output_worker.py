from __future__ import annotations

import threading
import unittest

from upwork_scraper.analyzer import LeadAnalysis
from upwork_scraper.models import JobLead
from upwork_scraper.orchestration.output_worker import (
    LeadOutputWorker,
    PlatformOutputWorkers,
)
from upwork_scraper.pipeline.processor import ProcessedLead


class _RepositoryFake:
    def pending_uploads(self):
        return []

    def __init__(self) -> None:
        self.saved: list[tuple[str, str]] = []
        self.threads: list[str] = []

    def save(
        self,
        platform_worker: str,
        keyword: str,
        item: ProcessedLead,
        dedup_key: str,
    ) -> None:
        self.saved.append((platform_worker, keyword))
        self.threads.append(threading.current_thread().name)


class _SheetsFake:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.flushed: list[str] = []
        self.threads: list[str] = []

    def add(self, item: ProcessedLead, dedup_key: str) -> None:
        self.added.append(dedup_key)
        self.threads.append(threading.current_thread().name)

    def flush_platform(self, platform_label: str) -> None:
        self.flushed.append(platform_label)
        self.threads.append(threading.current_thread().name)

    def flush_all(self) -> None:
        self.flushed.append("all")
        self.threads.append(threading.current_thread().name)


class OutputWorkerTests(unittest.TestCase):
    def test_sqlite_commits_before_async_sheets_output(self) -> None:
        repository = _RepositoryFake()
        sheets = _SheetsFake()
        worker = LeadOutputWorker(
            "upwork_selenium", repository, sheets, queue_size=10
        )
        item = ProcessedLead(
            JobLead(title="Lead", platform="Upwork"),
            LeadAnalysis(priority="GREEN", lead_score=70),
        )

        worker.start()
        worker.submit("marketing", item, "lead")
        worker.flush()
        worker.finish()

        self.assertEqual(repository.saved, [("upwork_selenium", "marketing")])
        self.assertEqual(sheets.added, ["lead"])
        self.assertEqual(
            sheets.flushed,
            ["upwork_selenium", "upwork_selenium"],
        )
        self.assertEqual(len(worker.processed), 1)
        self.assertTrue(
            all(
                name == threading.current_thread().name
                for name in repository.threads
            )
        )
        self.assertTrue(
            all(
                name == "upwork_selenium-output"
                for name in sheets.threads
            )
        )

    def test_pool_routes_each_platform_to_its_own_thread(self) -> None:
        repository = _RepositoryFake()
        sheets = _SheetsFake()
        pool = PlatformOutputWorkers(
            ["upwork_selenium", "guru"],
            repository,
            sheets,
            queue_size=10,
        )
        upwork = ProcessedLead(
            JobLead(title="Upwork Lead", platform="Upwork"),
            LeadAnalysis(priority="GREEN", lead_score=70),
        )
        guru = ProcessedLead(
            JobLead(title="Guru Lead", platform="Guru"),
            LeadAnalysis(priority="YELLOW", lead_score=50),
        )

        pool.start()
        pool.submit("upwork_selenium", "react", upwork, "upwork")
        pool.submit("guru", "react", guru, "guru")
        pool.finish()

        self.assertEqual(
            set(repository.threads),
            {threading.current_thread().name},
        )
        self.assertEqual(len(pool.processed), 2)


if __name__ == "__main__":
    unittest.main()
