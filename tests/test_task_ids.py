"""Stable task ids, title migration and category-preserving edits."""

import json

import pytest

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
    # A re-sync updates the deadline but reports a duplicate, so the sync
    # counts it as skipped instead of "imported" on every run.
    with pytest.raises(ValueError):
        tasks.add_tasks("task", "Remote task renamed", source_id="google-1", deadline="2026-12-01")

    loaded = tasks.load_tasks()
    assert len(loaded) == 1
    assert loaded[0]["source_id"] == "google-1"
    assert loaded[0]["deadline"] == "2026-12-01"


def test_title_unescape_runs_once(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.save_tasks([{"title": "AT&amp;amp;T", "category": "study"}])
    assert tasks.load_tasks()[0]["title"] == "AT&amp;T"
    # Now a literal title with an id: further loads must not touch it.
    assert tasks.load_tasks()[0]["title"] == "AT&amp;T"
    tasks.add_tasks("study", "Style &lt;div&gt;")
    assert [t["title"] for t in tasks.load_tasks()] == ["AT&amp;T", "Style &lt;div&gt;"]


def test_migration_carries_titles_into_order_and_completed_files(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "task_order.json").write_text(json.dumps({"order": ["AT&amp;T"]}))
    (tmp_path / "completed_tasks.json").write_text(
        json.dumps([{"title": "Q&amp;A", "category": "study", "completed_at": "2026-01-01"}])
    )
    tasks.save_tasks([{"title": "AT&amp;T", "category": "study"}])

    tasks.load_tasks()
    order = json.loads((tmp_path / "task_order.json").read_text())["order"]
    assert order == ["AT&T"]
    assert tasks.load_completed_tasks()[0]["title"] == "Q&A"
    # A second migration pass (a new id-less task) leaves them alone.
    current = tasks.load_tasks()
    current.append({"title": "Hand added"})
    tasks.save_tasks(current)
    tasks.load_tasks()
    assert tasks.load_completed_tasks()[0]["title"] == "Q&A"


def test_resync_backfills_source_id_on_old_synced_task(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.add_tasks("task", "Remote task")  # synced before source ids existed
    with pytest.raises(ValueError):
        tasks.add_tasks("task", "Remote task", source_id="google-1")
    assert tasks.load_tasks()[0]["source_id"] == "google-1"


def test_edit_keeps_or_clears_deadline(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.add_tasks("study", "Synced", deadline="2026-12-01 09:30")
    task_id = tasks.load_tasks()[0]["id"]

    tasks.edit_task(None, "Synced (typo fixed)", task_id=task_id)
    assert tasks.load_tasks()[0]["deadline"] == "2026-12-01 09:30"

    tasks.edit_task(None, "Synced (typo fixed)", new_deadline="", task_id=task_id)
    assert tasks.load_tasks()[0]["deadline"] is None


def test_chat_tools_refuse_ambiguous_duplicate_titles(tmp_path, monkeypatch):
    from system_sl.chatbot import system as system_mod

    _isolate(tmp_path, monkeypatch)
    tasks.save_tasks([
        {"id": "a", "title": "Read", "category": "study"},
        {"id": "b", "title": "Read", "category": "fun"},
    ])
    monkeypatch.setattr(system_mod, "load_tasks", tasks.load_tasks)
    assert system_mod._resolve_task_id("Read", "fun") == "b"
    with pytest.raises(ValueError):
        system_mod._resolve_task_id("Read", "work")
