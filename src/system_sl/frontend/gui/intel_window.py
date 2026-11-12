"""Daily Intel window for THE SYSTEM.

Aether-styled, card-based briefing with three sections, feedback controls and
a preferences dialog. All external article text is rendered as plain text and
never interpreted as HTML.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from system_sl.frontend.gui.icons import get_icon
from system_sl.frontend.gui.theme import get_current_theme_tokens
from system_sl.intel.briefing import BriefingService, get_intel_service
from system_sl.intel.topics import TOPICS
from system_sl.services.intel_scheduler import IntelRefreshWorker

SECTION_META = (
    ("persona", "01", "Relevant to Your Journey", "Intelligence aligned with your Player Profile and long-term goals."),
    ("tasks", "02", "Relevant to Your Active Quests", "Developments connected to the work currently on your board."),
    ("general", "03", "Beyond Your Horizon", "Important discoveries and industry shifts worth knowing, even outside your path."),
)


def _clean(value: object, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value)
    text = " ".join(text.split())
    return text or fallback


def _format_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "Not yet"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%d %b %Y, %H:%M")
    except (ValueError, TypeError):
        return "Not yet"


def _format_date(value: object) -> str:
    if not isinstance(value, str) or not value:
        return datetime.now().strftime("%A, %d %B %Y")
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.strftime("%A, %d %B %Y")
    except (ValueError, TypeError):
        return value


def _label(text: str, object_name: str, *, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setObjectName(object_name)
    label.setWordWrap(wrap)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    return label


class ArticleCard(QFrame):
    """One article, with explanation, relevance and feedback controls."""

    feedback_requested = Signal(str, str, str)
    quest_requested = Signal(str)

    def __init__(self, entry: dict, service: BriefingService, parent=None) -> None:
        super().__init__(parent)
        self.entry = entry if isinstance(entry, dict) else {}
        self.article = self.entry.get("article") if isinstance(self.entry.get("article"), dict) else {}
        self.score = self.entry.get("score") if isinstance(self.entry.get("score"), dict) else {}
        self.summary = self.entry.get("summary") if isinstance(self.entry.get("summary"), dict) else {}
        self.service = service
        self.setObjectName("intelCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)
        category = _clean(self.article.get("category"), "General Technology")
        badge = _label(category.upper(), "intelBadge", wrap=False)
        badge.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        header.addWidget(badge)
        header.addStretch(1)
        relevance = int(round(float(self.score.get("R") or 0)))
        match = _label(f"{relevance}% relevance", "intelRelevance", wrap=False)
        match.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        header.addWidget(match)
        root.addLayout(header)

        title = _label(_clean(self.article.get("title"), "Untitled update"), "intelTitle")
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(title)

        source = _clean(self.article.get("source_name"), "Unknown source")
        published = _format_timestamp(self.article.get("published_at"))
        meta = _label(f"{source}  ·  {published}", "intelMeta")
        root.addWidget(meta)

        self._add_block(root, "WHAT HAPPENED", _clean(self.summary.get("what_happened")), "intelBody")
        self._add_block(root, "WHY IT MATTERS", _clean(self.summary.get("why_it_matters")), "intelWhy")
        self._add_block(root, "WHAT YOU CAN LEARN", _clean(self.summary.get("what_can_you_learn")), "intelBody")
        self._add_block(root, "POTENTIAL ACTION", _clean(self.summary.get("potential_action")), "intelBody")

        reasons = self.entry.get("explanation") or " · ".join(self.score.get("reasons", [])[:3])
        if reasons:
            root.addWidget(_label(_clean(reasons), "intelReason"))

        actions = QHBoxLayout()
        actions.setSpacing(4)

        read_button = QPushButton("Read")
        read_button.setObjectName("intelAction")
        read_button.setIcon(get_icon("external_link"))
        read_button.setToolTip("Read the original article")
        read_button.setCursor(Qt.CursorShape.PointingHandCursor)
        read_button.clicked.connect(self._open_article)
        actions.addWidget(read_button)

        state = self.service.feedback_state(self.article.get("id", "")) if self.article.get("id") else {}
        for action, label, icon, tooltip in (
            ("like", "Like", "like", "More stories like this"),
            ("dislike", "Dislike", "dislike", "Fewer stories like this"),
            ("save", "Save", "save", "Save for later"),
            ("known", "Known", "known", "Mark as already known"),
        ):
            button = QPushButton(label)
            button.setObjectName("intelAction")
            button.setIcon(get_icon(icon))
            button.setCheckable(True)
            button.setChecked(bool(state.get({"like": "liked", "dislike": "disliked", "save": "saved", "known": "known"}[action])))
            button.setToolTip(tooltip)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, name=action: self._emit_feedback(name))
            actions.addWidget(button)

        mute_button = QPushButton("Mute topic")
        mute_button.setObjectName("intelAction")
        mute_button.setIcon(get_icon("topic"))
        mute_button.setToolTip(f"Show fewer articles about {category}")
        mute_button.setCursor(Qt.CursorShape.PointingHandCursor)
        mute_button.clicked.connect(lambda: self._emit_feedback("topic_not_interested"))
        actions.addWidget(mute_button)

        quest_button = QPushButton("Add quest")
        quest_button.setObjectName("intelAction")
        quest_button.setIcon(get_icon("tasks"))
        quest_button.setToolTip("Add this article as an active quest (asks for confirmation)")
        quest_button.setCursor(Qt.CursorShape.PointingHandCursor)
        quest_button.clicked.connect(self._request_quest)
        actions.addWidget(quest_button)

        actions.addStretch(1)
        root.addLayout(actions)

    def _add_block(self, layout: QVBoxLayout, heading: str, text: str, object_name: str) -> None:
        if not text:
            return
        layout.addWidget(_label(heading, "intelKicker"))
        layout.addWidget(_label(text, object_name))

    def _open_article(self) -> None:
        url = self.article.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            QDesktopServices.openUrl(QUrl(url))

    def _emit_feedback(self, action: str) -> None:
        article_id = self.article.get("id")
        if not isinstance(article_id, str) or not article_id:
            return
        self.feedback_requested.emit(article_id, action, _clean(self.article.get("category")))

    def _request_quest(self) -> None:
        title = _clean(self.article.get("title"))
        if title:
            self.quest_requested.emit(title)


class IntelPreferencesDialog(QDialog):
    """Enable/disable, topics, sources and reset for Daily Intel."""

    def __init__(self, service: BriefingService, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Daily Intel Preferences")
        self.setMinimumSize(640, 560)
        self._topic_checks: dict[str, QCheckBox] = {}
        self._source_checks: dict[str, QCheckBox] = {}

        root = QVBoxLayout(self)
        root.setSpacing(10)

        intro = _label(
            "Choose what the SYSTEM should watch. Personalization from your Player Profile and active quests "
            "is optional; general discovery works on its own.",
            "intelMeta",
        )
        root.addWidget(intro)

        self.enabled_check = QCheckBox("Enable Daily Intel")
        root.addWidget(self.enabled_check)

        root.addWidget(_label("Preferred topics", "settingsHeading"))
        topics_grid = QGridLayout()
        topics_grid.setHorizontalSpacing(8)
        topics_grid.setVerticalSpacing(4)
        for index, topic in enumerate(TOPICS):
            check = QCheckBox(topic)
            self._topic_checks[topic] = check
            topics_grid.addWidget(check, index // 2, index % 2)
        root.addLayout(topics_grid)

        root.addWidget(_label("News sources", "settingsHeading"))
        source_scroll = QScrollArea()
        source_scroll.setWidgetResizable(True)
        source_scroll.setFrameShape(QFrame.Shape.NoFrame)
        source_widget = QWidget()
        source_layout = QVBoxLayout(source_widget)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(4)
        for source in self.service.source_catalog():
            check = QCheckBox(f"{source['name']}  ·  reliability {source['reliability']}")
            check.setToolTip(source.get("url", ""))
            self._source_checks[source["id"]] = check
            source_layout.addWidget(check)
        source_layout.addStretch(1)
        source_scroll.setWidget(source_widget)
        root.addWidget(source_scroll, stretch=1)

        reset_button = QPushButton("Reset News Preferences")
        reset_button.clicked.connect(self._reset_preferences)
        root.addWidget(reset_button, alignment=Qt.AlignmentFlag.AlignLeft)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._reload()

    def _reload(self) -> None:
        config = self.service.get_config()
        self.enabled_check.setChecked(bool(config.get("enabled", True)))
        preferred = set(config.get("preferred_topics", []))
        for topic, check in self._topic_checks.items():
            check.setChecked(topic in preferred)
        catalog = {source["id"]: source for source in self.service.source_catalog()}
        for source_id, check in self._source_checks.items():
            check.setChecked(bool(catalog.get(source_id, {}).get("enabled", True)))

    def _save(self) -> None:
        self.service.set_enabled(self.enabled_check.isChecked())
        self.service.set_preferred_topics(
            [topic for topic, check in self._topic_checks.items() if check.isChecked()]
        )
        for source_id, check in self._source_checks.items():
            self.service.set_source_enabled(source_id, check.isChecked())
        self.accept()

    def _reset_preferences(self) -> None:
        answer = QMessageBox.question(
            self,
            "Reset News Preferences",
            "Clear your likes, dislikes, saved articles and topic choices?\n"
            "Your Player Profile and quests will not be changed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.service.reset_preferences()
            self._reload()


class DailyIntelWindow(QWidget):
    """Main Daily Intel window."""

    persona_requested = Signal()
    tasks_requested = Signal()
    settings_requested = Signal()
    enabled_changed = Signal(bool)

    def __init__(self, service: BriefingService | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Daily Intel")
        self.setMinimumSize(900, 620)
        self.resize(980, 760)

        self.service = service or get_intel_service()
        self._briefing: Optional[dict] = None
        self._worker: Optional[IntelRefreshWorker] = None
        self._saved_only = False
        self._tokens = get_current_theme_tokens()

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        title_block.addWidget(_label("DAILY INTEL", "intelHeading", wrap=False))
        self.date_label = _label(_format_date(None), "intelDate", wrap=False)
        title_block.addWidget(self.date_label)
        self.last_updated_label = _label("Not yet updated", "intelMeta", wrap=False)
        title_block.addWidget(self.last_updated_label)
        header.addLayout(title_block)
        header.addStretch(1)

        self.saved_button = QPushButton("Saved")
        self.saved_button.setObjectName("intelAction")
        self.saved_button.setIcon(get_icon("saved_filter"))
        self.saved_button.setCheckable(True)
        self.saved_button.setToolTip("Show only articles saved for later")
        self.saved_button.clicked.connect(self._toggle_saved)
        header.addWidget(self.saved_button)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setIcon(get_icon("refresh"))
        self.refresh_button.clicked.connect(self._manual_refresh)
        header.addWidget(self.refresh_button)

        self.preferences_button = QPushButton("Sources & Topics")
        self.preferences_button.setIcon(get_icon("settings"))
        self.preferences_button.clicked.connect(self._open_preferences)
        header.addWidget(self.preferences_button)
        root.addLayout(header)

        self.topic_container = QWidget()
        topic_layout = QVBoxLayout(self.topic_container)
        topic_layout.setContentsMargins(0, 0, 0, 0)
        topic_layout.setSpacing(4)
        topic_layout.addWidget(_label("Your topics", "intelMeta", wrap=False))
        self.topic_grid = QGridLayout()
        self.topic_grid.setHorizontalSpacing(4)
        self.topic_grid.setVerticalSpacing(4)
        topic_layout.addLayout(self.topic_grid)
        root.addWidget(self.topic_container)
        self._topic_buttons: dict[str, QPushButton] = {}
        self._topic_timer = QTimer(self)
        self._topic_timer.setSingleShot(True)
        self._topic_timer.setInterval(450)
        self._topic_timer.timeout.connect(self._rebuild_after_topic_change)
        self._refresh_topic_chips()

        self.suggestion = QFrame()
        self.suggestion.setObjectName("intelSuggestion")
        suggestion_layout = QHBoxLayout(self.suggestion)
        suggestion_layout.setContentsMargins(10, 8, 10, 8)
        suggestion_layout.setSpacing(8)
        self.suggestion_label = _label("", "intelSuggestionText")
        suggestion_layout.addWidget(self.suggestion_label, stretch=1)
        persona_button = QPushButton("Build Persona")
        persona_button.setObjectName("intelAction")
        persona_button.setIcon(get_icon("profile"))
        persona_button.clicked.connect(self.persona_requested.emit)
        suggestion_layout.addWidget(persona_button)
        quests_button = QPushButton("Open Quests")
        quests_button.setObjectName("intelAction")
        quests_button.setIcon(get_icon("tasks"))
        quests_button.clicked.connect(self.tasks_requested.emit)
        suggestion_layout.addWidget(quests_button)
        self.suggestion.setVisible(False)
        root.addWidget(self.suggestion)

        self.warning_label = _label("", "intelWarning")
        self.warning_label.setVisible(False)
        root.addWidget(self.warning_label)

        self.progress = QProgressBar()
        self.progress.setObjectName("intelProgress")
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.state_label = _label("", "intelState")
        self.state_label.setVisible(False)
        root.addWidget(self.state_label)

        self.state_actions = QWidget()
        state_layout = QHBoxLayout(self.state_actions)
        state_layout.setContentsMargins(0, 0, 0, 0)
        state_layout.setSpacing(6)
        self.retry_button = QPushButton("Retry")
        self.retry_button.setObjectName("intelAction")
        self.retry_button.setIcon(get_icon("refresh"))
        self.retry_button.clicked.connect(self._manual_refresh)
        self.enable_button = QPushButton("Enable Daily Intel")
        self.enable_button.setObjectName("intelAction")
        self.enable_button.setIcon(get_icon("success"))
        self.enable_button.clicked.connect(self._enable_daily_intel)
        self.state_preferences_button = QPushButton("Sources & Topics")
        self.state_preferences_button.setObjectName("intelAction")
        self.state_preferences_button.setIcon(get_icon("settings"))
        self.state_preferences_button.clicked.connect(self._open_preferences)
        state_layout.addWidget(self.retry_button)
        state_layout.addWidget(self.enable_button)
        state_layout.addWidget(self.state_preferences_button)
        state_layout.addStretch(1)
        self.state_actions.setVisible(False)
        root.addWidget(self.state_actions)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(14)
        self.content_layout.addStretch(1)
        self.scroll.setWidget(self.content)
        root.addWidget(self.scroll, stretch=1)

        self._load_initial()

    # ------------------------------------------------------------------
    # Loading and rendering
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """Re-read configuration and cached data (used after Settings changes)."""
        self._refresh_topic_chips()
        if not self.service.get_config().get("enabled", True):
            self._briefing = None
            self._show_disabled()
            return
        self._load_initial()

    def _load_initial(self) -> None:
        config = self.service.get_config()
        if not config.get("enabled", True):
            self._show_disabled()
            return
        cached = self.service.get_cached_briefing()
        if cached:
            self._render(cached)
            if cached.get("date") != self.service.today():
                self._start_refresh(force=False, allow_network=True)
            return
        self._show_loading()
        self._start_refresh(force=False, allow_network=True)

    def _show_loading(self) -> None:
        self.warning_label.setVisible(False)
        self.state_label.setText("Consulting trusted sources and preparing your briefing…")
        self.state_label.setVisible(True)
        self.progress.setVisible(True)
        self._set_state_buttons(retry=False, enable=False, preferences=False)
        self.scroll.setVisible(False)
        self.suggestion.setVisible(False)

    def _show_disabled(self) -> None:
        self._clear_content()
        self.progress.setVisible(False)
        self.warning_label.setVisible(False)
        self.state_label.setText(
            "Daily Intel is turned off. Enable it to receive a daily briefing from trusted sources."
        )
        self.state_label.setVisible(True)
        self._set_state_buttons(retry=False, enable=True, preferences=True)
        self.scroll.setVisible(False)
        self.suggestion.setVisible(False)
        self.saved_button.setEnabled(False)

    def _show_empty(self, message: str) -> None:
        self._clear_content()
        self.progress.setVisible(False)
        self.state_label.setText(message)
        self.state_label.setVisible(True)
        self._set_state_buttons(retry=True, enable=False, preferences=True)
        self.scroll.setVisible(False)

    def _show_error(self, message: str) -> None:
        self.progress.setVisible(False)
        self.state_label.setText(f"Daily Intel could not be prepared. {message}")
        self.state_label.setVisible(True)
        self._set_state_buttons(retry=True, enable=False, preferences=True)

    def _set_state_buttons(self, *, retry: bool, enable: bool, preferences: bool) -> None:
        self.retry_button.setVisible(retry)
        self.enable_button.setVisible(enable)
        self.state_preferences_button.setVisible(preferences)
        self.state_actions.setVisible(retry or enable or preferences)

    def _render(self, briefing: dict) -> None:
        self._briefing = briefing
        self.saved_button.setEnabled(True)
        state = briefing.get("state", "ok")
        if state == "disabled":
            self._show_disabled()
            return

        self.date_label.setText(_format_date(briefing.get("date")))
        self.last_updated_label.setText(f"Last updated: {_format_timestamp(briefing.get('generated_at'))}")

        warnings = [warning for warning in briefing.get("warnings", []) if isinstance(warning, str) and warning]
        if briefing.get("date") and briefing.get("date") != self.service.today():
            warnings.insert(0, f"Showing the last successful briefing from {briefing.get('date')}.")
        if warnings:
            self.warning_label.setText("  ·  ".join(dict.fromkeys(warnings)))
            self.warning_label.setVisible(True)
        else:
            self.warning_label.setVisible(False)

        sections = briefing.get("sections") if isinstance(briefing.get("sections"), dict) else {}
        entries_by_section = {
            key: self._visible_entries(sections.get(key, [])) for key, _, _, _ in SECTION_META
        }
        total = sum(len(entries) for entries in entries_by_section.values())
        if total == 0:
            message = "No suitable intelligence found for your current preferences."
            if self._saved_only:
                message = "No saved articles in this briefing yet."
            self._show_empty(message)
            self._update_suggestion(briefing.get("meta", {}))
            return

        self._clear_content()
        self.progress.setVisible(False)
        self.state_label.setVisible(False)
        self.state_actions.setVisible(False)
        self.scroll.setVisible(True)

        for key, number, title, subtitle in SECTION_META:
            entries = entries_by_section.get(key, [])
            if not entries:
                continue
            self.content_layout.addWidget(_label(f"{number}. {title}", "intelSection"))
            self.content_layout.addWidget(_label(subtitle, "intelSectionSub"))
            for entry in entries:
                card = ArticleCard(entry, self.service)
                card.feedback_requested.connect(self._on_feedback)
                card.quest_requested.connect(self._on_add_quest)
                self.content_layout.addWidget(card)
        self.content_layout.addStretch(1)
        self._update_suggestion(briefing.get("meta", {}))

    def _visible_entries(self, entries: object) -> list[dict]:
        if not isinstance(entries, list):
            return []
        visible: list[dict] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            article = entry.get("article") if isinstance(entry.get("article"), dict) else {}
            article_id = article.get("id")
            topic = article.get("category")
            if article_id and self.service.is_hidden(article_id, topic):
                continue
            if self._saved_only:
                if not article_id or not self.service.feedback_state(article_id).get("saved"):
                    continue
            visible.append(entry)
        return visible

    def _clear_content(self) -> None:
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _update_suggestion(self, meta: dict) -> None:
        meta = meta if isinstance(meta, dict) else {}
        has_persona = bool(meta.get("persona"))
        has_tasks = bool(meta.get("tasks"))
        if has_persona and has_tasks:
            self.suggestion.setVisible(False)
            return

        if not has_persona and not has_tasks:
            text = "Add your persona and active quests to receive personalized intelligence."
        elif not has_persona:
            text = "Add your persona to align this briefing with your long-term goals."
        else:
            text = "Add active quests to connect intelligence to the work in front of you."
        self.suggestion_label.setText(text)
        self.suggestion.setVisible(True)

    # ------------------------------------------------------------------
    # Refresh and worker lifecycle
    # ------------------------------------------------------------------

    def _start_refresh(self, *, force: bool, allow_network: bool) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        if self._briefing is None:
            self._show_loading()
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("Refreshing…")
        self.progress.setVisible(True)

        worker = IntelRefreshWorker(self.service, force=force, allow_network=allow_network)
        worker.result.connect(self._on_result)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_finished)
        self._worker = worker
        worker.start()

    def _on_result(self, briefing) -> None:
        if isinstance(briefing, dict):
            self._render(briefing)

    def _on_failed(self, message: str) -> None:
        if self._briefing is not None:
            current = list(self._briefing.get("warnings", []))
            current.append(f"Refresh failed: {message}")
            self._briefing = {**self._briefing, "warnings": current}
            self._render(self._briefing)
        else:
            self._show_error(message)

    def _on_finished(self) -> None:
        worker = self._worker
        self._worker = None
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("Refresh")
        self.progress.setVisible(False)
        if worker is not None:
            worker.deleteLater()

    def _manual_refresh(self) -> None:
        if not self.service.get_config().get("enabled", True):
            self._show_disabled()
            return
        self._start_refresh(force=True, allow_network=True)

    def handle_background_briefing(self, briefing: dict) -> None:
        """Accept a briefing generated by the daily scheduler."""
        if not isinstance(briefing, dict):
            return
        if self._worker is not None and self._worker.isRunning():
            return
        if self.service.get_config().get("enabled", True):
            self._render(briefing)

    # ------------------------------------------------------------------
    # Topics
    # ------------------------------------------------------------------

    def _refresh_topic_chips(self) -> None:
        while self.topic_grid.count():
            item = self.topic_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._topic_buttons = {}
        preferred = set(self.service.get_config().get("preferred_topics", []))
        for index, topic in enumerate(TOPICS):
            button = QPushButton(topic)
            button.setObjectName("intelChip")
            button.setCheckable(True)
            button.setChecked(topic in preferred)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda checked, name=topic: self._on_topic_toggled(name, checked))
            self._topic_buttons[topic] = button
            self.topic_grid.addWidget(button, index // 4, index % 4)

    def _on_topic_toggled(self, topic: str, checked: bool) -> None:
        config = self.service.get_config()
        topics = set(config.get("preferred_topics", []))
        if checked:
            topics.add(topic)
        else:
            topics.discard(topic)
        self.service.set_preferred_topics(topics)
        self._topic_timer.start()

    def _rebuild_after_topic_change(self) -> None:
        if not self.service.get_config().get("enabled", True):
            return
        if self._briefing is not None:
            self._start_refresh(force=True, allow_network=False)
        else:
            self._start_refresh(force=False, allow_network=True)

    def _toggle_saved(self) -> None:
        self._saved_only = bool(self.saved_button.isChecked())
        if self._briefing is not None:
            self._render(self._briefing)

    # ------------------------------------------------------------------
    # Feedback and preferences
    # ------------------------------------------------------------------

    def _on_feedback(self, article_id: str, action: str, topic: str) -> None:
        try:
            self.service.record_feedback(article_id, action, topic=topic or None)
        except Exception as exc:  # noqa: BLE001 - feedback is never worth a crash
            self.warning_label.setText(f"Could not save feedback: {exc}")
            self.warning_label.setVisible(True)
            return

        if action == "topic_not_interested" and topic:
            config = self.service.get_config()
            topics = [item for item in config.get("preferred_topics", []) if item != topic]
            if len(topics) != len(config.get("preferred_topics", [])):
                self.service.set_preferred_topics(topics)
                self._refresh_topic_chips()
        if self._briefing is not None:
            self._render(self._briefing)

    def _open_preferences(self) -> None:
        dialog = IntelPreferencesDialog(self.service, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._refresh_topic_chips()
            if not self.service.get_config().get("enabled", True):
                self._show_disabled()
                self.enabled_changed.emit(False)
                return
            self.enabled_changed.emit(True)
            self._start_refresh(force=True, allow_network=True)

    def _on_add_quest(self, title: str) -> None:
        """Add an article as a quest, only after explicit confirmation."""
        if not isinstance(title, str) or not title.strip():
            return
        answer = QMessageBox.question(
            self,
            "Add Quest",
            f"Add this article as an active quest?\n\n{title.strip()}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        from system_sl.core.tasks import add_tasks

        try:
            add_tasks("general", title.strip())
        except ValueError:
            QMessageBox.information(
                self,
                "Add Quest",
                "A quest with this title already exists.",
            )
            return
        except Exception as exc:  # noqa: BLE001 - a quest failure must not crash Intel
            QMessageBox.warning(
                self,
                "Add Quest",
                f"The quest could not be added: {exc}",
            )
            return
        QMessageBox.information(
            self,
            "Add Quest",
            "Quest added. It will influence tomorrow's briefing.",
        )

    def _enable_daily_intel(self) -> None:
        self.service.set_enabled(True)
        self.enabled_changed.emit(True)
        self._refresh_topic_chips()
        self._start_refresh(force=False, allow_network=True)

    def shutdown(self, wait_ms: int = 3000) -> None:
        """Give a running refresh a chance to finish before the window goes away."""
        self._topic_timer.stop()
        worker = self._worker
        if worker is None:
            return
        if worker.isRunning():
            worker.requestInterruption()
            # It is a daemon thread: if a request is still in flight, the
            # process can exit without Qt aborting on a running QThread.
            worker.wait(wait_ms)
        else:
            worker.wait(0)

    def closeEvent(self, event):
        # Workers are daemon threads, so closing the window can let an
        # in-flight request finish in the background. Application shutdown
        # asks for a longer grace period.
        self.shutdown(wait_ms=250)
        super().closeEvent(event)

    def refresh_theme(self, tokens: dict | None = None) -> None:
        self._tokens = tokens or get_current_theme_tokens()
        self.update()
