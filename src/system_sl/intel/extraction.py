"""Selective full-article extraction for Daily Intel.

Daily Intel does not download every page it collects. Extraction runs only for
articles that are being considered for a briefing and whose feed description is
too thin to summarize honestly. Results are cached so the same URL is not
fetched twice, and failures fall back to the RSS excerpt.

The extractor is deliberately lightweight: standard-library HTML parsing plus
``requests``. It removes navigation, footers, cookie banners, newsletter
prompts and similar boilerplate, and never attempts to bypass a paywall or an
access restriction.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from system_sl.intel.sources import USER_AGENT, canonicalize_url
from system_sl.intel.topics import strip_html

STATUS_FULL = "full"
STATUS_EXCERPT = "excerpt"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"

MIN_USEFUL_DESCRIPTION_CHARS = 420
MIN_ARTICLE_CHARS = 350
MAX_RESPONSE_BYTES = 1_500_000
MAX_STORED_CHARS = 12_000
REQUEST_TIMEOUT = 8.0

CACHE_TTL_HOURS = {
    STATUS_FULL: 14 * 24,
    STATUS_FAILED: 24,
    STATUS_BLOCKED: 7 * 24,
}

BLOCK_STATUSES = (401, 402, 403, 451)
BOILERPLATE_HINTS = (
    "cookie",
    "consent",
    "newsletter",
    "subscribe",
    "subscription",
    "advert",
    "sponsor",
    "promo",
    "share",
    "social",
    "comment",
    "related",
    "sidebar",
    "navbar",
    "breadcrumb",
    "paywall",
    "banner",
    "footer",
    "header",
    "signup",
    "sign-up",
    "modal",
    "popup",
)
BLOCK_TAGS = {"p", "h2", "h3", "h4", "blockquote", "pre", "li"}
SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "nav",
    "footer",
    "header",
    "aside",
    "form",
    "iframe",
    "svg",
    "button",
    "select",
    "option",
    "menu",
    "dialog",
    "template",
}
PAYWALL_MARKERS = (
    "subscribe to continue",
    "subscribers only",
    "subscriber-only",
    "sign in to read",
    "log in to continue",
    "this article is for",
    "already a subscriber",
    "create a free account to read",
    "paywall",
)
BOILERPLATE_TEXT = re.compile(
    r"^(advertisement|share this|follow us|sign up for|subscribe to our|"
    r"all rights reserved|cookie policy|privacy policy|terms of service)\b",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _normalize(text: object) -> str:
    return " ".join(str(text or "").split())


def _is_boilerplate(attrs) -> bool:
    for key, value in attrs or ():
        if key in ("class", "id", "role", "aria-label", "data-testid") and value:
            lowered = str(value).lower()
            if any(hint in lowered for hint in BOILERPLATE_HINTS):
                return True
    return False


class _ReadableParser(HTMLParser):
    """Collect readable paragraphs while skipping page furniture."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self.text_parts: list[str] = []
        self._skip_stack: list[str] = []
        self._capture_tag: str | None = None
        self._capture_depth = 0
        self._capture_parts: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        self._depth += 1
        if self._skip_stack:
            if tag not in SKIP_TAGS:
                self._skip_stack.append(tag)
            return
        if tag in SKIP_TAGS or _is_boilerplate(attrs):
            self._skip_stack.append(tag)
            return
        if tag in BLOCK_TAGS and self._capture_tag is None:
            self._capture_tag = tag
            self._capture_depth = self._depth
            self._capture_parts = []

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if not self._skip_stack and tag in SKIP_TAGS:
            return

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_stack:
            if tag == self._skip_stack[-1]:
                self._skip_stack.pop()
            elif tag in self._skip_stack:
                while self._skip_stack and self._skip_stack[-1] != tag:
                    self._skip_stack.pop()
                if self._skip_stack:
                    self._skip_stack.pop()
            self._depth = max(0, self._depth - 1)
            return
        if self._capture_tag == tag and self._capture_depth == self._depth:
            text = _normalize(" ".join(self._capture_parts))
            if text:
                self.blocks.append((tag, text))
            self._capture_tag = None
            self._capture_parts = []
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data):
        if not data:
            return
        if self._skip_stack:
            return
        if self._capture_tag is not None:
            self._capture_parts.append(data)
        self.text_parts.append(data)

    def close(self):
        super().close()
        if self._capture_tag is not None:
            text = _normalize(" ".join(self._capture_parts))
            if text:
                self.blocks.append((self._capture_tag, text))
            self._capture_tag = None


def extract_readable_text(payload: object, url: str = "") -> str:
    """Return the main readable text from an HTML document.

    Paragraph and heading blocks are preferred. If a page has no usable blocks
    (for example, a JavaScript-only page), the visible text is returned instead;
    callers treat a short or empty result as an extraction failure.
    """
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8", errors="replace")
        except Exception:
            return ""
    elif isinstance(payload, str):
        text = payload
    else:
        return ""

    marker = url.lower()
    if marker.endswith((".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".mp4")):
        return ""

    parser = _ReadableParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        return ""

    blocks: list[str] = []
    previous = ""
    for _tag, block in parser.blocks:
        if len(block) < 40 or block == previous:
            continue
        if BOILERPLATE_TEXT.match(block):
            continue
        blocks.append(block)
        previous = block

    if blocks:
        extracted = "\n\n".join(blocks)
    else:
        extracted = _normalize(" ".join(parser.text_parts))

    extracted = _collapse_repeated_lines(extracted)
    return extracted[:MAX_STORED_CHARS].strip()


def _collapse_repeated_lines(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(line)
    if len(unique) > 1:
        return "\n\n".join(unique)
    return " ".join(unique)


def needs_extraction(article: dict, *, min_chars: int = MIN_USEFUL_DESCRIPTION_CHARS) -> bool:
    """Whether the feed description is too thin to summarize honestly."""
    description = strip_html(article.get("description"))
    if not description:
        return True
    if len(description) < min_chars:
        return True
    lowered = description.lower()
    return "read more" in lowered or "continue reading" in lowered


def _parse_time(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_limited(response: object, limit: int = MAX_RESPONSE_BYTES) -> tuple[Optional[bytes], str]:
    """Read a response body while respecting a hard size limit."""
    headers = getattr(response, "headers", None) or {}
    try:
        declared = headers.get("Content-Length") or headers.get("content-length")
        if declared is not None and int(declared) > limit:
            return None, "response too large"
    except (TypeError, ValueError):
        pass

    chunks: list[bytes] = []
    total = 0
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        try:
            for chunk in iterator(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    return None, "response too large"
                chunks.append(chunk)
            return b"".join(chunks), ""
        except Exception as exc:  # noqa: BLE001 - read failures become a status
            return None, f"read failed: {type(exc).__name__}"

    content = getattr(response, "content", None)
    if content is None:
        content = getattr(response, "text", "")
    if isinstance(content, str):
        content = content.encode("utf-8", errors="replace")
    if not isinstance(content, (bytes, bytearray)):
        return None, "empty response"
    if len(content) > limit:
        return None, "response too large"
    return bytes(content), ""


class RobotsPolicy:
    """Best-effort robots.txt gate, cached per host.

    If robots.txt cannot be read, the page is allowed: publisher availability
    problems should not silently turn into a global extraction block. The gate
    only ever *prevents* requests; it never bypasses access controls.
    """

    def __init__(self, *, session=None, timeout: float = 4.0) -> None:
        self.session = session
        self.timeout = timeout
        # Rules are cached per host, not per URL: robots.txt rules are
        # path-specific, so a single allowed/blocked boolean would be wrong.
        self._cache: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                return False
            host = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
        except ValueError:
            return False

        with self._lock:
            if host in self._cache:
                rules = self._cache[host]
                return True if rules is None else rules.can_fetch(USER_AGENT, url)

        rules: RobotFileParser | None = None
        try:
            robots_url = host + "/robots.txt"
            getter = self.session.get if self.session is not None else None
            if getter is None:
                import requests

                getter = requests.get
            response = getter(
                robots_url,
                timeout=self.timeout,
                headers={"User-Agent": USER_AGENT},
            )
            status = int(getattr(response, "status_code", 200) or 200)
            if status < 400:
                body = getattr(response, "text", None)
                if not isinstance(body, str):
                    body = getattr(response, "content", b"").decode("utf-8", errors="replace")
                parser = RobotFileParser()
                parser.parse(body.splitlines())
                rules = parser
        except Exception:
            # Publisher availability problems should not silently become a
            # global extraction block. The gate only ever prevents requests.
            rules = None

        with self._lock:
            self._cache[host] = rules
        return True if rules is None else rules.can_fetch(USER_AGENT, url)


class ContentExtractor:
    """Fetches, extracts and caches article text."""

    def __init__(
        self,
        store,
        *,
        session=None,
        timeout: float = REQUEST_TIMEOUT,
        max_bytes: int = MAX_RESPONSE_BYTES,
        robots: RobotsPolicy | None = None,
        clock=None,
    ) -> None:
        self.store = store
        self.session = session
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.robots = robots if robots is not None else RobotsPolicy(session=session)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def cached(self, article_id: str) -> Optional[dict]:
        if not article_id:
            return None
        entry = self.store.get_content(article_id)
        if not isinstance(entry, dict):
            return None
        status = entry.get("status")
        if status not in (STATUS_FULL, STATUS_EXCERPT, STATUS_FAILED, STATUS_BLOCKED):
            return None
        if status == STATUS_FULL and not entry.get("text"):
            return None
        fetched = _parse_time(entry.get("fetched_at"))
        ttl = CACHE_TTL_HOURS.get(status)
        if fetched is None or ttl is None:
            return None
        if self.now() - fetched > timedelta(hours=ttl):
            return None
        return entry

    def clear(self) -> None:
        self.store.clear_content()

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def should_fetch(self, article: dict, *, source=None) -> bool:
        if source is not None and not getattr(source, "extract", True):
            return False
        url = article.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return False
        return needs_extraction(article)

    def status_for(self, article: dict) -> dict:
        """Return the known status without doing network work."""
        cached = self.cached(str(article.get("id") or ""))
        if cached:
            return cached
        if strip_html(article.get("description")):
            return {"status": STATUS_EXCERPT, "reason": "feed excerpt only"}
        return {"status": STATUS_FAILED, "reason": "no feed description"}

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def fetch(self, article: dict, *, source=None) -> dict:
        """Fetch and cache one article. Never raises."""
        article_id = str(article.get("id") or "")
        url = article.get("url")
        if not article_id or not isinstance(url, str):
            return self._store(article_id, {"status": STATUS_FAILED, "reason": "missing article URL"})
        if source is not None and not getattr(source, "extract", True):
            return {
                "status": STATUS_EXCERPT,
                "reason": "full-text extraction is disabled for this source",
                "url": url,
            }

        with self._lock:
            existing = self.cached(article_id)
        if existing is not None:
            return existing

        if not self.robots.allowed(url):
            return self._store(
                article_id,
                {
                    "status": STATUS_BLOCKED,
                    "reason": "robots.txt disallows this page",
                    "url": url,
                    "http_status": None,
                },
            )

        started = self.now()
        try:
            import requests

            getter = self.session.get if self.session is not None else requests.get
            response = getter(
                url,
                timeout=self.timeout,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.5",
                },
                stream=True,
            )
        except Exception as exc:  # noqa: BLE001 - extraction failures are normal
            return self._store(
                article_id,
                {
                    "status": STATUS_FAILED,
                    "reason": f"{type(exc).__name__}: {exc}"[:300],
                    "url": url,
                    "fetched_at": started.isoformat(),
                },
            )

        try:
            status = int(getattr(response, "status_code", 200) or 200)
        except (TypeError, ValueError):
            status = 0

        try:
            if status in BLOCK_STATUSES:
                return self._store(
                    article_id,
                    {
                        "status": STATUS_BLOCKED,
                        "reason": f"publisher access restriction (HTTP {status})",
                        "url": url,
                        "http_status": status,
                        "fetched_at": started.isoformat(),
                    },
                )
            if status == 429:
                return self._store(
                    article_id,
                    {
                        "status": STATUS_FAILED,
                        "reason": "HTTP 429 (rate limited)",
                        "url": url,
                        "http_status": status,
                        "fetched_at": started.isoformat(),
                    },
                )
            if status >= 400:
                return self._store(
                    article_id,
                    {
                        "status": STATUS_FAILED,
                        "reason": f"HTTP {status}",
                        "url": url,
                        "http_status": status,
                        "fetched_at": started.isoformat(),
                    },
                )

            headers = getattr(response, "headers", None) or {}
            content_type = str(headers.get("Content-Type") or headers.get("content-type") or "").lower()
            if content_type and "html" not in content_type and "text/" not in content_type:
                return self._store(
                    article_id,
                    {
                        "status": STATUS_FAILED,
                        "reason": f"unsupported content type: {content_type[:80]}",
                        "url": url,
                        "http_status": status,
                        "fetched_at": started.isoformat(),
                    },
                )

            payload, error = _read_limited(response, self.max_bytes)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

        if payload is None:
            return self._store(
                article_id,
                {
                    "status": STATUS_FAILED,
                    "reason": error or "could not read the page",
                    "url": url,
                    "http_status": status,
                    "fetched_at": started.isoformat(),
                },
            )

        raw_text = payload.decode("utf-8", errors="replace")
        extracted = extract_readable_text(raw_text, url)
        lowered = raw_text.lower()
        paywall = any(marker in lowered for marker in PAYWALL_MARKERS)

        if extracted and paywall and len(extracted) < 900:
            return self._store(
                article_id,
                {
                    "status": STATUS_BLOCKED,
                    "reason": "publisher access restriction",
                    "url": url,
                    "http_status": status,
                    "fetched_at": started.isoformat(),
                },
            )
        if len(extracted) < MIN_ARTICLE_CHARS:
            return self._store(
                article_id,
                {
                    "status": STATUS_FAILED,
                    "reason": "could not extract readable article text",
                    "url": url,
                    "http_status": status,
                    "fetched_at": started.isoformat(),
                },
            )

        entry = {
            "status": STATUS_FULL,
            "text": extracted,
            "chars": len(extracted),
            "url": canonicalize_url(url),
            "http_status": status,
            "reason": "",
            "fetched_at": started.isoformat(),
        }
        return self._store(article_id, entry)

    def _store(self, article_id: str, entry: dict) -> dict:
        entry = {**entry, "fetched_at": entry.get("fetched_at") or self.now().isoformat()}
        if article_id:
            with self._lock:
                self.store.save_content(article_id, entry)
        return entry
