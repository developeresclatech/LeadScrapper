"""
Upwork scraper using Selenium + undetected_chromedriver.
Logs in with user credentials and scrapes job listings directly
from Upwork's search results. Returns JobLead objects.
"""

import re
import time
import json
import logging
import os
import sys
from urllib.parse import urlencode
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

import undetected_chromedriver as uc
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .config import ScraperConfig
from .browser_cleanup import close_chrome_safely, discard_chrome_safely
from .chrome_runtime import chrome_launch_options
from .models import JobLead
from .pipeline.recency_filter import RecencyFilter

logger = logging.getLogger(__name__)
_DRIVER_LAUNCH_LOCK = Lock()
_LOGIN_LOCK = Lock()
_LOCATION_CACHE_LOCK = Lock()
_LOCATION_CACHE: dict[str, str] = {}

LOGIN_URL = "https://www.upwork.com/ab/account-security/login"
SEARCH_URL = "https://www.upwork.com/nx/search/jobs/"

JOB_CARD_SELECTORS = [
    "article[data-test='JobTile']",
    "section[data-test='JobTile']",
    "[data-test='job-tile-list'] article",
    "[data-test='job-tile-list'] section",
]
JOB_LINK_SELECTORS = [
    "a[data-test='job-tile-title-link']",
    "h2 a[href*='/jobs/']",
    "a[href*='/jobs/']",
]
CLIENT_LOCATION_SELECTORS = [
    "[data-qa='client-location']",
    "[data-test='client-location']",
    "[data-test='client-country']",
]


class UpworkSeleniumScraper:
    """Scrape Upwork job data using Selenium with undetected_chromedriver.
    Requires a logged-in Upwork session (credentials in .env)."""

    def __init__(self, config: Optional[ScraperConfig] = None, *, profile_worker: int = 0):
        self.config = config or ScraperConfig()
        self._profile_worker = profile_worker
        self._driver: Optional[uc.Chrome] = None
        self._seen_ids: set[str] = set()
        self._cached_leads: Optional[list[JobLead]] = None
        self._login_retry_at = 0.0
        self._authenticated = False
        self._manual_login_pending = False

    # ==================================================================
    # Public API
    # ==================================================================

    def search_keyword(self, keyword: str, *, on_lead=None, prepare_leads=None) -> list[JobLead]:
        for attempt in range(2):
            try:
                leads = self._scrape_keyword(keyword)
                break
            except Exception as exc:
                if attempt == 0 and not self._manual_login_pending and self._is_browser_failure(exc):
                    logger.warning(
                        "Upwork browser failed for '%s'; restarting its "
                        "isolated browser and retrying once: %s",
                        keyword,
                        exc,
                    )
                    self._discard_driver()
                    continue
                raise
        candidates = prepare_leads(leads) if prepare_leads else leads
        logger.info("Upwork worker %d: %d searched, %d detail checks; saving each completed lead.",
                    self._profile_worker + 1, len(leads), len(candidates))
        for lead in candidates:
            try:
                self.enrich_attachments([lead])
                self.enrich_client_locations([lead], ensure_logged_in=False)
            except Exception as exc:
                logger.warning("Detail check incomplete for %s (%s); preserving collected data.",
                               lead.job_id or lead.title, type(exc).__name__)
            if on_lead:
                on_lead(lead)
        return leads[:self.config.max_results_per_keyword]

    def enrich_client_locations(
        self,
        leads: list[JobLead],
        *,
        ensure_logged_in: bool = True,
    ) -> None:
        """Populate missing client countries from Upwork detail pages."""
        unresolved = [
            lead
            for lead in leads
            if not (lead.country or lead.location)
            and self._location_cache_key(lead.url)
        ]
        if not unresolved:
            return

        driver = None
        resolved_count = 0
        unavailable_count = 0
        for lead in unresolved:
            cache_key = self._location_cache_key(lead.url)
            with _LOCATION_CACHE_LOCK:
                cached = _LOCATION_CACHE.get(cache_key)
            if cached:
                lead.country = cached
                lead.location = cached
                resolved_count += 1
                continue

            try:
                if driver is None:
                    driver = self._get_driver()
                    if (
                        ensure_logged_in
                        and not self._ensure_logged_in(driver)
                    ):
                        logger.warning(
                            "Cannot enrich Upwork client locations "
                            "without an authenticated session."
                        )
                        return
                location = self._resolve_client_location(driver, lead.url)
            except Exception as exc:
                if self._is_browser_failure(exc):
                    logger.warning(
                        "Upwork location browser failed for %s: %s",
                        lead.url,
                        str(exc).splitlines()[0],
                    )
                    self._discard_driver()
                    driver = None
                else:
                    logger.debug(
                        "Upwork client location unavailable for %s: %s",
                        lead.url,
                        type(exc).__name__,
                    )
                unavailable_count += 1
                continue

            if not location:
                unavailable_count += 1
                continue
            lead.country = location
            lead.location = location
            resolved_count += 1
            with _LOCATION_CACHE_LOCK:
                _LOCATION_CACHE[cache_key] = location
        logger.info(
            "Upwork detail locations: %d resolved, %d unavailable.",
            resolved_count,
            unavailable_count,
        )

        

    def _resolve_client_location(self, driver, url: str) -> str:
        driver.get(url)
        selector = ", ".join(CLIENT_LOCATION_SELECTORS)
        timeout = max(
            1,
            getattr(self.config, "upwork_location_timeout", 12),
        )
        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
        except TimeoutException:
            return ""
        raw_location = self._first_text(
            driver,
            CLIENT_LOCATION_SELECTORS,
        )
        return self._clean_client_location(raw_location)

    @staticmethod
    def _location_cache_key(url: str) -> str:
        if "upwork.com" not in (url or "").casefold():
            return ""
        match = re.search(r"(~\d+)", url)
        return match.group(1) if match else url.split("?", 1)[0]

    @staticmethod
    def _clean_client_location(value: str) -> str:
        """Return the country line from Upwork's location detail block."""
        lines = [
            line.strip()
            for line in (value or "").splitlines()
            if line.strip()
        ]
        return lines[0] if lines else ""

    def close(self):
        if self._driver:
            close_chrome_safely(self._driver)
            self._driver = None
            logger.info("Browser closed.")

    def enrich_attachments(self, leads: list[JobLead]) -> None:
        """Inspect authenticated job details without downloading files."""
        leads = [lead for lead in leads if lead.url and lead.attachment_status == "Not checked"]
        if not leads:
            return
        driver = self._get_driver()
        for index, lead in enumerate(leads, 1):
            if not lead.url or lead.attachment_status != "Not checked":
                continue
            logger.info(
                "Upwork worker %d: attachment check %d/%d for '%s'.",
                self._profile_worker + 1, index, len(leads), lead.keyword_searched,
            )
            try:
                driver.get(lead.url)
                WebDriverWait(driver, self.config.timeout).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR,
                        "[data-test='job-description-text'], [data-test='description' i], "
                        "[data-qa='job-description'], .job-description"))
                )
                if self._is_verification_page(driver) or not self._is_logged_in(driver):
                    continue
                self._read_attachments(driver, lead)
            except (TimeoutException, WebDriverException):
                logger.warning(
                    "Upwork worker %d: attachment check %d/%d unavailable; "
                    "continuing with attachment status unknown.",
                    self._profile_worker + 1, index, len(leads),
                )
        logger.info(
            "Upwork worker %d: attachment checks finished for %d jobs.",
            self._profile_worker + 1, len(leads),
        )

    @staticmethod
    def _read_attachments(driver, lead: JobLead) -> None:
        selectors = (
            "[data-test*='attachment'] a[href], [data-qa*='attachment'] a[href], "
            "a[href*='/att/download/'], a[href*='/attachments/']"
        )
        files = {}
        for element in driver.find_elements(By.CSS_SELECTOR, selectors):
            if not element.is_displayed():
                continue
            url = element.get_attribute("href")
            if url and not url.startswith("javascript:"):
                files[url] = element.text.strip() or element.get_attribute("download") or "Attachment"
        # Only declare absence after a rendered authenticated detail page.
        lead.attachment_status = "Yes" if files else "No"
        lead.attachment_count = len(files)
        lead.attachment_urls = list(files)
        lead.attachment_names = list(files.values())

    # ==================================================================
    # Selenium Lifecycle
    # ==================================================================

    def _get_driver(self) -> uc.Chrome:
        if self._driver is not None:
            try:
                self._driver.current_url
                return self._driver
            except Exception:
                if self._manual_login_pending:
                    raise RuntimeError(
                        "Manual-login browser is disconnected. Automatic restart is paused; "
                        "stop the scraper and restart it when ready."
                    ) from None
                logger.info("Browser session lost, recreating...")
                self._discard_driver()
        logger.info("Launching Chrome via undetected_chromedriver...")
        with _DRIVER_LAUNCH_LOCK:
            options = uc.ChromeOptions()
            options.add_argument(
                "--disable-blink-features=AutomationControlled"
            )
            options.add_argument("--start-maximized")
            options.add_experimental_option("prefs", {
                "credentials_enable_service": True,
                "profile.password_manager_enabled": True,
            })
            profile_directory = os.getenv("UPWORK_CHROME_PROFILE_DIRECTORY", "Default").strip() or "Default"
            options.add_argument(f"--profile-directory={profile_directory}")
            if os.getenv("RUNNING_IN_CONTAINER") == "1":
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-dev-shm-usage")
            launch_options: dict[str, object] = {"options": options}
            launch_options.update(chrome_launch_options(profile_name="upwork", profile_worker=self._profile_worker))
            self._driver = uc.Chrome(**launch_options)
            logger.info("Chrome automation connected; preparing Upwork navigation.")
        self._driver.set_page_load_timeout(120)
        client_config = getattr(
            self._driver.command_executor,
            "_client_config",
            None,
        )
        if client_config is not None:
            client_config.timeout = self.config.selenium_command_timeout
        return self._driver

    def _discard_driver(self) -> None:
        driver = self._driver
        self._driver = None
        discard_chrome_safely(driver)

    @staticmethod
    def _is_browser_failure(exc: Exception) -> bool:
        if isinstance(exc, WebDriverException):
            return True
        message = str(exc).casefold()
        return (
            "localhost" in message
            and (
                "timed out" in message
                or "connection" in message
                or "max retries" in message
            )
        )

    def _ensure_logged_in(self, driver: uc.Chrome) -> bool:
        if self.config.upwork_manual_login:
            return self._ensure_manual_login(driver)
        if self._authenticated and self._is_logged_in(driver):
            return True
        if time.monotonic() < self._login_retry_at:
            if self._is_logged_in(driver):
                self._authenticated = True
                self._login_retry_at = 0.0
                return True
            remaining = max(1, self._login_retry_at - time.monotonic())
            logger.warning(
                "Waiting up to %.0fs for manual Upwork login in this worker's "
                "browser. Complete login/2FA there to resume this keyword.",
                remaining,
            )
            try:
                WebDriverWait(driver, remaining).until(self._is_logged_in)
            except TimeoutException:
                raise RuntimeError(
                    "Upwork authentication is cooling down after a failed login; "
                    "manual login was not completed before the cooldown ended."
                ) from None
            self._authenticated = True
            self._login_retry_at = 0.0
            return True
        # Serialize authentication only; searches remain parallel.
        if not _LOGIN_LOCK.acquire(timeout=self.config.upwork_login_timeout):
            raise RuntimeError("Another Upwork worker is logging in; login lock wait timed out.")
        try:
            self._authenticated = self._attempt_login(driver)
            if not self._authenticated:
                self._login_retry_at = time.monotonic() + self.config.upwork_login_cooldown
                raise RuntimeError(
                    f"Upwork login failed; pausing this worker's login attempts for "
                    f"{self.config.upwork_login_cooldown}s. Complete verification in its browser."
                )
            self._login_retry_at = 0.0
            return True
        except Exception:
            self._login_retry_at = time.monotonic() + self.config.upwork_login_cooldown
            raise
        finally:
            _LOGIN_LOCK.release()

    def _ensure_manual_login(self, driver: uc.Chrome) -> bool:
        """Leave the same page open for the operator; never fill credentials."""
        try:
            if self._is_logged_in(driver):
                self._authenticated = True
                self._manual_login_pending = False
                self._login_retry_at = 0.0
                return True
            if not self._manual_login_pending:
                self._manual_login_pending = True
                driver.get(SEARCH_URL)
                time.sleep(3)
                if self._is_logged_in(driver):
                    self._authenticated = True
                    self._manual_login_pending = False
                    return True
                # Leave a verification challenge untouched. Otherwise open
                # login once, and retain this page across subsequent waits.
                if not self._is_verification_page(driver):
                    driver.get(LOGIN_URL)
            self._authenticated = False
            timeout = max(1, self.config.upwork_verification_timeout)
            logger.warning(
                "Upwork worker %d: complete manual login/verification in its "
                "browser. Waiting %ss; credentials will not be submitted "
                "automatically and this page will stay open.",
                self._profile_worker + 1, timeout,
            )
            WebDriverWait(driver, timeout).until(self._is_logged_in)
        except TimeoutException:
            raise RuntimeError(
                "Manual Upwork login is still pending; browser remains open. "
                "Complete sign-in there to resume subsequent work."
            ) from None
        except Exception as exc:
            raise RuntimeError(
                "Manual Upwork login could not be checked; automatic browser "
                "restart and credential submission are paused."
            ) from exc
        self._authenticated = True
        self._manual_login_pending = False
        self._login_retry_at = 0.0
        return True

    def _wait_for_verification(self, driver: uc.Chrome) -> bool:
        if self._is_verification_page(driver):
            timeout = max(1, self.config.upwork_verification_timeout)
            logger.warning(
                "Upwork verification is required. Complete it in this "
                "worker's browser (Docker: http://localhost:7900/vnc.html) within "
                "%d seconds.",
                timeout,
            )
            try:
                WebDriverWait(driver, timeout).until_not(
                    self._is_verification_page
                )
            except TimeoutException:
                logger.error(
                    "Upwork verification was not completed within %d seconds.",
                    timeout,
                )
                return False
        return True

    def _attempt_login(self, driver: uc.Chrome) -> bool:
        username = self.config.upwork_username
        password = self.config.upwork_password
        logger.info("Checking the saved Upwork session before logging in...")
        driver.get(SEARCH_URL)
        time.sleep(3)
        if not self._wait_for_verification(driver):
            return False

        if self._is_logged_in(driver):
            logger.info("Already logged in.")
            return True

        logger.info("Saved session is unauthenticated; opening Upwork login page...")
        driver.get(LOGIN_URL)
        time.sleep(3)
        if not self._wait_for_verification(driver):
            return False

        if not username or not password:
            logger.info("No Upwork credentials configured; complete login in the saved-profile browser.")
            try:
                WebDriverWait(driver, max(1, self.config.upwork_verification_timeout)).until(self._is_logged_in)
                return True
            except TimeoutException:
                return False

        username_entered = self._input_if_present(driver, [
            "input[name='login[username]']",
            "input#login_username",
            "input[type='email']",
        ], username, submit=True, stop_when_logged_in=True)
        if self._is_logged_in(driver):
            return True
        if not username_entered:
            logger.error("Upwork username field was not available; login failed.")
            return False
        time.sleep(2)
        if self._has_login_error(driver):
            logger.error("Upwork reported an error after username submission.")
            return False

        password_entered = self._input_if_present(driver, [
            "input[name='login[password]']",
            "input#login_password",
            "input[type='password']",
        ], password, submit=True, stop_when_logged_in=True)
        if self._is_logged_in(driver):
            return True
        if not password_entered:
            logger.error("Upwork password field was not available; login failed.")
            return False

        pause = max(self.config.upwork_login_timeout, self.config.upwork_verification_timeout)
        logger.info(
            "Waiting up to %ss for login/2FA. Complete any verification "
            "in this worker's browser.", pause,
        )
        end = time.monotonic() + pause
        while time.monotonic() < end:
            if self._has_login_error(driver):
                logger.error("Upwork rejected the login attempt; stopping this attempt.")
                return False
            if self._is_logged_in(driver):
                logger.info("Login successful.")
                return True
            time.sleep(2)

        logger.warning("Login not confirmed within timeout.")
        return False

    @staticmethod
    def _has_login_error(driver) -> bool:
        try:
            text = driver.find_element(By.TAG_NAME, "body").text.casefold()
            return any(message in text for message in (
                "due to technical difficulties", "incorrect password",
                "invalid password", "too many attempts",
            ))
        except WebDriverException:
            return False

    @staticmethod
    def _is_logged_in(driver) -> bool:
        """Require visible account controls; public search URLs are not proof."""
        if "/account-security/login" in driver.current_url:
            return False
        try:
            # A visible guest navigation link takes precedence over stale or
            # hidden account controls during page transitions.
            guest_selectors = (
                "header a[href*='/account-security/login']",
                "nav a[href*='/account-security/login']",
                "header a[href*='/signup']",
                "nav a[href*='/signup']",
            )
            for selector in guest_selectors:
                if any(el.is_displayed() for el in driver.find_elements(By.CSS_SELECTOR, selector)):
                    return False
            account_selectors = (
                "[data-test='user-menu']",
                "[data-test='user-menu-button']",
                "[data-qa='user-menu']",
                "[data-test='nav-user-dropdown']",
                "button[aria-label*='Account']",
                "button[aria-label*='account']",
                "a[href*='/ab/account-security/logout']",
                "nav a[href*='/ab/messages']",
                "aside a[href*='/ab/messages']",
                "nav a[href*='/nx/payments']",
                "aside a[href*='/nx/payments']",
            )
            if any(
                el.is_displayed()
                for selector in account_selectors
                for el in driver.find_elements(By.CSS_SELECTOR, selector)
            ):
                return True
            # The signed-in find-work dashboard uses a sidebar rather than
            # the older account menu. Require personal dashboard content too.
            if any(path in driver.current_url for path in ("/nx/find-work/", "/nx/s/find-work/")):
                body = driver.find_element(By.TAG_NAME, "body").text.casefold()
                return "profile visibility" in body and "connects:" in body
            return False
        except WebDriverException:
            return False

    @staticmethod
    def _is_verification_page(driver) -> bool:
        try:
            title = (driver.title or "").casefold()
            body = driver.find_element(By.TAG_NAME, "body").text.casefold()
        except Exception:
            return False
        return (
            "just a moment" in title
            or "cloudflare ray id" in body
            or "verify you are human" in body
            or "we couldn't complete verification" in body
        )

    @staticmethod
    def _input_if_present(driver, selectors, value, timeout=8, submit=True,
                          stop_when_logged_in=False):
        if not value:
            return False
        def field_or_session(browser):
            if stop_when_logged_in and UpworkSeleniumScraper._is_logged_in(browser):
                return True
            for selector in selectors:
                for element in browser.find_elements(By.CSS_SELECTOR, selector):
                    if element.is_displayed():
                        return element
            return False

        try:
            el = WebDriverWait(driver, timeout).until(field_or_session)
            if el is True:
                return False  # Caller confirms the newly authenticated session.
            # Autofill and controlled inputs can restore text after clear().
            # Replace through keyboard events and verify before submitting.
            for _ in range(2):
                if el.get_attribute("value") == value:
                    break
                el.click()
                el.send_keys(Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL, "a")
                el.send_keys(Keys.BACKSPACE)
                el.send_keys(value)
            if el.get_attribute("value") != value:
                logger.warning("Login field did not retain the intended value; submission stopped.")
                return False
            if submit:
                el.send_keys(Keys.ENTER)
            return True
        except TimeoutException:
            return False

    # ==================================================================
    # Scraping
    # ==================================================================

    def _scrape_keyword(self, keyword: str) -> list[JobLead]:
        driver = self._get_driver()
        if not self._ensure_logged_in(driver):
            return []
        locations = self._client_search_locations()
        per_location_limit = max(
            1,
            (
                self.config.max_results_per_keyword
                + len(locations)
                - 1
            )
            // len(locations),
        )
        leads: list[JobLead] = []
        full_locations: list[str] = []
        for client_location in locations:
            location_results = self._scrape_keyword_location(
                driver,
                keyword,
                client_location,
                per_location_limit,
            )
            leads.extend(location_results)
            if len(location_results) >= per_location_limit:
                full_locations.append(client_location)

        # If one country has fewer jobs, borrow its unused allowance from
        # the other country instead of returning a needlessly short batch.
        for client_location in full_locations:
            shortfall = self.config.max_results_per_keyword - len(leads)
            if shortfall <= 0:
                break
            leads.extend(
                self._scrape_keyword_location(
                    driver,
                    keyword,
                    client_location,
                    shortfall,
                )
            )
        return leads[:self.config.max_results_per_keyword]

    def _scrape_keyword_location(
        self,
        driver,
        keyword: str,
        client_location: str,
        result_limit: int,
    ) -> list[JobLead]:
        if driver is None:
            logger.error("Cannot scrape — login failed.")
            return []

        leads: list[JobLead] = []
        reference = datetime.now(timezone.utc)
        previous_page_signature: tuple[str, ...] | None = None
        for page in range(1, max(1, self.config.page_limit) + 1):
            params = {
                "sort": "recency",
                "q": keyword,
                "page": page,
                "per_page": 50,
            }
            if client_location:
                params["location"] = client_location
            feed_url = SEARCH_URL + "?" + urlencode(params)
            logger.info(
                "Searching Upwork %s page %d: %s",
                client_location or "all locations",
                page,
                feed_url,
            )
            cards = self._load_jobs(
                driver,
                feed_url,
                max_scrolls=self.config.selenium_max_scrolls,
            )
            logger.info(
                "Loaded %d job cards from page %d for '%s'",
                len(cards),
                page,
                keyword,
            )
            if not cards:
                break

            parsed_page: list[JobLead] = []
            for card in cards:
                try:
                    job = self._parse_card(card, keyword)
                    if job:
                        parsed_page.append(job)
                except WebDriverException:
                    continue

            page_signature = tuple(job.url or job.title for job in parsed_page)
            if page_signature and page_signature == previous_page_signature:
                logger.warning(
                    "Upwork page %d repeated the previous page for '%s'; "
                    "stopping pagination.",
                    page,
                    keyword,
                )
                break
            previous_page_signature = page_signature

            for job in parsed_page:
                if client_location:
                    job.location = client_location
                    job.country = client_location
                if job.url not in self._seen_ids:
                    self._seen_ids.add(job.url)
                    leads.append(job)
                    if len(leads) >= result_limit:
                        break
            if len(leads) >= result_limit:
                break
            if self._reached_lookback_boundary(parsed_page, reference):
                logger.info(
                    "Reached the %.0f-hour lookback boundary on Upwork "
                    "page %d for '%s'.",
                    self.config.collection_recency_hours,
                    page,
                    keyword,
                )
                break

        return leads[:result_limit]

    def _client_search_locations(self) -> list[str]:
        """Return canonical client countries supported by Upwork search."""
        if not self.config.target_locations:
            return [""]
        locations: list[str] = []
        for target in self.config.target_locations:
            normalized = target.casefold().strip()
            if normalized in {"north america", "america"}:
                for country in ("United States", "Canada"):
                    if country not in locations:
                        locations.append(country)
                continue
            if normalized in {
                "us",
                "u.s.",
                "usa",
                "u.s.a.",
                "united states",
                "united states of america",
            }:
                country = "United States"
            elif normalized == "canada":
                country = "Canada"
            else:
                continue
            if country not in locations:
                locations.append(country)
        return locations or [""]

    def _reached_lookback_boundary(
        self,
        leads: list[JobLead],
        reference: datetime,
    ) -> bool:
        hours = self.config.collection_recency_hours
        if hours is None:
            return False
        return RecencyFilter.page_reaches_boundary(
            leads,
            hours,
            reference,
        )

    def _load_jobs(self, driver, feed_url: str, max_scrolls: int = 8):
        driver.get(feed_url)
        try:
            selectors = ", ".join(JOB_CARD_SELECTORS)
            WebDriverWait(driver, 30).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, selectors))
            )
        except TimeoutException:
            current_url = getattr(driver, "current_url", "")
            title = getattr(driver, "title", "")
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                body_text = ""
            summary = " ".join(body_text.split())[:500]
            logger.warning(
                "Timeout waiting for job cards. URL=%s title=%r page=%r",
                current_url,
                title,
                summary,
            )
            return []

        seen = 0
        for _ in range(max_scrolls):
            cards = self._find_cards(driver)
            if len(cards) >= self.config.max_results_per_keyword:
                break
            if len(cards) == seen:
                break
            seen = len(cards)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)

        return self._find_cards(driver)

    def _find_cards(self, driver):
        cards = []
        seen = set()
        for selector in JOB_CARD_SELECTORS:
            for card in driver.find_elements(By.CSS_SELECTOR, selector):
                cid = card.id
                if cid not in seen:
                    seen.add(cid)
                    cards.append(card)
        return cards

    # ==================================================================
    # Card Parsing
    # ==================================================================

    def _parse_card(self, card, keyword: str) -> Optional[JobLead]:
        title, url = self._first_link(card)
        if not title or not url:
            return None

        raw_text = card.text
        desc = self._first_text(card, [
            "[data-test='UpCLineClamp JobDescription']",
            "[data-test='job-description-text']",
            "p",
        ])
        posted = self._extract_posted_str(card)
        skills = self._extract_skills(card)
        budget, job_type, exp_level = self._extract_job_meta(raw_text)
        client_location = self._first_text(card, [
            "[data-test='client-country']",
            "[data-test='client-location']",
            "[data-test='location']",
            "[data-qa='client-location']",
        ])

        return JobLead(
            title=title,
            url=url,
            description=desc[:1000],
            budget=budget,
            skills_required=", ".join(skills[:6]) if skills else "",
            posted_date=posted,
            job_type=job_type,
            experience_level=exp_level,
            platform="Upwork",
            keyword_searched=keyword,
            location=client_location,
            country=client_location,
        )

    def _first_link(self, card):
        for selector in JOB_LINK_SELECTORS:
            links = card.find_elements(By.CSS_SELECTOR, selector)
            for link in links:
                href = link.get_attribute("href")
                text = link.text.strip()
                if href and "/jobs/" in href:
                    clean_url = href.split("?")[0]
                    return text, clean_url
        return "", ""

    @staticmethod
    def _first_text(card, selectors):
        for selector in selectors:
            els = card.find_elements(By.CSS_SELECTOR, selector)
            for el in els:
                text = el.text.strip()
                if text:
                    return text
        return ""

    @staticmethod
    def _extract_skills(card):
        skills = []
        for selector in [
            "[data-test='TokenClamp JobAttrs'] button",
            "[data-test='skills-list'] a",
            "a[href*='ontology_skill_uid']",
        ]:
            for el in card.find_elements(By.CSS_SELECTOR, selector):
                text = el.text.strip()
                if text and text not in skills:
                    skills.append(text)
        return skills

    @staticmethod
    def _extract_posted_str(card):
        text = card.text
        patterns = [
            r"Posted\s+([^\n]+)",
            r"(\d+\s+(?:minute|hour|day|week)s?\s+ago)",
            r"(just now)",
            r"(yesterday)",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return re.split(r"[\u00b7\u2022]|\bProposals\s*:", m.group(1),
                                maxsplit=1, flags=re.IGNORECASE)[0].strip(" \t|\u00c2")
        return ""

    @staticmethod
    def _extract_job_meta(text: str):
        budget = ""
        job_type = ""
        exp_level = ""

        m = re.search(r"(Hourly|Fixed-price)[:\s]*\$?([\d,]+(?:\.\d{2})?(?:\s*-\s*\$?[\d,]+(?:\.\d{2})?)?)", text, re.IGNORECASE)
        if m:
            job_type = m.group(1)
            budget = m.group(0).strip()
        else:
            m2 = re.search(r"(Hourly|Fixed-price)", text, re.IGNORECASE)
            if m2:
                job_type = m2.group(1)

        for level in ["Expert", "Intermediate", "Entry"]:
            if level.lower() in text.lower():
                exp_level = level
                break

        return budget, job_type, exp_level
