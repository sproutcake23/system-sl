"""Daily briefing orchestration.

This module is deliberately UI-free. The GUI wraps :class:`BriefingService` in
a worker thread, while tests inject fake feeds and LLM summarizers.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Iterable, Optional

from system_sl.intel.clustering import cluster_articles, single_cluster
from system_sl.intel.extraction import STATUS_EXCERPT, STATUS_FULL, ContentExtractor
from system_sl.intel.health import SourceHealthTracker, success_rate
from system_sl.intel.profile import (
    PersonaSignal,
    TaskSignal,
    load_persona_signal,
    load_task_signal,
    profile_context,
)
from system_sl.intel.registry import SourceRegistry
from system_sl.intel.scoring import collect_history, score_article, select_briefing
from system_sl.intel.sources import (
    DEFAULT_SOURCES,
    FetchResult,
    NewsSource,
    SourceFetchOutcome,
    fetch_all,
    merge_article_cache,
)
from system_sl.intel.storage import IntelStore
from system_sl.intel.summaries import SummaryEngine, fallback_summary
from system_sl.intel.topics import TOPICS, topic_preference

_EMPTY_SECTIONS = {"persona": [], "tasks": [], "general": []}


class BriefingService:
    """Builds, caches and serves Daily Intel briefings."""

    def __init__(
        self,
        store: IntelStore | None = None,
        *,
        fetcher=None,
        summarizer=None,
        now=None,
        persona_path: str | None = None,
        tasks_path: str | None = None,
        completed_path: str | None = None,
        sources: Iterable[NewsSource] | None = None,
        registry: SourceRegistry | None = None,
        health: SourceHealthTracker | None = None,
        content_extractor: ContentExtractor | None = None,
    ) -> None:
        self.store = store or IntelStore()
        self.fetcher = fetcher or fetch_all
        self.summarizer = summarizer or SummaryEngine(self.store)
        self._now = now or (lambda: datetime.now().astimezone())
        self.persona_path = persona_path
        self.tasks_path = tasks_path
        self.completed_path = completed_path
        # An explicit ``sources`` tuple (tests, embedders) pins the catalog.
        # Otherwise the persisted registry is the source of truth.
        self.sources = tuple(sources) if sources is not None else None
        self.registry = (
            registry
            if registry is not None
            else (None if self.sources is not None else SourceRegistry(self.store))
        )
        self.health = health or SourceHealthTracker(self.store)
        self.content = content_extractor or ContentExtractor(self.store, clock=self._now)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Clocks and config
    # ------------------------------------------------------------------

    def now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def today(self) -> str:
        return self.now().date().isoformat()

    def get_config(self) -> dict:
        return self.store.load_config()

    def set_enabled(self, enabled: bool) -> dict:
        config = self.store.load_config()
        config["enabled"] = bool(enabled)
        self.store.save_config(config)
        return config

    def set_preferred_topics(self, topics: Iterable[str]) -> dict:
        config = self.store.load_config()
        valid = {str(topic) for topic in topics if str(topic) in TOPICS}
        config["preferred_topics"] = sorted(valid)
        self.store.save_config(config)
        return config

    def set_source_enabled(self, source_id: str, enabled: bool) -> dict:
        config = self.store.load_config()
        disabled = set(config.get("disabled_sources", []))
        if enabled:
            disabled.discard(source_id)
        else:
            disabled.add(source_id)
        config["disabled_sources"] = sorted(disabled)
        self.store.save_config(config)
        return config

    # ------------------------------------------------------------------
    # Source registry
    # ------------------------------------------------------------------

    def all_sources(self) -> tuple[NewsSource, ...]:
        """The active catalog: an injected tuple, or the persisted registry."""
        if self.sources is not None:
            return self.sources
        if self.registry is None:
            return DEFAULT_SOURCES
        return tuple(self.registry.sources())

    def source_catalog(self) -> list[dict]:
        config = self.store.load_config()
        disabled = set(config.get("disabled_sources", []))
        catalog = []
        for source in self.all_sources():
            health = self.health.get(source.id)
            catalog.append(
                {
                    **source.as_dict(),
                    "enabled": source.id not in disabled,
                    "interval_minutes": self.health.interval_minutes(
                        source,
                        self._default_interval_minutes(config),
                    ),
                    "health": {
                        **health,
                        "success_rate": success_rate(health),
                    },
                }
            )
        return catalog

    def add_source(self, payload: dict) -> dict:
        if self.registry is None:
            raise ValueError("the source registry is not available in this service")
        source = self.registry.add(payload)
        return self._source_entry(source)

    def update_source(self, source_id: str, payload: dict) -> dict:
        if self.registry is None:
            raise ValueError("the source registry is not available in this service")
        source = self.registry.update(source_id, payload)
        return self._source_entry(source)

    def remove_source(self, source_id: str) -> bool:
        if self.registry is None:
            return False
        return self.registry.remove(source_id)

    def reset_sources(self) -> list[dict]:
        if self.registry is None:
            return self.source_catalog()
        self.registry.reset()
        return self.source_catalog()

    def source_health(self) -> dict:
        return self.health.summary()

    def _source_entry(self, source: NewsSource) -> dict:
        config = self.store.load_config()
        disabled = set(config.get("disabled_sources", []))
        return {
            **source.as_dict(),
            "enabled": source.id not in disabled,
            "interval_minutes": self.health.interval_minutes(
                source, self._default_interval_minutes(config)
            ),
        }

    # ------------------------------------------------------------------
    # Cached reads
    # ------------------------------------------------------------------

    def has_briefing(self, date: str | None = None) -> bool:
        try:
            return self.store.get_briefing(date or self.today()) is not None
        except Exception:
            return False

    def get_cached_briefing(self, date: str | None = None) -> Optional[dict]:
        try:
            return self.store.get_briefing(date or self.today()) or self.store.latest_briefing()
        except Exception:
            return None

    def latest_briefing(self) -> Optional[dict]:
        try:
            return self.store.latest_briefing()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    def record_feedback(self, article_id: str, action: str, topic: str | None = None) -> dict:
        return self.store.record_feedback(article_id, action, topic=topic)

    def feedback_state(self, article_id: str) -> dict:
        return self.store.article_feedback(article_id)

    def is_hidden(self, article_id: str, topic: str | None = None) -> bool:
        state = self.store.article_feedback(article_id)
        if state.get("disliked") or state.get("known"):
            return True
        if topic:
            feedback = self.store.load_feedback()
            if topic_preference(feedback, topic) <= -3:
                return True
        return False

    def reset_preferences(self) -> None:
        """Clear news feedback and selected topics. Never touches the persona."""
        self.store.reset_feedback()
        config = self.store.load_config()
        config["preferred_topics"] = []
        self.store.save_config(config)
        self.store.clear_summaries()

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def fetch_due(self) -> bool:
        """Whether at least one enabled source is due for an update."""
        try:
            config = self.store.load_config()
            return bool(self._due_sources(config, self.now()))
        except Exception:
            return False

    def collect_if_due(self, *, allow_network: bool = True) -> bool:
        """Refresh the article cache without rebuilding the briefing.

        The scheduler calls this between daily briefings so the next briefing
        starts from recent articles instead of a day-old cache. Each source is
        only contacted when its own interval (or retry backoff) allows it.
        """
        if not allow_network or not self._lock.acquire(blocking=False):
            return False
        try:
            now = self.now()
            config = self.store.load_config()
            if not config.get("enabled", True):
                return False
            sources = self._due_sources(config, now)
            if not sources:
                return False
            articles = self.store.load_articles()
            self._fetch_into_cache(sources, articles, config, now)
            return True
        except Exception:
            return False
        finally:
            self._lock.release()

    def refresh(self, *, force: bool = False, allow_network: bool = True) -> dict:
        """Build today's briefing, or return a cached/fallback briefing.

        The method never raises for missing data or unavailable services. The
        returned dict always contains ``state``, ``sections``, ``warnings`` and
        ``meta`` so the GUI can render a state instead of a traceback.
        """
        now = self.now()
        if not self._lock.acquire(blocking=False):
            cached = self.store.get_briefing(now.date().isoformat()) or self.store.latest_briefing()
            if cached:
                result = dict(cached)
                result["warnings"] = list(result.get("warnings", [])) + [
                    "A briefing is already being prepared. Showing the last saved one."
                ]
                return result
            return self._state(
                "empty",
                now,
                warnings=["A briefing is already being prepared. Try again in a moment."],
            )
        try:
            return self._refresh_locked(now, force=force, allow_network=allow_network)
        finally:
            self._lock.release()

    def _refresh_locked(self, now: datetime, *, force: bool, allow_network: bool) -> dict:
        today = now.date().isoformat()
        config = self.store.load_config()
        if not config.get("enabled", True):
            return self._state(
                "disabled",
                now,
                warnings=["Daily Intel is turned off. Enable it to receive a daily briefing."],
            )

        if not force:
            cached = self.store.get_briefing(today)
            if cached is not None:
                return cached

        persona = load_persona_signal(self.persona_path)
        tasks = load_task_signal(
            self.tasks_path,
            self.completed_path,
            today=now.date(),
        )
        feedback = self.store.load_feedback()
        articles = self.store.load_articles()
        warnings: list[str] = []
        if persona.error:
            warnings.append("Your Player Profile could not be read; showing broader intelligence.")

        sources = [
            source
            for source in self.all_sources()
            if source.id not in set(config.get("disabled_sources", []))
        ]
        fetch_state = "cached"
        errors: list[str] = []

        if not sources:
            warnings.append("All news sources are disabled; using cached articles.")
            fetch_state = "cached"
        elif allow_network:
            due = self._due_sources(config, now)
            if due:
                fetch_state, errors = self._fetch_into_cache(due, articles, config, now)
                articles = self.store.load_articles()
                if errors:
                    warnings.append("Some news sources were unavailable.")
                    warnings.extend(errors[:3])
        health = self.health.summary().get("counts", {})
        unavailable = int(health.get("unavailable", 0) or 0)
        if unavailable:
            warnings.append(
                f"{unavailable} source{'s' if unavailable != 1 else ''} temporarily unavailable; "
                "they will be retried later."
            )

        if not articles:
            # A failed online refresh must not erase a good briefing.
            cached_today = self.store.get_briefing(today)
            if cached_today is not None:
                result = dict(cached_today)
                result["warnings"] = list(result.get("warnings", [])) + warnings
                return result
            latest = self.store.latest_briefing()
            if latest is not None:
                result = dict(latest)
                result["state"] = "offline"
                result["warnings"] = list(result.get("warnings", [])) + warnings + [
                    "No new articles could be collected. Showing the last successful briefing."
                ]
                return result
            return self._state(
                "empty",
                now,
                warnings=warnings + [
                    "No news could be collected. Check your connection or enable another source."
                ],
                meta=self._meta(persona, tasks, articles, sources, errors, fetch_state),
            )

        # Group coverage of the same event before scoring, so one story is not
        # shown several times just because multiple publishers wrote about it.
        clusters = (
            cluster_articles(articles, now=now)
            if config.get("story_clustering", True)
            else [single_cluster(article) for article in articles]
        )
        cluster_by_primary = {cluster["primary"]: cluster for cluster in clusters}

        previous = {
            date_key: briefing
            for date_key, briefing in self.store.load_briefings().items()
            if isinstance(date_key, str) and date_key < today
        }
        history_ids, history_titles = collect_history(previous, feedback, today=now)
        scored = [
            score_article(
                cluster["article"],
                persona,
                tasks,
                preferred_topics=config.get("preferred_topics", []),
                feedback=feedback,
                now=now,
                history_ids=history_ids,
                history_titles=history_titles,
            )
            for cluster in clusters
        ]
        sections = select_briefing(
            scored,
            has_persona=persona.available,
            has_tasks=tasks.available,
            max_articles=config.get("max_articles", 9),
        )

        if not any(sections.values()):
            cached_today = self.store.get_briefing(today)
            if cached_today is not None:
                result = dict(cached_today)
                result["warnings"] = list(result.get("warnings", [])) + warnings
                return result
            return self._state(
                "empty",
                now,
                warnings=warnings + [
                    "No suitable articles were found for your current preferences."
                ],
                meta=self._meta(persona, tasks, articles, sources, errors, fetch_state),
            )

        context = profile_context(persona, tasks, config.get("preferred_topics", []))
        entries = [entry for section in sections.values() for entry in section]
        sources_by_id = {source.id: source for source in sources}
        content_by_id: dict[str, dict] = {}

        # Selective extraction: only articles chosen for the briefing, only
        # when the feed text is thin, and only up to a per-run budget.
        extraction_allowed = (
            allow_network and config.get("extract_full_articles", True)
        )
        download_budget = (
            int(config.get("max_extractions_per_run", 6)) if extraction_allowed else 0
        )
        downloads = 0
        for entry in entries:
            article = entry.get("article") if isinstance(entry.get("article"), dict) else {}
            article_id = str(article.get("id") or "")
            cluster = cluster_by_primary.get(article_id)
            if isinstance(cluster, dict):
                entry["story"] = {
                    "id": cluster.get("id"),
                    "kind": cluster.get("kind"),
                    "size": cluster.get("size", 1),
                    "sources": cluster.get("sources", []),
                    "coverage": cluster.get("coverage", []),
                }
            source = sources_by_id.get(str(article.get("source_id") or ""))
            content_entry = self.content.cached(article_id) if article_id else None
            if content_entry is None:
                if downloads < download_budget and self.content.should_fetch(
                    article, source=source
                ):
                    content_entry = self.content.fetch(article, source=source)
                    downloads += 1
                else:
                    content_entry = self.content.status_for(article)
            status = str(content_entry.get("status") or STATUS_EXCERPT)
            entry["article"] = {**article, "content_status": status}
            if status == STATUS_FULL and content_entry.get("text"):
                content_by_id[article_id] = content_entry
            health_record = self.health.get(str(article.get("source_id") or ""))
            entry["source_status"] = {
                "availability": health_record.get("availability"),
                "last_status": health_record.get("last_status"),
                "success_rate": success_rate(health_record),
                "average_response_ms": health_record.get("average_response_ms"),
                "last_success": health_record.get("last_success"),
            }

        def summarize_entry(entry: dict) -> None:
            article = entry.get("article", {})
            score = entry.get("score", {})
            material = dict(article)
            content = content_by_id.get(str(article.get("id") or ""))
            if content:
                material["content"] = content.get("text", "")
            try:
                summary = self.summarizer.summarize(
                    material,
                    persona=persona,
                    tasks=tasks,
                    preferred_topics=config.get("preferred_topics", []),
                    score=score,
                    context=context,
                )
            except Exception:
                summary = fallback_summary(
                    material,
                    score=score,
                    persona=persona,
                    tasks=tasks,
                    preferred_topics=config.get("preferred_topics", []),
                )
            if not isinstance(summary, dict) or not summary.get("what_happened"):
                summary = fallback_summary(
                    material,
                    score=score,
                    persona=persona,
                    tasks=tasks,
                    preferred_topics=config.get("preferred_topics", []),
                )
            if isinstance(summary, dict):
                summary.setdefault("content_status", article.get("content_status"))
            entry["summary"] = summary

        if entries:
            # Summaries may call a remote model; a small pool keeps a refresh
            # responsive without flooding a provider with parallel requests.
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=min(4, len(entries))) as pool:
                list(pool.map(summarize_entry, entries))

        state = "ok"
        if fetch_state == "failed":
            state = "offline"
        elif fetch_state == "partial":
            state = "partial"

        stories = [
            {
                "id": entry["story"].get("id"),
                "kind": entry["story"].get("kind"),
                "size": entry["story"].get("size", 1),
                "primary": entry["article"].get("id"),
                "sources": entry["story"].get("sources", []),
                "coverage": entry["story"].get("coverage", []),
            }
            for entry in entries
            if isinstance(entry.get("story"), dict) and entry["story"].get("size", 1) > 1
        ]
        meta = self._meta(persona, tasks, articles, sources, errors, fetch_state)
        meta["stories"] = len(clusters)
        meta["stories_merged"] = sum(
            max(0, int(cluster.get("size", 1)) - 1) for cluster in clusters
        )
        meta["stories_in_briefing"] = len(stories)
        meta["extraction"] = {
            "enabled": bool(config.get("extract_full_articles", True)),
            "downloaded": downloads,
            "full_text": sum(
                1
                for entry in entries
                if (entry.get("article") or {}).get("content_status") == STATUS_FULL
            ),
        }
        meta["source_health"] = self.health.summary().get("counts", {})
        briefing = {
            "date": today,
            "generated_at": now.isoformat(),
            "state": state,
            "sections": sections,
            "stories": stories,
            "warnings": warnings,
            "meta": meta,
        }
        self.store.save_briefing(briefing)
        config["last_updated"] = now.isoformat()
        self.store.save_config(config)
        return briefing

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _default_interval_minutes(self, config: dict) -> int:
        try:
            return max(1, int(config.get("fetch_interval_hours", 6))) * 60
        except (TypeError, ValueError):
            return 6 * 60

    def _due_sources(self, config: dict, now: datetime) -> list[NewsSource]:
        """Enabled sources whose own interval or backoff allows a request."""
        disabled = set(config.get("disabled_sources", []))
        enabled = [source for source in self.all_sources() if source.id not in disabled]
        return self.health.due_sources(
            enabled,
            now,
            default_interval_minutes=self._default_interval_minutes(config),
        )

    def _fetch_into_cache(
        self,
        sources: list[NewsSource],
        articles: list[dict],
        config: dict,
        now: datetime,
    ) -> tuple[str, list[str]]:
        errors: list[str] = []
        fresh: list[dict] = []
        result = None
        fetch_exception: Exception | None = None
        if sources:
            try:
                if self.fetcher is fetch_all:
                    validators = {
                        source.id: self.health.conditional_headers(source.id)
                        for source in sources
                    }
                    result = self.fetcher(sources, state=validators, now=now)
                else:
                    result = self.fetcher(sources)
            except Exception as exc:  # noqa: BLE001 - offline is a normal state
                fetch_exception = exc
                errors = [f"News sources unavailable: {exc}"]

        if isinstance(result, FetchResult):
            fresh = list(result.articles)
            errors = list(result.errors)
            outcomes = list(result.outcomes)
        elif isinstance(result, list):
            fresh = [item for item in result if isinstance(item, dict)]
            outcomes = []
        else:
            outcomes = []

        default_interval = self._default_interval_minutes(config)
        if outcomes:
            for outcome in outcomes:
                source = next(
                    (item for item in sources if item.id == outcome.source_id), None
                )
                interval = (
                    self.health.interval_minutes(source, default_interval)
                    if source is not None
                    else default_interval
                )
                self.health.record(outcome, now=now, interval_minutes=interval)
        elif sources:
            if fetch_exception is not None:
                # The fetcher raised before any per-source outcome existed, so
                # every attempted source failed this pass.
                for source in sources:
                    self.health.record(
                        SourceFetchOutcome(
                            source.id,
                            source.name,
                            "failed",
                            error=f"{type(fetch_exception).__name__}: {fetch_exception}",
                        ),
                        now=now,
                        interval_minutes=self.health.interval_minutes(
                            source, default_interval
                        ),
                    )
            else:
                self.health.record_legacy(
                    sources,
                    errors,
                    now=now,
                    default_interval_minutes=default_interval,
                )
        self.health.save()

        merged = merge_article_cache(articles, fresh, now=now)
        self.store.save_articles(merged)

        # Record the attempt even when every feed failed, so a dead network
        # does not trigger a retry storm on every window repaint. Per-source
        # scheduling is authoritative; this remains for compatibility.
        config["last_fetch"] = now.astimezone(timezone.utc).isoformat()
        self.store.save_config(config)

        if not fresh:
            # Unchanged feeds (304) are a successful pass even though they
            # return no new articles; the cached articles remain valid.
            if int(getattr(result, "not_modified", 0) or 0) and not errors:
                return "ok", errors
            return "failed", errors
        return ("partial" if errors else "ok"), errors

    def _meta(
        self,
        persona: PersonaSignal,
        tasks: TaskSignal,
        articles: list[dict],
        sources: list[NewsSource],
        errors: list[str],
        fetch_state: str,
    ) -> dict:
        try:
            llm_available = bool(getattr(self.summarizer, "llm_available", lambda: False)())
        except Exception:
            llm_available = False
        return {
            "persona": persona.available,
            "persona_complete": persona.complete,
            "tasks": tasks.available,
            "task_count": len(tasks.tasks),
            "llm": llm_available,
            "sources_total": len(sources),
            "sources_failed": len(errors),
            "fetch_state": fetch_state,
            "articles_considered": len(articles),
        }

    def _state(
        self,
        state: str,
        now: datetime,
        *,
        warnings: list[str] | None = None,
        meta: dict | None = None,
    ) -> dict:
        return {
            "date": now.date().isoformat(),
            "generated_at": now.isoformat(),
            "state": state,
            "sections": {key: [] for key in _EMPTY_SECTIONS},
            "warnings": list(warnings or []),
            "meta": meta or {
                "persona": False,
                "persona_complete": False,
                "tasks": False,
                "task_count": 0,
                "llm": False,
                "sources_total": len(self.all_sources()),
                "sources_failed": 0,
                "fetch_state": "none",
                "articles_considered": 0,
            },
        }


_service: BriefingService | None = None
_service_lock = threading.Lock()


def get_intel_service() -> BriefingService:
    """Return the process-wide service so the GUI and scheduler share a cache."""
    global _service
    with _service_lock:
        if _service is None:
            _service = BriefingService()
        return _service
