"""News fetching utilities."""

import random
import re
from functools import lru_cache
from pathlib import Path
from typing import List, TypedDict
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import requests


NEWS_API_URL = "https://newsapi.org/v2/top-headlines"
RSS_OPML_PATH = Path(__file__).resolve().parent / "RSS Feeds.opml"


class HeadlineItem(TypedDict):
    """Headline metadata used throughout the generation pipeline."""

    title: str
    description: str
    source_name: str
    url: str

PRIORITY_TERMS = {
    "robot": 5,
    "robotics": 5,
    "ai": 4,
    "artificial intelligence": 5,
    "machine learning": 3,
    "computer": 4,
    "software": 3,
    "hardware": 3,
    "chip": 3,
    "semiconductor": 3,
    "legacy": 3,
    "retro": 3,
    "vintage": 3,
    "internet": 2,
    "open source": 3,
    "hacker": 3,
    "privacy": 2,
    "social": 2,
    "culture": 2,
    "quirky": 3,
    "weird": 3,
    "odd": 2,
    "bizarre": 3,
    "satire": 2,
    "science": 2,
    "space": 2,
    "gaming": 3,
    "video game": 3,
    "arcade": 2,
    "cyberpunk": 2,
    "fandom": 2,
    "comic con": 2,
    "indie game": 3,
    "modding": 2,
    "retro tech": 3,
    "sci-fi": 3,
    "future": 2,
}

DEPRIORITY_TERMS = {
    "basketball": 8,
    "nfl": 7,
    "nba": 7,
    "mlb": 7,
    "nhl": 7,
    "soccer": 5,
    "football match": 6,
    "playoff": 6,
    "tournament": 5,
    "perdue": 6,
    "purdue": 6,
    "box score": 6,
    "tmz": 10,
    "tabloid": 8,
    "paparazzi": 8,
    "red carpet": 8,
    "dating rumors": 8,
    "breakup rumors": 8,
    "celebrity couple": 8,
    "reality star": 7,
    "royal family": 6,
    "gossip": 8,
    "fashion week": 6,
    "horoscope": 8,
    "horoscopes": 8,
    "astrology": 8,
    "zodiac": 8,
    "star sign": 8,
    "star signs": 8,
}

CELEBRITY_TERMS = {
    "celebrity",
    "actor",
    "actress",
    "singer",
    "rapper",
    "influencer",
    "reality tv",
    "hollywood",
    "kardashian",
    "jenner",
}

TECH_CONTEXT_TERMS = {
    "ai",
    "artificial intelligence",
    "robot",
    "robotics",
    "software",
    "hardware",
    "chip",
    "semiconductor",
    "tesla",
    "spacex",
    "openai",
    "nvidia",
    "apple",
    "microsoft",
    "google",
    "meta",
    "github",
    "startup",
    "video game",
    "gaming",
    "computer vision",
    "autonomous",
    "algorithm",
    "analytics",
    "sensor",
    "wearable",
    "drone",
    "cybersecurity",
    "cloud",
    "platform",
    "app",
    "open source",
    "automation",
}

TABLOID_SOURCES = {
    "tmz",
    "people",
    "us weekly",
    "e! online",
    "dailymail",
    "daily mail",
    "the sun",
    "ok!",
    "hola!",
    "mirror",
}

HARD_BLOCKED_SOURCES = {
    "tmz",
    "people",
    "dailymail",
    "daily mail",
    "new york post",
    "the sun",
    "fox news",
    "breitbart news",
    "the daily wire",
    "newsmax",
    "one america news network",
    "oann",
    "the blaze",
    "washington examiner",
    "infowars",
}

POLITICAL_TERMS = {
    "election",
    "campaign",
    "polling",
    "vote",
    "voter",
    "parliament",
    "congress",
    "senate",
    "house of representatives",
    "prime minister",
    "president",
    "government",
    "policy debate",
    "border",
    "immigration",
    "migrant",
    "refugee",
    "asylum",
    "deportation",
    "left wing",
    "right wing",
    "far-right",
    "far right",
    "conservative party",
    "labour party",
    "democrat",
    "republican",
    "geopolitics",
    "foreign minister",
    "ceasefire",
    "sanctions",
    "war",
}

TRUMP_TERMS = {
    "trump",
    "donald trump",
    "trump administration",
    "maga",
}

IRAN_TERMS = {
    "iran",
    "iranian",
    "tehran",
}

IRAN_CONFLICT_TERMS = {
    "war",
    "conflict",
    "strike",
    "missile",
    "airstrike",
    "retaliation",
    "military",
    "ceasefire",
    "sanctions",
}

AMERICAN_FOOTBALL_TERMS = {
    "nfl",
    "american football",
    "touchdown",
    "quarterback",
    "gridiron",
    "super bowl",
    "superbowl",
}

SOCCER_TERMS = {
    "soccer",
    "fifa",
    "uefa",
    "premier league",
    "champions league",
    "la liga",
    "serie a",
    "bundesliga",
    "mls",
}

HOROSCOPE_TERMS = {
    "horoscope",
    "horoscopes",
    "astrology",
    "astrological",
    "zodiac",
    "star sign",
    "star signs",
    "aries",
    "taurus",
    "gemini",
    "cancer",
    "leo",
    "virgo",
    "libra",
    "scorpio",
    "sagittarius",
    "capricorn",
    "aquarius",
    "pisces",
}

SUPER_BOWL_CULTURAL_TERMS = {
    "halftime",
    "half-time",
    "half time",
    "performance",
    "concert",
    "show",
    "ads",
    "commercial",
    "trailer",
    "brand campaign",
}

MAINSTREAM_SPORTS_TERMS = {
    "basketball",
    "football",
    "soccer",
    "baseball",
    "hockey",
    "tennis",
    "golf",
    "cricket",
    "rugby",
    "formula 1",
    "f1",
    "olympic",
    "olympics",
    "nfl",
    "nba",
    "mlb",
    "nhl",
    "uefa",
    "fifa",
    "premier league",
    "champions league",
    "playoff",
    "box score",
}

SPORTS_TECH_EXCEPTION_TERMS = {
    "ai",
    "artificial intelligence",
    "machine learning",
    "robot",
    "robotics",
    "computer vision",
    "autonomous",
    "algorithm",
    "analytics",
    "data model",
    "sensor",
    "wearable",
    "chip",
    "semiconductor",
    "software",
    "hardware",
    "platform",
    "app",
    "detection system",
    "tracking system",
    "referee technology",
    "var system",
    "esports",
    "e-sports",
}


def _normalize_source_name(source_name: str) -> str:
    """Normalize source names for robust matching."""
    lowered = source_name.lower().strip()
    lowered = lowered.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


def _extract_domain(url: str) -> str:
    """Extract normalized domain from a URL string."""
    if not url:
        return ""
    parsed = urlparse(url)
    domain = parsed.netloc.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


@lru_cache(maxsize=1)
def _load_preferred_source_hints() -> tuple[set[str], set[str]]:
    """Load source-name and domain hints from RSS Feeds OPML, if available."""
    if not RSS_OPML_PATH.exists():
        return set(), set()

    try:
        tree = ET.parse(RSS_OPML_PATH)
    except ET.ParseError:
        return set(), set()

    source_names: set[str] = set()
    domains: set[str] = set()

    for outline in tree.findall(".//outline"):
        text = (outline.get("text") or "").strip()
        title = (outline.get("title") or "").strip()
        html_url = (outline.get("htmlUrl") or "").strip()
        xml_url = (outline.get("xmlUrl") or "").strip()

        for name_candidate in (text, title):
            normalized = _normalize_source_name(name_candidate)
            if normalized and len(normalized) >= 3:
                source_names.add(normalized)

        for url_candidate in (html_url, xml_url):
            domain = _extract_domain(url_candidate)
            if domain:
                domains.add(domain)

    return source_names, domains


def _preferred_source_boost(source_name: str, article_url: str) -> int:
    """Boost ranking for sources/domains listed in RSS Feeds OPML."""
    preferred_names, preferred_domains = _load_preferred_source_hints()
    if not preferred_names and not preferred_domains:
        return 0

    boost = 0
    normalized_source = _normalize_source_name(source_name)
    article_domain = _extract_domain(article_url)

    if normalized_source:
        if normalized_source in preferred_names:
            boost += 5

    if article_domain:
        if article_domain in preferred_domains:
            boost += 6
        elif any(article_domain.endswith(f".{domain}") for domain in preferred_domains):
            boost += 4

    return boost


def _is_hard_blocked_source(source_name: str) -> bool:
    """Return True when source should always be excluded."""
    normalized = _normalize_source_name(source_name)
    for blocked_source in HARD_BLOCKED_SOURCES:
        if re.search(rf"\b{re.escape(blocked_source)}\b", normalized):
            return True
    return False


def _is_political_or_immigration_article(title: str, description: str) -> bool:
    """Return True when article is heavily political/immigration-focused."""
    text = f"{title} {description}".lower()
    return any(re.search(rf"\b{re.escape(term)}\b", text) for term in POLITICAL_TERMS)


def _is_explicitly_blocked_topic(title: str, description: str) -> bool:
    """Return True for explicitly blocked user topics."""
    text = f"{title} {description}".lower()

    has_horoscope = any(re.search(rf"\b{re.escape(term)}\b", text) for term in HOROSCOPE_TERMS)
    if has_horoscope:
        return True

    has_trump = any(re.search(rf"\b{re.escape(term)}\b", text) for term in TRUMP_TERMS)
    if has_trump:
        return True

    has_iran = any(re.search(rf"\b{re.escape(term)}\b", text) for term in IRAN_TERMS)
    has_iran_conflict = any(
        re.search(rf"\b{re.escape(term)}\b", text) for term in IRAN_CONFLICT_TERMS
    )
    if has_iran and has_iran_conflict:
        return True

    has_soccer = any(re.search(rf"\b{re.escape(term)}\b", text) for term in SOCCER_TERMS)
    if has_soccer:
        return True

    has_american_football = any(
        re.search(rf"\b{re.escape(term)}\b", text) for term in AMERICAN_FOOTBALL_TERMS
    )
    if has_american_football:
        has_super_bowl = bool(
            re.search(r"\bsuper bowl\b", text) or re.search(r"\bsuperbowl\b", text)
        )
        has_super_bowl_culture = any(
            re.search(rf"\b{re.escape(term)}\b", text) for term in SUPER_BOWL_CULTURAL_TERMS
        )
        # Allow only cultural-event Super Bowl coverage (e.g., halftime/performance/ads).
        if not (has_super_bowl and has_super_bowl_culture):
            return True

    return False


def _count_term_hits(text: str, terms: set[str]) -> int:
    """Count how many unique terms match with word boundaries."""
    return sum(1 for term in terms if re.search(rf"\b{re.escape(term)}\b", text))


def _is_mainstream_sports_without_tech_context(title: str, description: str) -> bool:
    """Exclude mainstream sports unless strong tech context is present."""
    text = f"{title} {description}".lower()
    sports_hits = _count_term_hits(text, MAINSTREAM_SPORTS_TERMS)
    if sports_hits == 0:
        return False

    tech_hits = _count_term_hits(text, SPORTS_TECH_EXCEPTION_TERMS)
    # Require at least 2 distinct tech signals to keep a sports story.
    return tech_hits < 2


def _score_article(title: str, description: str) -> int:
    """Score an article by preferred and deprioritized themes."""
    text = f"{title} {description}".lower()
    score = 0

    for term, weight in PRIORITY_TERMS.items():
        if re.search(rf"\b{re.escape(term)}\b", text):
            score += weight

    for term, penalty in DEPRIORITY_TERMS.items():
        if re.search(rf"\b{re.escape(term)}\b", text):
            score -= penalty

    if "?" in title:
        score += 1

    return score


def _is_tabloid_celebrity_article(title: str, description: str, source_name: str) -> bool:
    """Return True when content looks like mainstream/tabloid celebrity news."""
    text = f"{title} {description} {source_name}".lower()

    has_celebrity = any(re.search(rf"\b{re.escape(term)}\b", text) for term in CELEBRITY_TERMS)
    has_tabloid_source = any(source in text for source in TABLOID_SOURCES)
    has_tabloid_language = any(
        re.search(rf"\b{re.escape(term)}\b", text) for term in DEPRIORITY_TERMS
    )
    has_tech_context = any(re.search(rf"\b{re.escape(term)}\b", text) for term in TECH_CONTEXT_TERMS)

    # Exclude tabloid-style celebrity stories unless clearly connected to tech/geek context.
    if (has_tabloid_source or has_tabloid_language or has_celebrity) and not has_tech_context:
        return True
    return False


def _rank_headlines(articles: list[dict]) -> List[HeadlineItem]:
    """Rank and deduplicate headlines by style relevance."""
    ranked: list[tuple[int, HeadlineItem]] = []
    for article in articles:
        title = str((article or {}).get("title", "")).strip()
        description = str((article or {}).get("description", "")).strip()
        source_name = str(((article or {}).get("source") or {}).get("name", "")).strip()
        url = str((article or {}).get("url", "")).strip()
        if not title:
            continue
        if _is_hard_blocked_source(source_name):
            continue
        if _is_explicitly_blocked_topic(title, description):
            continue
        if _is_political_or_immigration_article(title, description):
            continue
        if _is_mainstream_sports_without_tech_context(title, description):
            continue
        if _is_tabloid_celebrity_article(title, description, source_name):
            continue
        score = _score_article(title, description)
        score += _preferred_source_boost(source_name, url)
        ranked.append(
            (
                score,
                {
                    "title": title,
                    "description": description,
                    "source_name": source_name,
                    "url": url,
                },
            )
        )

    if not ranked:
        return []

    ranked.sort(key=lambda item: item[0], reverse=True)

    deduped: list[HeadlineItem] = []
    seen = set()
    for _, article in ranked:
        title = article["title"]
        if title in seen:
            continue
        seen.add(title)
        deduped.append(article)
    return deduped


def get_headlines(news_api_key: str, country: str = "us", page_size: int = 25) -> List[HeadlineItem]:
    """Fetch top headlines from NewsAPI and return style-ranked headline metadata."""
    params = {
        "country": country,
        "pageSize": min(max(page_size, 10), 100),
        "apiKey": news_api_key,
    }

    try:
        response = requests.get(NEWS_API_URL, params=params, timeout=20)
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch headlines from NewsAPI: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if response.status_code >= 400:
        code = payload.get("code", "unknown_code")
        message = payload.get("message", response.text[:200] or "unknown error")
        raise RuntimeError(
            f"NewsAPI unauthorized/error ({response.status_code}, {code}): {message}"
        )

    if payload.get("status") != "ok":
        message = payload.get("message", "unknown error")
        raise RuntimeError(f"NewsAPI returned an error: {message}")

    articles = payload.get("articles", [])
    headlines = _rank_headlines(articles)

    if not headlines:
        raise RuntimeError("NewsAPI returned no valid headlines.")

    return headlines


def choose_headline(headlines: List[HeadlineItem], top_pool_size: int = 8) -> HeadlineItem:
    """Pick one headline from the top-ranked pool to keep output varied."""
    if not headlines:
        raise RuntimeError("No headlines available for selection.")

    pool = headlines[: max(1, min(top_pool_size, len(headlines)))]
    return random.choice(pool)
