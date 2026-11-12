"""Tests for the explainable Daily Intel scoring engine."""

from __future__ import annotations

import unittest

from system_sl.intel.profile import PersonaSignal, TaskSignal
from system_sl.intel.scoring import (
    collect_history,
    freshness_score,
    normalized_weights,
    score_article,
    select_briefing,
)
from tests.intel_fixtures import NOW, default_articles, make_article


def persona() -> PersonaSignal:
    return PersonaSignal(
        available=True,
        complete=True,
        text="Python AI machine learning",
        keywords={"python": 3.0, "ai": 3.0, "machine": 3.0, "learning": 3.0},
        topics={"AI & Machine Learning"},
    )


def tasks() -> TaskSignal:
    return TaskSignal(
        available=True,
        tasks=[
            {
                "title": "Build an AI chatbot",
                "category": "general",
                "terms": {"build", "ai", "chatbot"},
            }
        ],
        keywords={"build": 1.0, "ai": 1.0, "chatbot": 1.0},
        labels=["Build an AI chatbot"],
    )


class ScoringTests(unittest.TestCase):
    def test_weight_renormalization(self):
        both = normalized_weights(True, True)
        self.assertAlmostEqual(sum(both.values()), 1.0)
        self.assertAlmostEqual(both["P"], 0.40)

        neither = normalized_weights(False, False)
        self.assertNotIn("P", neither)
        self.assertNotIn("T", neither)
        self.assertAlmostEqual(sum(neither.values()), 1.0)

        persona_only = normalized_weights(True, False)
        self.assertIn("P", persona_only)
        self.assertNotIn("T", persona_only)
        self.assertAlmostEqual(sum(persona_only.values()), 1.0)

    def test_missing_info_does_not_penalize(self):
        article = make_article("a", "Space telescope discovers exoplanet", "Astronomers found a new world.", "Space")
        no_profile = score_article(article, PersonaSignal(), TaskSignal(), now=NOW)
        empty = PersonaSignal(available=False)
        self.assertGreater(no_profile.R, 20)
        self.assertEqual(empty.available, False)

    def test_persona_match_raises_relevance(self):
        article = make_article("a", "Python machine learning update", "A new AI learning toolkit.", "AI & Machine Learning")
        matched = score_article(article, persona(), TaskSignal(), now=NOW)
        unrelated = score_article(
            make_article("b", "Gardening tips for autumn", "How to plant bulbs.", "General Technology"),
            persona(),
            TaskSignal(),
            now=NOW,
        )
        self.assertGreater(matched.P, unrelated.P)
        self.assertGreater(matched.R, unrelated.R)
        self.assertTrue(any("profile" in reason for reason in matched.reasons))

    def test_task_match(self):
        article = make_article("a", "New chatbot API for developers", "Build a chatbot with this API.", "Software & Developer Tools")
        matched = score_article(article, PersonaSignal(), tasks(), now=NOW)
        self.assertGreaterEqual(matched.T, 30)
        self.assertEqual(matched.matched_task, "Build an AI chatbot")
        self.assertTrue(any("quest" in reason for reason in matched.reasons))

    def test_freshness_prefers_recent(self):
        recent = make_article("r", "Recent story", "Recent.", "Science & Research", hours_old=1)
        old = make_article("o", "Old story", "Old.", "Science & Research", hours_old=24 * 40)
        self.assertGreater(freshness_score(recent, NOW, 90), freshness_score(old, NOW, 90))

    def test_feedback_hides_disliked_and_known(self):
        article = make_article("a", "AI update", "An AI update.", "AI & Machine Learning")
        feedback = {"articles": {"a": {"disliked": True}}, "topics": {}}
        scored = score_article(article, persona(), TaskSignal(), feedback=feedback, now=NOW)
        self.assertTrue(scored.hard_negative)

        feedback = {"articles": {"a": {"known": True}}, "topics": {}}
        scored = score_article(article, persona(), TaskSignal(), feedback=feedback, now=NOW)
        self.assertTrue(scored.hard_negative)

    def test_topic_not_interested_hides(self):
        article = make_article("a", "AI update", "An AI update.", "AI & Machine Learning")
        feedback = {"articles": {}, "topics": {"AI & Machine Learning": -3}}
        scored = score_article(article, persona(), TaskSignal(), feedback=feedback, now=NOW)
        self.assertTrue(scored.hard_negative)

    def test_select_briefing_distribution_and_no_duplicates(self):
        articles = default_articles()
        scored = [score_article(article, persona(), tasks(), now=NOW) for article in articles]
        sections = select_briefing(scored, has_persona=True, has_tasks=True, max_articles=9)
        total = sum(len(entries) for entries in sections.values())
        self.assertLessEqual(total, 9)
        ids = [entry["article"]["id"] for entries in sections.values() for entry in entries]
        self.assertEqual(len(ids), len(set(ids)))

    def test_select_general_only(self):
        articles = default_articles()[-2:]
        scored = [score_article(article, PersonaSignal(), TaskSignal(), now=NOW) for article in articles]
        sections = select_briefing(scored, has_persona=False, has_tasks=False, max_articles=9)
        self.assertEqual(len(sections["persona"]), 0)
        self.assertEqual(len(sections["tasks"]), 0)
        self.assertGreater(len(sections["general"]), 0)

    def test_collect_history(self):
        briefings = {
            "2026-11-11": {
                "sections": {
                    "general": [
                        {"article": {"id": "x", "title": "Seen story"}},
                    ]
                }
            }
        }
        ids, titles = collect_history(briefings, {"articles": {"y": {"saved": True}}}, today=NOW)
        self.assertIn("x", ids)
        self.assertIn("y", ids)
        self.assertIn("Seen story", titles)


if __name__ == "__main__":
    unittest.main()
