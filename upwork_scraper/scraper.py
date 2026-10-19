"""
Upwork scraper using Wayback Machine cached pages.
Bypasses Cloudflare by fetching from Wayback snapshots.
Uses standard `requests` (not curl_cffi) for Wayback since Archive.org
blocks curl_cffi impersonation. CDX checks run concurrently for speed.
"""

import time
import logging
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, quote_plus, urlparse
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests as std_requests
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .config import ScraperConfig
from .http_transport import create_scraper_session
from .models import JobLead
from .pipeline.recency_filter import RecencyFilter
from .selenium_scraper import UpworkSeleniumScraper

logger = logging.getLogger(__name__)


class UpworkScraper:
    """Scrape Upwork job data via Wayback Machine archives."""

    def __init__(
        self,
        config: ScraperConfig | None = None,
        selenium_fallback: bool = True,
    ):
        self.config = config or ScraperConfig()
        self.selenium_fallback = selenium_fallback
        self._curl = create_scraper_session(impersonate=self.config.impersonate_browser)
        self._plain = std_requests.Session()
        self._plain.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._seen_ids: set[str] = set()
        self._cdx_cache: dict[str, Optional[str]] = {}

    # ==================================================================
    # Public API
    # ==================================================================

    def search_keyword(self, keyword: str) -> list[JobLead]:
        # Archived snapshots cannot represent the current 14-hour window.
        if self.config.collection_recency_hours is not None:
            leads = self._search_direct(keyword)
            if leads:
                return self._with_client_locations(leads)
            if not self.selenium_fallback:
                return []
            return self._with_client_locations(
                self._search_via_selenium(keyword)
            )

        leads = self._search_via_wayback(keyword)
        if leads:
            return self._with_client_locations(leads)
        leads = self._search_direct(keyword)
        if leads:
            return self._with_client_locations(leads)
        if not self.selenium_fallback:
            return []
        return self._with_client_locations(
            self._search_via_selenium(keyword)
        )

    def _with_client_locations(
        self,
        leads: list[JobLead],
    ) -> list[JobLead]:
        selected = leads[:self.config.max_results_per_keyword]
        if self.config.recent_minutes is not None:
            recency = RecencyFilter(
                self.config.recent_minutes / 60, keep_unknown=False,
                strict_date_only=True, anchor_to_scraped_at=True,
            )
            selected = [lead for lead in selected if recency.matches(lead)]
        if not selected:
            return selected
        shared = getattr(self, "_shared_location_resolver", None)
        if shared is not None:
            shared.enrich_client_locations(selected)
            return selected
        if (
            not hasattr(self, "_selenium_scraper")
            or self._selenium_scraper is None
        ):
            self._selenium_scraper = UpworkSeleniumScraper(self.config)
        self._selenium_scraper.enrich_client_locations(selected)
        return selected

    def _search_via_selenium(self, keyword: str) -> list[JobLead]:
        try:
            if not hasattr(self, '_selenium_scraper') or self._selenium_scraper is None:
                self._selenium_scraper = UpworkSeleniumScraper(self.config)
            leads = self._selenium_scraper.search_keyword(keyword)
            if leads:
                logger.info("Selenium fallback found %d leads for '%s'.",
                            len(leads), keyword)
            return leads
        except Exception as exc:
            logger.debug("Selenium fallback failed for '%s': %s", keyword, exc)
            return []

    def close(self):
        self._curl.close()
        self._plain.close()
        if hasattr(self, '_selenium_scraper') and self._selenium_scraper:
            self._selenium_scraper.close()

    # ==================================================================
    # Wayback Machine mode (primary — bypasses Cloudflare)
    # ==================================================================

    def _search_via_wayback(self, keyword: str) -> list[JobLead]:
        slug = keyword.lower().replace(" ", "-")
        category_slugs = self._expand_keyword_to_slugs(slug)

        urls = []
        # Modern search pages (2024 snapshots available) first.
        for page in range(1, max(1, self.config.page_limit) + 1):
            urls.append(
                "https://www.upwork.com/nx/search/jobs/"
                f"?q={quote_plus(keyword)}&sort=recency&page={page}"
            )
        # Old category pages
        for cat_slug in category_slugs:
            urls.append(f"https://www.upwork.com/freelance-jobs/{cat_slug}/")
        urls = list(dict.fromkeys(urls))

        snapshots = self._find_all_snapshots(urls)
        if not snapshots:
            return []

        def snapshot_order(item: tuple[str, str, str]) -> tuple[int, int]:
            source_url, url_type, _ = item
            page = int(parse_qs(urlparse(source_url).query).get("page", ["1"])[0])
            return (0 if url_type == "search" else 1, page)

        leads: list[JobLead] = []
        for _, url_type, snap_url in sorted(
            snapshots, key=snapshot_order
        )[:self.config.page_limit]:
            page_html = self._fetch_wayback(snap_url)
            if not page_html:
                continue
            leads.extend(self._parse_html_jobs(page_html, keyword, url_type))
            if len(leads) >= self.config.max_results_per_keyword:
                break
        return leads[:self.config.max_results_per_keyword]

    def _find_all_snapshots(self, urls: list[str]) -> list[tuple[str, str, str]]:
        """Check CDX for all candidate URLs concurrently. Returns
        sorted list of (upwork_url, url_type, snapshot_url)."""
        def cdx_lookup(upwork_url: str):
            url_type = "category" if "/freelance-jobs/" in upwork_url else "search"
            snap = self._find_latest_snapshot(upwork_url)
            if snap:
                return (upwork_url, url_type, snap)
            return None

        results = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(cdx_lookup, u): u for u in urls}
            for fut in as_completed(futs, timeout=40):
                try:
                    r = fut.result()
                    if r:
                        results.append(r)
                except Exception:
                    pass
        return results

    def _find_latest_snapshot(self, url: str) -> Optional[str]:
        if url in self._cdx_cache:
            return self._cdx_cache[url]
        cdx = (
            f"https://web.archive.org/cdx/search/cdx"
            f"?url={quote(url, safe='')}&output=json&limit=1"
            f"&fl=timestamp,statuscode&filter=statuscode:200"
        )
        result = None
        try:
            resp = self._plain.get(cdx, timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                if len(data) >= 2:
                    ts = data[1][0]
                    result = f"https://web.archive.org/web/{ts}/{url}"
        except Exception:
            pass
        self._cdx_cache[url] = result
        return result

    def _expand_keyword_to_slugs(self, slug: str) -> list[str]:
        mapping = {
            "python": ["python", "python-script"],
            "web-scraping": ["web-scraping", "data-scraping"],
            "data-entry": ["data-entry"],
            "lead-generation": ["lead-generation"],
            "virtual-assistant": ["virtual-assistant"],
            "automation": ["automation", "scripting-automation"],
            "django": ["django"],
            "fastapi": ["fastapi"],
            "flask": ["flask"],
            "machine-learning": ["machine-learning"],
            "ai": ["artificial-intelligence"],
            "data-science": ["data-science"],
            "n8n": ["n8n"],
            "zapier": ["zapier"],
            "chatbot": ["chatbot-development"],
            "langchain": ["langchain"],
            "api": ["api-development"],
            "magento": ["magento-development", "magento"],
            "magento-development": ["magento-development", "magento"],
            "shopify": ["shopify-development", "shopify"],
            "wordpress": ["wordpress-development", "wordpress"],
            "react": ["react-js", "react-development"],
            "mobile-app": ["mobile-app-development", "mobile-development"],
            "ecommerce": ["ecommerce-development", "ecommerce"],
            "blockchain": ["blockchain-development", "blockchain"],
            "devops": ["devops-development", "devops"],
            "seo": ["seo-development", "seo"],
        }
        if slug in mapping:
            return mapping[slug]
        return [slug]

    def _fetch_wayback(self, snapshot_url: str) -> Optional[str]:
        try:
            resp = self._plain.get(snapshot_url, timeout=10)
            if resp.status_code == 200 and len(resp.text) > 5000:
                return resp.text
        except Exception:
            pass
        return None

    # ==================================================================
    # HTML Job Parser
    # ==================================================================

    def _parse_html_jobs(self, html: str, keyword: str, url_type: str) -> list[JobLead]:
        tree = HTMLParser(html)
        leads = self._parse_category_page(tree, keyword) if url_type == "category" else self._parse_search_page(tree, keyword)
        result = []
        for l in leads:
            if l.url not in self._seen_ids:
                self._seen_ids.add(l.url)
                result.append(l)
        return result

    def _parse_category_page(self, tree: HTMLParser, keyword: str) -> list[JobLead]:
        leads = []
        tiles = tree.css("section.up-card.up-card-section.job-tile")
        for tile in tiles:
            try:
                lead = self._extract_old_tile(tile, keyword)
                if lead:
                    leads.append(lead)
            except Exception:
                continue
        return leads

    @staticmethod
    def _extract_old_tile(tile, keyword: str) -> Optional[JobLead]:
        title_el = tile.css_first("a[data-qa='job-title']")
        if not title_el:
            return None
        title = title_el.text(strip=True)
        href = title_el.attributes.get("href", "")
        if href and not href.startswith("http"):
            href = f"https://www.upwork.com{href}"

        desc_el = tile.css_first("p[data-qa='job-description']")
        desc = desc_el.text(strip=True) if desc_el is not None else ""

        posted = ""
        budget = ""
        posting_data = tile.css_first("p[data-qa='job-posting-data']")
        if posting_data:
            smalls = posting_data.css("small.text-muted")
            if len(smalls) >= 2:
                budget = smalls[0].text(strip=True)
                posted = smalls[1].text(strip=True).lstrip("‐ ").replace("Posted ", "")
            elif len(smalls) == 1:
                budget = smalls[0].text(strip=True)

        skills = [
            a.text(strip=True)
            for a in tile.css("a[data-qa='legacy-skill']")[:6]
        ]
        location_el = tile.css_first(
            "[data-qa='client-location'], "
            "[data-qa='client-country'], "
            "[data-test='location']"
        )
        client_location = (
            location_el.text(strip=True) if location_el else ""
        )

        return JobLead(
            title=title,
            url=href,
            description=desc[:1000],
            budget=budget,
            posted_date=posted,
            platform="Upwork",
            keyword_searched=keyword,
            skills_required=", ".join(skills) if skills else "",
            location=client_location,
            country=client_location,
        )

    def _parse_search_page(self, tree: HTMLParser, keyword: str) -> list[JobLead]:
        leads = []
        cards = tree.css("article.job-tile")
        for card in cards:
            try:
                lead = self._extract_search_card(card, keyword)
                if lead:
                    leads.append(lead)
            except Exception:
                continue
        return leads

    @staticmethod
    def _extract_search_card(card, keyword: str) -> Optional[JobLead]:
        title_el = card.css_first("h2.job-tile-title a.up-n-link")
        if not title_el:
            return None
        title = title_el.text(strip=True)
        href = title_el.attributes.get("href", "")
        if href and not href.startswith("http"):
            href = f"https://www.upwork.com{href}"

        desc_el = card.css_first("p.mb-0")
        desc = desc_el.text(strip=True)[:1000] if desc_el is not None else ""

        budget_el = card.css_first("li[data-test='job-type-label'] strong")
        budget = budget_el.text(strip=True) if budget_el is not None else ""

        posted_el = card.css_first("small.text-light")
        posted = posted_el.text(strip=True) if posted_el is not None else ""

        exp_el = card.css_first("li[data-test='experience-level'] strong")
        exp = exp_el.text(strip=True) if exp_el is not None else ""
        location_el = card.css_first(
            "[data-test='client-country'], "
            "[data-test='client-location'], "
            "[data-test='location']"
        )
        client_location = (
            location_el.text(strip=True) if location_el else ""
        )

        return JobLead(
            title=title,
            url=href,
            description=desc,
            budget=budget,
            posted_date=posted,
            platform="Upwork",
            keyword_searched=keyword,
            experience_level=exp,
            location=client_location,
            country=client_location,
        )

    # ==================================================================
    # Direct mode (fast-fail if Cloudflare blocks)
    # ==================================================================

    def _search_direct(self, keyword: str) -> list[JobLead]:
        leads: list[JobLead] = []
        reference = datetime.now(timezone.utc)
        search_url = "https://www.upwork.com/nx/search/jobs/"
        locations = UpworkSeleniumScraper(
            self.config
        )._client_search_locations()
        per_location_limit = max(
            1,
            (
                self.config.max_results_per_keyword
                + len(locations)
                - 1
            )
            // len(locations),
        )
        for client_location in locations:
            location_leads: list[JobLead] = []
            for page in range(1, max(1, self.config.page_limit) + 1):
                params = {
                    "q": keyword,
                    "sort": "recency",
                    "page": str(page),
                    "per_page": "50",
                }
                if client_location:
                    params["location"] = client_location
                html = self._fetch_direct(search_url, params=params)
                if not html or len(html) <= 1000:
                    break
                cards = self._parse_search_page(
                    HTMLParser(html),
                    keyword,
                )
                for card in cards:
                    if client_location:
                        card.location = client_location
                        card.country = client_location
                location_leads.extend(cards)
                if (
                    len(location_leads) >= per_location_limit
                    or not cards
                    or RecencyFilter.page_reaches_boundary(
                        cards,
                        self.config.collection_recency_hours,
                        reference,
                    )
                ):
                    break
            leads.extend(location_leads[:per_location_limit])
        return leads[:self.config.max_results_per_keyword]

    def _fetch_direct(self, url: str, params: dict | None = None) -> Optional[str]:
        if getattr(self, "_direct_blocked", False):
            return None
        try:
            resp = self._curl.get(
                url, params=params,
                headers=self.config.headers,
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.text
            if resp.status_code in (403, 429):
                self._direct_blocked = True
                logger.warning(
                    "Direct Upwork HTTP returned %d; skipping further direct requests "
                    "this run. Authenticated Selenium collection remains separate.",
                    resp.status_code,
                )
            else:
                logger.warning("Direct Upwork HTTP returned %d.", resp.status_code)
        except Exception as exc:
            logger.warning("Direct Upwork HTTP request failed: %s", exc)
        return None
