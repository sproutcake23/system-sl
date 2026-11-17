"""Cross-source story clustering for Daily Intel.

Exact URLs are already deduplicated by :mod:`system_sl.intel.sources`. This
module goes one step further and groups articles that different publishers
wrote about the same underlying event, without embeddings or a vector database.

The signal is deliberately conservative:

* titles and descriptions are normalized into significant unigrams and bigrams;
* proper-noun-ish entities (brands, products, versions) add weight;
* publication times must fall inside a 72-hour window;
* at least two significant terms must be shared, so unrelated articles that
  merely share a common word are not merged.

Every original URL and source is preserved on the cluster. The briefing shows
one primary article per story and the other coverage is available from the
card.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Iterable, Optional

from system_sl.intel.sources import canonicalize_url, parse_published, title_similarity
from system_sl.intel.topics import STOPWORDS, normalize_text, tokenize

STORY_WINDOW_HOURS = 72
STORY_THRESHOLD = 0.50
SYNDICATION_THRESHOLD = 0.93
MIN_SHARED_TERMS = 2
MAX_CLUSTER_MEMBERS = 8

GENERIC_TERMS = frozenset(
    {
        "ai",
        "artificial",
        "intelligence",
        "new",
        "latest",
        "announce",
        "announces",
        "announced",
        "announcement",
        "release",
        "releases",
        "released",
        "launch",
        "launches",
        "launched",
        "unveil",
        "unveils",
        "unveiled",
        "introduce",
        "introduces",
        "introduced",
        "update",
        "updates",
        "updated",
        "report",
        "reports",
        "reported",
        "says",
        "said",
        "company",
        "companies",
        "tech",
        "technology",
        "major",
        "first",
        "now",
        "available",
        "today",
        "week",
        "year",
        "model",
        "models",
        "version",
        "versions",
        "feature",
        "features",
        "support",
        "supports",
        "users",
        "user",
        "future",
        "research",
        "study",
        "details",
        "here",
        "everything",
        "know",
        "explained",
        "guide",
    }
)

KNOWN_ENTITIES = frozenset(
    {
        "openai", "anthropic", "google", "deepmind", "microsoft", "apple",
        "amazon", "meta", "nvidia", "amd", "intel", "tsmc", "samsung",
        "tesla", "spacex", "waymo", "github", "gitlab", "mozilla", "linux",
        "kubernetes", "docker", "python", "javascript", "typescript", "rust",
        "golang", "java", "kotlin", "swift", "arxiv", "nature", "ieee",
        "chatgpt", "gemini", "claude", "llama", "mistral", "gpt", "nasa",
        "esa", "eu", "fda", "cisa", "android", "ios", "windows", "macos",
        "iphone", "ipad", "macbook", "chrome", "firefox", "playstation",
        "xbox", "nintendo", "hugging face", "huggingface", "langchain",
        "nist", "mit", "stanford", "berkeley",
    }
)

SENTENCE_START = frozenset(
    {
        "the", "a", "an", "new", "how", "why", "what", "when", "where",
        "who", "this", "that", "these", "those", "it", "we", "you", "they",
        "he", "she", "i", "after", "before", "inside", "meet", "first",
        "last", "top", "best", "more", "most", "here", "everything",
        "explained", "breaking", "exclusive", "opinion", "analysis", "video",
    }
)

_ENTITY_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9]*(?:[.\-][A-Za-z0-9]+)*(?:\s+[A-Z][A-Za-z0-9]*(?:[.\-][A-Za-z0-9]+)*)*\b"
)
_PREFIX_RE = re.compile(
    r"^\s*(?:\[[^\]]+\]|\([^)]+\)|video|live|breaking|exclusive|opinion|analysis)\s*[:\-–—]\s*",
    re.IGNORECASE,
)


def _timestamp(article: dict) -> Optional[datetime]:
    for key in ("published_at", "fetched_at"):
        parsed = parse_published(article.get(key))
        if parsed:
            try:
                value = datetime.fromisoformat(parsed)
            except ValueError:
                continue
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
    return None


def normalized_title(article_or_title: object) -> str:
    title = (
        article_or_title.get("title")
        if isinstance(article_or_title, dict)
        else article_or_title
    )
    text = normalize_text(title)
    text = _PREFIX_RE.sub("", text)
    return " ".join(text.split())


def title_terms(article_or_title: object) -> set[str]:
    """Significant unigrams plus bigrams from a title."""
    tokens = [
        token
        for token in tokenize(normalized_title(article_or_title))
        if token not in GENERIC_TERMS
    ]
    result: set[str] = set(tokens)
    for left, right in zip(tokens, tokens[1:]):
        result.add(f"{left} {right}")
    return result


def significant_terms(article: dict) -> set[str]:
    """Unigrams used for candidate blocking and the minimum-overlap gate."""
    return {
        term
        for term in title_terms(article)
        if " " not in term and term not in STOPWORDS
    }


def entities(article: dict) -> set[str]:
    """Extract brand/product/version-like entities from the raw title."""
    raw = str(article.get("title") or "")
    found: set[str] = set()
    for match in _ENTITY_RE.finditer(raw):
        words = match.group(0).split()
        while words and words[0].lower() in SENTENCE_START:
            words.pop(0)
        while words and words[-1].lower() in GENERIC_TERMS:
            words.pop()
        if not words:
            continue
        phrase = " ".join(words).strip()
        lowered = phrase.lower()
        if not lowered or lowered in STOPWORDS:
            continue
        if len(words) == 1:
            if lowered in KNOWN_ENTITIES or any(char.isdigit() for char in lowered):
                found.add(lowered)
            continue
        if len(words) > 4:
            continue
        if lowered in KNOWN_ENTITIES or any(
            word.lower() in KNOWN_ENTITIES or word[0].isupper() for word in words[1:]
        ):
            found.add(lowered)
            found.update(word.lower() for word in words if word.lower() in KNOWN_ENTITIES)
    tokens = set(tokenize(raw))
    for entity in KNOWN_ENTITIES:
        if entity in tokens or (len(entity) > 3 and entity in raw.lower()):
            found.add(entity)
    return {item for item in found if item and item not in GENERIC_TERMS}


def _jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    left_set, right_set = set(left), set(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def _within_window(left: dict, right: dict, window_hours: float) -> bool:
    left_time = _timestamp(left)
    right_time = _timestamp(right)
    if left_time is None or right_time is None:
        return True
    return abs((left_time - right_time).total_seconds()) <= window_hours * 3600


def story_similarity(
    left: dict,
    right: dict,
    *,
    window_hours: float = STORY_WINDOW_HOURS,
) -> tuple[float, str]:
    """Return ``(score, kind)`` for two articles.

    ``kind`` is ``"duplicate"``, ``"syndication"``, ``"story"`` or ``""`` when
    the pair must not be merged.
    """
    if not isinstance(left, dict) or not isinstance(right, dict):
        return 0.0, ""

    left_url = canonicalize_url(left.get("url"))
    right_url = canonicalize_url(right.get("url"))
    if left_url and left_url == right_url:
        return 1.0, "duplicate"
    if left.get("id") and left.get("id") == right.get("id"):
        return 1.0, "duplicate"

    if not _within_window(left, right, window_hours):
        return 0.0, ""

    left_title = normalized_title(left)
    right_title = normalized_title(right)
    if not left_title or not right_title:
        return 0.0, ""

    raw_title_ratio = max(
        title_similarity(left_title, right_title),
        SequenceMatcher(None, left_title, right_title).ratio(),
    )
    left_terms = title_terms(left)
    right_terms = title_terms(right)
    shared = {
        term
        for term in (left_terms & right_terms)
        if " " not in term and term not in GENERIC_TERMS
    }
    shared_bigrams = {
        term for term in (left_terms & right_terms) if " " in term
    }

    if raw_title_ratio >= SYNDICATION_THRESHOLD:
        # Identical or near-identical wording is a syndicated copy.
        return raw_title_ratio, "syndication"

    if len(shared) < MIN_SHARED_TERMS:
        return 0.0, ""

    title_jaccard = _jaccard(left_terms, right_terms)
    description_jaccard = _jaccard(
        set(tokenize(left.get("description"))[:40]),
        set(tokenize(right.get("description"))[:40]),
    )
    entity_jaccard = _jaccard(entities(left), entities(right))

    score = (
        0.55 * max(raw_title_ratio, title_jaccard)
        + 0.20 * description_jaccard
        + 0.15 * entity_jaccard
        + 0.10 * min(1.0, (len(shared) + len(shared_bigrams)) / 3.0)
    )
    score = min(1.0, score)
    if score >= STORY_THRESHOLD:
        return score, "story"
    return score, ""


def _primary_index(members: list[dict]) -> int:
    """Pick the most useful, most authoritative member to display."""

    def key(item: tuple[int, dict]):
        index, article = item
        timestamp = _timestamp(article)
        return (
            int(article.get("reliability") or 0),
            article.get("content_status") == "full",
            len(str(article.get("description") or "")),
            timestamp.timestamp() if timestamp else 0.0,
            -index,
        )

    return max(enumerate(members), key=key)[0]


def _coverage_entry(article: dict) -> dict:
    return {
        "id": article.get("id"),
        "source_id": article.get("source_id"),
        "source_name": article.get("source_name"),
        "title": article.get("title"),
        "url": article.get("url"),
        "published_at": article.get("published_at"),
    }


def _finalize(members: list[dict], kind: str, similarity: float, window_hours: float) -> dict:
    primary_index = _primary_index(members)
    primary = dict(members[primary_index])
    others = [article for index, article in enumerate(members) if index != primary_index]
    others.sort(
        key=lambda article: (
            int(article.get("reliability") or 0),
            str(article.get("published_at") or ""),
        ),
        reverse=True,
    )
    identifiers = sorted(str(article.get("id") or article.get("url")) for article in members)
    cluster_id = "story-" + hashlib.sha1("|".join(identifiers).encode("utf-8")).hexdigest()[:12]
    primary["story_cluster"] = cluster_id
    primary["story_ids"] = [
        str(article.get("id")) for article in members if article.get("id") != primary.get("id")
    ]
    sources: list[dict] = []
    for article in [primary, *others]:
        source = {
            "id": article.get("source_id"),
            "name": article.get("source_name"),
        }
        if source not in sources:
            sources.append(source)
    return {
        "id": cluster_id,
        "kind": kind or "single",
        "size": len(members),
        "primary": primary.get("id"),
        "article": primary,
        "articles": members,
        "sources": sources,
        "coverage": [_coverage_entry(article) for article in others],
        "title_similarity": round(similarity, 3),
        "window_hours": window_hours,
    }


def single_cluster(article: dict) -> dict:
    return _finalize([dict(article)], "", 1.0, STORY_WINDOW_HOURS)


def cluster_articles(
    articles: Iterable[dict],
    *,
    now: Optional[datetime] = None,
    window_hours: float = STORY_WINDOW_HOURS,
    threshold: float = STORY_THRESHOLD,
) -> list[dict]:
    """Group articles into story clusters and return them, newest first."""
    items = [article for article in articles if isinstance(article, dict)]
    items.sort(
        key=lambda article: (
            str(article.get("published_at") or ""),
            str(article.get("fetched_at") or ""),
        ),
        reverse=True,
    )

    clusters: list[dict] = []  # internal: {"members", "kind", "similarity"}
    term_index: dict[str, list[int]] = {}

    for article in items:
        article = dict(article)
        terms = significant_terms(article)
        candidate_indexes: set[int] = set()
        for term in terms:
            candidate_indexes.update(term_index.get(term, ()))

        best_index: Optional[int] = None
        best_score = 0.0
        best_kind = ""
        for index in candidate_indexes:
            cluster = clusters[index]
            for member in cluster["members"]:
                score, kind = story_similarity(article, member, window_hours=window_hours)
                if score > best_score:
                    best_index, best_score, best_kind = index, score, kind

        if (
            best_index is not None
            and best_score >= threshold
            and len(clusters[best_index]["members"]) < MAX_CLUSTER_MEMBERS
        ):
            cluster = clusters[best_index]
            cluster["members"].append(article)
            cluster["similarity"] = max(cluster["similarity"], best_score)
            cluster["kind"] = _merge_kind(cluster["kind"], best_kind)
            for term in terms:
                bucket = term_index.setdefault(term, [])
                if best_index not in bucket:
                    bucket.append(best_index)
            continue

        index = len(clusters)
        clusters.append(
            {
                "members": [article],
                "kind": best_kind if best_kind == "duplicate" else "",
                "similarity": 1.0 if best_kind == "duplicate" else 0.0,
            }
        )
        for term in terms:
            term_index.setdefault(term, []).append(index)

    finalized = [
        _finalize(cluster["members"], cluster["kind"], cluster["similarity"], window_hours)
        for cluster in clusters
    ]
    finalized.sort(
        key=lambda cluster: (
            str(cluster["article"].get("published_at") or ""),
            str(cluster["article"].get("fetched_at") or ""),
        ),
        reverse=True,
    )
    return finalized


def _merge_kind(current: str, new: str) -> str:
    order = {"": 0, "story": 1, "syndication": 2, "duplicate": 3}
    return new if order.get(new, 0) > order.get(current, 0) else current


def cluster_for_article(clusters: Iterable[dict], article_id: str) -> Optional[dict]:
    for cluster in clusters:
        if cluster.get("primary") == article_id:
            return cluster
    return None
