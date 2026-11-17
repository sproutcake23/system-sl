"""Per-source scheduling and technical health for Daily Intel.

Two separate ideas live here and must stay separate:

* **Credibility** is part of the source definition
  (:attr:`system_sl.intel.sources.NewsSource.reliability`). It answers "should
  this publisher be trusted?" and never changes because a server was slow.
* **Health** is the technical record kept in this module: last success, last
  failure, consecutive failures, success rate, latency, HTTP status and the
  next time a request is allowed.

Scheduling is independent of the daily briefing. Each source has its own
interval, conditional validators (ETag / Last-Modified), retry backoff and
``Retry-After`` handling. A source is only *temporarily* skipped when it keeps
failing; it is never removed from the catalog.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from system_sl.intel.sources import NewsSource, SourceFetchOutcome

HEALTH_OK = "ok"
HEALTH_DEGRADED = "degraded"
HEALTH_UNAVAILABLE = "unavailable"
HEALTH_UNKNOWN = "unknown"

# A source with no interval of its own falls back to the global setting.
DEFAULT_INTERVAL_MINUTES = 6 * 60
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 7 * 24 * 60
MAX_BACKOFF_MINUTES = 6 * 60
BASE_BACKOFF_MINUTES = 10
UNAVAILABLE_AFTER_FAILURES = 3
MAX_CACHE_CONTROL_MINUTES = 24 * 60

_FIELDS = (
    "source_id",
    "last_success",
    "last_failure",
    "last_checked",
    "consecutive_failures",
    "success_count",
    "failure_count",
    "average_response_ms",
    "last_status",
    "availability",
    "next_attempt",
    "last_error",
    "etag",
    "last_modified",
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


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


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat() if value else None


def backoff_minutes(consecutive_failures: int) -> float:
    """Exponential backoff: 10, 20, 40 … minutes, capped at six hours."""
    try:
        count = max(1, int(consecutive_failures))
    except (TypeError, ValueError):
        count = 1
    return min(MAX_BACKOFF_MINUTES, BASE_BACKOFF_MINUTES * (2 ** (count - 1)))


def empty_health(source_id: str) -> dict:
    return {
        "source_id": source_id,
        "last_success": None,
        "last_failure": None,
        "last_checked": None,
        "consecutive_failures": 0,
        "success_count": 0,
        "failure_count": 0,
        "average_response_ms": 0.0,
        "last_status": None,
        "availability": HEALTH_UNKNOWN,
        "next_attempt": None,
        "last_error": "",
        "etag": None,
        "last_modified": None,
    }


def normalize_health(raw: object, source_id: str) -> dict:
    """Return a safe health record, ignoring corrupt persisted values."""
    health = empty_health(source_id)
    if not isinstance(raw, dict):
        return health
    for key in ("last_success", "last_failure", "last_checked", "next_attempt"):
        parsed = _parse_time(raw.get(key))
        health[key] = _iso(parsed)
    for key, limit in (("consecutive_failures", 10_000), ("success_count", 10_000_000), ("failure_count", 10_000_000)):
        try:
            health[key] = max(0, min(limit, int(raw.get(key, 0))))
        except (TypeError, ValueError):
            health[key] = 0
    try:
        health["average_response_ms"] = max(0.0, float(raw.get("average_response_ms", 0.0)))
    except (TypeError, ValueError):
        health["average_response_ms"] = 0.0
    try:
        status = raw.get("last_status")
        health["last_status"] = int(status) if status is not None else None
    except (TypeError, ValueError):
        health["last_status"] = None
    availability = str(raw.get("availability") or HEALTH_UNKNOWN)
    health["availability"] = (
        availability if availability in (HEALTH_OK, HEALTH_DEGRADED, HEALTH_UNAVAILABLE) else HEALTH_UNKNOWN
    )
    health["last_error"] = str(raw.get("last_error") or "")[:300]
    for key in ("etag", "last_modified"):
        value = raw.get(key)
        health[key] = str(value)[:500] if isinstance(value, str) and value else None
    return health


def success_rate(health: dict) -> Optional[float]:
    """0–1 success ratio, or ``None`` before the first attempt."""
    try:
        successes = int(health.get("success_count", 0))
        failures = int(health.get("failure_count", 0))
    except (TypeError, ValueError):
        return None
    total = successes + failures
    if total <= 0:
        return None
    return round(successes / total, 3)


class SourceHealthTracker:
    """Reads and writes ``intel_source_state.json``."""

    def __init__(self, store) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._health: dict[str, dict] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Loading and saving
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        raw = self.store.load_source_state()
        if isinstance(raw, dict):
            self._health = {
                str(source_id): normalize_health(value, str(source_id))
                for source_id, value in raw.items()
            }
        self._loaded = True

    def reload(self) -> None:
        with self._lock:
            self._loaded = False
            self._ensure_loaded()

    def save(self) -> bool:
        with self._lock:
            self._ensure_loaded()
            return self.store.save_source_state(self._health)

    def all(self) -> dict[str, dict]:
        with self._lock:
            self._ensure_loaded()
            return {key: dict(value) for key, value in self._health.items()}

    def get(self, source_id: str) -> dict:
        with self._lock:
            self._ensure_loaded()
            return dict(self._health.get(str(source_id)) or empty_health(str(source_id)))

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def interval_minutes(
        self,
        source: NewsSource,
        default_interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    ) -> int:
        configured = source.interval_minutes
        if isinstance(configured, int) and configured > 0:
            return max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, configured))
        try:
            return max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, int(default_interval_minutes)))
        except (TypeError, ValueError):
            return DEFAULT_INTERVAL_MINUTES

    def is_due(
        self,
        source: NewsSource,
        now: datetime,
        *,
        default_interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    ) -> bool:
        health = self.get(source.id)
        now = now.astimezone(timezone.utc)
        next_attempt = _parse_time(health.get("next_attempt"))
        if next_attempt is not None:
            # ``next_attempt`` already combines the source interval, cache
            # lifetime and failure backoff, so it is the authoritative gate.
            return now >= next_attempt
        last_checked = _parse_time(health.get("last_checked"))
        if last_checked is None:
            return True
        interval = self.interval_minutes(source, default_interval_minutes)
        return now - last_checked >= timedelta(minutes=interval)

    def due_sources(
        self,
        sources: Iterable[NewsSource],
        now: datetime,
        *,
        default_interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    ) -> list[NewsSource]:
        now = now.astimezone(timezone.utc)
        return [
            source
            for source in sources
            if self.is_due(source, now, default_interval_minutes=default_interval_minutes)
        ]

    def conditional_headers(self, source_id: str) -> dict:
        health = self.get(source_id)
        headers = {}
        if health.get("etag"):
            headers["etag"] = health["etag"]
        if health.get("last_modified"):
            headers["last_modified"] = health["last_modified"]
        return headers

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(
        self,
        outcome: SourceFetchOutcome,
        *,
        now: datetime,
        interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    ) -> dict:
        now = now.astimezone(timezone.utc)
        with self._lock:
            self._ensure_loaded()
            health = self._health.get(outcome.source_id) or empty_health(outcome.source_id)
            health = normalize_health(health, outcome.source_id)
            health["last_checked"] = _iso(now)
            health["last_status"] = outcome.http_status
            if outcome.ok:
                health["last_success"] = _iso(now)
                health["consecutive_failures"] = 0
                health["success_count"] = int(health.get("success_count", 0)) + 1
                health["availability"] = HEALTH_OK
                health["last_error"] = ""
                if outcome.etag:
                    health["etag"] = outcome.etag[:500]
                if outcome.last_modified:
                    health["last_modified"] = outcome.last_modified[:500]
                if outcome.response_ms > 0:
                    previous = float(health.get("average_response_ms") or 0.0)
                    health["average_response_ms"] = round(
                        outcome.response_ms if previous <= 0 else previous * 0.7 + outcome.response_ms * 0.3,
                        1,
                    )
                wait_minutes = max(1, int(interval_minutes))
                cache_minutes = 0.0
                if outcome.cache_max_age_seconds:
                    cache_minutes = min(
                        MAX_CACHE_CONTROL_MINUTES, outcome.cache_max_age_seconds / 60.0
                    )
                health["next_attempt"] = _iso(
                    now + timedelta(minutes=max(wait_minutes, cache_minutes))
                )
            else:
                health["last_failure"] = _iso(now)
                health["consecutive_failures"] = int(health.get("consecutive_failures", 0)) + 1
                health["failure_count"] = int(health.get("failure_count", 0)) + 1
                health["availability"] = (
                    HEALTH_UNAVAILABLE
                    if health["consecutive_failures"] >= UNAVAILABLE_AFTER_FAILURES
                    else HEALTH_DEGRADED
                )
                health["last_error"] = (outcome.error or "fetch failed")[:300]
                delay = backoff_minutes(health["consecutive_failures"])
                if outcome.retry_after_seconds:
                    delay = max(delay, min(MAX_BACKOFF_MINUTES, outcome.retry_after_seconds / 60.0))
                health["next_attempt"] = _iso(now + timedelta(minutes=delay))
            self._health[outcome.source_id] = health
            return dict(health)

    def record_legacy(
        self,
        sources: Iterable[NewsSource],
        errors: Iterable[str],
        *,
        now: datetime,
        default_interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    ) -> None:
        """Record outcomes for injected fetchers that only return errors.

        Test doubles and third-party callers predate per-source outcomes. Map
        the familiar ``"Source name: reason"`` errors back to their sources so
        health still moves for them.
        """
        error_list = [str(item) for item in errors]
        for source in sources:
            failed = any(
                error.startswith(f"{source.name}:") or source.name in error
                for error in error_list
            )
            if failed:
                reason = next(
                    (
                        error.split(":", 1)[1].strip()
                        for error in error_list
                        if error.startswith(f"{source.name}:")
                    ),
                    "fetch failed",
                )
                outcome = SourceFetchOutcome(
                    source.id, source.name, "failed", error=reason
                )
            else:
                outcome = SourceFetchOutcome(source.id, source.name, "ok")
            self.record(
                outcome,
                now=now,
                interval_minutes=self.interval_minutes(source, default_interval_minutes),
            )

    def summary(self) -> dict:
        """Availability-only overview; credibility is never computed here."""
        health = self.all()
        counts = {HEALTH_OK: 0, HEALTH_DEGRADED: 0, HEALTH_UNAVAILABLE: 0, HEALTH_UNKNOWN: 0}
        for record in health.values():
            availability = record.get("availability", HEALTH_UNKNOWN)
            counts[availability if availability in counts else HEALTH_UNKNOWN] += 1
        return {
            "sources": health,
            "counts": counts,
            "success_rate": {
                source_id: success_rate(record) for source_id, record in health.items()
            },
        }
