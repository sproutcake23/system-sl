"""Shared fixtures for Daily Intel tests (stdlib unittest, no network)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from system_sl.intel.sources import FetchResult, NewsSource
from system_sl.intel.summaries import fallback_summary

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
SOURCE = NewsSource(
    "test-source",
    "Test Source",
    "https://example.com/feed",
    "https://example.com",
    "General Technology",
    90,
)


def write_persona(path: Path, answers: list[tuple[str, int]] | None = None) -> None:
    if answers is None:
        answers = [
            ("Python AI machine learning", 3),
            ("build software APIs and developer tools", 2),
        ]
    responses = [
        {
            "question_id": index + 1,
            "category": "SKILLS",
            "question": "Question",
            "answer": answer,
            "impact": impact,
        }
        for index, (answer, impact) in enumerate(answers)
    ]
    path.write_text(json.dumps({"version": "1.0", "responses": responses}), encoding="utf-8")


def write_tasks(path: Path, titles: list[str] | None = None) -> None:
    if titles is None:
        titles = ["Build an AI chatbot"]
    tasks = [
        {
            "title": title,
            "created_at": NOW.isoformat(),
            "deadline": None,
            "category": "general",
        }
        for title in titles
    ]
    path.write_text(json.dumps(tasks), encoding="utf-8")


def make_article(
    identifier: str,
    title: str,
    description: str,
    category: str,
    *,
    hours_old: float = 2,
    reliability: int = SOURCE.reliability,
) -> dict:
    published = NOW - timedelta(hours=hours_old)
    return {
        "id": identifier,
        "title": title,
        "url": f"https://example.com/{identifier}",
        "description": description,
        "source_id": SOURCE.id,
        "source_name": SOURCE.name,
        "category": category,
        "reliability": reliability,
        "published_at": published.isoformat(),
        "fetched_at": NOW.isoformat(),
    }


def default_articles() -> list[dict]:
    return [
        make_article("p1", "Machine learning Python release", "A new Python toolkit for machine learning.", "AI & Machine Learning"),
        make_article("p2", "AI research update", "Researchers publish a new AI learning method.", "AI & Machine Learning"),
        make_article("p3", "Python developer workflow", "Python developer tools get an update.", "Software & Developer Tools"),
        make_article("p4", "AI model safety", "A new machine learning model is released.", "AI & Machine Learning"),
        make_article("t1", "Chatbot API release", "A new chatbot API for developers.", "Software & Developer Tools"),
        make_article("t2", "Chatbot framework update", "The framework adds developer tools.", "Software & Developer Tools"),
        make_article("t3", "Developer chatbot plugin", "A plugin for building a chatbot.", "Software & Developer Tools"),
        make_article("g1", "Space telescope discovers exoplanet", "Astronomers found a new exoplanet.", "Space", hours_old=5),
        make_article("g2", "Cybersecurity breach reported", "A company disclosed a data breach.", "Cybersecurity", hours_old=8),
    ]


class FakeFetcher:
    def __init__(self, articles=None, errors=None, exc=None):
        self.articles = list(articles or [])
        self.errors = list(errors or [])
        self.exc = exc
        self.calls = 0

    def __call__(self, sources):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return FetchResult(
            articles=self.articles,
            errors=self.errors,
            attempted=len(list(sources)),
            succeeded=0 if self.errors else len(list(sources)),
        )


class FakeSummarizer:
    def __init__(self, *, fail: bool = False, available: bool = False):
        self.fail = fail
        self.available = available
        self.calls = 0

    def llm_available(self):
        return self.available

    def summarize(self, article, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider unavailable")
        return fallback_summary(
            article,
            score=kwargs.get("score"),
            persona=kwargs.get("persona"),
            tasks=kwargs.get("tasks"),
            preferred_topics=kwargs.get("preferred_topics", ()),
        )
