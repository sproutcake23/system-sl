"""Local persistence for Daily Intel.

Everything lives in the same ``~/.config/system-sl`` directory as the rest of
the application and uses the project's JSON conventions. Writes are atomic:
a crash mid-save leaves the previous file intact instead of a truncated one.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from system_sl.utils.paths import get_tasks_file_path

CONFIG_FILENAME = "intel_config.json"
ARTICLES_FILENAME = "intel_articles.json"
BRIEFINGS_FILENAME = "intel_briefings.json"
FEEDBACK_FILENAME = "intel_feedback.json"
SUMMARIES_FILENAME = "intel_summaries.json"

DEFAULT_CONFIG: dict = {
    "version": 1,
    "enabled": True,
    "preferred_topics": [],
    "disabled_sources": [],
    "max_articles": 9,
    "fetch_interval_hours": 6,
    "last_fetch": None,
    "last_updated": None,
}

DEFAULT_FEEDBACK: dict = {
    "version": 1,
    "articles": {},
    "topics": {},
    "events": [],
    "updated_at": None,
}

VALID_ACTIONS = {"like", "dislike", "save", "known", "topic_not_interested", "topic_interest"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _topic_value(feedback: dict, topic: str) -> int:
    """Read a topic preference without trusting corrupt JSON."""
    try:
        return int(feedback.get("topics", {}).get(topic, 0))
    except (TypeError, ValueError):
        return 0


class IntelStore:
    """Reads and writes Daily Intel JSON files."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        if data_dir is None:
            data_dir = Path(get_tasks_file_path(CONFIG_FILENAME)).parent
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.config_path = self.data_dir / CONFIG_FILENAME
        self.articles_path = self.data_dir / ARTICLES_FILENAME
        self.briefings_path = self.data_dir / BRIEFINGS_FILENAME
        self.feedback_path = self.data_dir / FEEDBACK_FILENAME
        self.summaries_path = self.data_dir / SUMMARIES_FILENAME

    # ------------------------------------------------------------------
    # Low-level JSON helpers
    # ------------------------------------------------------------------

    def _load(self, path: Path, default):
        if not path.is_file():
            return json.loads(json.dumps(default))
        try:
            with path.open("r", encoding="utf-8") as handle:
                text = handle.read().strip()
            if not text:
                return json.loads(json.dumps(default))
            data = json.loads(text)
        except (OSError, ValueError, TypeError):
            return json.loads(json.dumps(default))
        return data

    def _save(self, path: Path, data) -> bool:
        tmp_name: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.data_dir,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp_name = handle.name
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            return True
        except (OSError, TypeError, ValueError):
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            return False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def load_config(self) -> dict:
        raw = self._load(self.config_path, DEFAULT_CONFIG)
        if not isinstance(raw, dict):
            raw = {}
        config = dict(DEFAULT_CONFIG)
        config.update({key: value for key, value in raw.items() if key in DEFAULT_CONFIG})

        config["enabled"] = bool(config.get("enabled", True))

        topics = config.get("preferred_topics")
        config["preferred_topics"] = [str(item) for item in topics] if isinstance(topics, list) else []

        disabled = config.get("disabled_sources")
        config["disabled_sources"] = [str(item) for item in disabled] if isinstance(disabled, list) else []

        try:
            config["max_articles"] = max(3, min(20, int(config.get("max_articles", 9))))
        except (TypeError, ValueError):
            config["max_articles"] = 9
        try:
            config["fetch_interval_hours"] = max(1, min(72, int(config.get("fetch_interval_hours", 6))))
        except (TypeError, ValueError):
            config["fetch_interval_hours"] = 6

        for key in ("last_fetch", "last_updated"):
            if config.get(key) is not None and not isinstance(config.get(key), str):
                config[key] = None
        return config

    def save_config(self, config: dict) -> bool:
        merged = dict(DEFAULT_CONFIG)
        if isinstance(config, dict):
            merged.update({key: value for key, value in config.items() if key in DEFAULT_CONFIG})
        return self._save(self.config_path, merged)

    # ------------------------------------------------------------------
    # Articles
    # ------------------------------------------------------------------

    def load_articles(self) -> list[dict]:
        raw = self._load(self.articles_path, {"version": 1, "articles": []})
        if isinstance(raw, dict):
            raw = raw.get("articles", [])
        if not isinstance(raw, list):
            return []
        return [article for article in raw if isinstance(article, dict) and article.get("id")]

    def save_articles(self, articles: list[dict]) -> bool:
        return self._save(
            self.articles_path,
            {
                "version": 1,
                "updated_at": _utc_now(),
                "articles": [article for article in articles if isinstance(article, dict)],
            },
        )

    # ------------------------------------------------------------------
    # Briefings
    # ------------------------------------------------------------------

    def load_briefings(self) -> dict:
        raw = self._load(self.briefings_path, {"version": 1, "briefings": {}})
        if isinstance(raw, dict) and isinstance(raw.get("briefings"), dict):
            return raw["briefings"]
        return {}

    def get_briefing(self, date: str) -> Optional[dict]:
        briefing = self.load_briefings().get(date)
        return briefing if isinstance(briefing, dict) else None

    def latest_briefing(self) -> Optional[dict]:
        briefings = self.load_briefings()
        if not briefings:
            return None
        latest_date = max(briefings)
        briefing = briefings.get(latest_date)
        return briefing if isinstance(briefing, dict) else None

    def save_briefing(self, briefing: dict) -> bool:
        if not isinstance(briefing, dict) or not briefing.get("date"):
            return False
        briefings = self.load_briefings()
        briefings[briefing["date"]] = briefing

        # Keep the archive small; the UI only needs recent briefings for
        # novelty scoring and offline fallback.
        keep = sorted(briefings)[-21:]
        briefings = {date: briefings[date] for date in keep}
        return self._save(self.briefings_path, {"version": 1, "updated_at": _utc_now(), "briefings": briefings})

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    def load_feedback(self) -> dict:
        raw = self._load(self.feedback_path, DEFAULT_FEEDBACK)
        if not isinstance(raw, dict):
            raw = {}
        feedback = {
            "version": 1,
            "articles": raw.get("articles") if isinstance(raw.get("articles"), dict) else {},
            "topics": raw.get("topics") if isinstance(raw.get("topics"), dict) else {},
            "events": raw.get("events") if isinstance(raw.get("events"), list) else [],
            "updated_at": raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None,
        }
        return feedback

    def save_feedback(self, feedback: dict) -> bool:
        return self._save(self.feedback_path, feedback)

    def article_feedback(self, article_id: str) -> dict:
        feedback = self.load_feedback()
        state = feedback.get("articles", {}).get(article_id)
        if not isinstance(state, dict):
            return {}
        return {
            key: bool(state.get(key))
            for key in ("liked", "disliked", "saved", "known")
            if state.get(key)
        }

    def record_feedback(self, article_id: str, action: str, *, topic: str | None = None) -> dict:
        """Record one player action and return the updated feedback payload."""
        if not article_id or action not in VALID_ACTIONS:
            return self.load_feedback()

        feedback = self.load_feedback()
        articles = feedback["articles"]
        state = articles.get(article_id)
        if not isinstance(state, dict):
            state = {"liked": False, "disliked": False, "saved": False, "known": False}

        if action == "like":
            state["liked"] = not bool(state.get("liked"))
            if topic:
                current = _topic_value(feedback, topic)
                feedback["topics"][topic] = (
                    min(3, current + 1) if state["liked"] else max(-3, current - 1)
                )
            if state["liked"]:
                state["disliked"] = False
        elif action == "dislike":
            state["disliked"] = not bool(state.get("disliked"))
            if topic:
                current = _topic_value(feedback, topic)
                feedback["topics"][topic] = (
                    max(-3, current - 1) if state["disliked"] else min(3, current + 1)
                )
            if state["disliked"]:
                state["liked"] = False
        elif action == "save":
            state["saved"] = not bool(state.get("saved"))
        elif action == "known":
            state["known"] = not bool(state.get("known"))
        elif action == "topic_not_interested":
            if topic:
                feedback["topics"][topic] = -3
        elif action == "topic_interest":
            if topic:
                feedback["topics"][topic] = min(3, _topic_value(feedback, topic) + 1)

        state["updated_at"] = _utc_now()
        articles[article_id] = state

        events = feedback["events"]
        events.append(
            {
                "article_id": article_id,
                "action": action,
                "topic": topic,
                "at": _utc_now(),
            }
        )
        feedback["events"] = events[-500:]
        feedback["updated_at"] = _utc_now()
        self.save_feedback(feedback)
        return feedback

    def reset_feedback(self) -> dict:
        feedback = json.loads(json.dumps(DEFAULT_FEEDBACK))
        feedback["updated_at"] = _utc_now()
        self.save_feedback(feedback)
        return feedback

    # ------------------------------------------------------------------
    # Summary cache
    # ------------------------------------------------------------------

    def load_summaries(self) -> dict:
        raw = self._load(self.summaries_path, {"version": 1, "summaries": {}})
        if isinstance(raw, dict) and isinstance(raw.get("summaries"), dict):
            return raw["summaries"]
        return {}

    def get_summary(self, key: str) -> Optional[dict]:
        summary = self.load_summaries().get(key)
        return summary if isinstance(summary, dict) else None

    def save_summary(self, key: str, summary: dict) -> bool:
        summaries = self.load_summaries()
        summaries[key] = summary
        # A generous cap keeps the cache useful without unbounded growth.
        if len(summaries) > 400:
            for old_key in list(summaries)[: len(summaries) - 400]:
                summaries.pop(old_key, None)
        return self._save(
            self.summaries_path,
            {"version": 1, "updated_at": _utc_now(), "summaries": summaries},
        )

    def clear_summaries(self) -> None:
        self._save(self.summaries_path, {"version": 1, "summaries": {}})
