"""One bounded live login/search/detail check; writes results locally."""
import json
import logging
from pathlib import Path
from upwork_scraper.config import ScraperConfig
from upwork_scraper.selenium_scraper import UpworkSeleniumScraper


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = ScraperConfig(max_results_per_keyword=3, page_limit=1,
                           upwork_verification_timeout=45)
    scraper = UpworkSeleniumScraper(config)
    report = {"keyword": "AI Automation", "navigation": [], "leads": []}
    path = Path("output/live_selenium_check.json")
    path.parent.mkdir(exist_ok=True)
    try:
        driver = scraper._get_driver()
        navigate = driver.get
        def trace(url):
            report["navigation"].append(url)
            logging.info("LIVE navigation: %s", url)
            navigate(url)
        driver.get = trace
        leads = scraper.search_keyword(report["keyword"])
        report["leads"] = [lead.model_dump() for lead in leads]
        report["final_url"] = driver.current_url
        report["authenticated"] = scraper._is_logged_in(driver)
        logging.info("LIVE result: %d leads; authenticated=%s", len(leads), report["authenticated"])
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
        logging.error("LIVE check failed: %s", type(exc).__name__)
    finally:
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        scraper.close()
        logging.info("Report saved to %s", path)


if __name__ == "__main__":
    main()
