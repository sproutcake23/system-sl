"""Topic taxonomy and lightweight text helpers for Daily Intel.

The first version deliberately uses deterministic keyword matching rather than
embeddings. It is fast, works offline, and every recommendation can be
explained to the player.
"""

from __future__ import annotations

import html
import re
import unicodedata
from html.parser import HTMLParser

GENERAL_TOPIC = "General Technology"

# Ordered roughly from most specific to broadest. ``classify()`` picks the
# topic with the strongest keyword evidence, so order only breaks ties.
TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "AI & Machine Learning": (
        "artificial intelligence", "machine learning", "deep learning",
        "neural network", "neural networks", "large language model",
        "language model", "llm", "llms", "transformer", "transformers",
        "generative ai", "genai", "foundation model", "diffusion model",
        "reinforcement learning", "computer vision", "nlp",
        "natural language processing", "inference", "fine-tuning", "rag",
        "retrieval augmented generation", "agent", "agents", "agentic",
        "openai", "anthropic", "deepmind", "gemini", "chatgpt", "claude",
        "llama", "mistral", "hugging face", "ai model", "ai research",
    ),
    "Robotics": (
        "robot", "robots", "robotics", "robotic", "humanoid", "humanoids",
        "autonomous vehicle", "autonomous driving", "self-driving", "drone",
        "drones", "actuator", "manipulator", "embodied ai", "boston dynamics",
        "waymo", "tesla bot", "optimus",
    ),
    "Software & Developer Tools": (
        "software", "developer", "developers", "programming", "python",
        "javascript", "typescript", "rust", "golang", "java", "kotlin",
        "swift", "api", "apis", "sdk", "framework", "library", "github",
        "gitlab", "devops", "ci/cd", "container", "docker", "kubernetes",
        "compiler", "debugging", "code", "coding", "ide", "vscode",
        "open source", "maintainer", "release", "developer tools",
    ),
    "Cybersecurity": (
        "cybersecurity", "security", "vulnerability", "vulnerabilities",
        "exploit", "exploits", "malware", "ransomware", "phishing",
        "breach", "data breach", "zero-day", "zero day", "cve", "patch",
        "encryption", "authentication", "passkey", "passkeys", "privacy",
        "threat actor", "hacker", "hackers", "botnet", "spyware",
        "supply chain attack", "password", "passwords",
    ),
    "Science & Research": (
        "research", "study", "scientists", "science", "physics", "chemistry",
        "biology", "mathematics", "arxiv", "paper", "papers", "peer-reviewed",
        "experiment", "experiments", "discovery", "discoveries", "quantum",
        "particle", "astronomy", "astrophysics", "climate", "neuroscience",
        "genome", "genomics", "laboratory", "journal",
    ),
    "Hardware & Chips": (
        "chip", "chips", "semiconductor", "semiconductors", "processor",
        "processors", "gpu", "gpus", "cpu", "cpus", "nvidia", "amd", "intel",
        "tsmc", "arm", "risc-v", "silicon", "wafer", "fab", "transistor",
        "transistors", "memory", "dram", "hbm", "ssd", "hardware", "device",
        "devices", "laptop", "laptops", "smartphone", "smartphones",
    ),
    "Cloud & Infrastructure": (
        "cloud", "aws", "azure", "google cloud", "kubernetes", "serverless",
        "data center", "datacenter", "datacenters", "infrastructure",
        "platform", "saas", "paas", "iaas", "edge computing", "networking",
        "database", "databases", "storage", "distributed systems",
    ),
    "Business & Industry": (
        "business", "company", "companies", "startup", "startups", "funding",
        "investment", "investors", "valuation", "acquisition", "acquires",
        "merger", "ipo", "revenue", "earnings", "market", "markets",
        "regulation", "antitrust", "layoffs", "hiring", "enterprise",
        "industry", "economy", "financial", "finance",
    ),
    "Space": (
        "space", "nasa", "esa", "spacex", "rocket", "rockets", "satellite",
        "satellites", "orbit", "lunar", "moon", "mars", "astronaut",
        "astronauts", "telescope", "james webb", "launch vehicle",
        "space station", "asteroid", "exoplanet",
    ),
    "Health & Biotech": (
        "health", "medicine", "medical", "biotech", "biotechnology", "drug",
        "drugs", "vaccine", "vaccines", "clinical trial", "clinical trials",
        "disease", "cancer", "gene therapy", "crispr", "protein", "proteins",
        "hospital", "patient", "patients", "fda", "diagnosis", "therapy",
    ),
    "Open Source": (
        "open source", "open-source", "oss", "linux", "apache", "mozilla",
        "foundation", "community", "license", "licensing", "fork", "maintainers",
        "github", "gitlab", "copyleft",
    ),
    "Policy & Regulation": (
        "policy", "regulation", "regulations", "law", "laws", "legislation",
        "government", "congress", "parliament", "senate", "court", "legal",
        "compliance", "privacy law", "ai act", "executive order", "antitrust",
        "tariff", "tariffs", "sanctions",
    ),
    "Consumer Tech": (
        "consumer", "gadget", "gadgets", "iphone", "android", "windows",
        "macos", "chrome", "browser", "app", "apps", "wearable", "wearables",
        "smart home", "virtual reality", "vr", "augmented reality", "ar",
        "gaming", "console", "playstation", "xbox", "nintendo",
    ),
    GENERAL_TOPIC: (
        "technology", "tech", "innovation", "digital", "internet", "platform",
        "product", "launch", "update", "announcement", "future", "science",
    ),
}

TOPICS: tuple[str, ...] = tuple(TOPIC_KEYWORDS.keys())

# Topics that deserve extra weight in general discovery, even when the player
# has no persona or tasks.
HIGH_IMPACT_TOPICS = frozenset({
    "AI & Machine Learning",
    "Cybersecurity",
    "Science & Research",
    "Space",
    "Health & Biotech",
    "Policy & Regulation",
    "Hardware & Chips",
})

STOPWORDS = frozenset({
    "a", "about", "above", "after", "again", "against", "all", "am", "an",
    "and", "any", "are", "as", "at", "be", "because", "been", "before",
    "being", "below", "between", "both", "but", "by", "can", "could", "did",
    "do", "does", "doing", "down", "during", "each", "few", "for", "from",
    "further", "had", "has", "have", "having", "he", "her", "here", "hers",
    "herself", "him", "himself", "his", "how", "i", "if", "in", "into", "is",
    "it", "its", "itself", "just", "me", "more", "most", "my", "myself", "no",
    "nor", "not", "now", "of", "off", "on", "once", "only", "or", "other",
    "our", "ours", "ourselves", "out", "over", "own", "same", "she", "should",
    "so", "some", "such", "than", "that", "the", "their", "theirs", "them",
    "themselves", "then", "there", "these", "they", "this", "those",
    "through", "to", "too", "under", "until", "up", "very", "was", "we",
    "were", "what", "when", "where", "which", "while", "who", "whom", "why",
    "will", "with", "you", "your", "yours", "yourself", "yourselves",
    "new", "news", "says", "said", "also", "may", "might", "one", "two",
    "first", "last", "next", "today", "week", "year", "years", "day", "days",
    "get", "gets", "got", "make", "makes", "made", "use", "uses", "used",
    "using", "via", "per", "including", "include", "includes", "like", "just",
})

_TOKEN_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9+#.\-]*")


class _TextExtractor(HTMLParser):
    """Collect visible text from a fragment of HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style"}:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.parts.append(data)


def strip_html(text: object) -> str:
    """Return visible text from an HTML fragment, tolerating malformed input."""
    if not isinstance(text, str) or not text:
        return ""
    # Feed payloads are frequently double-escaped (``&lt;p&gt;``).
    value = html.unescape(text)
    if "<" not in value or ">" not in value:
        return _collapse(value)
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        value = " ".join(parser.parts)
    except Exception:
        value = re.sub(r"<[^>]+>", " ", value)
    return _collapse(html.unescape(value))


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_text(text: object) -> str:
    """Normalize text for matching: NFKC, HTML-free, lowercase, collapsed."""
    value = strip_html(text)
    value = unicodedata.normalize("NFKC", value)
    return _collapse(value).lower()


def tokenize(text: object, *, keep_stopwords: bool = False) -> list[str]:
    """Split text into meaningful lowercase tokens."""
    normalized = normalize_text(text)
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(normalized):
        token = raw.strip(".-")
        if len(token) < 2:
            continue
        if not keep_stopwords and token in STOPWORDS:
            continue
        if token.isdigit():
            continue
        tokens.append(token)
    return tokens


def terms(text: object, *, max_terms: int = 160) -> set[str]:
    """Return unigrams and bigrams used for keyword matching."""
    tokens = tokenize(text)
    result: set[str] = set(tokens)
    for left, right in zip(tokens, tokens[1:]):
        result.add(f"{left} {right}")
        if len(result) >= max_terms:
            break
    return result


def classify(text: object) -> tuple[str, float]:
    """Assign the best topic and a 0–100 confidence to ``text``.

    The score is keyword evidence, not a probability. It is only used to pick a
    label and to add a small relevance bonus, so a rough heuristic is enough.
    """
    normalized = normalize_text(text)
    if not normalized:
        return GENERAL_TOPIC, 0.0

    token_set = set(tokenize(normalized))
    scores: dict[str, float] = {}
    for topic, keywords in TOPIC_KEYWORDS.items():
        score = 0.0
        for keyword in keywords:
            if " " in keyword:
                if keyword in normalized:
                    score += 2.0
            elif keyword in token_set:
                score += 1.5 if len(keyword) >= 5 else 1.0
        if score:
            scores[topic] = score

    if not scores:
        return GENERAL_TOPIC, 0.0

    topic = max(scores, key=scores.get)
    confidence = min(100.0, scores[topic] * 18.0)
    if confidence < 18.0:
        return GENERAL_TOPIC, confidence
    return topic, confidence


def topic_preference(feedback: dict, topic: str) -> int:
    """Return the player's stored preference for a topic (−3…3)."""
    topics = feedback.get("topics") if isinstance(feedback, dict) else None
    if not isinstance(topics, dict):
        return 0
    try:
        value = int(topics.get(topic, 0))
    except (TypeError, ValueError):
        return 0
    return max(-3, min(3, value))
