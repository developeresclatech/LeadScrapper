"""Regression checks for public Upwork pages mistaken for login sessions."""
import unittest
from unittest.mock import MagicMock, patch

from upwork_scraper.config import ScraperConfig
from upwork_scraper.selenium_scraper import UpworkSeleniumScraper


class UpworkLoginTests(unittest.TestCase):
    def driver(self, visible=()):
        driver = MagicMock()
        driver.current_url = "https://www.upwork.com/nx/search/jobs/"
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
        scraper = UpworkSeleniumScraper(ScraperConfig(
            upwork_username="test@example.com", upwork_password="test-password",
        ))
        with patch.object(scraper, "_is_verification_page", return_value=False), \
             patch.object(scraper, "_is_logged_in", side_effect=[False, False, False, True]), \
             patch.object(scraper, "_input_if_present", return_value=True) as enter, \
             patch("upwork_scraper.selenium_scraper.time.sleep"):
            self.assertTrue(scraper._ensure_logged_in(self.driver()))
        self.assertEqual(enter.call_count, 2)
        self.assertEqual(enter.call_args_list[0].args[2], "test@example.com")
        self.assertEqual(enter.call_args_list[1].args[2], "test-password")

    def test_scraping_stops_when_login_fails(self):
        scraper = UpworkSeleniumScraper()
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
        scraper = UpworkSeleniumScraper(ScraperConfig(
            upwork_username="test@example.com", upwork_password="test-password",
        ))
        with patch.object(scraper, "_is_verification_page", return_value=False), \
             patch.object(scraper, "_is_logged_in", side_effect=[False, True]), \
             patch.object(scraper, "_input_if_present") as enter, \
             patch("upwork_scraper.selenium_scraper.time.sleep"):
            self.assertTrue(scraper._ensure_logged_in(self.driver()))
        enter.assert_not_called()
