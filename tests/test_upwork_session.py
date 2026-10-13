import unittest
import signal
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, get_ident

from upwork_scraper.config import ScraperConfig
from upwork_scraper.platforms.upwork_session import UpworkBrowserSession
from upwork_scraper.platforms.registry import build_platform_adapters


class SessionTests(unittest.TestCase):
    def test_shutdown_temporarily_ignores_repeated_ctrl_c(self):
        session = UpworkBrowserSession()
        original = signal.getsignal(signal.SIGINT)
        with patch.object(session, "_close_workers") as close:
            close.side_effect = lambda: self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
            session.close()
        self.assertEqual(signal.getsignal(signal.SIGINT), original)
        for executor in session._executors:
            executor.shutdown()

    def test_workers_and_browsers_survive_cycles_and_close_on_owner_threads(self):
        created = []
        barrier = Barrier(2)

        class Scraper:
            def __init__(self, config, profile_worker):
                self.config = config
                self.owner = get_ident()
                self.browser = object()
                self._seen_ids = set()
                self._cached_leads = None
                self.closed = 0
                created.append(self)

            def search_keyword(self, keyword):
                assert get_ident() == self.owner
                barrier.wait(timeout=5)
                self._seen_ids.add(keyword)
                return [self.browser]

            def close(self):
                assert get_ident() == self.owner
                self.closed += 1

        session = UpworkBrowserSession(2, factory=Scraper)
        config = ScraperConfig(platforms=["upwork_selenium"])
        try:
            first = build_platform_adapters(config, upwork_session=session)
            with ThreadPoolExecutor(2) as callers:
                browsers = list(callers.map(first["upwork_selenium"].scrape, ["a", "b"]))
            first["upwork_selenium"].close()
            self.assertTrue(all(s.closed == 0 for s in created))
            new_config = ScraperConfig(platforms=["upwork_selenium"], page_limit=7)
            second = build_platform_adapters(new_config, upwork_session=session)
            self.assertTrue(all(not s._seen_ids and s.config.page_limit == 7 for s in created))
            with ThreadPoolExecutor(2) as callers:
                reused = list(callers.map(second["upwork_selenium"].scrape, ["c", "d"]))
            self.assertEqual({row[0] for row in browsers}, {row[0] for row in reused})
            self.assertEqual(len(created), 2)
            self.assertEqual(len({s.owner for s in created}), 2)
        finally:
            session.close()
        session.close()
        self.assertTrue(all(s.closed == 1 for s in created))
