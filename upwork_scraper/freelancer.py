"""Freelancer.com job scraper."""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .models import JobLead
from .pipeline.location_filter import LocationFilter
from .pipeline.recency_filter import RecencyFilter

logger = logging.getLogger(__name__)

FREELANCER_CATEGORIES: dict[str, str] = {
    "python": "/job-search/python/",
    "data-entry": "/job-search/data-entry/",
    "web-scraping": "/job-search/web-scraping/",
    "lead-generation": "/job-search/lead-generation/",
    "django": "/job-search/django/",
    "fastapi": "/job-search/fastapi/",
    "flask": "/job-search/python/",
    "javascript": "/job-search/javascript/",
    "react": "/job-search/react-js/",
    "react.js": "/job-search/react-js/",
    "reactjs": "/job-search/react-js/",
    "wordpress": "/job-search/wordpress/",
}

BASE_URL = "https://www.freelancer.com"


class FreelancerScraper:
    def __init__(
        self,
        target_locations: list[str] | None = None,
    ) -> None:
        # Keep concurrent Freelancer requests off the native curl backend,
        # which can terminate the entire process on Windows.
        self._session = requests.Session()
        self._target_locations = (
            ["United States", "Canada"]
            if target_locations is None
            else target_locations
        )
        self._location_filter = LocationFilter(self._target_locations)

    def close(self) -> None:
        self._session.close()

    def _pick_category(self, keyword: str) -> str:
        kw = keyword.lower().strip()
        aliases = {
            "search engine optimization": "seo",
            "cms development": "cms",
            "ai automation": "automation",
            "saas development": "saas",
            "it solution provider": "software-development",
            "ui design": "user-interface-ia",
            "web design": "website-design",
            "website": "website-design",
        }
        if kw in aliases:
            return f"/jobs/{aliases[kw]}/"
        if kw in FREELANCER_CATEGORIES:
            return FREELANCER_CATEGORIES[kw].replace("/job-search/", "/jobs/")
        slug = re.sub(r"[^a-z0-9]+", "-", kw).strip("-")
        return f"/jobs/{slug}/"

    def _parse_cards(self, html: str, source_url: str) -> list[JobLead]:
        tree = HTMLParser(html)
        leads: list[JobLead] = []

        for card in tree.css(".JobSearchCard-item"):
            title_el = card.css_first("a.JobSearchCard-primary-heading-link")
            if not title_el:
                continue

            title = title_el.text(strip=True)
            href = title_el.attributes.get("href", "")
            job_url = urljoin(BASE_URL, href) if href else ""

            budget_el = card.css_first(
                ".JobSearchCard-primary-budget, [class*=budget]"
            )
            raw_budget = budget_el.text(strip=True) if budget_el else ""
            budget = re.sub(r"Average bid$", "", raw_budget).strip()

            desc_el = card.css_first(".JobSearchCard-primary-description")
            description = desc_el.text(strip=True)[:500] if desc_el else ""

            skills_els = card.css(
                ".JobSearchCard-primary-tagsLink, .JobSearchCard-primary-skills a, [class*=skill] a"
            )
            skills = [s.text(strip=True) for s in skills_els if s.text(strip=True)]

            verified = bool(card.css_first("[class*=verified], [class*=Verified]"))

            lead = JobLead(
                title=title,
                company_client="Freelancer Client",
                platform="Freelancer",
                location="",
                job_type="",
                budget=budget,
                # Search cards expose the bid deadline ("6 days left"), not
                # the posting date. The real value is populated from the job
                # detail page before the lead is returned.
                posted_date="",
                url=job_url,
                description=description[:500],
                skills_required=", ".join(skills) if skills else "",
                experience_level="",
                country="",
                valid_job="Yes",
                job_id=href.split("/")[-1] if href else "",
            )
            leads.append(lead)

        return leads

    @classmethod
    def _parse_posted_date(
        cls,
        html: str,
        now: datetime | None = None,
    ) -> str:
        """Extract age from stable project data, not hydrated placeholder text."""
        start_match = re.search(
            r'"startTime"\s*:\s*(\d{10,13})',
            html,
        )
        if start_match:
            raw_timestamp = start_match.group(1)
            timestamp = int(raw_timestamp)
            if len(raw_timestamp) > 10:
                timestamp /= 1000
            try:
                posted_at = datetime.fromtimestamp(timestamp, timezone.utc)
                reference = now or datetime.now(timezone.utc)
                if reference.tzinfo is None:
                    reference = reference.replace(tzinfo=timezone.utc)
                age_seconds = (
                    reference.astimezone(timezone.utc) - posted_at
                ).total_seconds()
                if age_seconds >= 0:
                    return cls._format_relative_age(age_seconds)
            except (OSError, OverflowError, ValueError):
                pass

        # Fallback for older pages that do not embed project startTime.
        tree = HTMLParser(html)
        relative_time = tree.css_first("fl-relative-time")
        return relative_time.text(strip=True) if relative_time else ""

    @staticmethod
    def _parse_client_country(html: str) -> str:
        """Extract the project owner's country from embedded project data."""
        match = re.search(
            r'"client"\s*:\s*\{[^{}]*'
            r'"address"\s*:\s*\{[^{}]*'
            r'"country"\s*:\s*"([^"]+)"',
            html,
            re.DOTALL,
        )
        return match.group(1).strip() if match else ""

    @staticmethod
    def _format_relative_age(age_seconds: float) -> str:
        if age_seconds < 60:
            amount = max(1, round(age_seconds))
            unit = "second"
        elif age_seconds < 3600:
            amount = max(1, round(age_seconds / 60))
            unit = "minute"
        elif age_seconds < 172800:
            amount = max(1, round(age_seconds / 3600))
            unit = "hour"
        else:
            amount = max(1, round(age_seconds / 86400))
            unit = "day"
        suffix = "" if amount == 1 else "s"
        return f"{amount} {unit}{suffix} ago"

    @classmethod
    def _fetch_detail_metadata(cls, job_url: str) -> tuple[str, str]:
        if not job_url:
            return "", ""
        try:
            response = requests.get(
                job_url,
                timeout=30,
                headers={
                    "Accept": "text/html",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            if response.status_code != 200:
                logger.warning(
                    "Freelancer detail: status %d for %s",
                    response.status_code,
                    job_url,
                )
                return "", ""
            return (
                cls._parse_posted_date(response.text),
                cls._parse_client_country(response.text),
            )
        except Exception as exc:
            logger.warning(
                "Freelancer detail request failed for %s: %s",
                job_url,
                exc,
            )
            return "", ""

    @classmethod
    def _fetch_posted_date(cls, job_url: str) -> str:
        """Backward-compatible posting-date-only detail helper."""
        return cls._fetch_detail_metadata(job_url)[0]

    def _populate_posted_dates(self, leads: list[JobLead], on_lead=None) -> None:
        """Fetch detail dates concurrently without changing result order."""
        if not leads:
            return
        workers = min(5, len(leads))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self._fetch_detail_metadata,
                    lead.url,
                ): lead
                for lead in leads
            }
            for future in as_completed(futures):
                lead = futures[future]
                posted_date, client_country = future.result()
                lead.posted_date = posted_date
                lead.location = client_country
                lead.country = client_country
                if on_lead is not None and self._target_leads([lead]):
                    on_lead(lead)

    def _target_leads(self, leads: list[JobLead]) -> list[JobLead]:
        if not self._target_locations:
            return leads
        return [
            lead
            for lead in leads
            if self._location_filter.matches(lead)
        ]

    def scrape(
        self,
        keyword: str,
        max_results: int = 25,
        page_limit: int = 1,
        recency_hours: float | None = None,
        *,
        on_lead=None,
        prepare_leads=None,
    ) -> list[JobLead]:
        cat = self._pick_category(keyword)
        url = f"{BASE_URL}{cat}"
        leads: list[JobLead] = []
        seen: set[str] = set()
        for page in range(1, max(1, page_limit) + 1):
            page_url = url if page == 1 else f"{url}{page}/"
            logger.info("Freelancer: fetching %s (page %d)", page_url, page)
            try:
                resp = self._session.get(
                    page_url,
                    params={"sort": "newest"},
                    timeout=30,
                    headers={
                        "Accept": "text/html",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )
                if resp.status_code != 200:
                    logger.warning("Freelancer: status %d", resp.status_code)
                    break
            except Exception as exc:
                logger.error("Freelancer: request failed: %s", exc)
                break

            page_leads = self._parse_cards(resp.text, url)
            new_page_leads: list[JobLead] = []
            for lead in page_leads:
                key = lead.url or lead.title
                if key not in seen:
                    seen.add(key)
                    new_page_leads.append(lead)
                    if len(leads) + len(new_page_leads) >= max_results:
                        break
            if on_lead is None:
                self._populate_posted_dates(new_page_leads)
            else:
                self._populate_posted_dates(new_page_leads, on_lead=on_lead)
            target_page_leads = self._target_leads(new_page_leads)
            logger.info(
                "Freelancer page %d: %d found, %d target-country, %d missing posting dates",
                page, len(new_page_leads), len(target_page_leads),
                sum(not lead.posted_date for lead in new_page_leads),
            )
            leads.extend(target_page_leads)
            if (
                len(leads) >= max_results
                or not page_leads
                or (new_page_leads and all(
                    RecencyFilter.page_reaches_boundary([lead], recency_hours)
                    for lead in new_page_leads
                ))
            ):
                break

        logger.info("Freelancer: parsed %d jobs", len(leads))
        return leads[:max_results]
