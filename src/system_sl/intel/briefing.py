"""Daily briefing orchestration.

This module is deliberately UI-free. The GUI wraps :class:`BriefingService` in
a worker thread, while tests inject fake feeds and LLM summarizers.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Iterable, Optional

from system_sl.intel.profile import (
    PersonaSignal,
    TaskSignal,
    load_persona_signal,
    load_task_signal,
    profile_context,
)
from system_sl.intel.scoring import collect_history, score_article, select_briefing
from system_sl.intel.sources import (
    DEFAULT_SOURCES,
    FetchResult,
    NewsSource,
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
    ) -> None:
        self.store = store or IntelStore()
        self.fetcher = fetcher or fetch_all
        self.summarizer = summarizer or SummaryEngine(self.store)
        self._now = now or (lambda: datetime.now().astimezone())
        self.persona_path = persona_path
        self.tasks_path = tasks_path
        self.completed_path = completed_path
        self.sources = tuple(sources) if sources is not None else DEFAULT_SOURCES
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

    def source_catalog(self) -> list[dict]:
        config = self.store.load_config()
        disabled = set(config.get("disabled_sources", []))
        return [
            {**source.as_dict(), "enabled": source.id not in disabled}
            for source in self.sources
        ]

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
        """Whether the configured feed interval has elapsed."""
        try:
            return self._fetch_due(self.store.load_config(), self.now())
        except Exception:
            return False

    def collect_if_due(self, *, allow_network: bool = True) -> bool:
        """Refresh the article cache without rebuilding the briefing.

        The scheduler calls this between daily briefings so the next briefing
        starts from recent articles instead of a day-old cache.
        """
        if not allow_network or not self._lock.acquire(blocking=False):
            return False
        try:
            now = self.now()
            config = self.store.load_config()
            if not config.get("enabled", True) or not self._fetch_due(config, now):
                return False
            sources = [
                source
                for source in self.sources
                if source.id not in set(config.get("disabled_sources", []))
            ]
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

        sources = [source for source in self.sources if source.id not in set(config.get("disabled_sources", []))]
        fetch_state = "cached"
        errors: list[str] = []

        if not sources:
            warnings.append("All news sources are disabled; using cached articles.")
            fetch_state = "cached"
        elif allow_network and (force or self._fetch_due(config, now)):
            fetch_state, errors = self._fetch_into_cache(sources, articles, config, now)
            articles = self.store.load_articles()
            if errors:
                warnings.append("Some news sources were unavailable.")
                warnings.extend(errors[:3])

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

        previous = {
            date_key: briefing
            for date_key, briefing in self.store.load_briefings().items()
            if isinstance(date_key, str) and date_key < today
        }
        history_ids, history_titles = collect_history(previous, feedback, today=now)
        scored = [
            score_article(
                article,
                persona,
                tasks,
                preferred_topics=config.get("preferred_topics", []),
                feedback=feedback,
                now=now,
                history_ids=history_ids,
                history_titles=history_titles,
            )
            for article in articles
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

        def summarize_entry(entry: dict) -> None:
            article = entry.get("article", {})
            score = entry.get("score", {})
            try:
                summary = self.summarizer.summarize(
                    article,
                    persona=persona,
                    tasks=tasks,
                    preferred_topics=config.get("preferred_topics", []),
                    score=score,
                    context=context,
                )
            except Exception:
                summary = fallback_summary(
                    article,
                    score=score,
                    persona=persona,
                    tasks=tasks,
                    preferred_topics=config.get("preferred_topics", []),
                )
            if not isinstance(summary, dict) or not summary.get("what_happened"):
                summary = fallback_summary(
                    article,
                    score=score,
                    persona=persona,
                    tasks=tasks,
                    preferred_topics=config.get("preferred_topics", []),
                )
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

        briefing = {
            "date": today,
            "generated_at": now.isoformat(),
            "state": state,
            "sections": sections,
            "warnings": warnings,
            "meta": self._meta(persona, tasks, articles, sources, errors, fetch_state),
        }
        self.store.save_briefing(briefing)
        config["last_updated"] = now.isoformat()
        self.store.save_config(config)
        return briefing

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fetch_due(self, config: dict, now: datetime) -> bool:
        last = config.get("last_fetch")
        if not isinstance(last, str):
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        hours = (now - last_dt).total_seconds() / 3600.0
        return hours >= float(config.get("fetch_interval_hours", 6))

    def _fetch_into_cache(
        self,
        sources: list[NewsSource],
        articles: list[dict],
        config: dict,
        now: datetime,
    ) -> tuple[str, list[str]]:
        errors: list[str] = []
        fresh: list[dict] = []
        try:
            result = self.fetcher(sources)
            if isinstance(result, FetchResult):
                fresh = list(result.articles)
                errors = list(result.errors)
            elif isinstance(result, list):
                fresh = [item for item in result if isinstance(item, dict)]
        except Exception as exc:  # noqa: BLE001 - offline is a normal state
            errors = [f"News sources unavailable: {exc}"]

        merged = merge_article_cache(articles, fresh, now=now)
        self.store.save_articles(merged)

        # Record the attempt even when every feed failed, so a dead network
        # does not trigger a retry storm on every window repaint.
        config["last_fetch"] = now.astimezone(timezone.utc).isoformat()
        self.store.save_config(config)

        if not fresh:
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
                "sources_total": len(self.sources),
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
