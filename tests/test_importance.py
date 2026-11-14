"""Tests for goal loading and importance suggestions.

The fake embedder keeps every test deterministic and avoids downloading the
real MiniLM model.
"""

import json
import os

import numpy as np
import pytest

import system_sl.core.importance as importance
from system_sl.core.ranking_config import DEFAULT_IMPORTANCE, IMPACT_WEIGHTS


class FakeModel:
    """Deterministic embedder backed by a text-to-vector mapping."""

    def __init__(self, vectors, *, fail=False):
        self.vectors = vectors
        self.fail = fail
        self.batch_calls = 0
        self.text_calls = []

    def encode(self, texts, normalize_embeddings=True):
        assert normalize_embeddings is True
        if self.fail:
            raise RuntimeError("fake embedder failure")

        if isinstance(texts, str):
            self.text_calls.append(texts)
            return np.asarray(self.vectors[texts], dtype=np.float32)

        batch = list(texts)
        self.batch_calls += 1
        return np.asarray([self.vectors[text] for text in batch], dtype=np.float32)


def _write_persona(path, responses):
    path.write_text(json.dumps({"responses": responses}), encoding="utf-8")


# ── Goal loading ─────────────────────────────────────────────────────────────


def test_load_goals_skips_empty_answers_and_normalizes_text(tmp_path):
    path = tmp_path / "persona.json"
    _write_persona(
        path,
        [
            {"answer": "  Python  ", "impact": 2},
            {"answer": "   ", "impact": 3},
            {"answer": "", "impact": 1},
            {"answer": "Machine Learning", "impact": 3},
            {"answer": "Pythn", "impact": 0},
            {"answer": "Python", "impact": 9},
            {"answer": None, "impact": 2},
            {"answer": "No impact"},
        ],
    )

    assert importance.load_goals(path) == [
        ("python", IMPACT_WEIGHTS[2]),
        ("machine learning", IMPACT_WEIGHTS[3]),
        ("pythn", IMPACT_WEIGHTS[1]),
        ("python", IMPACT_WEIGHTS[3]),
        ("no impact", IMPACT_WEIGHTS[1]),
    ]


def test_missing_persona_returns_no_goals(tmp_path):
    assert importance.load_goals(tmp_path / "missing.json") == []


def test_malformed_persona_returns_no_goals(tmp_path):
    path = tmp_path / "persona.json"
    path.write_text("{not json", encoding="utf-8")

    assert importance.load_goals(path) == []


# ── Suggestions ──────────────────────────────────────────────────────────────


def test_no_goals_returns_default_without_encoding(tmp_path, monkeypatch):
    monkeypatch.setattr(importance, "DEFAULT_PERSONA_PATH", tmp_path / "missing.json")
    model = FakeModel({})

    assert importance.suggest_importance("task", model=model) == (
        DEFAULT_IMPORTANCE,
        None,
        0.0,
    )
    assert model.batch_calls == 0
    assert model.text_calls == []


def test_empty_goals_list_returns_default():
    model = FakeModel({})

    assert importance.suggest_importance("task", model=model, goals=[]) == (
        DEFAULT_IMPORTANCE,
        None,
        0.0,
    )


def test_impact_weighting_changes_the_argmax():
    model = FakeModel(
        {
            "alpha": [1.0, 0.0],
            "beta": [0.0, 1.0],
            "task": [0.9, 0.6],
        }
    )
    goals = [
        ("alpha", IMPACT_WEIGHTS[1]),
        ("beta", IMPACT_WEIGHTS[3]),
    ]

    level, matched, relevance = importance.suggest_importance(
        "  TASK  ", model=model, goals=goals
    )

    # alpha has the higher raw similarity (0.9) but beta is impact 3.
    assert matched == "beta"
    assert relevance == pytest.approx(0.6)
    assert level == 5
    assert model.text_calls == ["task"]


@pytest.mark.parametrize(
    ("relevance", "expected"),
    [
        (0.0, 1),
        (0.0999, 1),
        (0.10, 2),
        (0.1999, 2),
        (0.20, 3),
        (0.2999, 3),
        (0.30, 4),
        (0.3999, 4),
        (0.40, 5),
        (0.90, 5),
    ],
)
def test_importance_band_boundaries(relevance, expected):
    goal_vector = np.array([1.0, 0.0], dtype=np.float32)
    title_vector = np.array(
        [relevance, np.sqrt(max(0.0, 1.0 - relevance * relevance))],
        dtype=np.float32,
    )
    model = FakeModel({"goal": goal_vector, "task": title_vector})

    level, matched, got = importance.suggest_importance(
        "task", model=model, goals=[("goal", 1.0)]
    )

    assert matched == "goal"
    assert got == pytest.approx(relevance, abs=1e-6)
    assert level == expected


def test_exception_returns_default_and_logs_warning(caplog):
    model = FakeModel({}, fail=True)

    with caplog.at_level("WARNING", logger="system_sl.core.importance"):
        level, matched, relevance = importance.suggest_importance(
            "task", model=model, goals=[("goal", 1.0)]
        )

    assert (level, matched, relevance) == (DEFAULT_IMPORTANCE, None, 0.0)
    assert "Could not suggest importance" in caplog.text


# ── Caching ──────────────────────────────────────────────────────────────────


def test_goal_vectors_are_cached_and_rebuilt_when_mtime_changes(
    tmp_path, monkeypatch
):
    persona = tmp_path / "persona.json"
    _write_persona(persona, [{"answer": "alpha", "impact": 3}])
    monkeypatch.setattr(importance, "DEFAULT_PERSONA_PATH", persona)

    model = FakeModel(
        {
            "alpha": [1.0, 0.0],
            "beta": [0.0, 1.0],
            "task": [1.0, 0.0],
        }
    )

    assert importance.suggest_importance("task", model=model)[1] == "alpha"
    assert model.batch_calls == 1

    # Same persona mtime: the goal vectors come from the process cache.
    assert importance.suggest_importance("task", model=model)[1] == "alpha"
    assert model.batch_calls == 1

    _write_persona(persona, [{"answer": "beta", "impact": 3}])
    stat = persona.stat()
    os.utime(persona, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    assert importance.suggest_importance("task", model=model)[1] == "beta"
    assert model.batch_calls == 2