"""Tests for cross-source story clustering."""

from __future__ import annotations

import unittest

from system_sl.intel.clustering import (
    STORY_THRESHOLD,
    cluster_articles,
    cluster_for_article,
    story_similarity,
    title_terms,
)
from tests.intel_fixtures import make_article

EVENT_TITLE = "OpenAI releases GPT-6 language model"
EVENT_DESCRIPTION = "OpenAI announced GPT-6, a new language model for developers."


def event_article(identifier, *, source_id, source_name, title, description=None, hours_old=2, reliability=80, url=None):
    return make_article(
        identifier,
        title,
        description or EVENT_DESCRIPTION,
        "AI & Machine Learning",
        hours_old=hours_old,
        reliability=reliability,
        source_id=source_id,
        source_name=source_name,
        url=url,
    )


def gpt_event() -> list[dict]:
    return [
        event_article(
            "a1",
            source_id="alpha",
            source_name="Alpha News",
            title=EVENT_TITLE,
            reliability=90,
        ),
        event_article(
            "b1",
            source_id="beta",
            source_name="Beta Wire",
            title="OpenAI launches GPT-6 with new developer capabilities",
            description="The GPT-6 language model from OpenAI adds developer capabilities.",
            reliability=70,
        ),
        event_article(
            "c1",
            source_id="gamma",
            source_name="Gamma Daily",
            title="GPT-6 from OpenAI brings new capabilities",
            description="OpenAI's GPT-6 model introduces new capabilities for developers.",
            reliability=80,
        ),
    ]


class SimilarityTests(unittest.TestCase):
    def test_exact_url_is_a_duplicate(self):
        left = event_article(
            "one", source_id="alpha", source_name="Alpha", title=EVENT_TITLE,
            url="https://example.com/story?utm_source=feed",
        )
        right = event_article(
            "two", source_id="beta", source_name="Beta", title="Something else entirely",
            url="https://example.com/story",
        )
        score, kind = story_similarity(left, right)
        self.assertEqual(score, 1.0)
        self.assertEqual(kind, "duplicate")

    def test_syndicated_copy_is_recognised(self):
        left = event_article("one", source_id="alpha", source_name="Alpha", title=EVENT_TITLE)
        right = event_article("two", source_id="beta", source_name="Beta", title=EVENT_TITLE)
        score, kind = story_similarity(left, right)
        self.assertEqual(kind, "syndication")
        self.assertGreaterEqual(score, 0.93)

    def test_different_headlines_for_one_event_are_related(self):
        members = gpt_event()
        score, kind = story_similarity(members[0], members[1])
        self.assertGreaterEqual(score, STORY_THRESHOLD)
        self.assertEqual(kind, "story")

    def test_similar_words_for_unrelated_events_are_not_merged(self):
        apple_device = event_article(
            "apple-1",
            source_id="alpha",
            source_name="Alpha",
            title="Apple releases iOS 19 update",
            description="Apple's iOS 19 update adds features for iPhone users.",
        )
        apple_earnings = event_article(
            "apple-2",
            source_id="beta",
            source_name="Beta",
            title="Apple quarterly earnings beat expectations",
            description="Apple reported quarterly earnings above analyst estimates.",
        )
        score, kind = story_similarity(apple_device, apple_earnings)
        self.assertEqual(kind, "")
        self.assertLess(score, STORY_THRESHOLD)

        python_release = event_article(
            "py-1",
            source_id="alpha",
            source_name="Alpha",
            title="Python 3.14 released with performance improvements",
            description="The Python 3.14 release speeds up the interpreter.",
        )
        python_snake = event_article(
            "py-2",
            source_id="beta",
            source_name="Beta",
            title="Python snake found in Florida backyard",
            description="A large python was found behind a house in Florida.",
        )
        self.assertEqual(story_similarity(python_release, python_snake)[1], "")

    def test_stories_far_apart_in_time_are_not_merged(self):
        members = gpt_event()
        older = dict(members[1], published_at=members[1]["published_at"])
        # Move the second article outside the 72-hour story window.
        from datetime import datetime, timedelta

        published = datetime.fromisoformat(older["published_at"])
        older["published_at"] = (published - timedelta(hours=100)).isoformat()
        score, kind = story_similarity(members[0], older)
        self.assertEqual(kind, "")
        self.assertEqual(score, 0.0)

    def test_title_terms_include_significant_bigrams(self):
        terms = title_terms(EVENT_TITLE)
        self.assertIn("openai", terms)
        self.assertIn("openai gpt-6", terms)
        self.assertNotIn("releases", terms)


class ClusterTests(unittest.TestCase):
    def test_event_coverage_becomes_one_story_cluster(self):
        clusters = cluster_articles(gpt_event())
        self.assertEqual(len(clusters), 1)
        cluster = clusters[0]
        self.assertEqual(cluster["size"], 3)
        self.assertGreaterEqual(cluster["title_similarity"], STORY_THRESHOLD)
        self.assertEqual(len(cluster["coverage"]), 2)
        covered = {entry["id"] for entry in cluster["coverage"]}
        self.assertEqual(covered, {"a1", "b1", "c1"} - {cluster["primary"]})

    def test_all_urls_and_sources_are_preserved(self):
        clusters = cluster_articles(gpt_event())
        cluster = clusters[0]
        self.assertEqual(
            {article["url"] for article in cluster["articles"]},
            {article["url"] for article in gpt_event()},
        )
        self.assertEqual(
            {source["id"] for source in cluster["sources"]},
            {"alpha", "beta", "gamma"},
        )
        for entry in cluster["coverage"]:
            self.assertTrue(entry["url"])
            self.assertTrue(entry["source_name"])

    def test_most_authoritative_article_is_primary(self):
        members = gpt_event()
        members[1]["reliability"] = 98
        clusters = cluster_articles(members)
        self.assertEqual(clusters[0]["primary"], "b1")

    def test_full_text_breaks_a_reliability_tie(self):
        members = gpt_event()
        for member in members:
            member["reliability"] = 80
        members[2]["content_status"] = "full"
        clusters = cluster_articles(members)
        self.assertEqual(clusters[0]["primary"], "c1")

    def test_unrelated_stories_stay_separate(self):
        articles = gpt_event() + [
            event_article(
                "space-1",
                source_id="delta",
                source_name="Delta Science",
                title="Space telescope discovers distant exoplanet",
                description="Astronomers found an exoplanet orbiting a nearby star.",
            ),
            event_article(
                "sec-1",
                source_id="epsilon",
                source_name="Epsilon Security",
                title="Cybersecurity breach exposes customer records",
                description="A company disclosed a breach affecting customer records.",
            ),
        ]
        clusters = cluster_articles(articles)
        self.assertEqual(len(clusters), 3)
        self.assertEqual(sorted(cluster["size"] for cluster in clusters), [1, 1, 3])

    def test_exact_duplicates_are_flagged(self):
        first = event_article(
            "dup-1", source_id="alpha", source_name="Alpha", title=EVENT_TITLE,
            url="https://example.com/story?utm_source=a",
        )
        second = event_article(
            "dup-2", source_id="beta", source_name="Beta", title=EVENT_TITLE,
            url="https://example.com/story",
        )
        cluster = cluster_articles([first, second])[0]
        self.assertEqual(cluster["kind"], "duplicate")
        self.assertEqual(cluster["size"], 2)

    def test_cluster_lookup_by_primary(self):
        clusters = cluster_articles(gpt_event())
        primary = clusters[0]["primary"]
        self.assertIs(cluster_for_article(clusters, primary), clusters[0])
        self.assertIsNone(cluster_for_article(clusters, "not-present"))

    def test_single_article_cluster_is_stable(self):
        article = event_article(
            "solo",
            source_id="alpha",
            source_name="Alpha",
            title="OpenAI releases GPT-6 language model",
        )
        clusters = cluster_articles([article])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["size"], 1)
        self.assertEqual(clusters[0]["primary"], "solo")
        self.assertEqual(clusters[0]["coverage"], [])
        self.assertEqual(clusters[0]["kind"], "single")

    def test_cluster_ids_are_deterministic(self):
        first = cluster_articles(gpt_event())[0]["id"]
        second = cluster_articles(list(reversed(gpt_event())))[0]["id"]
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("story-"))


if __name__ == "__main__":
    unittest.main()
