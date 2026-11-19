import logging

from PySide6.QtCore import (
    QDate,
    QObject,
    QSize,
    Qt,
    QThread,
    Signal,
    Slot,
    QTimer,
    QRegularExpression,
)
from pathlib import Path
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QHeaderView,
    QMessageBox,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPalette
from system_sl.frontend.gui.icons import get_icon
from system_sl.core.importance import suggest_importance
from system_sl.core.onboarding import ONBOARDING_QUESTIONS, PersonaStorageHandler
from system_sl.core.ranking_config import DEFAULT_IMPORTANCE, QUADRANTS
from system_sl.core.tasks import (
    add_tasks,
    edit_task,
    load_display_order,
    load_tasks,
    mark_task_completed,
    remove_tasks,
    save_manual_order,
)
from system_sl.core.priority_engine_new import run_prioritization  # we are importing run_prioritization from api section(priority_engine_new)
from system_sl.frontend.gui.theme import get_current_theme_tokens
from system_sl.frontend.gui.data_issue_reporter import (
    drain_data_issues,
    latest_data_issue_summary,
)
from system_sl.utils.data_health import DataIssue, record_issue
from system_sl.utils.paths import get_tasks_file_path
from system_sl.utils.worker_threads import new_worker_thread
from system_sl.utils.i18n import tr


# GUI-added tasks no longer carry a user-chosen category — the model assigns one
# later. Until then they land in this default bucket.
DEFAULT_CATEGORY = "general"

log = logging.getLogger(__name__)

_QUADRANT_ORDER = ("Q1", "Q2", "Q3", "Q4")

# Extra item roles. Qt.UserRole holds the stable task id/key.
_ROW_KIND_ROLE = Qt.UserRole + 1
_QUADRANT_ROLE = Qt.UserRole + 2
_REASON_ROLE = Qt.UserRole + 3

_EFFORT_OPTIONS = (
    ("unknown", None),
    ("15 minutes", 15),
    ("30 minutes", 30),
    ("60 minutes", 60),
    ("120 minutes", 120),
    ("240 minutes", 240),
)


def _select_combo_value(combo: QComboBox, value) -> None:
    index = combo.findData(value)
    combo.setCurrentIndex(index if index >= 0 else 0)


def _make_importance_combo(value) -> QComboBox:
    combo = QComboBox()
    for level in range(1, 6):
        combo.addItem(str(level), level)
    if not isinstance(value, int) or isinstance(value, bool):
        value = DEFAULT_IMPORTANCE
    _select_combo_value(combo, max(1, min(5, value)))
    return combo


def _make_effort_combo(value) -> QComboBox:
    combo = QComboBox()
    known = set()
    for label, minutes in _EFFORT_OPTIONS:
        combo.addItem(label, minutes)
        known.add(minutes)
    # Preserve a stored value the six standard choices cannot represent.
    if value is not None and value not in known:
        combo.addItem(f"{value} minutes", value)
    _select_combo_value(combo, value)
    return combo


class _TaskRowDelegate(QStyledItemDelegate):
    """Draw a task as a bold title line plus a smaller muted reason line."""

    def paint(self, painter, option, index):
        if index.data(_ROW_KIND_ROLE) != "task":
            super().paint(painter, option, index)
            return

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        rect = opt.rect.adjusted(10, 4, -10, -4)
        selected = bool(opt.state & QStyle.State_Selected)
        title = str(index.data(Qt.DisplayRole) or "")
        reason = str(index.data(_REASON_ROLE) or "")

        title_font = QFont(opt.font)
        title_font.setBold(True)
        title_colour = opt.palette.color(
            QPalette.HighlightedText if selected else QPalette.Text
        )
        painter.save()
        painter.setFont(title_font)
        painter.setPen(title_colour)
        metrics = QFontMetrics(title_font)
        painter.drawText(rect, Qt.AlignLeft | Qt.AlignTop, title)

        if reason:
            reason_font = QFont(opt.font)
            if reason_font.pointSizeF() > 0:
                reason_font.setPointSizeF(max(7.0, reason_font.pointSizeF() - 1.5))
            painter.setFont(reason_font)
            tokens = get_current_theme_tokens()
            reason_colour = tokens.get(
                "sys_color_text_muted" if not selected else "sys_color_text_main",
                "#7a7a7a",
            )
            painter.setPen(QColor(reason_colour))
            reason_rect = rect.adjusted(0, metrics.height(), 0, 0)
            painter.drawText(reason_rect, Qt.AlignLeft | Qt.AlignTop, reason)
        painter.restore()

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        if index.data(_ROW_KIND_ROLE) == "task":
            metrics = QFontMetrics(option.font)
            size.setHeight(max(size.height(), metrics.height() * 2 + 10))
        return size


class _ImportanceSuggestionWorker(QObject):
    """Run ``suggest_importance`` away from the GUI thread."""

    finished = Signal(object)  # (title, importance | None, matched_goal | None)

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.title = title

    def run(self) -> None:
        try:
            importance, matched_goal, _relevance = suggest_importance(self.title)
        except Exception:
            log.debug("importance suggestion failed", exc_info=True)
            self.finished.emit((self.title, None, None))
            return
        self.finished.emit((self.title, importance, matched_goal))


class _SuggestionReceiver(QObject):
    """Queued GUI-thread target for a suggestion worker result."""

    def __init__(self, handler, parent=None):
        super().__init__(parent)
        self._handler = handler
        self.closed = False

    @Slot(object)
    def on_finished(self, payload) -> None:
        if self.closed:
            return
        title, importance, matched_goal = payload
        self._handler(title, importance, matched_goal)


def _start_importance_suggestion(title: str, receiver: _SuggestionReceiver) -> None:
    worker = _ImportanceSuggestionWorker(title)
    thread = new_worker_thread(worker)
    worker.finished.connect(receiver.on_finished)
    thread.start()


class ReorderableTaskList(QListWidget):
    """Task list the user can drag to re-rank.

    Emits ``reordered(moved_text, old_rank, new_rank, new_texts, new_titles)``
    after a valid same-quadrant drag. Ranks and the two lists contain only task
    rows, never section headers. A drop into another quadrant emits
    ``reorderRejected`` and the caller restores the previous order.
    """

    reordered = Signal(str, int, int, list, list)
    reorderRejected = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setItemDelegate(_TaskRowDelegate(self))

    def _task_items(self) -> list[QListWidgetItem]:
        return [
            self.item(row)
            for row in range(self.count())
            if self.item(row).data(_ROW_KIND_ROLE) == "task"
        ]

    def _quadrant_at(self, row: int):
        """Return the quadrant of the nearest header at or above ``row``."""
        for current in range(row, -1, -1):
            item = self.item(current)
            if item is not None and item.data(_ROW_KIND_ROLE) == "header":
                return item.data(_QUADRANT_ROLE)
        return None

    def dropEvent(self, event):
        moved_item = self.currentItem()
        if moved_item is None or moved_item.data(_ROW_KIND_ROLE) != "task":
            event.ignore()
            return

        moved_key = moved_item.data(Qt.UserRole)
        source_quadrant = moved_item.data(_QUADRANT_ROLE)
        old_keys = [item.data(Qt.UserRole) for item in self._task_items()]

        super().dropEvent(event)

        keyed_rows = [
            (row, self.item(row))
            for row in range(self.count())
            if self.item(row).data(_ROW_KIND_ROLE) == "task"
            and self.item(row).data(Qt.UserRole) == moved_key
        ]
        if not keyed_rows:
            return
        moved_row, moved_item = keyed_rows[0]
        if self._quadrant_at(moved_row) != source_quadrant:
            self.reorderRejected.emit()
            return

        items = self._task_items()
        new_keys = [item.data(Qt.UserRole) for item in items]
        new_texts = [item.text() for item in items]
        try:
            old_rank = old_keys.index(moved_key) + 1
            new_rank = new_keys.index(moved_key) + 1
        except ValueError:
            return
        if old_rank != new_rank:
            self.reordered.emit(
                moved_item.text(), old_rank, new_rank, new_texts, new_keys
            )


class _RankingWorker(QObject):
    """Runs the priority engine away from the GUI thread.

    Loading the NLP model and scoring every task takes seconds on first use,
    which used to freeze the window on every refresh.
    """

    finished = Signal(object)  # ranking result dict
    failed = Signal(str)

    def run(self) -> None:
        try:
            result = run_prioritization(display=False)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")
            return
        self.finished.emit(result)


class TasksWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tasks")
        self.setMinimumSize(QSize(800, 400))

        # Current display order (prioritized task dicts) and a lookup from each
        # item's label back to its task dict, kept in sync by _render().
        self._order = []
        self._text_to_task = {}
        # Ranked results are grouped by quadrant; the degraded fallback is not.
        self._grouped = False

        # Add-form suggestion state. The selector is only auto-filled while
        # the player has not changed it by hand.
        self._suggestion_title = None
        self._suggestion_value = None
        self._suggestion_goal = None
        self._importance_touched = False
        self._setting_importance = False
        self._suggestion_receiver = _SuggestionReceiver(
            self._on_add_suggestion, self
        )

        # Status text is split in two so a transient "Added: x" message is not
        # wiped by, nor endlessly appended to, a standing ranking notice.
        self._status_message = ""
        self._priority_note = ""

        # One ranking worker at a time. A refresh requested while it runs is
        # coalesced: its result is discarded and ranking starts again, so the
        # list never shows a stale order.
        self._ranking_thread: QThread | None = None
        self._ranking_worker: _RankingWorker | None = None
        self._refresh_pending = False
        self._shut_down = False

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Here are your tasks!!"))

        self.tasks_list_widget = ReorderableTaskList()
        self.tasks_list_widget.reordered.connect(self._on_reordered)
        self.tasks_list_widget.reorderRejected.connect(self._on_reorder_rejected)
        root.addWidget(self.tasks_list_widget, stretch=1)

        action_row = QHBoxLayout()
        self.complete_button = QPushButton()
        self.complete_button.setIcon(get_icon("complete"))
        self.complete_button.setText("Mark Complete")
        self.complete_button.clicked.connect(self._on_complete)
        self.remove_button = QPushButton()
        self.remove_button.setIcon(get_icon("remove_task"))
        self.remove_button.setText("Remove")
        self.remove_button.clicked.connect(self._on_remove)
        self.edit_button = QPushButton()
        self.edit_button.setIcon(get_icon("edit"))
        self.edit_button.setText("Edit")
        self.edit_button.clicked.connect(self._on_edit)
        action_row.addWidget(self.complete_button)
        action_row.addWidget(self.remove_button)
        action_row.addWidget(self.edit_button)
        action_row.addStretch(1)
        root.addLayout(action_row)

        root.addWidget(QLabel("Add task"))

        add_row = QHBoxLayout()
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("Title")
        self.title_input.textEdited.connect(self._on_title_edited)
        self.title_input.editingFinished.connect(self._on_title_editing_finished)

        self.deadline_checkbox = QCheckBox("Deadline:")
        self.deadline_edit = QDateEdit()
        self.deadline_edit.setCalendarPopup(True)
        self.deadline_edit.setDisplayFormat("yyyy-MM-dd")
        self.deadline_edit.setDate(QDate.currentDate())
        self.deadline_edit.setMinimumDate(QDate.currentDate())
        self.deadline_edit.setEnabled(False)
        self.deadline_checkbox.toggled.connect(self.deadline_edit.setEnabled)

        self.effort_combo = _make_effort_combo(None)
        self.importance_combo = _make_importance_combo(DEFAULT_IMPORTANCE)
        self.importance_combo.currentIndexChanged.connect(
            self._on_importance_changed
        )

        self.add_button = QPushButton()
        self.add_button.setIcon(get_icon("add"))
        self.add_button.setText("Add")
        self.add_button.clicked.connect(self._on_add)
        add_row.addWidget(self.title_input)
        add_row.addWidget(self.deadline_checkbox)
        add_row.addWidget(self.deadline_edit)
        add_row.addWidget(QLabel("Effort:"))
        add_row.addWidget(self.effort_combo)
        add_row.addWidget(QLabel("Importance:"))
        add_row.addWidget(self.importance_combo)
        add_row.addWidget(self.add_button)
        root.addLayout(add_row)

        self.suggestion_label = QLabel("")
        self.suggestion_label.setTextFormat(Qt.PlainText)
        self.suggestion_label.setStyleSheet("font-size: 11px;")
        root.addWidget(self.suggestion_label)

        self.status_label = QLabel("")
        # Task titles are stored literally; never let one render as HTML.
        self.status_label.setTextFormat(Qt.PlainText)
        # Several data problems are listed one per line, with full paths.
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self._refresh_list()

    def showEvent(self, event):
        super().showEvent(event)
        # Re-read from disk every time the window is shown so chatbot-driven
        # mutations made while the window was hidden show up.
        self._refresh_list()

    def _set_status(self, message: str) -> None:
        """Show ``message`` alongside any standing ranking notice."""
        self._status_message = message
        self._apply_status()

    def _apply_status(self) -> None:
        """Compose the transient message and the standing notice into the label."""
        text = self._status_message
        if self._priority_note:
            text = f"{text} · {self._priority_note}" if text else self._priority_note
        self.status_label.setText(text)

    def _report_data_issues(self) -> None:
        """Show any data-file problems recorded since the last refresh.

        Read failures and quarantine happen below the GUI, so the interface
        drains the queue and explains them once instead of letting a corrupt
        file look like an empty task list.
        """
        # The main window's monitor usually drains the queue first. Use the
        # summary it stored so this status line still names the problem;
        # only drain here when nothing has claimed the queue yet.
        from system_sl.utils.data_health import has_issues

        # New issues first; drain_data_issues also stores the summary, which
        # is taken right away so it is not shown again on the next refresh.
        if has_issues():
            drain_data_issues(self)
        summary = latest_data_issue_summary()
        if summary:
            self._status_message = summary
        self._apply_status()

    def _report_write_failure(self, path: str, error: OSError) -> None:
        """Record and show a failed save without crashing the window."""
        record_issue(DataIssue("write_failed", Path(path), detail=str(error)))
        self._set_status(f"Changes could not be saved: {error}")

    def _refresh_list(self):
        """Start ranking in the background, keeping the last list visible.

        The engine loads an NLP model and scores every task, so doing this in
        the GUI thread froze the window. The last rendered order stays on
        screen until a newer result arrives.
        """
        if self._shut_down:
            return
        if self._ranking_thread is not None:
            # A newer refresh supersedes the result still being computed.
            self._refresh_pending = True
            return

        self._priority_note = "ranking…"
        self._apply_status()
        self._start_ranking()

    def _start_ranking(self) -> None:
        """Run one prioritization pass in a worker thread.

        The thread has no Qt parent and is kept alive by ``worker_threads``
        until the pass ends, so closing or deleting this window mid-pass can
        never destroy a running QThread; the app's quit path waits for it.
        """
        self._ranking_worker = _RankingWorker()
        self._ranking_thread = new_worker_thread(self._ranking_worker)
        self._ranking_worker.finished.connect(self._on_ranking_done)
        self._ranking_worker.failed.connect(self._on_ranking_failed)
        self._ranking_thread.finished.connect(self._cleanup_ranking)
        self._ranking_thread.start()

    def _on_ranking_done(self, result: dict) -> None:
        if self._refresh_pending or self._shut_down:
            return  # a newer refresh will render instead
        self._render_ranking(result)

    def _on_ranking_failed(self, message: str) -> None:
        if self._refresh_pending or self._shut_down:
            return
        self._priority_note = ""
        self._set_status(f"Ranking failed: {message}")
        if not self._order:
            self._grouped = False
            self._order = self._unranked_tasks()
            self._render(self._order)
        self._report_data_issues()

    def _cleanup_ranking(self) -> None:
        # The thread and worker are released by worker_threads once finished.
        self._ranking_worker = None
        self._ranking_thread = None
        if self._refresh_pending and not self._shut_down:
            self._refresh_pending = False
            self._refresh_list()

    def is_ranking(self) -> bool:
        """True while a ranking pass is still running."""
        return self._ranking_thread is not None and self._ranking_thread.isRunning()

    def shutdown(self, timeout_ms: int = 0) -> bool:
        """Stop reacting to ranking and optionally wait for the running pass.

        The pass is one blocking call and cannot be interrupted, so this drops
        any queued re-rank, ignores the pending result and waits at most
        ``timeout_ms``. Returns True when no pass is running any more. The
        thread stays tracked either way, so it is never destroyed while live.
        """
        self._shut_down = True
        self._refresh_pending = False
        if self._ranking_thread is None:
            return True
        return self._ranking_thread.wait(max(0, timeout_ms))

    def closeEvent(self, event):
        # Closing only hides the window; a queued re-rank is pointless now,
        # since showing it again starts a fresh pass anyway.
        self._refresh_pending = False
        super().closeEvent(event)

    def _render_ranking(self, result: dict) -> None:
        """Apply a ranking result, falling back to the stored order."""
        ranked = result.get("all_tasks_ranked") or []
        self._grouped = bool(ranked)

        # An empty ranking does not mean the player has no tasks. The ranking
        # depends on the player profile and the bundled NLP model, so when it
        # cannot be produced fall back to the stored list rather than showing a
        # blank window that makes tasks look lost even though they were saved.
        if not ranked:
            self._priority_note = self._explain(result.get("_reason"))
            self._order = self._unranked_tasks()
        else:
            if result.get("_reason"):
                # Ranked, but only partially: point the player at what unlocks
                # the full ordering instead of leaving the note unexplained.
                self._priority_note = (
                    f"{result['_reason']} — use 'Edit Player Profile' to improve it"
                )
            else:
                self._priority_note = ""

            quadrants = result.get("quadrants") or {}
            self._order = [
                task
                for quadrant in _QUADRANT_ORDER
                for task in (quadrants.get(quadrant) or {}).get("tasks", [])
            ]
            if not self._order:
                # Malformed snapshot: keep the tasks visible in score order.
                self._grouped = False
                self._order = list(ranked)

        self._apply_status()
        self._render(self._order)
        self._report_data_issues()

    @staticmethod
    def _explain(reason: str | None) -> str:
        """Turn a prioritization failure reason into something a player can act on."""
        if not reason or reason == "No tasks found":
            return ""
        return f"Showing tasks unranked — {reason}"

    def _unranked_tasks(self) -> list:
        """Return every stored task, in the last shown order, for unranked rendering."""
        try:
            stored = [t for t in load_tasks() if isinstance(t, dict)]
        except Exception as e:
            self._status_message = f"Could not load tasks: {e}"
            return []

        order = load_display_order()
        position = {key: i for i, key in enumerate(order)}
        return sorted(
            stored,
            key=lambda t: position.get(
                t.get("id") or t.get("title", ""), len(position)
            ),
        )

    def _render(self, tasks):
        """Render tasks, grouped into quadrants when the result was ranked."""
        self.tasks_list_widget.clear()
        self._text_to_task = {}

        if self._grouped:
            for quadrant in _QUADRANT_ORDER:
                quadrant_tasks = [
                    task for task in tasks if task.get("quadrant") == quadrant
                ]
                self._add_quadrant_header(quadrant, len(quadrant_tasks))
                for task in quadrant_tasks:
                    self._add_task_row(task)
            return

        for task in tasks:
            self._add_task_row(task)

    def _add_quadrant_header(self, quadrant: str, count: int) -> None:
        name = QUADRANTS.get(quadrant, {}).get("name", quadrant)
        item = QListWidgetItem(f"{quadrant} · {name} ({count})")
        item.setData(_ROW_KIND_ROLE, "header")
        item.setData(_QUADRANT_ROLE, quadrant)
        item.setFlags(Qt.ItemIsEnabled)
        font = QFont(item.font())
        font.setBold(True)
        item.setFont(font)
        item.setSizeHint(QSize(0, 28))
        self.tasks_list_widget.addItem(item)

    def _add_task_row(self, task: dict) -> None:
        title = str(task.get("title") or "untitled")
        deadline = task.get("deadline")
        display = title
        if deadline:
            display += f"  → due {deadline}"

        item = QListWidgetItem(display)
        # Key by the stable task id: titles can be edited or duplicated.
        key = task.get("id") or title
        item.setData(Qt.UserRole, key)
        item.setData(_ROW_KIND_ROLE, "task")
        item.setData(_QUADRANT_ROLE, task.get("quadrant"))
        item.setData(_REASON_ROLE, str(task.get("reason") or ""))
        item.setFlags(
            Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled
        )
        reason = str(task.get("reason") or "")
        if reason:
            item.setToolTip(reason)
        self.tasks_list_widget.addItem(item)
        self._text_to_task[key] = task

    def _on_reorder_rejected(self) -> None:
        self._set_status("Tasks can only be reordered within their quadrant.")
        QTimer.singleShot(0, lambda: self._render(self._order))

    def _on_reordered(self, moved_text, old_rank, new_rank, new_texts, new_titles):
        # new_titles contains the actual task titles from Qt.UserRole (no parsing needed)
        new_order_dicts = [
            self._text_to_task[t] for t in new_titles if t in self._text_to_task
        ]
        # The moved item is now at new_rank in the new order
        moved_key = new_titles[new_rank - 1] if 0 <= new_rank - 1 < len(new_titles) else None
        moved = self._text_to_task.get(moved_key)

        if moved is None or len(new_order_dicts) != len(new_titles):
            # Safe deferred rendering
            QTimer.singleShot(0, lambda: self._render(self._order))
            return

        moved_title = moved.get("title", "")
        self._order = new_order_dicts

        # 2. HYBRID ACTION A: Save exact layout to disk
        try:
            save_manual_order(self._order)
        except OSError as e:
            self._report_write_failure(get_tasks_file_path("task_order.json"), e)

        # 3. The manual order is the whole signal. Drag-reorder is not fed to
        # the bandit: the scoring weights it reports are not used by the
        # gravity blend, so pretending it trains anything was misleading.
        self._set_status(f"Reordered '{moved_title}'")

        # 4. Render safely outside the drop event loop
        QTimer.singleShot(0, lambda: self._render(self._order))

    def _set_importance_value(self, value) -> None:
        """Select ``value`` without looking like a manual choice."""
        self._setting_importance = True
        try:
            _select_combo_value(self.importance_combo, value)
        finally:
            self._setting_importance = False

    def _on_importance_changed(self, _index: int) -> None:
        # A suggestion must never override a value the player picked.
        if self._setting_importance:
            return
        self._importance_touched = True

    def _clear_add_suggestion(self) -> None:
        self._suggestion_title = None
        self._suggestion_value = None
        self._suggestion_goal = None
        self.suggestion_label.clear()

    def _on_title_edited(self, text: str) -> None:
        """Drop a suggestion that belongs to the previous title."""
        if self._suggestion_title is None or text.strip() == self._suggestion_title:
            return
        if (
            not self._importance_touched
            and self._suggestion_value is not None
            and self.importance_combo.currentData() == self._suggestion_value
        ):
            # The visible value came from the stale suggestion, not the player.
            self._set_importance_value(DEFAULT_IMPORTANCE)
        self._clear_add_suggestion()

    def _on_title_editing_finished(self) -> None:
        if self._shut_down:
            return
        title = self.title_input.text().strip()
        if title:
            _start_importance_suggestion(title, self._suggestion_receiver)

    def _on_add_suggestion(self, title, importance, matched_goal) -> None:
        """Show and maybe apply a suggestion on the GUI thread."""
        if self._shut_down:
            return
        if title != self.title_input.text().strip():
            # The title changed while the model was running; this answer is
            # about a different task.
            return
        if importance is None or matched_goal is None:
            self._clear_add_suggestion()
            if not self._importance_touched:
                self._set_importance_value(DEFAULT_IMPORTANCE)
            return

        self._suggestion_title = title
        self._suggestion_value = importance
        self._suggestion_goal = matched_goal
        self.suggestion_label.setText(
            f"Suggested: {importance} (matches goal: {matched_goal})"
        )
        if not self._importance_touched:
            self._set_importance_value(importance)

    def _selected(self):
        item = self.tasks_list_widget.currentItem()
        if item is None or item.data(_ROW_KIND_ROLE) != "task":
            self._set_status("Select a task first.")
            return None
        return item.data(Qt.UserRole)

    def _on_complete(self):
        task_id = self._selected()
        if task_id is None:
            return
        task = self._text_to_task.get(task_id) or {}
        title = task.get("title", "")
        try:
            mark_task_completed(task_id=task_id)
            self._set_status(f"Completed: {title}")
        except ValueError as e:
            self._set_status(str(e))
        except OSError as e:
            self._report_write_failure(get_tasks_file_path("tasks.json"), e)
        self._refresh_list()

    def _on_remove(self):
        task_id = self._selected()
        if task_id is None:
            return
        task = self._text_to_task.get(task_id) or {}
        title = task.get("title", "")
        try:
            remove_tasks(task_id=task_id)
            self._set_status(f"Removed: {title}")
        except ValueError as e:
            self._set_status(str(e))
        except OSError as e:
            self._report_write_failure(get_tasks_file_path("tasks.json"), e)
        self._refresh_list()

    def _on_edit(self):
        """Open a dialog to edit a task, including its priority fields."""
        task_id = self._selected()
        if task_id is None:
            return

        task = self._text_to_task.get(task_id)
        if task is None:
            self._set_status("Could not find task data.")
            return
        title = task.get("title", "")

        current_deadline = task.get("deadline")
        stored_importance = task.get("importance")
        if (
            not isinstance(stored_importance, int)
            or isinstance(stored_importance, bool)
            or not 1 <= stored_importance <= 5
        ):
            stored_importance = DEFAULT_IMPORTANCE
        stored_source = task.get("importance_source")
        if stored_source not in ("user", "suggested", "default"):
            stored_source = "default"
        stored_goal = task.get("matched_goal")
        if stored_source != "suggested" or not isinstance(stored_goal, str):
            stored_goal = None
        if stored_goal is not None and not stored_goal.strip():
            stored_goal = None

        dialog = QDialog(self)
        dialog.setWindowTitle(tr("Edit Task"))
        dialog.setModal(True)
        form = QFormLayout(dialog)

        title_edit = QLineEdit(title)
        form.addRow(tr("Title:"), title_edit)

        deadline_check = QCheckBox(tr("Set deadline:"))
        deadline_edit = QDateEdit()
        deadline_edit.setCalendarPopup(True)
        deadline_edit.setDisplayFormat("yyyy-MM-dd")
        # A task with a deadline opens with it already checked and filled in.
        # Google-synced deadlines are "yyyy-MM-dd HH:mm": the date is the
        # first ten characters and the time is kept when the date changes.
        current_deadline = current_deadline if isinstance(current_deadline, str) else ""
        deadline_date = current_deadline[:10]
        deadline_time = current_deadline[10:]
        parsed = QDate.fromString(deadline_date, "yyyy-MM-dd")
        has_deadline = parsed.isValid()
        deadline_check.setChecked(has_deadline)
        deadline_edit.setEnabled(has_deadline)
        deadline_edit.setDate(parsed if has_deadline else QDate.currentDate())
        deadline_check.toggled.connect(deadline_edit.setEnabled)
        form.addRow(deadline_check, deadline_edit)

        effort_combo = _make_effort_combo(task.get("effort_minutes"))
        form.addRow(tr("Effort:"), effort_combo)

        importance_combo = _make_importance_combo(stored_importance)
        form.addRow(tr("Importance:"), importance_combo)

        suggestion_label = QLabel("")
        suggestion_label.setTextFormat(Qt.PlainText)
        suggestion_label.setStyleSheet("font-size: 11px;")
        form.addRow(tr("Suggestion:"), suggestion_label)

        # A stored suggestion stays valid until the player edits the title or
        # the importance, so saving an untouched task preserves its source.
        suggestion_title = (
            title if stored_source == "suggested" and stored_goal else None
        )
        suggestion_value = stored_importance if suggestion_title else None
        suggestion_goal = stored_goal if suggestion_title else None
        if suggestion_title is not None:
            suggestion_label.setText(
                f"Suggested: {suggestion_value} (matches goal: {suggestion_goal})"
            )

        touched = False
        setting_importance = False

        def set_importance(value) -> None:
            nonlocal setting_importance
            setting_importance = True
            try:
                _select_combo_value(importance_combo, value)
            finally:
                setting_importance = False

        def on_importance_changed(_index: int) -> None:
            nonlocal touched
            if setting_importance:
                return
            touched = True

        importance_combo.currentIndexChanged.connect(on_importance_changed)

        def on_suggestion(result_title, importance, matched_goal) -> None:
            nonlocal suggestion_title, suggestion_value, suggestion_goal
            if result_title != title_edit.text().strip():
                return
            if importance is None or matched_goal is None:
                suggestion_title = None
                suggestion_value = None
                suggestion_goal = None
                suggestion_label.clear()
                return

            suggestion_title = result_title
            suggestion_value = importance
            suggestion_goal = matched_goal
            suggestion_label.setText(
                f"Suggested: {importance} (matches goal: {matched_goal})"
            )
            # A value the player set is theirs; an existing "user" importance
            # is never replaced by a suggestion.
            if not touched and stored_source != "user":
                set_importance(importance)

        receiver = _SuggestionReceiver(on_suggestion, dialog)

        def on_title_edited(text: str) -> None:
            nonlocal suggestion_title, suggestion_value, suggestion_goal
            if suggestion_title is None or text.strip() == suggestion_title:
                return
            if (
                not touched
                and suggestion_value is not None
                and importance_combo.currentData() == suggestion_value
            ):
                # The visible value came from the stale suggestion.
                set_importance(DEFAULT_IMPORTANCE)
            suggestion_title = None
            suggestion_value = None
            suggestion_goal = None
            suggestion_label.clear()

        title_edit.textEdited.connect(on_title_edited)

        def on_title_finished() -> None:
            candidate = title_edit.text().strip()
            if candidate:
                _start_importance_suggestion(candidate, receiver)

        title_edit.editingFinished.connect(on_title_finished)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        accepted = dialog.exec()
        # Ignore a result that was still in flight when the dialog closed.
        receiver.closed = True
        if accepted != QDialog.Accepted:
            return

        new_title = title_edit.text().strip()
        if deadline_check.isChecked():
            picked = deadline_edit.date().toString("yyyy-MM-dd")
            # None leaves an untouched deadline exactly as stored.
            new_deadline = (
                None if has_deadline and picked == deadline_date
                else picked + (deadline_time if has_deadline else "")
            )
        else:
            # "" clears a deadline the Player unticked; None = nothing to do.
            new_deadline = "" if current_deadline else None
        if not new_title:
            self._set_status(tr("Title is required."))
            return

        importance = importance_combo.currentData()
        if not isinstance(importance, int) or isinstance(importance, bool):
            importance = DEFAULT_IMPORTANCE
        suggested = (
            not touched
            and stored_source != "user"
            and suggestion_goal is not None
            and suggestion_title == new_title
            and suggestion_value == importance
        )
        source = "suggested" if suggested else "user"
        matched_goal = suggestion_goal if suggested else None
        effort_minutes = effort_combo.currentData()

        try:
            edit_task(
                task_id=task_id,
                old_title=title,
                new_title=new_title,
                new_deadline=new_deadline,
                importance=importance,
                importance_source=source,
                matched_goal=matched_goal,
                effort_minutes=effort_minutes,
                # edit_task treats None as "leave unchanged"; the selector's
                # "unknown" must therefore clear the stored value explicitly.
                clear_effort=effort_minutes is None,
            )
            self._set_status(f"Edited: {new_title}")
        except ValueError as e:
            self._set_status(str(e))
        except OSError as e:
            self._report_write_failure(get_tasks_file_path("tasks.json"), e)
        self._refresh_list()

    def _on_add(self):
        title = self.title_input.text().strip()
        deadline = (
            self.deadline_edit.date().toString("yyyy-MM-dd")
            if self.deadline_checkbox.isChecked()
            else None
        )
        if not title:
            self._set_status(tr("Title is required."))
            return

        importance = self.importance_combo.currentData()
        if not isinstance(importance, int) or isinstance(importance, bool):
            importance = DEFAULT_IMPORTANCE

        # "Suggested" only describes the exact value the model proposed when
        # the player did not choose a value of their own.
        suggested = (
            not self._importance_touched
            and self._suggestion_goal is not None
            and self._suggestion_title == title
            and self._suggestion_value == importance
        )
        source = "suggested" if suggested else "user"
        matched_goal = self._suggestion_goal if suggested else None
        effort_minutes = self.effort_combo.currentData()

        try:
            # Category is assigned by the model later; default for now.
            add_tasks(
                DEFAULT_CATEGORY,
                title,
                deadline,
                importance=importance,
                importance_source=source,
                matched_goal=matched_goal,
                effort_minutes=effort_minutes,
            )
            self._set_status(f"Added: {title}")
            self.title_input.clear()
            self.deadline_checkbox.setChecked(False)
            self.deadline_edit.setDate(QDate.currentDate())
            self._importance_touched = False
            self._set_importance_value(DEFAULT_IMPORTANCE)
            _select_combo_value(self.effort_combo, None)
            self._clear_add_suggestion()
        except ValueError as e:
            self._set_status(str(e))
        except OSError as e:
            self._report_write_failure(get_tasks_file_path("tasks.json"), e)
        self._refresh_list()


class OnboardingWindow(QWidget):
    onboarding_complete = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("System Awakening")
        self.setMinimumSize(QSize(640, 520))

        self.storage = PersonaStorageHandler()
        self.responses = []
        self.current_index = 0
        self._done = False

        self.questions = ONBOARDING_QUESTIONS

        self.stack = QStackedWidget()
        root = QVBoxLayout(self)
        root.addWidget(self.stack)

        self.welcome_page = self._build_welcome_page()
        self.question_page = self._build_question_page()
        self.completion_page = self._build_completion_page()

        self.stack.addWidget(self.welcome_page)
        self.stack.addWidget(self.question_page)
        self.stack.addWidget(self.completion_page)

        self.stack.setCurrentIndex(0)

        # Apply theme colors based on the selected theme mode.
        self._tokens = get_current_theme_tokens()
        self.refresh_theme()

    def _build_welcome_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 40, 40, 40)

        self.welcome_title = QLabel("THE SYSTEM AWAKENS")
        self.welcome_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.welcome_title)

        body = QLabel(
            '"Arise, Player."\n\n'
            "Before you begin your journey, the SYSTEM must understand "
            "who you are and what you seek to achieve.\n\n"
            "The SYSTEM will ask you 10 questions to build your Player Profile. "
            "This profile will help prioritize quests aligned with YOUR goals.\n\n"
            "Answer with KEYWORDS, not sentences. "
            "Rate each answer's IMPACT on your journey (1-3)."
        )
        body.setWordWrap(True)
        body.setStyleSheet("font-size: 14px;")
        layout.addWidget(body)

        layout.addStretch(1)

        button_row = QHBoxLayout()
        begin_btn = QPushButton()
        begin_btn.setIcon(get_icon("begin"))
        begin_btn.setText("Begin")
        begin_btn.clicked.connect(self._on_begin)
        skip_btn = QPushButton()
        skip_btn.setIcon(get_icon("skip"))
        skip_btn.setText("Skip")
        skip_btn.clicked.connect(self._on_skip)
        button_row.addStretch(1)
        button_row.addWidget(begin_btn)
        button_row.addWidget(skip_btn)
        layout.addLayout(button_row)

        return page

    def _build_question_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 20, 30, 20)

        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)

        self.quest_label = QLabel()
        self.quest_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(self.quest_label)

        self.question_label = QLabel()
        self.question_label.setWordWrap(True)
        self.question_label.setStyleSheet("font-size: 15px; padding: 10px 0;")
        layout.addWidget(self.question_label)

        self.hint_label = QLabel()
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("font-size: 13px;")
        layout.addWidget(self.hint_label)

        self.answer_input = QLineEdit()
        self.answer_input.setPlaceholderText("Your Answer")
        self.answer_input.textChanged.connect(self._validate_input)
        layout.addWidget(self.answer_input)

        impact_label = QLabel("Impact Level:")
        impact_label.setStyleSheet("font-size: 13px; margin-top: 10px;")
        layout.addWidget(impact_label)

        impact_row = QHBoxLayout()
        self.impact_buttons = []
        for i, label in enumerate(["Low (1)", "Medium (2)", "High (3)"]):
            btn = QPushButton(label)
            btn.setProperty("impact", i + 1)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.toggled.connect(self._validate_input)
            self.impact_buttons.append(btn)
            impact_row.addWidget(btn)
        self.impact_group = QButtonGroup()
        for i, btn in enumerate(self.impact_buttons):
            self.impact_group.addButton(btn, i + 1)
        impact_row.addStretch(1)
        layout.addLayout(impact_row)

        layout.addStretch(1)

        button_row = QHBoxLayout()
        self.back_button = QPushButton()
        self.back_button.setIcon(get_icon("back"))
        self.back_button.setText("Back")
        self.back_button.setMinimumHeight(36)
        self.back_button.clicked.connect(self._on_back)
        self.next_button = QPushButton()
        self.next_button.setIcon(get_icon("next"))
        self.next_button.setText("Next")
        self.next_button.setEnabled(False)
        self.next_button.setMinimumHeight(36)
        self.next_button.clicked.connect(self._on_next)
        button_row.addWidget(self.back_button)
        button_row.addStretch(1)
        button_row.addWidget(self.next_button)
        layout.addLayout(button_row)

        return page

    def _build_completion_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 40, 40, 40)

        self.completion_title = QLabel("PLAYER PROFILE CREATED")
        self.completion_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.completion_title)

        self.completion_subtitle = QLabel('"The System has acknowledged your potential."')
        self.completion_subtitle.setAlignment(Qt.AlignCenter)
        self.completion_subtitle.setStyleSheet("font-size: 14px; margin-bottom: 20px;")
        layout.addWidget(self.completion_subtitle)

        summary_label = QLabel("Review and edit your answers before saving:")
        summary_label.setStyleSheet("font-size: 13px;")
        layout.addWidget(summary_label)

        # The summary is editable: the Player can correct a keyword or change
        # an impact rating before the profile is written.
        self.summary_table = QTableWidget(0, 3)
        self.summary_table.setHorizontalHeaderLabels(
            ["Category", "Answer", "Impact (1-3)"]
        )
        self.summary_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.summary_table.verticalHeader().setVisible(False)
        self.summary_table.setEditTriggers(
            QAbstractItemView.EditTrigger.AllEditTriggers
        )
        layout.addWidget(self.summary_table, 1)

        self.stats_label = QLabel()
        self.stats_label.setStyleSheet("font-size: 14px;")
        layout.addWidget(self.stats_label)

        completion_buttons = QHBoxLayout()
        completion_back = QPushButton()
        completion_back.setIcon(get_icon("back"))
        completion_back.setText("Back")
        completion_back.clicked.connect(self._on_completion_back)
        finish_btn = QPushButton()
        finish_btn.setIcon(get_icon("finish"))
        finish_btn.setText("Save Profile")
        finish_btn.setMinimumHeight(36)
        finish_btn.clicked.connect(self._on_finish)
        completion_buttons.addWidget(completion_back)
        completion_buttons.addStretch(1)
        completion_buttons.addWidget(finish_btn)
        layout.addLayout(completion_buttons)

        self.path_label = QLabel()
        self.path_label.setStyleSheet("font-size: 12px; margin-top: 10px;")
        self.path_label.setWordWrap(True)
        layout.addWidget(self.path_label)

        return page

    def refresh_theme(self, tokens=None):
        """Re-apply dynamic colors to all onboarding labels."""
        if tokens is None:
            tokens = get_current_theme_tokens()

        primary = tokens.get("sys_color_primary", "#00d9ff")
        text_secondary = tokens.get("sys_color_text_secondary", "#b3d4ff")

        self.welcome_title.setStyleSheet(
            f"font-size: 22px; font-weight: bold; color: {primary};"
        )
        self.quest_label.setStyleSheet(
            f"font-size: 16px; font-weight: bold; color: {primary};"
        )
        self.hint_label.setStyleSheet(
            f"color: {text_secondary}; font-size: 13px;"
        )
        self.completion_title.setStyleSheet(
            f"font-size: 22px; font-weight: bold; color: {primary};"
        )
        self.completion_subtitle.setStyleSheet(
            f"font-size: 14px; color: {text_secondary}; margin-bottom: 20px;"
        )
        self.path_label.setStyleSheet(
            f"font-size: 12px; color: {text_secondary}; margin-top: 10px;"
        )

    def _on_begin(self):
        # Re-running onboarding starts from the saved answers, so changing one
        # answer doesn't mean typing all the others again.
        self.responses = self.storage.load_saved_responses(self.questions)
        self.current_index = 0
        self._show_question()
        self.stack.setCurrentIndex(1)

    def _on_skip(self):
        self.storage.record_onboarding_declined()
        self._done = True
        self.onboarding_complete.emit(False)
        self.close()

    def _show_question(self):
        q = self.questions[self.current_index]
        n = len(self.questions)
        self.quest_label.setText(f"QUEST {q['id']}/{n} - {q['category']}")
        self.question_label.setText(q["question"])
        self.hint_label.setText(f"Example: {q['hint']}")
        existing = (
            self.responses[self.current_index]
            if self.current_index < len(self.responses)
            else None
        )
        self.answer_input.setText((existing or {}).get("answer", ""))
        self.impact_group.setExclusive(False)
        for btn in self.impact_buttons:
            btn.setChecked(
                bool(existing) and btn.property("impact") == existing.get("impact")
            )
        self.impact_group.setExclusive(True)
        self.back_button.setVisible(self.current_index > 0)
        self.next_button.setEnabled(False)
        self._validate_input()
        if self.current_index == n - 1:
            self.next_button.setText("Finish")
        else:
            self.next_button.setText("Next")
        self.progress_bar.setMaximum(n)
        self.progress_bar.setValue(self.current_index)
        self.progress_bar.setFormat(f"{self.current_index} / {n}")

    def _validate_input(self):
        has_text = len(self.answer_input.text().strip()) >= 3
        has_impact = self.impact_group.checkedId() != -1
        self.next_button.setEnabled(has_text and has_impact)

    def _on_next(self):
        answer = self.answer_input.text().strip().lower()
        impact = self.impact_group.checkedId()
        q = self.questions[self.current_index]

        response = {
            "question_id": q["id"],
            "category": q["category"],
            "question": q["question"],
            "answer": answer,
            "impact": impact,
        }
        if self.current_index < len(self.responses):
            self.responses[self.current_index] = response
        else:
            self.responses.append(response)

        if self.current_index < len(self.questions) - 1:
            self.current_index += 1
            self._show_question()
        else:
            self._show_completion()

    def _on_back(self):
        if self.current_index > 0:
            self.current_index -= 1
            self._show_question()

    def _show_completion(self):
        """Show every answer in an editable table; nothing is saved yet."""
        self.summary_table.setRowCount(len(self.questions))
        for row, q in enumerate(self.questions):
            existing = (
                self.responses[row]
                if row < len(self.responses)
                else {}
            )
            category = QTableWidgetItem(q["category"])
            category.setFlags(category.flags() & ~Qt.ItemFlag.ItemIsEditable)
            answer = QTableWidgetItem(str(existing.get("answer", "")))
            impact = QTableWidgetItem(
                str(existing.get("impact", ""))
            )
            self.summary_table.setItem(row, 0, category)
            self.summary_table.setItem(row, 1, answer)
            self.summary_table.setItem(row, 2, impact)

        high = sum(1 for r in self.responses if r.get("impact") == 3)
        medium = sum(1 for r in self.responses if r.get("impact") == 2)
        low = sum(1 for r in self.responses if r.get("impact") == 1)
        self.stats_label.setText(
            f"Core Attributes:      {high} (High Priority)\n"
            f"Secondary Attributes: {medium} (Medium Priority)\n"
            f"Support Attributes:   {low} (Low Priority)"
        )
        self.path_label.setText(
            f"Profile will be saved to: {self.storage.persona_file}"
        )
        self.stack.setCurrentIndex(2)

    def _on_completion_back(self):
        self.current_index = len(self.questions) - 1
        self._show_question()
        self.stack.setCurrentIndex(1)

    def _on_finish(self):
        responses = []
        for row, q in enumerate(self.questions):
            answer_item = self.summary_table.item(row, 1)
            impact_item = self.summary_table.item(row, 2)
            answer = (answer_item.text() if answer_item else "").strip().lower()
            try:
                impact = int((impact_item.text() if impact_item else "").strip())
            except (TypeError, ValueError):
                impact = 0
            if not answer or len(answer) < 3 or impact not in (1, 2, 3):
                QMessageBox.warning(
                    self,
                    "Incomplete profile",
                    f"Please give question {q['id']} an answer of at least 3 "
                    "characters and an impact of 1, 2 or 3.",
                )
                return
            responses.append(
                {
                    "question_id": q["id"],
                    "category": q["category"],
                    "question": q["question"],
                    "answer": answer,
                    "impact": impact,
                }
            )

        self.responses = responses
        stats = {
            "high_count": sum(1 for r in responses if r["impact"] == 3),
            "medium_count": sum(1 for r in responses if r["impact"] == 2),
            "low_count": sum(1 for r in responses if r["impact"] == 1),
        }
        self.storage.write_persona_profile(responses, stats)
        self._done = True
        self.onboarding_complete.emit(True)
        self.close()

    def closeEvent(self, event):
        if not self._done:
            self._done = True
            self.onboarding_complete.emit(False)
        super().closeEvent(event)


class BatchProposalDialog(QDialog):
    """Dialog for reviewing and confirming multiple tasks from a document upload.

    Non-modal: use show() and connect to confirmed/cancelled signals.
    """

    confirmed = Signal(list)  # list of selected task dicts
    cancelled = Signal()

    def __init__(self, tasks: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Confirm Tasks from Document")
        self.setMinimumSize(700, 500)
        # Non-modal: don't call setModal(True). As a child of the chat panel
        # it already stays above the app window, without covering others.
        # Exactly one of confirmed/cancelled is emitted, however it closes.
        self._decided = False

        tokens = get_current_theme_tokens()
        self._duplicate_tint = self._tint_for(tokens)
        self._duplicate_rows: list[int] = []

        self._original_tasks = tasks
        self._existing_titles = {str(task.get("title", "")) for task in load_tasks()}

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # Header label
        header = QLabel(f"Review {len(tasks)} task(s) extracted from document:")
        self._header = header
        self._style_header(tokens)
        root.addWidget(header)

        # Table widget
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["", "Task Title", "Category", "Deadline (YYYY-MM-DD)"])
        self.table.setRowCount(len(tasks))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(True)

        # Set checkbox column header icon
        header_item = QTableWidgetItem()
        header_item.setIcon(get_icon("complete", size=14))
        self.table.setHorizontalHeaderItem(0, header_item)

        for row, task in enumerate(tasks):
            # Checkbox column
            checkbox_item = QTableWidgetItem()
            checkbox_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            checkbox_item.setCheckState(Qt.Checked)  # Default checked
            self.table.setItem(row, 0, checkbox_item)

            # Title column (editable)
            title = task.get("title", "")
            title_item = QTableWidgetItem(title)
            title_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable)
            self.table.setItem(row, 1, title_item)

            # Category column (editable)
            category = task.get("category", "study")
            category_item = QTableWidgetItem(category)
            category_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable)
            self.table.setItem(row, 2, category_item)

            # Deadline column (editable, validated)
            deadline = task.get("deadline", "") or ""
            deadline_item = QTableWidgetItem(deadline)
            deadline_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable)
            self.table.setItem(row, 3, deadline_item)

            # Highlight duplicates
            if title in self._existing_titles:
                self._duplicate_rows.append(row)
                for col in range(4):
                    item = self.table.item(row, col)
                    if item:
                        item.setBackground(self._duplicate_tint)
                        item.setToolTip("Task with this title already exists — adding will update its deadline")

        root.addWidget(self.table, stretch=1)

        # Buttons row
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        self.select_all_btn = QPushButton()
        self.select_all_btn.setIcon(get_icon("select_all"))
        self.select_all_btn.setText("Select All")
        self.select_all_btn.clicked.connect(self._select_all)
        btn_row.addWidget(self.select_all_btn)

        self.deselect_all_btn = QPushButton()
        self.deselect_all_btn.setIcon(get_icon("deselect_all"))
        self.deselect_all_btn.setText("Deselect All")
        self.deselect_all_btn.clicked.connect(self._deselect_all)
        btn_row.addWidget(self.deselect_all_btn)

        btn_row.addStretch(1)

        # Custom buttons instead of QDialogButtonBox for non-modal behavior
        self.ok_btn = QPushButton()
        self.ok_btn.setIcon(get_icon("confirm_add"))
        self.ok_btn.setText("Add Selected Tasks")
        self.ok_btn.clicked.connect(self._on_accept)
        btn_row.addWidget(self.ok_btn)

        self.cancel_btn = QPushButton()
        self.cancel_btn.setIcon(get_icon("cancel"))
        self.cancel_btn.setText("Cancel")
        self.cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self.cancel_btn)

        root.addLayout(btn_row)

    @staticmethod
    def _tint_for(tokens: dict) -> QColor:
        tint = QColor(tokens.get("sys_color_alert", "#ff6b8a"))
        tint.setAlpha(70)
        return tint

    def _style_header(self, tokens: dict) -> None:
        self._header.setStyleSheet(
            "font-weight: bold; font-size: 13px; "
            f"color: {tokens.get('sys_color_text_main', '#d6e7ff')};"
        )

    def refresh_theme(self, tokens: dict | None = None) -> None:
        """Recolour the header and the duplicate-row tint for a new palette."""
        tokens = tokens or get_current_theme_tokens()
        self._style_header(tokens)
        self._duplicate_tint = self._tint_for(tokens)
        for row in self._duplicate_rows:
            for col in range(self.table.columnCount()):
                item = self.table.item(row, col)
                if item:
                    item.setBackground(self._duplicate_tint)

    def _select_all(self) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(Qt.Checked)

    def _deselect_all(self) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(Qt.Unchecked)

    def _on_accept(self) -> None:
        """Validate all selected rows before emitting confirmed."""
        selected_tasks = self.get_selected_tasks()

        # Validate deadlines for selected tasks
        invalid_deadlines = []
        for row, task in selected_tasks:
            deadline_item = self.table.item(row, 3)
            deadline = deadline_item.text().strip() if deadline_item else ""
            if deadline:
                # Basic format validation
                if not QRegularExpression(r"^\d{4}-\d{2}-\d{2}$").match(deadline).hasMatch():
                    invalid_deadlines.append(f"Row {row + 1}: invalid date format '{deadline}' (expected YYYY-MM-DD)")

        if invalid_deadlines:
            QMessageBox.warning(self, "Invalid Deadlines", "\n".join(invalid_deadlines))
            return

        self._decided = True
        self.confirmed.emit([task for _row, task in selected_tasks])
        self.close()

    def _on_cancel(self) -> None:
        if not self._decided:
            self._decided = True
            self.cancelled.emit()
        self.close()

    def reject(self) -> None:
        """Esc counts as Cancel, so the proposal is recorded as declined."""
        self._on_cancel()

    def closeEvent(self, event) -> None:
        """Closing with the window's X also counts as Cancel."""
        if not self._decided:
            self._decided = True
            self.cancelled.emit()
        super().closeEvent(event)

    def get_selected_tasks(self) -> list[tuple[int, dict]]:
        """Return list of (row_index, task_dict) for checked rows."""
        result = []
        for row in range(self.table.rowCount()):
            checkbox = self.table.item(row, 0)
            if checkbox and checkbox.checkState() == Qt.Checked:
                title_item = self.table.item(row, 1)
                category_item = self.table.item(row, 2)
                deadline_item = self.table.item(row, 3)

                title = title_item.text().strip() if title_item else ""
                category = category_item.text().strip().lower() if category_item else "study"
                deadline = deadline_item.text().strip() if deadline_item else ""

                if title:
                    result.append((
                        row,
                        {
                            "title": title,
                            "category": category,
                            "deadline": deadline if deadline else None,
                            # Which proposed task this row was, so skipped
                            # rows are known even after a title edit.
                            "source_index": row,
                        }
                    ))
        return result
