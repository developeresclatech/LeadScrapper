"""
Automated test: verifies parsing logic against real Upwork HTML structures.
Run: python -m tests.test_parsing
"""

import json
from upwork_scraper.models import JobLead
from upwork_scraper.scraper import UpworkScraper
from upwork_scraper.config import ScraperConfig
from selectolax.lexbor import LexborHTMLParser as HTMLParser


# ── Realistic Upwork job page HTML (captured from live site) ──────────────

MOCK_JOB_PAGE = """
<!DOCTYPE html>
<html lang="en-US" theme="air-3-0">
<head><title>Python Automation Developer | Upwork</title></head>
<body>
<h1 class="up-font-bold mb-3">Python Automation Developer</h1>
<section class="up-card-section">
  <p>We need an experienced Python developer to build automation scripts for data processing.
  The ideal candidate should have strong experience with pandas, requests, and API integrations.</p>
</section>
<div class="up-card-section">
  <p>Additional requirements include experience with scheduling, error handling, and logging.</p>
</div>
<strong data-test="budget">$500 - $1,500</strong>
<span data-test="client-country">United States</span>
<span data-test="client-rating">4.9</span>
<span data-test="posted-on">Posted 2 hours ago</span>
<span data-test="proposals">12 Proposals</span>
<span class="air3-badge-tiny">Python</span>
<span class="air3-badge-tiny">Automation</span>
<span class="air3-badge-tiny">API</span>
<span class="air3-badge-tiny">Pandas</span>
</body>
</html>
"""

MOCK_SEARCH_CARD = """
<article class="air3-card-section">
  <a class="up-n-link" href="/freelance-jobs/apply/Python-Automation-Dev_~022064123456789012345">
    Build Python Scraping Bot
  </a>
  <span data-test="job-description-text">Looking for a developer to build a web scraping bot using BeautifulSoup and requests.</span>
  <strong class="ng-binding">$200 - $800</strong>
  <span data-test="client-country">United Kingdom</span>
  <span data-test="posted-on">Posted 1 day ago</span>
  <span data-test="proposals">8 Proposals</span>
  <span data-test="client-rating">4.7</span>
  <span class="air3-badge-tiny">Python</span>
  <span class="air3-badge-tiny">Web Scraping</span>
  <span class="air3-badge-tiny">BeautifulSoup</span>
</article>
"""


def test_job_detail_parsing():
    scraper = UpworkScraper(ScraperConfig())
    lead = scraper._parse_job_detail(MOCK_JOB_PAGE, "https://www.upwork.com/freelance-jobs/apply/test_123")

    assert lead is not None, "Should parse job lead"
    assert lead.title == "Python Automation Developer", f"Wrong title: {lead.title}"
    assert "$500" in lead.budget, f"Wrong budget: {lead.budget}"
    assert lead.client_country == "United States", f"Wrong country: {lead.client_country}"
    assert lead.client_rating == 4.9, f"Wrong rating: {lead.client_rating}"
    assert lead.posted_time == "Posted 2 hours ago", f"Wrong posted: {lead.posted_time}"
    assert "Python" in lead.skill_tags, f"Missing Python tag: {lead.skill_tags}"
    assert "Automation" in lead.skill_tags, f"Missing Automation tag: {lead.skill_tags}"
    assert len(lead.description) > 20, f"Description too short: {lead.description}"
    print("[PASS] Job detail parsing works")


def test_search_card_parsing():
    tree = HTMLParser(MOCK_SEARCH_CARD)
    card = tree.css_first("article.air3-card-section")
    lead = UpworkScraper._extract_lead_from_card(card, "python")

    assert lead is not None, "Should parse card"
    assert lead.title == "Build Python Scraping Bot", f"Wrong title: {lead.title}"
    assert "$200" in lead.budget, f"Wrong budget: {lead.budget}"
    assert lead.client_country == "United Kingdom", f"Wrong country: {lead.client_country}"
    assert lead.keyword_searched == "python", f"Wrong keyword: {lead.keyword_searched}"
    assert "Python" in lead.skill_tags, f"Missing Python tag"
    assert "Web Scraping" in lead.skill_tags, f"Missing Web Scraping tag"
    assert lead.client_rating == 4.7, f"Wrong rating: {lead.client_rating}"
    print("[PASS] Search card parsing works")


def test_ddg_url_extraction():
    scraper = UpworkScraper(ScraperConfig())

    mock_ddg_html = """
    <html><body>
    <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.upwork.com%2Ffreelance-jobs%2Fapply%2FPython-Dev_~022064123456789012345&rut=abc">
        Python Dev - Upwork
    </a>
    <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.upwork.com%2Ffreelance-jobs%2Fapply%2FData-Entry_~022064123456789099999&rut=def">
        Data Entry - Upwork
    </a>
    <a href="https://www.upwork.com/freelance-jobs/python">Not an apply page</a>
    <a href="https://google.com/search">Google link</a>
    </body></html>
    """

    urls = scraper._extract_upwork_urls_from_ddg(mock_ddg_html)
    assert len(urls) == 2, f"Expected 2 URLs, got {len(urls)}: {urls}"
    assert "freelance-jobs/apply/" in urls[0], f"Wrong URL format: {urls[0]}"
    assert "~022064123456789012345" in urls[0], f"URL decode failed: {urls[0]}"
    print("[PASS] DDG URL extraction works")


def test_model_serialization():
    lead = JobLead(
        title="Test Job",
        url="https://www.upwork.com/freelance-jobs/apply/test_123",
        description="A test job description",
        budget="$100",
        client_country="US",
        client_rating=5.0,
        skill_tags=["Python", "FastAPI"],
        keyword_searched="python",
    )
    data = lead.model_dump()
    assert data["title"] == "Test Job"
    assert data["skill_tags"] == ["Python", "FastAPI"]
    assert data["url"] == "https://www.upwork.com/freelance-jobs/apply/test_123"
    print("[PASS] Model serialization works")


def test_config_defaults():
    config = ScraperConfig()
    assert config.mode == "google"
    assert config.impersonate_browser == "chrome"
    assert config.output_format == "csv"
    assert len(config.keywords) == 5
    assert "python automation" in config.keywords
    print("[PASS] Config defaults correct")


if __name__ == "__main__":
    test_job_detail_parsing()
    test_search_card_parsing()
    test_ddg_url_extraction()
    test_model_serialization()
    test_config_defaults()
    print("\n✅ All tests passed!")
