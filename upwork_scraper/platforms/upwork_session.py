"""Session-owned browser workers that survive individual collection runs."""

from concurrent.futures import ThreadPoolExecutor
from threading import Lock, current_thread, main_thread
from queue import Queue
import signal
import logging

from ..selenium_scraper import UpworkSeleniumScraper


class UpworkBrowserSession:
    def __init__(self, workers=2, factory=UpworkSeleniumScraper):
        self.worker_count = max(2, workers)
        self._factory = factory
        self._executors = [
            ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"upwork-browser-{i + 1}")
            for i in range(self.worker_count)
        ]
        self._scrapers = [None] * self.worker_count
        self._next = 0
        self._available = Queue()
        for index in range(self.worker_count):
            self._available.put(index)
        self._lock = Lock()
        self._closed = False

    def begin_run(self, config):
        """Apply run limits on the owning threads without replacing browsers."""
        if self._closed:
            raise RuntimeError("Upwork browser session is closed")

        def prepare(index):
            scraper = self._scrapers[index]
            if scraper is None:
                scraper = self._factory(config, profile_worker=index)
                self._scrapers[index] = scraper
            else:
                scraper.config = config
                scraper._seen_ids.clear()
                scraper._cached_leads = None

        futures = [executor.submit(prepare, i) for i, executor in enumerate(self._executors)]
        for future in futures:
            future.result()

    def search_keyword(self, keyword, **callbacks):
        index = self._available.get()
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Upwork browser session is closed")
                future = self._executors[index].submit(
                    self._scrapers[index].search_keyword, keyword, **callbacks
                )
            return future.result()
        finally:
            self._available.put(index)

    def enrich_client_locations(self, leads, **options):
        index = self._available.get()
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Upwork browser session is closed")
                future = self._executors[index].submit(
                    self._scrapers[index].enrich_client_locations, leads, **options
                )
            return future.result()
        finally:
            self._available.put(index)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        # A second Ctrl+C must not interrupt executor teardown and leave Python
        # trying to join the same workers again during interpreter shutdown.
        previous_handler = None
        if current_thread() is main_thread():
            previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            self._close_workers()
        finally:
            if previous_handler is not None:
                signal.signal(signal.SIGINT, previous_handler)

    def _close_workers(self):
        futures = [
            executor.submit(scraper.close)
            for executor, scraper in zip(self._executors, self._scrapers)
            if scraper is not None
        ]
        try:
            for future in futures:
                try:
                    future.result()
                except Exception:
                    logging.exception("Failed to close an Upwork browser worker.")
        finally:
            for executor in self._executors:
                executor.shutdown(wait=True)
