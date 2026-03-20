from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests
import time
import re
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()  
origins = [
    "https://www.risktakers.net",
    "https://risktakers.net"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,   
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CATEGORY_FEEDS = {
    "world": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://feeds.reuters.com/Reuters/worldNews",
        "https://feeds.apnews.com/apf-worldnews",
        "http://rss.cnn.com/rss/edition_world.rss",
        "https://feeds.npr.org/1004/rss.xml",
    ],
    "us": [
        "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
        "https://feeds.reuters.com/Reuters/domesticNews",
        "https://feeds.apnews.com/apf-usnews",
        "http://rss.cnn.com/rss/cnn_us.rss",
        "https://feeds.npr.org/1003/rss.xml",
    ],
    "business": [
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.apnews.com/apf-business",
        "http://rss.cnn.com/rss/money_latest.rss",
        "https://feeds.npr.org/1017/rss.xml",
    ],
    "science": [
        "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
        "https://feeds.apnews.com/apf-science",
        "http://rss.cnn.com/rss/edition_technology.rss",
        "https://feeds.npr.org/1007/rss.xml",
    ],
    "culture": [
        "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
        "https://feeds.apnews.com/apf-entertainment",
        "http://rss.cnn.com/rss/edition_entertainment.rss",
        "https://feeds.npr.org/1048/rss.xml",
    ],
}

RSS2JSON_URL = "https://api.rss2json.com/v1/api.json?rss_url="

# Simple in-memory caches
feed_cache = {}
image_cache = {}

FEED_CACHE_TTL = 60
IMAGE_CACHE_TTL = 60 * 30

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0 Safari/537.36"
})


def get_cached(cache_dict, key, ttl):
    item = cache_dict.get(key)
    if not item:
        return None
    if time.time() - item["time"] > ttl:
        del cache_dict[key]
        return None
    return item["value"]


def set_cached(cache_dict, key, value):
    cache_dict[key] = {
        "time": time.time(),
        "value": value
    }


def clean_text(value: str) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def normalize_title(title: str) -> str:
    title = clean_text(title).lower()
    title = re.sub(r"[^a-z0-9\s]", "", title)
    return title


def get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def extract_og_image(article_url: str) -> str:
    cached = get_cached(image_cache, article_url, IMAGE_CACHE_TTL)
    if cached is not None:
        return cached

    try:
        resp = SESSION.get(article_url, timeout=8)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        selectors = [
            ("meta", {"property": "og:image"}),
            ("meta", {"name": "og:image"}),
            ("meta", {"property": "twitter:image"}),
            ("meta", {"name": "twitter:image"}),
        ]

        for tag_name, attrs in selectors:
            tag = soup.find(tag_name, attrs=attrs)
            if tag and tag.get("content"):
                img = tag["content"].strip()
                if img.startswith("http"):
                    set_cached(image_cache, article_url, img)
                    return img

    except Exception:
        pass

    set_cached(image_cache, article_url, "")
    return ""


def source_weight(domain: str) -> int:
    weights = {
        "bbc.co.uk": 5,
        "reuters.com": 5,
        "apnews.com": 5,
        "cnn.com": 4,
        "npr.org": 4,
    }
    for k, v in weights.items():
        if k in domain:
            return v
    return 1


def score_article(item: dict) -> int:
    score = 0

    domain = item.get("source_domain", "")
    score += source_weight(domain)

    title = (item.get("title") or "").lower()

    urgent_terms = [
        "breaking", "attack", "war", "election", "crisis",
        "earthquake", "storm", "wildfire", "alert", "death"
    ]
    for term in urgent_terms:
        if term in title:
            score += 2

    if item.get("image"):
        score += 2

    if item.get("description"):
        score += 1

    return score


def normalize_item(raw: dict) -> dict:
    link = raw.get("link", "").strip()
    thumbnail = (raw.get("thumbnail") or "").strip()

    image = thumbnail if thumbnail.startswith("http") else ""
    if not image and link:
        image = extract_og_image(link)

    domain = get_domain(link)

    return {
        "title": clean_text(raw.get("title", "")),
        "link": link,
        "description": clean_text(raw.get("description", "")),
        "content": clean_text(raw.get("content", "")),
        "pubDate": raw.get("pubDate", ""),
        "author": clean_text(raw.get("author", "")) or domain or "Source",
        "image": image,
        "source_domain": domain,
    }


def dedupe_articles(items: list[dict]) -> list[dict]:
    seen = set()
    result = []

    for item in items:
        key = normalize_title(item.get("title", ""))
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(item)

    return result


def fetch_feed(feed_url: str) -> list[dict]:
    cached = get_cached(feed_cache, feed_url, FEED_CACHE_TTL)
    if cached is not None:
        return cached

    url = RSS2JSON_URL + feed_url
    try:
        resp = SESSION.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        set_cached(feed_cache, feed_url, items)
        return items
    except Exception:
        return []


@app.get("/api/news")
def get_news(category: str = "world"):
    if category not in CATEGORY_FEEDS:
        return {"error": "invalid category", "items": []}

    all_items = []

    for feed in CATEGORY_FEEDS[category]:
        items = fetch_feed(feed)
        all_items.extend(items[:12])

    normalized = [normalize_item(item) for item in all_items if item.get("link")]
    deduped = dedupe_articles(normalized)

    for item in deduped:
        item["score"] = score_article(item)

    ranked = sorted(
        deduped,
        key=lambda x: (x["score"], x.get("pubDate", "")),
        reverse=True
    )

    return {
        "status": "ok",
        "category": category,
        "count": len(ranked),
        "items": ranked[:30]
    }