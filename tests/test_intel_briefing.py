"""End-to-end tests for Daily Intel briefing generation."""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from system_sl.intel.briefing import BriefingService
from system_sl.intel.storage import IntelStore
from tests.intel_fixtures import (
    NOW,
    SOURCE,
    FakeFetcher,
    FakeSummarizer,
    default_articles,
    make_article,
    write_persona,
    write_tasks,
)


class BriefingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = IntelStore(self.root)
        self.persona_path = self.root / "persona.json"
        self.tasks_path = self.root / "tasks.json"
        self.completed_path = self.root / "completed_tasks.json"
        self.tasks_path.write_text("[]", encoding="utf-8")
        self.completed_path.write_text("[]", encoding="utf-8")

    def service(self, fetcher=None, summarizer=None, **kwargs):
        return BriefingService(
            self.store,
            fetcher=fetcher or FakeFetcher(default_articles()),
            summarizer=summarizer or FakeSummarizer(),
            now=lambda: NOW,
            persona_path=str(self.persona_path),
            tasks_path=str(self.tasks_path),
            completed_path=str(self.completed_path),
            sources=(SOURCE,),
            **kwargs,
        )

    def all_ids(self, briefing):
        return [
            entry["article"]["id"]
            for entries in briefing["sections"].values()
            for entry in entries
        ]

    def test_both_persona_and_tasks(self):
        write_persona(self.persona_path)
        write_tasks(self.tasks_path)
        briefing = self.service().refresh(force=True)
        self.assertEqual(briefing["state"], "ok")
        self.assertGreater(len(briefing["sections"]["persona"]), 0)
        self.assertGreater(len(briefing["sections"]["tasks"]), 0)
        self.assertGreater(len(briefing["sections"]["general"]), 0)
        self.assertLessEqual(len(self.all_ids(briefing)), 9)
        self.assertTrue(briefing["meta"]["persona"])
        self.assertTrue(briefing["meta"]["tasks"])

    def test_persona_only(self):
        write_persona(self.persona_path)
        briefing = self.service().refresh(force=True)
        self.assertGreater(len(briefing["sections"]["persona"]), 0)
        self.assertEqual(len(briefing["sections"]["tasks"]), 0)
        self.assertGreater(len(briefing["sections"]["general"]), 0)
        self.assertFalse(briefing["meta"]["tasks"])

    def test_tasks_only(self):
        write_tasks(self.tasks_path)
        briefing = self.service().refresh(force=True)
        self.assertEqual(len(briefing["sections"]["persona"]), 0)
        self.assertGreater(len(briefing["sections"]["tasks"]), 0)
        self.assertGreater(len(briefing["sections"]["general"]), 0)
        self.assertFalse(briefing["meta"]["persona"])

    def test_neither_persona_nor_tasks(self):
        briefing = self.service().refresh(force=True)
        self.assertEqual(briefing["state"], "ok")
        self.assertEqual(len(briefing["sections"]["persona"]), 0)
        self.assertEqual(len(briefing["sections"]["tasks"]), 0)
        self.assertGreater(len(briefing["sections"]["general"]), 0)
        self.assertFalse(briefing["meta"]["persona"])
        self.assertFalse(briefing["meta"]["tasks"])

    def test_incomplete_persona_still_works(self):
        write_persona(self.persona_path, [("Python AI", 3), ("", 1)])
        briefing = self.service().refresh(force=True)
        self.assertTrue(briefing["meta"]["persona"])
        self.assertFalse(briefing["meta"]["persona_complete"])
        self.assertEqual(briefing["state"], "ok")

    def test_all_tasks_completed(self):
        write_persona(self.persona_path)
        write_tasks(self.tasks_path, [])
        self.completed_path.write_text(
            '[{"title": "Build an AI chatbot", "completed_at": "2026-11-11"}]',
            encoding="utf-8",
        )
        briefing = self.service().refresh(force=True)
        self.assertEqual(len(briefing["sections"]["tasks"]), 0)
        self.assertFalse(briefing["meta"]["tasks"])

    def test_expired_tasks_are_ignored(self):
        write_persona(self.persona_path)
        expired = (NOW - timedelta(days=90)).date().isoformat()
        self.tasks_path.write_text(
            f'[{{"title": "Build an AI chatbot", "deadline": "{expired}", "category": "general"}}]',
            encoding="utf-8",
        )
        briefing = self.service().refresh(force=True)
        self.assertEqual(len(briefing["sections"]["tasks"]), 0)
        self.assertFalse(briefing["meta"]["tasks"])

    def test_overdue_task_still_counts_as_active(self):
        write_persona(self.persona_path)
        overdue = (NOW - timedelta(days=3)).date().isoformat()
        self.tasks_path.write_text(
            f'[{{"title": "Build an AI chatbot", "deadline": "{overdue}", "category": "general"}}]',
            encoding="utf-8",
        )
        briefing = self.service().refresh(force=True)
        self.assertTrue(briefing["meta"]["tasks"])
        self.assertEqual(briefing["meta"]["task_count"], 1)

    def test_no_relevant_news_falls_back_to_general(self):
        write_persona(self.persona_path)
        gardening = [
            make_article("g1", "Autumn gardening guide", "Plant bulbs before winter.", "General Technology"),
            make_article("g2", "Community garden opens", "A new garden opens downtown.", "General Technology"),
        ]
        briefing = self.service(FakeFetcher(gardening)).refresh(force=True)
        self.assertEqual(len(briefing["sections"]["persona"]), 0)
        self.assertGreater(len(briefing["sections"]["general"]), 0)

    def test_sources_fail_uses_cache(self):
        cached = default_articles()[:3]
        self.store.save_articles(cached)
        fetcher = FakeFetcher(articles=[], errors=["Test Source: connection refused"])
        briefing = self.service(fetcher).refresh(force=True)
        self.assertEqual(briefing["state"], "offline")
        self.assertGreater(len(briefing["sections"]["general"]), 0)
        self.assertTrue(any("unavailable" in warning.lower() or "Test Source" in warning for warning in briefing["warnings"]))

    def test_internet_unavailable_with_cached_briefing(self):
        cached = {
            "date": "2026-11-12",
            "generated_at": "2026-11-12T07:00:00+00:00",
            "state": "ok",
            "sections": {"persona": [], "tasks": [], "general": [{"article": {"id": "x", "title": "Cached"}}]},
            "warnings": [],
            "meta": {},
        }
        self.store.save_briefing(cached)
        fetcher = FakeFetcher(exc=ConnectionError("offline"))
        briefing = self.service(fetcher).refresh(force=False)
        self.assertEqual(fetcher.calls, 0)
        self.assertEqual(briefing["generated_at"], cached["generated_at"])

        forced = self.service(fetcher).refresh(force=True)
        self.assertEqual(forced["generated_at"], cached["generated_at"])
        self.assertTrue(forced["warnings"])

    def test_no_cache_and_no_internet_returns_empty(self):
        fetcher = FakeFetcher(exc=ConnectionError("offline"))
        briefing = self.service(fetcher).refresh(force=True)
        self.assertEqual(briefing["state"], "empty")
        self.assertEqual(self.all_ids(briefing), [])

    def test_llm_failure_uses_fallback(self):
        write_persona(self.persona_path)
        briefing = self.service(summarizer=FakeSummarizer(fail=True)).refresh(force=True)
        entries = [entry for entries in briefing["sections"].values() for entry in entries]
        self.assertTrue(entries)
        for entry in entries:
            self.assertEqual(entry["summary"]["generated_by"], "fallback")
            self.assertTrue(entry["summary"]["what_happened"])

    def test_duplicate_articles_are_collected_once(self):
        first = make_article("p1", "AI model released", "A new model.", "AI & Machine Learning")
        second = make_article("p2", "AI model released", "A new model with more detail.", "AI & Machine Learning")
        first.pop("id")
        second.pop("id")
        second["url"] = "https://example.com/p1?utm_source=feed"
        briefing = self.service(FakeFetcher([first, second])).refresh(force=True)
        self.assertEqual(len(self.store.load_articles()), 1)
        self.assertLessEqual(len(self.all_ids(briefing)), 1)

    def test_cached_briefing_avoids_network(self):
        fetcher = FakeFetcher(default_articles())
        service = self.service(fetcher)
        first = service.refresh(force=True)
        second = service.refresh(force=False)
        self.assertEqual(fetcher.calls, 1)
        self.assertEqual(first["generated_at"], second["generated_at"])

    def test_generation_twice_keeps_one_briefing(self):
        service = self.service()
        service.refresh(force=True)
        service.refresh(force=True)
        self.assertEqual(list(self.store.load_briefings()), ["2026-11-12"])

    def test_disabled_daily_intel(self):
        service = self.service()
        service.set_enabled(False)
        fetcher = FakeFetcher(default_articles())
        service.fetcher = fetcher
        briefing = service.refresh(force=True)
        self.assertEqual(briefing["state"], "disabled")
        self.assertEqual(fetcher.calls, 0)

    def test_feedback_is_saved_and_influences_next_briefing(self):
        service = self.service()
        briefing = service.refresh(force=True)
        target = self.all_ids(briefing)[0]
        topic = next(
            entry["article"]["category"]
            for entries in briefing["sections"].values()
            for entry in entries
            if entry["article"]["id"] == target
        )
        service.record_feedback(target, "dislike", topic=topic)
        self.assertTrue(service.feedback_state(target)["disliked"])

        refreshed = service.refresh(force=True)
        self.assertNotIn(target, self.all_ids(refreshed))

        service.record_feedback(target, "save")
        self.assertTrue(service.feedback_state(target)["saved"])
        service.reset_preferences()
        self.assertEqual(service.feedback_state(target), {})
        self.assertEqual(service.get_config()["preferred_topics"], [])

    def test_topic_preferences_and_source_toggles_persist(self):
        service = self.service()
        service.set_preferred_topics(["AI & Machine Learning"])
        self.assertEqual(service.get_config()["preferred_topics"], ["AI & Machine Learning"])
        service.set_source_enabled(SOURCE.id, False)
        self.assertFalse(service.source_catalog()[0]["enabled"])
        service.set_source_enabled(SOURCE.id, True)
        self.assertTrue(service.source_catalog()[0]["enabled"])

    def test_collect_if_due_refreshes_cache_without_briefing(self):
        fetcher = FakeFetcher(default_articles())
        service = self.service(fetcher)
        self.assertTrue(service.collect_if_due())
        self.assertEqual(fetcher.calls, 1)
        self.assertEqual(len(self.store.load_articles()), len(default_articles()))
        self.assertIsNone(self.store.get_briefing("2026-11-12"))

        # The interval has not elapsed yet, so a second pass is a no-op.
        self.assertFalse(service.collect_if_due())
        self.assertEqual(fetcher.calls, 1)

        service.set_enabled(False)
        self.assertFalse(service.collect_if_due())

    def test_collect_if_due_skips_network_when_disallowed(self):
        fetcher = FakeFetcher(default_articles())
        service = self.service(fetcher)
        self.assertFalse(service.collect_if_due(allow_network=False))
        self.assertEqual(fetcher.calls, 0)


if __name__ == "__main__":
    unittest.main()
