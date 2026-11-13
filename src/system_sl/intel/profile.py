"""Persona and active-task signals for Daily Intel.

Both sources are optional. Nothing here raises for missing, corrupt, empty or
incomplete data: the recommendation engine is told whether a signal is usable
and adapts its weights accordingly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from system_sl.intel.topics import TOPIC_KEYWORDS, classify, terms, tokenize
from system_sl.utils.paths import get_tasks_file_path

EXPECTED_PERSONA_ANSWERS = 10
EXPIRED_TASK_GRACE_DAYS = 30


@dataclass
class PersonaSignal:
    """Weighted interests derived from the player's persona answers."""

    available: bool = False
    complete: bool = False
    text: str = ""
    keywords: dict[str, float] = field(default_factory=dict)
    topics: set[str] = field(default_factory=set)
    answers: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class TaskSignal:
    """Active quests and their weighted terms."""

    available: bool = False
    tasks: list[dict] = field(default_factory=list)
    keywords: dict[str, float] = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)
    error: str | None = None


def _read_json(path: Path | str | None, default):
    if not path:
        return default
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            text = handle.read().strip()
        if not text:
            return default
        return json.loads(text)
    except (OSError, ValueError, TypeError):
        return default


def _add_terms(profile: dict[str, float], text: str, weight: float) -> None:
    for term in terms(text):
        old = profile.get(term, 0.0)
        profile[term] = max(old, weight) if " " in term else old + weight


def _topic_matches(text: str) -> set[str]:
    normalized = " ".join(tokenize(text))
    token_set = set(tokenize(text))
    found: set[str] = set()
    for topic, keywords in TOPIC_KEYWORDS.items():
        for keyword in keywords:
            if " " in keyword:
                if keyword in normalized:
                    found.add(topic)
                    break
            elif keyword in token_set:
                found.add(topic)
                break
    return found


def load_persona_signal(persona_path: str | Path | None = None) -> PersonaSignal:
    """Load the optional persona profile into a weighted keyword signal."""
    path = Path(persona_path) if persona_path else Path(get_tasks_file_path("persona.json"))
    data = _read_json(path, None)
    if not isinstance(data, dict):
        return PersonaSignal(error=None if not path.exists() else "persona.json is unreadable")

    responses = data.get("responses")
    if not isinstance(responses, list):
        return PersonaSignal(error="persona.json has no responses")

    answers: list[str] = []
    keywords: dict[str, float] = {}
    topics: set[str] = set()
    non_empty = 0

    for response in responses:
        if not isinstance(response, dict):
            continue
        answer = response.get("answer")
        if not isinstance(answer, str):
            continue
        answer = " ".join(answer.split())
        if len(answer) < 2:
            continue
        non_empty += 1
        try:
            impact = max(1, min(3, int(response.get("impact", 1))))
        except (TypeError, ValueError):
            impact = 1

        answers.append(answer)
        # Repeating the answer by impact mirrors the existing vectorizer's
        # weighting while keeping matching cheap.
        _add_terms(keywords, answer, float(impact))
        category = response.get("category")
        if isinstance(category, str) and category.strip():
            _add_terms(keywords, category, 0.75)
        topics.update(_topic_matches(answer))
        _, confidence = classify(answer)
        if confidence >= 30:
            topic, _ = classify(answer)
            if topic:
                topics.add(topic)

    available = bool(answers and keywords)
    return PersonaSignal(
        available=available,
        complete=non_empty >= EXPECTED_PERSONA_ANSWERS,
        text=" ".join(answers),
        keywords=keywords,
        topics=topics,
        answers=answers,
    )


def _parse_deadline(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return None


def _completed_titles(path: str | Path | None) -> set[str]:
    if not path:
        return set()
    data = _read_json(path, [])
    titles: set[str] = set()
    if isinstance(data, dict):
        values = []
        for task_list in data.values():
            if isinstance(task_list, list):
                values.extend(task_list)
        data = values
    if not isinstance(data, list):
        return titles
    for entry in data:
        if isinstance(entry, dict):
            title = entry.get("title")
        else:
            title = entry
        if isinstance(title, str) and title.strip():
            titles.add(title.strip().casefold())
    return titles


def load_task_signal(
    tasks_path: str | Path | None = None,
    completed_path: str | Path | None = None,
    *,
    today: date | None = None,
) -> TaskSignal:
    """Load active quests, excluding completed and long-expired entries."""
    task_file = Path(tasks_path) if tasks_path else Path(get_tasks_file_path("tasks.json"))
    completed_file = (
        Path(completed_path) if completed_path else Path(get_tasks_file_path("completed_tasks.json"))
    )
    data = _read_json(task_file, [])
    completed = _completed_titles(completed_file)
    today = today or date.today()

    raw_tasks: list[dict] = []
    if isinstance(data, list):
        raw_tasks = [item for item in data if isinstance(item, dict)]
    elif isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                raw_tasks.extend(item for item in value if isinstance(item, dict))

    tasks: list[dict] = []
    keywords: dict[str, float] = {}
    labels: list[str] = []

    for task in raw_tasks:
        title = task.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        title = " ".join(title.split())
        if title.casefold() in completed:
            continue

        deadline = _parse_deadline(task.get("deadline"))
        if deadline and (today - deadline).days > EXPIRED_TASK_GRACE_DAYS:
            continue

        # Overdue quests still matter, but less than work that can still be
        # completed. Completed quests were removed above.
        weight = 0.6 if deadline and deadline < today else 1.0
        category = task.get("category")
        terms_text = title
        if isinstance(category, str) and category.strip() and category.casefold() != "none":
            terms_text = f"{title} {category}"

        _add_terms(keywords, title, weight)
        if isinstance(category, str) and category.strip() and category.casefold() != "none":
            _add_terms(keywords, category, weight * 0.5)

        tasks.append(
            {
                "title": title,
                "category": category if isinstance(category, str) else "",
                "deadline": deadline.isoformat() if deadline else None,
                "overdue": bool(deadline and deadline < today),
                "weight": weight,
                "terms": terms(terms_text),
            }
        )
        labels.append(title)

    return TaskSignal(
        available=bool(tasks),
        tasks=tasks,
        keywords=keywords,
        labels=labels[:8],
    )


def profile_context(persona: PersonaSignal, tasks: TaskSignal, preferred_topics: Iterable[str] = ()) -> str:
    """Build a short, human-readable context block for LLM summaries."""
    parts: list[str] = []
    if persona.available:
        interests = [answer for answer in persona.answers[:5] if answer]
        if interests:
            parts.append("Persona interests: " + "; ".join(interests))
        if persona.topics:
            parts.append("Inferred topics: " + ", ".join(sorted(persona.topics)[:8]))
    if tasks.available:
        parts.append("Active quests: " + "; ".join(tasks.labels[:5]))
    topics = [topic for topic in preferred_topics if topic]
    if topics:
        parts.append("Selected topics: " + ", ".join(sorted(set(topics))[:8]))
    return "\n".join(parts)


def matched_task(tasks: TaskSignal, article_terms: set[str]) -> str | None:
    """Return the active quest that overlaps most with an article, if any."""
    best_title = None
    best_overlap = 0
    for task in tasks.tasks:
        overlap = len(article_terms & task.get("terms", set()))
        if overlap > best_overlap:
            best_overlap = overlap
            best_title = task.get("title")
    return best_title if best_overlap >= 1 else None
