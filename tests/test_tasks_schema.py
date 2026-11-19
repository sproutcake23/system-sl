"""Optional importance fields on stored tasks: defaults, migration and edits."""

import json

import pytest

import system_sl.core.tasks as tasks
from system_sl.core.ranking_config import DEFAULT_IMPORTANCE


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_FILE_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(tasks, "TASK_ORDER_FILE_PATH", str(tmp_path / "task_order.json"))
    monkeypatch.setattr(
        tasks, "COMPLETED_TASKS_FILE_PATH", str(tmp_path / "completed_tasks.json")
    )
    monkeypatch.setattr(tasks, "TASKS_LOCK_PATH", str(tmp_path / "tasks.lock"))


def _stored_task(tmp_path):
    return json.loads((tmp_path / "tasks.json").read_text())[0]


# ── add_tasks ────────────────────────────────────────────────────────────────


def test_add_without_priority_fields_stores_defaults(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    tasks.add_tasks("study", "Read a paper")

    task = tasks.load_tasks()[0]
    assert task["importance"] == DEFAULT_IMPORTANCE
    assert task["importance_source"] == "default"
    assert task["matched_goal"] is None
    assert task["effort_minutes"] is None


def test_add_with_priority_fields_stores_them(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    tasks.add_tasks(
        "study",
        "Read a paper",
        importance=4,
        importance_source="suggested",
        matched_goal="publish papers",
        effort_minutes=45,
    )

    task = tasks.load_tasks()[0]
    assert task["importance"] == 4
    assert task["importance_source"] == "suggested"
    assert task["matched_goal"] == "publish papers"
    assert task["effort_minutes"] == 45


def test_add_with_importance_but_no_source_uses_user(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    tasks.add_tasks("study", "Read a paper", importance=2)

    task = tasks.load_tasks()[0]
    assert task["importance"] == 2
    assert task["importance_source"] == "user"


# ── edit_task ────────────────────────────────────────────────────────────────


def test_edit_each_priority_field(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.add_tasks("study", "Read a paper")
    task_id = tasks.load_tasks()[0]["id"]

    tasks.edit_task(
        None,
        "Read a paper",
        task_id=task_id,
        importance=5,
        importance_source="suggested",
        matched_goal="publish papers",
        effort_minutes=30,
    )

    task = tasks.load_tasks()[0]
    assert task["importance"] == 5
    assert task["importance_source"] == "suggested"
    assert task["matched_goal"] == "publish papers"
    assert task["effort_minutes"] == 30


def test_edit_none_leaves_priority_fields_unchanged(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.add_tasks(
        "study",
        "Read a paper",
        importance=5,
        importance_source="suggested",
        matched_goal="publish papers",
        effort_minutes=30,
    )
    task_id = tasks.load_tasks()[0]["id"]

    tasks.edit_task(None, "Read a paper (renamed)", task_id=task_id)

    task = tasks.load_tasks()[0]
    assert task["importance"] == 5
    assert task["importance_source"] == "suggested"
    assert task["matched_goal"] == "publish papers"
    assert task["effort_minutes"] == 30


def test_edit_importance_alone_leaves_source_and_goal_unchanged(
    tmp_path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    tasks.add_tasks(
        "study",
        "Read a paper",
        importance=3,
        importance_source="suggested",
        matched_goal="publish papers",
    )
    task_id = tasks.load_tasks()[0]["id"]

    tasks.edit_task(None, "Read a paper", task_id=task_id, importance=4)

    task = tasks.load_tasks()[0]
    assert task["importance"] == 4
    assert task["importance_source"] == "suggested"
    assert task["matched_goal"] == "publish papers"


def test_matched_goal_clears_when_source_moves_away_from_suggested(
    tmp_path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    tasks.add_tasks(
        "study",
        "Read a paper",
        importance=3,
        importance_source="suggested",
        matched_goal="publish papers",
    )
    task_id = tasks.load_tasks()[0]["id"]

    tasks.edit_task(None, "Read a paper", task_id=task_id, importance_source="default")

    task = tasks.load_tasks()[0]
    assert task["importance_source"] == "default"
    assert task["matched_goal"] is None


def test_clear_effort_wins_over_supplied_value(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.add_tasks("study", "Read a paper", effort_minutes=30)
    task_id = tasks.load_tasks()[0]["id"]

    tasks.edit_task(
        None,
        "Read a paper",
        task_id=task_id,
        effort_minutes=60,
        clear_effort=True,
    )

    assert tasks.load_tasks()[0]["effort_minutes"] is None


# ── migration ────────────────────────────────────────────────────────────────


def test_list_format_migration_adds_defaults_and_preserves_source_id(
    tmp_path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    old_task = {
        "id": "fixed-id",
        "title": "Old task",
        "created_at": "2026-01-01T00:00:00",
        "deadline": None,
        "category": "study",
        "source_id": "google-42",
    }
    tasks.save_tasks([old_task])

    loaded = tasks.load_tasks()

    assert loaded[0] == {
        **old_task,
        "importance": DEFAULT_IMPORTANCE,
        "importance_source": "default",
        "matched_goal": None,
        "effort_minutes": None,
    }
    assert _stored_task(tmp_path)["source_id"] == "google-42"


def test_legacy_dict_format_is_flat_list_with_defaults(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "tasks.json").write_text(
        json.dumps(
            {
                "study": [
                    {"title": "Old dict task", "deadline": None, "category": "study"}
                ]
            }
        )
    )

    loaded = tasks.load_tasks()

    assert isinstance(loaded, list)
    assert loaded[0]["title"] == "Old dict task"
    assert loaded[0]["importance"] == DEFAULT_IMPORTANCE
    assert loaded[0]["importance_source"] == "default"
    assert loaded[0]["matched_goal"] is None
    assert loaded[0]["effort_minutes"] is None

    stored = json.loads((tmp_path / "tasks.json").read_text())
    assert isinstance(stored, list)
    assert stored[0]["importance_source"] == "default"


def test_migration_is_idempotent_and_does_not_rewrite_unchanged_file(
    tmp_path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    tasks.save_tasks(
        [{"id": "fixed-id", "title": "Old task", "category": "study"}]
    )
    first = tasks.load_tasks()
    first_on_disk = (tmp_path / "tasks.json").read_text()

    # A second read has nothing to migrate, so save_tasks must not run.
    def fail_save(_tasks):
        raise AssertionError("load_tasks rewrote an already-migrated file")

    monkeypatch.setattr(tasks, "save_tasks", fail_save)

    assert tasks.load_tasks() == first
    assert (tmp_path / "tasks.json").read_text() == first_on_disk


def test_fully_migrated_file_is_read_without_writing(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.save_tasks(
        [
            {
                "id": "fixed-id",
                "title": "Old task",
                "category": "study",
                "importance": 3,
                "importance_source": "default",
                "matched_goal": None,
                "effort_minutes": None,
            }
        ]
    )

    def fail_save(_tasks):
        raise AssertionError("load_tasks rewrote a fully migrated file")

    monkeypatch.setattr(tasks, "save_tasks", fail_save)

    assert tasks.load_tasks()[0]["title"] == "Old task"


def test_schema_only_migration_does_not_write_completed_tasks(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.save_tasks(
        [{"id": "fixed-id", "title": "Old task", "category": "study"}]
    )

    tasks.load_tasks()

    assert not (tmp_path / "completed_tasks.json").exists()


# ── validation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("importance", [0, -3, 1, 5, 6, 99])
def test_importance_integers_are_clamped(tmp_path, monkeypatch, importance):
    _isolate(tmp_path, monkeypatch)

    tasks.add_tasks("study", "Read a paper", importance=importance)

    assert tasks.load_tasks()[0]["importance"] == max(1, min(5, importance))


@pytest.mark.parametrize("importance", ["3", 3.0, 2.5, True, False])
def test_non_integer_importance_is_rejected(tmp_path, monkeypatch, importance):
    _isolate(tmp_path, monkeypatch)

    with pytest.raises(ValueError):
        tasks.add_tasks("study", "Read a paper", importance=importance)


@pytest.mark.parametrize("effort", [0, -1, "30", 1.5, True, False])
def test_invalid_effort_is_rejected(tmp_path, monkeypatch, effort):
    _isolate(tmp_path, monkeypatch)

    with pytest.raises(ValueError):
        tasks.add_tasks("study", "Read a paper", effort_minutes=effort)


def test_invalid_importance_source_is_rejected(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    with pytest.raises(ValueError):
        tasks.add_tasks(
            "study", "Read a paper", importance=3, importance_source="guess"
        )


def test_invalid_matched_goal_is_rejected(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    with pytest.raises(ValueError):
        tasks.add_tasks("study", "Read a paper", importance=3, matched_goal=5)


def test_edit_rejects_invalid_values_without_changing_the_task(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.add_tasks("study", "Read a paper")
    task_id = tasks.load_tasks()[0]["id"]

    with pytest.raises(ValueError):
        tasks.edit_task(None, "Read a paper", task_id=task_id, importance="4")
    with pytest.raises(ValueError):
        tasks.edit_task(None, "Read a paper", task_id=task_id, effort_minutes=0)
    with pytest.raises(ValueError):
        tasks.edit_task(
            None, "Read a paper", task_id=task_id, importance_source="guess"
        )

    task = tasks.load_tasks()[0]
    assert task["importance"] == DEFAULT_IMPORTANCE
    assert task["importance_source"] == "default"
    assert task["effort_minutes"] is None