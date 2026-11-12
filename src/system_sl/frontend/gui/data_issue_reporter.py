"""Show queued data-file problems to the player, from the GUI thread.

Problems are recorded by :mod:`system_sl.utils.data_health` wherever they
happen, often in a worker thread. This module drains that queue on the GUI
thread and explains each new problem once, in a non-modal dialog.
"""

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QMessageBox, QWidget

from system_sl.utils.data_health import (
    DataIssue,
    combine_messages,
    has_issues,
    latest_summary,
    pop_issues,
    set_latest_summary,
)


# Dialog dedupe keys already shown this session, shared by every window so
# the main window and the Tasks window never explain the same event twice.
_shown_keys: set[str] = set()


def show_issue_dialogs(parent: QWidget | None, issues: list[DataIssue]) -> list[QMessageBox]:
    """Open a non-modal dialog for each issue not already explained."""
    boxes = []
    for issue in issues:
        key = issue.dedupe_key()
        if key is None or key in _shown_keys:
            continue
        _shown_keys.add(key)
        box = QMessageBox(parent)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(issue.title())
        box.setText(issue.message())
        box.setInformativeText(issue.advice())
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        # show(), not open(): open() makes the box window-modal, which
        # blocks the parent window until it is dismissed.
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.show()
        boxes.append(box)
    return boxes


def drain_data_issues(parent: QWidget | None) -> str:
    """Take every queued issue, show new ones, and return a combined summary.

    The summary holds one line per distinct problem, for a status label.
    Returns an empty string when nothing was queued.
    """
    issues = pop_issues()
    if not issues:
        return ""
    show_issue_dialogs(parent, issues)
    summary = combine_messages(issues)
    set_latest_summary(summary)
    return summary


def latest_data_issue_summary() -> str:
    """The last summary another window drained, for status lines."""
    return latest_summary()


class DataIssueMonitor(QObject):
    """Polls the issue queue so problems found anywhere reach the player.

    Without this only the Tasks window drained the queue, after a ranking
    pass, so a damaged config or chat-history file went unmentioned. The
    timer lives on the GUI thread; issues recorded from worker threads are
    picked up on the next tick.
    """

    def __init__(self, parent: QWidget, interval_ms: int = 1000) -> None:
        super().__init__(parent)
        self._widget = parent
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.check)
        self.timer.start()

    def check(self) -> str:
        """Drain and show anything queued since the last check."""
        if not has_issues():
            return ""
        return drain_data_issues(self._widget)

    def stop(self) -> None:
        self.timer.stop()
