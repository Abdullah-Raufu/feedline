"""
Feedline — RSS feeds -> SQLite -> cards, search, saved stories, accounts.

New in v4:
  * Sign-up / login with "keep me signed in on this device"
  * Language picker after the first login (8 languages, RTL support)
  * Country selector -> local news is fetched and shown first
  * Profile & settings: username, password, country, language, card theme, layout
  * Search, saved/bookmark stories, compact list view
  * Search falls back to the last 30 days, then to a live web news search
    (Bing, with Google as backup) and stores what it finds
  * Cross-feed story de-duplication, safer parallel feed fetching, better images
  * Archive tabs: 24h = today, 48h = yesterday, 7d = 3-7 days, 14d = 8-30 days
  * Missing pictures are recovered from the article page (og:image) and an
    image proxy keeps hotlink-protected photos working

Setup:   pip install flask feedparser
         Set NEWS_SMTP_HOST (and the other NEWS_SMTP_* variables) to enable
         email OTP password recovery. See SMTP_SETUP.md.
Run:     python feedline.py
Open:    http://localhost:5000
"""
import gzip
import hashlib
import html
import json
import os
import re
import secrets
import smtplib
import ssl
import sqlite3
import tempfile
import threading
import traceback
import time
import unicodedata
import urllib.error
import urllib.request
from calendar import timegm
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

import feedparser
from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from jinja2 import DictLoader
from markupsafe import Markup, escape
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB = os.environ.get("NEWS_DB") or str(BASE_DIR / "feedline.db")


def log(*parts):
    """Print a line that always survives a non-UTF-8 Windows console."""
    text = " ".join(str(part) for part in parts)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "backslashreplace").decode("ascii"), flush=True)

REFRESH_MINUTES = 1            # poll every minute so the room stays alive
KEEP_DAYS = 30                 # the 14d tab reaches back to 30 days
COUNTRY_TTL = 30 * 60          # refresh local news for a country at most every 30 min
MAX_FEED_BYTES = 4_000_000
MAX_PAGE_BYTES = 900_000
FEED_TIMEOUT = 5
FEED_WORKERS = 48
FEEDS_PER_ROUND = 120       # only a rotating slice is fetched on each refresh
IMAGE_RETRY = 3 * 86400        # try a story page again after 3 days if it had no image
IMAGE_WORKERS = 16             # recover pictures in parallel so the grid fills fast
BACKFILL_BATCH = 600           # newest stories are repaired first
REPAIR_BATCH = 120
image_worker_event = threading.Event()
RESET_TTL = 10 * 60            # password-reset OTP lifetime
OTP_MAX_ATTEMPTS = 5
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ---------------------------------------------------------------- feeds
# Add or remove feeds freely. The key is the fallback topic, used only when no
# keyword matches an article.
FEEDS = {
    "Africa": [
        "https://feeds.bbci.co.uk/news/world/africa/rss.xml",
        "https://www.africanews.com/feed/rss",
        "https://www.premiumtimesng.com/feed",
        "https://www.myjoyonline.com/feed/",
        # Popular Nigerian news sites, added at the user's request.
        "https://www.vanguardngr.com/feed/",
        "https://rss.punchng.com/v1/category/latest_news",
        "https://consumerhalla.ng/feed/",
        "https://www.channelstv.com/feed/",
        "https://www.thisdaylive.com/feed/",
        "https://www.dailytrust.com.ng/feed",
        "https://thenationonlineng.net/feed/",
        "https://www.thecable.ng/feed",
        "https://guardian.ng/feed/",
        "https://www.legit.ng/feed",
    ],
    "Middle East": [
        "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
        "https://www.middleeasteye.net/rss",
        "https://www.timesofisrael.com/feed/",
    ],
    "Tech": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://feeds.arstechnica.com/arstechnica/index",
        "https://www.wired.com/feed/rss",
        "https://www.technologyreview.com/feed/",
    ],
    "AI": [
        "https://venturebeat.com/category/ai/feed/",
    ],
    "Business": [
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://www.theguardian.com/uk/business/rss",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "http://feeds.marketwatch.com/marketwatch/topstories/",
        "https://www.ft.com/rss/home",
    ],
    "Sports": [
        "https://feeds.bbci.co.uk/sport/rss.xml",
        "https://www.theguardian.com/uk/sport/rss",
        "https://www.skysports.com/rss/12040",
    ],
    "Science": [
        "https://www.theguardian.com/science/rss",
        "https://www.sciencedaily.com/rss/all.xml",
        "https://www.newscientist.com/feed/home/",
        "https://phys.org/rss-feed/",
        "https://www.nasa.gov/rss/dyn/breaking_news.rss",
    ],
    "Health": [
        "https://feeds.bbci.co.uk/news/health/rss.xml",
        "https://www.statnews.com/feed/",
    ],
    "Entertainment": [
        "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
        "https://www.theguardian.com/culture/rss",
        "https://variety.com/feed/",
        "https://deadline.com/feed/",
    ],
    # Regional feeds make country news richer even before the local feed loads.
    "World": [
        "http://feeds.bbci.co.uk/news/world/rss.xml",
        "https://feeds.bbci.co.uk/news/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.theguardian.com/world/rss",
        "https://www.theguardian.com/international/rss",
        "https://feeds.npr.org/1001/rss.xml",
        "https://rss.dw.com/rdf/rss-en-world",
        "https://www.france24.com/en/rss",
        "http://rss.cnn.com/rss/edition_world.rss",
        "https://feeds.skynews.com/feeds/rss/world.xml",
        "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
        "https://feeds.bbci.co.uk/news/uk/rss.xml",
        "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
        "https://feeds.bbci.co.uk/news/world/europe/rss.xml",
        "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
        "https://feeds.npr.org/1003/rss.xml",
        # High-volume Google News topic feeds. Their items still get sorted into
        # the normal topic tabs by TOPIC_KEYWORDS below.
        "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/headlines/section/topic/NATION?hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/headlines/section/topic/SPORTS?hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/headlines/section/topic/ENTERTAINMENT?hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/headlines/section/topic/SCIENCE?hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/headlines/section/topic/HEALTH?hl=en-US&gl=US&ceid=US:en",
    ],
}

# Used only when a normal refresh finds fewer than MIN_FRESH_PER_ROUND new
# stories. These are real news searches, not invented filler.
MIN_FRESH_PER_ROUND = 5
FRESH_QUERY_TERMS = (
    "breaking news",
    "world news",
    "Nigeria news",
    "Africa news",
    "technology news",
    "business news",
    "sports news",
    "health news",
    "science news",
)

# --------------------------------------------------------------- topics
# Checked top to bottom: the highest score wins; ties go to the earlier topic.
TOPIC_KEYWORDS = {
    "Accidents": [
        "accident", "accidents", "crash", "crashes", "crashed", "collision",
        "pile-up", "derail\\w*", "capsize\\w*", "explosion", "explosions",
        "blaze", "fire", "fires", "wildfire\\w*", "collapse\\w*", "drown\\w*",
        "gas leak", "plane crash", "bus crash", "road crash", "stampede",
        "earthquake\\w*", "tsunami\\w*", "landslide\\w*", "flood\\w*",
    ],
    "Promotions": [
        "sale", "sales", "discount\\w*", "deal", "deals", "promo", "promos",
        "promotion", "promotions", "coupon\\w*", "voucher\\w*", "black friday",
        "cyber monday", "giveaway\\w*", "cashback", "bargain\\w*", "% off",
        "special offer\\w*", "free gift",
    ],
    "Celebrations": [
        "celebrat\\w*", "festival\\w*", "birthday\\w*", "wedding\\w*",
        "anniversar\\w*", "christmas", "diwali", "eid", "ramadan", "carnival",
        "parade", "jubilee", "new year", "thanksgiving", "halloween",
        "congratulat\\w*", "honou?red", "tribute",
    ],
    "Crime": [
        "police", "arrest\\w*", "murder\\w*", "robbery", "robber\\w*", "court",
        "trial", "jailed", "prison", "kidnap\\w*", "fraud", "shooting",
        "stabbing", "theft", "suspect\\w*", "charged", "smuggl\\w*",
        "homicide\\w*", "assault\\w*", "corruption",
    ],
    "Politics": [
        "election\\w*", "president\\w*", "minister\\w*", "parliament\\w*",
        "senate", "senator\\w*", "congress", "government", "vote\\w*", "voters",
        "policy", "sanction\\w*", "campaign", "lawmakers", "mps?", "treaty",
        "diplomat\\w*", "summit", "referendum",
    ],
    "Health": [
        "health", "hospital\\w*", "vaccine\\w*", "disease\\w*", "cancer",
        "virus", "outbreak\\w*", "doctor\\w*", "nhs", "medical", "patients?",
        "cholera", "malaria", "covid", "measles", "diabetes", "obesity",
        "mental health", "pandemic\\w*",
    ],
    "Science": [
        "science", "scientist\\w*", "research\\w*", "researchers?", "study",
        "studies", "nasa", "spacecraft?", "satellite\\w*", "quantum",
        "physics", "astronom\\w*", "telescope\\w*", "dinosaur\\w*",
        "fossil\\w*", "genome\\w*", "dna", "particle\\w*", "experiment\\w*",
        "space station", "moon mission",
    ],
    "Climate": [
        "climate", "global warming", "emissions?", "carbon", "renewable\\w*",
        "solar", "wind farm\\w*", "drought\\w*", "heatwave\\w*", "heat wave\\w*",
        "temperature\\w*", "cop\\d+", "biodiversity", "extinction",
    ],
    "AI": [
        "ai", "artificial intelligence", "openai", "chatgpt", "gemini",
        "claude", "deepmind", "anthropic", "machine learning", "llm", "llms",
        "generative", "chatbot\\w*", "neural network\\w*",
    ],
    "Entertainment": [
        "film\\w*", "movie\\w*", "music", "singer", "album\\w*", "actor",
        "actress", "celebrity", "oscar\\w*", "grammy\\w*", "netflix", "concert\\w*",
        "tv series", "box office", "nollywood", "bollywood", "hollywood",
    ],
    "Sports": [
        "football", "soccer", "match", "league", "goal", "tennis", "nba", "nfl",
        "cricket", "olympic\\w*", "coach", "premier league", "champions league",
        "world cup", "rugby", "boxing", "formula 1", "f1",
    ],
    "Tech": [
        "apple", "google", "microsoft", "software", "startup\\w*",
        "cyber\\w*", "smartphone\\w*", "iphone", "android", "chip\\w*",
        "robot\\w*", "app", "apps", "openai", "tesla", "gadget\\w*",
    ],
    "Business": [
        "economy", "economic", "market\\w*", "stocks?", "inflation", "bank\\w*",
        "oil", "trade", "company", "companies", "shares", "prices?", "profit\\w*",
        "investors?", "currency", "tax\\w*",
    ],
    "Africa": [
        "africa", "african", "nigeria", "nigerian", "ghana", "kenya", "kenyan",
        "south africa", "ethiopia", "sudan", "somalia", "congo", "uganda",
        "tanzania", "zimbabwe", "cameroon", "senegal", "mali", "chad", "libya",
        "algeria", "morocco", "tunisia", "rwanda", "zambia", "mozambique",
        "botswana", "namibia", "ivory coast",
    ],
    "Middle East": [
        "middle east", "israel", "israeli", "palestin\\w*", "gaza", "lebanon",
        "syria", "iran", "iraqi?", "yemen", "saudi", "emirates?", "qatar",
        "jordan", "egypt", "turkey", "turkish", "hezbollah", "west bank",
    ],
}

TOPIC_REGEX = {
    name: re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I)
    for name, words in TOPIC_KEYWORDS.items()
}

ORDER = [
    "Accidents", "Promotions", "Celebrations", "Crime", "Politics", "Health",
    "Science", "Climate", "AI", "Entertainment", "Sports", "Tech", "Business",
    "Africa", "Middle East", "World",
]

COLORS = {
    "Accidents": "#ef4444",
    "Promotions": "#f59e0b",
    "Celebrations": "#ec4899",
    "Crime": "#64748b",
    "Politics": "#3b82f6",
    "Health": "#10b981",
    "Science": "#8b5cf6",
    "Climate": "#22c55e",
    "AI": "#06b6d4",
    "Entertainment": "#d946ef",
    "Sports": "#f97316",
    "Tech": "#0ea5e9",
    "Business": "#84cc16",
    "Africa": "#eab308",
    "Middle East": "#14b8a6",
    "World": "#6366f1",
}

# Each tab is an archive window: (hours ago it ends, hours ago it starts).
# 24h = the last day, 48h = the day before that, 7d = 3-7 days ago,
# 14d = 8-30 days ago. Windows never overlap, so every dated story shows once.
RANGES = {
    "24h": (0, 24),
    "48h": (24, 48),
    "7d": (72, 168),
    "14d": (192, 720),
}

# ------------------------------------------------------------- countries
# Local news for the chosen country is pulled on demand from Google News.
# "aliases" are used to mark country stories in the global feeds as local.
COUNTRIES = {
    # Africa
    "NG": {"name": "Nigeria", "aliases": ["lagos", "abuja", "nigerian"]},
    "GH": {"name": "Ghana", "aliases": ["accra", "ghanaian"]},
    "KE": {"name": "Kenya", "aliases": ["nairobi", "kenyan"]},
    "ZA": {"name": "South Africa", "aliases": ["johannesburg", "cape town", "south african"]},
    "EG": {"name": "Egypt", "aliases": ["cairo", "egyptian"]},
    "ET": {"name": "Ethiopia", "aliases": ["addis ababa", "ethiopian"]},
    "TZ": {"name": "Tanzania", "aliases": ["dar es salaam", "tanzanian"]},
    "UG": {"name": "Uganda", "aliases": ["kampala", "ugandan"]},
    "ZW": {"name": "Zimbabwe", "aliases": ["harare", "zimbabwean"]},
    "CM": {"name": "Cameroon", "aliases": ["yaounde", "douala", "cameroonian"]},
    "SN": {"name": "Senegal", "aliases": ["dakar", "senegalese"]},
    "MA": {"name": "Morocco", "aliases": ["rabat", "casablanca", "moroccan"]},
    "DZ": {"name": "Algeria", "aliases": ["algiers", "algerian"]},
    "TN": {"name": "Tunisia", "aliases": ["tunis", "tunisian"]},
    "LY": {"name": "Libya", "aliases": ["tripoli", "libyan"]},
    "SD": {"name": "Sudan", "aliases": ["khartoum", "sudanese"]},
    "SO": {"name": "Somalia", "aliases": ["mogadishu", "somali"]},
    "CD": {"name": "DR Congo", "aliases": ["kinshasa", "congolese"]},
    "CI": {"name": "Ivory Coast", "aliases": ["abidjan", "ivorian"]},
    "MZ": {"name": "Mozambique", "aliases": ["maputo", "mozambican"]},
    "ZM": {"name": "Zambia", "aliases": ["lusaka", "zambian"]},
    "BW": {"name": "Botswana", "aliases": ["gaborone", "botswanan"]},
    "NA": {"name": "Namibia", "aliases": ["windhoek", "namibian"]},
    "RW": {"name": "Rwanda", "aliases": ["kigali", "rwandan"]},
    "MW": {"name": "Malawi", "aliases": ["lilongwe", "malawian"]},
    # Americas
    "US": {"name": "United States", "aliases": ["united states", "washington", "white house", "american", "americans"]},
    "CA": {"name": "Canada", "aliases": ["ottawa", "toronto", "canadian"]},
    "MX": {"name": "Mexico", "aliases": ["mexico city", "mexican"]},
    "BR": {"name": "Brazil", "aliases": ["brasilia", "brazilian"]},
    "AR": {"name": "Argentina", "aliases": ["buenos aires", "argentine", "argentinian"]},
    "CL": {"name": "Chile", "aliases": ["santiago", "chilean"]},
    "CO": {"name": "Colombia", "aliases": ["bogota", "colombian"]},
    "PE": {"name": "Peru", "aliases": ["lima", "peruvian"]},
    # Asia
    "IN": {"name": "India", "aliases": ["delhi", "mumbai", "indian"]},
    "PK": {"name": "Pakistan", "aliases": ["islamabad", "karachi", "lahore", "pakistani"]},
    "BD": {"name": "Bangladesh", "aliases": ["dhaka", "bangladeshi"]},
    "LK": {"name": "Sri Lanka", "aliases": ["colombo", "sri lankan"]},
    "NP": {"name": "Nepal", "aliases": ["kathmandu", "nepali"]},
    "CN": {"name": "China", "aliases": ["beijing", "chinese"]},
    "JP": {"name": "Japan", "aliases": ["tokyo", "japanese"]},
    "KR": {"name": "South Korea", "aliases": ["seoul", "korean"]},
    "ID": {"name": "Indonesia", "aliases": ["jakarta", "indonesian"]},
    "PH": {"name": "Philippines", "aliases": ["manila", "filipino"]},
    "MY": {"name": "Malaysia", "aliases": ["kuala lumpur", "malaysian"]},
    "SG": {"name": "Singapore", "aliases": ["singaporean"]},
    "TH": {"name": "Thailand", "aliases": ["bangkok", "thai"]},
    "VN": {"name": "Vietnam", "aliases": ["hanoi", "vietnamese"]},
    # Middle East
    "SA": {"name": "Saudi Arabia", "aliases": ["riyadh", "saudi arabian"]},
    "AE": {"name": "United Arab Emirates", "aliases": ["dubai", "abu dhabi", "emirati"]},
    "QA": {"name": "Qatar", "aliases": ["doha", "qatari"]},
    "IL": {"name": "Israel", "aliases": ["jerusalem", "tel aviv", "israeli"]},
    "LB": {"name": "Lebanon", "aliases": ["beirut", "lebanese"]},
    "JO": {"name": "Jordan", "aliases": ["amman", "jordanian"]},
    "IQ": {"name": "Iraq", "aliases": ["baghdad", "iraqi"]},
    "IR": {"name": "Iran", "aliases": ["tehran", "iranian"]},
    "TR": {"name": "Turkey", "aliases": ["ankara", "istanbul", "turkish"]},
    "KW": {"name": "Kuwait", "aliases": ["kuwaiti"]},
    "OM": {"name": "Oman", "aliases": ["muscat", "omani"]},
    "BH": {"name": "Bahrain", "aliases": ["manama", "bahraini"]},
    "YE": {"name": "Yemen", "aliases": ["sanaa", "yemeni"]},
    # Europe
    "GB": {"name": "United Kingdom", "aliases": ["uk", "britain", "british", "england", "scotland", "wales", "london", "westminster"]},
    "IE": {"name": "Ireland", "aliases": ["dublin", "irish"]},
    "FR": {"name": "France", "aliases": ["paris", "french"]},
    "DE": {"name": "Germany", "aliases": ["berlin", "german"]},
    "ES": {"name": "Spain", "aliases": ["madrid", "spanish"]},
    "IT": {"name": "Italy", "aliases": ["rome", "italian"]},
    "NL": {"name": "Netherlands", "aliases": ["amsterdam", "dutch"]},
    "PT": {"name": "Portugal", "aliases": ["lisbon", "portuguese"]},
    "BE": {"name": "Belgium", "aliases": ["brussels", "belgian"]},
    "CH": {"name": "Switzerland", "aliases": ["bern", "swiss"]},
    "SE": {"name": "Sweden", "aliases": ["stockholm", "swedish"]},
    "NO": {"name": "Norway", "aliases": ["oslo", "norwegian"]},
    "DK": {"name": "Denmark", "aliases": ["copenhagen", "danish"]},
    "PL": {"name": "Poland", "aliases": ["warsaw", "polish"]},
    "UA": {"name": "Ukraine", "aliases": ["kyiv", "kiev", "ukrainian"]},
    "RU": {"name": "Russia", "aliases": ["moscow", "russian"]},
    # Oceania
    "AU": {"name": "Australia", "aliases": ["sydney", "canberra", "australian"]},
    "NZ": {"name": "New Zealand", "aliases": ["wellington", "auckland", "kiwi"]},
}

COUNTRY_OPTIONS = sorted(
    ((code, info["name"]) for code, info in COUNTRIES.items()),
    key=lambda item: item[1],
)

# ------------------------------------------------------ expanded source pool
# The hand-picked feeds above always stay in the rotation. The generated feeds
# below are real Google News / Bing RSS endpoints; they give the app a pool of
# 1,000+ distinct sources while each refresh fetches only a small rotating
# slice, so page loads never wait on a thousand network requests.
CORE_FEEDS = {category: list(urls) for category, urls in FEEDS.items()}

GOOGLE_TOPIC_FEEDS = {
    "WORLD": "World",
    "NATION": "World",
    "BUSINESS": "Business",
    "TECHNOLOGY": "Tech",
    "SPORTS": "Sports",
    "ENTERTAINMENT": "Entertainment",
    "SCIENCE": "Science",
    "HEALTH": "Health",
}

NEWS_QUERY_TERMS = (
    "breaking news", "latest headlines", "world news", "Nigeria news",
    "Africa news", "Lagos news", "Abuja news", "Ghana news", "Kenya news",
    "South Africa news", "technology news", "AI news", "business news",
    "economy news", "stock market news", "sports news", "football news",
    "Premier League news", "basketball news", "health news", "medical news",
    "science news", "space news", "climate news", "politics news",
    "election news", "court news", "crime news", "accident news", "fire news",
    "flood news", "promotion news", "sale news", "discount news",
    "entertainment news", "Nollywood news", "movie news", "music news",
    "smartphone news", "electric car news", "crypto news", "real estate news",
    "education news", "jobs news", "visa news", "travel news", "food news",
    "fashion news", "culture news", "wedding news", "festival news",
    "farming news", "agriculture news", "oil news", "currency news",
    "bank news", "startup news", "funding news", "cybersecurity news",
    "social media news", "higher education news", "transport news",
    "housing news", "small business news", "retail news", "manufacturing news",
    "energy news", "renewable energy news", "water news", "sanitation news",
    "public health news", "mental health news", "vaccine news",
    "local government news", "parliament news", "policy news",
    "humanitarian news", "refugee news", "development news",
    "artificial intelligence news", "robot news", "chip news",
)

SEARCH_LOCALES = (
    ("US", "en-US", "US:en"),
    ("GB", "en-GB", "GB:en"),
    ("NG", "en-NG", "NG:en"),
    ("IN", "en-IN", "IN:en"),
    ("AU", "en-AU", "AU:en"),
)

SEARCH_CATEGORIES = (
    "World", "Business", "Tech", "Sports", "Health", "Science",
    "Politics", "Entertainment", "Africa", "AI", "Climate",
    "Accidents", "Promotions",
)

_known_feed_urls = {url for urls in FEEDS.values() for url in urls}


def add_feed(category, url):
    """Add a distinct feed URL once, without duplicating the hand-picked list."""
    if url in _known_feed_urls:
        return
    _known_feed_urls.add(url)
    FEEDS.setdefault(category, []).append(url)


for _code in COUNTRIES:
    for _topic, _category in GOOGLE_TOPIC_FEEDS.items():
        add_feed(
            _category,
            "https://news.google.com/rss/headlines/section/topic/"
            + _topic
            + f"?hl=en-US&gl={_code}&ceid={_code}:en",
        )

for _index, _query in enumerate(NEWS_QUERY_TERMS):
    _search_category = SEARCH_CATEGORIES[_index % len(SEARCH_CATEGORIES)]
    for _gl, _hl, _ceid in SEARCH_LOCALES:
        add_feed(
            _search_category,
            "https://news.google.com/rss/search?"
            + urlencode({
                "q": _query,
                "hl": _hl,
                "gl": _gl,
                "ceid": _ceid,
            }),
        )
    add_feed(
        _search_category,
        "https://www.bing.com/news/search?"
        + urlencode({"q": _query, "format": "RSS"}),
    )

del _known_feed_urls

SOURCE_COUNT = sum(len(urls) for urls in FEEDS.values())
CORE_FEED_JOBS = [
    (category, url) for category, urls in CORE_FEEDS.items() for url in urls
]
ROTATING_FEED_JOBS = [
    (category, url)
    for category, urls in FEEDS.items()
    for url in urls
    if all(url != core_url for _core, core_url in CORE_FEED_JOBS)
]
feed_cursor = {"index": 0}

LANGUAGES = {
    "en": "English",
    "ar": "العربية",
    "fr": "Français",
    "es": "Español",
    "pt": "Português",
    "sw": "Kiswahili",
    "ur": "اردو",
    "hi": "हिन्दी",
}
RTL_LANGS = {"ar", "ur"}

THEMES = ["classic", "ocean", "forest", "sunset", "violet", "mono"]

# Reading-room fonts. The stack is sent to React as a CSS value, so the same
# preference works for every signed-in device without downloading a webfont.
READER_FONTS = {
    "serif": {
        "name": "Newspaper serif",
        "stack": 'Georgia, "Times New Roman", "Noto Serif", serif',
    },
    "sans": {
        "name": "Clean sans",
        "stack": 'Inter, "Segoe UI", system-ui, -apple-system, "Noto Sans", sans-serif',
    },
    "georgia": {"name": "Georgia", "stack": 'Georgia, "Times New Roman", serif'},
    "times": {"name": "Times", "stack": '"Times New Roman", Times, serif'},
    "verdana": {"name": "Verdana", "stack": 'Verdana, Geneva, "DejaVu Sans", sans-serif'},
    "arabic": {
        "name": "Arabic / Urdu",
        "stack": '"Noto Naskh Arabic", Amiri, "Traditional Arabic", Georgia, serif',
    },
}

# -------------------------------------------------------------- languages
STRINGS = {
    "en": {
        "app_name": "Feedline",
        "greeting_morning": "Good morning",
        "greeting_afternoon": "Good afternoon",
        "greeting_evening": "Good evening",
        "greeting_night": "Good night",
        "login": "Log in", "signup": "Sign up", "logout": "Sign out",
        "profile": "Profile", "settings": "Settings", "saved": "Saved",
        "login_title": "Welcome back",
        "login_sub": "Log in to see the news that matters to you.",
        "signup_title": "Create your account",
        "signup_sub": "Get the headlines, plus more news from your country.",
        "username": "Username", "password": "Password",
        "confirm_password": "Confirm password",
        "current_password": "Current password", "new_password": "New password",
        "country": "Country", "language": "Language",
        "theme": "Card colour", "layout": "Layout",
        "reader_font": "Reading room font",
        "save_preferences": "Save preferences",
        "change_password": "Change password",
        "remember": "Keep me signed in on this device",
        "remember_hint": "You won't need to log in again on this device for 6 months.",
        "show": "Show", "hide": "Hide",
        "no_account": "New here?", "have_account": "Already have an account?",
        "create_account": "Create account", "sign_in": "Log in",
        "email": "Email (optional)",
        "email_hint": "Only needed if you forget your password.",
        "forgot_password": "Forgot password?",
        "forgot_title": "Reset your account",
        "forgot_sub": "Enter the email on your account and we will send a 6-digit code.",
        "send_code": "Send code",
        "otp_title": "Check your email",
        "otp_sub": "We sent a 6-digit code to {email}. It expires in 10 minutes.",
        "otp": "6-digit code",
        "verify_code": "Verify code",
        "resend_code": "Resend code",
        "reset_title": "Choose new details",
        "reset_sub": "Pick a new username and password. You will be signed in automatically.",
        "new_username": "New username",
        "reset_password": "Reset and sign in",
        "reset_support": "No email on your account?",
        "or": "or",
        "back_login": "Back to log in",
        "search": "Search", "search_placeholder": "Search news…", "clear": "Clear",
        "all_topics": "All topics", "local": "Local", "also": "Also reported by",
        "read": "Read", "save": "Save", "unsave": "Remove from saved",
        "toast_saved": "Saved to your stories", "toast_unsaved": "Removed from saved",
        "reader_back": "Back", "original": "Open original",
        "read_blocked": "This site only shares a preview. You can read the rest on the original page.",
        "read_partial": "Part of this story is shown. Open the original for the full article.",
        "reading_time": "{n} min read", "in_app": "Read here",
        "font_smaller": "Smaller text", "font_larger": "Larger text",
        "refresh": "Refresh", "refreshing": "Refreshing…",
        "loading_stories": "Loading stories…",
        "updated": "updated {time}",
        "articles_past": "{n} stories in the past {range}",
        "results_for": "Results for “{q}”",
        "today": "Today", "yesterday": "Yesterday",
        "empty_title": "No stories here yet",
        "empty_sub": "Try a longer time range, another topic, or press Refresh.",
        "search_empty_title": "No matches for “{q}”",
        "search_empty_sub": "Try different words or a longer time range.",
        "search_wide": "No matches in {range} — showing results from the last 30 days.",
        "search_web": "These live results come from a web news search.",
        "web_results": "web search",
        "search_fail": "Web search is unavailable right now. Check the spelling or try again in a moment.",
        "saved_title": "Saved stories",
        "saved_sub": "{n} stories saved to your account",
        "saved_empty": "Nothing saved yet. Tap the bookmark on any card.",
        "welcome_title": "Choose your language",
        "welcome_sub": "Pick the language you want to read the news in. You can change it later in Settings.",
        "continue": "Continue",
        "profile_title": "Profile & settings",
        "profile_sub": "Your account, your country, and how the app looks.",
        "account": "Account", "security": "Password",
        "preferences": "News & language", "appearance": "Appearance",
        "update_username": "Update username", "update_password": "Update password",
        "save_changes": "Save changes",
        "member_since": "Member since {date}", "saved_count": "{n} saved",
        "password_hint": "At least 8 characters.",
        "password_changed": "Password updated.",
        "username_changed": "Username updated.",
        "prefs_changed": "Preferences saved.",
        "err_required": "Please fill in every field.",
        "err_username": "Username must be 2–32 characters.",
        "err_password": "Password must be at least 8 characters.",
        "err_match": "Passwords do not match.",
        "err_exists": "That username is already taken.",
        "err_credentials": "Wrong username or password.",
        "err_current": "Current password is incorrect.",
        "err_same": "New password must be different.",
        "err_rate": "Too many attempts. Try again in a few minutes.",
        "err_email": "Enter a valid email address.",
        "err_email_not_found": "No account uses that email yet. Add it in Profile, or contact support.",
        "err_email_taken": "That email is already used by another account.",
        "err_email_send": "We could not send the email right now. Check the mail settings or try again later.",
        "err_otp": "Enter the 6-digit code we sent.",
        "err_otp_attempts": "That code is not right. {n} attempts left.",
        "err_reset_expired": "This reset code has expired. Please request a new one.",
        "err_no_reset": "Start the reset again from the beginning.",
        "err_generic": "Something went wrong.",
        "range_24h": "24 hours", "range_48h": "48 hours",
        "range_7d": "7 days", "range_14d": "14 days",
        "min_ago": "{n} min ago", "hour_ago": "{n} h ago",
        "day_ago": "{n} d ago", "just_now": "just now",
        "loading_news": "Fetching the latest stories… this page will refresh automatically.",
        "theme_classic": "Classic", "theme_ocean": "Ocean",
        "theme_forest": "Forest", "theme_sunset": "Sunset",
        "theme_violet": "Violet", "theme_mono": "Mono",
        "grid": "Grid", "list": "Compact",
        "account_created": "Welcome, {name}!",
        "local_first": "Local stories are shown first",
    },
    "ar": {
        "app_name": "Feedline",
        "greeting_morning": "صباح الخير",
        "greeting_afternoon": "مساء الخير",
        "greeting_evening": "مساء الخير",
        "greeting_night": "تصبح على خير",
        "login": "تسجيل الدخول", "signup": "إنشاء حساب", "logout": "تسجيل الخروج",
        "profile": "الملف الشخصي", "settings": "الإعدادات", "saved": "المحفوظات",
        "login_title": "مرحبًا بعودتك",
        "login_sub": "سجّل الدخول لترى الأخبار التي تهمك.",
        "signup_title": "أنشئ حسابك",
        "signup_sub": "احصل على العناوين، والمزيد من أخبار بلدك.",
        "username": "اسم المستخدم", "password": "كلمة المرور",
        "confirm_password": "تأكيد كلمة المرور",
        "current_password": "كلمة المرور الحالية", "new_password": "كلمة المرور الجديدة",
        "country": "البلد", "language": "اللغة",
        "theme": "لون البطاقات", "layout": "التنسيق",
        "remember": "أبقني مسجّلًا على هذا الجهاز",
        "remember_hint": "لن تحتاج إلى تسجيل الدخول مرة أخرى على هذا الجهاز لمدة ٦ أشهر.",
        "show": "إظهار", "hide": "إخفاء",
        "no_account": "جديد هنا؟", "have_account": "لديك حساب بالفعل؟",
        "create_account": "إنشاء حساب", "sign_in": "تسجيل الدخول",
        "search": "بحث", "search_placeholder": "ابحث في الأخبار…", "clear": "مسح",
        "all_topics": "كل المواضيع", "local": "محلي", "also": "نُشر أيضًا في",
        "read": "اقرأ", "save": "حفظ", "unsave": "إزالة من المحفوظات",
        "reader_back": "رجوع", "original": "فتح الأصل",
        "read_blocked": "هذا الموقع يعرض مقتطفًا فقط. يمكنك قراءة البقية في الصفحة الأصلية.",
        "read_partial": "تم عرض جزء من هذا الخبر. افتح الأصل لقراءة المقال كاملًا.",
        "reading_time": "{n} دقيقة قراءة", "in_app": "اقرأ هنا",
        "font_smaller": "نص أصغر", "font_larger": "نص أكبر",
        "toast_saved": "تم الحفظ في قصصك", "toast_unsaved": "أُزيل من المحفوظات",
        "refresh": "تحديث", "refreshing": "جارٍ التحديث…",
        "updated": "حُدّث {time}",
        "articles_past": "{n} خبرًا خلال {range}",
        "results_for": "نتائج «{q}»",
        "today": "اليوم", "yesterday": "أمس",
        "empty_title": "لا توجد أخبار بعد",
        "empty_sub": "جرّب نطاقًا زمنيًا أطول أو موضوعًا آخر، أو اضغط تحديث.",
        "search_empty_title": "لا نتائج لـ «{q}»",
        "search_empty_sub": "جرّب كلمات أخرى أو نطاقًا زمنيًا أطول.",
        "search_wide": "لا نتائج في {range} — نعرض نتائج آخر ٣٠ يومًا.",
        "search_web": "هذه النتائج المباشرة من بحث إخباري على الويب.",
        "web_results": "بحث الويب",
        "search_fail": "البحث على الويب غير متاح الآن. تحقق من الإملاء أو حاول بعد قليل.",
        "saved_title": "الأخبار المحفوظة",
        "saved_sub": "{n} خبرًا محفوظًا في حسابك",
        "saved_empty": "لا شيء محفوظ بعد. اضغط على أيقونة الحفظ في أي بطاقة.",
        "welcome_title": "اختر لغتك",
        "welcome_sub": "اختر اللغة التي تريد قراءة الأخبار بها. يمكنك تغييرها لاحقًا من الإعدادات.",
        "continue": "متابعة",
        "profile_title": "الملف الشخصي والإعدادات",
        "profile_sub": "حسابك وبلدك ومظهر التطبيق.",
        "account": "الحساب", "security": "كلمة المرور",
        "preferences": "الأخبار واللغة", "appearance": "المظهر",
        "update_username": "تحديث اسم المستخدم", "update_password": "تحديث كلمة المرور",
        "save_changes": "حفظ التغييرات",
        "member_since": "عضو منذ {date}", "saved_count": "{n} محفوظ",
        "password_hint": "٨ أحرف على الأقل.",
        "password_changed": "تم تحديث كلمة المرور.",
        "username_changed": "تم تحديث اسم المستخدم.",
        "prefs_changed": "تم حفظ التفضيلات.",
        "err_required": "يرجى ملء جميع الحقول.",
        "err_username": "يجب أن يكون اسم المستخدم بين ٢ و٣٢ حرفًا.",
        "err_password": "يجب أن تكون كلمة المرور ٨ أحرف على الأقل.",
        "err_match": "كلمتا المرور غير متطابقتين.",
        "err_exists": "اسم المستخدم مستخدم بالفعل.",
        "err_credentials": "اسم المستخدم أو كلمة المرور غير صحيحة.",
        "err_current": "كلمة المرور الحالية غير صحيحة.",
        "err_same": "يجب أن تكون كلمة المرور الجديدة مختلفة.",
        "err_rate": "محاولات كثيرة. حاول بعد بضع دقائق.",
        "err_generic": "حدث خطأ ما.",
        "range_24h": "٢٤ ساعة", "range_48h": "٤٨ ساعة",
        "range_7d": "٧ أيام", "range_14d": "١٤ يومًا",
        "min_ago": "منذ {n} دقيقة", "hour_ago": "منذ {n} ساعة",
        "day_ago": "منذ {n} يوم", "just_now": "الآن",
        "loading_news": "جارٍ إحضار أحدث الأخبار… ستُحدَّث الصفحة تلقائيًا.",
        "theme_classic": "كلاسيكي", "theme_ocean": "محيط",
        "theme_forest": "غابة", "theme_sunset": "غروب",
        "theme_violet": "بنفسجي", "theme_mono": "أحادي",
        "grid": "شبكة", "list": "مدمج",
        "account_created": "مرحبًا، {name}!",
        "local_first": "تظهر الأخبار المحلية أولًا",
    },
    "fr": {
        "app_name": "Feedline",
        "greeting_morning": "Bonjour", "greeting_afternoon": "Bon après-midi",
        "greeting_evening": "Bonsoir", "greeting_night": "Bonne nuit",
        "login": "Se connecter", "signup": "S'inscrire", "logout": "Se déconnecter",
        "profile": "Profil", "settings": "Paramètres", "saved": "Enregistrés",
        "login_title": "Bon retour",
        "login_sub": "Connectez-vous pour voir les nouvelles qui vous concernent.",
        "signup_title": "Créez votre compte",
        "signup_sub": "Recevez les gros titres et plus d'actualités de votre pays.",
        "username": "Nom d'utilisateur", "password": "Mot de passe",
        "confirm_password": "Confirmer le mot de passe",
        "current_password": "Mot de passe actuel", "new_password": "Nouveau mot de passe",
        "country": "Pays", "language": "Langue",
        "theme": "Couleur des cartes", "layout": "Affichage",
        "remember": "Rester connecté sur cet appareil",
        "remember_hint": "Vous n'aurez pas à vous reconnecter sur cet appareil pendant 6 mois.",
        "show": "Afficher", "hide": "Masquer",
        "no_account": "Nouveau ici ?", "have_account": "Vous avez déjà un compte ?",
        "create_account": "Créer un compte", "sign_in": "Se connecter",
        "search": "Rechercher", "search_placeholder": "Rechercher dans les actualités…",
        "clear": "Effacer",
        "all_topics": "Tous les sujets", "local": "Local", "also": "Aussi rapporté par",
        "read": "Lire", "save": "Enregistrer", "unsave": "Retirer des enregistrés",
        "reader_back": "Retour", "original": "Ouvrir l’original",
        "read_blocked": "Ce site ne partage qu’un aperçu. Lisez la suite sur la page d’origine.",
        "read_partial": "Une partie de cet article est affichée. Ouvrez l’original pour lire la suite.",
        "reading_time": "{n} min de lecture", "in_app": "Lire ici",
        "font_smaller": "Texte plus petit", "font_larger": "Texte plus grand",
        "toast_saved": "Enregistré dans vos histoires", "toast_unsaved": "Retiré des enregistrements",
        "refresh": "Actualiser", "refreshing": "Actualisation…",
        "updated": "mis à jour à {time}",
        "articles_past": "{n} articles ces dernières {range}",
        "results_for": "Résultats pour « {q} »",
        "today": "Aujourd'hui", "yesterday": "Hier",
        "empty_title": "Pas encore d'articles",
        "empty_sub": "Essayez une période plus longue, un autre sujet, ou actualisez.",
        "search_empty_title": "Aucun résultat pour « {q} »",
        "search_empty_sub": "Essayez d'autres mots ou une période plus longue.",
        "search_wide": "Aucun résultat sur {range} — affichage des 30 derniers jours.",
        "search_web": "Ces résultats en direct viennent d'une recherche d'actualités sur le web.",
        "web_results": "recherche web",
        "search_fail": "La recherche web est indisponible. Vérifiez l'orthographe ou réessayez.",
        "saved_title": "Articles enregistrés",
        "saved_sub": "{n} articles enregistrés sur votre compte",
        "saved_empty": "Rien d'enregistré. Touchez le signet sur une carte.",
        "welcome_title": "Choisissez votre langue",
        "welcome_sub": "Choisissez la langue des actualités. Vous pourrez la changer plus tard dans les paramètres.",
        "continue": "Continuer",
        "profile_title": "Profil et paramètres",
        "profile_sub": "Votre compte, votre pays et l'apparence de l'application.",
        "account": "Compte", "security": "Mot de passe",
        "preferences": "Actualités et langue", "appearance": "Apparence",
        "update_username": "Mettre à jour le nom",
        "update_password": "Mettre à jour le mot de passe",
        "save_changes": "Enregistrer",
        "member_since": "Membre depuis {date}", "saved_count": "{n} enregistrés",
        "password_hint": "8 caractères minimum.",
        "password_changed": "Mot de passe mis à jour.",
        "username_changed": "Nom d'utilisateur mis à jour.",
        "prefs_changed": "Préférences enregistrées.",
        "err_required": "Veuillez remplir tous les champs.",
        "err_username": "Le nom doit contenir 2 à 32 caractères.",
        "err_password": "Le mot de passe doit contenir au moins 8 caractères.",
        "err_match": "Les mots de passe ne correspondent pas.",
        "err_exists": "Ce nom est déjà pris.",
        "err_credentials": "Nom d'utilisateur ou mot de passe incorrect.",
        "err_current": "Mot de passe actuel incorrect.",
        "err_same": "Le nouveau mot de passe doit être différent.",
        "err_rate": "Trop de tentatives. Réessayez dans quelques minutes.",
        "err_generic": "Une erreur est survenue.",
        "range_24h": "24 heures", "range_48h": "48 heures",
        "range_7d": "7 jours", "range_14d": "14 jours",
        "min_ago": "il y a {n} min", "hour_ago": "il y a {n} h",
        "day_ago": "il y a {n} j", "just_now": "à l'instant",
        "loading_news": "Récupération des derniers articles… la page va s'actualiser.",
        "theme_classic": "Classique", "theme_ocean": "Océan",
        "theme_forest": "Forêt", "theme_sunset": "Coucher de soleil",
        "theme_violet": "Violet", "theme_mono": "Mono",
        "grid": "Grille", "list": "Compact",
        "account_created": "Bienvenue, {name} !",
        "local_first": "Les actualités locales apparaissent en premier",
    },
    "es": {
        "app_name": "Feedline",
        "greeting_morning": "Buenos días", "greeting_afternoon": "Buenas tardes",
        "greeting_evening": "Buenas noches", "greeting_night": "Buenas noches",
        "login": "Iniciar sesión", "signup": "Registrarse", "logout": "Cerrar sesión",
        "profile": "Perfil", "settings": "Ajustes", "saved": "Guardadas",
        "login_title": "Bienvenido de nuevo",
        "login_sub": "Inicia sesión para ver las noticias que te importan.",
        "signup_title": "Crea tu cuenta",
        "signup_sub": "Recibe los titulares y más noticias de tu país.",
        "username": "Nombre de usuario", "password": "Contraseña",
        "confirm_password": "Confirmar contraseña",
        "current_password": "Contraseña actual", "new_password": "Nueva contraseña",
        "country": "País", "language": "Idioma",
        "theme": "Color de las tarjetas", "layout": "Diseño",
        "remember": "Mantener mi sesión en este dispositivo",
        "remember_hint": "No tendrás que volver a iniciar sesión en este dispositivo durante 6 meses.",
        "show": "Mostrar", "hide": "Ocultar",
        "no_account": "¿Eres nuevo?", "have_account": "¿Ya tienes una cuenta?",
        "create_account": "Crear cuenta", "sign_in": "Iniciar sesión",
        "search": "Buscar", "search_placeholder": "Buscar noticias…",
        "clear": "Borrar", "all_topics": "Todos los temas", "local": "Local",
        "also": "También informaron", "read": "Leer", "save": "Guardar",
        "unsave": "Quitar de guardadas",
        "reader_back": "Volver", "original": "Abrir original",
        "read_blocked": "Este sitio solo comparte un avance. Puedes leer el resto en la página original.",
        "read_partial": "Se muestra parte de esta noticia. Abre el original para leer el artículo completo.",
        "reading_time": "{n} min de lectura", "in_app": "Leer aquí",
        "font_smaller": "Texto más pequeño", "font_larger": "Texto más grande",
        "toast_saved": "Guardado en tus historias", "toast_unsaved": "Eliminado de guardados",
        "refresh": "Actualizar", "refreshing": "Actualizando…",
        "updated": "actualizado a las {time}",
        "articles_past": "{n} noticias en las últimas {range}",
        "results_for": "Resultados para «{q}»",
        "today": "Hoy", "yesterday": "Ayer",
        "empty_title": "Aún no hay noticias",
        "empty_sub": "Prueba un periodo más largo, otro tema, o pulsa Actualizar.",
        "search_empty_title": "Sin resultados para «{q}»",
        "search_empty_sub": "Prueba otras palabras o un periodo más largo.",
        "search_wide": "Sin resultados en {range}: mostrando los últimos 30 días.",
        "search_web": "Estos resultados en vivo vienen de una búsqueda de noticias en la web.",
        "web_results": "búsqueda web",
        "search_fail": "La búsqueda web no está disponible. Revisa la ortografía o inténtalo de nuevo.",
        "saved_title": "Noticias guardadas",
        "saved_sub": "{n} noticias guardadas en tu cuenta",
        "saved_empty": "Nada guardado aún. Toca el marcador en cualquier tarjeta.",
        "welcome_title": "Elige tu idioma",
        "welcome_sub": "Elige el idioma para leer las noticias. Puedes cambiarlo después en Ajustes.",
        "continue": "Continuar",
        "profile_title": "Perfil y ajustes",
        "profile_sub": "Tu cuenta, tu país y el aspecto de la aplicación.",
        "account": "Cuenta", "security": "Contraseña",
        "preferences": "Noticias e idioma", "appearance": "Apariencia",
        "update_username": "Actualizar nombre", "update_password": "Actualizar contraseña",
        "save_changes": "Guardar cambios",
        "member_since": "Miembro desde {date}", "saved_count": "{n} guardadas",
        "password_hint": "Al menos 8 caracteres.",
        "password_changed": "Contraseña actualizada.",
        "username_changed": "Nombre de usuario actualizado.",
        "prefs_changed": "Preferencias guardadas.",
        "err_required": "Rellena todos los campos.",
        "err_username": "El nombre debe tener entre 2 y 32 caracteres.",
        "err_password": "La contraseña debe tener al menos 8 caracteres.",
        "err_match": "Las contraseñas no coinciden.",
        "err_exists": "Ese nombre de usuario ya está en uso.",
        "err_credentials": "Usuario o contraseña incorrectos.",
        "err_current": "La contraseña actual es incorrecta.",
        "err_same": "La nueva contraseña debe ser diferente.",
        "err_rate": "Demasiados intentos. Inténtalo de nuevo en unos minutos.",
        "err_generic": "Algo salió mal.",
        "range_24h": "24 horas", "range_48h": "48 horas",
        "range_7d": "7 días", "range_14d": "14 días",
        "min_ago": "hace {n} min", "hour_ago": "hace {n} h",
        "day_ago": "hace {n} d", "just_now": "ahora mismo",
        "loading_news": "Buscando las últimas noticias… la página se actualizará sola.",
        "theme_classic": "Clásico", "theme_ocean": "Océano",
        "theme_forest": "Bosque", "theme_sunset": "Atardecer",
        "theme_violet": "Violeta", "theme_mono": "Mono",
        "grid": "Cuadrícula", "list": "Compacto",
        "account_created": "¡Bienvenido, {name}!",
        "local_first": "Las noticias locales aparecen primero",
    },
    "pt": {
        "app_name": "Feedline",
        "greeting_morning": "Bom dia", "greeting_afternoon": "Boa tarde",
        "greeting_evening": "Boa noite", "greeting_night": "Boa noite",
        "login": "Entrar", "signup": "Criar conta", "logout": "Sair",
        "profile": "Perfil", "settings": "Definições", "saved": "Guardadas",
        "login_title": "Bem-vindo de volta",
        "login_sub": "Entre para ver as notícias que importam para você.",
        "signup_title": "Crie a sua conta",
        "signup_sub": "Receba as manchetes e mais notícias do seu país.",
        "username": "Nome de utilizador", "password": "Palavra-passe",
        "confirm_password": "Confirmar palavra-passe",
        "current_password": "Palavra-passe atual", "new_password": "Nova palavra-passe",
        "country": "País", "language": "Idioma",
        "theme": "Cor dos cartões", "layout": "Disposição",
        "remember": "Manter sessão iniciada neste dispositivo",
        "remember_hint": "Não precisará de entrar novamente neste dispositivo durante 6 meses.",
        "show": "Mostrar", "hide": "Ocultar",
        "no_account": "É novo aqui?", "have_account": "Já tem uma conta?",
        "create_account": "Criar conta", "sign_in": "Entrar",
        "search": "Pesquisar", "search_placeholder": "Pesquisar notícias…",
        "clear": "Limpar", "all_topics": "Todos os temas", "local": "Local",
        "also": "Também noticiado por", "read": "Ler", "save": "Guardar",
        "unsave": "Remover das guardadas",
        "reader_back": "Voltar", "original": "Abrir original",
        "read_blocked": "Este site mostra apenas uma prévia. Leia o resto na página original.",
        "read_partial": "Parte desta notícia é exibida. Abra o original para ler o artigo completo.",
        "reading_time": "{n} min de leitura", "in_app": "Ler aqui",
        "font_smaller": "Texto menor", "font_larger": "Texto maior",
        "toast_saved": "Guardado nas suas histórias", "toast_unsaved": "Removido das guardadas",
        "refresh": "Atualizar", "refreshing": "A atualizar…",
        "updated": "atualizado às {time}",
        "articles_past": "{n} notícias nas últimas {range}",
        "results_for": "Resultados para «{q}»",
        "today": "Hoje", "yesterday": "Ontem",
        "empty_title": "Ainda não há notícias",
        "empty_sub": "Tente um período maior, outro tema, ou atualize.",
        "search_empty_title": "Sem resultados para «{q}»",
        "search_empty_sub": "Tente outras palavras ou um período maior.",
        "search_wide": "Sem resultados em {range} — a mostrar os últimos 30 dias.",
        "search_web": "Estes resultados ao vivo vêm de uma pesquisa de notícias na web.",
        "web_results": "pesquisa web",
        "search_fail": "A pesquisa na web não está disponível. Verifique a ortografia ou tente novamente.",
        "saved_title": "Notícias guardadas",
        "saved_sub": "{n} notícias guardadas na sua conta",
        "saved_empty": "Nada guardado ainda. Toque no marcador de qualquer cartão.",
        "welcome_title": "Escolha o seu idioma",
        "welcome_sub": "Escolha o idioma para ler as notícias. Pode alterá-lo depois nas definições.",
        "continue": "Continuar",
        "profile_title": "Perfil e definições",
        "profile_sub": "A sua conta, o seu país e o aspeto da aplicação.",
        "account": "Conta", "security": "Palavra-passe",
        "preferences": "Notícias e idioma", "appearance": "Aparência",
        "update_username": "Atualizar nome", "update_password": "Atualizar palavra-passe",
        "save_changes": "Guardar alterações",
        "member_since": "Membro desde {date}", "saved_count": "{n} guardadas",
        "password_hint": "Pelo menos 8 caracteres.",
        "password_changed": "Palavra-passe atualizada.",
        "username_changed": "Nome de utilizador atualizado.",
        "prefs_changed": "Preferências guardadas.",
        "err_required": "Preencha todos os campos.",
        "err_username": "O nome deve ter entre 2 e 32 caracteres.",
        "err_password": "A palavra-passe deve ter pelo menos 8 caracteres.",
        "err_match": "As palavras-passe não coincidem.",
        "err_exists": "Esse nome de utilizador já existe.",
        "err_credentials": "Utilizador ou palavra-passe incorretos.",
        "err_current": "A palavra-passe atual está incorreta.",
        "err_same": "A nova palavra-passe deve ser diferente.",
        "err_rate": "Demasiadas tentativas. Tente novamente dentro de alguns minutos.",
        "err_generic": "Algo correu mal.",
        "range_24h": "24 horas", "range_48h": "48 horas",
        "range_7d": "7 dias", "range_14d": "14 dias",
        "min_ago": "há {n} min", "hour_ago": "há {n} h",
        "day_ago": "há {n} d", "just_now": "agora mesmo",
        "loading_news": "A obter as últimas notícias… a página vai atualizar-se.",
        "theme_classic": "Clássico", "theme_ocean": "Oceano",
        "theme_forest": "Floresta", "theme_sunset": "Pôr do sol",
        "theme_violet": "Violeta", "theme_mono": "Mono",
        "grid": "Grelha", "list": "Compacto",
        "account_created": "Bem-vindo, {name}!",
        "local_first": "As notícias locais aparecem primeiro",
    },
    "sw": {
        "app_name": "Feedline",
        "greeting_morning": "Habari za asubuhi", "greeting_afternoon": "Habari za mchana",
        "greeting_evening": "Habari za jioni", "greeting_night": "Usiku mwema",
        "login": "Ingia", "signup": "Jisajili", "logout": "Toka",
        "profile": "Wasifu", "settings": "Mipangilio", "saved": "Zilizohifadhiwa",
        "login_title": "Karibu tena",
        "login_sub": "Ingia ili kuona habari zinazokuhusu.",
        "signup_title": "Tengeneza akaunti yako",
        "signup_sub": "Pata vichwa vya habari na habari zaidi kutoka nchini kwako.",
        "username": "Jina la mtumiaji", "password": "Nenosiri",
        "confirm_password": "Thibitisha nenosiri",
        "current_password": "Nenosiri la sasa", "new_password": "Nenosiri jipya",
        "country": "Nchi", "language": "Lugha",
        "theme": "Rangi ya kadi", "layout": "Muundo",
        "remember": "Nibakishe nimeingia kwenye kifaa hiki",
        "remember_hint": "Hutahitaji kuingia tena kwenye kifaa hiki kwa miezi 6.",
        "show": "Onyesha", "hide": "Ficha",
        "no_account": "Mgeni hapa?", "have_account": "Tayari una akaunti?",
        "create_account": "Tengeneza akaunti", "sign_in": "Ingia",
        "search": "Tafuta", "search_placeholder": "Tafuta habari…", "clear": "Futa",
        "all_topics": "Mada zote", "local": "Za nchini", "also": "Pia iliripotiwa na",
        "read": "Soma", "save": "Hifadhi", "unsave": "Ondoa kwenye zilizohifadhiwa",
        "reader_back": "Rudi", "original": "Fungua asili",
        "read_blocked": "Tovuti hii inaonyesha muhtasari tu. Unaweza kusoma zaidi kwenye ukurasa asili.",
        "read_partial": "Sehemu ya habari hii imeonyeshwa. Fungua asili kwa makala kamili.",
        "reading_time": "Dakika {n} za kusoma", "in_app": "Soma hapa",
        "font_smaller": "Maandishi madogo", "font_larger": "Maandishi makubwa",
        "toast_saved": "Imehifadhiwa kwenye habari zako", "toast_unsaved": "Imeondolewa kwenye zilizohifadhiwa",
        "refresh": "Sasisha", "refreshing": "Inasasisha…",
        "updated": "imesasishwa {time}",
        "articles_past": "habari {n} katika {range} zilizopita",
        "results_for": "Matokeo ya «{q}»",
        "today": "Leo", "yesterday": "Jana",
        "empty_title": "Bado hakuna habari",
        "empty_sub": "Jaribu muda mrefu zaidi, mada nyingine, au bofya Sasisha.",
        "search_empty_title": "Hakuna matokeo ya «{q}»",
        "search_empty_sub": "Jaribu maneno mengine au muda mrefu zaidi.",
        "search_wide": "Hakuna matokeo katika {range} — tunaonyesha siku 30 zilizopita.",
        "search_web": "Matokeo haya ya moja kwa moja yanatoka kwenye utafutaji wa habari mtandaoni.",
        "web_results": "utafutaji wa wavuti",
        "search_fail": "Utafutaji wa wavuti haupatikani sasa. Angalia tahajia au jaribu tena.",
        "saved_title": "Habari zilizohifadhiwa",
        "saved_sub": "habari {n} zimehifadhiwa kwenye akaunti yako",
        "saved_empty": "Bado hakuna kitu kilichohifadhiwa. Gusa alama ya kuhifadhi kwenye kadi yoyote.",
        "welcome_title": "Chagua lugha yako",
        "welcome_sub": "Chagua lugha unayotaka kusoma habari. Unaweza kuibadilisha baadaye kwenye Mipangilio.",
        "continue": "Endelea",
        "profile_title": "Wasifu na mipangilio",
        "profile_sub": "Akaunti yako, nchi yako, na mwonekano wa programu.",
        "account": "Akaunti", "security": "Nenosiri",
        "preferences": "Habari na lugha", "appearance": "Muonekano",
        "update_username": "Sasisha jina la mtumiaji",
        "update_password": "Sasisha nenosiri",
        "save_changes": "Hifadhi mabadiliko",
        "member_since": "Mwanachama tangu {date}", "saved_count": "{n} zimehifadhiwa",
        "password_hint": "Angalau herufi 8.",
        "password_changed": "Nenosiri limesasishwa.",
        "username_changed": "Jina la mtumiaji limesasishwa.",
        "prefs_changed": "Mipangilio imehifadhiwa.",
        "err_required": "Tafadhali jaza sehemu zote.",
        "err_username": "Jina la mtumiaji liwe herufi 2–32.",
        "err_password": "Nenosiri liwe angalau herufi 8.",
        "err_match": "Manenosiri hayafanani.",
        "err_exists": "Jina hilo la mtumiaji tayari linatumika.",
        "err_credentials": "Jina la mtumiaji au nenosiri si sahihi.",
        "err_current": "Nenosiri la sasa si sahihi.",
        "err_same": "Nenosiri jipya liwe tofauti.",
        "err_rate": "Majaribio mengi. Jaribu tena baada ya dakika chache.",
        "err_generic": "Kuna hitilafu.",
        "range_24h": "saa 24", "range_48h": "saa 48",
        "range_7d": "siku 7", "range_14d": "siku 14",
        "min_ago": "dakika {n} zilizopita", "hour_ago": "saa {n} zilizopita",
        "day_ago": "siku {n} zilizopita", "just_now": "sasa hivi",
        "loading_news": "Inapakua habari za hivi punde… ukurasa utasasishwa.",
        "theme_classic": "Kawaida", "theme_ocean": "Bahari",
        "theme_forest": "Msitu", "theme_sunset": "Machweo",
        "theme_violet": "Zambarau", "theme_mono": "Mono",
        "grid": "Gridi", "list": "Fupi",
        "account_created": "Karibu, {name}!",
        "local_first": "Habari za nchini zinaonyeshwa kwanza",
    },
    "ur": {
        "app_name": "Feedline",
        "greeting_morning": "صبح بخیر", "greeting_afternoon": "دوپہر بخیر",
        "greeting_evening": "شام بخیر", "greeting_night": "شب بخیر",
        "login": "لاگ ان", "signup": "سائن اپ", "logout": "لاگ آؤٹ",
        "profile": "پروفائل", "settings": "ترتیبات", "saved": "محفوظ",
        "login_title": "خوش آمدید",
        "login_sub": "اپنی پسند کی خبریں دیکھنے کے لیے لاگ ان کریں۔",
        "signup_title": "اپنا اکاؤنٹ بنائیں",
        "signup_sub": "سرخیاں اور اپنے ملک کی مزید خبریں حاصل کریں۔",
        "username": "صارف نام", "password": "پاس ورڈ",
        "confirm_password": "پاس ورڈ کی تصدیق",
        "current_password": "موجودہ پاس ورڈ", "new_password": "نیا پاس ورڈ",
        "country": "ملک", "language": "زبان",
        "theme": "کارڈ کا رنگ", "layout": "ترتیب",
        "remember": "اس ڈیوائس پر مجھے لاگ ان رکھیں",
        "remember_hint": "آپ کو اس ڈیوائس پر 6 ماہ تک دوبارہ لاگ ان نہیں کرنا پڑے گا۔",
        "show": "دکھائیں", "hide": "چھپائیں",
        "no_account": "نئے ہیں؟", "have_account": "پہلے سے اکاؤنٹ ہے؟",
        "create_account": "اکاؤنٹ بنائیں", "sign_in": "لاگ ان",
        "search": "تلاش", "search_placeholder": "خبریں تلاش کریں…", "clear": "صاف کریں",
        "all_topics": "تمام موضوعات", "local": "مقامی", "also": "یہ بھی رپورٹ کیا",
        "read": "پڑھیں", "save": "محفوظ کریں", "unsave": "محفوظ سے ہٹائیں",
        "reader_back": "واپس", "original": "اصل کھولیں",
        "read_blocked": "یہ سائٹ صرف ایک جھلک دکھاتی ہے۔ باقی مضمون اصل صفحے پر پڑھیں۔",
        "read_partial": "اس خبر کا کچھ حصہ دکھایا گیا ہے۔ مکمل مضمون کے لیے اصل کھولیں۔",
        "reading_time": "{n} منٹ کا مطالعہ", "in_app": "یہاں پڑھیں",
        "font_smaller": "چھوٹا متن", "font_larger": "بڑا متن",
        "toast_saved": "آپ کی کہانیوں میں محفوظ ہو گیا", "toast_unsaved": "محفوظات سے ہٹا دیا گیا",
        "refresh": "تازہ کریں", "refreshing": "تازہ ہو رہا ہے…",
        "updated": "{time} کو تازہ کیا گیا",
        "articles_past": "گزشتہ {range} میں {n} خبریں",
        "results_for": "«{q}» کے نتائج",
        "today": "آج", "yesterday": "کل",
        "empty_title": "ابھی کوئی خبر نہیں",
        "empty_sub": "لمبا دورانیہ، دوسرا موضوع آزمائیں یا تازہ کریں۔",
        "search_empty_title": "«{q}» کے لیے کوئی نتیجہ نہیں",
        "search_empty_sub": "دوسرے الفاظ یا لمبا دورانیہ آزمائیں۔",
        "search_wide": "{range} میں کوئی نتیجہ نہیں — گزشتہ 30 دن کے نتائج دکھائے جا رہے ہیں۔",
        "search_web": "یہ براہِ راست نتائج ویب پر خبروں کی تلاش سے ہیں۔",
        "web_results": "ویب تلاش",
        "search_fail": "ویب تلاش ابھی دستیاب نہیں۔ ہجے دیکھیں یا تھوڑی دیر بعد کوشش کریں۔",
        "saved_title": "محفوظ خبریں",
        "saved_sub": "آپ کے اکاؤنٹ میں {n} خبریں محفوظ ہیں",
        "saved_empty": "ابھی کچھ محفوظ نہیں۔ کسی کارڈ پر بک مارک دبائیں۔",
        "welcome_title": "اپنی زبان منتخب کریں",
        "welcome_sub": "خبریں پڑھنے کی زبان منتخب کریں۔ آپ بعد میں ترتیبات میں بدل سکتے ہیں۔",
        "continue": "جاری رکھیں",
        "profile_title": "پروفائل اور ترتیبات",
        "profile_sub": "آپ کا اکاؤنٹ، ملک اور ایپ کی ظاہری شکل۔",
        "account": "اکاؤنٹ", "security": "پاس ورڈ",
        "preferences": "خبریں اور زبان", "appearance": "ظاہری شکل",
        "update_username": "صارف نام اپ ڈیٹ کریں",
        "update_password": "پاس ورڈ اپ ڈیٹ کریں",
        "save_changes": "تبدیلیاں محفوظ کریں",
        "member_since": "{date} سے رکن", "saved_count": "{n} محفوظ",
        "password_hint": "کم از کم 8 حروف۔",
        "password_changed": "پاس ورڈ اپ ڈیٹ ہو گیا۔",
        "username_changed": "صارف نام اپ ڈیٹ ہو گیا۔",
        "prefs_changed": "ترتیبات محفوظ ہو گئیں۔",
        "err_required": "براہ کرم تمام خانے بھریں۔",
        "err_username": "صارف نام 2 سے 32 حروف کا ہونا چاہیے۔",
        "err_password": "پاس ورڈ کم از کم 8 حروف کا ہونا چاہیے۔",
        "err_match": "پاس ورڈ مماثل نہیں ہیں۔",
        "err_exists": "یہ صارف نام پہلے سے موجود ہے۔",
        "err_credentials": "صارف نام یا پاس ورڈ غلط ہے۔",
        "err_current": "موجودہ پاس ورڈ غلط ہے۔",
        "err_same": "نیا پاس ورڈ مختلف ہونا چاہیے۔",
        "err_rate": "بہت زیادہ کوششیں۔ چند منٹ بعد دوبارہ کوشش کریں۔",
        "err_generic": "کچھ غلط ہو گیا۔",
        "range_24h": "24 گھنٹے", "range_48h": "48 گھنٹے",
        "range_7d": "7 دن", "range_14d": "14 دن",
        "min_ago": "{n} منٹ پہلے", "hour_ago": "{n} گھنٹے پہلے",
        "day_ago": "{n} دن پہلے", "just_now": "ابھی",
        "loading_news": "تازہ ترین خبریں لائی جا رہی ہیں… صفحہ خود بہ خود تازہ ہوگا۔",
        "theme_classic": "کلاسک", "theme_ocean": "سمندر",
        "theme_forest": "جنگل", "theme_sunset": "غروب",
        "theme_violet": "بنفشی", "theme_mono": "مونو",
        "grid": "گرڈ", "list": "مختصر",
        "account_created": "خوش آمدید، {name}!",
        "local_first": "مقامی خبریں پہلے دکھائی جاتی ہیں",
    },
    "hi": {
        "app_name": "Feedline",
        "greeting_morning": "सुप्रभात", "greeting_afternoon": "नमस्कार",
        "greeting_evening": "शुभ संध्या", "greeting_night": "शुभ रात्रि",
        "login": "लॉग इन", "signup": "साइन अप", "logout": "लॉग आउट",
        "profile": "प्रोफ़ाइल", "settings": "सेटिंग्स", "saved": "सहेजे गए",
        "login_title": "वापसी पर स्वागत है",
        "login_sub": "अपनी पसंद की ख़बरें देखने के लिए लॉग इन करें।",
        "signup_title": "अपना खाता बनाएँ",
        "signup_sub": "मुख्य ख़बरें और अपने देश की और ख़बरें पाएँ।",
        "username": "उपयोगकर्ता नाम", "password": "पासवर्ड",
        "confirm_password": "पासवर्ड की पुष्टि",
        "current_password": "वर्तमान पासवर्ड", "new_password": "नया पासवर्ड",
        "country": "देश", "language": "भाषा",
        "theme": "कार्ड का रंग", "layout": "लेआउट",
        "remember": "इस डिवाइस पर मुझे लॉग इन रखें",
        "remember_hint": "इस डिवाइस पर 6 महीने तक दोबारा लॉग इन नहीं करना पड़ेगा।",
        "show": "दिखाएँ", "hide": "छिपाएँ",
        "no_account": "नए हैं?", "have_account": "पहले से खाता है?",
        "create_account": "खाता बनाएँ", "sign_in": "लॉग इन",
        "search": "खोजें", "search_placeholder": "ख़बरें खोजें…", "clear": "साफ़ करें",
        "all_topics": "सभी विषय", "local": "स्थानीय", "also": "इन्होंने भी बताया",
        "read": "पढ़ें", "save": "सहेजें", "unsave": "सहेजे गए से हटाएँ",
        "reader_back": "वापस", "original": "मूल खोलें",
        "read_blocked": "यह साइट केवल एक झलक दिखाती है। बाकी मूल पेज पर पढ़ें।",
        "read_partial": "इस खबर का कुछ हिस्सा दिखाया गया है। पूरा लेख मूल पेज पर खोलें।",
        "reading_time": "{n} मिनट का पाठ", "in_app": "यहाँ पढ़ें",
        "font_smaller": "छोटा टेक्स्ट", "font_larger": "बड़ा टेक्स्ट",
        "toast_saved": "आपकी कहानियों में सहेजा गया", "toast_unsaved": "सहेजे गए से हटाया गया",
        "refresh": "रीफ़्रेश", "refreshing": "रीफ़्रेश हो रहा है…",
        "updated": "{time} पर अपडेट",
        "articles_past": "पिछले {range} में {n} ख़बरें",
        "results_for": "«{q}» के परिणाम",
        "today": "आज", "yesterday": "कल",
        "empty_title": "अभी कोई ख़बर नहीं",
        "empty_sub": "लंबी अवधि, दूसरा विषय चुनें या रीफ़्रेश करें।",
        "search_empty_title": "«{q}» के लिए कोई परिणाम नहीं",
        "search_empty_sub": "दूसरे शब्द या लंबी अवधि आज़माएँ।",
        "search_wide": "{range} में कोई परिणाम नहीं — पिछले 30 दिन के परिणाम दिखाए जा रहे हैं।",
        "search_web": "ये ताज़ा परिणाम वेब समाचार खोज से हैं।",
        "web_results": "वेब खोज",
        "search_fail": "वेब खोज अभी उपलब्ध नहीं है। वर्तनी जाँचें या थोड़ी देर बाद कोशिश करें।",
        "saved_title": "सहेजी गई ख़बरें",
        "saved_sub": "आपके खाते में {n} ख़बरें सहेजी गई हैं",
        "saved_empty": "अभी कुछ सहेजा नहीं गया। किसी कार्ड पर बुकमार्क दबाएँ।",
        "welcome_title": "अपनी भाषा चुनें",
        "welcome_sub": "ख़बरें पढ़ने की भाषा चुनें। आप इसे बाद में सेटिंग्स में बदल सकते हैं।",
        "continue": "जारी रखें",
        "profile_title": "प्रोफ़ाइल और सेटिंग्स",
        "profile_sub": "आपका खाता, देश और ऐप का रूप।",
        "account": "खाता", "security": "पासवर्ड",
        "preferences": "ख़बरें और भाषा", "appearance": "रूप",
        "update_username": "उपयोगकर्ता नाम अपडेट करें",
        "update_password": "पासवर्ड अपडेट करें",
        "save_changes": "बदलाव सहेजें",
        "member_since": "{date} से सदस्य", "saved_count": "{n} सहेजे गए",
        "password_hint": "कम से कम 8 अक्षर।",
        "password_changed": "पासवर्ड अपडेट हो गया।",
        "username_changed": "उपयोगकर्ता नाम अपडेट हो गया।",
        "prefs_changed": "सेटिंग्स सहेजी गईं।",
        "err_required": "कृपया सभी फ़ील्ड भरें।",
        "err_username": "उपयोगकर्ता नाम 2–32 अक्षरों का होना चाहिए।",
        "err_password": "पासवर्ड कम से कम 8 अक्षरों का होना चाहिए।",
        "err_match": "पासवर्ड मेल नहीं खाते।",
        "err_exists": "यह उपयोगकर्ता नाम पहले से मौजूद है।",
        "err_credentials": "उपयोगकर्ता नाम या पासवर्ड ग़लत है।",
        "err_current": "वर्तमान पासवर्ड ग़लत है।",
        "err_same": "नया पासवर्ड अलग होना चाहिए।",
        "err_rate": "बहुत अधिक प्रयास। कुछ मिनट बाद फिर कोशिश करें।",
        "err_generic": "कुछ गड़बड़ हो गई।",
        "range_24h": "24 घंटे", "range_48h": "48 घंटे",
        "range_7d": "7 दिन", "range_14d": "14 दिन",
        "min_ago": "{n} मिनट पहले", "hour_ago": "{n} घंटे पहले",
        "day_ago": "{n} दिन पहले", "just_now": "अभी",
        "loading_news": "नई ख़बरें लाई जा रही हैं… पेज अपने आप रीफ़्रेश होगा।",
        "theme_classic": "क्लासिक", "theme_ocean": "समुद्र",
        "theme_forest": "जंगल", "theme_sunset": "सूर्यास्त",
        "theme_violet": "बैंगनी", "theme_mono": "मोनो",
        "grid": "ग्रिड", "list": "संक्षिप्त",
        "account_created": "स्वागत है, {name}!",
        "local_first": "स्थानीय ख़बरें पहले दिखाई जाती हैं",
    },
}

# ------------------------------------------------------------------- app
def load_secret():
    env = os.environ.get("NEWS_SECRET")
    if env:
        return env
    # Local runs use .news_secret. Vercel builds generate deployment_secret.txt
    # once, so every serverless invocation shares the same signing key.
    for name in (".news_secret", "deployment_secret.txt"):
        path = BASE_DIR / name
        try:
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            pass
    value = secrets.token_hex(32)
    try:
        (BASE_DIR / "deployment_secret.txt").write_text(value, encoding="utf-8")
    except OSError:
        pass
    return value


TEMPLATES = {}
app = Flask(__name__)
app.jinja_loader = DictLoader(TEMPLATES)
app.config.update(
    SECRET_KEY=load_secret(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=180),
    MAX_CONTENT_LENGTH=1_000_000,
)

refresh_lock = threading.Lock()
country_locks = defaultdict(threading.Lock)
country_state = {}
state = {"last_refresh": None, "fresh_link": None}
login_fails = {}
login_fails_lock = threading.Lock()
refresh_job = {
    "running": False,
    "started": None,
    "finished": None,
    "changed": 0,
    "error": "",
}
refresh_job_lock = threading.Lock()


# ------------------------------------------------------------- database
write_lock = threading.RLock()   # one writer at a time; WAL readers stay fast


def db():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def commit_retry(conn, attempts=6):
    """Commit even when a background image pass briefly holds the write lock."""
    with write_lock:
        for attempt in range(attempts):
            try:
                conn.commit()
                return True
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == attempts - 1:
                    raise
                time.sleep(0.15 * (attempt + 1))
    return False


def fetchall(sql, args=()):
    conn = db()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def fetchone(sql, args=()):
    conn = db()
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def execute(sql, args=()):
    with write_lock:
        conn = db()
        try:
            for attempt in range(8):
                try:
                    cur = conn.execute(sql, args)
                    commit_retry(conn)
                    return cur
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 7:
                        raise
                    time.sleep(0.15 * (attempt + 1))
        finally:
            conn.close()


def init_db():
    conn = db()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS articles (
                link TEXT PRIMARY KEY,
                title TEXT,
                summary TEXT,
                source TEXT,
                category TEXT,
                published INTEGER,
                ingested INTEGER NOT NULL DEFAULT 0,
                image TEXT,
                country TEXT,
                image_tried INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                username_lower TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                email_lower TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                country TEXT NOT NULL DEFAULT 'US',
                language TEXT NOT NULL DEFAULT 'en',
                theme TEXT NOT NULL DEFAULT 'classic',
                view_mode TEXT NOT NULL DEFAULT 'grid',
                reader_font TEXT NOT NULL DEFAULT 'serif',
                welcome_done INTEGER NOT NULL DEFAULT 0,
                created INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS saved (
                user_id INTEGER NOT NULL,
                link TEXT NOT NULL,
                saved_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, link)
            );
            CREATE TABLE IF NOT EXISTS read_cache (
                link TEXT PRIMARY KEY,
                fetched INTEGER NOT NULL,
                status TEXT,
                title TEXT,
                byline TEXT,
                body TEXT,
                image TEXT,
                publisher_link TEXT
            );
            CREATE TABLE IF NOT EXISTS password_resets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                otp_hash TEXT NOT NULL,
                created INTEGER NOT NULL,
                expires INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                verified INTEGER NOT NULL DEFAULT 0,
                used INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_lower
                ON users(username_lower);
            CREATE INDEX IF NOT EXISTS idx_articles_published
                ON articles(published);
            CREATE INDEX IF NOT EXISTS idx_saved_user
                ON saved(user_id, saved_at);
            """
        )
        for column, ddl in (
            ("image", "TEXT"),
            ("country", "TEXT"),
            ("image_tried", "INTEGER NOT NULL DEFAULT 0"),
            ("ingested", "INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                conn.execute(f"ALTER TABLE articles ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError:
                pass
        # Existing rows were already "ingested" when they were first published;
        # seed the column so the first sort does not push them all to the top.
        conn.execute("UPDATE articles SET ingested = published WHERE ingested = 0")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_ingested ON articles(ingested)"
        )
        for column, ddl in (
            ("reader_font", "TEXT NOT NULL DEFAULT 'serif'"),
            ("email", "TEXT NOT NULL DEFAULT ''"),
            ("email_lower", "TEXT NOT NULL DEFAULT ''"),
        ):
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError:
                pass
        for column, ddl in (("verified", "INTEGER NOT NULL DEFAULT 0"),):
            try:
                conn.execute(f"ALTER TABLE password_resets ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError:
                pass
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower "
            "ON users(email_lower) WHERE email_lower <> ''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_resets_user "
            "ON password_resets(user_id, created)"
        )
        # Old Bing thumbnails were saved over plain http; browsers now block
        # those on an https page, so upgrade the stored links once.
        for table in ("articles", "read_cache"):
            try:
                conn.execute(
                    f"UPDATE {table} SET image = 'https://' || substr(image, 8) "
                    "WHERE image LIKE 'http://%bing.com/th%'"
                )
            except sqlite3.OperationalError:
                pass
        # Vercel starts each cold invocation with an empty /tmp database.
        # Seed a compact article bundle so the newsroom is never blank.
        if conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0:
            seed_path = BASE_DIR.parent / "data" / "seed_articles.json"
            try:
                items = json.loads(seed_path.read_text(encoding="utf-8"))
                if items:
                    newest = max(int(item.get("published") or 0) for item in items)
                    shift = int(time.time()) - newest - 60
                    for item in items:
                        item["published"] = int(item.get("published") or 0) + shift
                        item["ingested"] = int(item.get("ingested") or item["published"]) + shift
                columns = (
                    "link", "title", "summary", "source", "category", "published",
                    "ingested", "image", "country", "image_tried",
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO articles "
                    "(link, title, summary, source, category, published, ingested, "
                    "image, country, image_tried) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [tuple(item.get(column) for column in columns) for item in items],
                )
            except (OSError, ValueError, sqlite3.Error):
                pass
        # Keep the original account available even when Vercel wipes /tmp.
        # Upsert on every cold start so a stale password hash cannot lock the
        # user out after a redeploy.
        user_seed_path = BASE_DIR.parent / "data" / "seed_users.json"
        try:
            users = json.loads(user_seed_path.read_text(encoding="utf-8"))
            columns = (
                "username", "username_lower", "email", "email_lower", "password_hash",
                "country", "language", "theme", "view_mode", "reader_font",
                "welcome_done", "created",
            )
            defaults = {
                "username": "", "username_lower": "", "email": "", "email_lower": "",
                "password_hash": "", "country": "US", "language": "en",
                "theme": "classic", "view_mode": "grid", "reader_font": "serif",
                "welcome_done": 0, "created": int(time.time()),
            }
            placeholders = ", ".join("?" for _ in columns)
            updates = ", ".join(
                f"{column} = excluded.{column}"
                for column in columns
                if column not in ("username_lower", "created")
            )
            upsert = (
                f"INSERT INTO users ({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(username_lower) DO UPDATE SET {updates}"
            )
            for user in users:
                values = tuple(user.get(column, defaults[column]) for column in columns)
                try:
                    conn.execute(upsert, values)
                except sqlite3.IntegrityError:
                    # Another row may already own that email; seed without it.
                    values = tuple(
                        "" if column in ("email", "email_lower")
                        else user.get(column, defaults[column])
                        for column in columns
                    )
                    conn.execute(upsert, values)
        except (OSError, ValueError, sqlite3.Error):
            pass
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------ i18n / auth
def t(key, **kwargs):
    lang = getattr(g, "lang", "en")
    table = STRINGS.get(lang, STRINGS["en"])
    value = table.get(key) or STRINGS["en"].get(key) or key
    try:
        return value.format(**kwargs)
    except (KeyError, ValueError):
        return value


def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def current_user():
    return getattr(g, "user", None)


ACCOUNT_KEYS = (
    "id", "username", "username_lower", "email", "email_lower", "password_hash",
    "country", "language", "theme", "view_mode", "reader_font", "welcome_done",
    "created",
)


def account_snapshot(row):
    """Compact copy of a user row kept in the signed session cookie."""
    if not row:
        return None
    keys = row.keys()
    return {key: row[key] for key in ACCOUNT_KEYS if key in keys}


def sync_session_account(row):
    data = account_snapshot(row)
    if data:
        session["account"] = data


def refresh_session_account():
    user = current_user()
    if user:
        sync_session_account(fetchone("SELECT * FROM users WHERE id = ?", (user["id"],)))


def clear_auth_session():
    """Log out, but keep the account hint so login still works on cold starts."""
    account = session.get("account")
    session.clear()
    if account:
        session["account"] = account


def restore_account_from_session():
    """Recreate this user on a cold Vercel instance with an empty /tmp DB."""
    data = session.get("account")
    if not isinstance(data, dict) or not data.get("id"):
        return None
    try:
        execute(
            """
            INSERT OR IGNORE INTO users
                (id, username, username_lower, email, email_lower, password_hash,
                 country, language, theme, view_mode, reader_font, welcome_done, created)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                data.get(key, default)
                for key, default in (
                    ("id", 0), ("username", ""), ("username_lower", ""),
                    ("email", ""), ("email_lower", ""), ("password_hash", ""),
                    ("country", "US"), ("language", "en"), ("theme", "classic"),
                    ("view_mode", "grid"), ("reader_font", "serif"),
                    ("welcome_done", 0), ("created", int(time.time())),
                )
            ),
        )
    except sqlite3.Error:
        return None
    row = fetchone("SELECT * FROM users WHERE id = ?", (data["id"],))
    if row:
        session["uid"] = row["id"]
        session["lang"] = row["language"]
    return row


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            target = request.path
            if request.query_string:
                target += "?" + request.query_string.decode("utf-8", "ignore")
            return redirect(url_for("login", next=target))
        return view(*args, **kwargs)

    return wrapped


def api_required(view):
    """Like login_required, but unauthenticated API calls get JSON, not HTML."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return jsonify(ok=False, error="auth_required"), 401
        return view(*args, **kwargs)

    return wrapped


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("home")


def normalize_username(raw):
    value = unicodedata.normalize("NFKC", raw or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def valid_username(value):
    if not (2 <= len(value) <= 32):
        return False
    return not re.search(r"[\x00-\x1f<>/\\]", value)


def rate_limited(key):
    now = time.time()
    with login_fails_lock:
        attempts = [x for x in login_fails.get(key, []) if now - x < 900]
        login_fails[key] = attempts
        return len(attempts) >= 8


def record_fail(key):
    with login_fails_lock:
        login_fails.setdefault(key, []).append(time.time())


def clear_fails(key):
    with login_fails_lock:
        login_fails.pop(key, None)


# ------------------------------------------------------- password recovery
def normalize_email(raw):
    value = unicodedata.normalize("NFKC", raw or "").strip().lower()
    if not value or len(value) > 254 or not EMAIL_RE.match(value):
        return ""
    return value


def mask_email(value):
    name, sep, domain = value.partition("@")
    if not sep or not name:
        return value
    return name[0] + "•••@" + domain


def otp_digest(token, otp):
    payload = "|".join((str(app.config["SECRET_KEY"]), token, otp))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def smtp_config():
    ssl_flag = os.environ.get("NEWS_SMTP_SSL", "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    tls_flag = os.environ.get("NEWS_SMTP_TLS", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }
    try:
        port = int(os.environ.get("NEWS_SMTP_PORT") or (465 if ssl_flag else 587))
    except ValueError:
        port = 465 if ssl_flag else 587
    try:
        timeout = float(os.environ.get("NEWS_SMTP_TIMEOUT") or 10)
    except ValueError:
        timeout = 10.0
    user = os.environ.get("NEWS_SMTP_USER", "").strip()
    sender = os.environ.get("NEWS_SMTP_FROM", "").strip() or user or "no-reply@feedline.local"
    return {
        "host": os.environ.get("NEWS_SMTP_HOST", "").strip(),
        "port": port,
        "user": user,
        "password": os.environ.get("NEWS_SMTP_PASSWORD", ""),
        "sender": sender,
        "ssl": ssl_flag,
        "tls": not ssl_flag and tls_flag,
        "timeout": timeout,
    }


def send_otp_email(to, otp, username=""):
    """Send the reset code using SMTP settings from the environment."""
    cfg = smtp_config()
    if not cfg["host"]:
        log("OTP email skipped: NEWS_SMTP_HOST is not configured")
        return False

    greeting = "Hello " + username + "," if username else "Hello,"
    message = EmailMessage()
    message["Subject"] = "Your Feedline password reset code"
    message["From"] = cfg["sender"]
    message["To"] = to
    message.set_content(
        greeting
        + "\n\n"
        + "Your Feedline reset code is "
        + otp
        + ". It expires in 10 minutes.\n\n"
        + "If you did not ask to reset your account, you can ignore this email.\n\n"
        + "— Feedline"
    )

    context = ssl.create_default_context()
    try:
        if cfg["ssl"]:
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=cfg["timeout"], context=context) as smtp:
                if cfg["user"]:
                    smtp.login(cfg["user"], cfg["password"])
                smtp.send_message(message)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=cfg["timeout"]) as smtp:
                smtp.ehlo()
                if cfg["tls"]:
                    smtp.starttls(context=context)
                    smtp.ehlo()
                if cfg["user"]:
                    smtp.login(cfg["user"], cfg["password"])
                smtp.send_message(message)
        return True
    except Exception as exc:  # noqa: BLE001 - mail providers fail in many ways
        log("OTP email failed:", type(exc).__name__, exc)
        return False


@app.before_request
def before_request():
    g.user = None
    g.lang = session.get("lang", "en")
    uid = session.get("uid")
    if uid:
        row = fetchone("SELECT * FROM users WHERE id = ?", (uid,))
        if not row:
            row = restore_account_from_session()
        if row:
            g.user = row
            g.lang = row["language"]
        else:
            session.clear()
    if g.lang not in LANGUAGES:
        g.lang = "en"

    if request.method == "POST" and request.endpoint != "static":
        sent = request.headers.get("X-CSRF-Token") or request.form.get("_csrf")
        expected = session.get("_csrf", "")
        if not sent or not expected or not secrets.compare_digest(sent, expected):
            abort(400, "Invalid or missing CSRF token.")


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


@app.context_processor
def inject_globals():
    user = current_user()
    lang = getattr(g, "lang", "en")
    return {
        "user": user,
        "lang": lang,
        "rtl": lang in RTL_LANGS,
        "t": t,
        "csrf_token": csrf_token,
        "LANGUAGES": LANGUAGES,
        "COUNTRY_OPTIONS": COUNTRY_OPTIONS,
        "THEMES": THEMES,
        "base_css": Markup(BASE_CSS),
        "base_js": Markup(BASE_JS),
        "theme": user["theme"] if user else "classic",
    }


# ------------------------------------------------------------- React SPA
# The React app is the project root; this API lives in the server/ folder.
SPA_DIR = BASE_DIR.parent / "dist"


def bootstrap_payload():
    """Everything the React app needs before it makes its first API call."""
    user = current_user()
    lang = getattr(g, "lang", "en")
    strings = dict(STRINGS["en"])
    strings.update(STRINGS.get(lang, {}))
    return {
        "csrf": csrf_token(),
        "lang": lang,
        "rtl": lang in RTL_LANGS,
        "strings": strings,
        "languages": [{"code": code, "name": name} for code, name in LANGUAGES.items()],
        "countries": [
            {"code": code, "name": info["name"]} for code, info in COUNTRIES.items()
        ],
        "themes": THEMES,
        "reader_fonts": [
            {"code": code, "name": info["name"], "stack": info["stack"]}
            for code, info in READER_FONTS.items()
        ],
        "ranges": list(RANGES.keys()),
        # The Profile no longer lists sources. Keep a compact count for
        # diagnostics instead of shipping 1,000 URLs in every HTML response.
        "source_count": SOURCE_COUNT,
        "sources": [
            {"category": name, "count": len(urls)} for name, urls in FEEDS.items()
        ],
        "user": (
            {
                "id": user["id"],
                "username": user["username"],
                "email": user["email"] if "email" in user.keys() else "",
                "country": user["country"],
                "language": user["language"],
                "theme": user["theme"],
                "view_mode": user["view_mode"],
                "reader_font": user["reader_font"] if "reader_font" in user.keys() else "serif",
                "welcome_done": bool(user["welcome_done"]),
            }
            if user
            else None
        ),
    }


def spa_page():
    index = SPA_DIR / "index.html"
    if not index.exists():
        return Response(
            "React build not found. Run: npm install && npm run build in the project folder",
            status=500,
            mimetype="text/plain",
        )
    html = index.read_text(encoding="utf-8")
    payload = json.dumps(bootstrap_payload(), ensure_ascii=False).replace("<", "\\u003c")
    return Response(
        html.replace("= __NEWS_BOOTSTRAP__;", "= " + payload + ";", 1),
        mimetype="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.route("/assets/<path:name>")
def spa_assets(name):
    return send_from_directory(SPA_DIR / "assets", name)


# --------------------------------------------------------------- fetching
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Feedline/4.0"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    "Accept-Encoding": "gzip",
    "Accept-Language": "en-US,en;q=0.8",
}


def fetch_feed(url, timeout=FEED_TIMEOUT):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if getattr(resp, "status", 200) >= 400:
            raise urllib.error.HTTPError(url, resp.status, "HTTP error", resp.headers, None)
        raw = resp.read(MAX_FEED_BYTES)
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in encoding:
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
    return feedparser.parse(raw)


def fresh_feed_urls():
    """Search feeds used to top the store up to MIN_FRESH_PER_ROUND stories."""
    urls = []
    for query in FRESH_QUERY_TERMS:
        for window in ("when:1h", "when:1d"):
            urls.append(
                "https://news.google.com/rss/search?"
                + urlencode({
                    "q": query + " " + window,
                    "hl": "en-US",
                    "gl": "US",
                    "ceid": "US:en",
                })
            )
        urls.append(
            "https://www.bing.com/news/search?"
            + urlencode({"q": query, "format": "RSS"})
        )
    return urls


def feed_source(feed, url):
    title = feed.feed.get("title") if getattr(feed, "feed", None) else None
    if title:
        return clean(title)[:90]
    return re.sub(r"^https?://([^/]+).*$", r"\1", url)


def entry_timestamp(entry):
    parsed = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
        or entry.get("created_parsed")
    )
    if not parsed:
        return None
    try:
        return int(timegm(parsed))
    except (TypeError, ValueError, OverflowError):
        return None


def insert_article(conn, link, title, summary, source, category, published, image, country):
    with write_lock:
        exists = conn.execute("SELECT 1 FROM articles WHERE link = ?", (link,)).fetchone()
        image = prepare_image(image)
        ingested = int(time.time())
        conn.execute(
            """
            INSERT INTO articles
                (link, title, summary, source, category, published, ingested, image, country)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(link) DO UPDATE SET
                title = excluded.title,
                summary = CASE
                    WHEN length(COALESCE(excluded.summary, '')) >
                         length(COALESCE(articles.summary, ''))
                    THEN excluded.summary ELSE articles.summary END,
                image = COALESCE(excluded.image, articles.image),
                country = COALESCE(articles.country, excluded.country),
                ingested = COALESCE(articles.ingested, excluded.ingested)
            """,
            (
                link,
                title[:300],
                (summary or "")[:900],
                source[:90],
                category,
                published,
                ingested,
                image,
                country,
            ),
        )
        return 0 if exists else 1


def ingest_feed(conn, feed, url, category, now=None, country=None):
    """Save one parsed feed and return the number of genuinely new stories."""
    now = int(now or time.time())
    source = feed_source(feed, url)
    added = 0
    for entry in getattr(feed, "entries", []):
        link = (entry.get("link") or "").strip()
        published = entry_timestamp(entry)
        if not link or not published:
            continue
        if published > now + 3600:
            published = now
        if published < now - KEEP_DAYS * 86400:
            continue
        title = clean(entry.get("title", ""))
        if not title:
            continue
        summary = entry.get("summary", "") or ""
        if not summary:
            content = entry.get("content") or []
            if content and isinstance(content, list):
                summary = content[0].get("value", "") or ""
        added += insert_article(
            conn,
            link=link,
            title=title,
            summary=summary,
            source=source,
            category=classify(title, summary, category),
            published=published,
            image=get_image(entry),
            country=country,
        )
    return added


def top_up_fresh(conn, needed, now=None):
    """If the minute was quiet, pull real stories from live news searches."""
    added = 0
    urls = fresh_feed_urls()
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch_feed, url, 4): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                feed = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad search must not stop the rest
                log("Fresh feed failed:", url, type(exc).__name__, exc)
                continue
            added += ingest_feed(conn, feed, url, "World", now=now)
            commit_retry(conn)
            if added >= needed:
                for pending in futures:
                    if not pending.done():
                        pending.cancel()
                break
    return added


def select_refresh_jobs():
    """Core feeds plus the next rotating slice of the large source pool."""
    jobs = list(CORE_FEED_JOBS)
    if ROTATING_FEED_JOBS:
        total = len(ROTATING_FEED_JOBS)
        start = feed_cursor["index"] % total
        count = min(FEEDS_PER_ROUND, total)
        jobs.extend(
            ROTATING_FEED_JOBS[(start + offset) % total] for offset in range(count)
        )
        feed_cursor["index"] = (start + count) % total
    return jobs


def refresh():
    """Fetch a rotating feed slice in parallel and save new articles."""
    if not refresh_lock.acquire(blocking=False):
        return 0
    try:
        now = int(time.time())
        jobs = select_refresh_jobs()
        fetched = {}
        with ThreadPoolExecutor(max_workers=FEED_WORKERS) as pool:
            futures = {pool.submit(fetch_feed, url): (category, url) for category, url in jobs}
            for future in as_completed(futures):
                category, url = futures[future]
                try:
                    fetched[(category, url)] = future.result()
                except Exception as exc:  # noqa: BLE001 - one bad feed must not stop the rest
                    log("Feed failed:", url, type(exc).__name__, exc)

        new = 0
        with write_lock, db() as conn:
            for (category, url), feed in fetched.items():
                new += ingest_feed(conn, feed, url, category, now=now)
                commit_retry(conn)  # short transactions leave room for sign-ins
                time.sleep(0.02)    # let the image worker and sign-ins take the writer

            # The user-facing promise is at least five fresh stories a minute.
            # If the published feeds were quiet, fill the gap from live searches.
            if new < MIN_FRESH_PER_ROUND:
                needed = MIN_FRESH_PER_ROUND - new
                topped = top_up_fresh(conn, needed, now=now)
                new += topped
                log("Fresh top-up:", topped)

            conn.execute(
                "DELETE FROM articles WHERE published < ? "
                "AND link NOT IN (SELECT link FROM saved)",
                (now - KEEP_DAYS * 86400,),
            )
            conn.execute(
                "DELETE FROM read_cache WHERE fetched < ? "
                "AND link NOT IN (SELECT link FROM saved)",
                (now - KEEP_DAYS * 86400,),
            )
            commit_retry(conn)
            newest = conn.execute(
                "SELECT link FROM articles ORDER BY ingested DESC, published DESC LIMIT 1"
            ).fetchone()
            state["fresh_link"] = newest["link"] if newest else None
        state["last_refresh"] = time.time()
        log("Refresh done, new articles:", new, "sources this round:", len(jobs))
        if new < MIN_FRESH_PER_ROUND:
            log("Refresh warning: only", new, "new articles this round")
        return new
    finally:
        refresh_lock.release()


def country_feed_url(code):
    return f"https://news.google.com/rss?hl=en-US&gl={code}&ceid={code}:en"


def fetch_country_news(code):
    info = COUNTRIES.get(code)
    if not info:
        return 0
    feed = fetch_feed(country_feed_url(code), timeout=6)
    now = int(time.time())
    changed = 0
    with write_lock, db() as conn:
        before = conn.total_changes
        for entry in getattr(feed, "entries", []):
            link = (entry.get("link") or "").strip()
            published = entry_timestamp(entry)
            if not link or not published:
                continue
            if published > now + 3600:
                published = now
            if published < now - KEEP_DAYS * 86400:
                continue
            title = clean(entry.get("title", ""))
            source = f"Google News · {info['name']}"
            if " - " in title:
                head, tail = title.rsplit(" - ", 1)
                if 2 <= len(tail) <= 60:
                    title, source = head, tail
            if not title:
                continue
            insert_article(
                conn,
                link=link,
                title=title,
                summary=entry.get("summary", "") or "",
                source=source,
                category="World",
                published=published,
                image=get_image(entry),
                country=code,
            )
        changed = conn.total_changes - before
        commit_retry(conn)
    log(f"Local news {code}: {changed} rows changed")
    return changed


def ensure_country_news(code, force=False):
    if code not in COUNTRIES:
        return 0
    now = time.time()
    if not force and now - country_state.get(code, 0) < COUNTRY_TTL:
        return 0
    lock = country_locks[code]
    if not lock.acquire(blocking=False):
        return 0
    try:
        if not force and time.time() - country_state.get(code, 0) < COUNTRY_TTL:
            return 0
        country_state[code] = time.time()
        # Local news is a bonus. Never hold the first page load hostage to a
        # busy writer; the next request retries after the short cooldown.
        if not write_lock.acquire(blocking=False):
            country_state[code] = time.time() - COUNTRY_TTL + 120
            return 0
        try:
            return fetch_country_news(code)
        except Exception as exc:  # noqa: BLE001 - local news is a bonus
            log("Local feed failed:", code, type(exc).__name__, exc)
            # retry in a couple of minutes instead of waiting the full 30
            country_state[code] = time.time() - COUNTRY_TTL + 120
            return 0
        finally:
            write_lock.release()
    finally:
        lock.release()


def refresh_loop():
    while True:
        try:
            refresh()
        except Exception as exc:  # noqa: BLE001 - keep the thread alive
            log("Refresh loop error:", type(exc).__name__, exc)
            log(traceback.format_exc())
        image_worker_event.set()
        time.sleep(REFRESH_MINUTES * 60)


def run_refresh_job(country=None):
    """Background worker behind the instant Refresh response."""
    changed = 0
    error = ""
    try:
        changed = refresh()
        if country:
            country_state.pop(country, None)
            ensure_country_news(country, force=True)
        image_worker_event.set()
    except Exception as exc:  # noqa: BLE001 - status endpoint reports the error
        error = f"{type(exc).__name__}: {exc}"
        log("Refresh job error:", error)
    finally:
        with refresh_job_lock:
            refresh_job.update(
                running=False,
                finished=time.time(),
                changed=changed,
                error=error,
            )


def start_refresh_job(country=None):
    """Start one refresh without making the click wait for the network."""
    with refresh_job_lock:
        if refresh_job["running"]:
            return False
        refresh_job.update(
            running=True,
            started=time.time(),
            finished=None,
            changed=0,
            error="",
        )
    threading.Thread(target=run_refresh_job, args=(country,), daemon=True).start()
    return True


# --------------------------------------------------------------- text utils
TAG_RE = re.compile(r"<[^>]+>")


def clean(text):
    value = html.unescape(text or "")
    value = re.sub(r"<(script|style)\b.*?</\1>", " ", value, flags=re.I | re.S)
    value = TAG_RE.sub(" ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def search_terms(query):
    """Split a search into words so 'trump iran' matches both words anywhere."""
    normalized = unicodedata.normalize("NFKC", query or "").casefold().strip()
    if not normalized:
        return []
    words = [
        word for word in re.findall(r"[^\W_]+", normalized, re.UNICODE)
        if len(word) >= 2
    ]
    return words[:8] or [normalized]


def highlight(text, query):
    terms = search_terms(query)
    if not terms:
        return escape(text)
    pattern = re.compile(
        "|".join(re.escape(term) for term in sorted(set(terms), key=len, reverse=True)),
        re.I,
    )
    out = []
    last = 0
    for match in pattern.finditer(text):
        out.append(escape(text[last:match.start()]))
        out.append(Markup("<mark>"))
        out.append(escape(match.group(0)))
        out.append(Markup("</mark>"))
        last = match.end()
    out.append(escape(text[last:]))
    return Markup("").join(out)


def match_score(row, terms):
    """Relevance of a stored story: every word must appear somewhere."""
    title = clean(row["title"]).casefold()
    summary = clean(row["summary"]).casefold()
    source = (row["source"] or "").casefold()
    score = 0
    for term in terms:
        title_hits = title.count(term)
        summary_hits = summary.count(term)
        if not title_hits and not summary_hits and term not in source:
            return 0
        score += title_hits * 6 + summary_hits * 2 + (4 if term in source else 0)
    return score


def classify(title, summary, fallback):
    title_text = title.casefold()
    summary_text = summary.casefold()
    best, best_score = None, 0
    for name in ORDER:
        regex = TOPIC_REGEX.get(name)
        if not regex:
            continue
        title_hits = len({m.group(0).lower() for m in regex.finditer(title_text)})
        summary_hits = len({m.group(0).lower() for m in regex.finditer(summary_text)})
        score = title_hits * 4 + summary_hits
        if score > best_score:
            best, best_score = name, score
    if best:
        return best
    return fallback if fallback in ORDER else "World"


def time_ago(ts):
    diff = max(int(time.time()) - int(ts or 0), 0)
    if diff < 60:
        return t("just_now")
    if diff < 3600:
        return t("min_ago", n=max(diff // 60, 1))
    if diff < 86400:
        return t("hour_ago", n=diff // 3600)
    return t("day_ago", n=diff // 86400)


def day_label(ts):
    today = datetime.now().date()
    date = datetime.fromtimestamp(ts).date()
    if date == today:
        return t("today")
    if date == today - timedelta(days=1):
        return t("yesterday")
    return date.strftime("%A, %d %b")


# ------------------------------------------------------------------ images
BAD_IMAGE = re.compile(
    r"(sprite|logo|icon|avatar|placeholder|1x1|pixel|blank|spacer|"
    r"default[-_]?image|transparent|grey\.gif|feedsportal|"
    r"J6_coFbogxhRI9iM864NL)",  # Google News' generic fallback picture
    re.I,
)


def int_or_zero(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def url_width(url):
    match = re.search(r"[?&](?:width|w)=(\d+)", url, re.I)
    return int(match.group(1)) if match else 0


def usable_image(url):
    if not url or url.startswith("data:"):
        return False
    # RSS feeds sometimes put a photo caption inside the image field. Those
    # strings contain spaces or quotes and can never load as a picture.
    if re.search(r"\s", url):
        return False
    if url.lower().endswith(".svg"):
        return False
    if VIDEO_IMAGE.search(url):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    return not BAD_IMAGE.search(url)


PHOTON_RX = re.compile(r"^https?://i\d\.wp\.com/", re.I)
VIDEO_IMAGE = re.compile(r"\.(?:mp4|m4v|webm|mov|m3u8|mp3)(?:$|[?#])", re.I)
PAGE_IMAGE_PATH = re.compile(r"/image/(?:jpe?g|png|webp|gif|avif)/?$", re.I)
SIGNED_URL = re.compile(r"[?&](?:s|sig|signature|hash)=", re.I)
DEAD_IMAGE_HOSTS = {"scx1.b-cdn.net"}
MIN_IMAGE_WIDTH = 500


def ascii_url(url):
    """Percent-encode a URL so urllib never chokes on narrow spaces etc."""
    if not url:
        return url
    return quote(html.unescape(url).strip(), safe=":/?&=#%+,;@!$'()*~[]")


def unproxy_image(url):
    """WordPress' Photon CDN (i0.wp.com) 404s for many sites; use the origin."""
    if not url:
        return url
    match = PHOTON_RX.match(url)
    if not match:
        return url
    rest = url[match.end():]
    if rest.lower().startswith(("http://", "https://")):
        return rest
    return "https://" + rest


def page_like_image(url):
    """True when the 'image' is really the article page or a video file."""
    if not url:
        return True
    if VIDEO_IMAGE.search(url):
        return True
    path = urlparse(url).path
    if PAGE_IMAGE_PATH.search(path):
        return True
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return last.isdigit() and len(last) <= 5  # Guardian/TechCrunch use .../1200


def image_too_small(url):
    url = url or ""
    width = url_width(url)
    if not width and "ichef.bbci.co.uk" in url:
        match = re.search(r"/standard/(\d{2,4})/", url)
        width = int(match.group(1)) if match else 0
    match = re.search(r"=s\d+-w(\d+)", url)
    if match:
        width = max(width, int(match.group(1)))
    return bool(width and width < MIN_IMAGE_WIDTH)


def image_needs_repair(url):
    """Pictures worth re-fetching: dead hosts, web pages, videos, tiny crops."""
    if not url or not usable_image(url):
        return True
    if PHOTON_RX.match(url):
        return True  # i0.wp.com: rewrite to the publisher's own copy
    if page_like_image(url) or image_too_small(url):
        return True
    host = (urlparse(url).hostname or "").lower()
    return host in DEAD_IMAGE_HOSTS


def prepare_image(url):
    """Clean, enlarge and validate an image URL before it is stored."""
    if not url:
        return None
    url = html.unescape(url.strip())
    if not usable_image(url):
        return None
    url = unproxy_image(url)
    url = big_image(url)
    if page_like_image(url):
        return None
    return ascii_url(url)


def get_image(entry):
    """Pick the largest usable image a feed offers."""
    candidates = []

    def add(url, width=0):
        url = html.unescape((url or "").strip())
        if not usable_image(url):
            return
        candidates.append((width or url_width(url), url))

    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key) or []:
            if (media.get("type") or "").startswith("video"):
                continue
            add(media.get("url"), int_or_zero(media.get("width")))

    for link in entry.get("links") or []:
        if (link.get("type") or "").startswith("image"):
            add(link.get("href"))

    for enclosure in entry.get("enclosures") or []:
        if (enclosure.get("type") or "").startswith("image"):
            add(enclosure.get("href"))

    html_blocks = []
    for block in entry.get("content") or []:
        html_blocks.append(block.get("value") or "")
    html_blocks.append(entry.get("summary") or "")
    for block in html_blocks:
        for src in re.findall(r"<img[^>]+src=[\"']([^\"']+)", block, re.I)[:4]:
            add(src)

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def big_image(url):
    """Ask common CDNs for a larger (crisper) version of a thumbnail."""
    if not url:
        return None
    url = html.unescape(url.strip())
    if url.startswith("http://") and "bing.com/th" in url:
        url = "https://" + url[7:]
    if "ichef.bbci.co.uk" in url:
        return ascii_url(re.sub(r"/standard/\d{2,4}/", "/standard/976/", url))
    if "cnn.com" in url:
        url = re.sub(r"([?&]q=)w_\d+", r"\g<1>w_900", url)
    if "googleusercontent.com" in url:
        # Google News story thumbnails: =s0-w300-rw -> a usable size
        sized = re.sub(r"=s\d+-w\d+(-rw)?$", "=s0-w1200-rw", url)
        if sized != url:
            return ascii_url(sized)
    if "deadline.com" in url:
        # ?w=200&h=1000 is a tall crop; drop the height and ask for a real photo
        url = re.sub(r"[?&]h=\d+", "", url)
        url = re.sub(r"([?&]w=)\d+", r"\g<1>1200", url)
    if not SIGNED_URL.search(url):  # signed URLs (the Guardian) break if edited
        for name in ("width", "w"):
            match = re.search(rf"([?&]{name}=)(\d+)", url)
            if match and int(match.group(2)) < 1200:
                url = url[: match.start(2)] + "1200" + url[match.end(2):]
    return ascii_url(url)


META_IMAGE_RE = re.compile(r"og:image|twitter:image", re.I)
CONTENT_RE = re.compile(r"""content\s*=\s*["']([^"']+)""", re.I)


def page_image(url, timeout=FEED_TIMEOUT):
    """Read og:image / twitter:image from an article page.

    Many feeds (ScienceDaily, Al Jazeera, CNBC, TechCrunch, DW) carry no
    image at all, and some stored CDN links expire. The article page still
    advertises a picture, so we recover it there.
    """
    if not url or not url.startswith(("http://", "https://")):
        return None
    parsed = urlparse(url)
    if (parsed.hostname or "").endswith("news.google.com"):
        # Google's wrapper pages are a JavaScript shell with no real photo.
        return None
    req = urllib.request.Request(
        ascii_url(url),
        headers={**HEADERS, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(MAX_PAGE_BYTES)
        if "gzip" in (resp.headers.get("Content-Encoding") or "").lower():
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        final_url = resp.geturl() or url
    page = raw.decode("utf-8", "ignore")
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        if not META_IMAGE_RE.search(tag):
            continue
        match = CONTENT_RE.search(tag)
        if not match:
            continue
        candidate = html.unescape(match.group(1).strip())
        if not candidate.lower().startswith(("http://", "https://")):
            candidate = urljoin(final_url, candidate)
        candidate = prepare_image(candidate)
        if candidate:
            return candidate
    return None


def google_title_image(title):
    """Google News links hide the publisher URL; find the same story on Bing."""
    fallback = None
    for link, entry in bing_story_hits(title):
        try:
            image = page_image(link)
        except Exception:  # noqa: BLE001 - try the next match
            image = None
        if image:
            return image
        if not fallback:
            # Some publishers (Phys.org) block bots: Bing's own thumbnail works.
            fallback = prepare_image(bing_thumbnail(entry))
    return fallback


def recover_image(link, title=""):
    """Best picture for a story: the page itself, then a Bing headline match."""
    if "news.google.com" in (link or ""):
        return google_title_image(title)
    try:
        image = page_image(link)
    except Exception:  # noqa: BLE001 - publishers such as Phys.org block bots
        image = None
    if image:
        return image
    return google_title_image(title)


def backfill_images(limit=BACKFILL_BATCH):
    """Find pictures for stories that arrived without one, newest arrivals first."""
    now = int(time.time())
    rows = fetchall(
        """
        SELECT link, title FROM articles
        WHERE (image IS NULL OR image = '')
          AND published >= ?
          AND COALESCE(image_tried, 0) < ?
        ORDER BY ingested DESC, published DESC LIMIT ?
        """,
        (now - KEEP_DAYS * 86400, now - IMAGE_RETRY, limit),
    )
    if not rows:
        return 0
    found = 0
    results = []
    # Do the slow network work first, with no SQLite transaction open.
    with ThreadPoolExecutor(max_workers=IMAGE_WORKERS) as pool:
        futures = {
            pool.submit(recover_image, row["link"], row["title"]): row["link"]
            for row in rows
        }
        for future in as_completed(futures):
            try:
                image = future.result()
            except Exception:  # noqa: BLE001 - a dead page is not fatal
                image = None
            results.append((futures[future], image))

    # Then hold one continuous write lock: releasing it between uncommitted
    # updates lets a refresh hit a real "database is locked" error.
    with write_lock:
        conn = db()
        try:
            for index, (link, image) in enumerate(results, 1):
                conn.execute(
                    "UPDATE articles SET image = ?, image_tried = ? WHERE link = ?",
                    (image, now, link),
                )
                found += 1 if image else 0
                if index % 50 == 0:
                    commit_retry(conn)
            commit_retry(conn)
        finally:
            conn.close()
    log(f"Image backfill: {found}/{len(rows)} found")
    return found


def repair_images(limit=REPAIR_BATCH):
    """Upgrade stored pictures: dead CDNs, page URLs, video files, tiny crops."""
    now = int(time.time())
    rows = fetchall(
        """
        SELECT link, title, image FROM articles
        WHERE image IS NOT NULL AND image <> ''
          AND published >= ?
          AND COALESCE(image_tried, 0) < ?
        ORDER BY ingested DESC, published DESC LIMIT ?
        """,
        (now - KEEP_DAYS * 86400, now - IMAGE_RETRY, limit * 4),
    )
    if not rows:
        return 0

    todo = []
    fixed = 0
    for row in rows:
        current = row["image"]
        if not image_needs_repair(current):
            continue
        upgraded = prepare_image(current)
        if upgraded and not image_needs_repair(upgraded):
            execute(
                "UPDATE articles SET image = ? WHERE link = ?",
                (upgraded, row["link"]),
            )
            fixed += 1
            continue
        todo.append(row)

    if not todo:
        if fixed:
            log(f"Image repair (no network): {fixed} upgraded")
        return fixed

    todo = todo[:limit]
    stamp = int(time.time())
    found = 0
    results = []
    # Recover over the network first, then write under a single held lock.
    with ThreadPoolExecutor(max_workers=IMAGE_WORKERS) as pool:
        futures = {
            pool.submit(recover_image, row["link"], row["title"]): row
            for row in todo
        }
        for future in as_completed(futures):
            row = futures[future]
            try:
                image = future.result()
            except Exception:  # noqa: BLE001 - a dead page is not fatal
                image = None
            results.append((row["link"], image))

    with write_lock:
        conn = db()
        try:
            for index, (link, image) in enumerate(results, 1):
                if image and not image_needs_repair(image):
                    conn.execute(
                        "UPDATE articles SET image = ?, image_tried = ? WHERE link = ?",
                        (image, stamp, link),
                    )
                    found += 1
                else:
                    # A photo that never loads is worse than a clean letter tile.
                    conn.execute(
                        "UPDATE articles SET image = NULL, image_tried = ? WHERE link = ?",
                        (stamp, link),
                    )
                if index % 50 == 0:
                    commit_retry(conn)
            commit_retry(conn)
        finally:
            conn.close()
    log(f"Image repair: {fixed} upgraded, {found}/{len(todo)} recovered")
    return fixed + found


def image_worker_loop():
    """Continuously fill the grid with pictures, starting with new arrivals.

    Running this on its own thread keeps image recovery off the refresh and
    click paths. A refresh only signals the event so fresh stories are picked
    up on the next pass.
    """
    while True:
        worked = False
        try:
            worked = bool(backfill_images())
        except Exception as exc:  # noqa: BLE001 - pictures are best effort
            log("Image backfill error:", type(exc).__name__, exc)
        try:
            worked = bool(repair_images()) or worked
        except Exception as exc:  # noqa: BLE001 - pictures are best effort
            log("Image repair error:", type(exc).__name__, exc)
        image_worker_event.wait(2 if worked else 20)
        image_worker_event.clear()


IMAGE_CACHE = Path(tempfile.gettempdir()) / "feedline_image_cache"
PRIVATE_HOST = re.compile(
    r"^(localhost|127\.|10\.|192\.168\.|169\.254\.|0\.|\[?::1\]?|"
    r"172\.(?:1[6-9]|2\d|3[01])\.)",
    re.I,
)


def proxy_url_allowed(url):
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip()
    if not host or "." not in host and ":" not in host:
        return False
    return not PRIVATE_HOST.search(host)


# ---------------------------------------------------------- live web search
live_cache = {}
live_cache_lock = threading.Lock()


def live_cache_key(query, country):
    return (" ".join((query or "").casefold().split()), country)


def cached_live_links(query, country):
    """Links from a web search done in the last few minutes, if any."""
    key = live_cache_key(query, country)
    with live_cache_lock:
        cached = live_cache.get(key)
        if cached and time.time() - cached[0] < 600:
            return list(cached[1])
    return []


def enrich_images(links, limit=24, workers=6, budget=None):
    """Find pictures for stored stories that arrived without one.

    ``budget`` is an optional number of seconds to wait; anything unfinished
    is left for the background round so a page never blocks on slow sites.
    """
    if not links:
        return 0
    placeholders = ",".join("?" for _ in links)
    rows = fetchall(
        f"SELECT link, title FROM articles WHERE (image IS NULL OR image = '') "
        f"AND link IN ({placeholders}) LIMIT ?",
        tuple(links) + (limit,),
    )
    if not rows:
        return 0
    stamp = int(time.time())
    found = 0
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {}
    for row in rows:
        try:
            futures[pool.submit(recover_image, row["link"], row["title"])] = row["link"]
        except RuntimeError:  # interpreter is shutting down
            break
    try:
        wait(futures, timeout=budget)
        for future, link in list(futures.items()):
            if not future.done() or future.cancelled():
                continue
            try:
                image = future.result()
            except Exception:  # noqa: BLE001 - a dead page is not fatal
                image = None
            execute(
                "UPDATE articles SET image = ?, image_tried = ? WHERE link = ?",
                (image, stamp, link),
            )
            found += 1 if image else 0
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return found


def direct_link(link):
    """Bing wraps results in apiclick.aspx?url=...; unwrap to the publisher."""
    link = (link or "").strip()
    if "bing.com/news/apiclick.aspx" in link:
        params = parse_qs(urlparse(link).query)
        real = (params.get("url") or [""])[0]
        if real.startswith(("http://", "https://")):
            return real
    return link


def web_entries(query, country, limit):
    """Search the web for news. Bing first (real links), Google as backup."""
    errors = []
    answered = False
    try:
        params = urlencode({"q": query, "format": "RSS"})
        feed = fetch_feed("https://www.bing.com/news/search?" + params, timeout=8)
        entries = list(getattr(feed, "entries", []))
        answered = True
        if entries:
            return entries
        errors.append("bing empty")
    except Exception as exc:  # noqa: BLE001 - try the next provider
        errors.append(f"bing {type(exc).__name__}")

    try:
        params = urlencode(
            {
                "q": f"{query} when:{KEEP_DAYS}d",
                "hl": "en-US",
                "gl": country,
                "ceid": f"{country}:en",
            }
        )
        feed = fetch_feed("https://news.google.com/rss/search?" + params, timeout=8)
        entries = list(getattr(feed, "entries", []))
        answered = True
        if entries:
            return entries
        errors.append("google empty")
    except Exception as exc:  # noqa: BLE001 - surface the combined failure
        errors.append(f"google {type(exc).__name__}")

    if answered:
        return []
    log("Web search failed:", ", ".join(errors))
    raise RuntimeError("web search unavailable: " + ", ".join(errors))


def bing_thumbnail(entry):
    """Bing's feed carries a ready-made (and resizable) story thumbnail."""
    url = entry.get("news_image")
    if not url:
        return None
    url = html.unescape(url).strip()
    if not url.startswith(("http://", "https://")):
        return None
    if url.startswith("http://"):
        url = "https://" + url[7:]
    url = re.sub(r"([?&])(?:w|h|c)=\d+", "", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}w=1200&h=675&c=7"


def bing_story_hits(title, limit=3):
    """Bing News results that look like the same story (real publisher links)."""
    title = clean(title)
    title = re.sub(r"\s+[-–|]\s+[^-–|]{2,60}$", "", title).strip()
    if len(title) < 12:
        return []
    try:
        params = urlencode({"q": title[:160], "format": "RSS"})
        feed = fetch_feed("https://www.bing.com/news/search?" + params, timeout=6)
    except Exception:  # noqa: BLE001 - just no match
        return []
    wanted = story_tokens(title)
    hits = []
    for entry in list(getattr(feed, "entries", []))[:6]:
        link = direct_link(entry.get("link"))
        if not link or "news.google.com" in link:
            continue
        if similar_story(wanted, story_tokens(clean(entry.get("title", "")))):
            hits.append((link, entry))
            if len(hits) >= limit:
                break
    return hits


# ------------------------------------------------------------------ reader
# Clicking a story opens it here: the page is fetched once, reduced to clean
# paragraphs (never raw third-party HTML) and cached in SQLite.
READ_TTL = 3 * 86400
READ_SHORT_TTL = 6 * 3600
READ_MAX_BLOCKS = 160
READ_MAX_CHARS = 45_000
READ_DROP = re.compile(
    r"<(script|style|noscript|svg|iframe|form|nav|header|footer|aside|"
    r"template|button|figure)\b.*?</\1>",
    re.I | re.S,
)
# Many news sites (Punch included) leave <p> tags unclosed. Finding the opening
# tags and stopping at the next one is far more reliable than looking for
# </p>, which used to swallow the whole article into a single block.
READ_TOKEN = re.compile(r"<(p|h1|h2|h3|h4|blockquote)\b[^>]*>", re.I | re.S)
READ_CONTENT_CLASS = re.compile(
    r"\b(post-content|entry-content|article-content|article-body|artbody|"
    r"story-body|storyBody|postBody)\b",
    re.I,
)
READ_JUNK_CONTAINER = re.compile(
    r"<div\b[^>]*?(?:class|id)=([\"'])[^\"']*\b(?:"
    r"punch-ps-cta|read-also|ad-container|adsbygoogle|primisVideoUnit|"
    r"show-primis-ads|text-ads|widget-wrapper|author-summary|copyright|"
    r"post-meta|share-article|OUTBRAIN|related|recommended|newsletter|social"
    r")[^\"']*\1[^>]*>.*?</div>",
    re.I | re.S,
)
READ_JUNK = re.compile(
    r"^(advertisement|share|subscribe|sign up|follow us|read more|related|"
    r"copyright|all rights reserved|photo|image|getty|cookie|we use cookies|"
    r"download the app|listen to|watch:|video:|trending|recommended|"
    r"skip to|menu|home|newsletter|support our journalism|discover more|"
    r"add us to your preferred sources|make punch part|find legal aid)\b",
    re.I,
)
READ_JUNK_ANY = re.compile(
    r"make punch part of your everyday experience|add us to your preferred "
    r"sources|discover more|related news|find legal aid|kindly share this "
    r"story|all rights reserved|stay informed and ahead|stay in the know|"
    r"news update:|attention nigerians:|download now and never miss",
    re.I,
)
SHORT_HEADING_HTML = re.compile(
    r"^\s*<(strong|b)\b[^>]*>(.*?)</\1>\s*$", re.I | re.S
)
READ_KIND = {"p": "p", "h1": "h2", "h2": "h2", "h3": "h3", "h4": "h3", "blockquote": "quote"}


def fetch_page(url, timeout=8, limit=1_500_000):
    """Fetch an article page and decode it to text."""
    req = urllib.request.Request(
        ascii_url(url),
        headers={**HEADERS, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(limit)
        if "gzip" in (resp.headers.get("Content-Encoding") or "").lower():
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        charset = resp.headers.get_content_charset()
        final_url = resp.geturl() or url
    if not charset:
        match = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:8192], re.I)
        charset = match.group(1).decode("ascii", "ignore") if match else "utf-8"
    try:
        page = raw.decode(charset, "ignore")
    except LookupError:
        page = raw.decode("utf-8", "ignore")
    return page, final_url


def meta_value(page, names):
    """First <meta> content whose property/name matches one of ``names``."""
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        low = tag.lower()
        if not any(name in low for name in names):
            continue
        match = CONTENT_RE.search(tag)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def tagged_div_inner(page, class_pattern):
    """Inner HTML of the first <div> whose class matches ``class_pattern``.

    Divs are counted instead of using a lazy regex so ad blocks nested inside
    the article body do not cut the story short.
    """
    opening = re.compile(r"<div\b[^>]*?class=([\"'])(.*?)\1[^>]*>", re.I | re.S)
    for match in opening.finditer(page):
        if not class_pattern.search(match.group(2)):
            continue
        depth = 1
        for token in re.finditer(r"<(/?)div\b", page[match.end():], re.I):
            depth += -1 if token.group(1) else 1
            if depth == 0:
                return page[match.end():match.end() + token.start()]
    return ""


def read_region(page):
    """The most article-like chunk of a page."""
    content = tagged_div_inner(page, READ_CONTENT_CLASS)
    if len(content) > 200:
        return content
    for pattern in (
        r"<article\b[^>]*>(.*?)</article>",
        r"<main\b[^>]*>(.*?)</main>",
    ):
        chunks = re.findall(pattern, page, re.I | re.S)
        if chunks:
            best = max(chunks, key=len)
            if len(READ_TOKEN.findall(best)) >= 2:
                return best
    return page


def is_reader_junk(text):
    """Drop promotions and recommendation strips that sit inside article HTML."""
    return bool(READ_JUNK.match(text) or READ_JUNK_ANY.search(text))


def looks_like_heading(text):
    """A short standalone line with no sentence ending is usually a subhead."""
    if not (5 <= len(text) <= 85):
        return False
    if len(text.split()) > 12:
        return False
    if text.endswith((".", "!", "?", ":", ";", "”", '"')):
        return False
    return not text.startswith(("“", '"', "‘"))


def extract_reading(page):
    """Pull a structured story out of a page. Returns (blocks, chars).

    Blocks use intentional types so the React reader can present a real
    article: lead, paragraph, h2, h3 and pull quote. Short lines that the
    publisher marked with <strong> (or that stand alone) become h3 subheads.
    """
    page = READ_DROP.sub(" ", page)
    region = read_region(page)
    for _ in range(5):
        stripped = READ_JUNK_CONTAINER.sub(" ", region)
        if stripped == region:
            break
        region = stripped

    tokens = list(READ_TOKEN.finditer(region))
    blocks = []
    seen = set()
    total = 0
    for index, match in enumerate(tokens):
        tag = match.group(1).lower()
        start = match.end()
        limit = tokens[index + 1].start() if index + 1 < len(tokens) else len(region)
        close = re.search(r"</" + tag + r"\s*>", region[start:limit], re.I)
        if close:
            limit = start + close.start()
        raw = region[start:limit]
        text = clean(raw)
        if not text:
            continue
        kind = READ_KIND[tag]
        strong_heading = SHORT_HEADING_HTML.match(raw.strip()) if kind == "p" else None
        if strong_heading:
            text = clean(strong_heading.group(2))
            kind = "h3"
        elif kind == "p" and looks_like_heading(text):
            kind = "h3"
        if is_reader_junk(text):
            continue
        minimum = 24 if kind == "quote" else (4 if kind in ("h2", "h3") else 40)
        if len(text) < minimum:
            continue
        key = text[:120].casefold()
        if key in seen:
            continue
        seen.add(key)
        blocks.append({"kind": kind, "text": text})
        total += len(text)
        if len(blocks) >= READ_MAX_BLOCKS or total >= READ_MAX_CHARS:
            break

    # The first substantial paragraph becomes the lead: larger type, drop cap.
    for block in blocks:
        if block["kind"] == "p":
            block["kind"] = "lead"
            break

    # Drop empty headings at the edges, and repeated tiny headings.
    while blocks and blocks[0]["kind"] in ("h2", "h3") and len(blocks[0]["text"]) < 12:
        blocks.pop(0)
    while blocks and blocks[-1]["kind"] in ("h2", "h3", "quote"):
        blocks.pop()
    return blocks, total


def page_byline(page, fallback=""):
    """Author name from article metadata, with a DOM fallback for Punch."""
    value = clean(meta_value(page, ("article:author", "author", "parsely-author")))
    if not value:
        match = re.search(
            r"<[^>]+class=([\"'])[^\"']*\bpost-author\b[^\"']*\1[^>]*>(.*?)</\w+>",
            page,
            re.I | re.S,
        )
        if match:
            value = clean(match.group(2))
    value = re.sub(r"^\s*by\s+", "", value, flags=re.I).strip()
    return value[:120] or fallback


def read_status(blocks, total):
    paragraphs = sum(1 for block in blocks if block["kind"] in ("p", "lead"))
    if total >= 400 and paragraphs >= 3:
        return "full"
    if total >= 120:
        return "partial"
    return "blocked"


def page_heading(page, fallback):
    title = meta_value(page, ("og:title", "twitter:title"))
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        title = clean(match.group(1)) if match else ""
        if " | " in title:
            title = title.split(" | ", 1)[0]
    return title.strip()[:300] or fallback


def reading_payload(row):
    try:
        blocks = json.loads(row["body"] or "[]")
    except (TypeError, ValueError):
        blocks = []
    if not isinstance(blocks, list):
        blocks = []
    clean_blocks = []
    title = clean(row["title"])
    title_key = re.sub(r"\W+", "", title.casefold())
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("kind")
        if kind == "h":  # cached by the previous version of the reader
            kind = "h2"
        if kind not in ("deck", "lead", "p", "h2", "h3", "quote"):
            continue
        text = clean(block.get("text", ""))
        if not text or is_reader_junk(text):
            continue
        # A heading that merely repeats the headline adds noise, not structure.
        if kind in ("h2", "h3") and title_key and re.sub(r"\W+", "", text.casefold()) == title_key:
            continue
        clean_blocks.append({"kind": kind, "text": text})
    words = sum(
        len(block["text"].split())
        for block in clean_blocks
        if block["kind"] in ("p", "lead")
    )
    deck = next((b["text"] for b in clean_blocks if b["kind"] == "deck"), "")
    first_story_text = next(
        (b["text"] for b in clean_blocks if b["kind"] in ("lead", "p")), ""
    )
    # Social descriptions often repeat the opening paragraph. Showing both
    # makes the reader look broken, so keep only the real article paragraph.
    if deck and first_story_text:
        if first_story_text.startswith(deck[:60]) or deck.startswith(first_story_text[:60]):
            deck = ""
    byline = clean(row["byline"])
    if byline and not byline.casefold().startswith("by "):
        byline = "By " + byline
    headings = [
        {"kind": b["kind"], "text": b["text"]}
        for b in clean_blocks
        if b["kind"] in ("h2", "h3")
    ]
    body = [b for b in clean_blocks if b["kind"] != "deck"]
    return {
        "status": row["status"] or "blocked",
        "blocks": body,
        "deck": deck,
        "headings": headings[:12],
        "title": title,
        "byline": byline,
        "image": row["image"],
        "original": row["publisher_link"] or "",
        "minutes": max(1, round(words / 220)),
    }


def resolve_publisher(link, title):
    """The real article URL behind a Google News wrapper, if we can find it."""
    if "news.google.com" not in (link or ""):
        return link
    cached = fetchone("SELECT publisher_link FROM read_cache WHERE link = ?", (link,))
    if cached and cached["publisher_link"]:
        return cached["publisher_link"]
    for publisher, _entry in bing_story_hits(title, limit=2):
        return publisher
    return None


def get_reading(article):
    """Cached in-app reading view for one story."""
    link = article["link"]
    title = clean(article["title"])
    now = int(time.time())
    cached = fetchone("SELECT * FROM read_cache WHERE link = ?", (link,))
    if cached:
        ttl = READ_TTL if (cached["status"] or "") == "full" else READ_SHORT_TTL
        if now - int(cached["fetched"] or 0) < ttl:
            return reading_payload(cached)

    publisher = None
    blocks, total = [], 0
    status = "blocked"
    byline = ""
    deck = ""
    read_title = title
    image = prepare_image(article["image"])
    try:
        publisher = resolve_publisher(link, title)
        if publisher:
            page, final_url = fetch_page(publisher)
            read_title = page_heading(page, title)
            byline = page_byline(page)
            found = prepare_image(meta_value(page, ("og:image", "twitter:image")))
            if found:
                image = found
            deck = clean(meta_value(page, ("og:description", "description")))[:300]
            blocks, total = extract_reading(page)
            if deck and len(deck) >= 40:
                blocks.insert(0, {"kind": "deck", "text": deck})
            status = read_status(blocks, total)
    except Exception as exc:  # noqa: BLE001 - show the summary instead
        safe_link = link.encode("ascii", "backslashreplace").decode("ascii")
        log("Reader failed:", safe_link[:90], type(exc).__name__)

    execute(
        """
        INSERT INTO read_cache
            (link, fetched, status, title, byline, body, image, publisher_link)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(link) DO UPDATE SET
            fetched = excluded.fetched,
            status = excluded.status,
            title = excluded.title,
            byline = excluded.byline,
            body = excluded.body,
            image = COALESCE(excluded.image, read_cache.image),
            publisher_link = COALESCE(excluded.publisher_link, read_cache.publisher_link)
        """,
        (
            link,
            now,
            status,
            read_title,
            byline,
            json.dumps(blocks, ensure_ascii=False),
            image,
            publisher,
        ),
    )
    row = fetchone("SELECT * FROM read_cache WHERE link = ?", (link,))
    return reading_payload(row)


def live_search(query, country, limit=24):
    """Search the web when nothing in the local database matches.

    Results are stored like normal stories, so bookmarks keep working and they
    show up in the archive tabs later.
    """
    key = live_cache_key(query, country)
    now = time.time()
    with live_cache_lock:
        cached = live_cache.get(key)
        if cached and now - cached[0] < 600:
            return list(cached[1])

    entries = web_entries(query, country, limit)[:limit]
    stamp = int(time.time())
    links = []
    seen = set()

    conn = db()
    try:
        for entry in entries:
            link = direct_link(entry.get("link"))
            published = entry_timestamp(entry) or stamp
            if not link or not link.startswith(("http://", "https://")):
                continue
            if link in seen:
                continue
            if published > stamp + 3600:
                published = stamp
            if published < stamp - KEEP_DAYS * 86400:
                continue
            title = clean(entry.get("title", ""))
            source = (entry.get("source") or {}).get("title") or entry.get("news_source")
            if not source and " - " in title:
                head, tail = title.rsplit(" - ", 1)
                if 2 <= len(tail) <= 60:
                    title, source = head, tail
            if not title:
                continue
            seen.add(link)
            insert_article(
                conn,
                link=link,
                title=title,
                summary=entry.get("summary", "") or "",
                source=clean(source) or (urlparse(link).hostname or "Web").removeprefix("www."),
                category=classify(title, entry.get("summary", "") or "", "World"),
                published=published,
                image=get_image(entry) or bing_thumbnail(entry),
                country=None,
            )
            links.append(link)
        commit_retry(conn)
    finally:
        conn.close()

    # Fill in any result that had no thumbnail at all, without blocking.
    if links:
        threading.Thread(
            target=enrich_images, args=(links, 24, 6, None), daemon=True
        ).start()

    with live_cache_lock:
        if len(live_cache) > 80:
            live_cache.clear()
        live_cache[key] = (now, links)
    # The Windows console is not always UTF-8; never let logging break a search.
    safe_query = query.encode("ascii", "backslashreplace").decode("ascii")
    log(f"Live search '{safe_query[:80]}': {len(links)} results")
    return links


# -------------------------------------------------------------- de-duping
STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "at",
    "with", "from", "by", "as", "is", "are", "was", "were", "be", "been",
    "has", "have", "had", "will", "would", "could", "should", "says", "said",
    "after", "before", "over", "under", "into", "about", "new", "live",
    "update", "updates", "video", "watch", "breaking", "news", "its", "it's",
    "this", "that", "his", "her", "their", "our", "your", "you", "not",
}


def story_tokens(title):
    words = re.findall(r"[^\W_]+", title.casefold(), re.UNICODE)
    return {w for w in words if len(w) > 2 and w not in STOPWORDS and not w.isdigit()}


def similar_story(a, b):
    if not a or not b:
        return False
    if a == b:
        return True
    shared = len(a & b)
    if shared < 3:
        return False
    union = len(a | b)
    if union and shared / union >= 0.62:
        return True
    return shared / min(len(a), len(b)) >= 0.8 and shared >= 4


def dedupe_items(items):
    """Merge the same story when several feeds carry it."""
    buckets = {}
    kept = []
    for item in items:
        tokens = story_tokens(item["title"])
        bucket = item["published"] // 86400
        match = None
        for day in (bucket, bucket - 1):
            for other in buckets.get(day, []):
                if abs(item["published"] - other["published"]) > 2 * 86400:
                    continue
                if similar_story(tokens, other["_tokens"]):
                    match = other
                    break
            if match:
                break
        if match:
            match["_sources"].add(item["source"])
            if not match["image"] and item["image"]:
                match["image"] = item["image"]
            if len(item["summary"]) > len(match["summary"]):
                match["summary"] = item["summary"]
                match["summary_html"] = item["summary_html"]
            match["local"] = match["local"] or item["local"]
            continue
        item["_tokens"] = tokens
        item["_sources"] = {item["source"]}
        kept.append(item)
        buckets.setdefault(bucket, []).append(item)

    for item in kept:
        item["also"] = sorted(src for src in item["_sources"] if src != item["source"])[:3]
        item.pop("_tokens", None)
        item.pop("_sources", None)
    return kept


COUNTRY_RX = {}
for _code, _info in COUNTRIES.items():
    _words = [_info["name"]] + list(_info.get("aliases", []))
    COUNTRY_RX[_code] = re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(word) for word in _words) + r")(?!\w)",
        re.I,
    )


def mentions_country(text, code):
    regex = COUNTRY_RX.get(code)
    return bool(regex and regex.search(text))

# ------------------------------------------------------------------- css
BASE_CSS = r"""
:root { color-scheme: light; }
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans",
    "Noto Sans Arabic", "Noto Sans Devanagari", "Noto Sans CJK SC", sans-serif;
  font-size: 16px; line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
button, input, select, textarea { font: inherit; color: inherit; }
a { color: inherit; }
img { max-width: 100%; }
.wrap { max-width: 1120px; margin: 0 auto; padding: 0 18px 70px; }

/* ---------------------------------------------------------- palettes */
body[data-theme="classic"] {
  --bg:#f6f4ee; --card:#fffefb; --text:#1c1917; --muted:#6f6a63;
  --line:#e7e2d8; --chip:#ece7dc; --accent:#b45309; --accent-text:#fff;
}
body[data-theme="ocean"] {
  --bg:#eff6fb; --card:#ffffff; --text:#0c1b26; --muted:#5b7280;
  --line:#dbe9f2; --chip:#e2eef6; --accent:#0369a1; --accent-text:#fff;
}
body[data-theme="forest"] {
  --bg:#f0f7f1; --card:#ffffff; --text:#102014; --muted:#5f7363;
  --line:#dceadf; --chip:#e3f0e5; --accent:#15803d; --accent-text:#fff;
}
body[data-theme="sunset"] {
  --bg:#fff4ef; --card:#ffffff; --text:#2a1512; --muted:#7c625b;
  --line:#f6ded4; --chip:#f8e3da; --accent:#e11d48; --accent-text:#fff;
}
body[data-theme="violet"] {
  --bg:#f5f3ff; --card:#ffffff; --text:#1d1730; --muted:#6d6785;
  --line:#e5e0f5; --chip:#ece8fa; --accent:#7c3aed; --accent-text:#fff;
}
body[data-theme="mono"] {
  --bg:#f4f4f4; --card:#ffffff; --text:#151515; --muted:#6b6b6b;
  --line:#e3e3e3; --chip:#eaeaea; --accent:#171717; --accent-text:#fff;
}
@media (prefers-color-scheme: dark) {
  :root { color-scheme: dark; }
  body[data-theme="classic"] {
    --bg:#12110f; --card:#1c1a17; --text:#f2ede4; --muted:#a8a196;
    --line:#2d2923; --chip:#26221d; --accent:#fbbf24; --accent-text:#201a0d;
  }
  body[data-theme="ocean"] {
    --bg:#0b141b; --card:#121e27; --text:#e6f2fa; --muted:#93a9b8;
    --line:#22323e; --chip:#1a2a35; --accent:#38bdf8; --accent-text:#06202c;
  }
  body[data-theme="forest"] {
    --bg:#0d1510; --card:#142019; --text:#e7f3ea; --muted:#9ab0a1;
    --line:#26372c; --chip:#1b2b21; --accent:#4ade80; --accent-text:#06210f;
  }
  body[data-theme="sunset"] {
    --bg:#180e0c; --card:#241512; --text:#fbeae5; --muted:#c4a59d;
    --line:#3a2420; --chip:#2e1b17; --accent:#fb7185; --accent-text:#2a0a10;
  }
  body[data-theme="violet"] {
    --bg:#120f1b; --card:#1b1626; --text:#eee9fb; --muted:#a99fc4;
    --line:#2c2439; --chip:#241d31; --accent:#a78bfa; --accent-text:#18102e;
  }
  body[data-theme="mono"] {
    --bg:#0f0f0f; --card:#191919; --text:#ededed; --muted:#a0a0a0;
    --line:#2a2a2a; --chip:#232323; --accent:#e5e5e5; --accent-text:#111;
  }
}

/* ------------------------------------------------------------ top bar */
.topbar {
  position: sticky; top: 0; z-index: 20;
  background: color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--line);
}
.topbar-in {
  max-width: 1120px; margin: 0 auto; padding: 10px 18px;
  display: flex; align-items: center; gap: 14px;
}
.brand {
  display: flex; align-items: center; gap: 9px; font-weight: 800;
  font-size: 18px; letter-spacing: -.02em; text-decoration: none;
  white-space: nowrap;
}
.brand .mark {
  width: 28px; height: 28px; border-radius: 9px;
  background: var(--accent); color: var(--accent-text);
  display: grid; place-items: center; font-size: 14px; font-weight: 900;
}
.search {
  flex: 1; min-width: 130px; max-width: 540px;
  display: flex; align-items: center; gap: 6px;
  background: var(--chip); border-radius: 999px; padding: 3px 5px 3px 14px;
}
.search input {
  flex: 1; min-width: 0; border: 0; outline: 0; background: transparent;
  padding: 7px 0; font-size: 14px;
}
.search input::placeholder { color: var(--muted); }
.search button, .iconbtn {
  border: 0; background: var(--accent); color: var(--accent-text);
  width: 32px; height: 32px; border-radius: 50%; display: grid;
  place-items: center; cursor: pointer; flex: none;
}
.search button svg, .iconbtn svg { width: 16px; height: 16px; }
.nav { display: flex; align-items: center; gap: 4px; margin-inline-start: auto; }
.nav a {
  display: flex; align-items: center; gap: 7px; text-decoration: none;
  font-size: 13px; font-weight: 650; padding: 8px 11px; border-radius: 999px;
  color: var(--muted);
}
.nav a:hover, .nav a.active { background: var(--chip); color: var(--text); }
.nav a svg { width: 18px; height: 18px; }
.nav a.avatar {
  width: 36px; height: 36px; padding: 0; border-radius: 50%;
  background: var(--accent); color: var(--accent-text);
  display: grid; place-items: center; font-size: 15px; font-weight: 800;
}

/* -------------------------------------------------------------- hero */
.hero { padding: 26px 0 2px; }
.hero h1 {
  margin: 0; font-size: clamp(22px, 3.2vw, 32px);
  letter-spacing: -.025em; line-height: 1.2;
}
.hero .uname { color: var(--accent); }
.hero .sub { color: var(--muted); font-size: 13.5px; margin-top: 6px; }
.hero .window {
  display: inline-block; margin-inline-start: 6px; padding: 1px 9px;
  border-radius: 999px; background: var(--chip); color: var(--text);
  font-size: 11.5px; font-weight: 700; letter-spacing: .02em;
}

.tools {
  display: flex; gap: 10px; flex-wrap: wrap; align-items: center;
  margin-top: 18px;
}
.seg {
  display: inline-flex; background: var(--chip); border-radius: 999px;
  padding: 3px; max-width: 100%; overflow-x: auto;
}
.seg a {
  padding: 6px 13px; border-radius: 999px; font-size: 12.5px;
  text-decoration: none; white-space: nowrap;
}
.seg a.on { background: var(--accent); color: var(--accent-text); font-weight: 700; }
select.control {
  appearance: none; -webkit-appearance: none; cursor: pointer;
  background: var(--chip) url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'><path d='M1 1l5 5 5-5' fill='none' stroke='%23888' stroke-width='2' stroke-linecap='round'/></svg>") no-repeat right 14px center;
  border: 0; border-radius: 999px; font-size: 13px; font-weight: 650;
  padding: 9px 38px 9px 15px; min-width: 180px;
}
select.control option { background: var(--card); color: var(--text); }
.view-toggle {
  margin-inline-start: auto; display: inline-flex;
  background: var(--chip); border-radius: 999px; padding: 3px;
}
.view-toggle a {
  padding: 6px 11px; border-radius: 999px; text-decoration: none;
  font-size: 12px; font-weight: 650; color: var(--muted);
}
.view-toggle a.on { background: var(--accent); color: var(--accent-text); }
.refresh {
  display: inline-flex; align-items: center; gap: 7px; border: 1px solid var(--line);
  background: var(--card); border-radius: 999px; padding: 8px 14px;
  font-size: 12.5px; font-weight: 700; cursor: pointer;
}
.refresh:hover { border-color: var(--accent); }
.refresh:disabled { opacity: .6; cursor: wait; }
.refresh svg { width: 14px; height: 14px; }
.refresh.spin svg { animation: spin .9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

.day {
  display: flex; align-items: baseline; gap: 8px;
  margin: 28px 0 0; font-size: 18px; letter-spacing: -.01em;
}
.day span { font-size: 12px; font-weight: 500; color: var(--muted); }
.notice {
  margin-top: 20px; padding: 14px 16px; border-radius: 14px;
  background: var(--chip); color: var(--muted); font-size: 13.5px;
}
.empty { text-align: center; color: var(--muted); padding: 56px 0 30px; }
.empty h2 { color: var(--text); margin: 0 0 6px; font-size: 19px; }
.empty p { margin: 0; font-size: 13.5px; }
.empty-actions {
  display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; margin-top: 16px;
}
.empty-actions .btn { font-size: 13px; padding: 9px 15px; }

/* ------------------------------------------------------------- cards */
.grid {
  display: grid; gap: 16px; margin-top: 12px;
  grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
}
.card {
  background: var(--card); border: 1px solid var(--line);
  border-radius: 16px; overflow: hidden; display: flex;
  flex-direction: column; position: relative;
  transition: transform .16s, box-shadow .16s;
}
.card:hover { transform: translateY(-2px); box-shadow: 0 10px 28px rgba(0,0,0,.10); }
.media {
  position: relative; display: block; aspect-ratio: 16 / 9; overflow: hidden;
  text-decoration: none;
  background: linear-gradient(135deg,
    color-mix(in srgb, var(--tc) 28%, var(--card)),
    color-mix(in srgb, var(--tc) 7%, var(--card)));
}
.media img {
  position: absolute; inset: 0; width: 100%; height: 100%;
  object-fit: cover; display: block;
}
.media .ph {
  position: absolute; inset: 0; display: grid; place-items: center;
  font-weight: 900; font-size: 46px; letter-spacing: -.05em;
  color: color-mix(in srgb, var(--tc) 72%, var(--text)); opacity: .55;
}
.local-badge {
  position: absolute; bottom: 8px; inset-inline-start: 8px;
  background: rgba(0,0,0,.66); color: #fff; font-size: 10px;
  font-weight: 800; letter-spacing: .03em; text-transform: uppercase;
  padding: 3px 8px; border-radius: 999px; backdrop-filter: blur(4px);
}
.body { padding: 13px 14px 14px; display: flex; flex-direction: column; gap: 7px; flex: 1; }
.row { display: flex; align-items: center; gap: 8px; }
.badge {
  font-size: 10.5px; font-weight: 800; letter-spacing: .04em;
  text-transform: uppercase; padding: 3px 8px; border-radius: 999px;
  background: color-mix(in srgb, var(--tc) 15%, transparent);
  color: var(--tc); white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; max-width: 100%;
}
.savebtn {
  margin-inline-start: auto; border: 0; background: transparent;
  color: var(--muted); cursor: pointer; padding: 4px; border-radius: 9px;
  display: grid; place-items: center; flex: none;
}
.savebtn:hover { background: var(--chip); color: var(--text); }
.savebtn.on { color: var(--accent); }
.savebtn svg { width: 18px; height: 18px; }
.title {
  font-size: 15.5px; font-weight: 700; line-height: 1.32;
  text-decoration: none; letter-spacing: -.012em;
}
.title:hover { text-decoration: underline; }
.sum {
  margin: 0; font-size: 13px; color: var(--muted); line-height: 1.45;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
  overflow: hidden;
}
.also {
  font-size: 11px; color: var(--muted);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.meta {
  margin-top: auto; padding-top: 5px; display: flex; justify-content: space-between;
  align-items: center; gap: 8px; font-size: 11.5px; color: var(--muted);
}
.meta .src { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.more { color: var(--text); text-decoration: none; font-weight: 700; white-space: nowrap; }
mark {
  background: color-mix(in srgb, var(--accent) 28%, transparent);
  color: inherit; border-radius: 3px; padding: 0 1px;
}

/* compact list view */
.grid.list { grid-template-columns: 1fr; }
.grid.list .card { flex-direction: row; }
.grid.list .media { width: 190px; flex: none; aspect-ratio: auto; min-height: 126px; }
.grid.list .media .ph { font-size: 34px; }
.grid.list .body { padding: 12px 16px; }

@media (max-width: 700px) {
  .wrap { padding: 0 10px 56px; }
  .topbar-in { padding: 8px 10px; gap: 8px; flex-wrap: wrap; }
  .brand { font-size: 16px; }
  .brand .mark { width: 26px; height: 26px; }
  .search { order: 3; flex-basis: 100%; max-width: none; }
  .nav { gap: 0; }
  .nav a span { display: none; }
  .nav a { padding: 8px; }
  .nav a.avatar { width: 34px; height: 34px; }
  .hero { padding-top: 16px; }
  .hero h1 { font-size: 22px; }
  .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; margin-top: 8px; }
  .card { border-radius: 12px; }
  .body { padding: 9px 10px 10px; gap: 5px; }
  .title { font-size: 12.5px; line-height: 1.3; }
  .sum, .also, .more { display: none; }
  .badge { font-size: 8.5px; padding: 2px 6px; }
  .savebtn svg { width: 15px; height: 15px; }
  .meta { font-size: 9.5px; padding-top: 2px; }
  .media .ph { font-size: 30px; }
  .day { margin-top: 18px; font-size: 15px; }
  .seg a { padding: 6px 10px; font-size: 11.5px; }
  .view-toggle { display: none; }
  select.control { min-width: 0; flex: 1; }
  .grid.list .media { width: 112px; min-height: 92px; }
  .grid.list .media .ph { font-size: 26px; }
  .grid.list .title { font-size: 13px; }
  .grid.list .body { padding: 10px; }
}

/* -------------------------------------------------------- forms/auth */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  border: 0; border-radius: 12px; background: var(--accent);
  color: var(--accent-text); font-weight: 750; font-size: 14px;
  padding: 11px 18px; cursor: pointer; text-decoration: none;
}
.btn:hover { filter: brightness(1.06); }
.btn.ghost {
  background: var(--chip); color: var(--text); border: 1px solid var(--line);
}
.field { display: flex; flex-direction: column; gap: 6px; margin-top: 14px; }
.field > label { font-size: 12.5px; font-weight: 700; color: var(--muted); }
.field input, .field select {
  width: 100%; border: 1px solid var(--line); background: var(--card);
  border-radius: 12px; padding: 11px 13px; outline: none; font-size: 14.5px;
}
.field input:focus, .field select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent);
}
.pw { position: relative; }
.pw input { padding-inline-end: 64px; }
.pw-toggle {
  position: absolute; inset-inline-end: 6px; top: 50%; transform: translateY(-50%);
  border: 0; background: transparent; color: var(--muted);
  font-size: 12px; font-weight: 750; cursor: pointer; padding: 6px;
}
.pw-toggle:hover { color: var(--text); }
.check {
  display: flex; align-items: flex-start; gap: 9px; margin-top: 16px;
  font-size: 13.5px; cursor: pointer;
}
.check input { margin-top: 3px; accent-color: var(--accent); }
.hint { color: var(--muted); font-size: 11.5px; margin-top: 5px; }
.auth-wrap { min-height: 72vh; display: grid; place-items: center; padding: 22px 0; }
.auth-card {
  width: 100%; max-width: 430px; background: var(--card);
  border: 1px solid var(--line); border-radius: 22px; padding: 26px;
  box-shadow: 0 18px 50px rgba(0,0,0,.08);
}
.auth-brand {
  display: flex; align-items: center; gap: 9px; font-weight: 800;
  font-size: 16px; text-decoration: none; margin-bottom: 22px;
}
.auth-card h1 { margin: 0; font-size: 24px; letter-spacing: -.02em; }
.auth-sub { color: var(--muted); font-size: 13.5px; margin: 7px 0 0; }
.auth-switch {
  margin: 20px 0 0; text-align: center; font-size: 13.5px; color: var(--muted);
}
.auth-switch a { color: var(--text); font-weight: 700; text-decoration: none; }
.auth-switch a:hover { text-decoration: underline; }
.auth-card .btn { width: 100%; margin-top: 20px; }
.flashes { margin-top: 16px; display: grid; gap: 8px; }
.flash {
  padding: 10px 13px; border-radius: 12px; font-size: 13px;
  border: 1px solid var(--line); background: var(--card);
}
.flash.error { border-color: color-mix(in srgb, #ef4444 45%, var(--line)); }
.flash.ok { border-color: color-mix(in srgb, #10b981 45%, var(--line)); }

/* ------------------------------------------------------- profile/auth */
.panels { display: grid; gap: 16px; max-width: 780px; margin-top: 20px; }
.panel {
  background: var(--card); border: 1px solid var(--line);
  border-radius: 18px; padding: 20px;
}
.panel h2 { margin: 0; font-size: 16px; letter-spacing: -.01em; }
.panel .desc { color: var(--muted); font-size: 12.5px; margin: 4px 0 0; }
.panel form { margin: 0; }
.panel .btn { margin-top: 16px; }
.panel .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.profile-head {
  display: flex; align-items: center; gap: 15px; margin-top: 20px;
  padding: 18px; background: var(--card); border: 1px solid var(--line);
  border-radius: 18px;
}
.avatar-lg {
  width: 58px; height: 58px; border-radius: 50%; flex: none;
  background: var(--accent); color: var(--accent-text);
  display: grid; place-items: center; font-size: 24px; font-weight: 900;
}
.profile-head h1 { margin: 0; font-size: 21px; }
.profile-head p { margin: 3px 0 0; color: var(--muted); font-size: 13px; }
.pill {
  display: inline-block; padding: 2px 9px; border-radius: 999px;
  background: var(--chip); color: var(--muted); font-size: 11.5px;
  font-weight: 650; margin-inline-start: 6px;
}
.theme-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(92px, 1fr));
  gap: 8px; margin-top: 10px;
}
.theme-opt { position: relative; cursor: pointer; }
.theme-opt input { position: absolute; opacity: 0; pointer-events: none; }
.theme-opt .swatch {
  display: block; text-align: center; padding: 9px 6px; border-radius: 11px;
  border: 2px solid var(--line); font-size: 11.5px; font-weight: 700;
  background: var(--card);
}
.theme-opt .swatch::before {
  content: ""; display: block; width: 18px; height: 18px; border-radius: 50%;
  margin: 0 auto 5px; background: var(--sw, #888);
}
.theme-opt input:checked + .swatch { border-color: var(--accent); }
.theme-opt[data-t="classic"] .swatch { --sw:#b45309; }
.theme-opt[data-t="ocean"] .swatch { --sw:#0369a1; }
.theme-opt[data-t="forest"] .swatch { --sw:#15803d; }
.theme-opt[data-t="sunset"] .swatch { --sw:#e11d48; }
.theme-opt[data-t="violet"] .swatch { --sw:#7c3aed; }
.theme-opt[data-t="mono"] .swatch { --sw:#555; }
.lang-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px; margin-top: 18px;
}
.lang-opt { position: relative; cursor: pointer; }
.lang-opt input { position: absolute; opacity: 0; pointer-events: none; }
.lang-opt span {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 14px 15px; border-radius: 14px; border: 2px solid var(--line);
  background: var(--card); font-weight: 650; font-size: 14.5px;
}
.lang-opt small { color: var(--muted); font-size: 10.5px; font-weight: 700; }
.lang-opt input:checked + span {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 8%, var(--card));
}
.welcome-card { max-width: 680px; margin: 30px auto 0; }
.welcome-card .btn { margin-top: 22px; }
@media (max-width: 560px) {
  .panel .row2 { grid-template-columns: 1fr; }
  .profile-head { align-items: flex-start; }
}
/* in-app reader */
.reader-progress { position: fixed; inset-inline: 0; top: 0; height: 3px; z-index: 40; pointer-events: none; }
.reader-progress i { display: block; height: 100%; width: 0; background: var(--accent); transition: width .12s linear; }
.reader { max-width: 760px; margin: 6px auto 70px; }
.reader-bar { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 4px 0 14px; }
.rback { text-decoration: none; color: var(--muted); font-weight: 750; font-size: 13.5px; padding: 6px 2px; }
.rback:hover { color: var(--text); }
.rtools { display: flex; align-items: center; gap: 6px; }
.rfont, .rsave {
  border: 1px solid var(--line); background: var(--card); color: var(--text);
  border-radius: 10px; padding: 6px 10px; font-size: 13px; font-weight: 700;
  cursor: pointer; line-height: 1.2;
}
.rfont:hover, .rsave:hover { border-color: var(--accent); }
.rsave.on { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 14%, var(--card)); }
.reader-head h1 {
  font-size: clamp(26px, 4.6vw, 40px); line-height: 1.14;
  letter-spacing: -.025em; margin: 8px 0 10px; font-weight: 850;
}
.rkicker {
  display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px;
  font-size: 11.5px; font-weight: 800; letter-spacing: .06em;
  text-transform: uppercase; color: var(--muted);
}
.rkicker .dot { opacity: .55; }
.rby { color: var(--muted); font-size: 14px; margin: 0 0 6px; }
.rhero { margin: 18px 0 10px; border-radius: 16px; overflow: hidden; background: var(--chip); }
.rhero img { display: block; width: 100%; height: auto; max-height: 460px; object-fit: cover; }
.read-body {
  font-family: Georgia, "Times New Roman", "Noto Serif", "Noto Naskh Arabic", serif;
  font-size: var(--read-size, 19px); line-height: 1.74; margin-top: 18px;
}
.read-body p { margin: 0 0 1.05em; }
.read-body h2 { font-size: 1.14em; line-height: 1.3; margin: 1.55em 0 .5em; letter-spacing: -.01em; }
.rlead { font-weight: 600; }
.rnote {
  margin: 18px 0 4px; padding: 12px 14px; border: 1px solid var(--line);
  border-radius: 12px; background: var(--card); color: var(--muted);
  font-family: system-ui, sans-serif; font-size: 13.5px; line-height: 1.5;
}
.rfoot { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 28px; padding-top: 18px; border-top: 1px solid var(--line); }
.rfoot .btn { text-decoration: none; }
.reader[dir="rtl"] .read-body { font-family: "Noto Naskh Arabic", "Segoe UI", Tahoma, sans-serif; line-height: 1.9; }
@media (max-width: 560px) {
  .reader { margin-top: 0; }
  .reader-head h1 { font-size: 27px; }
  .rhero { margin-inline: -10px; border-radius: 0; max-height: 300px; }
  .read-body { font-size: var(--read-size, 18px); }
}
"""

BASE_JS = r"""
function toast(message) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = message;
  el.style.display = "block";
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => { el.style.display = "none"; }, 2400);
}
function csrf() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.content : "";
}
// Press "/" to jump to the search box (Esc leaves it).
document.addEventListener("keydown", (event) => {
  const active = document.activeElement;
  const typing = active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName);
  if (event.key === "/" && !typing && !event.ctrlKey && !event.metaKey) {
    const box = document.querySelector('input[name="q"]');
    if (box) { event.preventDefault(); box.focus(); box.select(); }
  }
});
// Broken image: try the local proxy, then ask the server to find a fresh one.
async function imgFail(img) {
  const stage = img.dataset.stage || "";
  if (stage === "") {
    img.dataset.stage = "proxy";
    const original = img.dataset.orig || img.currentSrc || img.src;
    img.src = "/img?u=" + encodeURIComponent(original);
    return;
  }
  if (stage === "proxy") {
    img.dataset.stage = "remote";
    const link = img.dataset.link;
    if (!link) { img.remove(); return; }
    try {
      const res = await fetch("/api/image", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
        body: JSON.stringify({ link })
      });
      if (res.ok) {
        const data = await res.json();
        if (data.image && data.image !== (img.dataset.orig || "")) {
          img.dataset.orig = data.image;
          img.src = data.image;
          return;
        }
      }
    } catch (err) { /* fall through to the letter placeholder */ }
  }
  img.remove();
}
async function toggleSave(btn) {
  const link = btn.dataset.link;
  if (!link) return;
  btn.disabled = true;
  try {
    const res = await fetch("/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify({ link })
    });
    if (!res.ok) throw new Error("save failed");
    const data = await res.json();
    btn.classList.toggle("on", !!data.saved);
    btn.setAttribute("aria-pressed", data.saved ? "true" : "false");
    document.querySelectorAll(".savebtn").forEach((other) => {
      if (other.dataset.link !== link) return;
      other.classList.toggle("on", !!data.saved);
      other.setAttribute("aria-pressed", data.saved ? "true" : "false");
      const icon = other.querySelector("svg");
      if (icon) icon.setAttribute("fill", data.saved ? "currentColor" : "none");
      if (other.dataset.saveLabel) {
        const label = data.saved ? other.dataset.unsaveLabel : other.dataset.saveLabel;
        other.textContent = label;
        other.title = label;
      }
    });
    const toastEl = document.getElementById("toast");
    toast(data.saved ? toastEl.dataset.saved : toastEl.dataset.unsaved);
    if (!data.saved && document.body.dataset.page === "saved") {
      const card = btn.closest(".card");
      if (card) card.remove();
      if (!document.querySelector(".card")) location.reload();
    }
  } catch (err) {
    const toastEl = document.getElementById("toast");
    toast(toastEl.dataset.error);
  } finally {
    btn.disabled = false;
  }
}
async function doRefresh() {
  const btn = document.getElementById("refresh");
  const label = btn ? btn.querySelector("span") : null;
  if (btn) { btn.disabled = true; btn.classList.add("spin"); }
  if (label) label.textContent = label.dataset.busy || label.textContent;
  try {
    const res = await fetch("/refresh", {
      method: "POST",
      headers: { "X-CSRF-Token": csrf() }
    });
    if (!res.ok) throw new Error("refresh failed");
    location.reload();
  } catch (err) {
    toast(document.getElementById("toast").dataset.error);
    if (btn) { btn.disabled = false; btn.classList.remove("spin"); }
  }
}
document.addEventListener("click", function (event) {
  const toggle = event.target.closest(".pw-toggle");
  if (!toggle) return;
  const input = document.getElementById(toggle.dataset.target);
  if (!input) return;
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  toggle.textContent = show ? toggle.dataset.hide : toggle.dataset.show;
});
"""

# --------------------------------------------------------------- templates
TEMPLATES["base.html"] = r"""
<!doctype html>
<html lang="{{ lang }}" dir="{{ 'rtl' if rtl else 'ltr' }}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="{{ csrf_token() }}">
<title>{% block title %}{{ t('app_name') }}{% endblock %}</title>
<style>{{ base_css }}</style>
{% block head %}{% endblock %}
</head>
<body data-theme="{{ theme }}" data-page="{% block page %}home{% endblock %}">
{% block header %}
<header class="topbar">
  <div class="topbar-in">
    <a class="brand" href="{{ url_for('home') }}"><span class="mark">N</span>{{ t('app_name') }}</a>
    <form class="search" action="{{ url_for('home') }}" method="get" role="search">
      <input type="search" name="q" value="{{ request.args.get('q', '') }}"
             placeholder="{{ t('search_placeholder') }}" aria-label="{{ t('search') }}">
      <input type="hidden" name="range" value="{{ request.args.get('range', '24h') }}">
      <input type="hidden" name="category" value="{{ request.args.get('category', 'All') }}">
      {% if request.args.get('view') %}<input type="hidden" name="view" value="{{ request.args.get('view') }}">{% endif %}
      <button type="submit" aria-label="{{ t('search') }}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
          <circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.6-3.6"></path>
        </svg>
      </button>
    </form>
    <nav class="nav">
      <a href="{{ url_for('saved') }}" class="{{ 'active' if request.endpoint == 'saved' }}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round">
          <path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z"></path>
        </svg><span>{{ t('saved') }}</span>
      </a>
      <a href="{{ url_for('profile') }}" class="{{ 'active' if request.endpoint in ('profile', 'settings') }}" title="{{ t('settings') }}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
          <circle cx="12" cy="12" r="3"></circle>
          <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"></path>
        </svg><span>{{ t('settings') }}</span>
      </a>
      {% if user %}
      <a class="avatar" href="{{ url_for('profile') }}" title="{{ user.username }}">{{ user.username[0]|upper }}</a>
      {% endif %}
    </nav>
  </div>
</header>
{% endblock %}

<main class="wrap">
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
      <div class="flashes">
        {% for category, message in messages %}
          <div class="flash {{ category }}">{{ message }}</div>
        {% endfor %}
      </div>
    {% endif %}
  {% endwith %}
  {% block content %}{% endblock %}
</main>

<div id="toast"
     data-saved="{{ t('toast_saved') }}"
     data-unsaved="{{ t('toast_unsaved') }}"
     data-error="{{ t('err_generic') }}"></div>
<script>{{ base_js }}</script>
{% block scripts %}{% endblock %}
</body>
</html>
"""

TEMPLATES["macros.html"] = r"""
{% macro card(a) %}
<article class="card" style="--tc: {{ a.color }}">
  <a class="media" href="{{ url_for('read_article', u=a.link) }}">
    <span class="ph">{{ a.initial }}</span>
    {% if a.image %}
      <img src="{{ a.image }}" alt="" loading="lazy" decoding="async"
           data-orig="{{ a.image }}" data-link="{{ a.link }}"
           referrerpolicy="no-referrer" onerror="imgFail(this)">
    {% endif %}
    {% if a.local %}<span class="local-badge">{{ t('local') }}</span>{% endif %}
  </a>
  <div class="body">
    <div class="row">
      <span class="badge">{{ a.topic }}</span>
      <button class="savebtn {{ 'on' if a.saved }}" type="button"
              data-link="{{ a.link }}" onclick="toggleSave(this)"
              aria-pressed="{{ 'true' if a.saved else 'false' }}"
              title="{{ t('unsave') if a.saved else t('save') }}">
        <svg viewBox="0 0 24 24" fill="{{ 'currentColor' if a.saved else 'none' }}"
             stroke="currentColor" stroke-width="2" stroke-linejoin="round">
          <path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z"></path>
        </svg>
      </button>
    </div>
    <a class="title" href="{{ url_for('read_article', u=a.link) }}">{{ a.title_html }}</a>
    {% if a.summary %}<p class="sum">{{ a.summary_html }}</p>{% endif %}
    {% if a.also %}<div class="also">{{ t('also') }}: {{ a.also|join(', ') }}</div>{% endif %}
    <div class="meta">
      <span class="src">{{ a.source }} · {{ a.ago }}</span>
      <a class="more" href="{{ url_for('read_article', u=a.link) }}">{{ t('read') }} →</a>
    </div>
  </div>
</article>
{% endmacro %}
"""

TEMPLATES["read.html"] = r"""
{% extends "base.html" %}
{% block title %}{{ reader.title or article.title }} · {{ t('app_name') }}{% endblock %}
{% block page %}read{% endblock %}
{% block content %}
<div class="reader-progress" aria-hidden="true"><i id="readProgress"></i></div>
<article class="reader" dir="{{ 'rtl' if rtl_article else 'ltr' }}">
  <div class="reader-bar">
    <a class="rback" href="{{ url_for('home') }}" onclick="return readerBack(event)">← {{ t('reader_back') }}</a>
    <div class="rtools">
      <button type="button" class="rfont" onclick="readerFont(-1)" title="{{ t('font_smaller') }}" aria-label="{{ t('font_smaller') }}">A−</button>
      <button type="button" class="rfont" onclick="readerFont(1)" title="{{ t('font_larger') }}" aria-label="{{ t('font_larger') }}">A+</button>
      <button type="button" class="savebtn rsave {{ 'on' if saved }}" data-link="{{ article.link }}"
              data-save-label="{{ t('save') }}" data-unsave-label="{{ t('unsave') }}"
              onclick="toggleSave(this)" aria-pressed="{{ 'true' if saved else 'false' }}">{{ t('unsave') if saved else t('save') }}</button>
    </div>
  </div>

  <header class="reader-head">
    <div class="rkicker">
      <span>{{ article.source }}</span><span class="dot">·</span><span>{{ ago }}</span>
      <span class="dot">·</span><span>{{ t('reading_time', n=reader.minutes) }}</span>
    </div>
    <h1>{{ reader.title or article.title }}</h1>
    {% if reader.byline %}<p class="rby">{{ reader.byline }}</p>{% endif %}
  </header>

  {% if reader.image %}
  <figure class="rhero">
    <img src="{{ url_for('img_proxy', u=reader.image) }}" alt=""
         referrerpolicy="no-referrer" onerror="this.closest('figure').remove()">
  </figure>
  {% endif %}

  <div class="read-body">
    {% if reader.status == 'blocked' and article.summary %}
      <p class="rlead">{{ article.summary }}</p>
    {% endif %}
    {% for block in reader.blocks %}
      {% if block.kind == 'h' %}<h2>{{ block.text }}</h2>{% else %}<p>{{ block.text }}</p>{% endif %}
    {% endfor %}
  </div>

  {% if reader.status != 'full' %}
  <div class="rnote">{{ t('read_blocked') if reader.status == 'blocked' else t('read_partial') }}</div>
  {% endif %}

  <div class="rfoot">
    <a class="btn" href="{{ reader.original or article.link }}" target="_blank" rel="noopener">{{ t('original') }} ↗</a>
    <a class="btn ghost" href="{{ url_for('home') }}">{{ t('reader_back') }}</a>
  </div>
</article>
{% endblock %}
{% block scripts %}
<script>
function readerBack(event) {
  if (history.length > 1) { event.preventDefault(); history.back(); return false; }
  return true;
}
function readerFont(delta) {
  const art = document.querySelector('.reader');
  if (!art) return;
  const current = parseFloat(localStorage.getItem('readerSize') || '19');
  const next = Math.min(30, Math.max(15, current + delta));
  localStorage.setItem('readerSize', String(next));
  art.style.setProperty('--read-size', next + 'px');
}
(function () {
  const art = document.querySelector('.reader');
  const saved = parseFloat(localStorage.getItem('readerSize') || '19');
  if (art && saved) art.style.setProperty('--read-size', saved + 'px');
  const bar = document.getElementById('readProgress');
  if (!bar) return;
  const tick = function () {
    const doc = document.documentElement;
    const max = doc.scrollHeight - doc.clientHeight;
    bar.style.width = max > 0 ? Math.min(100, (doc.scrollTop / max) * 100) + '%' : '0%';
  };
  document.addEventListener('scroll', tick, { passive: true });
  window.addEventListener('resize', tick);
  tick();
})();
</script>
{% endblock %}
"""

TEMPLATES["auth.html"] = r"""
{% extends "base.html" %}
{% block title %}{{ t('signup') if mode == 'signup' else t('login') }} · {{ t('app_name') }}{% endblock %}
{% block page %}auth{% endblock %}
{% block header %}{% endblock %}
{% block content %}
<div class="auth-wrap">
  <div class="auth-card">
    <a class="auth-brand" href="{{ url_for('login') }}"><span class="brand"><span class="mark">N</span>{{ t('app_name') }}</span></a>
    <h1>{{ t('signup_title') if mode == 'signup' else t('login_title') }}</h1>
    <p class="auth-sub">{{ t('signup_sub') if mode == 'signup' else t('login_sub') }}</p>

    <form method="post"
          action="{{ url_for('signup') if mode == 'signup' else url_for('login', next=request.args.get('next')) }}">
      <input type="hidden" name="_csrf" value="{{ csrf_token() }}">

      <div class="field">
        <label for="username">{{ t('username') }}</label>
        <input id="username" name="username" type="text" required
               maxlength="32" autocomplete="username" autocapitalize="words"
               value="{{ form.get('username', '') }}">
      </div>

      {% if mode == 'signup' %}
      <div class="field">
        <label for="country">{{ t('country') }}</label>
        <select id="country" name="country" required>
          <option value="" disabled {{ 'selected' if not form.get('country') }}>—</option>
          {% for code, name in COUNTRY_OPTIONS %}
            <option value="{{ code }}" {{ 'selected' if form.get('country') == code }}>{{ name }}</option>
          {% endfor %}
        </select>
      </div>
      {% endif %}

      <div class="field">
        <label for="password">{{ t('password') }}</label>
        <div class="pw">
          <input id="password" name="password" type="password" required
                 minlength="8" autocomplete="{{ 'new-password' if mode == 'signup' else 'current-password' }}">
          <button type="button" class="pw-toggle" data-target="password"
                  data-show="{{ t('show') }}" data-hide="{{ t('hide') }}">{{ t('show') }}</button>
        </div>
        {% if mode == 'signup' %}<div class="hint">{{ t('password_hint') }}</div>{% endif %}
      </div>

      {% if mode == 'signup' %}
      <div class="field">
        <label for="confirm">{{ t('confirm_password') }}</label>
        <div class="pw">
          <input id="confirm" name="confirm" type="password" required
                 minlength="8" autocomplete="new-password">
          <button type="button" class="pw-toggle" data-target="confirm"
                  data-show="{{ t('show') }}" data-hide="{{ t('hide') }}">{{ t('show') }}</button>
        </div>
      </div>
      {% endif %}

      <label class="check">
        <input type="checkbox" name="remember" value="1" checked>
        <span>{{ t('remember') }}<span class="hint" style="display:block">{{ t('remember_hint') }}</span></span>
      </label>

      <button class="btn" type="submit">{{ t('create_account') if mode == 'signup' else t('sign_in') }}</button>
    </form>

    <p class="auth-switch">
      {% if mode == 'signup' %}
        {{ t('have_account') }} <a href="{{ url_for('login') }}">{{ t('sign_in') }}</a>
      {% else %}
        {{ t('no_account') }} <a href="{{ url_for('signup') }}">{{ t('create_account') }}</a>
      {% endif %}
    </p>
  </div>
</div>
{% endblock %}
"""

TEMPLATES["home.html"] = r"""
{% extends "base.html" %}
{% from "macros.html" import card with context %}
{% block title %}{{ t('app_name') }}{% endblock %}
{% block page %}home{% endblock %}
{% block head %}{% if booting %}<meta http-equiv="refresh" content="7">{% endif %}{% endblock %}
{% block content %}
<section class="hero">
  <h1>{{ t(greeting) }}, <span class="uname">{{ user.username }}</span></h1>
  <p class="sub">
    {% if q %}
      {{ t('results_for', q=q) }} · {{ shown }}
      {% if web %} · {{ t('web_results') }}{% endif %}
    {% else %}
      {{ t('articles_past', n=shown, range=label) }}
      <span class="window">{{ window }}</span>
      {% if updated %} · {{ t('updated', time=updated) }}{% endif %}
    {% endif %}
  </p>
</section>

<div class="tools">
  <div class="seg">
    {% for key in ranges %}
      <a href="{{ url_for('home', range=key, category=category, q=q or None, view=view) }}"
         class="{{ 'on' if key == rng }}">{{ t('range_' + key) }}</a>
    {% endfor %}
  </div>

  <select class="control" aria-label="{{ t('all_topics') }}"
          onchange="if (this.value) location.href = this.value">
    {% for name, n in counts %}
      <option value="{{ url_for('home', range=rng, category=name, q=q or None, view=view) }}"
              {{ 'selected' if name == category }}>
        {{ t('all_topics') if name == 'All' else name }} ({{ n }})
      </option>
    {% endfor %}
  </select>

  <div class="view-toggle" aria-label="{{ t('layout') }}">
    <a href="{{ url_for('home', range=rng, category=category, q=q or None, view='grid') }}"
       class="{{ 'on' if view == 'grid' }}">{{ t('grid') }}</a>
    <a href="{{ url_for('home', range=rng, category=category, q=q or None, view='list') }}"
       class="{{ 'on' if view == 'list' }}">{{ t('list') }}</a>
  </div>

  <button class="refresh" id="refresh" type="button" onclick="doRefresh()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"
         stroke-linecap="round" stroke-linejoin="round">
      <path d="M21 12a9 9 0 1 1-3-6.7"></path><path d="M21 3v6h-6"></path>
    </svg>
    <span data-busy="{{ t('refreshing') }}">{{ t('refresh') }}</span>
  </button>
</div>

{% if booting %}
  <div class="notice">{{ t('loading_news') }}</div>
{% endif %}
{% if widened %}
  <div class="notice">{{ t('search_wide', range=label) }}</div>
{% endif %}
{% if web %}
  <div class="notice">{{ t('search_web') }}</div>
{% endif %}
{% if search_failed %}
  <div class="notice">{{ t('search_fail') }}</div>
{% endif %}

{% if q %}
  {% if items %}
    <div class="grid {{ view }}">
      {% for a in items %}{{ card(a) }}{% endfor %}
    </div>
  {% else %}
    <div class="empty">
      <h2>{{ t('search_empty_title', q=q) }}</h2>
      <p>{{ t('search_empty_sub') }}</p>
      <p class="empty-actions">
        <a class="btn ghost" href="{{ url_for('home') }}">{{ t('clear') }}</a>
        <a class="btn ghost" href="https://news.google.com/search?q={{ q|urlencode }}"
           target="_blank" rel="noopener">Google News</a>
      </p>
    </div>
  {% endif %}
{% elif groups %}
  {% for day, group in groups %}
    <h2 class="day">{{ day }} <span>{{ group|length }}</span></h2>
    <div class="grid {{ view }}">
      {% for a in group %}{{ card(a) }}{% endfor %}
    </div>
  {% endfor %}
{% else %}
  <div class="empty">
    <h2>{{ t('empty_title') }}</h2>
    <p>{{ t('empty_sub') }}</p>
  </div>
{% endif %}
{% endblock %}
"""

TEMPLATES["welcome.html"] = r"""
{% extends "base.html" %}
{% block title %}{{ t('welcome_title') }} · {{ t('app_name') }}{% endblock %}
{% block page %}welcome{% endblock %}
{% block content %}
<div class="welcome-card">
  <section class="hero" style="padding-top:10px">
    <h1>{{ t('welcome_title') }}</h1>
    <p class="sub">{{ t('welcome_sub') }}</p>
  </section>
  <form method="post" action="{{ url_for('welcome') }}">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <div class="lang-grid">
      {% for code, name in LANGUAGES.items() %}
        <label class="lang-opt">
          <input type="radio" name="language" value="{{ code }}" {{ 'checked' if code == lang }}>
          <span>{{ name }}<small>{{ code|upper }}</small></span>
        </label>
      {% endfor %}
    </div>
    <button class="btn" type="submit">{{ t('continue') }}</button>
  </form>
</div>
{% endblock %}
"""

TEMPLATES["profile.html"] = r"""
{% extends "base.html" %}
{% block title %}{{ t('profile_title') }} · {{ t('app_name') }}{% endblock %}
{% block page %}profile{% endblock %}
{% block content %}
<div class="profile-head">
  <div class="avatar-lg">{{ user.username[0]|upper }}</div>
  <div>
    <h1>{{ user.username }}</h1>
    <p>{{ country_name }} <span class="pill">{{ t('saved_count', n=saved_count) }}</span></p>
    <p>{{ t('member_since', date=member_since) }}</p>
  </div>
</div>

<div class="panels">
  <section class="panel" id="account">
    <h2>{{ t('account') }}</h2>
    <p class="desc">{{ t('username') }}</p>
    <form method="post" action="{{ url_for('profile_username') }}">
      <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
      <div class="field">
        <label for="new-username">{{ t('username') }}</label>
        <input id="new-username" name="username" type="text" required
               maxlength="32" autocomplete="username" value="{{ user.username }}">
      </div>
      <button class="btn" type="submit">{{ t('update_username') }}</button>
    </form>
  </section>

  <section class="panel" id="settings">
    <h2>{{ t('preferences') }}</h2>
    <p class="desc">{{ t('profile_sub') }}</p>
    <form method="post" action="{{ url_for('profile_prefs') }}">
      <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
      <div class="row2">
        <div class="field">
          <label for="pref-country">{{ t('country') }}</label>
          <select id="pref-country" name="country">
            {% for code, name in COUNTRY_OPTIONS %}
              <option value="{{ code }}" {{ 'selected' if user.country == code }}>{{ name }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="field">
          <label for="pref-language">{{ t('language') }}</label>
          <select id="pref-language" name="language">
            {% for code, name in LANGUAGES.items() %}
              <option value="{{ code }}" {{ 'selected' if user.language == code }}>{{ name }}</option>
            {% endfor %}
          </select>
        </div>
      </div>
      <div class="field">
        <label>{{ t('theme') }}</label>
        <div class="theme-grid">
          {% for key in THEMES %}
            <label class="theme-opt" data-t="{{ key }}">
              <input type="radio" name="theme" value="{{ key }}" {{ 'checked' if user.theme == key }}>
              <span class="swatch">{{ t('theme_' + key) }}</span>
            </label>
          {% endfor %}
        </div>
      </div>
      <div class="field">
        <label for="pref-view">{{ t('layout') }}</label>
        <select id="pref-view" name="view_mode">
          <option value="grid" {{ 'selected' if user.view_mode == 'grid' }}>{{ t('grid') }}</option>
          <option value="list" {{ 'selected' if user.view_mode == 'list' }}>{{ t('list') }}</option>
        </select>
      </div>
      <button class="btn" type="submit">{{ t('save_changes') }}</button>
    </form>
  </section>

  <section class="panel" id="password">
    <h2>{{ t('security') }}</h2>
    <p class="desc">{{ t('profile_title') }}</p>
    <form method="post" action="{{ url_for('profile_password') }}">
      <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
      <div class="field">
        <label for="pw-current">{{ t('current_password') }}</label>
        <div class="pw">
          <input id="pw-current" name="current_password" type="password" required autocomplete="current-password">
          <button type="button" class="pw-toggle" data-target="pw-current"
                  data-show="{{ t('show') }}" data-hide="{{ t('hide') }}">{{ t('show') }}</button>
        </div>
      </div>
      <div class="row2">
        <div class="field">
          <label for="pw-new">{{ t('new_password') }}</label>
          <div class="pw">
            <input id="pw-new" name="new_password" type="password" required minlength="8" autocomplete="new-password">
            <button type="button" class="pw-toggle" data-target="pw-new"
                    data-show="{{ t('show') }}" data-hide="{{ t('hide') }}">{{ t('show') }}</button>
          </div>
        </div>
        <div class="field">
          <label for="pw-confirm">{{ t('confirm_password') }}</label>
          <div class="pw">
            <input id="pw-confirm" name="confirm" type="password" required minlength="8" autocomplete="new-password">
            <button type="button" class="pw-toggle" data-target="pw-confirm"
                    data-show="{{ t('show') }}" data-hide="{{ t('hide') }}">{{ t('show') }}</button>
          </div>
        </div>
      </div>
      <div class="hint">{{ t('password_hint') }}</div>
      <button class="btn" type="submit">{{ t('update_password') }}</button>
    </form>
  </section>

  <section class="panel">
    <h2>{{ t('saved') }}</h2>
    <p class="desc">{{ t('saved_sub', n=saved_count) }}</p>
    <p style="margin-bottom:0">
      <a class="btn ghost" href="{{ url_for('saved') }}">{{ t('saved_title') }}</a>
    </p>
  </section>

  <section class="panel">
    <h2>{{ t('logout') }}</h2>
    <form method="post" action="{{ url_for('logout') }}">
      <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
      <button class="btn ghost" type="submit">{{ t('logout') }}</button>
    </form>
  </section>
</div>
{% endblock %}
"""

TEMPLATES["saved.html"] = r"""
{% extends "base.html" %}
{% from "macros.html" import card with context %}
{% block title %}{{ t('saved_title') }} · {{ t('app_name') }}{% endblock %}
{% block page %}saved{% endblock %}
{% block content %}
<section class="hero">
  <h1>{{ t('saved_title') }}</h1>
  <p class="sub">{{ t('saved_sub', n=shown) }}</p>
</section>
{% if items %}
  <div class="grid {{ view }}">
    {% for a in items %}{{ card(a) }}{% endfor %}
  </div>
{% else %}
  <div class="empty">
    <h2>{{ t('saved_empty') }}</h2>
    <p><a class="btn ghost" href="{{ url_for('home') }}">{{ t('read') }}</a></p>
  </div>
{% endif %}
{% endblock %}
"""

# ------------------------------------------------------------------ routes
def greeting_key():
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "greeting_morning"
    if 12 <= hour < 17:
        return "greeting_afternoon"
    if 17 <= hour < 22:
        return "greeting_evening"
    return "greeting_night"


def build_item(row, country, saved_links, query=""):
    title = clean(row["title"])
    summary = clean(row["summary"])
    published = min(int(row["published"] or 0), int(time.time()))
    ingested = max(int(row["ingested"] or 0), published)
    topic = classify(title, summary, row["category"])
    source = clean(row["source"]) or "News"
    local = bool(country and row["country"] == country)
    if not local and not row["country"]:
        local = mentions_country(title + " " + summary, country)
    return {
        "title": title,
        "title_html": highlight(title, query),
        "summary": summary,
        "summary_html": highlight(summary, query),
        "link": row["link"],
        "source": source,
        "initial": (source[:1] or "N").upper(),
        "image": big_image(row["image"]),
        "topic": topic,
        "color": COLORS.get(topic, "#6366f1"),
        "ago": time_ago(published),
        "day": day_label(published),
        "published": published,
        "ingested": ingested,
        "just_in": ingested >= int(time.time()) - 600,
        "local": local,
        "saved": row["link"] in saved_links,
    }


def topic_counts(items):
    tally = Counter(item["topic"] for item in items)
    return [("All", len(items))] + [(name, tally.get(name, 0)) for name in ORDER]


def window_label(key):
    newer, older = RANGES[key]
    now = datetime.now()
    start = now - timedelta(hours=older)
    end = now - timedelta(hours=newer)
    if start.date() == end.date():
        return start.strftime("%d %b")
    return f"{start.strftime('%d %b')} – {end.strftime('%d %b')}"


def group_by_day(items):
    grouped = {}
    for item in items:
        grouped.setdefault(item["day"], []).append(item)
    for group in grouped.values():
        group.sort(
            key=lambda item: (
                not item["local"], -item.get("ingested", 0), -item["published"]
            )
        )
    return list(grouped.items())


def seen_key(link):
    """Short, cookie-safe id for a story link."""
    return hashlib.sha256((link or "").encode("utf-8")).hexdigest()[:16]


def remember_seen(items):
    """Keep a short per-session history so Refresh can surface a new-to-you story."""
    keys = [seen_key(item["link"]) for item in items[:60]]
    if not keys:
        return
    key_set = set(keys)
    previous = session.get("seen_links", [])
    session["seen_links"] = (keys + [key for key in previous if key not in key_set])[:120]


def prioritize_unseen(items):
    """Move the newest story the reader has not seen to the top."""
    if not items:
        return items
    seen = set(session.get("seen_links", []))
    unseen = [item for item in items if seen_key(item["link"]) not in seen]
    spotlight = next((item for item in unseen if item.get("image")), None)
    if spotlight is None and unseen:
        spotlight = unseen[0]
    if spotlight:
        items.sort(
            key=lambda item: (
                item["link"] == spotlight["link"],
                item.get("ingested", 0),
                item["published"],
            ),
            reverse=True,
        )
    return items


def news_payload():
    """Everything the React home screen needs, as plain JSON-friendly data."""
    rng = request.args.get("range", "24h")
    if rng not in RANGES:
        rng = "24h"
    category = request.args.get("category", "All")
    if category != "All" and category not in ORDER:
        category = "All"
    query = (request.args.get("q") or "").strip()[:120]
    try:
        limit = min(max(int(request.args.get("limit", 240)), 20), 500)
    except (TypeError, ValueError):
        limit = 240
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
    except (TypeError, ValueError):
        offset = 0

    requested_view = request.args.get("view")
    if requested_view in ("grid", "list"):
        view = requested_view
    else:
        view = g.user["view_mode"] if g.user["view_mode"] in ("grid", "list") else "grid"

    country = g.user["country"]
    # Local news is a bonus: refresh it in the background so the first page
    # never waits on a publisher's network or a database writer.
    if time.time() - country_state.get(country, 0) >= COUNTRY_TTL:
        country_state[country] = time.time()
        threading.Thread(target=ensure_country_news, args=(country, True), daemon=True).start()

    newer, older = RANGES[rng]
    now = int(time.time())
    start = now - older * 3600      # oldest moment in the window
    end = now - newer * 3600 - 1    # newest moment in the window

    def window_rows(low, high, terms=None):
        """Stories in a time window. For searches, prefilter in SQL when the
        words are plain ASCII (SQLite's LIKE is case-insensitive there)."""
        sql = (
            "SELECT * FROM articles WHERE published >= ? AND published <= ? "
            "AND (country IS NULL OR country = ?)"
        )
        args = [low, high, country]
        ascii_terms = [
            term for term in (terms or [])
            if term.isascii() and re.search(r"[^\W_]", term)
        ]
        if ascii_terms:
            sql += " AND (" + " OR ".join(
                "(title LIKE ? OR summary LIKE ? OR source LIKE ?)" for _ in ascii_terms
            ) + ")"
            for term in ascii_terms:
                args.extend([f"%{term}%"] * 3)
        # Picture-bearing stories first. Thousands of Google News arrivals have
        # no image in the feed and repair cannot keep up with ingestion. Sorting
        # in SQL matters: Python only sees the row slice below, so a later sort
        # cannot pull older pictures into the first page. A manual refresh is
        # the exception: it must surface newly ingested stories even while the
        # image worker is still catching up with them.
        if request.args.get("fresh") == "1":
            order = "ingested DESC, published DESC"
        elif not terms:
            order = "(image IS NOT NULL AND image <> '') DESC, ingested DESC, published DESC"
        else:
            order = "ingested DESC, published DESC"
        sql += f" ORDER BY {order} LIMIT ?"
        args.append(min(offset + limit * 3, 15000))
        return fetchall(sql, tuple(args))

    saved_links = {
        row["link"]
        for row in fetchall("SELECT link FROM saved WHERE user_id = ?", (g.user["id"],))
    }

    terms = search_terms(query) if query else []
    widened = False
    web = False
    search_failed = False
    scores = {}

    if query:
        rows = window_rows(start, end, terms)
        scored = [(match_score(row, terms), row) for row in rows]
        scored = [(score, row) for score, row in scored if score]
        live_links = cached_live_links(query, country)
        if not scored and not live_links:
            # Nothing in this tab: look through everything we keep, not just 24h.
            rows = window_rows(now - KEEP_DAYS * 86400, end, terms)
            scored = [(match_score(row, terms), row) for row in rows]
            scored = [(score, row) for score, row in scored if score]
            widened = bool(scored)
        if not scored and not live_links:
            # Still nothing: search the web and save what comes back.
            try:
                live_links = live_search(query, country)
            except Exception as exc:  # noqa: BLE001 - show a friendly notice
                log("Live search failed:", type(exc).__name__, exc)
                live_links = []
                search_failed = True
        if live_links:
            placeholders = ",".join("?" for _ in live_links)
            live_rows = fetchall(
                f"SELECT * FROM articles WHERE link IN ({placeholders})",
                tuple(live_links),
            )
            order = {link: index for index, link in enumerate(live_links)}
            live_rows.sort(key=lambda row: order.get(row["link"], 999))
            seen = {row["link"] for row in live_rows}
            if live_rows:
                web = True
                # Keep the search engine's ranking, then any local matches.
                scored = [
                    (len(live_rows) - index, row)
                    for index, row in enumerate(live_rows)
                ] + [(score, row) for score, row in scored if row["link"] not in seen]
        scored.sort(key=lambda pair: (pair[0], pair[1]["published"]), reverse=True)
        scores = {row["link"]: score for score, row in scored}
        rows = [row for _score, row in scored]
    else:
        rows = window_rows(start, end)

    items = [build_item(row, country, saved_links, query) for row in rows]
    if query:
        items.sort(key=lambda item: scores.get(item["link"], 0), reverse=True)
    else:
        # Picture-bearing stories first. Thousands of Google News arrivals have
        # no photo in the feed and repair cannot keep up with ingestion; the
        # rest surface naturally once the background worker finds their image.
        items.sort(
            key=lambda item: (
                bool(item.get("image")),
                item.get("ingested", 0),
                item["published"],
            ),
            reverse=True,
        )
    items = dedupe_items(items)
    if query:
        items.sort(key=lambda item: scores.get(item["link"], 0), reverse=True)

    counts = topic_counts(items)
    if category != "All":
        items = [item for item in items if item["topic"] == category]

    total = len(items)
    page = items[offset:offset + limit]
    has_more = offset + len(page) < total

    if request.args.get("fresh") == "1":
        page = prioritize_unseen(page)
    remember_seen(page)

    updated = None
    if state["last_refresh"]:
        updated = time.strftime("%H:%M", time.localtime(state["last_refresh"]))
    booting = not query and not page and not state["last_refresh"]

    return {
        "greeting": t(greeting_key()),
        "groups": [
            {"label": label, "items": group}
            for label, group in group_by_day(page)
        ] if not query else [],
        "items": page,
        "shown": len(page),
        "total": total,
        "has_more": has_more,
        "next_offset": offset + len(page),
        "counts": [{"name": name, "count": count} for name, count in counts],
        "ranges": list(RANGES.keys()),
        "range": rng,
        "range_label": t("range_" + rng),
        "window": window_label(rng),
        "category": category,
        "query": query,
        "view": view,
        "updated": updated,
        "booting": booting,
        "widened": widened,
        "web": web,
        "search_failed": search_failed,
    }


@app.route("/api/news")
@api_required
def api_news():
    return jsonify(news_payload())


@app.route("/api/gallery")
@api_required
def api_gallery():
    """A picture-first view across the whole archive, not one narrow window."""
    country = g.user["country"]
    saved_links = {
        row["link"]
        for row in fetchall("SELECT link FROM saved WHERE user_id = ?", (g.user["id"],))
    }
    rows = fetchall(
        "SELECT * FROM articles WHERE image IS NOT NULL AND image <> '' "
        "AND published >= ? ORDER BY published DESC LIMIT 200",
        (int(time.time()) - KEEP_DAYS * 86400,),
    )
    items = dedupe_items(
        [build_item(row, country, saved_links) for row in rows]
    )[:72]
    return jsonify(ok=True, items=items, shown=len(items), window="Last 30 days")


@app.route("/")
@login_required
def home():
    return spa_page()


@app.route("/signup")
def signup():
    return spa_page()


@app.route("/api/auth/signup", methods=["POST"])
def api_signup():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = request.form
    username = normalize_username(data.get("username"))
    email_raw = str(data.get("email") or "").strip()
    email = normalize_email(email_raw)
    country = data.get("country", "")
    password = data.get("password", "")
    confirm = data.get("confirm", "")
    remember = bool(data.get("remember"))
    if current_user():
        return jsonify(ok=False, error="already_signed_in"), 400
    if not username or not password or not confirm or not country:
        return jsonify(ok=False, error=t("err_required")), 400
    if not valid_username(username):
        return jsonify(ok=False, error=t("err_username")), 400
    if country not in COUNTRIES:
        return jsonify(ok=False, error=t("err_required")), 400
    if email_raw and not email:
        return jsonify(ok=False, error=t("err_email")), 400
    if len(password) < 8:
        return jsonify(ok=False, error=t("err_password")), 400
    if password != confirm:
        return jsonify(ok=False, error=t("err_match")), 400
    if fetchone("SELECT id FROM users WHERE username_lower = ?", (username.casefold(),)):
        return jsonify(ok=False, error=t("err_exists")), 409
    if email and fetchone("SELECT id FROM users WHERE email_lower = ?", (email,)):
        return jsonify(ok=False, error=t("err_email_taken")), 409
    try:
        cursor = execute(
            """
            INSERT INTO users
                (username, username_lower, email, email_lower, password_hash, country,
                 language, theme, view_mode, welcome_done, created)
            VALUES (?, ?, ?, ?, ?, ?, 'en', 'classic', 'grid', 0, ?)
            """,
            (username, username.casefold(), email_raw, email,
             generate_password_hash(password), country, int(time.time())),
        )
    except sqlite3.IntegrityError:
        return jsonify(ok=False, error=t("err_exists")), 409
    new_row = fetchone("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,))
    session.clear()
    session["uid"] = cursor.lastrowid
    session["lang"] = "en"
    session.permanent = remember
    sync_session_account(new_row)
    return jsonify(ok=True, next="/welcome", csrf=csrf_token())


@app.route("/login")
def login():
    return spa_page()


@app.route("/forgot")
def forgot():
    return spa_page()


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = request.form
    username = normalize_username(data.get("username"))
    password = data.get("password", "")
    remember = bool(data.get("remember"))
    email_identifier = normalize_email(username)
    limit_key = (username.casefold() or "-") + "|" + (request.remote_addr or "-")
    if current_user():
        return jsonify(ok=True, next="/")
    if not username or not password:
        return jsonify(ok=False, error=t("err_required")), 400
    if rate_limited(limit_key):
        return jsonify(ok=False, error=t("err_rate")), 429
    row = fetchone("SELECT * FROM users WHERE username_lower = ?", (username.casefold(),))
    if not row and email_identifier:
        row = fetchone("SELECT * FROM users WHERE email_lower = ?", (email_identifier,))
    if not row or not check_password_hash(row["password_hash"], password):
        # A cold Vercel instance may have an empty /tmp database. The signed
        # session keeps the last account so login works without re-signup.
        account = session.get("account")
        matches_account = (
            isinstance(account, dict)
            and (
                account.get("username_lower") == username.casefold()
                or (email_identifier and account.get("email_lower") == email_identifier)
            )
        )
        if (
            matches_account
            and account.get("password_hash")
            and check_password_hash(account["password_hash"], password)
            and restore_account_from_session()
        ):
            row = fetchone("SELECT * FROM users WHERE id = ?", (account.get("id"),))
        if not row or not check_password_hash(row["password_hash"], password):
            record_fail(limit_key)
            return jsonify(ok=False, error=t("err_credentials")), 401
    clear_fails(limit_key)
    session.clear()
    session["uid"] = row["id"]
    session["lang"] = row["language"]
    session.permanent = remember
    sync_session_account(row)
    return jsonify(
        ok=True,
        next="/" if row["welcome_done"] else "/welcome",
        csrf=csrf_token(),
    )


@app.route("/api/auth/forgot", methods=["POST"])
def api_forgot():
    """Step 1: email in, 6-digit OTP out (by email)."""
    if current_user():
        return jsonify(ok=True, next="/")
    data = request.get_json(silent=True) or {}
    raw_email = str(data.get("email") or "").strip()
    email = normalize_email(raw_email)
    limit_key = "reset|" + (email or raw_email[:80]).casefold() + "|" + (request.remote_addr or "-")
    if rate_limited(limit_key):
        return jsonify(ok=False, error=t("err_rate")), 429
    if not email:
        record_fail(limit_key)
        return jsonify(ok=False, error=t("err_email")), 400

    user = fetchone("SELECT * FROM users WHERE email_lower = ?", (email,))
    if not user:
        record_fail(limit_key)
        return jsonify(ok=False, error=t("err_email_not_found")), 404

    execute("UPDATE password_resets SET used = 1 WHERE user_id = ? AND used = 0", (user["id"],))
    token = secrets.token_urlsafe(24)
    otp = f"{secrets.randbelow(1_000_000):06d}"
    now = int(time.time())
    execute(
        """
        INSERT INTO password_resets
            (user_id, email, token, otp_hash, created, expires, attempts, verified, used)
        VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0)
        """,
        (user["id"], user["email"] or email, token, otp_digest(token, otp),
         now, now + RESET_TTL),
    )
    if not send_otp_email(user["email"] or email, otp, user["username"]):
        execute("DELETE FROM password_resets WHERE token = ?", (token,))
        return jsonify(ok=False, error=t("err_email_send")), 503

    clear_fails(limit_key)
    return jsonify(
        ok=True,
        reset_token=token,
        email_hint=mask_email(email),
        expires_in=RESET_TTL,
    )


@app.route("/api/auth/verify-otp", methods=["POST"])
def api_verify_otp():
    """Step 2: check the OTP and unlock the new-details form."""
    data = request.get_json(silent=True) or {}
    token = str(data.get("token") or "").strip()
    otp = re.sub(r"\D", "", str(data.get("otp") or ""))
    if not token:
        return jsonify(ok=False, error=t("err_no_reset")), 400
    if len(otp) != 6:
        return jsonify(ok=False, error=t("err_otp")), 400

    row = fetchone("SELECT * FROM password_resets WHERE token = ?", (token,))
    now = int(time.time())
    if not row or row["used"] or row["expires"] < now:
        return jsonify(ok=False, error=t("err_reset_expired")), 400
    if row["attempts"] >= OTP_MAX_ATTEMPTS:
        return jsonify(ok=False, error=t("err_rate")), 429

    if not secrets.compare_digest(row["otp_hash"], otp_digest(token, otp)):
        execute("UPDATE password_resets SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
        left = OTP_MAX_ATTEMPTS - row["attempts"] - 1
        if left <= 0:
            execute("UPDATE password_resets SET used = 1 WHERE id = ?", (row["id"],))
            return jsonify(ok=False, error=t("err_rate")), 429
        return jsonify(
            ok=False,
            error=t("err_otp_attempts", n=left),
            attempts_left=left,
        ), 400

    execute(
        "UPDATE password_resets SET verified = 1, attempts = 0, expires = expires + 300 "
        "WHERE id = ?",
        (row["id"],),
    )
    user = fetchone("SELECT username FROM users WHERE id = ?", (row["user_id"],))
    return jsonify(ok=True, reset_token=token, username=user["username"] if user else "")


@app.route("/api/auth/reset", methods=["POST"])
def api_reset_password():
    """Step 3: verified OTP allows a new username and password, then sign in."""
    data = request.get_json(silent=True) or {}
    token = str(data.get("token") or "").strip()
    username = normalize_username(data.get("username"))
    password = str(data.get("password") or "")
    confirm = str(data.get("confirm") or "")
    remember = bool(data.get("remember"))

    row = fetchone("SELECT * FROM password_resets WHERE token = ?", (token,)) if token else None
    now = int(time.time())
    if not row or not row["verified"] or row["used"] or row["expires"] < now:
        return jsonify(ok=False, error=t("err_reset_expired")), 400
    if not valid_username(username):
        return jsonify(ok=False, error=t("err_username")), 400
    if len(password) < 8:
        return jsonify(ok=False, error=t("err_password")), 400
    if password != confirm:
        return jsonify(ok=False, error=t("err_match")), 400
    if fetchone(
        "SELECT id FROM users WHERE username_lower = ? AND id <> ?",
        (username.casefold(), row["user_id"]),
    ):
        return jsonify(ok=False, error=t("err_exists")), 409

    user = fetchone("SELECT * FROM users WHERE id = ?", (row["user_id"],))
    if not user:
        return jsonify(ok=False, error=t("err_no_reset")), 400
    execute(
        "UPDATE users SET username = ?, username_lower = ?, password_hash = ? WHERE id = ?",
        (username, username.casefold(), generate_password_hash(password), row["user_id"]),
    )
    execute("UPDATE password_resets SET used = 1 WHERE user_id = ?", (row["user_id"],))
    ip = request.remote_addr or "-"
    clear_fails(user["username_lower"].casefold() + "|" + ip)
    clear_fails(username.casefold() + "|" + ip)

    fresh = fetchone("SELECT * FROM users WHERE id = ?", (row["user_id"],))
    session.clear()
    session["uid"] = row["user_id"]
    session["lang"] = user["language"]
    session.permanent = remember
    sync_session_account(fresh)
    return jsonify(
        ok=True,
        next="/" if user["welcome_done"] else "/welcome",
        csrf=csrf_token(),
    )


@app.route("/welcome")
@login_required
def welcome():
    return spa_page()


@app.route("/api/auth/welcome", methods=["POST"])
@api_required
def api_welcome():
    language = (request.get_json(silent=True) or {}).get("language", "")
    if language not in LANGUAGES:
        return jsonify(ok=False, error=t("err_required")), 400
    execute(
        "UPDATE users SET language = ?, welcome_done = 1 WHERE id = ?",
        (language, g.user["id"]),
    )
    session["lang"] = language
    refresh_session_account()
    return jsonify(ok=True)

@app.route("/profile")
@login_required
def profile():
    return spa_page()


@app.route("/settings")
@login_required
def settings():
    return spa_page()


@app.route("/api/profile")
@api_required
def api_profile():
    saved_count = fetchone(
        "SELECT COUNT(*) AS n FROM saved WHERE user_id = ?", (g.user["id"],)
    )["n"]
    return jsonify(
        ok=True,
        user=bootstrap_payload()["user"],
        saved_count=saved_count,
        member_since=datetime.fromtimestamp(g.user["created"]).strftime("%d %b %Y"),
        country_name=COUNTRIES.get(g.user["country"], {}).get("name", g.user["country"]),
    )


@app.route("/profile/username", methods=["POST"])
@login_required
def profile_username():
    new_name = normalize_username(request.form.get("username"))
    if not valid_username(new_name):
        flash(t("err_username"), "error")
    elif new_name.casefold() != g.user["username_lower"]:
        if fetchone(
            "SELECT id FROM users WHERE username_lower = ? AND id <> ?",
            (new_name.casefold(), g.user["id"]),
        ):
            flash(t("err_exists"), "error")
        else:
            try:
                execute(
                    "UPDATE users SET username = ?, username_lower = ? WHERE id = ?",
                    (new_name, new_name.casefold(), g.user["id"]),
                )
            except sqlite3.IntegrityError:
                flash(t("err_exists"), "error")
            else:
                flash(t("username_changed"), "ok")
                return redirect(url_for("profile"))
    return redirect(url_for("profile"))


@app.route("/profile/password", methods=["POST"])
@login_required
def profile_password():
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm", "")

    if not current or not new or not confirm:
        flash(t("err_required"), "error")
    elif not check_password_hash(g.user["password_hash"], current):
        flash(t("err_current"), "error")
    elif len(new) < 8:
        flash(t("err_password"), "error")
    elif new != confirm:
        flash(t("err_match"), "error")
    elif check_password_hash(g.user["password_hash"], new):
        flash(t("err_same"), "error")
    else:
        execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new), g.user["id"]),
        )
        flash(t("password_changed"), "ok")
    return redirect(url_for("profile") + "#password")


@app.route("/profile/prefs", methods=["POST"])
@login_required
def profile_prefs():
    country = request.form.get("country", g.user["country"])
    language = request.form.get("language", g.user["language"])
    theme = request.form.get("theme", g.user["theme"])
    view_mode = request.form.get("view_mode", g.user["view_mode"])
    reader_font = request.form.get(
        "reader_font", g.user["reader_font"] if "reader_font" in g.user.keys() else "serif"
    )

    if country not in COUNTRIES or language not in LANGUAGES:
        flash(t("err_required"), "error")
        return redirect(url_for("profile") + "#settings")
    if theme not in THEMES:
        theme = "classic"
    if view_mode not in ("grid", "list"):
        view_mode = "grid"
    if reader_font not in READER_FONTS:
        reader_font = "serif"

    old_country = g.user["country"]
    execute(
        """
        UPDATE users
        SET country = ?, language = ?, theme = ?, view_mode = ?, reader_font = ?,
            welcome_done = 1
        WHERE id = ?
        """,
        (country, language, theme, view_mode, reader_font, g.user["id"]),
    )
    session["lang"] = language
    if country != old_country:
        country_state.pop(country, None)
    flash(t("prefs_changed"), "ok")
    return redirect(url_for("profile") + "#settings")


@app.route("/api/profile/username", methods=["POST"])
@api_required
def api_profile_username():
    new_name = normalize_username((request.get_json(silent=True) or {}).get("username"))
    if not valid_username(new_name):
        return jsonify(ok=False, error=t("err_username")), 400
    if new_name.casefold() != g.user["username_lower"]:
        if fetchone(
            "SELECT id FROM users WHERE username_lower = ? AND id <> ?",
            (new_name.casefold(), g.user["id"]),
        ):
            return jsonify(ok=False, error=t("err_exists")), 409
        execute(
            "UPDATE users SET username = ?, username_lower = ? WHERE id = ?",
            (new_name, new_name.casefold(), g.user["id"]),
        )
        refresh_session_account()
    return jsonify(ok=True)


@app.route("/api/profile/password", methods=["POST"])
@api_required
def api_profile_password():
    data = request.get_json(silent=True) or {}
    current = data.get("current_password", "")
    new = data.get("new_password", "")
    confirm = data.get("confirm", "")
    if not current or not new or not confirm:
        return jsonify(ok=False, error=t("err_required")), 400
    if not check_password_hash(g.user["password_hash"], current):
        return jsonify(ok=False, error=t("err_current")), 400
    if len(new) < 8:
        return jsonify(ok=False, error=t("err_password")), 400
    if new != confirm:
        return jsonify(ok=False, error=t("err_match")), 400
    if check_password_hash(g.user["password_hash"], new):
        return jsonify(ok=False, error=t("err_same")), 400
    execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new), g.user["id"]),
    )
    refresh_session_account()
    return jsonify(ok=True)


@app.route("/api/profile/prefs", methods=["POST"])
@api_required
def api_profile_prefs():
    data = request.get_json(silent=True) or {}
    current_email = (g.user["email"] if "email" in g.user.keys() else "") or ""
    current_email_lower = (g.user["email_lower"] if "email_lower" in g.user.keys() else "") or ""
    country = data.get("country", g.user["country"])
    language = data.get("language", g.user["language"])
    theme = data.get("theme", g.user["theme"])
    view_mode = data.get("view_mode", g.user["view_mode"])
    reader_font = data.get(
        "reader_font", g.user["reader_font"] if "reader_font" in g.user.keys() else "serif"
    )
    email_raw = str(data.get("email", current_email) or "").strip()
    email = normalize_email(email_raw) if email_raw else ""
    if country not in COUNTRIES or language not in LANGUAGES:
        return jsonify(ok=False, error=t("err_required")), 400
    if email_raw and not email:
        return jsonify(ok=False, error=t("err_email")), 400
    if email and email != current_email_lower:
        if fetchone(
            "SELECT id FROM users WHERE email_lower = ? AND id <> ?",
            (email, g.user["id"]),
        ):
            return jsonify(ok=False, error=t("err_email_taken")), 409
    theme = theme if theme in THEMES else "classic"
    view_mode = view_mode if view_mode in ("grid", "list") else "grid"
    reader_font = reader_font if reader_font in READER_FONTS else "serif"
    old_country = g.user["country"]
    execute(
        """
        UPDATE users
        SET country = ?, language = ?, theme = ?, view_mode = ?, reader_font = ?,
            email = ?, email_lower = ?, welcome_done = 1
        WHERE id = ?
        """,
        (country, language, theme, view_mode, reader_font, email_raw, email, g.user["id"]),
    )
    session["lang"] = language
    if country != old_country:
        country_state.pop(country, None)
    refresh_session_account()
    return jsonify(ok=True)


@app.route("/api/auth/logout", methods=["POST"])
@api_required
def api_logout():
    clear_auth_session()
    return jsonify(ok=True, csrf=csrf_token())


@app.route("/api/csrf")
def api_csrf():
    """Let the SPA refresh its CSRF token after a session change."""
    return jsonify(ok=True, csrf=csrf_token())


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    clear_auth_session()
    return redirect(url_for("login"))


def text_is_rtl(text):
    rtl = len(re.findall(r"[\u0590-\u08FF]", text or ""))
    latin = len(re.findall(r"[A-Za-z]", text or ""))
    return rtl > latin


@app.route("/read")
@login_required
def read_article():
    return spa_page()


@app.route("/api/read")
@api_required
def api_read():
    """Structured, professional reading view for one story."""
    link = (request.args.get("u") or "").strip()
    if not link or len(link) > 2000:
        abort(404)
    row = fetchone("SELECT * FROM articles WHERE link = ?", (link,))
    if not row:
        abort(404)
    reader = get_reading(row)
    saved = bool(
        fetchone(
            "SELECT 1 FROM saved WHERE user_id = ? AND link = ?",
            (g.user["id"], link),
        )
    )
    body = " ".join(block.get("text", "") for block in reader["blocks"])
    return jsonify(
        ok=True,
        article={
            "link": row["link"],
            "title": clean(row["title"]),
            "summary": clean(row["summary"]),
            "source": clean(row["source"]) or "News",
            "published": row["published"],
            "ago": time_ago(row["published"]),
        },
        reader=reader,
        saved=saved,
        rtl=text_is_rtl((reader["title"] or "") + " " + (body or row["summary"] or "")),
    )


@app.route("/saved")
@login_required
def saved():
    return spa_page()


@app.route("/pictures")
@login_required
def pictures():
    return spa_page()


@app.route("/api/saved")
@api_required
def api_saved():
    rows = fetchall(
        """
        SELECT a.*, s.saved_at FROM saved s
        JOIN articles a ON a.link = s.link
        WHERE s.user_id = ?
        ORDER BY s.saved_at DESC
        """,
        (g.user["id"],),
    )
    saved_links = {row["link"] for row in rows}
    items = [build_item(row, g.user["country"], saved_links) for row in rows]
    return jsonify(ok=True, items=items, shown=len(items))


@app.route("/api/save", methods=["POST"])
@login_required
def api_save():
    data = request.get_json(silent=True) or {}
    link = (data.get("link") or "").strip()
    if not link or len(link) > 2000:
        return jsonify(ok=False, error="bad link"), 400
    if not fetchone("SELECT link FROM articles WHERE link = ?", (link,)):
        return jsonify(ok=False, error="unknown article"), 404
    already = fetchone(
        "SELECT 1 FROM saved WHERE user_id = ? AND link = ?", (g.user["id"], link)
    )
    if already:
        execute(
            "DELETE FROM saved WHERE user_id = ? AND link = ?", (g.user["id"], link)
        )
        return jsonify(ok=True, saved=False)
    execute(
        "INSERT OR IGNORE INTO saved (user_id, link, saved_at) VALUES (?, ?, ?)",
        (g.user["id"], link, int(time.time())),
    )
    return jsonify(ok=True, saved=True)


@app.route("/refresh", methods=["POST"])
@app.route("/api/refresh", methods=["POST"])
@api_required
def refresh_route():
    started = start_refresh_job(g.user["country"])
    return jsonify(ok=True, started=started, running=True)


@app.route("/api/refresh/status")
@api_required
def api_refresh_status():
    with refresh_job_lock:
        snapshot = dict(refresh_job)
    return jsonify(ok=True, **snapshot)


@app.route("/api/image", methods=["POST"])
@login_required
def api_image():
    """Find a fresh picture for one story when its stored image dies."""
    data = request.get_json(silent=True) or {}
    link = (data.get("link") or "").strip()
    if not link or len(link) > 2000:
        return jsonify(ok=False), 400
    row = fetchone("SELECT link, title FROM articles WHERE link = ?", (link,))
    if not row:
        return jsonify(ok=False), 404
    image = None
    try:
        image = recover_image(row["link"], row["title"])
    except Exception as exc:  # noqa: BLE001 - the page may be gone
        log("Image repair failed:", link[:90], type(exc).__name__)
    image = prepare_image(image) if image else None
    if image and image_needs_repair(image):
        image = None
    # Clear pictures that cannot be recovered so the UI shows its clean letter
    # tile instead of retrying a dead URL on every visit.
    execute(
        "UPDATE articles SET image = ?, image_tried = ? WHERE link = ?",
        (image, int(time.time()), link),
    )
    return jsonify(ok=True, image=image)


@app.route("/img")
@login_required
def img_proxy():
    """Small image proxy so hotlink-protected pictures still render."""
    url = ascii_url((request.args.get("u") or "").strip())
    if not proxy_url_allowed(url):
        abort(400)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    IMAGE_CACHE.mkdir(parents=True, exist_ok=True)
    data_file = IMAGE_CACHE / (key + ".bin")
    type_file = IMAGE_CACHE / (key + ".type")
    if data_file.exists() and time.time() - data_file.stat().st_mtime < 7 * 86400:
        try:
            cached_type = type_file.read_text(encoding="utf-8").strip() or "image/jpeg"
            return Response(
                data_file.read_bytes(),
                mimetype=cached_type,
                headers={"Cache-Control": "public, max-age=604800"},
            )
        except OSError:
            pass

    req = urllib.request.Request(
        url,
        headers={**HEADERS, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
    )
    def download(context=None):
        with urllib.request.urlopen(req, timeout=10, context=context) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                abort(415)
            return ctype, resp.read(6_000_000)

    try:
        try:
            ctype, data = download()
        except ssl.SSLError:
            # A few image CDNs have broken certificate chains; the browser only
            # needs the bytes, and this keeps their pictures on the page.
            ctype, data = download(ssl._create_unverified_context())
    except (urllib.error.URLError, OSError, ValueError):
        abort(502)
    try:
        data_file.write_bytes(data)
        type_file.write_text(ctype, encoding="utf-8")
    except OSError:
        pass
    return Response(
        data,
        mimetype=ctype,
        headers={"Cache-Control": "public, max-age=604800"},
    )


init_db()


if __name__ == "__main__":
    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=image_worker_loop, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)

# <<<APPEND>>>
