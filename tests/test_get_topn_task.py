"""get_topn_task must be a pure read: it may not create the order file."""

import json

import system_sl.core.tasks as tasks


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_FILE_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(tasks, "TASK_ORDER_FILE_PATH", str(tmp_path / "task_order.json"))
    monkeypatch.setattr(tasks, "TASKS_LOCK_PATH", str(tmp_path / "tasks.lock"))


def test_missing_order_file_is_not_created(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.save_tasks(
        [
            {"title": "First", "category": "general"},
            {"title": "Second", "category": "general"},
            {"title": "Third", "category": "general"},
            {"title": "Fourth", "category": "general"},
        ]
    )
    order_path = tmp_path / "task_order.json"

    assert not order_path.exists()
    assert tasks.get_topn_task() == ["First", "Second", "Third"]
    assert not order_path.exists()


def test_existing_order_is_used_and_ghosts_are_filtered(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.save_tasks(
        [
            {"title": "First", "category": "general"},
            {"title": "Second", "category": "general"},
            {"title": "Third", "category": "general"},
        ]
    )
    order_path = tmp_path / "task_order.json"
    order_path.write_text(
        json.dumps({"order": ["Third", "Ghost", "First"]}), encoding="utf-8"
    )

    assert tasks.get_topn_task() == ["Third", "First"]
