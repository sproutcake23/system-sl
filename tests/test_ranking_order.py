"""The drag order and the ranking's own output are kept apart.

The ranking used to save its result as the order it read back next time, so
the first ranking stuck forever. Only an order the player dragged may steer it.
"""

import json

import system_sl.core.tasks as tasks
from system_sl.core.priority_engine_new import apply_manual_order


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_FILE_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(tasks, "TASK_ORDER_FILE_PATH", str(tmp_path / "task_order.json"))
    monkeypatch.setattr(
        tasks, "COMPLETED_TASKS_FILE_PATH", str(tmp_path / "completed_tasks.json")
    )
    monkeypatch.setattr(tasks, "TASKS_LOCK_PATH", str(tmp_path / "tasks.lock"))


def _t(task_id):
    return {"id": task_id, "title": task_id.upper()}


def test_without_drag_order_the_score_order_is_kept():
    ranked = [_t("a"), _t("b"), _t("c")]
    assert apply_manual_order(ranked, []) == ranked


def test_dragged_tasks_keep_their_order_and_new_ones_go_in_by_score():
    # Score order: n (new, most urgent), a, b, m (new, least urgent).
    ranked = [_t("n"), _t("a"), _t("b"), _t("m")]
    # The player dragged b above a.
    result = apply_manual_order(ranked, ["b", "a"])
    assert [t["id"] for t in result] == ["n", "b", "a", "m"]


def test_legacy_title_entries_still_match():
    ranked = [_t("a"), _t("b")]
    result = apply_manual_order(ranked, ["B", "A"])
    assert [t["id"] for t in result] == ["b", "a"]


def test_ranked_output_does_not_become_the_drag_order(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.save_ranked_order([_t("a"), _t("b")])
    assert tasks.load_manual_order() == []
    assert tasks.load_display_order() == ["a", "b"]

    # A later ranking with a different result replaces the shown order.
    tasks.save_ranked_order([_t("b"), _t("a")])
    assert tasks.load_display_order() == ["b", "a"]
    assert tasks.load_manual_order() == []


def test_ranking_keeps_an_existing_drag_order(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.save_manual_order([_t("b"), _t("a")])
    tasks.save_ranked_order([_t("c"), _t("b"), _t("a")])
    stored = json.loads((tmp_path / "task_order.json").read_text())
    assert stored == {"order": ["c", "b", "a"], "manual": ["b", "a"]}


def test_old_order_files_are_not_treated_as_a_drag_order(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "task_order.json").write_text(json.dumps({"order": ["a", "b"]}))
    assert tasks.load_manual_order() == []
    assert tasks.load_display_order() == ["a", "b"]


def test_removing_a_task_drops_it_from_both_lists(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks.add_tasks("study", "First")
    tasks.add_tasks("study", "Second")
    first, second = tasks.load_tasks()
    tasks.save_manual_order([second, first])

    tasks.remove_tasks(task_id=first["id"])

    stored = json.loads((tmp_path / "task_order.json").read_text())
    assert stored == {"order": [second["id"]], "manual": [second["id"]]}

