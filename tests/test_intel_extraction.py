"""Tests for selective full-article extraction and caching."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from system_sl.intel.extraction import (
    STATUS_BLOCKED,
    STATUS_EXCERPT,
    STATUS_FAILED,
    STATUS_FULL,
    ContentExtractor,
    RobotsPolicy,
    extract_readable_text,
    needs_extraction,
)
from system_sl.intel.sources import NewsSource
from system_sl.intel.storage import IntelStore
from tests.intel_fixtures import (
    AllowAllRobots,
    BlockAllRobots,
    FakeResponse,
    FakeSession,
    make_article,
)

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
URL = "https://publisher.example.com/articles/python-toolkit"

PARAGRAPH = (
    "The new Python toolkit gives developers a faster way to build production "
    "services, with typed configuration and a small dependency footprint."
)
PARAGRAPH_TWO = (
    "Its parser handles streaming input, and the project ships with "
    "documentation and a migration guide for existing applications."
)
PARAGRAPH_THREE = (
    "Maintainers say the release focuses on reliability, clearer error "
    "messages, and predictable performance under heavy load."
)

ARTICLE_HTML = f"""<!DOCTYPE html>
<html><head><title>Python toolkit</title>
<script>window.analytics = {{track: true}};</script>
</head>
<body>
<nav>Home | News | Subscribe</nav>
<header class="site-header">Site menu and account links</header>
<article>
  <h1>A new Python toolkit arrives</h1>
  <p>{PARAGRAPH}</p>
  <p>{PARAGRAPH_TWO}</p>
  <p>{PARAGRAPH_THREE}</p>
</article>
<div class="newsletter">Sign up for our newsletter to receive updates.</div>
<footer>All rights reserved. Privacy policy. Terms of service.</footer>
</body></html>
"""

SOURCE = NewsSource(
    "publisher",
    "Publisher",
    "https://publisher.example.com/feed",
    "https://publisher.example.com",
    "Software & Developer Tools",
    85,
)


def article(**kwargs) -> dict:
    values = {
        "identifier": "a1",
        "title": "A new Python toolkit arrives",
        "description": "A short teaser.",
        "category": "Software & Developer Tools",
        "url": URL,
    }
    values.update(kwargs)
    return make_article(**values)


class ReadableTextTests(unittest.TestCase):
    def test_main_text_is_kept_and_boilerplate_is_removed(self):
        text = extract_readable_text(ARTICLE_HTML, URL)
        self.assertIn("faster way to build production services", text)
        self.assertNotIn("Home | News", text)
        self.assertNotIn("Site menu", text)
        self.assertNotIn("Sign up for our newsletter", text)
        self.assertNotIn("All rights reserved", text)
        self.assertNotIn("window.analytics", text)

    def test_script_only_page_has_no_readable_text(self):
        html = "<html><body><div id='root'></div><script>var x = 1;</script></body></html>"
        self.assertEqual(extract_readable_text(html, URL), "")

    def test_binary_and_non_html_payloads_return_empty_text(self):
        self.assertEqual(extract_readable_text(None, URL), "")
        self.assertEqual(extract_readable_text(b"", URL), "")
        self.assertEqual(extract_readable_text(ARTICLE_HTML, "https://example.com/file.pdf"), "")

    def test_thin_descriptions_need_extraction(self):
        self.assertTrue(needs_extraction(article(description="Short.")))
        self.assertTrue(needs_extraction(article(description="")))
        self.assertTrue(needs_extraction(article(description="Summary. Read more")))
        long_description = "This is a sufficiently detailed feed description. " * 12
        self.assertFalse(needs_extraction(article(description=long_description)))
        self.assertTrue(needs_extraction(article(description=long_description), min_chars=1000))


class ContentExtractorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = IntelStore(Path(self.temp.name))
        self.now = NOW

    def extractor(self, session, *, robots=None, max_bytes=1_500_000):
        return ContentExtractor(
            self.store,
            session=session,
            robots=robots if robots is not None else AllowAllRobots(),
            max_bytes=max_bytes,
            clock=lambda: self.now,
        )

    def test_full_article_is_extracted_and_cached(self):
        session = FakeSession({URL: FakeResponse(content=ARTICLE_HTML)})
        extractor = self.extractor(session)
        item = article()

        self.assertTrue(extractor.should_fetch(item, source=SOURCE))
        entry = extractor.fetch(item, source=SOURCE)
        self.assertEqual(entry["status"], STATUS_FULL)
        self.assertGreaterEqual(entry["chars"], 350)
        self.assertIn("faster way to build", entry["text"])
        self.assertEqual(entry["http_status"], 200)
        self.assertEqual(extractor.cached("a1")["status"], STATUS_FULL)

        # The same article is never fetched twice.
        calls_before = len(session.calls)
        again = extractor.fetch(item, source=SOURCE)
        self.assertEqual(again["status"], STATUS_FULL)
        self.assertEqual(len(session.calls), calls_before)

    def test_cache_survives_a_new_extractor_instance(self):
        session = FakeSession({URL: FakeResponse(content=ARTICLE_HTML)})
        self.extractor(session).fetch(article())
        second_session = FakeSession({})
        entry = self.extractor(second_session).fetch(article())
        self.assertEqual(entry["status"], STATUS_FULL)
        self.assertEqual(second_session.calls, [])

    def test_cache_expires(self):
        session = FakeSession({URL: FakeResponse(content=ARTICLE_HTML)})
        extractor = self.extractor(session)
        extractor.fetch(article())
        self.now = NOW + timedelta(days=20)
        self.assertIsNone(extractor.cached("a1"))

    def test_long_description_is_not_fetched(self):
        session = FakeSession({URL: FakeResponse(content=ARTICLE_HTML)})
        extractor = self.extractor(session)
        item = article(description=PARAGRAPH * 4)
        self.assertFalse(extractor.should_fetch(item, source=SOURCE))
        self.assertEqual(session.calls, [])

    def test_source_can_disable_extraction(self):
        session = FakeSession({URL: FakeResponse(content=ARTICLE_HTML)})
        extractor = self.extractor(session)
        no_extract = NewsSource(
            "no-extract", "No Extract", "https://x.example.com/feed", "https://x.example.com",
            "General Technology", 70, extract=False,
        )
        self.assertFalse(extractor.should_fetch(article(), source=no_extract))
        entry = extractor.fetch(article(), source=no_extract)
        self.assertEqual(entry["status"], STATUS_EXCERPT)
        self.assertEqual(session.calls, [])

    def test_http_error_becomes_failed_status(self):
        session = FakeSession({URL: FakeResponse(status_code=500)})
        entry = self.extractor(session).fetch(article())
        self.assertEqual(entry["status"], STATUS_FAILED)
        self.assertIn("500", entry["reason"])

    def test_transport_error_becomes_failed_status(self):
        session = FakeSession({URL: ConnectionError("offline")})
        entry = self.extractor(session).fetch(article())
        self.assertEqual(entry["status"], STATUS_FAILED)
        self.assertIn("ConnectionError", entry["reason"])

    def test_access_restriction_becomes_blocked(self):
        session = FakeSession({URL: FakeResponse(status_code=403)})
        entry = self.extractor(session).fetch(article())
        self.assertEqual(entry["status"], STATUS_BLOCKED)
        self.assertIn("403", entry["reason"])

    def test_paywalled_page_is_not_bypassed(self):
        html = (
            "<html><body><article>"
            "<p>Subscribe to continue reading this premium article.</p>"
            f"<p>{PARAGRAPH}</p><p>{PARAGRAPH}</p><p>{PARAGRAPH}</p>"
            "</article></body></html>"
        )
        session = FakeSession({URL: FakeResponse(content=html)})
        entry = self.extractor(session).fetch(article())
        self.assertEqual(entry["status"], STATUS_BLOCKED)
        self.assertIn("access restriction", entry["reason"])

    def test_oversized_response_is_rejected(self):
        session = FakeSession({URL: FakeResponse(content=ARTICLE_HTML)})
        entry = self.extractor(session, max_bytes=200).fetch(article())
        self.assertEqual(entry["status"], STATUS_FAILED)
        self.assertIn("too large", entry["reason"])

    def test_oversized_declared_content_length_is_rejected(self):
        response = FakeResponse(
            content=b"small",
            headers={"Content-Length": str(10 * 1024 * 1024)},
        )
        session = FakeSession({URL: response})
        entry = self.extractor(session).fetch(article())
        self.assertEqual(entry["status"], STATUS_FAILED)
        self.assertIn("too large", entry["reason"])

    def test_javascript_only_page_fails_gracefully(self):
        session = FakeSession(
            {
                URL: FakeResponse(
                    content="<html><body><div id='root'></div><script>render()</script></body></html>"
                )
            }
        )
        entry = self.extractor(session).fetch(article())
        self.assertEqual(entry["status"], STATUS_FAILED)
        self.assertIn("readable article text", entry["reason"])

    def test_robots_disallow_blocks_before_request(self):
        session = FakeSession({URL: FakeResponse(content=ARTICLE_HTML)})
        entry = self.extractor(session, robots=BlockAllRobots()).fetch(article())
        self.assertEqual(entry["status"], STATUS_BLOCKED)
        self.assertIn("robots", entry["reason"])
        self.assertEqual(session.calls, [])

    def test_robots_disallows_are_cached_per_host(self):
        session = FakeSession(
            {
                "https://publisher.example.com/robots.txt": FakeResponse(
                    content=b"User-agent: *\nDisallow: /private\n"
                )
            }
        )
        policy = RobotsPolicy(session=session)
        self.assertFalse(policy.allowed("https://publisher.example.com/private/story"))
        self.assertTrue(policy.allowed("https://publisher.example.com/public/story"))
        # A second check reuses the cached robots result.
        self.assertFalse(policy.allowed("https://publisher.example.com/private/other"))
        self.assertEqual(len(session.calls), 1)

    def test_unavailable_robots_does_not_block_extraction(self):
        policy = RobotsPolicy(session=FakeSession({}))
        self.assertTrue(policy.allowed("https://unknown.example.com/story"))

    def test_status_for_reports_excerpt_or_failure_without_network(self):
        session = FakeSession({})
        extractor = self.extractor(session)
        self.assertEqual(extractor.status_for(article())["status"], STATUS_EXCERPT)
        self.assertEqual(
            extractor.status_for(article(description=""))["status"], STATUS_FAILED
        )
        self.assertEqual(session.calls, [])

        # A cached full text is reported even if the feed description is empty.
        self.store.save_content(
            "a1",
            {
                "status": STATUS_FULL,
                "text": "Body " * 100,
                "fetched_at": NOW.isoformat(),
            },
        )
        self.assertEqual(extractor.status_for(article(description=""))["status"], STATUS_FULL)


if __name__ == "__main__":
    unittest.main()
