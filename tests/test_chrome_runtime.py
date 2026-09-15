"""Offline tests for portable Chrome/ChromeDriver selection."""

from __future__ import annotations

import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory

from pathlib import Path

from upwork_scraper.chrome_runtime import (
    chrome_launch_options,
    detect_chrome_major,
)


class ChromeRuntimeTests(unittest.TestCase):
    @patch("upwork_scraper.chrome_runtime.os.name", "nt")
    @patch("upwork_scraper.chrome_runtime._windows_file_major", return_value=151)
    def test_active_executable_wins_over_staged_update(
        self, file_major
    ) -> None:
        with TemporaryDirectory() as application_dir:
            Path(application_dir, "151.0.7922.174").mkdir()
            Path(application_dir, "152.0.7977.65").mkdir()
            chrome = Path(application_dir, "chrome.exe")
            chrome.touch()

            self.assertEqual(detect_chrome_major(str(chrome)), 151)

        file_major.assert_called_once_with(chrome)

    @patch("upwork_scraper.chrome_runtime.detect_chrome_major", return_value=150)
    def test_local_browser_pins_driver_download_to_installed_major(
        self, detect_major
    ) -> None:
        result = chrome_launch_options(
            {}, browser_finder=lambda: r"C:\Program Files\Chrome\chrome.exe"
        )

        self.assertEqual(result["version_main"], 150)
        self.assertEqual(
            result["browser_executable_path"],
            r"C:\Program Files\Chrome\chrome.exe",
        )
        detect_major.assert_called_once()

    @patch("upwork_scraper.chrome_runtime.detect_chrome_major")
    def test_docker_uses_distribution_matched_driver_without_download(
        self, detect_major
    ) -> None:
        result = chrome_launch_options(
            {
                "CHROME_BINARY": "/usr/bin/chromium",
                "CHROMEDRIVER_PATH": "/opt/undetected_chromedriver",
            },
            browser_finder=lambda: None,
        )

        self.assertEqual(
            result,
            {
                "browser_executable_path": "/usr/bin/chromium",
                "driver_executable_path": "/opt/undetected_chromedriver",
            },
        )
        detect_major.assert_not_called()

    def test_explicit_version_override_takes_precedence(self) -> None:
        result = chrome_launch_options(
            {"CHROME_VERSION_MAIN": "149.0.1"},
            browser_finder=lambda: None,
        )

        self.assertEqual(result["version_main"], 149)

    def test_invalid_explicit_version_has_a_clear_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "numeric major version"):
            chrome_launch_options(
                {"CHROME_VERSION_MAIN": "latest"},
                browser_finder=lambda: None,
            )

    def test_container_profile_is_persistent_and_worker_specific(self) -> None:
        with TemporaryDirectory() as profile_root:
            with patch(
                "upwork_scraper.chrome_runtime.threading.current_thread"
            ) as current_thread:
                current_thread.return_value.name = "upwork worker/1"
                result = chrome_launch_options(
                    {
                        "CHROME_BINARY": "/usr/bin/chromium",
                        "CHROMEDRIVER_PATH": "/opt/chromedriver",
                        "CHROME_PROFILE_ROOT": profile_root,
                    },
                    browser_finder=lambda: None,
                )

        self.assertTrue(
            str(result["user_data_dir"]).endswith("upwork-worker-1")
        )


if __name__ == "__main__":
    unittest.main()
