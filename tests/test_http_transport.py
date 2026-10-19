import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import requests

from upwork_scraper.http_transport import create_scraper_session
from upwork_scraper.scraper import UpworkScraper


class HttpTransportTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows transport regression")
    def test_all_enabled_http_scrapers_avoid_native_curl_in_fresh_process(self):
        code = '''
import sys
import requests
from upwork_scraper.config import ScraperConfig
from upwork_scraper.platforms.registry import build_platform_adapters
from upwork_scraper.freelancer import FreelancerScraper
from upwork_scraper.guru import GuruScraper
from upwork_scraper.vollna import VollnaScraper
from upwork_scraper.scraper import UpworkScraper
scrapers = [FreelancerScraper(), GuruScraper(), VollnaScraper(), UpworkScraper()]
try:
    clients = [scrapers[0]._session, scrapers[1]._session, scrapers[2].session, scrapers[3]._curl]
    assert all(isinstance(client, requests.Session) for client in clients)
    assert not any(name == 'curl_cffi' or name.startswith('curl_cffi.') for name in sys.modules)
finally:
    for scraper in scrapers:
        scraper.close()
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    @patch("upwork_scraper.http_transport.sys.platform", "win32")
    def test_windows_uses_requests(self):
        session = create_scraper_session()
        self.addCleanup(session.close)
        self.assertIsInstance(session, requests.Session)

    def test_blocked_direct_source_does_not_repeat_requests(self):
        scraper = UpworkScraper()
        self.addCleanup(scraper.close)
        with patch.object(scraper._curl, "get", return_value=Mock(status_code=403)) as get:
            self.assertIsNone(scraper._fetch_direct("https://www.upwork.com/nx/search/jobs/"))
            self.assertIsNone(scraper._fetch_direct("https://www.upwork.com/nx/search/jobs/"))
        get.assert_called_once()
