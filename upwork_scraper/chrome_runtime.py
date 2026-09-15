"""Resolve a compatible Chrome/ChromeDriver setup on hosts and in Docker."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import ctypes
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Mapping

import undetected_chromedriver as uc

logger = logging.getLogger(__name__)

_VERSION_PATTERN = re.compile(r"\b(\d+)\.\d+(?:\.\d+){0,2}\b")


def _major_from_text(value: str) -> int | None:
    match = _VERSION_PATTERN.search(value or "")
    return int(match.group(1)) if match else None


def _windows_file_major(path: Path) -> int | None:
    """Read the executable's product version from its Windows resources."""
    if os.name != "nt":
        return None
    try:
        version = ctypes.windll.version
        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None
        value = ctypes.c_void_p()
        value_size = wintypes.UINT()
        if not version.VerQueryValueW(
            buffer, "\\", ctypes.byref(value), ctypes.byref(value_size)
        ):
            return None
        # VS_FIXEDFILEINFO starts with two DWORDs, followed by the product
        # version MS DWORD whose high word is Chrome's major version.
        fields = ctypes.cast(value, ctypes.POINTER(wintypes.DWORD))
        return fields[4] >> 16
    except (AttributeError, OSError, ValueError):
        return None


def detect_chrome_major(browser_path: str) -> int | None:
    """Return the installed browser major without assuming an operating system."""
    path = Path(browser_path)

    # Read chrome.exe itself first. During an update, its directory can contain
    # both the active version and a newer staged version.
    if os.name == "nt":
        executable_major = _windows_file_major(path)
        if executable_major is not None:
            return executable_major

    # Keep the directory scan as a fallback for unusual Windows executables
    # whose version resource cannot be read.
    if os.name == "nt" and path.parent.is_dir():
        versions = [
            version
            for child in path.parent.iterdir()
            if child.is_dir()
            and (version := _major_from_text(child.name)) is not None
        ]
        if versions:
            return max(versions)

    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _major_from_text(f"{result.stdout} {result.stderr}")


def chrome_launch_options(
    environ: Mapping[str, str] | None = None,
    browser_finder: Callable[[], str | None] | None = None,
) -> dict[str, object]:
    """Build portable keyword arguments for ``uc.Chrome``.

    Docker supplies a browser and its distribution-matched driver. On a local
    host, undetected-chromedriver downloads a driver for the detected browser
    major instead of blindly selecting the newest available release.
    """
    env = os.environ if environ is None else environ
    finder = browser_finder or uc.find_chrome_executable
    browser_path = (env.get("CHROME_BINARY") or "").strip() or finder()
    driver_path = (env.get("CHROMEDRIVER_PATH") or "").strip()
    profile_root = (env.get("CHROME_PROFILE_ROOT") or "").strip()
    configured_version = (env.get("CHROME_VERSION_MAIN") or "").strip()

    options: dict[str, object] = {}
    if browser_path:
        options["browser_executable_path"] = browser_path
    if driver_path:
        options["driver_executable_path"] = driver_path
    if profile_root:
        worker_name = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "-",
            threading.current_thread().name,
        ).strip("-.") or "main"
        profile_path = Path(profile_root) / worker_name
        profile_path.mkdir(parents=True, exist_ok=True)
        options["user_data_dir"] = str(profile_path)

    if configured_version:
        try:
            major = int(configured_version.split(".", 1)[0])
        except ValueError as exc:
            raise ValueError(
                "CHROME_VERSION_MAIN must start with a numeric major version"
            ) from exc
    elif browser_path and not driver_path:
        major = detect_chrome_major(browser_path)
    else:
        # A supplied driver (the Docker default) is already paired with its
        # browser by the package manager and must not trigger a download.
        major = None

    if major is not None:
        options["version_main"] = major
        logger.info("Using Chrome/Chromium major version %d", major)
    elif not driver_path:
        logger.warning(
            "Could not detect the Chrome major version; set "
            "CHROME_VERSION_MAIN if driver auto-selection fails."
        )
    return options
