"""Wiring tests for the deadline-first ranking and its chat consumers."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

import system_sl.chatbot.dynamic_context as dynamic_context
import system_sl.core.priority_engine_new as engine
import system_sl.core.tasks as tasks
import system_sl.utils as utils
from system_sl.core.importance import load_goals, suggest_importance


EXPECTED_TASK_FIELDS = {
    "id",
    "title",
    "category",
    "deadline",
    "created_at",
    "importance",
    "importance_source",
    "matched_goal",
    "effort_minutes",
    "urgency",
    "quick_win",
    "score",
    "quadrant",
    "reason",
}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_FILE_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(tasks, "TASK_ORDER_FILE_PATH", str(tmp_path / "task_order.json"))
    monkeypatch.setattr(
        tasks, "COMPLETED_TASKS_FILE_PATH", str(tmp_path / "completed_tasks.json")
    )
    monkeypatch.setattr(tasks, "TASKS_LOCK_PATH", str(tmp_path / "tasks.lock"))
    monkeypatch.setattr(
        engine, "PRIORITIZE_FILE_PATH", str(tmp_path / "prioritize.json")
    )
    # dynamic_context resolves its own paths through this helper.
    monkeypatch.setattr(
        utils, "get_tasks_file_path", lambda name: str(tmp_path / name)
    )
    return tmp_path


class FakeEmbedder:
    """Deterministic vectors, so no MiniLM download is needed."""

    def __init__(self, vectors):
        self.vectors = vectors
        self.batch_calls = 0

    def encode(self, texts, normalize_embeddings=True):
        assert normalize_embeddings is True
        if isinstance(texts, str):
            return np.asarray(self.vectors[texts], dtype=np.float32)
        self.batch_calls += 1
        return np.asarray([self.vectors[text] for text in texts], dtype=np.float32)


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _in_days(days):
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


def _seed_six_tasks():
    tasks.add_tasks(
        "study", "Q1 soon", deadline=_today(), importance=4, importance_source="user"
    )
    tasks.add_tasks(
        "study", "Q1 later", deadline=_in_days(1), importance=5, importance_source="user"
    )
    tasks.add_tasks("study", "Q2 alpha", importance=4, importance_source="user")
    tasks.add_tasks("study", "Q2 beta", importance=5, importance_source="user")
    tasks.add_tasks(
        "study", "Q3 quick", deadline=_today(), importance=2, importance_source="user"
    )
    tasks.add_tasks("study", "Q4 someday", importance=2, importance_source="user")
    return {task["title"]: task["id"] for task in tasks.load_tasks()}


def _no_persona(monkeypatch):
    monkeypatch.setattr(engine, "load_goals", lambda: [])


# ── ranking payload ──────────────────────────────────────────────────────────


def test_ranking_six_tasks_across_all_quadrants(isolated, monkeypatch):
    _no_persona(monkeypatch)
    _seed_six_tasks()

    result = engine.run_prioritization(display=False)

    assert result["task_count"] == 6
    assert result["quadrant_counts"] == {"Q1": 2, "Q2": 2, "Q3": 1, "Q4": 1}
    assert set(result) == {
        "generated_at",
        "task_count",
        "quadrant_counts",
        "quadrants",
        "all_tasks_ranked",
    }
    assert "_reason" not in result

    for quadrant, data in result["quadrants"].items():
        assert set(data) == {"name", "icon", "subtitle", "tasks"}
        assert "sub" not in data
        assert all(task["quadrant"] == quadrant for task in data["tasks"])

    ranked = result["all_tasks_ranked"]
    assert len(ranked) == 6
    assert set(ranked[0]) == EXPECTED_TASK_FIELDS
    scores = [task["score"] for task in ranked]
    assert scores == sorted(scores, reverse=True)
    # Raw 1..5 importance survives, rather than scoring's normalized value.
    assert {task["importance"] for task in ranked} == {2, 4, 5}

    for old_key in (
        "context",
        "context_description",
        "bandit_confidence",
        "bandit_weights",
        "P_final",
        "pull_normalized",
    ):
        assert old_key not in result
        assert all(old_key not in task for task in ranked)

    on_disk = json.loads(Path(engine.PRIORITIZE_FILE_PATH).read_text())
    assert on_disk == result


def test_manual_order_only_reorders_within_its_quadrant(isolated, monkeypatch):
    _no_persona(monkeypatch)
    ids = _seed_six_tasks()
    # Only the lower-scoring Q1 task was dragged: it still comes first, and
    # the other Q1 task follows by score.
    tasks.save_manual_order([{"id": ids["Q1 later"]}])

    result = engine.run_prioritization(display=False)

    q1_titles = [task["title"] for task in result["quadrants"]["Q1"]["tasks"]]
    assert q1_titles == ["Q1 later", "Q1 soon"]

    # The global score list is not reordered by the drag order.
    global_titles = [task["title"] for task in result["all_tasks_ranked"]]
    assert global_titles.index("Q1 soon") < global_titles.index("Q1 later")

    # Tasks in the other quadrants stay where their scores put them.
    assert [t["title"] for t in result["quadrants"]["Q2"]["tasks"]] == [
        "Q2 beta",
        "Q2 alpha",
    ]
    assert [t["title"] for t in result["quadrants"]["Q3"]["tasks"]] == ["Q3 quick"]
    assert [t["title"] for t in result["quadrants"]["Q4"]["tasks"]] == ["Q4 someday"]


def test_ranking_failure_writes_degraded_payload(isolated, monkeypatch):
    tasks.add_tasks("study", "Any task")
    monkeypatch.setattr(engine, "ensure_importance_suggestions", lambda _tasks: False)

    def boom(*args, **kwargs):
        raise RuntimeError("scoring exploded")

    monkeypatch.setattr(engine.scoring, "rank_tasks", boom)

    result = engine.run_prioritization(display=False)

    assert result["task_count"] == 0
    assert "scoring exploded" in result["_reason"]
    assert set(result["quadrants"]) == {"Q1", "Q2", "Q3", "Q4"}
    assert all("subtitle" in data and "sub" not in data for data in result["quadrants"].values())
    assert result["all_tasks_ranked"] == []
    for old_key in ("context", "bandit_confidence", "bandit_weights"):
        assert old_key not in result

    on_disk = json.loads(Path(engine.PRIORITIZE_FILE_PATH).read_text())
    assert on_disk == result


def test_empty_ranking_has_the_same_shape(isolated):
    payload = engine.empty_ranking("nothing to do")

    assert set(payload) == {
        "generated_at",
        "task_count",
        "quadrant_counts",
        "quadrants",
        "all_tasks_ranked",
        "_reason",
    }
    assert payload["task_count"] == 0
    assert payload["quadrant_counts"] == {"Q1": 0, "Q2": 0, "Q3": 0, "Q4": 0}
    assert payload["_reason"] == "nothing to do"


# ── suggestion filling ───────────────────────────────────────────────────────


def test_ensure_importance_suggestions_leaves_user_tasks_untouched(
    isolated, monkeypatch
):
    writes = []
    monkeypatch.setattr(tasks, "save_tasks", lambda rows: writes.append(rows))

    rows = [
        {
            "title": "alpha task",
            "importance": 3,
            "importance_source": "default",
            "matched_goal": None,
        },
        {
            "title": "user task",
            "importance": 5,
            "importance_source": "user",
            "matched_goal": None,
        },
        {
            "title": "suggested task",
            "importance": 2,
            "importance_source": "suggested",
            "matched_goal": "beta",
        },
    ]
    model = FakeEmbedder(
        {
            "alpha": [1.0, 0.0],
            "beta": [0.0, 1.0],
            "alpha task": [1.0, 0.0],
        }
    )

    changed = engine.ensure_importance_suggestions(
        rows, model=model, goals=[("alpha", 1.0), ("beta", 1.0)]
    )

    assert changed is True
    assert rows[0]["importance"] == 5
    assert rows[0]["importance_source"] == "suggested"
    assert rows[0]["matched_goal"] == "alpha"
    assert rows[1] == {
        "title": "user task",
        "importance": 5,
        "importance_source": "user",
        "matched_goal": None,
    }
    assert rows[2] == {
        "title": "suggested task",
        "importance": 2,
        "importance_source": "suggested",
        "matched_goal": "beta",
    }
    assert len(writes) == 1


def test_ensure_importance_suggestions_is_a_noop_without_goals(
    isolated, monkeypatch
):
    writes = []
    monkeypatch.setattr(tasks, "save_tasks", lambda rows: writes.append(rows))
    rows = [{"title": "task", "importance": 3, "importance_source": "default"}]

    assert (
        engine.ensure_importance_suggestions(rows, model=FakeEmbedder({}), goals=[])
        is False
    )
    assert rows[0]["importance_source"] == "default"
    assert writes == []


# ── query functions ──────────────────────────────────────────────────────────

def _snapshot_payload(tasks_list):
    return {
        "generated_at": datetime.now().isoformat(),
        "task_count": len(tasks_list),
        "quadrant_counts": {"Q1": 1, "Q2": 0, "Q3": 0, "Q4": 0},
        "quadrants": {
            "Q1": {"name": "DO NOW", "icon": "⚡", "subtitle": "x", "tasks": tasks_list},
            "Q2": {"name": "SCHEDULE", "icon": "📅", "subtitle": "x", "tasks": []},
            "Q3": {"name": "DEFER", "icon": "⏩", "subtitle": "x", "tasks": []},
            "Q4": {"name": "CONSIDER DROPPING", "icon": "🗑", "subtitle": "x", "tasks": []},
        },
        "all_tasks_ranked": tasks_list,
    }


def test_query_functions_read_the_snapshot(isolated):
    snapshot = _snapshot_payload(
        [
            {
                "id": "id-1",
                "title": "Read a paper",
                "quadrant": "Q1",
                "reason": "due today, importance 4 (set by you)",
            },
            {
                "id": "id-2",
                "title": "Organize files",
                "quadrant": "Q4",
                "reason": "no deadline, importance 2 (set by you)",
            },
        ]
    )
    Path(engine.PRIORITIZE_FILE_PATH).write_text(json.dumps(snapshot))

    assert engine.load_latest_prioritization() == snapshot
    assert engine.get_quadrant("id-1") == "Q1"
    assert engine.get_quadrant("Organize files") == "Q4"
    assert engine.get_quadrant("missing") is None

    summary = engine.get_task_summary_for_chat(top_n=1)
    assert summary == (
        "1. [Q1 DO NOW] Read a paper | due today, importance 4 (set by you)"
    )


def test_query_functions_degrade_on_missing_or_corrupt_file(isolated):
    Path(engine.PRIORITIZE_FILE_PATH).unlink(missing_ok=True)

    missing = engine.load_latest_prioritization()
    assert missing["task_count"] == 0
    assert missing["_reason"]
    assert engine.get_quadrant("anything") is None
    assert "No ranked tasks" in engine.get_task_summary_for_chat()

    Path(engine.PRIORITIZE_FILE_PATH).write_text("{not json")
    corrupt = engine.load_latest_prioritization()
    assert corrupt["task_count"] == 0
    assert corrupt["_reason"]
    assert engine.get_quadrant("anything") is None


# ── chat context keys ────────────────────────────────────────────────────────


def test_renamed_task_still_matches_the_ranked_snapshot(isolated):
    tasks.add_tasks("study", "Renamed title", importance=4, importance_source="user")
    task_id = tasks.load_tasks()[0]["id"]
    snapshot = _snapshot_payload(
        [
            {
                "id": task_id,
                "title": "Old title",
                "score": 0.75,
                "quadrant": "Q1",
                "reason": "due today",
            }
        ]
    )
    Path(engine.PRIORITIZE_FILE_PATH).write_text(json.dumps(snapshot))

    priorities = dynamic_context._priority_index()
    assert task_id in priorities
    assert "Old title" not in priorities

    top = dynamic_context.load_top_tasks(top_n=1)
    assert top[0]["title"] == "Renamed title"
    assert dynamic_context._task_priority(top[0], priorities) == "high"


def test_old_snapshot_without_ids_falls_back_to_title(isolated):
    tasks.add_tasks("study", "Renamed title", importance=4, importance_source="user")
    snapshot = _snapshot_payload(
        [
            {
                "title": "Renamed title",
                "score": 0.5,
                "quadrant": "Q4",
                "reason": "no deadline",
            }
        ]
    )
    Path(engine.PRIORITIZE_FILE_PATH).write_text(json.dumps(snapshot))

    priorities = dynamic_context._priority_index()
    assert "Renamed title" in priorities

    top = dynamic_context.load_top_tasks(top_n=1)
    assert top[0]["title"] == "Renamed title"
    assert dynamic_context._task_priority(top[0], priorities) == "low"


# Keep the imported production helpers honest without calling the real model.
def test_helpers_are_importable():
    assert load_goals is engine.load_goals
    assert suggest_importance is engine.suggest_importance