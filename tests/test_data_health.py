"""Data-health reporting: honest backups, unique names, wording, GUI drain."""

from datetime import datetime
from pathlib import Path

import pytest

from system_sl.utils import atomic_json
from system_sl.utils.atomic_json import LoadStatus, corrupt_backup_path
from system_sl.utils.data_health import (
    DataIssue,
    combine_messages,
    has_issues,
    pop_issues,
    record_issue,
)
from system_sl.utils.json_client import load_data_result


@pytest.fixture(autouse=True)
def _clean_queue():
    pop_issues()
    yield
    pop_issues()


def _corrupt(path: Path) -> None:
    path.write_text("{not json", encoding="utf-8")


# --- 1a: a failed move must not report a backup ------------------------------


def test_failed_move_reports_no_backup(tmp_path, monkeypatch, caplog):
    path = tmp_path / "tasks.json"
    _corrupt(path)

    def fail_move(src, dst):
        raise PermissionError("read-only directory")

    monkeypatch.setattr(atomic_json.shutil, "move", fail_move)
    with caplog.at_level("WARNING"):
        result = load_data_result(str(path))

    assert result.status is LoadStatus.CORRUPT
    assert result.corrupt_path is None
    assert path.exists(), "original must stay in place"
    assert list(tmp_path.glob("*.corrupt*")) == []
    assert "read-only directory" in caplog.text

    [issue] = pop_issues()
    assert issue.kind == "corrupt"
    assert issue.corrupt_path is None
    text = issue.message()
    assert "moved aside to" not in text
    assert "could not be moved aside" in text
    assert "read-only directory" in text
    assert issue.title() == "Task data damaged"


# --- 1b: every quarantine gets its own backup --------------------------------


def test_repeat_corruption_keeps_both_backups(tmp_path):
    path = tmp_path / "tasks.json"
    _corrupt(path)
    first = load_data_result(str(path)).corrupt_path
    path.write_text("{second broken", encoding="utf-8")
    second = load_data_result(str(path)).corrupt_path

    assert first is not None and second is not None
    assert first != second
    assert first.read_text(encoding="utf-8") == "{not json"
    assert second.read_text(encoding="utf-8") == "{second broken"


def test_backup_name_is_timestamped_and_unique(tmp_path):
    path = tmp_path / "tasks.json"
    now = datetime(2026, 1, 2, 3, 4, 5)
    first = corrupt_backup_path(path, now)
    assert first.name == "tasks.json.corrupt-20260102-030405"
    first.write_text("x")
    second = corrupt_backup_path(path, now)
    assert second.name == "tasks.json.corrupt-20260102-030405-1"
    second.write_text("x")
    assert corrupt_backup_path(path, now).name.endswith("-2")


# --- 1c: dedupe is per event, not per fixed path ------------------------------


def test_repeat_corruption_has_new_dedupe_key(tmp_path):
    path = tmp_path / "tasks.json"
    _corrupt(path)
    load_data_result(str(path))
    _corrupt(path)
    load_data_result(str(path))

    issues = pop_issues()
    assert len(issues) == 2
    assert issues[0].dedupe_key() != issues[1].dedupe_key()


def test_unmoved_corruption_dedupes_on_file(tmp_path):
    path = tmp_path / "tasks.json"
    a = DataIssue("corrupt", path, None, detail="denied")
    b = DataIssue("corrupt", path, None, detail="denied")
    assert a.dedupe_key() == b.dedupe_key()
    assert DataIssue("write_failed", path, detail="x").dedupe_key() is None


# --- 1d: wording depends on file and status ----------------------------------


def test_wording_for_non_task_file(tmp_path):
    path = tmp_path / "llm_config.json"
    backup = tmp_path / "llm_config.json.corrupt-1"
    issue = DataIssue("corrupt", path, backup)

    assert "empty list" not in issue.message()
    assert "task" not in issue.message().replace(str(backup), "").lower()
    assert str(backup) in issue.message()
    assert "Default chat settings" in issue.message()
    assert issue.title() == "Chat settings recovered"


def test_wording_for_task_file(tmp_path):
    issue = DataIssue("corrupt", tmp_path / "tasks.json", tmp_path / "b")
    assert "empty task list" in issue.message()
    assert issue.title() == "Task data recovered"


def test_wording_for_unreadable_file(tmp_path):
    issue = DataIssue("unreadable", tmp_path / "chat_conversations.json", detail="denied")
    assert issue.title() == "Chat history unreadable"
    assert "Could not read chat_conversations.json: denied" in issue.message()
    assert "recovered" not in issue.title()
    assert "permissions" in issue.advice()


def test_wording_for_unknown_file(tmp_path):
    backup = tmp_path / "b"
    issue = DataIssue("corrupt", tmp_path / "something.json", backup)
    assert issue.title() == "Data file recovered"
    assert "task" not in issue.message().replace(str(backup), "").lower()


# --- 1e: several issues are combined ------------------------------------------


def test_combine_messages_keeps_every_issue(tmp_path):
    a = DataIssue("unreadable", tmp_path / "tasks.json", detail="denied")
    b = DataIssue("corrupt", tmp_path / "llm_config.json", tmp_path / "x")
    combined = combine_messages([a, b, a])
    assert a.message() in combined
    assert b.message() in combined
    assert combined.count(a.message()) == 1


# --- GUI drain (offscreen) ----------------------------------------------------


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _close_boxes(boxes):
    for box in boxes:
        box.close()


def test_gui_shows_repeat_corruption_again(qapp, tmp_path):
    from PySide6.QtWidgets import QMessageBox, QWidget

    from system_sl.frontend.gui import data_issue_reporter as reporter

    parent = QWidget()
    path = tmp_path / "tasks.json"
    _corrupt(path)
    load_data_result(str(path))
    first = reporter.show_issue_dialogs(parent, pop_issues())
    _corrupt(path)
    load_data_result(str(path))
    second = reporter.show_issue_dialogs(parent, pop_issues())
    try:
        assert len(first) == 1
        assert len(second) == 1, "a second corruption must be shown too"
        assert isinstance(second[0], QMessageBox)
        assert second[0].windowTitle() == "Task data recovered"
        from PySide6.QtCore import Qt

        assert second[0].windowModality() == Qt.WindowModality.NonModal
        assert second[0].isVisible()
    finally:
        _close_boxes(first + second)
        parent.deleteLater()


def test_tasks_window_status_lists_every_issue(qapp, tmp_path):
    from PySide6.QtWidgets import QLabel, QWidget

    from system_sl.frontend.gui.popup_windows import TasksWindow

    # Exercise the real method on a light stand-in, so no ranking pass runs.
    class Stub(QWidget):
        _status_message = ""
        _priority_note = ""

        def _apply_status(self):
            TasksWindow._apply_status(self)

    stub = Stub()
    stub.status_label = QLabel()
    record_issue(DataIssue("write_failed", tmp_path / "tasks.json", detail="disk full"))
    record_issue(DataIssue("write_failed", tmp_path / "task_order.json", detail="denied"))

    TasksWindow._report_data_issues(stub)

    text = stub.status_label.text()
    assert "Could not save tasks.json: disk full" in text
    assert "Could not save task_order.json: denied" in text
    stub.deleteLater()


def test_monitor_drains_queue_without_tasks_window(qapp, tmp_path):
    from PySide6.QtWidgets import QWidget

    from system_sl.frontend.gui.data_issue_reporter import DataIssueMonitor

    parent = QWidget()
    monitor = DataIssueMonitor(parent, interval_ms=60_000)
    try:
        record_issue(
            DataIssue("unreadable", tmp_path / "chat_conversations.json", detail="denied")
        )
        assert has_issues()
        summary = monitor.check()
        assert "chat_conversations.json" in summary
        assert not has_issues()
        assert monitor.check() == ""
    finally:
        monitor.stop()
        for box in parent.findChildren(QWidget):
            box.close()
        parent.deleteLater()


def test_latest_summary_survives_a_second_drain(tmp_path, monkeypatch):
    """The Tasks status line can name an issue the main monitor drained."""
    from system_sl.frontend.gui import data_issue_reporter as reporter
    from system_sl.utils import data_health
    from pathlib import Path

    issue = data_health.DataIssue("corrupt", tmp_path / "tasks.json")
    data_health.record_issue(issue)
    # Avoid opening a real dialog in the test.
    monkeypatch.setattr(reporter, "show_issue_dialogs", lambda parent, issues: [])
    first = reporter.drain_data_issues(None)
    assert "tasks.json" in first
    assert reporter.drain_data_issues(None) == ""
    assert reporter.latest_data_issue_summary() == first
