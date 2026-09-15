"""
Scrape Upwork job data from Vollna.com RSS feed — includes full descriptions,
skills, budgets, and direct Upwork URLs for LeadAnalyzer to extract
company names, emails, decision-makers, and other business intelligence.
"""

import re
import time
import random
import logging
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, parse_qs, unquote
from typing import Optional

from curl_cffi import requests as cffi_requests

from .config import ScraperConfig
from .models import JobLead
from .pipeline.location_filter import LocationFilter

logger = logging.getLogger(__name__)

VOLLNA_RSS_URL = "https://www.vollna.com/rss/XswrDNEvze23uFN217i2"


class VollnaScraper:
    """Scrape Upwork job data from Vollna.com RSS feed."""

    def __init__(self, config: ScraperConfig | None = None):
        self.config = config or ScraperConfig()
        self.session = cffi_requests.Session(
            impersonate=self.config.impersonate_browser
        )
        self._seen_titles: set[str] = set()
        self._location_resolver = None
        self._location_filter = LocationFilter(
            self.config.target_locations
        )

    def search_keyword(self, keyword: str) -> list[JobLead]:
        """Fetch jobs from Vollna RSS feed filtered by keyword."""
        logger.info("Vollna: fetching %s", VOLLNA_RSS_URL)
        xml_data = self._fetch(VOLLNA_RSS_URL)
        if not xml_data:
            return []

        leads = self._parse_jobs(
            xml_data, keyword
        )[:self.config.max_results_per_keyword]
        self._enrich_locations(leads)
        return self._target_leads(leads)

    def search_keywords(
        self,
        keywords: list[str],
    ) -> dict[str, list[JobLead]]:
        """Fetch RSS once, then filter the same payload for every keyword."""
        logger.info(
            "Vollna: fetching %s once for %d keywords",
            VOLLNA_RSS_URL,
            len(keywords),
        )
        xml_data = self._fetch(VOLLNA_RSS_URL)
        if not xml_data:
            return {keyword: [] for keyword in keywords}
        results = {
            keyword: self._parse_jobs(xml_data, keyword)[
                :self.config.max_results_per_keyword
            ]
            for keyword in keywords
        }
        unique_leads = {
            lead.url: lead
            for leads in results.values()
            for lead in leads
            if lead.url
        }
        self._enrich_locations(list(unique_leads.values()))
        return {
            keyword: self._target_leads(leads)
            for keyword, leads in results.items()
        }

    def _target_leads(self, leads: list[JobLead]) -> list[JobLead]:
        if not self.config.target_locations:
            return leads
        return [
            lead
            for lead in leads
            if self._location_filter.matches(lead)
        ]

    def _enrich_locations(self, leads: list[JobLead]) -> None:
        unresolved = list({
            lead.url: lead
            for lead in leads
            if not (lead.country or lead.location)
            and "upwork.com" in lead.url.casefold()
        }.values())
        if not unresolved:
            return
        if self._location_resolver is None:
            from .selenium_scraper import UpworkSeleniumScraper

            self._location_resolver = UpworkSeleniumScraper(self.config)
        self._location_resolver.enrich_client_locations(unresolved)

    def _fetch(self, url: str) -> Optional[str]:
        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = self.session.get(
                    url,
                    timeout=60,
                    headers=self.config.headers,
                )
                if resp.status_code == 200:
                    return resp.text
                logger.warning("HTTP %d for %s", resp.status_code, url)
            except Exception as exc:
                logger.error("Fetch error: %s", exc)
            time.sleep(random.uniform(1, 3))
        return None

    def _parse_jobs(self, xml_data: str, keyword: str) -> list[JobLead]:
        """Parse RSS XML and extract job items matching the keyword."""
        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError as exc:
            logger.error("RSS XML parse error: %s", exc)
            return []

        channel = root.find("channel")
        if channel is None:
            return []

        items = channel.findall("item")
        keyword_lower = keyword.lower()

        leads = []
        for item in items:
            title = item.findtext("title", "")
            description = item.findtext("description", "")

            if keyword_lower not in title.lower() and keyword_lower not in description.lower():
                continue

            lead = self._extract_job(item, keyword)
            if lead and lead.title not in self._seen_titles:
                self._seen_titles.add(lead.title)
                leads.append(lead)

        return leads

    def _extract_job(self, item, keyword: str) -> Optional[JobLead]:
        title = item.findtext("title", "").strip()
        if not title or len(title) < 10:
            return None

        description_cdata = item.findtext("description", "").strip()
        link = item.findtext("link", "").strip()
        pub_date = item.findtext("pubDate", "").strip()

        # Extract skills from "Skills: ..." line in description
        skills = ""
        skills_match = re.search(r"Skills:\s*(.+?)(?:\n|$)", description_cdata)
        if skills_match:
            skills = skills_match.group(1).strip()

        # Clean description — remove "Skills:" and "Categories:" trailing sections
        desc_clean = re.sub(
            r"\s*Skills:.*", "", description_cdata, flags=re.DOTALL
        ).strip()
        desc_clean = re.sub(
            r"\s*Categories:.*", "", desc_clean, flags=re.DOTALL
        ).strip()
        location = ""
        location_match = re.search(
            r"(?:Client Country|Country|Location)\s*:\s*"
            r"([^\n\r<]+)",
            description_cdata,
            re.IGNORECASE,
        )
        if location_match:
            location = location_match.group(1).strip()

        # Extract budget from title e.g. "(Fixed Price: 2,000 USD)"
        budget = ""
        budget_match = re.search(
            r"\((?:Fixed Price|Hourly|Budget)\s*:\s*"
            r"([\d,\s$-]+(?:\s*-\s*[\d,\s$-]+)?)\s*(?:USD)?\s*\)",
            title,
            re.IGNORECASE,
        )
        if budget_match:
            budget = budget_match.group(1).strip()

        # Clean title — remove the budget suffix
        title_clean = re.sub(r"\s*\(.*?\)\s*$", "", title).strip()

        # Extract real Upwork URL from Vollna redirect link
        job_url = link
        try:
            parsed = urlparse(link)
            params = parse_qs(parsed.query)
            if "url" in params:
                upwork_url = unquote(unquote(params["url"][0]))
                if "upwork.com" in upwork_url:
                    job_url = upwork_url
        except Exception:
            pass

        return JobLead(
            title=title_clean,
            company_client="Upwork Client",
            platform="Upwork (Vollna)",
            location=location,
            job_type="",
            budget=budget,
            posted_date=pub_date,
            url=job_url,
            description=desc_clean[:5000] if desc_clean else "",
            skills_required=skills,
            experience_level="",
            country=location,
            valid_job="Yes",
            job_id=title_clean,
            keyword_searched=keyword,
        )

    def close(self):
        if self._location_resolver is not None:
            self._location_resolver.close()
            self._location_resolver = None
        self.session.close()
