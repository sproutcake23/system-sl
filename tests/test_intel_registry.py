"""Tests for the configurable Daily Intel source registry."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from system_sl.intel.registry import (
    SourceRegistry,
    SourceValidationError,
    slugify,
    validate_source_payload,
)
from system_sl.intel.sources import NewsSource
from system_sl.intel.storage import IntelStore
from tests.intel_fixtures import SOURCE


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = IntelStore(Path(self.temp.name))
        self.defaults = (SOURCE,)
        self.registry = SourceRegistry(self.store, defaults=self.defaults)

    def new_registry(self):
        """Simulate an application restart against the same data directory."""
        return SourceRegistry(self.store, defaults=self.defaults)

    def test_builtin_defaults_are_available(self):
        sources = self.registry.sources()
        self.assertEqual([source.id for source in sources], [SOURCE.id])
        self.assertEqual(sources[0].reliability, SOURCE.reliability)

    def test_add_custom_source_persists_across_restart(self):
        added = self.registry.add(
            {
                "id": "example-lab",
                "name": "Example Lab",
                "url": "https://lab.example.com/feed.xml",
                "category": "AI & Machine Learning",
                "reliability": 88,
                "type": "atom",
                "interval_minutes": 30,
            }
        )
        self.assertEqual(added.id, "example-lab")
        self.assertEqual(added.source_type, "atom")
        self.assertEqual(added.interval_minutes, 30)

        restarted = self.new_registry()
        ids = {source.id for source in restarted.sources()}
        self.assertIn("example-lab", ids)
        restored = restarted.get("example-lab")
        self.assertEqual(restored.name, "Example Lab")
        self.assertEqual(restored.reliability, 88)
        self.assertEqual(restored.source_type, "atom")

    def test_remove_builtin_is_not_resurrected_on_restart(self):
        self.assertTrue(self.registry.remove(SOURCE.id))
        self.assertEqual(self.registry.sources(), [])

        restarted = self.new_registry()
        self.assertEqual(restarted.sources(), [])
        # Removing an unknown id is a safe no-op.
        self.assertFalse(restarted.remove("does-not-exist"))

    def test_re_adding_removed_builtin_restores_it(self):
        self.registry.remove(SOURCE.id)
        restored = self.registry.add(
            {
                "id": SOURCE.id,
                "name": "Restored Source",
                "url": "https://example.com/new-feed",
                "homepage": "https://example.com",
                "category": SOURCE.category,
                "reliability": 75,
                "type": "rss",
            }
        )
        self.assertEqual(restored.name, "Restored Source")
        restarted = self.new_registry()
        sources = {source.id: source for source in restarted.sources()}
        self.assertIn(SOURCE.id, sources)
        self.assertEqual(sources[SOURCE.id].name, "Restored Source")
        self.assertEqual(sources[SOURCE.id].url, "https://example.com/new-feed")

    def test_update_builtin_is_persisted_as_override(self):
        updated = self.registry.update(
            SOURCE.id,
            {"name": "Renamed Source", "interval_minutes": 45, "reliability": 60},
        )
        self.assertEqual(updated.name, "Renamed Source")
        self.assertEqual(updated.interval_minutes, 45)
        self.assertEqual(updated.reliability, 60)

        restarted = self.new_registry()
        restored = restarted.get(SOURCE.id)
        self.assertEqual(restored.name, "Renamed Source")
        self.assertEqual(restored.interval_minutes, 45)
        self.assertEqual(restored.url, SOURCE.url)

    def test_update_unknown_source_fails(self):
        with self.assertRaises(SourceValidationError):
            self.registry.update("missing", {"name": "Nope"})

    def test_reset_restores_builtins_and_drops_custom_sources(self):
        self.registry.add(
            {"name": "Custom", "url": "https://custom.example.com/rss", "type": "rss"}
        )
        self.registry.remove(SOURCE.id)
        reset = self.registry.reset()
        self.assertEqual([source.id for source in reset], [SOURCE.id])
        self.assertEqual([source.id for source in self.new_registry().sources()], [SOURCE.id])

    def test_invalid_payloads_are_rejected(self):
        cases = [
            {"name": "", "url": "https://example.com/feed"},
            {"name": "No URL"},
            {"name": "Bad scheme", "url": "ftp://example.com/feed"},
            {"name": "Bad type", "url": "https://example.com/feed", "type": "carrier-pigeon"},
            {"name": "Bad interval", "url": "https://example.com/feed", "interval_minutes": "soon"},
            {"name": "Bad reliability", "url": "https://example.com/feed", "reliability": "high"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(SourceValidationError):
                    self.registry.add(payload)

    def test_duplicate_id_and_url_are_rejected(self):
        self.registry.add(
            {"id": "custom", "name": "Custom", "url": "https://custom.example.com/rss"}
        )
        with self.assertRaises(SourceValidationError):
            self.registry.add(
                {"id": "custom", "name": "Another", "url": "https://other.example.com/rss"}
            )
        with self.assertRaises(SourceValidationError):
            self.registry.add(
                {
                    "id": "custom-two",
                    "name": "Duplicate URL",
                    "url": "https://custom.example.com/rss",
                }
            )

    def test_invalid_persisted_entry_is_ignored(self):
        self.store.save_source_definitions(
            {
                "version": 2,
                "removed_defaults": [],
                "sources": [
                    {"id": "broken", "name": "Broken", "url": "not-a-url"},
                    {
                        "id": "valid-custom",
                        "name": "Valid",
                        "url": "https://valid.example.com/feed",
                        "type": "rss",
                    },
                ],
            }
        )
        ids = {source.id for source in self.registry.sources()}
        self.assertIn(SOURCE.id, ids)
        self.assertIn("valid-custom", ids)
        self.assertNotIn("broken", ids)

    def test_validation_clamps_values_and_infers_homepage(self):
        definition = validate_source_payload(
            {
                "name": "  Example   Feed ",
                "url": "https://example.com/rss",
                "reliability": 150,
                "interval_minutes": 1,
            }
        )
        self.assertEqual(definition["name"], "Example Feed")
        self.assertEqual(definition["homepage"], "https://example.com")
        self.assertEqual(definition["reliability"], 100)
        self.assertEqual(definition["interval_minutes"], 15)
        self.assertEqual(definition["type"], "rss")

    def test_slugify_produces_safe_ids(self):
        self.assertEqual(slugify("The Example Lab!"), "the-example-lab")
        self.assertEqual(slugify("---"), "")

    def test_source_defaults_are_not_written_until_changed(self):
        self.registry.sources()
        stored = self.store.load_source_definitions()
        self.assertEqual(stored["sources"], [])
        self.assertEqual(stored["removed_defaults"], [])

        self.registry.update(SOURCE.id, {"name": "Changed"})
        stored = self.store.load_source_definitions()
        self.assertEqual(len(stored["sources"]), 1)
        self.assertEqual(stored["sources"][0]["id"], SOURCE.id)


class RegistryCatalogTests(unittest.TestCase):
    """The real default catalog should always validate."""

    def test_default_sources_are_valid(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        registry = SourceRegistry(IntelStore(Path(temp.name)))
        sources = registry.sources()
        self.assertGreater(len(sources), 10)
        ids = {source.id for source in sources}
        self.assertEqual(len(ids), len(sources))
        for source in sources:
            self.assertTrue(source.url.startswith(("http://", "https://")))
            self.assertIsInstance(source, NewsSource)
            self.assertIn(source.source_type, ("rss", "atom", "api"))


if __name__ == "__main__":
    unittest.main()
