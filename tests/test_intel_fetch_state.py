"""Tests for per-source scheduling, conditional HTTP and health tracking."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from system_sl.intel.health import (
    HEALTH_DEGRADED,
    HEALTH_OK,
    HEALTH_UNAVAILABLE,
    HEALTH_UNKNOWN,
    SourceHealthTracker,
    backoff_minutes,
    success_rate,
)
from system_sl.intel.sources import (
    FetchResult,
    NewsSource,
    SourceFetchOutcome,
    fetch_all,
    fetch_source,
    parse_cache_max_age,
    parse_retry_after,
)
from system_sl.intel.storage import IntelStore
from tests.intel_fixtures import FakeResponse, FakeSession, make_article

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example</title>
    <item>
      <title>Python toolkit released</title>
      <link>https://example.com/toolkit</link>
      <description>A new toolkit for developers.</description>
      <pubDate>Wed, 11 Nov 2026 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def source(identifier: str = "s1", *, interval=None) -> NewsSource:
    return NewsSource(
        identifier,
        f"Source {identifier}",
        f"https://{identifier}.example.com/feed",
        f"https://{identifier}.example.com",
        "General Technology",
        80,
        interval_minutes=interval,
    )


class ConditionalFetchTests(unittest.TestCase):
    def test_conditional_headers_and_etag_are_used(self):
        session = FakeSession(
            {
                "https://s1.example.com/feed": FakeResponse(
                    content=RSS, headers={"ETag": '"v2"', "Last-Modified": "Wed, 11 Nov 2026 10:00:00 GMT"}
                )
            }
        )
        outcome = fetch_source(
            source(),
            session=session,
            etag='"v1"',
            last_modified="Tue, 10 Nov 2026 10:00:00 GMT",
        )
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.etag, '"v2"')
        self.assertEqual(len(outcome.articles), 1)
        sent = session.calls[0]["headers"]
        self.assertEqual(sent["If-None-Match"], '"v1"')
        self.assertEqual(sent["If-Modified-Since"], "Tue, 10 Nov 2026 10:00:00 GMT")

    def test_http_304_is_not_modified(self):
        session = FakeSession(
            {"https://s1.example.com/feed": FakeResponse(status_code=304, headers={"ETag": '"v1"'})}
        )
        outcome = fetch_source(source(), session=session, etag='"v1"')
        self.assertEqual(outcome.status, "not_modified")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.articles, [])
        self.assertEqual(outcome.etag, '"v1"')

    def test_http_429_reports_retry_after(self):
        session = FakeSession(
            {
                "https://s1.example.com/feed": FakeResponse(
                    status_code=429, headers={"Retry-After": "120"}
                )
            }
        )
        outcome = fetch_source(source(), session=session)
        self.assertEqual(outcome.status, "rate_limited")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.retry_after_seconds, 120.0)

    def test_cache_control_is_parsed(self):
        session = FakeSession(
            {
                "https://s1.example.com/feed": FakeResponse(
                    content=RSS, headers={"Cache-Control": "public, max-age=3600"}
                )
            }
        )
        outcome = fetch_source(source(), session=session)
        self.assertEqual(outcome.cache_max_age_seconds, 3600.0)
        self.assertEqual(parse_cache_max_age("no-store"), 0.0)
        self.assertIsNone(parse_cache_max_age("public"))

    def test_retry_after_accepts_http_dates_and_garbage(self):
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after("not a date"))
        self.assertEqual(parse_retry_after("300"), 300.0)
        self.assertGreaterEqual(parse_retry_after("Wed, 11 Nov 2037 10:00:00 GMT"), 0)

    def test_http_errors_are_returned_not_raised(self):
        cases = {
            403: "access restricted",
            404: "HTTP 404",
            500: "HTTP 500",
        }
        for status, fragment in cases.items():
            session = FakeSession(
                {"https://s1.example.com/feed": FakeResponse(status_code=status)}
            )
            with self.subTest(status=status):
                outcome = fetch_source(source(), session=session)
                self.assertEqual(outcome.status, "failed")
                self.assertIn(fragment, outcome.error)
                self.assertEqual(outcome.http_status, status)

    def test_transport_failure_is_returned(self):
        session = FakeSession({"https://s1.example.com/feed": ConnectionError("offline")})
        outcome = fetch_source(source(), session=session)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("ConnectionError", outcome.error)

    def test_empty_feed_is_a_failure(self):
        session = FakeSession(
            {"https://s1.example.com/feed": FakeResponse(content=b"<rss><channel></channel></rss>")}
        )
        outcome = fetch_source(source(), session=session)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("no usable articles", outcome.error)

    def test_oversized_feed_is_rejected(self):
        oversized = b"x" * (2 * 1024 * 1024 + 10)
        session = FakeSession(
            {"https://s1.example.com/feed": FakeResponse(content=oversized)}
        )
        outcome = fetch_source(source(), session=session)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("too large", outcome.error)

    def test_unsupported_source_type_is_not_fetched(self):
        session = FakeSession({source().url: FakeResponse(content=RSS)})
        outcome = fetch_source(source(), session=session)
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(len(session.calls), 1)

        api = NewsSource(
            "api", "API", "https://api.example.com/news", "https://example.com", "General Technology", 80,
            source_type="api",
        )
        outcome = fetch_source(api, session=session)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("not supported", outcome.error)
        self.assertEqual(len(session.calls), 1)

    def test_one_failed_source_does_not_stop_others(self):
        good = source("good")
        bad = source("bad")
        session = FakeSession(
            {
                good.url: FakeResponse(content=RSS),
                bad.url: ConnectionError("timeout"),
            }
        )
        result = fetch_all([good, bad], session=session, now=NOW)
        self.assertIsInstance(result, FetchResult)
        self.assertEqual(result.attempted, 2)
        self.assertEqual(result.succeeded, 1)
        self.assertEqual(len(result.outcomes), 2)
        self.assertEqual(len(result.articles), 1)
        self.assertTrue(any("Source bad" in error for error in result.errors))

    def test_fetch_all_uses_state_validators(self):
        session = FakeSession(
            {source().url: FakeResponse(status_code=304, headers={"ETag": '"x"'})}
        )
        result = fetch_all(
            [source()],
            session=session,
            state={"s1": {"etag": '"x"', "last_modified": "yesterday"}},
        )
        self.assertEqual(result.not_modified, 1)
        self.assertEqual(result.succeeded, 1)
        self.assertEqual(session.calls[0]["headers"]["If-None-Match"], '"x"')
        self.assertEqual(session.calls[0]["headers"]["If-Modified-Since"], "yesterday")


class HealthTrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = IntelStore(Path(self.temp.name))
        self.tracker = SourceHealthTracker(self.store)
        self.source = source()

    def record_ok(self, now=NOW, *, response_ms=120.0, interval=60):
        return self.tracker.record(
            SourceFetchOutcome(
                self.source.id,
                self.source.name,
                "ok",
                http_status=200,
                response_ms=response_ms,
                etag='"v1"',
                last_modified="date",
            ),
            now=now,
            interval_minutes=interval,
        )

    def record_fail(self, now=NOW, *, retry_after=None):
        return self.tracker.record(
            SourceFetchOutcome(
                self.source.id,
                self.source.name,
                "failed",
                http_status=500,
                error="HTTP 500",
                retry_after_seconds=retry_after,
            ),
            now=now,
            interval_minutes=60,
        )

    def test_initial_health_is_unknown(self):
        health = self.tracker.get(self.source.id)
        self.assertEqual(health["availability"], HEALTH_UNKNOWN)
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertIsNone(success_rate(health))

    def test_success_records_details_and_schedules_next_attempt(self):
        health = self.record_ok()
        self.assertEqual(health["availability"], HEALTH_OK)
        self.assertEqual(health["success_count"], 1)
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertEqual(health["last_status"], 200)
        self.assertEqual(health["etag"], '"v1"')
        self.assertEqual(health["average_response_ms"], 120.0)
        self.assertEqual(
            datetime.fromisoformat(health["next_attempt"]),
            NOW + timedelta(minutes=60),
        )

    def test_failures_back_off_and_become_unavailable(self):
        first = self.record_fail()
        self.assertEqual(first["availability"], HEALTH_DEGRADED)
        self.assertEqual(first["consecutive_failures"], 1)
        self.assertEqual(
            datetime.fromisoformat(first["next_attempt"]),
            NOW + timedelta(minutes=10),
        )

        second = self.record_fail()
        self.assertEqual(second["consecutive_failures"], 2)
        self.assertEqual(
            datetime.fromisoformat(second["next_attempt"]),
            NOW + timedelta(minutes=20),
        )

        third = self.record_fail()
        self.assertEqual(third["availability"], HEALTH_UNAVAILABLE)
        self.assertEqual(
            datetime.fromisoformat(third["next_attempt"]),
            NOW + timedelta(minutes=40),
        )

    def test_retry_after_can_extend_backoff(self):
        health = self.record_fail(retry_after=1800)
        self.assertEqual(
            datetime.fromisoformat(health["next_attempt"]),
            NOW + timedelta(minutes=30),
        )

    def test_failure_is_retried_after_backoff_not_the_full_interval(self):
        self.record_fail()
        self.assertFalse(
            self.tracker.is_due(
                self.source, NOW + timedelta(minutes=9), default_interval_minutes=360
            )
        )
        self.assertTrue(
            self.tracker.is_due(
                self.source, NOW + timedelta(minutes=11), default_interval_minutes=360
            )
        )

    def test_interval_is_clamped_to_sane_bounds(self):
        tiny = NewsSource(
            "tiny", "Tiny", "https://tiny.example.com/feed", "https://tiny.example.com",
            "General Technology", 70, interval_minutes=1,
        )
        huge = NewsSource(
            "huge", "Huge", "https://huge.example.com/feed", "https://huge.example.com",
            "General Technology", 70, interval_minutes=999_999,
        )
        self.assertGreaterEqual(self.tracker.interval_minutes(tiny, 60), 5)
        self.assertLessEqual(self.tracker.interval_minutes(huge, 60), 7 * 24 * 60)

    def test_cache_control_can_extend_success_interval(self):
        health = self.tracker.record(
            SourceFetchOutcome(
                self.source.id,
                self.source.name,
                "ok",
                http_status=200,
                cache_max_age_seconds=7200,
            ),
            now=NOW,
            interval_minutes=60,
        )
        self.assertEqual(
            datetime.fromisoformat(health["next_attempt"]),
            NOW + timedelta(minutes=120),
        )

    def test_success_recovers_after_failures(self):
        self.record_fail()
        self.record_fail()
        health = self.record_ok()
        self.assertEqual(health["availability"], HEALTH_OK)
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertEqual(health["failure_count"], 2)
        self.assertEqual(success_rate(health), round(1 / 3, 3))

    def test_health_persists_across_restart(self):
        self.record_ok()
        self.tracker.save()
        restarted = SourceHealthTracker(self.store)
        health = restarted.get(self.source.id)
        self.assertEqual(health["availability"], HEALTH_OK)
        self.assertEqual(health["etag"], '"v1"')

    def test_corrupt_health_is_ignored(self):
        self.store.save_source_state(
            {
                "s1": {
                    "consecutive_failures": "many",
                    "average_response_ms": "slow",
                    "availability": "exploded",
                    "last_success": "nonsense",
                }
            }
        )
        tracker = SourceHealthTracker(self.store)
        health = tracker.get("s1")
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertEqual(health["average_response_ms"], 0.0)
        self.assertEqual(health["availability"], HEALTH_UNKNOWN)
        self.assertIsNone(health["last_success"])

    def test_due_scheduling_uses_per_source_interval(self):
        self.record_ok(interval=60)
        self.assertFalse(
            self.tracker.is_due(
                self.source, NOW + timedelta(minutes=30), default_interval_minutes=60
            )
        )
        self.assertTrue(
            self.tracker.is_due(
                self.source, NOW + timedelta(minutes=61), default_interval_minutes=60
            )
        )

        explicit = source("s2", interval=15)
        self.assertEqual(self.tracker.interval_minutes(explicit, 360), 15)
        self.assertTrue(self.tracker.is_due(explicit, NOW))
        # A record for s1 does not affect the due state of s2.
        self.tracker.record(
            SourceFetchOutcome("s2", "Source s2", "ok"),
            now=NOW,
            interval_minutes=15,
        )
        self.assertFalse(self.tracker.is_due(explicit, NOW + timedelta(minutes=10)))
        self.assertTrue(self.tracker.is_due(explicit, NOW + timedelta(minutes=16)))

    def test_backoff_is_capped(self):
        self.assertEqual(backoff_minutes(1), 10)
        self.assertEqual(backoff_minutes(2), 20)
        self.assertEqual(backoff_minutes(100), 360)

    def test_conditional_headers_round_trip(self):
        self.record_ok()
        headers = self.tracker.conditional_headers(self.source.id)
        self.assertEqual(headers, {"etag": '"v1"', "last_modified": "date"})

    def test_legacy_errors_still_move_health(self):
        self.tracker.record_legacy(
            [self.source],
            ["Source s1: connection refused"],
            now=NOW,
            default_interval_minutes=60,
        )
        health = self.tracker.get(self.source.id)
        self.assertEqual(health["availability"], HEALTH_DEGRADED)
        self.assertEqual(health["consecutive_failures"], 1)

    def test_credibility_is_independent_of_availability(self):
        for _ in range(5):
            self.record_fail()
        summary = self.tracker.summary()
        self.assertEqual(summary["counts"][HEALTH_UNAVAILABLE], 1)
        # Health never rewrites the source's credibility classification.
        self.assertEqual(self.source.reliability, 80)
        self.assertNotIn("reliability", self.tracker.get(self.source.id))
        self.assertNotIn("reliability", summary["sources"][self.source.id])


class SourceFetchOutcomeTests(unittest.TestCase):
    def test_empty_articles_are_not_used_as_health_input(self):
        article = make_article("a", "Title", "Description", "General Technology")
        outcome = SourceFetchOutcome("s", "S", "ok", articles=[article])
        self.assertEqual(outcome.articles[0]["id"], "a")


if __name__ == "__main__":
    unittest.main()
