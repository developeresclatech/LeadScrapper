"""Choose a scraper HTTP client without native curl on Windows."""

import sys

import requests


def create_scraper_session(impersonate: str = "chrome"):
    if sys.platform == "win32":
        return requests.Session()
    # Keep fingerprint support on other hosts, without even loading the
    # native extension into the Windows scraper process.
    from curl_cffi import requests as curl_requests

    return curl_requests.Session(impersonate=impersonate)
