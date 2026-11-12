"""RSS/Atom collection for Daily Intel.

The parser is intentionally small and dependency-free: it uses the standard
library's ElementTree plus ``requests`` (already a project dependency). Each
source is isolated, so a malformed or unavailable feed cannot break the rest.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

from system_sl.intel.topics import classify, strip_html, tokenize

USER_AGENT = "System-SL-DailyIntel/1.0 (+https://github.com/sproutcake23/System-SL)"
MAX_ARTICLES_PER_FEED = 30
MAX_DESCRIPTION_CHARS = 2000
TRACKING_PARAM_PREFIXES = ("utm_", "ref", "source", "campaign", "fbclid", "gclid", "mc_cid", "mc_eid")


@dataclass(frozen=True)
class NewsSource:
    """A feed the player can enable or disable."""

    id: str
    name: str
    url: str
    homepage: str
    category: str
    reliability: int
    enabled: bool = True
    official: bool = False

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "homepage": self.homepage,
            "category": self.category,
            "reliability": self.reliability,
            "enabled": self.enabled,
            "official": self.official,
        }


# Reliability is a quality signal, not a claim of objectivity. Official
# company/research feeds rank highest; established publications follow.
DEFAULT_SOURCES: tuple[NewsSource, ...] = (
    NewsSource("openai", "OpenAI Blog", "https://openai.com/news/rss.xml", "https://openai.com/news", "AI & Machine Learning", 96, official=True),
    NewsSource("deepmind", "Google DeepMind", "https://deepmind.google/blog/rss.xml", "https://deepmind.google/blog", "AI & Machine Learning", 96, official=True),
    NewsSource("google-research", "Google Research", "https://research.google/blog/rss/", "https://research.google/blog", "AI & Machine Learning", 94, official=True),
    NewsSource("nvidia", "NVIDIA Blog", "https://blogs.nvidia.com/feed/", "https://blogs.nvidia.com", "Hardware & Chips", 92, official=True),
    NewsSource("github", "GitHub Blog", "https://github.blog/feed/", "https://github.blog", "Software & Developer Tools", 92, official=True),
    NewsSource("arxiv-ai", "arXiv cs.AI", "https://rss.arxiv.org/rss/cs.AI", "https://arxiv.org/list/cs.AI/recent", "AI & Machine Learning", 90, official=True),
    NewsSource("nature", "Nature", "https://www.nature.com/nature.rss", "https://www.nature.com", "Science & Research", 94, official=True),
    NewsSource("ieee-spectrum", "IEEE Spectrum", "https://spectrum.ieee.org/feeds/feed.rss", "https://spectrum.ieee.org", "Science & Research", 90),
    NewsSource("ars-technica", "Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "https://arstechnica.com", "General Technology", 86),
    NewsSource("mit-tech-review", "MIT Technology Review", "https://www.technologyreview.com/feed/", "https://www.technologyreview.com", "General Technology", 88),
    NewsSource("the-verge", "The Verge", "https://www.theverge.com/rss/index.xml", "https://www.theverge.com", "Consumer Tech", 82),
    NewsSource("techcrunch", "TechCrunch", "https://techcrunch.com/feed/", "https://techcrunch.com", "Business & Industry", 80),
    NewsSource("krebs", "Krebs on Security", "https://krebsonsecurity.com/feed/", "https://krebsonsecurity.com", "Cybersecurity", 90),
    NewsSource("hn", "Hacker News", "https://hnrss.org/frontpage", "https://news.ycombinator.com", "Software & Developer Tools", 72),
)

SOURCES_BY_ID: dict[str, NewsSource] = {source.id: source for source in DEFAULT_SOURCES}


@dataclass
class FetchResult:
    """Outcome of one collection pass."""

    articles: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    attempted: int = 0
    succeeded: int = 0

    @property
    def failed(self) -> int:
        return max(0, self.attempted - self.succeeded)


def canonicalize_url(url: object, base: str = "") -> str:
    """Normalize a URL so tracking variants deduplicate to one article."""
    if not isinstance(url, str):
        return ""
    value = html.unescape(url.strip())
    if not value:
        return ""
    if base:
        value = urljoin(base, value)

    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return ""

    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    path = re.sub(r"/+", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")

    kept = []
    for key, val in parse_qsl(parts.query, keep_blank_values=False):
        lowered = key.lower()
        if any(lowered == prefix or lowered.startswith(prefix) for prefix in TRACKING_PARAM_PREFIXES):
            continue
        kept.append((key, val))
    query = urlencode(sorted(kept))
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def article_id(url: object) -> str:
    canonical = canonicalize_url(url)
    if not canonical:
        return ""
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _local(tag: object) -> str:
    value = str(tag)
    return value.rsplit("}", 1)[-1].split(":")[-1].lower()


def _first_text(element: ElementTree.Element, names: Iterable[str]) -> str:
    wanted = {name.lower() for name in names}
    for child in element.iter():
        if child is element:
            continue
        if _local(child.tag) in wanted:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def _link_for(element: ElementTree.Element, base: str) -> str:
    # RSS: <link>https://…</link>, RDF: rdf:about, Atom: <link href="…">
    for child in element.iter():
        if _local(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        rel = child.attrib.get("rel", "alternate").lower()
        if href and rel in ("alternate", ""):
            return canonicalize_url(href, base)
        text = "".join(child.itertext()).strip()
        if text:
            return canonicalize_url(text, base)

    about = element.attrib.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about")
    if about:
        return canonicalize_url(about, base)

    guid = _first_text(element, ("guid", "id"))
    return canonicalize_url(guid, base)


def parse_published(value: object) -> Optional[str]:
    """Parse common RSS/Atom date formats into an ISO UTC string."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    # RFC 822 / RFC 2822 (RSS pubDate).
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(raw)
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        pass

    # ISO 8601 (Atom, arXiv).
    candidate = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d %b %Y %H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except ValueError:
            continue
    return None


def validate_article(raw: dict, source: NewsSource, *, now: Optional[datetime] = None) -> Optional[dict]:
    """Validate and normalize one article. Returns ``None`` when unusable."""
    if not isinstance(raw, dict):
        return None

    title = strip_html(raw.get("title"))
    url = canonicalize_url(raw.get("url"), source.homepage)
    if len(title) < 3 or len(title) > 300 or not url:
        return None

    description = strip_html(raw.get("description"))
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0].rstrip() + "…"

    published = raw.get("published_at")
    if published and not isinstance(published, str):
        published = None
    if isinstance(published, str):
        published = parse_published(published) or None

    category = raw.get("category")
    if not isinstance(category, str) or not category.strip():
        category, _ = classify(f"{title} {description}")
    else:
        category = category.strip()[:80]

    fetched = now or datetime.now(timezone.utc)
    return {
        "id": article_id(url),
        "title": title,
        "url": url,
        "description": description,
        "source_id": source.id,
        "source_name": source.name,
        "category": category,
        "reliability": int(source.reliability),
        "published_at": published,
        "fetched_at": fetched.astimezone(timezone.utc).isoformat(),
    }


def _items(root: ElementTree.Element) -> list[ElementTree.Element]:
    tag = _local(root.tag)
    if tag == "feed":  # Atom
        return [child for child in root.iter() if _local(child.tag) == "entry"]
    return [child for child in root.iter() if _local(child.tag) == "item"]


def parse_feed(payload: object, source: NewsSource, *, now: Optional[datetime] = None) -> list[dict]:
    """Parse an RSS 2.0, RSS 1.0 (RDF) or Atom feed payload."""
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8", errors="replace")
        except Exception:
            return []
    elif isinstance(payload, str):
        text = payload
    else:
        return []

    text = text.lstrip("\ufeff").strip()
    if not text:
        return []

    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        # A few feeds prepend junk or contain stray control characters.
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        try:
            root = ElementTree.fromstring(cleaned)
        except ElementTree.ParseError:
            return []

    articles: list[dict] = []
    for item in _items(root)[:MAX_ARTICLES_PER_FEED]:
        raw = {
            "title": _first_text(item, ("title",)),
            "url": _link_for(item, source.homepage),
            "description": _first_text(item, ("description", "summary", "content", "encoded", "subtitle")),
            "published_at": _first_text(item, ("pubdate", "published", "updated", "date", "created")),
        }
        article = validate_article(raw, source, now=now)
        if article:
            articles.append(article)
    return articles


def fetch_source(source: NewsSource, *, timeout: float = 10.0, session=None) -> list[dict]:
    """Fetch and parse one source. Raises on transport or parse failure."""
    import requests

    getter = session.get if session is not None else requests.get
    response = getter(
        source.url,
        timeout=timeout,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    status = getattr(response, "status_code", 200)
    if status >= 400:
        raise RuntimeError(f"HTTP {status}")
    content = getattr(response, "content", None)
    if content is None:
        content = getattr(response, "text", "")
    articles = parse_feed(content, source)
    if not articles:
        raise ValueError("feed contained no usable articles")
    return articles


def fetch_all(
    sources: Iterable[NewsSource],
    *,
    timeout: float = 8.0,
    session=None,
    now: Optional[datetime] = None,
    max_workers: int = 6,
) -> FetchResult:
    """Fetch every source in parallel, keeping failures local to their feed."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    feed_list = list(sources)
    result = FetchResult(attempted=len(feed_list))
    if not feed_list:
        return result

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(feed_list)))) as pool:
        futures = {
            pool.submit(fetch_source, source, timeout=timeout, session=session): source
            for source in feed_list
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                articles = future.result()
            except Exception as exc:  # noqa: BLE001 - one feed must not break the rest
                result.errors.append(f"{source.name}: {exc}")
                continue
            if now is not None:
                for article in articles:
                    article["fetched_at"] = now.astimezone(timezone.utc).isoformat()
            result.articles.extend(articles)
            result.succeeded += 1
    return result


def title_similarity(left: str, right: str) -> float:
    """Return 0–1 similarity between two titles, robust to punctuation."""
    left_tokens = set(tokenize(left))
    right_tokens = set(tokenize(right))
    if not left_tokens or not right_tokens:
        return 0.0
    jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence = SequenceMatcher(None, " ".join(sorted(left_tokens)), " ".join(sorted(right_tokens))).ratio()
    return max(jaccard, sequence)


def dedupe_articles(articles: Iterable[dict], *, threshold: float = 0.88) -> list[dict]:
    """Remove exact and near-duplicate articles, keeping the richest entry."""
    ordered = sorted(
        (article for article in articles if isinstance(article, dict)),
        key=lambda article: (
            bool(article.get("published_at")),
            article.get("published_at") or "",
            len(article.get("description") or ""),
        ),
        reverse=True,
    )

    kept: list[dict] = []
    by_id: dict[str, dict] = {}
    for article in ordered:
        identifier = article.get("id") or article_id(article.get("url"))
        if not identifier:
            continue
        article["id"] = identifier

        existing = by_id.get(identifier)
        if existing is not None:
            if len(article.get("description") or "") > len(existing.get("description") or ""):
                existing.update(article)
            continue

        if any(title_similarity(article.get("title", ""), other.get("title", "")) >= threshold for other in kept):
            continue

        by_id[identifier] = article
        kept.append(article)
    return kept


def merge_article_cache(cached: Iterable[dict], fresh: Iterable[dict], *, now: Optional[datetime] = None) -> list[dict]:
    """Merge fresh articles into the cache, preserving richer descriptions."""
    by_id: dict[str, dict] = {}
    for article in cached:
        if not isinstance(article, dict):
            continue
        identifier = article.get("id") or article_id(article.get("url"))
        if identifier:
            article["id"] = identifier
            by_id[identifier] = dict(article)

    for article in fresh:
        if not isinstance(article, dict):
            continue
        identifier = article.get("id") or article_id(article.get("url"))
        if not identifier:
            continue
        article["id"] = identifier
        existing = by_id.get(identifier)
        if existing is None or len(article.get("description") or "") > len(existing.get("description") or ""):
            by_id[identifier] = dict(article)

    merged = dedupe_articles(by_id.values())
    cutoff = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    recent: list[dict] = []
    for article in merged:
        stamp = article.get("published_at") or article.get("fetched_at")
        parsed = parse_published(stamp)
        if parsed:
            try:
                age_days = (cutoff - datetime.fromisoformat(parsed)).days
            except ValueError:
                age_days = 0
            if age_days > 45:
                continue
        recent.append(article)
    recent.sort(key=lambda article: article.get("published_at") or article.get("fetched_at") or "", reverse=True)
    return recent[:500]


def enabled_sources(disabled_ids: Iterable[str]) -> list[NewsSource]:
    disabled = {str(item) for item in disabled_ids}
    return [source for source in DEFAULT_SOURCES if source.id not in disabled]
