"""Regression checks for public Upwork pages mistaken for login sessions."""
import unittest
from unittest.mock import MagicMock, patch, call
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.keys import Keys

from upwork_scraper.config import ScraperConfig
from upwork_scraper.selenium_scraper import UpworkSeleniumScraper, LOGIN_URL, SEARCH_URL


class UpworkLoginTests(unittest.TestCase):
    def test_new_s_dashboard_confirms_authenticated_content(self):
        driver = self.driver()
        driver.current_url = "https://www.upwork.com/nx/s/find-work/"
        driver.find_element.return_value.text = "Profile visibility Public Connects: 0"
        self.assertTrue(UpworkSeleniumScraper._is_logged_in(driver))
        driver.find_element.return_value.text = "Find work"
        self.assertFalse(UpworkSeleniumScraper._is_logged_in(driver))

    def test_manual_timeout_keeps_page_and_never_submits_credentials(self):
        scraper = UpworkSeleniumScraper(ScraperConfig(upwork_manual_login=True))
        driver = self.driver()
        with patch.object(scraper, "_is_logged_in", return_value=False), \
             patch.object(scraper, "_is_verification_page", return_value=False), \
             patch.object(scraper, "_input_if_present") as enter, \
             patch("upwork_scraper.selenium_scraper.time.sleep"), \
             patch("upwork_scraper.selenium_scraper.WebDriverWait") as wait:
            wait.return_value.until.side_effect = TimeoutException
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "still pending"):
                    scraper._ensure_logged_in(driver)
        self.assertEqual(driver.get.call_args_list, [call(SEARCH_URL), call(LOGIN_URL)])
        enter.assert_not_called()
        self.assertTrue(scraper._manual_login_pending)

    def test_manual_success_clears_pending_without_navigation(self):
        scraper = UpworkSeleniumScraper(ScraperConfig(upwork_manual_login=True))
        scraper._manual_login_pending = True
        driver = self.driver()
        with patch.object(scraper, "_is_logged_in", return_value=True):
            self.assertTrue(scraper._ensure_logged_in(driver))
        driver.get.assert_not_called()
        self.assertFalse(scraper._manual_login_pending)

    def test_manual_disconnection_does_not_restart_browser(self):
        from selenium.common.exceptions import WebDriverException
        scraper = UpworkSeleniumScraper(ScraperConfig(upwork_manual_login=True))
        scraper._manual_login_pending = True
        with patch.object(scraper, "_scrape_keyword", side_effect=WebDriverException("disconnected")), \
             patch.object(scraper, "_discard_driver") as discard:
            with self.assertRaises(WebDriverException):
                scraper.search_keyword("python")
        discard.assert_not_called()

    def test_autofilled_email_is_not_appended(self):
        field = MagicMock()
        field.get_attribute.return_value = "test@example.com"
        with patch("upwork_scraper.selenium_scraper.WebDriverWait") as wait:
            wait.return_value.until.return_value = field
            self.assertTrue(UpworkSeleniumScraper._input_if_present(
                self.driver(), ["input"], "test@example.com",
            ))
        field.send_keys.assert_called_once_with(Keys.ENTER)

    def test_duplicate_email_is_replaced_before_submission(self):
        field = MagicMock()
        field.get_attribute.side_effect = ["test@example.comtest@example.com", "test@example.com", "test@example.com"]
        with patch("upwork_scraper.selenium_scraper.WebDriverWait") as wait, \
             patch("upwork_scraper.selenium_scraper.sys.platform", "win32"):
            wait.return_value.until.return_value = field
            self.assertTrue(UpworkSeleniumScraper._input_if_present(
                self.driver(), ["input"], "test@example.com",
            ))
        self.assertEqual(field.send_keys.call_args_list, [
            call(Keys.CONTROL, "a"), call(Keys.BACKSPACE),
            call("test@example.com"), call(Keys.ENTER),
        ])

    def test_unexpected_field_value_is_not_submitted(self):
        field = MagicMock()
        field.get_attribute.return_value = "unexpected"
        with patch("upwork_scraper.selenium_scraper.WebDriverWait") as wait:
            wait.return_value.until.return_value = field
            self.assertFalse(UpworkSeleniumScraper._input_if_present(
                self.driver(), ["input"], "test@example.com",
            ))
        self.assertNotIn(call(Keys.ENTER), field.send_keys.call_args_list)

    def driver(self, visible=()):
        driver = MagicMock()
        driver.current_url = "https://www.upwork.com/nx/search/jobs/"
        driver.find_element.return_value.text = ""
        element = MagicMock()
        element.is_displayed.return_value = True
        driver.find_elements.side_effect = lambda by, selector: (
            [element] if selector in visible else []
        )
        return driver

    def test_public_search_is_not_authenticated(self):
        self.assertFalse(UpworkSeleniumScraper._is_logged_in(self.driver()))

    def test_visible_account_menu_confirms_session(self):
        driver = self.driver(("[data-test='user-menu']",))
        self.assertTrue(UpworkSeleniumScraper._is_logged_in(driver))

    def test_guest_navigation_overrides_account_menu(self):
        driver = self.driver((
            "[data-test='user-menu']",
            "header a[href*='/account-security/login']",
        ))
        self.assertFalse(UpworkSeleniumScraper._is_logged_in(driver))

    def test_logged_out_search_submits_credentials(self):
        scraper = UpworkSeleniumScraper(ScraperConfig(upwork_manual_login=False,
            upwork_username="test@example.com", upwork_password="test-password",
        ))
        driver = self.driver()
        with patch.object(scraper, "_is_verification_page", return_value=False), \
             patch.object(scraper, "_is_logged_in", side_effect=[False, False, True]), \
             patch.object(scraper, "_input_if_present", return_value=True) as enter, \
             patch("upwork_scraper.selenium_scraper.time.sleep"):
            self.assertTrue(scraper._ensure_logged_in(driver))
        self.assertEqual(driver.get.call_args_list, [call(SEARCH_URL), call(LOGIN_URL)])
        self.assertEqual(enter.call_count, 2)
        self.assertEqual(enter.call_args_list[0].args[2], "test@example.com")
        self.assertEqual(enter.call_args_list[1].args[2], "test-password")

    def test_scraping_stops_when_login_fails(self):
        scraper = UpworkSeleniumScraper(ScraperConfig(upwork_manual_login=False))
        with patch.object(scraper, "_get_driver", return_value=self.driver()), \
             patch.object(scraper, "_ensure_logged_in", return_value=False), \
             patch.object(scraper, "_scrape_keyword_location") as scrape:
            self.assertEqual(scraper._scrape_keyword("python"), [])
        scrape.assert_not_called()

    def test_new_dashboard_is_authenticated(self):
        driver = self.driver()
        driver.current_url = "https://www.upwork.com/nx/find-work/"
        driver.find_element.return_value.text = "Profile visibility Public Connects: 0"
        self.assertTrue(UpworkSeleniumScraper._is_logged_in(driver))

    def test_login_redirect_does_not_wait_for_username(self):
        scraper = UpworkSeleniumScraper(ScraperConfig(upwork_manual_login=False,
            upwork_username="test@example.com", upwork_password="test-password",
        ))
        with patch.object(scraper, "_is_verification_page", return_value=False), \
             patch.object(scraper, "_is_logged_in", return_value=True), \
             patch.object(scraper, "_input_if_present") as enter, \
             patch("upwork_scraper.selenium_scraper.time.sleep"):
            self.assertTrue(scraper._ensure_logged_in(self.driver()))
        enter.assert_not_called()

    def test_failed_login_enters_cooldown_without_repeated_attempts(self):
        scraper = UpworkSeleniumScraper(ScraperConfig(upwork_manual_login=False))
        with patch.object(scraper, "_attempt_login", return_value=False) as attempt, \
             patch.object(scraper, "_is_logged_in", return_value=False), \
             patch("upwork_scraper.selenium_scraper.WebDriverWait") as wait:
            wait.return_value.until.side_effect = TimeoutException
            with self.assertRaisesRegex(RuntimeError, "login failed"):
                scraper._ensure_logged_in(self.driver())
            with self.assertRaisesRegex(RuntimeError, "cooling down"):
                scraper._ensure_logged_in(self.driver())
        self.assertEqual(attempt.call_count, 1)

    def test_manual_login_resumes_during_cooldown(self):
        scraper = UpworkSeleniumScraper(ScraperConfig(upwork_manual_login=False))
        scraper._login_retry_at = float("inf")
        with patch.object(scraper, "_is_logged_in", return_value=True), \
             patch.object(scraper, "_attempt_login") as attempt:
            self.assertTrue(scraper._ensure_logged_in(self.driver()))
        attempt.assert_not_called()

    def test_manual_login_while_waiting_preserves_keyword(self):
        scraper = UpworkSeleniumScraper(ScraperConfig(upwork_manual_login=False))
        driver = self.driver()
        scraper._login_retry_at = 400.0
        with patch("upwork_scraper.selenium_scraper.time.monotonic", return_value=100.0), \
             patch.object(scraper, "_is_logged_in", return_value=False), \
             patch.object(scraper, "_attempt_login") as attempt, \
             patch("upwork_scraper.selenium_scraper.WebDriverWait") as wait:
            wait.return_value.until.return_value = True
            self.assertTrue(scraper._ensure_logged_in(driver))
        wait.assert_called_once_with(driver, 300.0)
        attempt.assert_not_called()
        self.assertTrue(scraper._authenticated)
        self.assertEqual(scraper._login_retry_at, 0.0)

    def test_technical_error_is_detected(self):
        driver = self.driver()
        driver.find_element.return_value.text = "Due to technical difficulties we are unable to process your request."
        self.assertTrue(UpworkSeleniumScraper._has_login_error(driver))

    def test_saved_session_does_not_require_credentials(self):
        scraper = UpworkSeleniumScraper(ScraperConfig(upwork_manual_login=False, upwork_username="", upwork_password=""))
        driver = self.driver()
        with patch.object(scraper, "_is_verification_page", return_value=False), \
             patch.object(scraper, "_is_logged_in", return_value=True), \
             patch.object(scraper, "_input_if_present") as enter, \
             patch("upwork_scraper.selenium_scraper.time.sleep"):
            self.assertTrue(scraper._attempt_login(driver))
        enter.assert_not_called()
        driver.get.assert_called_once_with(SEARCH_URL)

    def test_two_factor_login_uses_verification_window(self):
        scraper = UpworkSeleniumScraper(ScraperConfig(upwork_manual_login=False,
            upwork_username="test@example.com", upwork_password="test-password",
            upwork_login_timeout=45, upwork_verification_timeout=180,
        ))
        with patch.object(scraper, "_is_verification_page", return_value=False), \
             patch.object(scraper, "_is_logged_in", side_effect=[False, False, False, True]), \
             patch.object(scraper, "_has_login_error", return_value=False), \
             patch.object(scraper, "_input_if_present", return_value=True), \
             patch("upwork_scraper.selenium_scraper.time.sleep"), \
             patch("upwork_scraper.selenium_scraper.time.monotonic", side_effect=[0, 60]):
            self.assertTrue(scraper._attempt_login(self.driver()))
