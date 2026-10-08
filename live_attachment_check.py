"""Inspect the supplied known-attachment job without downloading files."""
import json
import logging
from pathlib import Path
from upwork_scraper.config import ScraperConfig
from upwork_scraper.models import JobLead
from upwork_scraper.selenium_scraper import UpworkSeleniumScraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
scraper = UpworkSeleniumScraper(ScraperConfig(upwork_verification_timeout=45))
lead = JobLead(title="Web Designer Developer for Luxury Yacht Site", url="https://www.upwork.com/freelance-jobs/apply/Web-Designer-Developer-for-Luxury-Yacht-Site_~022107889779719775352")
report = {}
try:
    driver = scraper._get_driver()
    report["login"] = scraper._ensure_logged_in(driver)
    if report["login"]:
        scraper.enrich_attachments([lead])
        report["final_url"] = driver.current_url
        report["markup"] = driver.execute_script("""
            return Array.from(document.querySelectorAll('*')).filter(e =>
              /attach|download|description/i.test(e.getAttribute('data-test') || '') ||
              /attach|download|description/i.test(e.getAttribute('data-qa') || '') ||
              (e.tagName === 'A' && /attach|download|\\.pdf|\\.docx/i.test(e.href)) ||
              (e.tagName !== 'SCRIPT' && e.children.length === 0 && /attachment/i.test(e.textContent))
            ).map(e => e.outerHTML.slice(0, 4000)).slice(0, 60);
        """)
    report["lead"] = lead.model_dump()
finally:
    Path("output/live_attachment_check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    scraper.close()
    print("Attachment status:", lead.attachment_status, "count:", lead.attachment_count)
