"""Suggest task importance from the player's persona goals.

The MiniLM model is owned by :mod:`system_sl.core.vector_converter`; this
module only asks for the shared instance. Goal vectors are kept in memory for
the life of the process and rebuilt when persona.json's modification time
changes.
"""

from __future__ import annotations

import json
import logging
import threading
from bisect import bisect_right
from pathlib import Path

import numpy as np

from system_sl.core.ranking_config import (
    DEFAULT_IMPORTANCE,
    IMPACT_WEIGHTS,
    IMPORTANCE_BANDS,
)
from system_sl.utils import get_tasks_file_path

log = logging.getLogger(__name__)

DEFAULT_PERSONA_PATH = Path(get_tasks_file_path("persona.json"))

# One in-memory cache shared by every caller in this process.
_cache_lock = threading.Lock()
_goal_cache: dict = {
    "path": None,
    "mtime_ns": None,
    "model": None,
    "goals": [],
    "vectors": None,
}


def load_goals(persona_path=None) -> list[tuple[str, float]]:
    """Read one goal per persona response as ``(text, impact_weight)``.

    Answers are stripped and lowercased, never repeated and never
    spell-corrected. Empty answers are skipped. Missing or unreadable persona
    files produce an empty list.
    """
    path = Path(persona_path) if persona_path is not None else DEFAULT_PERSONA_PATH

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        log.debug("Could not read persona goals from %s", path, exc_info=True)
        return []

    if not isinstance(data, dict):
        return []

    responses = data.get("responses", [])
    if not isinstance(responses, list):
        return []

    goals: list[tuple[str, float]] = []
    for entry in responses:
        if not isinstance(entry, dict):
            continue

        raw_answer = entry.get("answer", "")
        answer = str(raw_answer).strip().lower() if raw_answer is not None else ""
        if not answer:
            continue

        try:
            impact = int(entry.get("impact", 1))
        except (TypeError, ValueError):
            impact = 1
        impact = max(1, min(3, impact))
        goals.append((answer, IMPACT_WEIGHTS[impact]))

    return goals


def _persona_mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _encode_goals(model, goals: list[tuple[str, float]]) -> np.ndarray:
    texts = [text for text, _ in goals]
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)

    vectors = np.asarray(
        model.encode(texts, normalize_embeddings=True), dtype=np.float32
    )
    if vectors.ndim == 1:
        vectors = vectors.reshape(1, -1)
    if vectors.shape[0] != len(goals):
        raise ValueError("embedder returned the wrong number of goal vectors")
    return vectors


def _default_goal_vectors(model):
    """Return cached default goals and their vectors for ``model``."""
    path = Path(DEFAULT_PERSONA_PATH)
    mtime_ns = _persona_mtime_ns(path)

    with _cache_lock:
        if (
            _goal_cache["path"] == path
            and _goal_cache["mtime_ns"] == mtime_ns
            and _goal_cache["model"] is model
            and _goal_cache["vectors"] is not None
        ):
            return _goal_cache["goals"], _goal_cache["vectors"]

        goals = load_goals(path)
        vectors = _encode_goals(model, goals)
        _goal_cache.update(
            {
                "path": path,
                "mtime_ns": mtime_ns,
                "model": model,
                "goals": goals,
                "vectors": vectors,
            }
        )
        return goals, vectors


def _importance_for_relevance(relevance: float) -> int:
    return bisect_right(IMPORTANCE_BANDS, relevance) + 1


def suggest_importance(
    title, *, model=None, goals=None
) -> tuple[int, str | None, float]:
    """Suggest importance 1-5 from how closely ``title`` matches a goal.

    ``matched_goal`` is the highest impact-weighted goal, and ``relevance`` is
    that weighted similarity. Empty or missing goals return the default
    importance with no match. Any failure also returns that fallback and logs
    a warning.
    """
    try:
        if model is None:
            from system_sl.core.vector_converter import get_shared_model

            model = get_shared_model()

        if goals is None:
            goal_list, goal_vectors = _default_goal_vectors(model)
        else:
            goal_list = list(goals)
            goal_vectors = None

        if not goal_list:
            return DEFAULT_IMPORTANCE, None, 0.0

        if goal_vectors is None:
            goal_vectors = _encode_goals(model, goal_list)

        title_vec = np.asarray(
            model.encode(str(title).strip().lower(), normalize_embeddings=True),
            dtype=np.float32,
        ).reshape(-1)

        sims = goal_vectors @ title_vec
        impact_weights = np.asarray(
            [weight for _, weight in goal_list], dtype=np.float32
        )
        weighted = impact_weights * sims
        best_index = int(np.argmax(weighted))

        relevance = float(weighted[best_index])
        return (
            _importance_for_relevance(relevance),
            goal_list[best_index][0],
            relevance,
        )
    except Exception as exc:
        log.warning("Could not suggest importance for %r: %s", title, exc, exc_info=True)
        return DEFAULT_IMPORTANCE, None, 0.0