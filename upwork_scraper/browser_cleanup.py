"""Idempotent cleanup for undetected-chromedriver on Windows."""

from __future__ import annotations

import logging
import os
import signal
from threading import Thread

logger = logging.getLogger(__name__)


def _neutralize_driver_destructor(driver: object) -> None:
    try:
        driver.service.process = None
    except Exception:
        pass
    try:
        driver.reactor = None
    except Exception:
        pass
    try:
        driver.keep_user_data_dir = True
    except Exception:
        pass
    try:
        driver.quit = lambda: None
    except Exception:
        pass


def close_chrome_safely(driver: object | None, *, timeout: float = 5.0) -> None:
    """Quit Chrome once, then make uc.Chrome.__del__ harmless.

    undetected-chromedriver 3.5.5 calls quit() unconditionally from __del__.
    On Windows/Python 3.14 that second call can raise WinError 6 while the
    interpreter is shutting down.
    """
    if driver is None:
        return

    def quit_browser():
        try:
            driver.quit()
        except Exception as exc:
            logger.debug("Chrome quit raised during cleanup: %s", exc)
            try:
                driver.close()
            except Exception:
                pass

    # Chrome's quit() may block on a dead driver. A daemon helper prevents that
    # command from trapping a session executor (which Python joins at exit).
    closer = Thread(target=quit_browser, name="chrome-cleanup", daemon=True)
    closer.start()
    closer.join(timeout=max(0, timeout))
    if closer.is_alive():
        logger.warning("Chrome cleanup timed out; stopping this browser's processes.")
        discard_chrome_safely(driver)
    else:
        _neutralize_driver_destructor(driver)


def discard_chrome_safely(driver: object | None) -> None:
    """Force-discard an unresponsive driver without another HTTP command."""
    if driver is None:
        return
    try:
        process = getattr(getattr(driver, "service", None), "process", None)
        if process is not None:
            process.kill()
    except Exception as exc:
        logger.debug("ChromeDriver process kill raised: %s", exc)
    try:
        browser_pid = getattr(driver, "browser_pid", None)
        if browser_pid:
            os.kill(int(browser_pid), signal.SIGTERM)
    except Exception as exc:
        logger.debug("Chrome browser process kill raised: %s", exc)
    finally:
        _neutralize_driver_destructor(driver)
