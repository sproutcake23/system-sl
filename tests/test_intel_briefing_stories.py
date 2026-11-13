"""Integration tests for clustering, extraction, diversity and scheduling."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from system_sl.intel.briefing import BriefingService
from system_sl.intel.extraction import ContentExtractor
from system_sl.intel.sources import FetchResult, NewsSource, SourceFetchOutcome
from system_sl.intel.storage import IntelStore
from tests.intel_fixtures import (
    NOW as FIXTURE_NOW,
    SOURCE,
    AllowAllRobots,
    FakeFetcher,
    FakeResponse,
    FakeSession,
    FakeSummarizer,
    default_articles,
    make_article,
)

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)

ALPHA = NewsSource(
    "alpha", "Alpha News", "https://alpha.example.com/feed",
    "https://alpha.example.com", "AI & Machine Learning", 90,
)
BETA = NewsSource(
    "beta", "Beta Wire", "https://beta.example.com/feed",
    "https://beta.example.com", "AI & Machine Learning", 70,
)
GAMMA = NewsSource(
    "gamma", "Gamma Daily", "https://gamma.example.com/feed",
    "https://gamma.example.com", "AI & Machine Learning", 80,
)
DELTA = NewsSource(
    "delta", "Delta Science", "https://delta.example.com/feed",
    "https://delta.example.com", "Science", 80,
)

FULL_TEXT_HTML = """<html><body><article>
<p>The new Python toolkit gives developers a faster way to build production services, with typed configuration and a small dependency footprint.</p>
<p>Its parser handles streaming input, and the project ships with documentation and a migration guide for existing applications.</p>
<p>Maintainers say the release focuses on reliability, clearer error messages, and predictable performance under heavy load.</p>
<p>Early adopters report fewer configuration mistakes and a simpler deployment process across containers and virtual environments.</p>
</article></body></html>
"""


def gpt_event() -> list[dict]:
    return [
        make_article(
            "a1", "OpenAI releases GPT-6 language model",
            "OpenAI announced GPT-6, a new language model for developers.",
            "AI & Machine Learning", source_id="alpha", source_name="Alpha News",
            reliability=90,
        ),
        make_article(
            "b1", "OpenAI launches GPT-6 with new developer capabilities",
            "The GPT-6 language model from OpenAI adds developer capabilities.",
            "AI & Machine Learning", source_id="beta", source_name="Beta Wire",
            reliability=70,
        ),
        make_article(
            "c1", "GPT-6 from OpenAI brings new capabilities",
            "OpenAI's GPT-6 model introduces new capabilities for developers.",
            "AI & Machine Learning", source_id="gamma", source_name="Gamma Daily",
            reliability=80,
        ),
    ]


class BriefingTestCase(unittest.TestCase):
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

    def service(self, fetcher=None, *, sources=(SOURCE,), **kwargs):
        return BriefingService(
            self.store,
            fetcher=fetcher or FakeFetcher(default_articles()),
            summarizer=kwargs.pop("summarizer", FakeSummarizer()),
            now=lambda: NOW,
            persona_path=str(self.persona_path),
            tasks_path=str(self.tasks_path),
            completed_path=str(self.completed_path),
            sources=sources,
            **kwargs,
        )

    def entries(self, briefing):
        return [entry for section in briefing["sections"].values() for entry in section]

    def set_config(self, **values):
        config = self.store.load_config()
        config.update(values)
        self.store.save_config(config)


class StoryClusteringBriefingTests(BriefingTestCase):
    def test_same_story_is_shown_once_with_coverage_links(self):
        articles = gpt_event() + [
            make_article(
                "space-1", "Space telescope discovers distant exoplanet",
                "Astronomers found an exoplanet orbiting a nearby star.",
                "Science", source_id="delta", source_name="Delta Science",
            )
        ]
        service = self.service(
            FakeFetcher(articles), sources=(ALPHA, BETA, GAMMA, DELTA)
        )
        briefing = service.refresh(force=True)

        ids = [entry["article"]["id"] for entry in self.entries(briefing)]
        self.assertIn("a1", ids)
        self.assertIn("space-1", ids)
        self.assertNotIn("b1", ids)
        self.assertNotIn("c1", ids)

        primary = next(entry for entry in self.entries(briefing) if entry["article"]["id"] == "a1")
        story = primary["story"]
        self.assertEqual(story["size"], 3)
        self.assertEqual(
            {entry["id"] for entry in story["coverage"]}, {"b1", "c1"}
        )
        self.assertEqual(
            {entry["source_name"] for entry in story["coverage"]},
            {"Beta Wire", "Gamma Daily"},
        )
        for entry in story["coverage"]:
            self.assertTrue(entry["url"])

        self.assertEqual(briefing["meta"]["stories_merged"], 2)
        self.assertEqual(briefing["meta"]["stories_in_briefing"], 1)

    def test_story_selection_stays_within_max_articles(self):
        self.set_config(max_articles=2)
        articles = gpt_event()
        briefing = self.service(
            FakeFetcher(articles), sources=(ALPHA, BETA, GAMMA)
        ).refresh(force=True)
        self.assertLessEqual(len(self.entries(briefing)), 2)
        self.assertEqual(len(self.entries(briefing)), 1)

    def test_story_ids_are_preserved_for_scoring(self):
        service = self.service(FakeFetcher(gpt_event()), sources=(ALPHA, BETA, GAMMA))
        briefing = service.refresh(force=True)
        primary = next(
            entry for entry in self.entries(briefing) if entry["article"]["id"] == "a1"
        )
        self.assertTrue(primary["article"]["story_cluster"].startswith("story-"))
        self.assertEqual(set(primary["article"]["story_ids"]), {"b1", "c1"})

    def test_disabling_clustering_keeps_separate_articles(self):
        self.set_config(story_clustering=False)
        service = self.service(FakeFetcher(gpt_event()), sources=(ALPHA, BETA, GAMMA))
        briefing = service.refresh(force=True)
        ids = {entry["article"]["id"] for entry in self.entries(briefing)}
        self.assertEqual(ids, {"a1", "b1", "c1"})
        self.assertEqual(briefing["meta"]["stories_merged"], 0)


class ExtractionBriefingTests(BriefingTestCase):
    def test_selected_articles_use_full_text(self):
        articles = default_articles()[:3]
        extractor = ContentExtractor(
            self.store,
            session=FakeSession(
                {
                    article["url"]: FakeResponse(content=FULL_TEXT_HTML)
                    for article in articles
                }
            ),
            robots=AllowAllRobots(),
            clock=lambda: NOW,
        )
        briefing = self.service(
            FakeFetcher(articles), content_extractor=extractor
        ).refresh(force=True)
        entries = self.entries(briefing)
        self.assertEqual(len(entries), 3)
        self.assertEqual(
            {entry["article"]["content_status"] for entry in entries}, {"full"}
        )
        self.assertEqual(briefing["meta"]["extraction"]["full_text"], 3)
        self.assertEqual(len(extractor.store.load_content_cache()), 3)
        self.assertEqual(briefing["meta"]["extraction"]["downloaded"], 3)

        # A second refresh reuses the cache instead of downloading again.
        for article in articles:
            article["fetched_at"] = FIXTURE_NOW.isoformat()
        self.service(FakeFetcher(articles), content_extractor=extractor).refresh(force=True)
        self.assertEqual(briefing["meta"]["extraction"]["full_text"], 3)

    def test_extraction_can_be_disabled(self):
        self.set_config(extract_full_articles=False)
        extractor = ContentExtractor(
            self.store,
            session=FakeSession({}),
            robots=AllowAllRobots(),
            clock=lambda: NOW,
        )
        briefing = self.service(
            FakeFetcher(default_articles()[:3]), content_extractor=extractor
        ).refresh(force=True)
        statuses = {entry["article"]["content_status"] for entry in self.entries(briefing)}
        self.assertEqual(statuses, {"excerpt"})
        self.assertFalse(briefing["meta"]["extraction"]["enabled"])
        self.assertEqual(briefing["meta"]["extraction"]["downloaded"], 0)

    def test_download_budget_is_respected(self):
        self.set_config(max_extractions_per_run=2)
        articles = default_articles()[:5]
        extractor = ContentExtractor(
            self.store,
            session=FakeSession(
                {
                    article["url"]: FakeResponse(content=FULL_TEXT_HTML)
                    for article in articles
                }
            ),
            robots=AllowAllRobots(),
            clock=lambda: NOW,
        )
        briefing = self.service(
            FakeFetcher(articles), content_extractor=extractor
        ).refresh(force=True)
        statuses = [entry["article"]["content_status"] for entry in self.entries(briefing)]
        self.assertEqual(statuses.count("full"), 2)
        self.assertEqual(statuses.count("excerpt"), 3)
        self.assertEqual(briefing["meta"]["extraction"]["downloaded"], 2)

    def test_article_status_distinguishes_full_failed_and_blocked(self):
        articles = [
            make_article("full1", "Python toolkit deep dive", "A short teaser.", "Software & Developer Tools"),
            make_article("fail1", "Database engine benchmark", "A short teaser.", "Software & Developer Tools"),
            make_article("block1", "Browser privacy sandbox", "A short teaser.", "Software & Developer Tools"),
        ]
        extractor = ContentExtractor(
            self.store,
            session=FakeSession(
                {
                    articles[0]["url"]: FakeResponse(content=FULL_TEXT_HTML),
                    articles[1]["url"]: FakeResponse(status_code=500),
                    articles[2]["url"]: FakeResponse(status_code=403),
                }
            ),
            robots=AllowAllRobots(),
            clock=lambda: NOW,
        )
        briefing = self.service(
            FakeFetcher(articles), content_extractor=extractor
        ).refresh(force=True)
        statuses = {
            entry["article"]["id"]: entry["article"]["content_status"]
            for entry in self.entries(briefing)
        }
        self.assertEqual(statuses["full1"], "full")
        self.assertEqual(statuses["fail1"], "failed")
        self.assertEqual(statuses["block1"], "blocked")

    def test_source_status_is_exposed_on_entries(self):
        briefing = self.service(FakeFetcher(default_articles()[:2])).refresh(force=True)
        for entry in self.entries(briefing):
            self.assertEqual(entry["source_status"]["availability"], "ok")
            self.assertIsNotNone(entry["source_status"]["success_rate"])


class DiversityTests(BriefingTestCase):
    def test_sources_are_balanced_when_several_publishers_cover_news(self):
        self.set_config(max_articles=8)
        sources = (ALPHA, BETA, GAMMA, DELTA)
        titles = [
            "Quantum chip reaches milestone",
            "Database engine benchmarked",
            "Browser privacy sandbox expands",
            "Robot arm learns dexterity",
            "Satellite launch delayed",
            "Compiler optimisation improves speed",
            "Wearable sensor monitors health",
            "Undersea cable repaired",
            "Gaming console price cut",
            "Photography lens reviewed",
            "Electric truck unveiled",
            "Weather model improves forecasts",
        ]
        articles = []
        for index, title in enumerate(titles):
            source = sources[index % len(sources)]
            articles.append(
                make_article(
                    f"div-{index}", title, f"Details about {title.lower()}.",
                    "General Technology", hours_old=1, reliability=80,
                    source_id=source.id, source_name=source.name,
                )
            )
        briefing = self.service(FakeFetcher(articles), sources=sources).refresh(force=True)
        entries = self.entries(briefing)
        self.assertEqual(len(entries), 8)
        counts: dict[str, int] = {}
        for entry in entries:
            source_id = entry["article"]["source_id"]
            counts[source_id] = counts.get(source_id, 0) + 1
        self.assertEqual(set(counts), {"alpha", "beta", "gamma", "delta"})
        self.assertLessEqual(max(counts.values()), 2)


class SchedulingBriefingTests(BriefingTestCase):
    def test_force_refresh_respects_backoff_after_failure(self):
        failing = FakeFetcher(exc=ConnectionError("offline"))
        first = self.service(failing).refresh(force=True)
        self.assertEqual(first["state"], "empty")
        self.assertEqual(failing.calls, 1)

        # The failure scheduled a retry in the future, so a forced refresh
        # does not hammer the same broken source again.
        recovered = FakeFetcher(default_articles())
        second = self.service(recovered).refresh(force=True)
        self.assertEqual(recovered.calls, 0)
        self.assertEqual(second["state"], "empty")

    def test_not_modified_response_keeps_cached_articles(self):
        cached = default_articles()[:2]
        self.store.save_articles(cached)

        class NotModifiedFetcher:
            def __init__(self):
                self.calls = 0

            def __call__(self, sources):
                self.calls += 1
                return FetchResult(
                    articles=[],
                    errors=[],
                    attempted=len(list(sources)),
                    succeeded=len(list(sources)),
                    not_modified=len(list(sources)),
                    outcomes=[
                        SourceFetchOutcome(
                            SOURCE.id, SOURCE.name, "not_modified", http_status=304
                        )
                        for _ in list(sources)
                    ],
                )

        fetcher = NotModifiedFetcher()
        briefing = self.service(fetcher).refresh(force=True)
        self.assertEqual(fetcher.calls, 1)
        self.assertEqual(briefing["state"], "ok")
        self.assertGreater(len(self.entries(briefing)), 0)

    def test_health_survives_service_restart(self):
        failing = FakeFetcher(exc=ConnectionError("offline"))
        self.service(failing).refresh(force=True)
        restarted = self.service(FakeFetcher())
        health = restarted.source_health()
        self.assertEqual(health["counts"]["degraded"], 1)
        self.assertFalse(restarted.fetch_due())


class RegistryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = IntelStore(Path(self.temp.name))

    def service(self):
        return BriefingService(
            self.store,
            fetcher=FakeFetcher(),
            summarizer=FakeSummarizer(),
            now=lambda: NOW,
        )

    def test_add_disable_and_remove_persist_through_service(self):
        service = self.service()
        catalog = service.source_catalog()
        self.assertGreater(len(catalog), 10)
        self.assertTrue(all("health" in entry for entry in catalog))

        added = service.add_source(
            {
                "id": "my-lab",
                "name": "My Lab",
                "url": "https://lab.example.com/feed.xml",
                "category": "AI & Machine Learning",
                "reliability": 92,
                "type": "rss",
                "interval_minutes": 30,
            }
        )
        self.assertEqual(added["id"], "my-lab")
        self.assertEqual(added["interval_minutes"], 30)

        service.set_source_enabled("my-lab", False)
        restarted = self.service()
        entries = {entry["id"]: entry for entry in restarted.source_catalog()}
        self.assertIn("my-lab", entries)
        self.assertFalse(entries["my-lab"]["enabled"])
        self.assertEqual(entries["my-lab"]["health"]["availability"], "unknown")

        self.assertTrue(restarted.remove_source("my-lab"))
        self.assertNotIn("my-lab", {entry["id"] for entry in self.service().source_catalog()})

    def test_registry_is_used_for_actual_refreshes(self):
        service = self.service()
        source = service.registry.get("ars-technica")
        self.assertIsNotNone(source)
        briefing = service.refresh(force=True)
        self.assertIn("sources_total", briefing["meta"])
        self.assertEqual(
            briefing["meta"]["sources_total"], len(service.source_catalog())
        )


if __name__ == "__main__":
    unittest.main()
