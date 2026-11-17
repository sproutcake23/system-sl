"""Tests for Daily Intel feed parsing and deduplication."""

from __future__ import annotations

import unittest

from system_sl.intel.sources import (
    NewsSource,
    canonicalize_url,
    dedupe_articles,
    merge_article_cache,
    parse_feed,
    parse_published,
    validate_article,
)

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example</title>
    <item>
      <title>OpenAI releases a Python toolkit</title>
      <link>https://example.com/news/toolkit?utm_source=feed</link>
      <description><![CDATA[<p>A new <b>machine learning</b> toolkit.</p>]]></description>
      <pubDate>Wed, 11 Nov 2026 10:00:00 GMT</pubDate>
    </item>
    <item>
      <title>No link here</title>
      <description>Should be skipped.</description>
    </item>
  </channel>
</rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <entry>
    <title>Cybersecurity update</title>
    <link rel="alternate" href="https://example.com/security/update" />
    <summary>A vulnerability was patched.</summary>
    <updated>2026-11-12T08:30:00Z</updated>
  </entry>
</feed>
"""

SOURCE = NewsSource(
    "example",
    "Example",
    "https://example.com/feed",
    "https://example.com",
    "General Technology",
    85,
)


class FeedTests(unittest.TestCase):
    def test_parse_rss_and_clean_html(self):
        articles = parse_feed(RSS, SOURCE)
        self.assertEqual(len(articles), 1)
        article = articles[0]
        self.assertEqual(article["title"], "OpenAI releases a Python toolkit")
        self.assertEqual(article["url"], "https://example.com/news/toolkit")
        self.assertIn("machine learning", article["description"])
        self.assertNotIn("<b>", article["description"])
        self.assertEqual(article["published_at"], "2026-11-11T10:00:00+00:00")

    def test_parse_atom(self):
        articles = parse_feed(ATOM, SOURCE)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["url"], "https://example.com/security/update")
        self.assertEqual(articles[0]["published_at"], "2026-11-12T08:30:00+00:00")

    def test_malformed_feed_returns_empty(self):
        self.assertEqual(parse_feed("<rss><channel><item>", SOURCE), [])
        self.assertEqual(parse_feed(b"\x00\x01\x02", SOURCE), [])

    def test_missing_metadata_skipped(self):
        self.assertIsNone(
            validate_article(
                {"title": "", "url": "https://example.com/x"},
                SOURCE,
            )
        )
        self.assertIsNone(
            validate_article(
                {"title": "Valid title", "url": "javascript:alert(1)"},
                SOURCE,
            )
        )

    def test_canonical_url_and_dedupe(self):
        left = "https://www.Example.com/a/?utm_source=x&b=2#frag"
        right = "https://example.com/a?b=2"
        self.assertEqual(canonicalize_url(left), canonicalize_url(right))

        first = {
            "id": "one",
            "title": "OpenAI releases a new model",
            "url": "https://example.com/a?utm_source=x",
            "description": "Short",
            "category": "AI & Machine Learning",
            "published_at": "2026-11-12T08:00:00+00:00",
        }
        duplicate = {
            "id": "two",
            "title": "OpenAI releases a new model!",
            "url": "https://example.com/a",
            "description": "A longer description of the same story",
            "category": "AI & Machine Learning",
            "published_at": "2026-11-12T08:00:00+00:00",
        }
        other = {
            "id": "three",
            "title": "Space telescope observes a distant galaxy",
            "url": "https://example.com/b",
            "description": "Different story",
            "category": "Space",
            "published_at": "2026-11-12T07:00:00+00:00",
        }
        result = dedupe_articles([first, duplicate, other])
        self.assertEqual(len(result), 2)
        titles = {item["title"] for item in result}
        self.assertIn("Space telescope observes a distant galaxy", titles)

    def test_merge_keeps_cache_and_prunes_old(self):
        cached = [
            {
                "id": "old",
                "title": "Old story",
                "url": "https://example.com/old",
                "description": "old",
                "published_at": "2020-01-01T00:00:00+00:00",
            }
        ]
        fresh = [
            {
                "id": "new",
                "title": "New story",
                "url": "https://example.com/new",
                "description": "new",
                "published_at": "2026-11-12T08:00:00+00:00",
            }
        ]
        from datetime import datetime, timezone

        merged = merge_article_cache(cached, fresh, now=datetime(2026, 11, 12, 9, tzinfo=timezone.utc))
        self.assertEqual([item["id"] for item in merged], ["new"])

    def test_parse_published_variants(self):
        self.assertEqual(parse_published("Wed, 11 Nov 2026 10:00:00 GMT"), "2026-11-11T10:00:00+00:00")
        self.assertEqual(parse_published("2026-11-12T08:30:00Z"), "2026-11-12T08:30:00+00:00")
        self.assertIsNone(parse_published("not a date"))


if __name__ == "__main__":
    unittest.main()
