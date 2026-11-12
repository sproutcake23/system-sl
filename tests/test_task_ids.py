"""Stable task ids, title migration and category-preserving edits."""

import json

import system_sl.core.tasks as tasks


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_FILE_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(tasks, "TASK_ORDER_FILE_PATH", str(tmp_path / "task_order.json"))
    monkeypatch.setattr(
        tasks, "COMPLETED_TASKS_FILE_PATH", str(tmp_path / "completed_tasks.json")
    )
    monkeypatch.setattr(tasks, "TASKS_LOCK_PATH", str(tmp_path / "tasks.lock"))


def test_missing_ids_are_assigned_and_html_escapes_are_undone(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.save_tasks([{"title": "AT&amp;T", "category": "study"}])

    loaded = tasks.load_tasks()
    assert loaded[0]["title"] == "AT&T"
    assert loaded[0]["id"]
    # The migration is persisted, not just returned.
    on_disk = json.loads((tmp_path / "tasks.json").read_text())
    assert on_disk[0]["id"] == loaded[0]["id"]


def test_edit_and_remove_key_on_the_stable_id(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.add_tasks("study", "AT&T")
    task = tasks.load_tasks()[0]
    task_id = task["id"]

    tasks.edit_task(
        task_id=task_id, old_title="AT&T", new_title="AT&T — review",
        new_deadline="2026-12-01",
    )
    updated = tasks.load_tasks()[0]
    assert updated["title"] == "AT&T — review"
    assert updated["category"] == "study"  # new_category=None means unchanged

    assert tasks.remove_tasks(task_id=task_id) == "AT&T — review"
    assert tasks.load_tasks() == []


def test_completion_logs_the_real_category(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.add_tasks("study", "Physics revision")
    task_id = tasks.load_tasks()[0]["id"]

    tasks.mark_task_completed(task_id=task_id)

    completed = tasks.load_completed_tasks()
    assert completed[0]["title"] == "Physics revision"
    assert completed[0]["category"] == "study"


def test_manual_order_uses_ids_and_notifications_resolve_titles(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.add_tasks("study", "First")
    tasks.add_tasks("study", "Second")
    first_id, second_id = [t["id"] for t in tasks.load_tasks()]

    tasks.save_manual_order([{"id": second_id}, {"id": first_id}])
    stored = json.loads((tmp_path / "task_order.json").read_text())
    assert stored["order"] == [second_id, first_id]
    assert tasks.get_topn_task() == ["Second", "First"]


def test_google_sync_deduplicates_by_source_id(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.add_tasks("task", "Remote task", source_id="google-1")
    tasks.add_tasks("task", "Remote task renamed", source_id="google-1", deadline="2026-12-01")

    loaded = tasks.load_tasks()
    assert len(loaded) == 1
    assert loaded[0]["source_id"] == "google-1"
    assert loaded[0]["deadline"] == "2026-12-01"