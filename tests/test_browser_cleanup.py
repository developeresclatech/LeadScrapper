"""Offline tests for idempotent undetected-chromedriver cleanup."""

from __future__ import annotations

import unittest
from threading import Event
from unittest.mock import MagicMock
from types import SimpleNamespace

from upwork_scraper.browser_cleanup import close_chrome_safely


class _DriverFake:
    def __init__(self, quit_error: Exception | None = None) -> None:
        self.quit_error = quit_error
        self.quit_calls = 0
        self.close_calls = 0
        self.service = SimpleNamespace(process=object())
        self.reactor = object()
        self.keep_user_data_dir = False

    def quit(self) -> None:
        self.quit_calls += 1
        if self.quit_error:
            raise self.quit_error

    def close(self) -> None:
        self.close_calls += 1


class BrowserCleanupTests(unittest.TestCase):
    def test_hung_quit_is_bounded_and_forces_owned_driver_cleanup(self):
        release = Event()
        entered = Event()
        driver = _DriverFake()
        process = MagicMock()
        driver.service.process = process

        def hang():
            entered.set()
            release.wait(5)

        driver.quit = hang
        try:
            close_chrome_safely(driver, timeout=0.05)
            self.assertTrue(entered.is_set())
            process.kill.assert_called_once()
            self.assertTrue(driver.keep_user_data_dir)
        finally:
            release.set()

    def test_cleanup_neutralizes_the_library_destructor_paths(self) -> None:
        driver = _DriverFake()

        close_chrome_safely(driver)

        self.assertEqual(driver.quit_calls, 1)
        self.assertIsNone(driver.service.process)
        self.assertIsNone(driver.reactor)
        self.assertTrue(driver.keep_user_data_dir)

        driver.quit()
        self.assertEqual(driver.quit_calls, 1)

    def test_cleanup_falls_back_to_close_when_quit_fails(self) -> None:
        driver = _DriverFake(OSError(6, "The handle is invalid"))

        close_chrome_safely(driver)

        self.assertEqual(driver.quit_calls, 1)
        self.assertEqual(driver.close_calls, 1)
        driver.quit()
        self.assertEqual(driver.quit_calls, 1)


if __name__ == "__main__":
    unittest.main()
