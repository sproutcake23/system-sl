"""Tests for Daily Intel persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from system_sl.intel.storage import IntelStore


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = IntelStore(self.temp.name)

    def test_config_defaults(self):
        config = self.store.load_config()
        self.assertTrue(config["enabled"])
        self.assertEqual(config["preferred_topics"], [])
        self.assertEqual(config["max_articles"], 9)

    def test_config_round_trip(self):
        config = self.store.load_config()
        config["enabled"] = False
        config["preferred_topics"] = ["AI & Machine Learning"]
        self.store.save_config(config)
        loaded = self.store.load_config()
        self.assertFalse(loaded["enabled"])
        self.assertEqual(loaded["preferred_topics"], ["AI & Machine Learning"])

    def test_corrupt_json_falls_back(self):
        Path(self.temp.name, "intel_config.json").write_text("{broken", encoding="utf-8")
        self.assertTrue(self.store.load_config()["enabled"])
        Path(self.temp.name, "intel_articles.json").write_text("[]", encoding="utf-8")
        self.assertEqual(self.store.load_articles(), [])

    def test_feedback_toggles_and_reset(self):
        self.store.record_feedback("a1", "like", topic="AI & Machine Learning")
        state = self.store.article_feedback("a1")
        self.assertTrue(state["liked"])
        feedback = self.store.load_feedback()
        self.assertEqual(feedback["topics"]["AI & Machine Learning"], 1)

        self.store.record_feedback("a1", "dislike", topic="AI & Machine Learning")
        state = self.store.article_feedback("a1")
        self.assertFalse(state.get("liked"))
        self.assertTrue(state.get("disliked"))

        self.store.record_feedback("a1", "save")
        self.assertTrue(self.store.article_feedback("a1")["saved"])

        self.store.reset_feedback()
        self.assertEqual(self.store.load_feedback()["articles"], {})

    def test_briefing_is_one_per_date(self):
        briefing = {"date": "2026-11-12", "generated_at": "x", "sections": {}}
        self.store.save_briefing(briefing)
        self.store.save_briefing({**briefing, "generated_at": "y"})
        briefings = self.store.load_briefings()
        self.assertEqual(list(briefings), ["2026-11-12"])
        self.assertEqual(briefings["2026-11-12"]["generated_at"], "y")

    def test_summary_cache(self):
        self.store.save_summary("a1:ctx", {"what_happened": "x"})
        self.assertEqual(self.store.get_summary("a1:ctx")["what_happened"], "x")
        self.store.clear_summaries()
        self.assertIsNone(self.store.get_summary("a1:ctx"))

    def test_atomic_write_leaves_valid_json(self):
        self.store.save_config({"enabled": True})
        raw = json.loads(Path(self.temp.name, "intel_config.json").read_text(encoding="utf-8"))
        self.assertTrue(raw["enabled"])


if __name__ == "__main__":
    unittest.main()
