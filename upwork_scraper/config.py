import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


ALL_PLATFORMS = [
    "upwork",
    "vollna",
    "freelancer",
    "guru",
    "upwork_selenium",
    # "bark",  # Temporarily disabled; scraper implementation is preserved.
]


@dataclass
class ScraperConfig:
    """Central configuration for the multi-platform lead scraper."""

    keywords: list[str] = field(default_factory=lambda: [
        "web development",
        "mobile app development",
        "marketing",
        "Search Engine Optimization",
        "Social Media Marketing",
        "CMS development",
        "AI Automation", 
        "SaaS Development",
        "IT Solution provider",
        "ui design",
        "Branding",
        "web design",
        "website",
        # High-Intent Buyer Keywords (Most Valuable)
        # "hire react native developer",
        # "hire react developer",
        # "hire next.js developer",
        # "hire node.js developer",
        # "hire full stack developer",
        # "hire shopify expert",
        # "hire shopify developer",
        # "hire wordpress developer",
        # "hire webflow developer",
        # "hire mobile app developer",
        # "hire ui ux designer",
        # "hire logo designer",
        # "custom software development",
        # "saas development company",
        # "web development agency",
        # "mobile app development company",
        # "ecommerce development company",
        # "mvp development",
        # "dedicated development team",
        # "offshore development team",
        # "remote developers",
        # "software development agency",
        # "digital product development",
        # "startup development company",
        # "ai development company",
        # "crm development company",
        # "erp development company",
        # "custom web application",
        # "custom mobile application",
        # "enterprise software development",
        # # Web Development
        # "web development",
        # "website development",
        # "custom website development",
        # "web application development",
        # "full stack development",
        # "frontend development",
        # "backend development",
        # "mern stack development",
        # "mean stack development",
        # "jamstack development",
        # "saas development",
        # "enterprise web development",
        # "landing page development",
        # "business website development",
        # "corporate website development",
        # "startup website development",
        # "progressive web app",
        # "portal development",
        # "dashboard development",
        # "crm development",
        # "erp development",
        # "booking system development",
        # "marketplace development",
        # "membership website",
        # "directory website",
        # "job portal",
        # "learning management system",
        # "custom cms development",
        # "api development",
        # "api integration",
        # "third-party integration",
        # # Frontend Technologies
        # "react.js",
        # "next.js",
        # "vue.js",
        # "nuxt.js",
        # "angular",
        # "typescript",
        # "javascript",
        # "tailwind css",
        # "bootstrap",
        # "material ui",
        # "shadcn ui",
        # # Backend Technologies
        # "node.js",
        # "express.js",
        # "nestjs",
        # "laravel",
        # "php",
        # "python django",
        # "flask",
        # "fastapi",
        # ".net",
        # "java spring boot",
        # # Database
        # "mongodb",
        # "mysql",
        # "postgresql",
        # "firebase",
        # "supabase",
        # "redis",
        # # Mobile App Development
        # "mobile app development",
        # "cross platform app development",
        # "native app development",
        # "hybrid app development",
        # "android app development",
        # "ios app development",
        # "react native development",
        # "flutter development",
        # "expo development",
        # "swift development",
        # "kotlin development",
        # "mobile app mvp",
        # "mobile app migration",
        # # CMS Development
        # "wordpress development",
        # "elementor development",
        # "woocommerce development",
        # "shopify development",
        # "shopify plus",
        # "shopify theme development",
        # "shopify app development",
        # "shopify store setup",
        # "shopify store migration",
        # "webflow development",
        # "wix development",
        # "squarespace development",
        # "framer development",
        # "hubspot cms",
        # "drupal development",
        # "joomla development",
        # "magento development",
        # "bigcommerce development",
        # "ghost cms",
        # "strapi cms",
        # "headless cms",
        # # Ecommerce
        # "ecommerce development",
        # "online store development",
        # "b2b ecommerce",
        # "b2c ecommerce",
        # "multi vendor marketplace",
        # "subscription platform",
        # "payment gateway integration",
        # "stripe integration",
        # "paypal integration",
        # "inventory management",
        # "pos integration",
        # # UI/UX Design
        # "ui design",
        # "ux design",
        # "product design",
        # "mobile app design",
        # "website design",
        # "dashboard design",
        # "saas dashboard design",
        # "landing page design",
        # "wireframing",
        # "prototyping",
        # "design system",
        # "figma design",
        # "adobe xd",
        # "user research",
        # "ux audit",
        # "information architecture",
        # # Graphic Design
        # "logo design",
        # "brand identity",
        # "branding",
        # "visual identity",
        # "brand guidelines",
        # "business card design",
        # "social media design",
        # "banner design",
        # "brochure design",
        # "flyer design",
        # "packaging design",
        # "infographic design",
        # "pitch deck design",
        # "presentation design",
        # # AI & Automation
        # "ai integration",
        # "openai integration",
        # "chatgpt integration",
        # "ai chatbot development",
        # "ai agent development",
        # "automation development",
        # "workflow automation",
        # "zapier",
        # "make.com",
        # "n8n",
        # "crm automation",
        # "business automation",
        # "email automation",
        # "whatsapp automation",
        # # Cloud & DevOps
        # "aws",
        # "azure",
        # "google cloud",
        # "cloudflare",
        # "docker",
        # "kubernetes",
        # "ci/cd",
        # "github actions",
        # "vercel",
        # "netlify",
        # "digitalocean",
        # # SEO & Marketing
        # "technical seo",
        # "on-page seo",
        # "website optimization",
        # "core web vitals",
        # "website speed optimization",
        # "google analytics",
        # "google search console",
        # "schema markup",
        # "local seo",
        # # APIs & Integrations
        # "rest api",
        # "graphql",
        # "webhooks",
        # "oauth",
        # "jwt authentication",
        # "twilio",
        # "sendgrid",
        # "mailchimp",
        # "hubspot",
        # "salesforce",
        # "zoho crm",
        # "quickbooks",
        # "netsuite",
        # "google maps api",
        # "google calendar api",
        # "gmail api",
    ])

    # Platforms to scrape: "upwork", "vollna", "freelancer", "guru", or "all"
    platforms: list[str] = field(default_factory=lambda: ["all"])

    # Scraping behavior
    max_results_per_keyword: int = 20
    page_limit: int = 2
    min_delay: float = 1.5
    max_delay: float = 4.0
    timeout: int = 30
    max_retries: int = 3
    max_platform_workers: int = 4
    max_browser_workers: int = 2
    upwork_keyword_workers: int = 2
    http_keyword_workers: int = 3
    event_queue_size: int = 100
    output_queue_size: int = 500

    # Daily adaptive run policy
    adaptive_daily_limits: bool = True
    force_catch_up: bool = False
    first_daily_run_results: int = 50
    first_daily_run_pages: int = 3
    later_daily_run_results: int = 20
    later_daily_run_pages: int = 3
    later_daily_run_recency_hours: float = 2.0
    first_daily_run_recency_hours: float = 14.0
    catch_up_after_hours: float = 14.0
    catch_up_max_results_per_keyword: int = 1000
    catch_up_max_pages: int = 100
    keep_unknown_posted_dates: bool = True
    normal_keep_unknown_posted_dates: bool = False
    # Runtime value set by LeadEngine for age-aware platform pagination.
    collection_recency_hours: float | None = None
    local_timezone: str = os.getenv("SCRAPER_TIMEZONE", "Asia/Karachi")

    # TLS fingerprint
    impersonate_browser: str = "chrome"

    # Target locations — only keep leads matching these (empty = keep all)
    target_locations: list[str] = field(default_factory=lambda: [
        "United States", "Canada", "North America", "America",
    ])

    # Output
    output_dir: str = "output"
    output_format: str = "csv"  # csv | json
    database_path: str = "data/leads.db"

    # Google Sheets
    google_sheet_id: str = os.getenv("GOOGLE_SHEET_ID", "")
    google_credentials_path: str = os.getenv("GOOGLE_CREDENTIALS_PATH", "service-account.json")
    google_sheet_tab: str = os.getenv("GOOGLE_SHEET_TAB", "Leads")
    sheets_batch_size: int = 5
    sheets_min_lead_score: int = 0
    sheets_min_write_interval: float = 1.1
    sheets_retry_attempts: int = 5
    sheets_retry_base_delay: float = 5.0
    sheets_retry_max_delay: float = 60.0
    sheets_quota_cooldown: float = 60.0

    # Selenium / authenticated Upwork
    upwork_username: str = os.getenv("UPWORK_USERNAME", "")
    upwork_password: str = os.getenv("UPWORK_PASSWORD", "")
    selenium_max_scrolls: int = 8
    selenium_command_timeout: float = 45.0
    upwork_location_timeout: int = 12
    upwork_verification_timeout: int = int(
        os.getenv("UPWORK_VERIFICATION_TIMEOUT", "180")
    )

    # Bark.com
    bark_username: str = os.getenv("BARK_USERNAME", "")
    bark_password: str = os.getenv("BARK_PASSWORD", "")

    @property
    def resolved_platforms(self) -> list[str]:
        """Resolve 'all' to actual platform list."""
        if "all" in self.platforms:
            return list(ALL_PLATFORMS)
        return self.platforms

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.upwork.com/",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
