from PySide6.QtCore import QDate, QSize, Qt, Signal, QTimer, QRegularExpression
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
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
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QHeaderView,
    QMessageBox,
)
from PySide6.QtGui import QRegularExpressionValidator, QColor
from system_sl.frontend.gui.icons import get_icon
from system_sl.core.onboarding import ONBOARDING_QUESTIONS, PersonaStorageHandler
from system_sl.core.tasks import (
    add_tasks,
    edit_task,
    load_tasks,
    mark_task_completed,
    remove_tasks,
)
from system_sl.core.priority_engine_new import (
    load_manual_order,
    run_prioritization,
    ThompsonSampler,
    save_manual_order,
)  # we are importing run_prioritization from api section(priority_engine_new)
from system_sl.frontend.gui.theme import get_current_theme_tokens


# GUI-added tasks no longer carry a user-chosen category — the model assigns one
# later. Until then they land in this default bucket.
DEFAULT_CATEGORY = "general"


class ReorderableTaskList(QListWidget):
    """Task list the user can drag to re-rank.

    Emits ``reordered(moved_text, old_rank, new_rank, new_texts)`` after a drag,
    where the ranks are 1-based positions and ``new_texts`` is the full new
    order of the item labels.
    """

    reordered = Signal(str, int, int, list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.InternalMove)

    def dropEvent(self, event):
        moved_item = self.currentItem()
        if moved_item is None:
            super().dropEvent(event)
            return
        moved_text = moved_item.text()
        old_texts = [self.item(i).text() for i in range(self.count())]
        super().dropEvent(event)
        new_texts = [self.item(i).text() for i in range(self.count())]
        if moved_text not in old_texts or moved_text not in new_texts:
            return
        old_rank = old_texts.index(moved_text) + 1
        new_rank = new_texts.index(moved_text) + 1
        if old_rank != new_rank:
            self.reordered.emit(moved_text, old_rank, new_rank, new_texts)


class TasksWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tasks")
        self.setMinimumSize(QSize(800, 400))

        # Current display order (prioritized task dicts) and a lookup from each
        # item's label back to its task dict, kept in sync by _render().
        self._order = []
        self._text_to_task = {}

        # Status text is split in two so a transient "Added: x" message is not
        # wiped by, nor endlessly appended to, a standing ranking notice.
        self._status_message = ""
        self._priority_note = ""

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Here are your tasks!!"))

        self.tasks_list_widget = ReorderableTaskList()
        self.tasks_list_widget.reordered.connect(self._on_reordered)
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

        self.deadline_checkbox = QCheckBox("Deadline:")
        self.deadline_edit = QDateEdit()
        self.deadline_edit.setCalendarPopup(True)
        self.deadline_edit.setDisplayFormat("yyyy-MM-dd")
        self.deadline_edit.setDate(QDate.currentDate())
        self.deadline_edit.setMinimumDate(QDate.currentDate())
        self.deadline_edit.setEnabled(False)
        self.deadline_checkbox.toggled.connect(self.deadline_edit.setEnabled)

        self.add_button = QPushButton()
        self.add_button.setIcon(get_icon("add"))
        self.add_button.setText("Add")
        self.add_button.clicked.connect(self._on_add)
        add_row.addWidget(self.title_input)
        add_row.addWidget(self.deadline_checkbox)
        add_row.addWidget(self.deadline_edit)
        add_row.addWidget(self.add_button)
        root.addLayout(add_row)

        self.status_label = QLabel("")
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

    def _refresh_list(self):
        # Re-rank from scratch via the prioritization engine, then render.
        result = run_prioritization(
            display=False
        )  # NOTE: To turn on the thompson sampling we also have to make changes here
        self._order = result.get("all_tasks_ranked", [])

        # An empty ranking does not mean the player has no tasks. The ranking
        # depends on the player profile and the bundled NLP model, so when it
        # cannot be produced fall back to the stored list rather than showing a
        # blank window that makes tasks look lost even though they were saved.
        if not self._order:
            self._priority_note = self._explain(result.get("_reason"))
            self._order = self._unranked_tasks()
        elif result.get("_reason"):
            # Ranked, but only partially: point the player at what unlocks the
            # full ordering instead of leaving the degraded score unexplained.
            self._priority_note = (
                f"{result['_reason']} — use 'Edit Player Profile' to improve it"
            )
        else:
            self._priority_note = ""

        self._apply_status()
        self._render(self._order)

    @staticmethod
    def _explain(reason: str | None) -> str:
        """Turn a prioritization failure reason into something a player can act on."""
        if not reason or reason == "No tasks found":
            return ""
        return f"Showing tasks unranked — {reason}"

    def _unranked_tasks(self) -> list:
        """Return every stored task, in manual order, for unranked rendering."""
        try:
            stored = [t for t in load_tasks() if isinstance(t, dict)]
        except Exception as e:
            self._status_message = f"Could not load tasks: {e}"
            return []

        position = {title: i for i, title in enumerate(load_manual_order())}
        return sorted(
            stored,
            key=lambda t: position.get(t.get("title", ""), len(position)),
        )

    def _render(self, tasks):
        """Render ``tasks`` (ordered list of dicts) as a numbered list."""
        self.tasks_list_widget.clear()
        self._text_to_task = {}
        for rank, task in enumerate(tasks, start=1):
            task["rank"] = rank
            title = task.get("title", "")
            deadline = task.get("deadline")
            p_score = task.get("P_final")  # absent when shown unranked
            display = f"{rank}. {title}"
            if deadline:
                display += f"  → due {deadline}"

            # Only claim a pull score when one was actually computed; the
            # unranked fallback would otherwise read as a real score of zero.
            if p_score is not None:
                display += f" [P={p_score:.3f}]"
            item = QListWidgetItem(display)
            # Keep (category, title) so complete/remove still address the right
            # task even though the category is no longer shown.
            # NOTE: removed the category thing
            item.setData(Qt.UserRole, title)
            self.tasks_list_widget.addItem(item)
            self._text_to_task[title] = task

    def _on_reordered(self, moved_text, old_rank, new_rank, new_texts):
        # BUG: REMEBER TO DEAL WITH THIS FUNCTION CAREFULLY

        # new_texts contains display strings, but _text_to_task is keyed by title.
        # Extract titles from the display strings (format: "N. Title [extras]")
        def extract_title(display: str) -> str:
            # Remove leading "N. " and anything after "  →" or " [P="
            title = display
            if ". " in title:
                title = title.split(". ", 1)[1]
            for sep in ("  →", " [P="):
                if sep in title:
                    title = title.split(sep)[0]
            return title.strip()

        new_titles = [extract_title(t) for t in new_texts]
        new_order_dicts = [
            self._text_to_task[t] for t in new_titles if t in self._text_to_task
        ]
        moved_title = extract_title(moved_text)
        moved = self._text_to_task.get(moved_title)

        if moved is None or len(new_order_dicts) != len(new_texts):
            # Safe deferred rendering
            QTimer.singleShot(0, lambda: self._render(self._order))
            return

        moved_title = moved.get("title", "")
        self._order = new_order_dicts

        # 2. HYBRID ACTION A: Save exact layout to disk
        save_manual_order(self._order)

        # 3. HYBRID ACTION B: Silently train the offline AI Bandit
        try:
            ThompsonSampler().record_feedback(moved_title, self._order)
            self._set_status(
                f"Reordered '{moved_title}' (AI silently updated!)"
            )
        except Exception as e:
            self._set_status(f"Reordered (AI train failed: {e})")

        # 4. Render safely outside the drop event loop
        QTimer.singleShot(0, lambda: self._render(self._order))

    def _selected(self):
        item = self.tasks_list_widget.currentItem()
        if item is None:
            self._set_status("Select a task first.")
            return None
        return item.data(Qt.UserRole)

    def _on_complete(self):
        title = self._selected()
        if title is None:
            return
        try:
            mark_task_completed(task_title=title)  # Removed the category thing
            self._set_status(f"Completed: {title}")
        except ValueError as e:
            self._set_status(str(e))
        self._refresh_list()

    def _on_remove(self):
        title = self._selected()
        if title is None:
            return
        try:
            remove_tasks(task_title=title)
            self._set_status(f"Removed: {title}")
        except ValueError as e:
            self._set_status(str(e))
        self._refresh_list()

    def _on_edit(self):
        """Open a dialog to edit the selected task's title and deadline."""
        title = self._selected()
        if title is None:
            return

        task = self._text_to_task.get(title)
        if task is None:
            self._set_status("Could not find task data.")
            return

        current_deadline = task.get("deadline")

        dialog = QDialog(self)
        dialog.setWindowTitle("Edit Task")
        dialog.setModal(True)
        form = QFormLayout(dialog)

        title_edit = QLineEdit(title)
        form.addRow("Title:", title_edit)

        deadline_check = QCheckBox("Set deadline:")
        deadline_edit = QDateEdit()
        deadline_edit.setCalendarPopup(True)
        deadline_edit.setDisplayFormat("yyyy-MM-dd")
        deadline_edit.setEnabled(bool(current_deadline))
        if current_deadline:
            from PySide6.QtCore import QDate
            deadline_edit.setDate(QDate.fromString(current_deadline, "yyyy-MM-dd"))
        deadline_check.toggled.connect(deadline_edit.setEnabled)
        form.addRow(deadline_check, deadline_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        if dialog.exec() != QDialog.Accepted:
            return

        new_title = title_edit.text().strip()
        new_deadline = (
            deadline_edit.date().toString("yyyy-MM-dd")
            if deadline_check.isChecked()
            else None
        )
        if not new_title:
            self._set_status("Title is required.")
            return
        try:
            edit_task(title, new_title, new_deadline)
            self._set_status(f"Edited: {new_title}")
        except ValueError as e:
            self._set_status(str(e))
        self._refresh_list()

    def _on_add(self):
        title = self.title_input.text().strip()
        deadline = (
            self.deadline_edit.date().toString("yyyy-MM-dd")
            if self.deadline_checkbox.isChecked()
            else None
        )
        if not title:
            self._set_status("Title is required.")
            return
        try:
            # Category is assigned by the model later; default for now.
            add_tasks(DEFAULT_CATEGORY,title,  deadline)
            self._set_status(f"Added: {title}")
            self.title_input.clear()
            self.deadline_checkbox.setChecked(False)
            self.deadline_edit.setDate(QDate.currentDate())
        except ValueError as e:
            self._set_status(str(e))
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
        for label in ["Low (1)", "Medium (2)", "High (3)"]:
            btn = QPushButton(label)
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

        self.next_button = QPushButton()
        self.next_button.setIcon(get_icon("next"))
        self.next_button.setText("Next")
        self.next_button.setEnabled(False)
        self.next_button.setMinimumHeight(36)
        self.next_button.clicked.connect(self._on_next)
        layout.addWidget(self.next_button)

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

        self.stats_label = QLabel()
        self.stats_label.setStyleSheet("font-size: 14px;")
        layout.addWidget(self.stats_label)

        self.path_label = QLabel()
        self.path_label.setStyleSheet("font-size: 12px; margin-top: 10px;")
        self.path_label.setWordWrap(True)
        layout.addWidget(self.path_label)

        layout.addStretch(1)

        finish_btn = QPushButton()
        finish_btn.setIcon(get_icon("finish"))
        finish_btn.setText("Begin Journey")
        finish_btn.setMinimumHeight(36)
        finish_btn.clicked.connect(self._on_finish)
        layout.addWidget(finish_btn)

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
        self.responses.clear()
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
        self.answer_input.clear()
        self.impact_group.setExclusive(False)
        for btn in self.impact_buttons:
            btn.setChecked(False)
        self.impact_group.setExclusive(True)
        self.next_button.setEnabled(False)
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

        self.responses.append(
            {
                "question_id": q["id"],
                "category": q["category"],
                "question": q["question"],
                "answer": answer,
                "impact": impact,
            }
        )

        if self.current_index < len(self.questions) - 1:
            self.current_index += 1
            self._show_question()
        else:
            stats = {
                "high_count": sum(1 for r in self.responses if r["impact"] == 3),
                "medium_count": sum(1 for r in self.responses if r["impact"] == 2),
                "low_count": sum(1 for r in self.responses if r["impact"] == 1),
            }
            self.storage.write_persona_profile(self.responses, stats)
            self.stats_label.setText(
                f"Core Attributes:      {stats['high_count']} (High Priority)\n"
                f"Secondary Attributes: {stats['medium_count']} (Medium Priority)\n"
                f"Support Attributes:   {stats['low_count']} (Low Priority)"
            )
            self.path_label.setText(f"Profile saved to: {self.storage.persona_file}")
            self.stack.setCurrentIndex(2)

    def _on_finish(self):
        self._done = True
        self.onboarding_complete.emit(True)
        self.close()

    def closeEvent(self, event):
        if not self._done:
            self._done = True
            self.onboarding_complete.emit(False)
        super().closeEvent(event)


class BatchProposalDialog(QDialog):
    """Dialog for reviewing and confirming multiple tasks from a document upload."""

    def __init__(self, tasks: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Confirm Tasks from Document")
        self.setMinimumSize(700, 500)
        self.setModal(True)

        self._original_tasks = tasks
        self._existing_titles = {str(task.get("title", "")) for task in load_tasks()}

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # Header label
        header = QLabel(f"Review {len(tasks)} task(s) extracted from document:")
        header.setStyleSheet("font-weight: bold; font-size: 13px; color: #d6e7ff;")
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

        # Deadline validator
        deadline_validator = QRegularExpressionValidator(QRegularExpression(r"^\d{4}-\d{2}-\d{2}$|^$"))

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
                for col in range(4):
                    item = self.table.item(row, col)
                    if item:
                        item.setBackground(QColor("#5a3a3a"))  # Reddish tint for duplicates
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

        self.button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.button_box.accepted.connect(self._on_accept)
        self.button_box.rejected.connect(self.reject)
        ok_btn = self.button_box.button(QDialogButtonBox.Ok)
        ok_btn.setIcon(get_icon("confirm_add"))
        ok_btn.setText("Add Selected Tasks")
        btn_row.addWidget(self.button_box)

        root.addLayout(btn_row)

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
        """Validate all selected rows before accepting."""
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

        self.accept()

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
                        }
                    ))
        return result
