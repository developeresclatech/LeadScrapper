"""
Lead Analyzer — extracts business clues, scores leads, and classifies priority.
Pure Python: regex + keyword rules, no external AI models.
"""

import re
import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse
from typing import Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Scoring thresholds (easy to modify)
# ──────────────────────────────────────────────────────────────
SCORE_GREEN = 70
SCORE_YELLOW = 50

# Positive scores
POINTS_WEBSITE = 25
POINTS_COMPANY_NAME = 15
POINTS_BUSINESS_EMAIL = 20
POINTS_GENERAL_EMAIL = 10
POINTS_DECISION_MAKER = 10
POINTS_BUDGET = 10
POINTS_SERVICE_KEYWORD = 10
POINTS_LONG_TERM = 10
POINTS_BUSINESS_CLUE = 10
POINTS_FULL_DESCRIPTION = 10

# Negative scores
POINTS_STUDENT = -40
POINTS_HOMEWORK = -40
POINTS_UNPAID = -50
POINTS_FREE_SAMPLE = -30
POINTS_LOW_BUDGET = -30
POINTS_SPAM = -50
POINTS_NO_SERVICE_MATCH = -20

# ──────────────────────────────────────────────────────────────
# Ignored domains (job platforms, not client websites)
# ──────────────────────────────────────────────────────────────
IGNORED_DOMAINS = {
    "upwork.com", "freelancer.com", "fiverr.com", "guru.com",
    "vollna.com", "indeed.com", "glassdoor.com", "linkedin.com",
    "behance.net", "dribbble.com", "toptal.com", "workable.com",
    "simplyhired.com", "monster.com", "ziprecruiter.com",
    "craigslist.org", "facebook.com", "twitter.com", "instagram.com",
    "youtube.com", "tiktok.com", "reddit.com",
}

EMAIL_IGNORED_DOMAINS = {
    "upwork.com", "freelancer.com", "fiverr.com", "guru.com",
    "vollna.com", "indeed.com", "glassdoor.com", "linkedin.com",
}

GENERIC_EMAIL_PREFIXES = {
    "admin", "info", "support", "contact", "help", "hello",
    "team", "office", "hr", "jobs", "careers", "noreply",
    "no-reply", "webmaster", "postmaster", "abuse",
}

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "aol.com", "icloud.com", "mail.com", "protonmail.com",
    "zoho.com", "yandex.com", "live.com", "msn.com",
}

# ──────────────────────────────────────────────────────────────
# Decision-maker titles
# ──────────────────────────────────────────────────────────────
DECISION_MAKER_TITLES = [
    "founder", "co-founder", "cofounder", "ceo", "chief executive",
    "owner", "president", "cto", "chief technology",
    "coo", "chief operating", "director", "managing director",
    "hiring manager", "recruiter", "project manager",
    "head of", "vp of", "vice president",
    "decision maker", "decision-maker",
]

# ──────────────────────────────────────────────────────────────
# Business clue phrases
# ──────────────────────────────────────────────────────────────
BUSINESS_CLUE_PHRASES = [
    "our company", "our startup", "our agency", "our business",
    "our organization", "our team", "we are a saas company",
    "founded by", "work directly with the founder",
    "report to the ceo", "our website", "company website",
    "visit us at", "learn more at", "long-term opportunity",
    "ongoing work", "potential for future projects",
    "we are looking for", "we need a", "we want to hire",
    "our platform", "our product", "our app", "our application",
    "we are building", "we are developing", "we are growing",
    "our clients", "our customers", "our users",
    "my business", "my company", "my startup", "my agency",
    "side business", "small business", "online business",
    "my small business", "my online store",
    "i run a", "i own a", "i have a",
    "we have a", "we run a",
    "for my business", "for our business",
    "selling my products", "sell my products",
    "my website", "my online",
]

# ──────────────────────────────────────────────────────────────
# Negative keywords
# ──────────────────────────────────────────────────────────────
STUDENT_KEYWORDS = [
    "student", "homework", "assignment", "coursework", "thesis",
    "dissertation", "university project", "college project",
    "school project", "academic", "class project",
]

UNPAID_KEYWORDS = [
    "unpaid", "volunteer", "for free", "no budget", "no pay",
    "free work", "exposure only", "portfolio building",
    "charity", "non-profit project",
]

FREE_SAMPLE_KEYWORDS = [
    "free sample", "free trial", "test project", "sample work",
    "demonstration", "proof of concept", "poc only",
    "just a small", "very simple", "quick task",
]

SPAM_KEYWORDS = [
    "earn money", "make money fast", "work from home guaranteed",
    "click here", "limited time", "act now", "free money",
    "get rich", "financial freedom", "passive income",
]

# ──────────────────────────────────────────────────────────────
# Service keywords for matching
# ──────────────────────────────────────────────────────────────
SERVICE_KEYWORDS = [
    "web development", "website development", "app development",
    "mobile app", "react", "next.js", "node.js", "python",
    "django", "fastapi", "laravel", "php", "shopify", "wordpress",
    "woocommerce", "webflow", "ui/ux", "design", "ecommerce",
    "saas", "api", "database", "mongodb", "postgresql", "mysql",
    "flutter", "react native", "angular", "vue", "typescript",
    "javascript", "html", "css", "tailwind", "bootstrap",
    "docker", "aws", "cloud", "devops", "seo", "automation",
    "ai", "chatbot", "openai", "crm", "erp", "dashboard",
    "frontend", "backend", "full stack", "mern", "mean",
    "graphql", "rest api", "firebase", "supabase",
    "logo design", "branding", "graphic design", "figma",
    "landing page", "business website", "corporate",
    "marketplace", "booking system", "portal", "membership",
    "cms", "headless", "strapi", "contentful",
    "stripe", "payment", "paypal", "integration",
    "automation", "zapier", "workflow", "n8n", "webiste", "custom website", "marketing", "mvp", "crm", "Ghl"
]

# ──────────────────────────────────────────────────────────────
# Low budget thresholds
# ──────────────────────────────────────────────────────────────
LOW_BUDGET_PATTERNS = [
    r"\$[0-9](?:\s|$)",            # $1 - $9 (space or end after digit)
    r"\$1[0-4](?:\s|$)",           # $10 - $14 (space or end after digit)
    r"under\s*\$15",
    r"under\s*\$20",
    r"budget[:\s]*\$[0-9]{1,2}(?:\s|$)",
]


@dataclass
class LeadAnalysis:
    """Result of analyzing a single job description."""

    # Extracted fields
    company_name: str = "Not Found"
    company_website: str = "Not Found"
    company_domain: str = "Not Found"
    email: str = "Not Found"
    business_email: str = "Not Found"
    phone: str = "Not Found"
    linkedin_url: str = "Not Found"
    decision_maker_name: str = "Not Found"
    decision_maker_title: str = "Not Found"
    budget: str = "Not Found"
    timeline: str = "Not Found"
    location: str = "Not Found"
    industry: str = "Not Found"
    technologies: str = "Not Found"
    services_required: str = "Not Found"
    qualification_reason: str = ""
    full_description: str = ""

    # Score and priority
    lead_score: int = 0
    priority: str = "RED"

    # Tracking what was found
    _found_website: bool = False
    _found_company: bool = False
    _found_business_email: bool = False
    _found_email: bool = False
    _found_decision_maker: bool = False
    _found_budget: bool = False
    _found_service: bool = False
    _found_long_term: bool = False
    _found_business_clue: bool = False
    _found_full_description: bool = False
    _is_student: bool = False
    _is_homework: bool = False
    _is_unpaid: bool = False
    _is_free_sample: bool = False
    _is_spam: bool = False
    _is_low_budget: bool = False
    _no_service_match: bool = False


class LeadAnalyzer:
    """Analyze job descriptions and extract business intelligence."""

    def __init__(self):
        self._compiled_patterns = self._compile_patterns()

    def _compile_patterns(self) -> dict:
        """Pre-compile regex patterns for performance."""
        return {
            "url": re.compile(
                r'https?://[^\s<>"\')\]]+',
                re.IGNORECASE,
            ),
            "url_domain": re.compile(
                r'(?:https?://)?(?:www\.)?([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z]{2,})+)',
                re.IGNORECASE,
            ),
            "email": re.compile(
                r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
                re.IGNORECASE,
            ),
            "phone": re.compile(
                r'(?:\+\d{1,4}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}(?:\s*(?:ext|extension|x)\s*\d{1,5})?',
            ),
            "phone_international": re.compile(
                r'(?:\+|00)\d{1,4}[-.\s]?\(?\d{1,4}\)?[-.\s]?\d{1,4}[-.\s]?\d{1,4}[-.\s]?\d{1,4}',
            ),
            "linkedin": re.compile(
                r'https?://(?:www\.)?linkedin\.com/(?:in|company)/[a-zA-Z0-9\-_/]+',
                re.IGNORECASE,
            ),
            "budget_usd": re.compile(
                r'(?<!\w)\$[\d,]+(?:\.\d{2})?'
                r'(?:/(?:hr|hour|month|project|fixed|mo|yr))?'
                r'(?:\s*[-–]\s*(?:\$)?[\d,]+(?:\.\d{2})?(?:/(?:hr|hour|month|project|fixed|mo|yr))?)?'
                r'(?!\s*[MKkB])(?![\w])',
                re.IGNORECASE,
            ),
            "budget_generic": re.compile(
                r'(?:budget|price|cost|pay|rate)[:\s]*\$?[\d,]+(?:\s*[-–]\s*\$?[\d,]+)?',
                re.IGNORECASE,
            ),
            "name_pattern": re.compile(
                r'(?:founder|co-founder|ceo|owner|president|cto|coo|director|hiring manager|recruiter|project manager)[:\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})',
                re.IGNORECASE,
            ),
            "timeline": re.compile(
                r'(?:duration|timeline|long[- ]?term|ongoing|months?|weeks?|years?)[:\s]*(?:\d+\s*(?:months?|weeks?|years?)|long[- ]?term|ongoing)',
                re.IGNORECASE,
            ),
            "industry": re.compile(
                r'(?:industry|sector|field|niche)[:\s]*([A-Z][a-zA-Z\s&,]+)',
            ),
        }

    def analyze(self, title: str, description: str, budget: str = "",
                url: str = "", platform: str = "") -> LeadAnalysis:
        """Main entry point: analyze a job and return LeadAnalysis."""
        analysis = LeadAnalysis()
        analysis.full_description = description[:2000] if description else ""
        analysis._found_full_description = bool(
            description and description.strip()
        )
        text = f"{title} {description}".strip()

        if not text or len(text) < 10:
            analysis.qualification_reason = "Insufficient description text"
            return analysis

        try:
            self._extract_all(analysis, text, description, budget, url)
            self._score(analysis)
            self._classify(analysis)
        except Exception as exc:
            logger.error("Analysis error: %s", exc)
            analysis.qualification_reason = f"Analysis error: {exc}"

        return analysis

    # ──────────────────────────────────────────────────────
    # Extraction methods
    # ──────────────────────────────────────────────────────

    def _extract_all(self, a: LeadAnalysis, text: str, desc: str,
                     budget: str, url: str):
        """Run all extraction methods."""
        self._extract_urls(a, desc)
        self._extract_emails(a, desc)
        self._extract_company_name_from_text(a, text)
        self._extract_phone(a, desc)
        self._extract_linkedin(a, desc)
        self._extract_decision_maker(a, desc)
        self._extract_budget(a, text, budget)
        self._extract_timeline(a, desc)
        self._extract_location(a, desc)
        self._extract_industry(a, desc)
        self._extract_technologies(a, desc)
        self._extract_services(a, desc)
        self._detect_negatives(a, text)
        self._detect_business_clues(a, text)

    def _extract_urls(self, a: LeadAnalysis, text: str):
        """Extract company website from text."""
        urls = self._compiled_patterns["url"].findall(text)
        domains_seen = set()

        # Known non-website patterns (tech terms that look like URLs)
        KNOWN_TECH_TERMS = {
            "react.js", "reactjs.com", "vue.js", "vuejs.com",
            "next.js", "nextjs.org", "node.js", "nodejs.org",
            "express.js", "angular.io", "typescript",
        }

        for raw_url in urls:
            raw_url = raw_url.rstrip(".,;:!?)")
            parsed = urlparse(raw_url)
            domain = parsed.netloc.lower()
            if domain.startswith("www."):
                domain = domain[4:]

            if not domain or domain in domains_seen:
                continue
            if any(domain.endswith(d) for d in IGNORED_DOMAINS):
                continue
            if any(domain.endswith(d) for d in EMAIL_IGNORED_DOMAINS):
                continue

            # Skip known tech terms that look like URLs
            full_url_lower = raw_url.lower()
            if any(term in full_url_lower for term in KNOWN_TECH_TERMS):
                continue

            # Skip URLs that are just documentation/reference sites
            skip_patterns = [
                "docs.", "documentation.", "github.com", "stackoverflow.com",
                "npmjs.com", "pypi.org", "mozilla.org", "w3schools.com",
                "medium.com", "dev.to", "hackernoon.com",
            ]
            if any(domain.startswith(p) or domain == p for p in skip_patterns):
                continue

            domains_seen.add(domain)

            if a.company_website == "Not Found":
                a.company_website = raw_url
                a.company_domain = domain
                a._found_website = True

                # Generate company name from domain if not found
                if a.company_name == "Not Found":
                    name = self._domain_to_name(domain)
                    a.company_name = name
                    a._found_company = True

        # Also check bare domain patterns in text (but only if no URL found yet)
        if not a._found_website:
            domain_matches = self._compiled_patterns["url_domain"].findall(text)
            for dm in domain_matches:
                dm = dm.lower().strip(".")
                if any(dm.endswith(d) for d in IGNORED_DOMAINS):
                    continue
                # Skip if it's a known tech term
                if any(term in dm for term in KNOWN_TECH_TERMS):
                    continue
                if "." in dm and len(dm) > 5:
                    a.company_website = f"https://{dm}"
                    a.company_domain = dm
                    a._found_website = True
                    if a.company_name == "Not Found":
                        a.company_name = self._domain_to_name(dm)
                        a._found_company = True
                    break

    def _extract_emails(self, a: LeadAnalysis, text: str):
        """Extract emails, preferring business emails."""
        emails = self._compiled_patterns["email"].findall(text)
        emails = list(dict.fromkeys(e.lower().strip(".") for e in emails))

        business_email = None
        general_email = None

        for email in emails:
            domain = email.split("@")[1]
            prefix = email.split("@")[0]

            # Skip platform emails
            if any(domain.endswith(d) for d in EMAIL_IGNORED_DOMAINS):
                continue

            # Company domain email (not free email provider) — always prefer
            if domain not in FREE_EMAIL_DOMAINS:
                # Even generic prefixes are useful if domain is a company domain
                business_email = email
                break

            # Free email provider — check prefix
            if prefix in GENERIC_EMAIL_PREFIXES:
                continue

            if general_email is None:
                general_email = email

        if business_email:
            a.business_email = business_email
            a.email = business_email
            a._found_business_email = True
            a._found_email = True
        elif general_email:
            a.email = general_email
            a._found_email = True

        # If we found a business email, also extract the company from it
        if a._found_business_email and a.company_name == "Not Found":
            domain = a.business_email.split("@")[1]
            if domain not in FREE_EMAIL_DOMAINS:
                a.company_name = self._domain_to_name(domain)
                a.company_domain = domain
                a._found_company = True
                if a.company_website == "Not Found":
                    a.company_website = f"https://{domain}"
                    a.company_domain = domain
                    a._found_website = True

    def _extract_company_name_from_text(self, a: LeadAnalysis, text: str):
        """Extract company name directly from description patterns."""
        if a.company_name != "Not Found":
            return

        patterns = [
            r'(?:company|organization|agency|startup|studio)\s*(?::|is|name|called|name is)?\s*["\u201c]*([A-Z][A-Za-z0-9\s&.]+?)["\u201d]*(?:\s+(?:is|was|has|provides|specializes|offers|based|located|focuses|works|that|which|a\s+))',
            r'(?:we\s+are|i\s+am|i\'?m)\s+(?:a\s+|an\s+)?(?:small\s+|new\s+|young\s+)?(?:company|startup|agency|studio|firm|business|saas)\s+(?:called|named)?\s*["\u201c]*([A-Z][A-Za-z0-9\s&.]+?)["\u201d]*(?:\s+(?:that|which|and|,|\.|!|\?))',
            r'our\s+(?:company|startup|agency|business|organization|platform|product|app|application|website)\s+(?:is\s+|name\s+)?(?:called\s+)?["\u201c]*([A-Z][A-Za-z0-9\s&.]+?)["\u201d]*(?:\s+(?:that|which|is|and|,|\.|!|\?))',
            r'at\s+([A-Z][A-Za-z0-9\s&.]+?)(?:,\s+(?:we|our|the|an?|a\s+)|\.\s+(?:we|our|the))\s',
            r'(?:founded|built|created|launched|started)\s+(?:in\s+\d{4}\s+)?(?:a\s+|an\s+)?(?:company|startup|agency|saas|platform|business)\s+(?:called|named)?\s*["\u201c]*([A-Z][A-Za-z0-9\s&.]+?)["\u201d]*(?:\s+(?:that|which|,|\.))',
            r'(?:ceo|founder|owner|president|cto|coo|director)\s+(?:of|at|@)\s+([A-Z][A-Za-z0-9\s&.]+?)(?:\s+(?:is|,|\.|\s+seeking|\s+hiring|\s+looking|\s+need))',
            r'(?:welcome\s+to|visit\s+)\s*["\u201c]*([A-Z][A-Za-z0-9\s&.]+?)["\u201d]*(?:\s+(?:website|platform|app|application|!|\.))',
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                name = match.group(1).strip().rstrip(".,;:!?\"'")
                # Validate — ignore very short matches or common non-company words
                if len(name) > 2 and len(name) < 80 and name.lower() not in {
                    "the", "our", "we", "you", "they", "this", "that",
                    "your", "their", "its", "them", "us",
                }:
                    a.company_name = name
                    a._found_company = True
                    return

    def _extract_phone(self, a: LeadAnalysis, text: str):
        """Extract phone numbers."""
        phones = self._compiled_patterns["phone"].findall(text)
        if not phones:
            phones = self._compiled_patterns["phone_international"].findall(text)
        for p in phones:
            cleaned = re.sub(r'[^\d+]', '', p)
            if len(cleaned) >= 7:
                a.phone = p.strip()
                break

    def _extract_linkedin(self, a: LeadAnalysis, text: str):
        """Extract LinkedIn URLs."""
        matches = self._compiled_patterns["linkedin"].findall(text)
        if matches:
            a.linkedin_url = matches[0]

    def _extract_decision_maker(self, a: LeadAnalysis, text: str):
        """Extract decision-maker name and title."""
        for title in DECISION_MAKER_TITLES:
            pattern = re.compile(
                rf'(?:{re.escape(title)})[:\s,]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){{0,2}})',
                re.IGNORECASE,
            )
            match = pattern.search(text)
            if match:
                name = match.group(1).strip()
                # Validate it looks like a name (not a common word)
                if len(name) > 2 and not name.lower() in {"the", "our", "we", "you", "our team", "the team"}:
                    a.decision_maker_name = name
                    a.decision_maker_title = title.title()
                    a._found_decision_maker = True
                    return

        # Also check for "name, title" patterns
        name_patterns = [
            r'([A-Z][a-z]+\s+[A-Z][a-z]+),?\s*(?:CEO|CTO|COO|Founder|Co-Founder|Director|Owner|President)',
            r'(?:CEO|CTO|COO|Founder|Co-Founder|Director|Owner|President)[,\s]+([A-Z][a-z]+\s+[A-Z][a-z]+)',
            r'([A-Z][a-z]+\s+[A-Z][a-z]+)\s*\|\s*(?:CEO|CTO|COO|Founder|Co-Founder|Director|Owner|President)',
            r'(?:CEO|CTO|COO|Founder|Co-Founder|Director|Owner|President)\s*\|\s*([A-Z][a-z]+\s+[A-Z][a-z]+)',
            r'([A-Z][a-z]+\s+[A-Z][a-z]+)\s*[–—]\s*(?:CEO|CTO|COO|Founder|Co-Founder|Director|Owner|President)',
            r'(?:CEO|CTO|COO|Founder|Co-Founder|Director|Owner|President)\s*[–—]\s*([A-Z][a-z]+\s+[A-Z][a-z]+)',
            r'([A-Z][a-z]+\s+[A-Z][a-z]+)\s*\((?:CEO|CTO|COO|Founder|Co-Founder|Director|Owner|President)\)',
            r'(?:contact|reach|talk to|speak with|email)\s+([A-Z][a-z]+\s+[A-Z][a-z]+),?\s*(?:the\s+)?(?:CEO|CTO|COO|Founder|Owner|Director|Manager)',
        ]
        for pat in name_patterns:
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                a.decision_maker_name = match.group(1).strip()
                # Extract the title from the match
                title_match = re.search(r'(?:CEO|CTO|COO|Founder|Co-Founder|Director|Owner|President|Manager)', match.group(0), re.IGNORECASE)
                if title_match:
                    a.decision_maker_title = title_match.group(0).title()
                else:
                    a.decision_maker_title = "Decision Maker"
                a._found_decision_maker = True
                return

    def _extract_budget(self, a: LeadAnalysis, text: str, budget_str: str):
        """Extract budget information."""
        # Use provided budget first
        if budget_str and budget_str.strip():
            a.budget = budget_str.strip()
            a._found_budget = True
            return

        # Try USD pattern
        match = self._compiled_patterns["budget_usd"].search(text)
        if match:
            a.budget = match.group(0).strip()
            a._found_budget = True
            return

        # Try generic budget pattern
        match = self._compiled_patterns["budget_generic"].search(text)
        if match:
            a.budget = match.group(0).strip()
            a._found_budget = True

    def _extract_timeline(self, a: LeadAnalysis, text: str):
        """Extract timeline/duration."""
        # Check for long-term keywords
        long_term_patterns = [
            r'long[- ]?term',
            r'ongoing\s+(?:work|project|opportunity)',
            r'potential\s+for\s+future',
            r'permanent\s+position',
            r'full[- ]?time',
        ]
        for pat in long_term_patterns:
            if re.search(pat, text, re.IGNORECASE):
                a.timeline = "Long-term"
                a._found_long_term = True
                return

        # Check for duration mentions
        match = self._compiled_patterns["timeline"].search(text)
        if match:
            a.timeline = match.group(0).strip()

    def _extract_location(self, a: LeadAnalysis, text: str):
        """Extract location from text.  Expanded to detect US/CA states,
        provinces, cities, timezones and Remote patterns."""

        # 1 – structured patterns
        location_patterns = [
            r'(?:location|based in|located in|headquarters|office in)[:\s]*([A-Z][a-zA-Z\s,]+)',
            r'(?:remote|onsite|on-site|hybrid)\s*(?:[-–]?\s*)?([A-Z][a-zA-Z\s,]+)?',
        ]
        for pat in location_patterns:
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                loc = match.group(1).strip() if match.group(1) else ""
                if loc and len(loc) > 2:
                    a.location = loc[:100]
                    return

        # 2 – US-specific full names
        us_states = [
            "Alabama", "Alaska", "Arizona", "Arkansas", "California",
            "Colorado", "Connecticut", "Delaware", "Florida", "Georgia",
            "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas",
            "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts",
            "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana",
            "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico",
            "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma",
            "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
            "South Dakota", "Tennessee", "Texas", "Utah", "Vermont",
            "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
            "District of Columbia", "Washington DC",
        ]

        us_cities = [
            "New York City", "New York", "San Francisco", "Los Angeles",
            "Chicago", "Boston", "Austin", "Seattle", "Miami", "Denver",
            "Dallas", "Houston", "Atlanta", "Portland", "Phoenix",
            "San Diego", "Las Vegas", "Philadelphia", "Nashville",
            "Charlotte", "Orlando", "San Jose", "Pittsburgh", "Minneapolis",
            "Tampa", "Raleigh", "Salt Lake City", "Sacramento",
            "Columbus", "Indianapolis", "Detroit", "Milwaukee",
            "Albuquerque", "Baltimore", "Kansas City", "St. Louis",
            "Cleveland", "Cincinnati", "Memphis", "Louisville",
        ]

        ca_provinces = [
            "Ontario", "British Columbia", "Quebec", "Alberta",
            "Manitoba", "Saskatchewan", "Nova Scotia", "New Brunswick",
            "Newfoundland", "Prince Edward Island", "Yukon", "Nunavut",
            "Northwest Territories",
        ]

        ca_cities = [
            "Toronto", "Vancouver", "Montreal", "Calgary", "Edmonton",
            "Ottawa", "Winnipeg", "Quebec City", "Hamilton", "Halifax",
            "London", "Kitchener", "Waterloo", "Mississauga", "Brampton",
        ]

        us_timezones = [
            "EST", "PST", "CST", "MST",
            "Eastern Standard Time", "Pacific Standard Time",
            "Central Standard Time", "Mountain Standard Time",
            "Eastern Time", "Pacific Time", "Central Time", "Mountain Time",
            "GMT-05:00", "GMT-08:00", "GMT-04:00", "GMT-07:00", "GMT-06:00",
            "GMT-5", "GMT-8", "GMT-4", "GMT-7", "GMT-6",
        ]

        # Combined priority — check from most specific to least
        text_lower = text.lower()

        # First: explicit country-level matches
        country_map = {
            "united states": "United States", "usa": "USA", "u.s.": "USA",
            "canada": "Canada", "americas": "Americas",
        }
        for key, label in country_map.items():
            if key in text_lower:
                a.location = label
                return

        # US states
        for state in us_states:
            if state.lower() in text_lower:
                a.location = f"United States ({state})"
                return

        # US cities
        for city in us_cities:
            if city.lower() in text_lower:
                a.location = f"United States ({city})"
                return

        # Canadian provinces
        for prov in ca_provinces:
            if prov.lower() in text_lower:
                a.location = f"Canada ({prov})"
                return

        # Canadian cities
        for city in ca_cities:
            if city.lower() in text_lower:
                a.location = f"Canada ({city})"
                return

        # US timezones → implies US client
        for tz in us_timezones:
            if tz.lower() in text_lower:
                a.location = "United States"
                return

        # Remote / work-from-home
        remote_patterns = [
            r'\bremote\b', r'work from home', r'work-from-home',
            r'100%\s*remote', r'fully\s*remote', r'remote[\s-]?(?:first|only)',
            r'\bwfh\b',
        ]
        for pat in remote_patterns:
            if re.search(pat, text_lower):
                a.location = "Remote"
                return

        # Fallback: generic city/country list (keep for non-US/CA detection)
        fallback_locations = [
            # Non-US countries to detect as non-target
            "United Kingdom", "UK", "Australia", "Germany", "India",
            "Pakistan", "London", "Berlin", "Dubai", "Singapore",
            "Europe", "Asia", "United Arab Emirates", "UAE",
            "Saudi Arabia", "France", "Spain", "Italy", "Netherlands",
            "Switzerland", "Sweden", "Norway", "Denmark", "Finland",
            "Belgium", "Austria", "Ireland", "Poland", "Portugal",
            "Brazil", "Mexico", "Japan", "South Korea", "China",
            "Hong Kong", "New Zealand", "South Africa", "Nigeria",
            "Kenya", "Paris", "Amsterdam", "Sydney", "Melbourne",
            "Mumbai", "Delhi", "Bangalore", "Hyderabad",
            "Abu Dhabi", "Riyadh", "Doha", "Zurich", "Geneva",
        ]
        for loc in fallback_locations:
            if loc.lower() in text_lower:
                a.location = loc
                return

    def _extract_industry(self, a: LeadAnalysis, text: str):
        """Extract industry from text."""
        industries = [
            "fintech", "healthtech", "edtech", "ecommerce", "e-commerce",
            "saas", "ai", "blockchain", "crypto", "real estate",
            "healthcare", "education", "finance", "banking", "insurance",
            "retail", "logistics", "travel", "food", "gaming",
            "media", "entertainment", "automotive", "manufacturing",
            "non-profit", "ngo", "government",
        ]
        for ind in industries:
            if ind.lower() in text.lower():
                a.industry = ind.title()
                return

    def _extract_technologies(self, a: LeadAnalysis, text: str):
        """Extract technologies mentioned."""
        tech_map = {
            "react": "React.js", "reactjs": "React.js", "react.js": "React.js",
            "next": "Next.js", "nextjs": "Next.js", "next.js": "Next.js",
            "vue": "Vue.js", "vuejs": "Vue.js", "vue.js": "Vue.js",
            "angular": "Angular",
            "node": "Node.js", "nodejs": "Node.js", "node.js": "Node.js",
            "express": "Express.js", "expressjs": "Express.js",
            "python": "Python", "django": "Django", "flask": "Flask",
            "fastapi": "FastAPI",
            "php": "PHP", "laravel": "Laravel",
            "java": "Java", "spring": "Spring Boot",
            "typescript": "TypeScript", "javascript": "JavaScript",
            "html": "HTML5", "css": "CSS3",
            "tailwind": "Tailwind CSS", "bootstrap": "Bootstrap",
            "mongodb": "MongoDB", "mysql": "MySQL", "postgresql": "PostgreSQL",
            "firebase": "Firebase", "supabase": "Supabase", "redis": "Redis",
            "docker": "Docker", "kubernetes": "Kubernetes",
            "aws": "AWS", "azure": "Azure", "gcp": "Google Cloud",
            "shopify": "Shopify", "wordpress": "WordPress", "webflow": "Webflow",
            "flutter": "Flutter", "react native": "React Native",
            "graphql": "GraphQL", "rest": "REST API",
            "stripe": "Stripe", "paypal": "PayPal",
            "figma": "Figma", "sketch": "Sketch", "xd": "Adobe XD",
            "openai": "OpenAI", "chatgpt": "ChatGPT",
            "zapier": "Zapier", "make.com": "Make.com", "n8n": "n8n",
        }

        found = []
        text_lower = text.lower()
        for key, display in tech_map.items():
            pattern = r'\b' + re.escape(key) + r'\b'
            if re.search(pattern, text_lower):
                if display not in found:
                    found.append(display)

        if found:
            a.technologies = ", ".join(found[:10])

    def _extract_services(self, a: LeadAnalysis, text: str):
        """Extract services required."""
        text_lower = text.lower()
        found = []

        service_map = {
            "web development": "Web Development",
            "website development": "Website Development",
            "web application": "Web Application",
            "mobile app": "Mobile App Development",
            "ui/ux": "UI/UX Design",
            "ui design": "UI Design",
            "ux design": "UX Design",
            "ecommerce": "Ecommerce Development",
            "e-commerce": "Ecommerce Development",
            "saas": "SaaS Development",
            "api development": "API Development",
            "api integration": "API Integration",
            "crm": "CRM Development",
            "erp": "ERP Development",
            "dashboard": "Dashboard Development",
            "landing page": "Landing Page Design",
            "logo design": "Logo Design",
            "branding": "Branding",
            "seo": "SEO",
            "automation": "Automation",
            "chatbot": "AI Chatbot",
            "ai integration": "AI Integration",
            "database": "Database Development",
            "frontend": "Frontend Development",
            "backend": "Backend Development",
            "full stack": "Full Stack Development",
            "shopify": "Shopify Development",
            "wordpress": "WordPress Development",
            "webflow": "Webflow Development",
            "marketplace": "Marketplace Development",
            "booking system": "Booking System",
            "payment integration": "Payment Integration",
            "api": "API Development",
            "cloud": "Cloud Services",
            "devops": "DevOps",
        }

        for key, display in service_map.items():
            if key in text_lower:
                if display not in found:
                    found.append(display)

        if found:
            a.services_required = ", ".join(found[:8])
            a._found_service = True

    def _detect_negatives(self, a: LeadAnalysis, text: str):
        """Detect negative indicators."""
        text_lower = text.lower()

        for kw in STUDENT_KEYWORDS:
            if kw in text_lower:
                a._is_student = True
                break

        for kw in UNPAID_KEYWORDS:
            if kw in text_lower:
                a._is_unpaid = True
                break

        for kw in FREE_SAMPLE_KEYWORDS:
            if kw in text_lower:
                a._is_free_sample = True
                break

        for kw in SPAM_KEYWORDS:
            if kw in text_lower:
                a._is_spam = True
                break

        # Check low budget
        for pattern in LOW_BUDGET_PATTERNS:
            if re.search(pattern, text_lower):
                a._is_low_budget = True
                break

        # Check if no service match
        if not a._found_service:
            match_count = sum(1 for kw in SERVICE_KEYWORDS[:30] if kw in text_lower)
            if match_count == 0:
                a._no_service_match = True

    def _detect_business_clues(self, a: LeadAnalysis, text: str):
        """Detect business-related phrases."""
        text_lower = text.lower()
        for phrase in BUSINESS_CLUE_PHRASES:
            if phrase in text_lower:
                a._found_business_clue = True
                return

    # ──────────────────────────────────────────────────────
    # Scoring
    # ──────────────────────────────────────────────────────

    def _score(self, a: LeadAnalysis):
        """Calculate lead score from 0 to 100."""
        score = 0

        # Positive
        if a._found_website:
            score += POINTS_WEBSITE
        if a._found_company:
            score += POINTS_COMPANY_NAME
        if a._found_business_email:
            score += POINTS_BUSINESS_EMAIL
        elif a._found_email:
            score += POINTS_GENERAL_EMAIL
        if a._found_decision_maker:
            score += POINTS_DECISION_MAKER
        if a._found_budget:
            score += POINTS_BUDGET
        if a._found_service:
            score += POINTS_SERVICE_KEYWORD
        if a._found_long_term:
            score += POINTS_LONG_TERM
        if a._found_business_clue:
            score += POINTS_BUSINESS_CLUE
        if a._found_full_description:
            score += POINTS_FULL_DESCRIPTION

        # Negative
        if a._is_student:
            score += POINTS_STUDENT
        if a._is_homework:
            score += POINTS_HOMEWORK
        if a._is_unpaid:
            score += POINTS_UNPAID
        if a._is_free_sample:
            score += POINTS_FREE_SAMPLE
        if a._is_low_budget:
            score += POINTS_LOW_BUDGET
        if a._is_spam:
            score += POINTS_SPAM
        if a._no_service_match:
            score += POINTS_NO_SERVICE_MATCH

        # Clamp 0-100
        a.lead_score = max(0, min(100, score))

    # ──────────────────────────────────────────────────────
    # Classification
    # ──────────────────────────────────────────────────────

    def _classify(self, a: LeadAnalysis):
        """Classify lead priority and build qualification reason."""
        details = []
        if a._found_website:
            details.append("company website")
        if a._found_business_email:
            details.append("business email")
        elif a._found_email:
            details.append("email")
        if a._found_decision_maker:
            details.append(
                f"decision-maker {a.decision_maker_name} "
                f"({a.decision_maker_title})"
            )
        if a._found_company:
            details.append("company name")
        if a._found_budget:
            details.append("budget")
        if a._found_service:
            details.append("services")
        if a._found_long_term:
            details.append("long-term")
        if a._found_full_description:
            details.append("description")

        if a.lead_score >= SCORE_GREEN:
            a.priority = "GREEN"
            suffix = f" — has {', '.join(details)}" if details else ""
            a.qualification_reason = (
                f"Score {a.lead_score}/100 — GREEN{suffix}"
            )
            return

        if a.lead_score >= SCORE_YELLOW:
            a.priority = "YELLOW"
            suffix = f" — has {', '.join(details)}" if details else ""
            a.qualification_reason = (
                f"Score {a.lead_score}/100 — YELLOW{suffix}"
            )
            return

        a.priority = "RED"
        negatives = []
        if a._is_student:
            negatives.append("student project")
        if a._is_unpaid:
            negatives.append("unpaid work")
        if a._is_free_sample:
            negatives.append("free sample request")
        if a._is_spam:
            negatives.append("spam/suspicious")
        if a._is_low_budget:
            negatives.append("very low budget")
        if a._no_service_match:
            negatives.append("no service match")

        if negatives:
            a.qualification_reason = f"Score {a.lead_score}/100 — {', '.join(negatives)}"
        else:
            a.qualification_reason = f"Score {a.lead_score}/100 — insufficient business data"

    # ──────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────

    @staticmethod
    def _domain_to_name(domain: str) -> str:
        """Generate a readable company name from a domain."""
        # Remove TLD
        parts = domain.split(".")
        if len(parts) >= 2:
            name_part = parts[-2]
        else:
            name_part = parts[0]

        # Split camelCase or hyphenated
        words = re.split(r'[-_]', name_part)
        return " ".join(w.capitalize() for w in words if w)
