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

    def test_upwork_profile_is_stable_across_worker_names(self) -> None:
        with TemporaryDirectory() as profile_root:
            env = {"CHROME_PROFILE_ROOT": profile_root, "CHROME_VERSION_MAIN": "149"}
            with patch("upwork_scraper.chrome_runtime.threading.current_thread") as thread:
                thread.return_value.name = "first-run"
                first = chrome_launch_options(env, browser_finder=lambda: None, profile_name="upwork")
                thread.return_value.name = "second-run"
                second = chrome_launch_options(env, browser_finder=lambda: None, profile_name="upwork")
            self.assertEqual(first["user_data_dir"], second["user_data_dir"])
            self.assertEqual(Path(first["user_data_dir"]), Path(profile_root).resolve() / "upwork")
            self.assertTrue(Path(first["user_data_dir"]).is_dir())

    def test_upwork_can_reuse_explicit_user_data_directory(self) -> None:
        with TemporaryDirectory() as profile_root:
            result = chrome_launch_options(
                {"UPWORK_CHROME_USER_DATA_DIR": profile_root, "CHROME_VERSION_MAIN": "149"},
                browser_finder=lambda: None, profile_name="upwork",
            )
            self.assertEqual(Path(result["user_data_dir"]), Path(profile_root).resolve())

    def test_upwork_workers_have_separate_stable_profiles(self) -> None:
        with TemporaryDirectory() as root:
            env = {"CHROME_PROFILE_ROOT": root, "CHROME_VERSION_MAIN": "149"}
            first = chrome_launch_options(env, browser_finder=lambda: None, profile_name="upwork", profile_worker=0)
            second = chrome_launch_options(env, browser_finder=lambda: None, profile_name="upwork", profile_worker=1)
            again = chrome_launch_options(env, browser_finder=lambda: None, profile_name="upwork", profile_worker=1)
            self.assertNotEqual(first["user_data_dir"], second["user_data_dir"])
            self.assertEqual(second["user_data_dir"], again["user_data_dir"])
            self.assertEqual(Path(second["user_data_dir"]).name, "upwork-worker-2")
            self.assertTrue(Path(second["user_data_dir"]).is_dir())

    def test_upwork_rejects_example_profile_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "placeholder"):
            chrome_launch_options(
                {"UPWORK_CHROME_USER_DATA_DIR": "C:/Users/YOUR_USER/AppData/Local/Google/Chrome/User Data"},
                browser_finder=lambda: None, profile_name="upwork",
            )

    def test_upwork_rejects_normal_chrome_user_data_root(self) -> None:
        with TemporaryDirectory() as local_app_data:
            normal_root = Path(local_app_data) / "Google" / "Chrome" / "User Data"
            normal_root.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "Chrome 136"):
                chrome_launch_options(
                    {"LOCALAPPDATA": local_app_data, "UPWORK_CHROME_USER_DATA_DIR": str(normal_root)},
                    browser_finder=lambda: None, profile_name="upwork",
                )

    def test_upwork_does_not_create_mistyped_existing_profile(self) -> None:
        with TemporaryDirectory() as root:
            missing = Path(root) / "missing"
            with self.assertRaisesRegex(ValueError, "does not exist"):
                chrome_launch_options(
                    {"UPWORK_CHROME_USER_DATA_DIR": str(missing)},
                    browser_finder=lambda: None, profile_name="upwork",
                )
            self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
