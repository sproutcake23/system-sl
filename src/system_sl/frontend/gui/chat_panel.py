"""Chat panel widget for System-SL GUI."""

import logging
import re
import html
from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTextEdit,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QMessageBox,
    QDialog,
    QFormLayout,
    QDialogButtonBox,
    QCheckBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
    QProgressBar,
    QFrame,
    QFileDialog,
    QSizePolicy,
    QDoubleSpinBox,
    QSpinBox,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QTextBlockFormat,
    QTextBlockUserData,
    QTextCharFormat,
    QTextCursor,
    QTextDocumentFragment,
    QDragEnterEvent,
    QDropEvent,
)

# --- Dynamic Theming Utilities ---
from system_sl.core import add_tasks, load_tasks
from system_sl.frontend.gui.theme import get_current_theme_tokens
from system_sl.frontend.gui.icons import get_icon
from system_sl.utils.i18n import tr

# Lazy-loaded chatbot imports (moved to functions to avoid startup cost)
# from system_sl.chatbot import ...
# from system_sl.chatbot.llm_factory import LLMFactoryError

if TYPE_CHECKING:
    from system_sl.chatbot import ChatConfig, ChatSession, ModelInfo
    from system_sl.chatbot.llm_factory import ProviderPreset
    from system_sl.chatbot.transcript import ChatMessage

logger = logging.getLogger(__name__)


def _colorize_markdown_html(body: str, color: str) -> str:
    """Give every markdown element an explicit theme colour.

    Wrapping the whole body in one ``<span>`` makes Qt's rich-text parser fold
    the trailing list into the answer paragraph (the label ends up bulleted),
    so the colour is applied per style attribute instead.
    """

    def add_color(match: re.Match) -> str:
        style = match.group(1)
        if "color:" in style:
            return match.group(0)  # the [SYSTEM] label keeps its own colour
        return f'style="color:{color};{style}"'

    return re.sub(r'style="([^"]*)"', add_color, body)


# =============================================================================
# MODEL FETCH WORKER
# =============================================================================

_MD_BLOCK_PREFIXES = ("- ", "* ", "+ ", "> ", "|", "#")
_MD_ORDERED = re.compile(r"^\d+\.\s")

_DOT_COUNT = 4
_DOT_CHAR = "•"


def _preserve_line_breaks(text: str) -> str:
    """Turn single newlines into markdown hard breaks.

    Standard markdown collapses a lone newline into a space, which would merge
    consecutive lines the model wrote (task lists, addresses) into one
    paragraph. Chat replies are written expecting the newline to show, so each
    continuation line gets the two trailing spaces that mean "hard break".
    Fenced code and block constructs are left untouched.
    """
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue

        following = lines[i + 1].strip() if i + 1 < len(lines) else ""
        continues = (
            bool(stripped)
            and bool(following)
            and not stripped.startswith(_MD_BLOCK_PREFIXES)
            and not following.startswith(_MD_BLOCK_PREFIXES)
            and not _MD_ORDERED.match(stripped)
            and not _MD_ORDERED.match(following)
        )
        out.append(line.rstrip() + "  " if continues else line)

    return "\n".join(out)

# Spin-box value shown as "Model default" (temperature left out of requests).
_TEMPERATURE_DEFAULT = -0.1


# Background threads whose owner went away while they were still blocked in
# I/O (an HTTP request cannot be interrupted). They are detached from the
# owner and kept referenced here until they finish, instead of being destroyed
# while running ("QThread: Destroyed while thread is still running").
_retired_threads: set = set()


def _retire_thread(thread: "QThread | None", worker: "QObject | None", wait_ms: int = 200) -> None:
    """Stop using a worker thread without blocking the GUI or crashing.

    The caller must already have dropped its own references, so late results
    are ignored (its slots check ``sender()``).
    """
    if thread is None:
        return
    thread.requestInterruption()
    thread.quit()
    if thread.wait(wait_ms):
        if worker is not None:
            worker.deleteLater()
        thread.deleteLater()
        return

    # Still running: let it finish on its own, then clean up.
    thread.setParent(None)
    _retired_threads.add(thread)

    def _finished() -> None:
        _retired_threads.discard(thread)
        if worker is not None:
            worker.deleteLater()
        thread.deleteLater()

    thread.finished.connect(_finished)


class _ModelFetchWorker(QObject):
    """Background worker for fetching models from provider API."""
    finished = Signal(list)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, provider: str, api_key: str, base_url: str):
        super().__init__()
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url

    def run(self):
        from system_sl.chatbot import fetch_models_for_provider, ModelFetcherError
        try:
            self.progress.emit("Fetching models...")
            models = fetch_models_for_provider(self.provider, self.api_key, self.base_url)
            self.finished.emit(models)
        except ModelFetcherError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class _ClickableLabel(QLabel):
    """A label that reports left clicks.

    Defined here rather than imported from main.py: main.py imports this
    module, so the dependency cannot be reversed.
    """
    clicked = Signal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _MessageBlockData(QTextBlockUserData):
    """Tag on a transcript block: which message this text belongs to.

    The browser is a view of the transcript model, so a click, a selection or
    a context menu has to be mapped back to a message. Every block of a
    message carries its id, which survives markdown rendering (one message can
    become many blocks: paragraphs, list items, headings).
    """

    def __init__(self, message_id: str):
        super().__init__()
        self.message_id = message_id


def _truncate_preview(text: str, limit: int = 60) -> str:
    """Shorten text for a chip, keeping it on one line."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "…"


class _ModelDiscoveryWorker(QObject):
    """Background worker that probes local model servers.

    Probing is slow: the Ollama CLI gets a 5s timeout before the HTTP
    fallback tries for another 3s, and LM Studio takes 3s more. Running
    that on the GUI thread freezes the window for up to eleven seconds, so
    it happens here instead.
    """
    finished = Signal(list)
    failed = Signal(str)

    def run(self):
        from system_sl.chatbot import discover_local_models
        try:
            self.finished.emit(discover_local_models())
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class _TestConnectionWorker(QObject):
    """Background worker that pings the configured LLM.

    A ping against a cold local model can take tens of seconds, so the
    round trip must not happen on the GUI thread.
    """
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, provider: str, model: str, base_url: str | None, api_key: str):
        super().__init__()
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.api_key = api_key

    def run(self):
        from system_sl.chatbot.llm_factory import create_llm
        try:
            llm = create_llm(
                provider=self.provider,
                model=self.model,
                base_url=self.base_url,
                api_key=self.api_key,
            )
            resp = llm.invoke([{"role": "user", "content": "ping"}])
            content = getattr(resp, "content", "")
            if not isinstance(content, str):
                content = str(content)
            self.finished.emit(content[:100])
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


# =============================================================================
# SETTINGS DIALOG
# =============================================================================

class LLMSettingsPage(QWidget):
    """LLM provider and model settings form with model fetching."""

    saved = Signal()
    finished = Signal(bool)  # True for OK after a successful save, False for Cancel

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        from system_sl.chatbot import load_config
        self._config = load_config()
        self._fetched_models: "list[ModelInfo]" = []
        self._fetch_thread: QThread | None = None
        self._fetch_worker: _ModelFetchWorker | None = None
        self._test_thread: QThread | None = None
        self._test_worker: "_TestConnectionWorker | None" = None

        self._build_ui()
        self._load_current_config()

    def _build_ui(self) -> None:
        from system_sl.chatbot import list_preset_items
        layout = QVBoxLayout(self)

        # --- Provider & API Key Section ---
        form = QFormLayout()

        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Custom", "custom")
        for preset_key, preset in list_preset_items():
            # "Custom" is already item 0; adding the custom preset again would
            # make findData("custom") ambiguous.
            if preset_key == "custom":
                continue
            # Store the preset key, not the display name: get_preset() looks up
            # by key, and the two differ ("LM Studio (Local)" vs "lm_studio").
            self.preset_combo.addItem(preset.name, preset_key)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        form.addRow("Provider:", self.preset_combo)

        # Base URL
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("https://api.example.com/v1")
        form.addRow("Base URL:", self.base_url_edit)

        # API Key row with fetch button
        key_row = QHBoxLayout()
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setPlaceholderText("Enter API key (or dummy for local)")
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        key_row.addWidget(self.api_key_edit, stretch=1)

        self.show_key_check = QCheckBox("Show")
        self.show_key_check.toggled.connect(
            lambda checked: self.api_key_edit.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password
            )
        )
        key_row.addWidget(self.show_key_check)

        self.fetch_btn = QPushButton()
        self.fetch_btn.setIcon(get_icon("refresh"))
        self.fetch_btn.setText("Fetch Models")
        self.fetch_btn.setToolTip("Fetch available models from provider API")
        self.fetch_btn.setAccessibleName("Fetch models")
        self.fetch_btn.setAccessibleDescription("Retrieve the list of available models from the configured provider API")
        self.fetch_btn.clicked.connect(self._fetch_models)
        key_row.addWidget(self.fetch_btn)

        form.addRow("API Key:", key_row)

        # Model selection (manual entry, used if no model selected from table)
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("e.g., llama3.1, gemini-3.5-flash-lite (or select from table below)")
        self.model_edit.setAccessibleName("Model name")
        self.model_edit.setAccessibleDescription("Enter a model identifier manually, or select from the table below")
        form.addRow("Model:", self.model_edit)

        # Temperature
        self.temperature_spin = QDoubleSpinBox()
        # The lowest value stands for "model default": no temperature is sent,
        # which some (mostly reasoning) models require.
        self.temperature_spin.setRange(_TEMPERATURE_DEFAULT, 2.0)
        self.temperature_spin.setSingleStep(0.1)
        self.temperature_spin.setDecimals(2)
        self.temperature_spin.setSpecialValueText("Model default")
        self.temperature_spin.setToolTip(
            "Sampling temperature (0.0 = deterministic, higher = more creative). "
            "\"Model default\" sends none, for models that reject it."
        )
        self.temperature_spin.setAccessibleName("Temperature")
        self.temperature_spin.setAccessibleDescription("Controls randomness in model outputs. 0.0 for consistent answers, higher for more creative responses")
        form.addRow("Temperature:", self.temperature_spin)

        # Max output tokens
        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(0, 1000000)
        self.max_tokens_spin.setSpecialValueText("Model default")
        self.max_tokens_spin.setToolTip("Maximum tokens in the model's reply (0 = model default)")
        self.max_tokens_spin.setAccessibleName("Max output tokens")
        self.max_tokens_spin.setAccessibleDescription("Limit the length of model responses. 0 means use the model's default limit")
        form.addRow("Max tokens:", self.max_tokens_spin)

        # Custom system prompt addendum
        self.system_prompt_addendum_edit = QTextEdit()
        self.system_prompt_addendum_edit.setPlaceholderText("Additional instructions to append to the system prompt (optional)")
        self.system_prompt_addendum_edit.setMaximumHeight(80)
        self.system_prompt_addendum_edit.setToolTip("Extra guidance for the model, appended to the built-in system prompt")
        self.system_prompt_addendum_edit.setAccessibleName("Custom system prompt addendum")
        self.system_prompt_addendum_edit.setAccessibleDescription("Additional instructions that will be appended to the system prompt")
        form.addRow("System prompt addendum:", self.system_prompt_addendum_edit)

        # Test Connection Button
        self.test_btn = QPushButton()
        self.test_btn.setIcon(get_icon("info"))
        self.test_btn.setText("Test Connection")
        self.test_btn.setAccessibleName("Test connection")
        self.test_btn.setAccessibleDescription("Send a test request to verify the LLM configuration works")
        self.test_btn.clicked.connect(self._test_connection)
        form.addRow("", self.test_btn)

        layout.addLayout(form)

        # --- Model Table Section ---
        self.model_table_frame = QFrame()
        self.model_table_frame.setFrameShape(QFrame.StyledPanel)
        table_layout = QVBoxLayout(self.model_table_frame)

        # Search/filter row
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter models...")
        self.search_edit.setAccessibleName("Model search")
        self.search_edit.setAccessibleDescription("Filter the model table by name or provider")
        self.search_edit.textChanged.connect(self._filter_models)
        search_row.addWidget(self.search_edit, stretch=1)

        self.select_free_btn = QPushButton()
        self.select_free_btn.setIcon(get_icon("select_all"))
        self.select_free_btn.setText("Select All Free")
        self.select_free_btn.setAccessibleName("Select all free models")
        self.select_free_btn.setAccessibleDescription("Check all free models in the filtered list")
        self.select_free_btn.clicked.connect(self._select_all_free)
        self.select_free_btn.setEnabled(False)
        search_row.addWidget(self.select_free_btn)

        self.refresh_cache_btn = QPushButton()
        self.refresh_cache_btn.setIcon(get_icon("refresh"))
        self.refresh_cache_btn.setText("Refresh Cache")
        self.refresh_cache_btn.setAccessibleName("Refresh model cache")
        self.refresh_cache_btn.setAccessibleDescription("Force a fresh fetch of models from the provider, ignoring the local cache")
        self.refresh_cache_btn.clicked.connect(self._refresh_cache)
        self.refresh_cache_btn.setEnabled(False)
        search_row.addWidget(self.refresh_cache_btn)

        table_layout.addLayout(search_row)

        # Model table
        self.model_table = QTableWidget()
        self.model_table.setColumnCount(5)
        self.model_table.setHorizontalHeaderLabels(["✓", "Model", "Provider", "Context", "Price"])
        self.model_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.model_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.model_table.setAlternatingRowColors(True)
        self.model_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.model_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.model_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.model_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.model_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.model_table.verticalHeader().setVisible(False)
        self.model_table.setShowGrid(False)
        self.model_table.itemChanged.connect(self._on_model_selection_changed)
        table_layout.addWidget(self.model_table)

        layout.addWidget(self.model_table_frame, stretch=1)
        self.model_table_frame.setVisible(False)  # Hidden until fetch

        # Outside the table frame, which a fetch hides while this is showing.
        self.fetch_progress = QProgressBar()
        self.fetch_progress.setRange(0, 0)  # Indeterminate
        layout.addWidget(self.fetch_progress)
        self.fetch_progress.setVisible(False)

        # --- Buttons ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Apply
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(lambda: self.finished.emit(False))
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self._on_apply)
        layout.addWidget(buttons)

    def reload(self) -> None:
        """Discard unsaved edits and show the saved config again."""
        from system_sl.chatbot import load_config
        self._config = load_config()
        self._fetched_models = []
        self.model_table_frame.setVisible(False)
        self._load_current_config()

    def _load_current_config(self) -> None:
        """Populate fields from current config."""
        from system_sl.chatbot import list_preset_items, get_model_cache
        # Find matching preset
        preset_key = "custom"
        for key, preset in list_preset_items():
            if key == "custom":
                continue
            if (preset.provider == self._config.provider and
                preset.base_url == self._config.base_url):
                preset_key = key
                break

        idx = self.preset_combo.findData(preset_key)
        if idx >= 0:
            self.preset_combo.setCurrentIndex(idx)
        else:
            self.preset_combo.setCurrentIndex(0)  # Custom

        self.base_url_edit.setText(self._config.base_url or "")
        self.api_key_edit.setText(self._config.api_key or "")
        self.model_edit.setText(self._config.system_model or "")
        temperature = self._config.temperature
        self.temperature_spin.setValue(
            _TEMPERATURE_DEFAULT if temperature is None else temperature
        )
        self.max_tokens_spin.setValue(self._config.max_tokens or 0)
        self.system_prompt_addendum_edit.setPlainText(self._config.system_prompt_addendum or "")

        # Try to load cached models for current provider (detect from base_url if needed)
        fetch_provider = self._get_fetch_provider()
        if fetch_provider in ("openrouter", "ollama", "lm_studio"):
            cached = get_model_cache(fetch_provider)
            if cached:
                self._fetched_models = cached
                self._populate_model_table()
                self.model_table_frame.setVisible(True)

    def _on_preset_changed(self, index: int) -> None:
        """Auto-fill fields when preset changes."""
        from system_sl.chatbot import get_preset
        preset_key = self.preset_combo.currentData()
        if preset_key == "custom":
            self.base_url_edit.setEnabled(True)
            self.api_key_edit.setEnabled(True)
            self.model_edit.setEnabled(True)
            self.fetch_btn.setEnabled(True)
            return

        preset = None
        if preset_key:
            preset = get_preset(preset_key)

        if preset:
            self.base_url_edit.setText(preset.base_url or "")
            self.base_url_edit.setEnabled(preset.base_url == "")
            self.api_key_edit.setText(preset.api_key or "")
            self.api_key_edit.setEnabled(preset.api_key == "")
            self.model_edit.setText(preset.default_model or "")
            self.model_edit.setEnabled(True)
            self.fetch_btn.setEnabled(preset_key in ("openrouter", "ollama", "lm_studio"))

    def _get_current_preset(self) -> "Optional[ProviderPreset]":
        from system_sl.chatbot import get_preset
        preset_key = self.preset_combo.currentData()
        if preset_key and preset_key != "custom":
            return get_preset(preset_key)
        return None

    def _fetch_models(self) -> None:
        """Fetch models from provider API in background."""
        fetch_provider = self._get_fetch_provider()
        api_key = self.api_key_edit.text().strip()
        base_url = self.base_url_edit.text().strip()

        if fetch_provider not in ("openrouter", "ollama", "lm_studio"):
            QMessageBox.warning(self, "Not Supported", f"Model fetching not supported for {fetch_provider}")
            return

        if not api_key:
            QMessageBox.warning(self, "Missing API Key", "Please enter an API key first")
            return

        if not base_url:
            QMessageBox.warning(self, "Missing Base URL", "Please enter a base URL first")
            return

        # Disable UI during fetch
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("Fetching...")
        self.fetch_progress.setVisible(True)
        self.model_table_frame.setVisible(False)

        # Start background fetch
        self._fetch_thread = QThread(self)
        self._fetch_worker = _ModelFetchWorker(fetch_provider, api_key, base_url)
        self._fetch_worker.moveToThread(self._fetch_thread)
        self._fetch_thread.started.connect(self._fetch_worker.run)
        self._fetch_worker.finished.connect(self._on_models_fetched)
        self._fetch_worker.failed.connect(self._on_fetch_failed)
        # A method rather than a lambda, so Qt delivers it on the GUI thread.
        self._fetch_worker.progress.connect(self._on_fetch_progress)
        self._fetch_worker.finished.connect(self._fetch_thread.quit)
        self._fetch_worker.failed.connect(self._fetch_thread.quit)
        thread = self._fetch_thread
        thread.finished.connect(lambda: self._cleanup_fetch_thread(thread))
        thread.start()

    def _on_fetch_progress(self, message: str) -> None:
        if self.sender() is not self._fetch_worker:
            return  # a fetch abandoned when the dialog closed
        self.fetch_btn.setText(message)

    def _on_models_fetched(self, models: "list[ModelInfo]") -> None:
        """Handle successful model fetch."""
        from system_sl.chatbot import set_model_cache

        if self.sender() is not self._fetch_worker:
            return
        self._fetched_models = models
        self._populate_model_table()
        self.model_table_frame.setVisible(True)

        # Cache the models
        fetch_provider = self._get_fetch_provider()
        set_model_cache(fetch_provider, models)

        self.select_free_btn.setEnabled(True)
        self.refresh_cache_btn.setEnabled(True)

    def _on_fetch_failed(self, error: str) -> None:
        """Handle fetch failure."""
        if self.sender() is not self._fetch_worker:
            return
        QMessageBox.critical(self, "Fetch Failed", f"Could not fetch models:\n{error}")

    def _cleanup_fetch_thread(self, thread: "QThread | None" = None) -> None:
        if thread is not None and thread is not self._fetch_thread:
            return  # a retired fetch finishing late; it cleans up itself
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("Fetch Models")
        self.fetch_progress.setVisible(False)
        if self._fetch_worker:
            self._fetch_worker.deleteLater()
            self._fetch_worker = None
        if self._fetch_thread:
            self._fetch_thread.deleteLater()
            self._fetch_thread = None

    def _populate_model_table(self) -> None:
        """Populate the model table with fetched models."""
        tokens = get_current_theme_tokens()
        success_color = QColor(tokens.get("sys_color_success", "#4ade80"))
        self.model_table.blockSignals(True)
        self.model_table.setRowCount(len(self._fetched_models))

        for row, model in enumerate(self._fetched_models):
            # Checkbox column
            checkbox_item = QTableWidgetItem()
            checkbox_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            checkbox_item.setCheckState(Qt.Unchecked)
            checkbox_item.setData(Qt.UserRole, model.id)  # Store model ID
            self.model_table.setItem(row, 0, checkbox_item)

            # Model name
            name_item = QTableWidgetItem(model.display_name)
            name_item.setData(Qt.UserRole, model.id)
            if model.is_free:
                name_item.setForeground(success_color)  # Green for free
                font = QFont()
                font.setBold(True)
                name_item.setFont(font)
            self.model_table.setItem(row, 1, name_item)

            # Provider
            provider_item = QTableWidgetItem(model.provider.upper())
            provider_item.setData(Qt.UserRole, model.id)
            self.model_table.setItem(row, 2, provider_item)

            # Context length
            context = f"{model.context_length // 1000}k" if model.context_length else "N/A"
            context_item = QTableWidgetItem(context)
            context_item.setData(Qt.UserRole, model.id)
            self.model_table.setItem(row, 3, context_item)

            # Price
            price_item = QTableWidgetItem(model.price_display)
            price_item.setData(Qt.UserRole, model.id)
            if model.is_free:
                price_item.setForeground(success_color)
            self.model_table.setItem(row, 4, price_item)

        self.model_table.blockSignals(False)

    def _filter_models(self, text: str) -> None:
        """Filter model table by search text."""
        text = text.lower()
        for row in range(self.model_table.rowCount()):
            name_item = self.model_table.item(row, 1)
            provider_item = self.model_table.item(row, 2)
            match = False
            if name_item and text in name_item.text().lower():
                match = True
            if provider_item and text in provider_item.text().lower():
                match = True
            self.model_table.setRowHidden(row, not match)

    def _select_all_free(self) -> None:
        """Check all free models."""
        self.model_table.blockSignals(True)
        for row in range(self.model_table.rowCount()):
            if not self.model_table.isRowHidden(row):
                checkbox = self.model_table.item(row, 0)
                if checkbox:
                    model_id = checkbox.data(Qt.UserRole)
                    model = next((m for m in self._fetched_models if m.id == model_id), None)
                    if model and model.is_free:
                        checkbox.setCheckState(Qt.Checked)
        self.model_table.blockSignals(False)

    def _refresh_cache(self) -> None:
        """Force refresh model cache."""
        from system_sl.chatbot import invalidate_model_cache
        invalidate_model_cache(self._get_fetch_provider())
        self._fetch_models()

    def _on_model_selection_changed(self, item: QTableWidgetItem) -> None:
        """Update model_edit when a model is checked."""
        if item.column() != 0:
            return

        model_id = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked:
            # Uncheck all others
            self.model_table.blockSignals(True)
            for row in range(self.model_table.rowCount()):
                other = self.model_table.item(row, 0)
                if other and other is not item:
                    other.setCheckState(Qt.Unchecked)
            self.model_table.blockSignals(False)

            # Update model edit
            model = next((m for m in self._fetched_models if m.id == model_id), None)
            if model:
                self.model_edit.setText(model.id)

    def _get_provider_from_ui(self) -> str:
        """Get the LLM provider for config (google or openai)."""
        preset = self._get_current_preset()
        if preset:
            return preset.provider  # "google" or "openai"
        # Custom preset: infer provider from base_url
        base_url = self.base_url_edit.text().strip().lower()
        return "openai" if base_url else "google"

    def _get_fetch_provider(self) -> str:
        """Get the model-fetching provider name (openrouter, ollama, lm_studio)."""
        preset_key = self.preset_combo.currentData()
        if preset_key in ("openrouter", "ollama", "lm_studio"):
            return preset_key
        # Custom: infer from base_url
        base_url = self.base_url_edit.text().strip().lower()
        if "openrouter.ai" in base_url:
            return "openrouter"
        elif "localhost:11434" in base_url or "127.0.0.1:11434" in base_url:
            return "ollama"
        elif "localhost:1234" in base_url or "127.0.0.1:1234" in base_url:
            return "lm_studio"
        return "google"

    def _collect_config(self) -> "ChatConfig":
        from system_sl.chatbot import ChatConfig
        return ChatConfig(
            provider=self._get_provider_from_ui(),
            base_url=self.base_url_edit.text().strip() or None,
            api_key=self.api_key_edit.text().strip(),
            system_model=self.model_edit.text().strip(),
            temperature=(
                None
                if self.temperature_spin.value() < 0
                else self.temperature_spin.value()
            ),
            max_tokens=self.max_tokens_spin.value() or None,
            system_prompt_addendum=self.system_prompt_addendum_edit.toPlainText().strip(),
        )

    def _test_connection(self) -> None:
        """Test the current configuration by creating an LLM and sending a ping.

        The ping runs in a background thread: a cold local model can take
        tens of seconds to answer, and the GUI must stay responsive.
        """
        from system_sl.chatbot import validate_config

        config = self._collect_config()

        errors = validate_config(config)
        if errors:
            QMessageBox.warning(self, "Invalid Config", "\n".join(errors))
            return

        if self._test_thread is not None:
            return  # a test is already running

        self.test_btn.setEnabled(False)
        self.test_btn.setText("Testing...")

        self._test_thread = QThread(self)
        self._test_worker = _TestConnectionWorker(
            provider=config.provider,
            model=config.system_model,
            base_url=config.base_url,
            api_key=config.api_key,
        )
        self._test_worker.moveToThread(self._test_thread)
        self._test_thread.started.connect(self._test_worker.run)
        self._test_worker.finished.connect(self._on_test_succeeded)
        self._test_worker.failed.connect(self._on_test_failed)
        self._test_worker.finished.connect(self._test_thread.quit)
        self._test_worker.failed.connect(self._test_thread.quit)
        thread = self._test_thread
        thread.finished.connect(lambda: self._cleanup_test_thread(thread))
        thread.start()

    def _on_test_succeeded(self, response: str) -> None:
        if self.sender() is not self._test_worker:
            return  # a test abandoned when the dialog closed
        QMessageBox.information(self, "Success", f"Connection OK!\nResponse: {response}")

    def _on_test_failed(self, error: str) -> None:
        if self.sender() is not self._test_worker:
            return
        QMessageBox.critical(self, "Connection Failed", error)

    def _cleanup_test_thread(self, thread: "QThread | None" = None) -> None:
        if thread is not None and thread is not self._test_thread:
            return
        self.test_btn.setEnabled(True)
        self.test_btn.setText("Test Connection")
        if self._test_worker is not None:
            self._test_worker.deleteLater()
            self._test_worker = None
        if self._test_thread is not None:
            self._test_thread.deleteLater()
            self._test_thread = None

    def _on_apply(self) -> bool:
        from system_sl.chatbot import save_config, validate_config
        config = self._collect_config()
        errors = validate_config(config)
        if errors:
            QMessageBox.warning(self, "Invalid Config", "\n".join(errors))
            return False
        save_config(config)
        self._config = config
        self.saved.emit()
        return True

    def _on_accept(self) -> None:
        if self._on_apply():
            self.finished.emit(True)

    def shutdown_threads(self) -> None:
        """Abandon a running fetch or connection test without blocking.

        Called whenever the dialog holding this page closes (Close, Esc,
        accept, reject or the window's X all go through ``QDialog.done``).
        """
        fetch = (self._fetch_thread, self._fetch_worker)
        self._fetch_thread = self._fetch_worker = None
        _retire_thread(*fetch)
        test = (self._test_thread, self._test_worker)
        self._test_thread = self._test_worker = None
        _retire_thread(*test)
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("Fetch Models")
        self.fetch_progress.setVisible(False)
        self.test_btn.setEnabled(True)
        self.test_btn.setText("Test Connection")

    def closeEvent(self, event) -> None:
        self.shutdown_threads()
        super().closeEvent(event)


class SettingsDialog(QDialog):
    """Standalone LLM settings, for first-run setup before the main window exists."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("LLM Settings")
        self.setModal(True)
        self.resize(700, 550)

        self.page = LLMSettingsPage(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.page)
        self.page.finished.connect(lambda ok: self.accept() if ok else self.reject())

    def done(self, result: int) -> None:
        # Every way of closing the dialog ends here, unlike closeEvent.
        self.page.shutdown_threads()
        super().done(result)


# =============================================================================
# CHAT WORKER
# =============================================================================

class _ChatWorker(QObject):
    reasoning = Signal(str)
    chunk = Signal(str)
    usage = Signal(dict)
    finished = Signal(str)
    failed = Signal(str)
    cancelled = Signal()
    proposal = Signal(dict)
    attachment_notice = Signal(str)
    tool_activity = Signal(str, str, str)  # tool_name, status, detail

    def __init__(
        self,
        session: "ChatSession",
        user_msg: str,
        attachment_paths: list[str] | None = None,
        typed_text: str | None = None,
        message_id: str | None = None,
    ):
        super().__init__()
        self.session = session
        self.user_msg = user_msg
        self.attachment_paths = attachment_paths or []
        # What the Player typed themselves, without quotes or documents. The
        # task gate only trusts this text (see system.add_task).
        self.typed_text = typed_text
        # Id the Player's message is stored under, so it can be rewound to.
        self.message_id = message_id

    def run(self):
        from system_sl.chatbot.context_window import (
            attachment_budget_chars,
            context_window_for_llm,
        )
        from system_sl.chatbot.documents import build_attachments, format_for_chat

        thread = QThread.currentThread()
        collected: list[str] = []

        # Extract text from attachments first. Every file of this message
        # shares one budget, derived from the model's context window.
        augmented_msg = self.user_msg
        if self.attachment_paths:
            window = context_window_for_llm(getattr(self.session, "llm", None))
            # One budget per message: quotes already in it are paid for out
            # of the same allowance, not on top of it.
            quoted = max(0, len(self.user_msg) - len(self.typed_text or self.user_msg))
            budget = max(2_000, attachment_budget_chars(window) - quoted)
            attachments = build_attachments(self.attachment_paths, budget)

            doc_parts = []
            for item in attachments:
                if item.error:
                    doc_parts.append(
                        f"[uploaded document: {item.name} — {item.error}]"
                    )
                    self.attachment_notice.emit(f"{item.name}: {item.error}")
                else:
                    doc_parts.append(format_for_chat(item))
                    if item.truncated:
                        self.attachment_notice.emit(item.summary())

            if doc_parts:
                augmented_msg = "\n\n".join(doc_parts) + "\n\n" + self.user_msg

        def on_tool(tool_name: str, status: str, detail: str) -> None:
            self.tool_activity.emit(tool_name, status, detail)

        try:
            for kind, delta in self.session.stream_turn(
                augmented_msg,
                on_proposal=self.proposal.emit,
                on_tool=on_tool,
                typed_text=self.typed_text,
                message_id=self.message_id,
            ):
                if thread.isInterruptionRequested():
                    self.cancelled.emit()
                    return
                if kind == "reasoning":
                    self.reasoning.emit(delta)
                elif kind == "usage":
                    if isinstance(delta, dict):
                        self.usage.emit(delta)
                else:
                    collected.append(delta)
                    self.chunk.emit(delta)
        except NotImplementedError:
            # Provider or model cannot stream; fall back to a blocking turn.
            try:
                reply = self.session.run_turn(
                    augmented_msg,
                    on_proposal=self.proposal.emit,
                    typed_text=self.typed_text,
                    message_id=self.message_id,
                )
            except Exception as e:
                self.failed.emit(f"{type(e).__name__}: {e}")
                return
            collected.append(reply)
            self.chunk.emit(reply)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")
            return

        if thread.isInterruptionRequested():
            self.cancelled.emit()
            return

        # The provider's counts are authoritative; emit them once more here so
        # the blocking fallback path is covered too.
        usage = self.session.last_usage.as_dict()
        context_tokens = getattr(self.session, "last_context_tokens", 0)
        if context_tokens:
            usage["context_tokens"] = context_tokens
        self.usage.emit(usage)

        # Attachment text is compacted by the panel once the turn is over,
        # whatever the outcome (see ChatPanel._cleanup_thread).
        self.finished.emit("".join(collected))


# =============================================================================
# CHAT PANEL
# =============================================================================

class ChatPanel(QWidget):
    settings_requested = Signal()
    busy_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)

        # One conversation at a time, backed by the shared LangGraph
        # checkpointer: the id (thread) does not depend on the model, so
        # switching models, pressing Stop, or restarting the app continues the
        # same history. The session object is rebuilt when the model changes.
        self._session: "ChatSession | None" = None
        self._thread_id: str = ""
        self._current_model: str = ""
        self._thread: QThread | None = None
        self._worker: _ChatWorker | None = None

        # Background scan of the local model servers. The scan can take
        # several seconds, so it never runs on the GUI thread.
        self._local_models: list[tuple[str, str]] = []
        self._discovery_running: bool = False
        self._discovery_thread: QThread | None = None
        self._discovery_worker: "_ModelDiscoveryWorker | None" = None

        # Attachment state
        self._attachments: list[Path] = []

        # The transcript model is the source of truth for what is on screen;
        # the QTextBrowser is only a view of it. Built from the checkpoint
        # history when a conversation is opened, then appended to as the turn
        # runs. Each message's blocks are tagged with its id, so a click or a
        # selection can be mapped back to the message it came from.
        from system_sl.chatbot.transcript import Transcript

        self._messages = Transcript()
        self._stream_message_id: str | None = None
        # Set when the palette changed mid-reply; redrawn when the turn ends.
        self._theme_redraw_pending = False

        # Quotes waiting to go into the next message. Each chip above the
        # input can be removed before sending.
        self._quotes: list = []

        # Messages composed while the assistant is busy. They are sent
        # automatically, in order, once the running turn finishes.
        # Each entry is (text, attachment_paths).
        self._message_queue: list[tuple[str, list[str]]] = []

        # Live-streaming state
        self._answer_open: bool = False       # the [SYSTEM] line exists
        self._streamed_any: bool = False      # any answer text arrived
        self._reasoning_shown: bool = False   # the thinking view is populated
        self._halted: bool = False            # user pressed Stop

        self._answer_text: str = ""           # raw markdown accumulated this turn
        self._answer_stopped: bool = False    # Stop was pressed; UI-only marker
        self._answer_start: int | None = None # doc position of the [SYSTEM] line
        self._last_rendered_html: str = ""
        self._stream_tokens: dict = {}        # theme captured once per turn
        # Incremental streaming: text before this character is already on
        # screen and never rewritten; the rest is the in-progress tail.
        self._answer_committed_chars: int = 0
        self._stream_tail_start: int | None = None

        # Four-dot loading indicator beside the label while the model works.
        # One bright dot travels left to right through a fixed-width field.
        self._dots_start: int | None = None
        self._dots_frame: int = 0
        _initial_tokens = get_current_theme_tokens()
        self._dots_bright: str = _initial_tokens.get("sys_color_primary", "#00d9ff")
        self._dots_dim: str = _initial_tokens.get("sys_color_text_secondary", "#9ab8e5")
        self._dots_timer = QTimer(self)
        self._dots_timer.setInterval(200)
        self._dots_timer.timeout.connect(self._tick_dots)

        # Throttled markdown re-render while streaming
        self._md_timer = QTimer(self)
        self._md_timer.setSingleShot(True)
        self._md_timer.setInterval(120)
        self._md_timer.timeout.connect(self._render_answer)

        # Task proposals awaiting the Player's Yes/No
        self._pending_proposals: list[dict] = []
        self._current_proposal: dict | None = None
        # Confirmation notes waiting for the running turn to end. Writing to
        # the checkpoint while the agent is still running would race with it,
        # so they are flushed from _cleanup_thread().
        self._pending_confirmations: list[tuple] = []
        self._notes_after_turn: list[tuple[Callable[[str], None], str]] = []
        # Transcript id of the Player message whose reply is running, and of
        # the one whose reply failed last (offered as "Retry").
        self._turn_user_id: str | None = None
        self._last_failed_user_id: str | None = None
        # The earlier Player message being edited (edit-and-resend), if any.
        self._editing_message = None
        self._shut_down = False

        root = QVBoxLayout(self)

        # --- Header with Model Selector ---
        header = QHBoxLayout()
        header.addWidget(QLabel("Model:"))

        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(250)
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        header.addWidget(self.model_combo, stretch=1)

        # Refresh button
        self.refresh_btn = QPushButton()
        self.refresh_btn.setIcon(get_icon("refresh"))
        self.refresh_btn.setFixedWidth(36)
        self.refresh_btn.setToolTip("Refresh model list")
        self.refresh_btn.setAccessibleName("Refresh model list")
        self.refresh_btn.setAccessibleDescription("Fetch the latest available models from the configured provider")
        self.refresh_btn.clicked.connect(self._refresh_models)
        header.addWidget(self.refresh_btn)

        # Settings button
        self.settings_btn = QPushButton()
        self.settings_btn.setIcon(get_icon("settings"))
        self.settings_btn.setFixedWidth(36)
        self.settings_btn.setToolTip("LLM Settings")
        self.settings_btn.setAccessibleName("LLM Settings")
        self.settings_btn.setAccessibleDescription("Open the LLM provider and model configuration dialog")
        self.settings_btn.clicked.connect(self._open_settings)
        header.addWidget(self.settings_btn)

        # Model choice is per-conversation. Saving it as the default is an
        # explicit action, never a side effect of browsing the dropdown.
        self.default_model_btn = QPushButton("Set as default")
        self.default_model_btn.setToolTip(
            "Use this model for new conversations from now on"
        )
        self.default_model_btn.setAccessibleName("Set as default model")
        self.default_model_btn.setAccessibleDescription("Save the currently selected model as the default for new conversations")
        self.default_model_btn.setEnabled(False)
        self.default_model_btn.clicked.connect(self._set_model_as_default)
        header.addWidget(self.default_model_btn)

        root.addLayout(header)

        # --- Conversation row: reopen a past chat, or start a new one ---
        chat_row = QHBoxLayout()
        chat_row.addWidget(QLabel("Chat:"))
        self.history_combo = QComboBox()
        self.history_combo.setMinimumWidth(220)
        self.history_combo.setToolTip("Reopen a past conversation")
        self.history_combo.setAccessibleName("Conversation history")
        self.history_combo.setAccessibleDescription("Select a previous conversation to reopen")
        self.history_combo.currentIndexChanged.connect(self._on_history_changed)
        chat_row.addWidget(self.history_combo, stretch=1)

        self.new_chat_btn = QPushButton()
        self.new_chat_btn.setIcon(get_icon("add"))
        self.new_chat_btn.setFixedWidth(36)
        self.new_chat_btn.setToolTip("Start a new conversation")
        self.new_chat_btn.setAccessibleName("New conversation")
        self.new_chat_btn.setAccessibleDescription("Start a fresh conversation with a new thread ID")
        self.new_chat_btn.clicked.connect(self._on_new_chat)
        chat_row.addWidget(self.new_chat_btn)

        self.delete_chat_btn = QPushButton()
        self.delete_chat_btn.setIcon(get_icon("delete_chat"))
        self.delete_chat_btn.setFixedWidth(36)
        self.delete_chat_btn.setToolTip("Delete this conversation")
        self.delete_chat_btn.setAccessibleName("Delete conversation")
        self.delete_chat_btn.setAccessibleDescription(
            "Permanently delete the open conversation and its history"
        )
        self.delete_chat_btn.clicked.connect(self._on_delete_chat)
        chat_row.addWidget(self.delete_chat_btn)
        root.addLayout(chat_row)

        # --- Transcript ---
        self.transcript = QTextBrowser()
        self.transcript.setReadOnly(True)
        self.transcript.setOpenExternalLinks(False)  # We handle links ourselves
        self.transcript.setPlaceholderText("[ awaiting Player input — channel: SYSTEM ]")
        # Handle anchor clicks for alert details and external links
        self.transcript.anchorClicked.connect(self._on_anchor_clicked)
        # Enable drag & drop on transcript
        self.transcript.setAcceptDrops(True)
        # Right-click a message to copy or quote it.
        self.transcript.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.transcript.customContextMenuRequested.connect(
            self._on_transcript_context_menu
        )
        root.addWidget(self.transcript, stretch=1)

        # How full the conversation's context window is, plus the tokens and
        # cost of the last reply. It stays out of the transcript itself so
        # quoting or copying a message never includes it.
        self._context_tokens = 0
        self._context_estimated = False
        self._reply_usage_line = ""
        # The finished turn's usage, added to the conversation's totals once
        # the turn ends (usage can be emitted twice per turn).
        self._turn_usage = None
        self.usage_label = QLabel("")
        self.usage_label.setObjectName("usageLabel")
        self.usage_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.usage_label.setVisible(False)
        self.usage_label.setAccessibleName("Token usage")
        self.usage_label.setToolTip(
            "Context: how much of the model's context window the conversation "
            "fills (the latest prompt plus its reply). Older turns are trimmed "
            "or summarised when it gets full.\n"
            "Last reply: tokens read and written for the most recent answer, "
            "and its estimated cost.\n"
            "Conversation: everything this conversation has used so far "
            "(≥ when some replies came from a model with no known price)."
        )
        root.addWidget(self.usage_label)

        # Store raw error details for alert detail links

        # --- Live reasoning ("thinking") view ---
        # Only shown while a reasoning model is thinking; cleared as soon as
        # real answer text starts arriving.
        self.reasoning_view = QTextEdit()
        self.reasoning_view.setReadOnly(True)
        self.reasoning_view.setVisible(False)
        self.reasoning_view.setMaximumHeight(96)
        self.reasoning_view.setMinimumHeight(48)
        root.addWidget(self.reasoning_view)

        # --- Task confirmation bar (Yes/No, inline like a permission prompt) ---
        self.confirm_bar = QFrame()
        self.confirm_bar.setObjectName("taskConfirmBar")
        bar_layout = QHBoxLayout(self.confirm_bar)
        bar_layout.setContentsMargins(8, 6, 8, 6)
        self.confirm_label = QLabel("")
        self.confirm_label.setWordWrap(True)
        self.confirm_label.setTextFormat(Qt.TextFormat.PlainText)
        self.confirm_yes = QPushButton("Yes")
        self.confirm_yes.setFixedWidth(64)
        self.confirm_yes.clicked.connect(self._on_confirm_yes)
        self.confirm_no = QPushButton("No")
        self.confirm_no.setFixedWidth(64)
        self.confirm_no.clicked.connect(self._on_confirm_no)
        bar_layout.addWidget(self.confirm_label, stretch=1)
        bar_layout.addWidget(self.confirm_yes)
        bar_layout.addWidget(self.confirm_no)
        root.addWidget(self.confirm_bar)
        # addWidget() re-parents and hides, so visibility is set afterwards.
        self.confirm_bar.setVisible(False)

        # --- Edit banner (shown while an earlier message is being edited) ---
        self.edit_bar = QFrame()
        edit_layout = QHBoxLayout(self.edit_bar)
        edit_layout.setContentsMargins(6, 2, 6, 2)
        self.edit_label = QLabel(
            "Editing an earlier message — sending replaces it and everything after it."
        )
        self.edit_label.setWordWrap(True)
        self.edit_cancel = QPushButton("Cancel")
        self.edit_cancel.setToolTip("Stop editing (Esc)")
        self.edit_cancel.setAccessibleName("Cancel editing")
        self.edit_cancel.clicked.connect(self._cancel_edit)
        edit_layout.addWidget(self.edit_label, 1)
        edit_layout.addWidget(self.edit_cancel)
        root.addWidget(self.edit_bar)
        self.edit_bar.setVisible(False)

        # --- Quote chips row (hidden when nothing is quoted) ---
        # Sits directly above the input: these are the quotes that will be
        # sent with the next message, unlike the attachments row below.
        self.quote_chips_container = QWidget()
        self.quote_chips_layout = QHBoxLayout(self.quote_chips_container)
        self.quote_chips_layout.setContentsMargins(4, 2, 4, 2)
        self.quote_chips_layout.setSpacing(4)
        self.quote_chips_layout.addStretch(1)
        root.addWidget(self.quote_chips_container)
        self.quote_chips_container.setVisible(False)

        # --- Attachment chips row (hidden when empty) ---
        self.attach_chips_container = QWidget()
        self.attach_chips_layout = QHBoxLayout(self.attach_chips_container)
        self.attach_chips_layout.setContentsMargins(4, 2, 4, 2)
        self.attach_chips_layout.setSpacing(4)
        self.attach_chips_layout.addStretch(1)
        root.addWidget(self.attach_chips_container)
        self.attach_chips_container.setVisible(False)

        # --- Input Row ---
        input_row = QHBoxLayout()
        self.input_box = QTextEdit()
        self.input_box.setPlaceholderText("Speak, Player... (Enter to send, Shift+Enter for new line)")
        self.input_box.setMaximumHeight(100)
        self.input_box.setMinimumHeight(44)
        self.input_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.input_box.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.input_box.installEventFilter(self)
        self.input_box.setAccessibleName("Message input")
        self.input_box.setAccessibleDescription("Type your message here. Press Enter to send, Shift+Enter for a new line")
        # Paperclip button for attachments
        self.attach_btn = QPushButton()
        self.attach_btn.setIcon(get_icon("attach"))
        self.attach_btn.setFixedWidth(36)
        self.attach_btn.setFixedHeight(36)
        self.attach_btn.setToolTip("Attach document (PDF, DOCX, HTML, TXT, MD, CSV...)")
        self.attach_btn.setAccessibleName("Attach document")
        self.attach_btn.setAccessibleDescription("Attach a file to include in your message")
        self.attach_btn.clicked.connect(self._on_attach_clicked)
        self.send_button = QPushButton()
        self.send_button.setIcon(get_icon("send"))
        self.send_button.setFixedWidth(36)
        self.send_button.setFixedHeight(36)
        self.send_button.setToolTip("Send message (Enter)")
        self.send_button.setAccessibleName("Send message")
        self.send_button.setAccessibleDescription("Send the typed message to the assistant")
        self.send_button.clicked.connect(self._on_send)
        self.stop_button = QPushButton()
        self.stop_button.setIcon(get_icon("stop"))
        self.stop_button.setFixedWidth(36)
        self.stop_button.setFixedHeight(36)
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip("Interrupt the reply")
        self.stop_button.setAccessibleName("Stop reply")
        self.stop_button.setAccessibleDescription("Interrupt the currently streaming assistant reply")
        self.stop_button.clicked.connect(self._on_stop)
        input_row.addWidget(self.input_box, stretch=1)
        input_row.addWidget(self.attach_btn)
        input_row.addWidget(self.send_button)
        input_row.addWidget(self.stop_button)
        input_row.setAlignment(Qt.AlignmentFlag.AlignBottom)
        root.addLayout(input_row)

        # Queue badge: shows how many messages are waiting to be sent.
        # Placed under the input row so it never competes with the input
        # for horizontal space. Clicking it clears the queue.
        self.queue_badge = _ClickableLabel("")
        self.queue_badge.setObjectName("queueBadge")
        self.queue_badge.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.queue_badge.setVisible(False)
        self.queue_badge.setToolTip("Click to clear the queued messages")
        self.queue_badge.setAccessibleName("Queued messages")
        self.queue_badge.setAccessibleDescription("Shows the number of messages queued to send. Click to clear the queue")
        self.queue_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.queue_badge.clicked.connect(self._on_queue_badge_clicked)
        root.addWidget(self.queue_badge)

        # Enable drag & drop on the panel itself
        self.setAcceptDrops(True)

        # Reopen the conversation that was on screen when the app last closed
        # (or start the first one) before the model dropdown is filled, so the
        # dropdown selects the model that conversation was last using.
        self._restore_active_conversation()

        # Initialize model list. The local-server scan runs in the background;
        # the dropdown is usable immediately.
        self._populate_model_list()
        self._start_discovery()

    # ----- Model Management -----

    def _populate_model_list(self) -> None:
        """Populate the model dropdown with configured + discovered + cached models.

        Local-server discovery is slow (up to ~11s of timeouts), so it runs
        in a background worker. This method only renders whatever the last
        discovery produced and shows a placeholder while a scan is running.

        Each model ID is added only once, preferring the richest source entry:
        current configured > discovered local > cached OpenRouter > cloud presets.
        """
        from system_sl.chatbot import load_config, get_model_cache, get_preset_models
        self.model_combo.blockSignals(True)
        self.model_combo.clear()

        config = load_config()
        # A per-conversation choice survives discovery refreshes; only fall
        # back to the configured default when nothing has been picked yet.
        current_model = self._current_model or config.system_model

        has_config = bool(current_model and config.provider)

        # Each group is a list of (display, model_id, enabled). Groups are
        # added in priority order with a separator between non-empty ones; a
        # model already listed by an earlier group is skipped.
        groups: list[list[tuple[str, str, bool]]] = []

        # 1. Current configured model, or a placeholder when nothing is set.
        if has_config:
            groups.append([(f"✦ {current_model}", current_model, True)])
        else:
            groups.append([("⚙ Configure LLM in Settings…", "", False)])

        # 2. Discovered local models — a placeholder while the scan runs,
        #    otherwise the models found by the last scan.
        if self._discovery_running:
            groups.append([("🔍 Discovering local models…", "", False)])
        else:
            groups.append([(d, m, True) for d, m in self._local_models])

        # 3. Cached models from OpenRouter (if any). provider is only ever
        #    "google" or "openai"; the endpoint is what identifies OpenRouter.
        if config.base_url and "openrouter.ai" in config.base_url.lower():
            cached = get_model_cache("openrouter") or []
            groups.append([
                (f"☁️ {'✨ ' if m.is_free else ''}{m.name} ({m.id})", m.id, True)
                for m in cached
            ])

        # 4. Cloud preset models (lowest priority).
        groups.append([(d, m, True) for d, m in get_preset_models()])

        seen_models: set[str] = set()
        for group in groups:
            entries = []
            for display, model_id, enabled in group:
                if model_id:
                    if model_id in seen_models:
                        continue
                    seen_models.add(model_id)
                entries.append((display, model_id, enabled))
            if not entries:
                continue
            if self.model_combo.count():
                self.model_combo.insertSeparator(self.model_combo.count())
            for display, model_id, enabled in entries:
                self.model_combo.addItem(display, model_id)
                if not enabled:
                    last = self.model_combo.count() - 1
                    self.model_combo.model().item(last).setEnabled(False)

        # Select current model
        self.model_combo.blockSignals(False)
        self._current_model = current_model
        if has_config:
            # Reuses the restored conversation's model even when discovery has
            # not returned it yet, so the dropdown matches what will answer.
            self._select_model_in_combo(current_model)
        else:
            self.model_combo.blockSignals(True)
            self.model_combo.setCurrentIndex(0)
            self.model_combo.blockSignals(False)
        self._update_default_button()

    # ----- Background model discovery -----

    def _start_discovery(self) -> None:
        """Probe the local model servers without blocking the GUI thread."""
        if self._discovery_running:
            return

        self._discovery_running = True
        self._populate_model_list()

        self._discovery_thread = QThread(self)
        self._discovery_worker = _ModelDiscoveryWorker()
        self._discovery_worker.moveToThread(self._discovery_thread)
        self._discovery_thread.started.connect(self._discovery_worker.run)
        self._discovery_worker.finished.connect(self._on_models_discovered)
        self._discovery_worker.failed.connect(self._on_discovery_failed)
        self._discovery_worker.finished.connect(self._discovery_thread.quit)
        self._discovery_worker.failed.connect(self._discovery_thread.quit)
        self._discovery_thread.finished.connect(self._cleanup_discovery_thread)
        self._discovery_thread.start()

    def _on_models_discovered(self, models: list) -> None:
        """Handle a completed local-server scan."""
        self._local_models = models
        self._discovery_running = False
        self._populate_model_list()

    def _on_discovery_failed(self, error: str) -> None:
        """Handle a failed local-server scan."""
        self._local_models = []
        self._discovery_running = False
        self._populate_model_list()
        self._append_alert(f"model discovery failed: {error}")

    def _cleanup_discovery_thread(self) -> None:
        if self._discovery_worker is not None:
            self._discovery_worker.deleteLater()
            self._discovery_worker = None
        if self._discovery_thread is not None:
            self._discovery_thread.deleteLater()
            self._discovery_thread = None

    def _refresh_models(self) -> None:
        """Refresh the model list from local servers."""
        self._populate_model_list()
        self._start_discovery()

    def _on_model_changed(self, index: int) -> None:
        """Handle model selection change for this conversation only."""
        if index < 0:
            return

        model_id = self.model_combo.itemData(index)
        if not model_id or model_id == self._current_model:
            return

        # Deliberately not saved to the global config: browsing the dropdown
        # must not change the default for every future conversation. The
        # "Set as default" button is the explicit way to persist it.
        self._current_model = model_id
        self._clear_proposals()
        # Queued messages were composed for the previous model; sending them
        # to a different one would surprise the Player.
        if self._message_queue:
            dropped = len(self._message_queue)
            self._message_queue.clear()
            self._update_queue_badge()
            self._append_alert(f"dropped {dropped} queued message(s) — model changed")

        # The conversation (thread) and its transcript stay exactly as they
        # are: the new model answers from the same message history. Only the
        # agent behind the session is replaced.
        from system_sl.chatbot.memory import update_conversation

        update_conversation(self._thread_id, last_model=model_id)
        self._refresh_history_combo()
        self._update_default_button()
        self._append_note(f"Switched to {model_id} — the conversation continues")

    def _update_default_button(self) -> None:
        """Offer to save the current model only when it is not the default."""
        from system_sl.chatbot import load_config

        is_default = bool(
            self._current_model
            and self._current_model == load_config().system_model
        )
        self.default_model_btn.setEnabled(bool(self._current_model) and not is_default)

    def _set_model_as_default(self) -> None:
        """Persist the current model as the default, on explicit request."""
        if not self._current_model:
            return

        from system_sl.chatbot import load_config, save_config, reload_agent

        config = load_config()
        if config.system_model == self._current_model:
            return
        config.system_model = self._current_model
        save_config(config)
        # The shared agent was built for the old default; rebuild it and drop
        # the session so the next turn uses the newly saved configuration.
        reload_agent()
        self._session = None
        self._update_default_button()
        self._append_alert(f"Default model set to: {self._current_model}")

    # ----- Conversations -----

    def _restore_active_conversation(self) -> None:
        """Open the conversation that was active when the app last closed."""
        from system_sl.chatbot import load_config
        from system_sl.chatbot.memory import ensure_active_conversation

        try:
            default_model = load_config().system_model
        except Exception:
            default_model = ""

        entry = ensure_active_conversation(model_name=default_model)
        self._thread_id = entry["id"]
        self._current_model = entry.get("last_model") or ""
        self._refresh_history_combo()
        messages = self._messages_for_thread(self._thread_id)
        self._render_transcript(messages)
        self._estimate_context(messages)

    def _conversation_label(self, entry: dict) -> str:
        """Display name for a conversation in the history dropdown."""
        title = (entry.get("title") or "").strip()
        return title or "New conversation"

    def _refresh_history_combo(self) -> None:
        """Rebuild the conversation dropdown, keeping the open one selected."""
        from system_sl.chatbot.memory import list_conversations

        self.history_combo.blockSignals(True)
        self.history_combo.clear()
        selected = -1
        for entry in list_conversations():
            self.history_combo.addItem(
                self._conversation_label(entry), entry["id"]
            )
            updated = entry.get("updated") or entry.get("created") or ""
            model = entry.get("last_model") or "default model"
            index = self.history_combo.count() - 1
            self.history_combo.setItemData(
                index,
                f"{self._conversation_label(entry)}\nlast used {updated}\nmodel: {model}",
                Qt.ItemDataRole.ToolTipRole,
            )
            if entry["id"] == self._thread_id:
                selected = index
        if selected < 0 and self._thread_id:
            # A new chat has no index entry until its first message. Without a
            # row of its own, the combo would show (and "select") the most
            # recent conversation, which then could not be picked to reopen.
            self.history_combo.insertItem(0, "New conversation", self._thread_id)
            selected = 0
        if selected >= 0:
            self.history_combo.setCurrentIndex(selected)
        self.history_combo.blockSignals(False)

    def _on_new_chat(self) -> None:
        """Start a new conversation with a fresh thread id."""
        if self.is_busy():
            return
        from system_sl.chatbot import load_config
        from system_sl.chatbot.memory import new_thread_id

        try:
            model = self._current_model or load_config().system_model
        except Exception:
            model = self._current_model

        # Hand out a thread id without writing an index entry: a new chat that
        # is never used must not leave an empty "New conversation" behind. The
        # entry is created by the first sent message (see _send_now).
        self._thread_id = new_thread_id()
        self._current_model = model
        self._session = None
        self._reset_stream_state()
        self._cancel_edit(clear_input=False)
        self._turn_user_id = None
        self._last_failed_user_id = None
        self._clear_quotes()
        self._render_transcript([])
        self._estimate_context([])
        self._refresh_history_combo()
        self.input_box.setFocus()

    def _on_delete_chat(self) -> None:
        """Delete the open conversation, after asking, and open another."""
        if self.is_busy() or not self._thread_id:
            return
        from system_sl.chatbot.memory import (
            delete_conversation,
            get_conversation,
            list_conversations,
        )

        entry = get_conversation(self._thread_id)
        if entry is None:
            # An unsaved new chat: nothing is stored, so just start over.
            self._on_new_chat()
            return

        title = self._conversation_label(entry)
        answer = QMessageBox.question(
            self,
            "Delete conversation?",
            f"Delete \"{title}\" and its whole history? This can't be undone.\n\n"
            "Tasks it added are kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        # Settle anything still tied to this conversation before it goes.
        self._clear_proposals()
        self._session = None
        try:
            delete_conversation(self._thread_id)
        except Exception:
            logger.warning("could not delete conversation %s", self._thread_id, exc_info=True)
            self._append_alert("could not delete this conversation")
            return

        remaining = list_conversations()
        if remaining:
            self._open_conversation(remaining[0]["id"])
        else:
            self._on_new_chat()

    def _on_history_changed(self, index: int) -> None:
        """Reopen the conversation picked in the dropdown."""
        if index < 0 or self.is_busy():
            return
        thread_id = self.history_combo.itemData(index)
        if thread_id and thread_id != self._thread_id:
            self._open_conversation(thread_id)

    def _open_conversation(self, thread_id: str) -> None:
        """Show a stored conversation and make it the active one."""
        from system_sl.chatbot.memory import get_conversation, select_conversation

        select_conversation(thread_id)

        # Resolve pending proposals against the conversation that produced
        # them before the session is replaced.
        self._clear_proposals()

        self._thread_id = thread_id
        self._session = None
        self._reset_stream_state()
        self._cancel_edit(clear_input=False)
        self._turn_user_id = None
        self._last_failed_user_id = None
        # Quotes composed for the previous conversation make no sense here.
        self._clear_quotes()

        entry = get_conversation(thread_id) or {}
        last_model = entry.get("last_model")
        if last_model and last_model != self._current_model:
            self._current_model = last_model
            self._select_model_in_combo(last_model)
            self._update_default_button()

        messages = self._messages_for_thread(thread_id)
        self._render_transcript(messages)
        self._estimate_context(messages)
        self._refresh_history_combo()
        self.input_box.setFocus()

    def _select_model_in_combo(self, model_name: str) -> None:
        """Show ``model_name`` in the dropdown without triggering a switch."""
        index = self.model_combo.findData(model_name)
        self.model_combo.blockSignals(True)
        if index < 0:
            # The model is not in the list (discovery has not found it, or it
            # is not in the cached presets). Still show what will answer.
            self.model_combo.insertItem(0, f"✦ {model_name}", model_name)
            index = 0
        self.model_combo.setCurrentIndex(index)
        self.model_combo.blockSignals(False)

    def _messages_for_thread(self, thread_id: str) -> list:
        """Read a conversation's messages straight from the checkpointer."""
        from system_sl.chatbot.memory import load_thread_messages

        try:
            return load_thread_messages(thread_id)
        except Exception:
            return []

    def _render_transcript(self, messages: list) -> None:
        """Rebuild the transcript model and the view from stored messages.

        Only the Player's messages and the System's answers are bubbles; tool
        traffic is internal, and app-injected notes are shown dimmed so they
        are not mistaken for something the Player typed. The model is kept so
        per-message actions (copy, quote) know what they are acting on.
        """
        from system_sl.chatbot.transcript import Transcript

        self._messages = Transcript.from_history(messages)
        self._redraw_transcript()
        self._scroll_to_bottom()

    def _redraw_transcript(self) -> None:
        """Draw every message in the model again, in the current colours."""
        self._stream_message_id = None
        self._theme_redraw_pending = False
        self.transcript.clear()

        # A very long conversation would build thousands of rich-text blocks
        # on every reopen. Render the newest slice; the model still holds the
        # full history, and the note says where the view starts.
        limit = 200
        shown = list(self._messages)
        hidden = max(0, len(shown) - limit)
        if hidden:
            self._append_system_note(
                f"… {hidden} earlier message(s) hidden — the conversation is unchanged"
            )
            shown = shown[-limit:]
        for message in shown:
            self._render_message(message)

    def _redraw_for_theme(self) -> None:
        """Redraw the transcript after a palette change, keeping the scroll."""
        bar = self.transcript.verticalScrollBar()
        at_bottom = self._is_at_bottom()
        position = bar.value()
        self._redraw_transcript()
        if at_bottom:
            self._scroll_to_bottom()
        else:
            bar.setValue(min(position, bar.maximum()))

    def _render_message(self, message: "ChatMessage") -> None:
        """Draw one message into the transcript view."""
        from system_sl.chatbot.transcript import (
            ALERT,
            ASSISTANT,
            CONFIRMATION,
            NOTE,
            USER,
        )

        if message.role == USER:
            self._append_player(message)
        elif message.role == ASSISTANT:
            self._append_bot(message)
        elif message.role == NOTE:
            self._append_system_note(message.text, message_id=message.id)
        elif message.role == ALERT and message.error:
            self._render_alert_with_details(message)
        elif message.role == ALERT:
            self._append_alert_bubble(message.text, message_id=message.id, timestamp=message.timestamp)
        elif message.role == CONFIRMATION:
            self._append_confirmation_bubble(message.text, message_id=message.id, timestamp=message.timestamp)

    def _message_id_at(self, position: int) -> str | None:
        """Return the id of the message whose text covers ``position``.

        A blank block (the trailing paragraph of a bubble) has no tag of its
        own; the search walks back to the message it belongs to.
        """
        block = self.transcript.document().findBlock(position)
        while block.isValid():
            data = block.userData()
            if isinstance(data, _MessageBlockData) and data.message_id:
                return data.message_id
            block = block.previous()
        return None

    def _tag_blocks(self, start: int, message_id: str | None, end: int | None = None) -> None:
        """Tag every block from ``start`` to ``end`` with ``message_id``."""
        if not message_id:
            return
        doc = self.transcript.document()
        last = doc.characterCount() - 1 if end is None else end
        block = doc.findBlock(start)
        while block.isValid() and block.position() <= last:
            block.setUserData(_MessageBlockData(message_id))
            block = block.next()

    @staticmethod
    def _strip_date_prefix(text: str) -> str:
        """Drop the "[Today's date: …]" line older turns were stored with.

        The client used to prefix every message with the date. That prefix is
        gone now (the date lives in the CONTEXT block instead), but old
        conversations still carry it, and the transcript should not show it.
        """
        from system_sl.chatbot.transcript import strip_date_prefix

        return strip_date_prefix(text)

    def _append_note(self, text: str) -> None:
        """Append an app-generated note, keeping the model in step."""
        from system_sl.chatbot.transcript import NOTE

        message = self._messages.append(NOTE, text)
        self._append_system_note(text, message_id=message.id)

    def _append_system_note(self, text: str, message_id: str | None = None) -> None:
        """Append a dim, italic, centred note (not a chat bubble)."""
        tokens = self._get_live_tokens()
        dim = tokens.get("sys_color_text_secondary", "#9ab8e5")

        follow_tail = self._is_at_bottom()
        cursor = QTextCursor(self.transcript.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)

        block_fmt = QTextBlockFormat()
        block_fmt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        block_fmt.setTopMargin(6)
        block_fmt.setBottomMargin(6)
        cursor.insertBlock(block_fmt)
        start = cursor.block().position()

        char_fmt = QTextCharFormat()
        char_fmt.setForeground(QColor(dim))
        char_fmt.setFontPointSize(11)
        char_fmt.setFontItalic(True)
        cursor.setCharFormat(char_fmt)
        cursor.insertText(text)

        self._tag_blocks(start, message_id)

        if follow_tail:
            self.transcript.moveCursor(QTextCursor.MoveOperation.End)
            self.transcript.ensureCursorVisible()

    def _open_settings(self) -> None:
        self.settings_requested.emit()

    def reload_llm_settings(self) -> None:
        """Pick up a newly saved LLM config."""
        from system_sl.chatbot import reload_agent
        reload_agent()
        # The session holds an agent built from the previous config.
        self._session = None
        self._populate_model_list()
        self._start_discovery()

    def eventFilter(self, obj: QObject, event) -> bool:
        """Handle Enter/Shift+Enter in the input box."""
        if obj is self.input_box and event.type() == event.Type.KeyPress:
            from PySide6.QtCore import Qt
            from PySide6.QtGui import QKeyEvent
            key_event: QKeyEvent = event
            if key_event.key() == Qt.Key.Key_Escape and self._editing_message is not None:
                self._cancel_edit()
                return True
            if key_event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if key_event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    # Shift+Enter: insert newline (default behavior)
                    return False
                else:
                    # Enter: send message
                    self._on_send()
                    return True
        return super().eventFilter(obj, event)

    # ----- Attachment Handling -----

    def _on_attach_clicked(self) -> None:
        """Handle paperclip button click - open file dialog."""
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Attach Document",
            str(Path.home()),
            "Documents (*.pdf *.docx *.html *.htm *.md *.txt *.csv *.log *.json *.xml *.rst *.tex);;All Files (*)",
        )
        if file_paths:
            self._add_attachments([Path(p) for p in file_paths])

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """Accept drag events with file URLs."""
        if event.mimeData().hasUrls():
            # Filter to only accept local file URLs
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        """Handle dropped files."""
        urls = event.mimeData().urls()
        paths = []
        for url in urls:
            if url.isLocalFile():
                paths.append(Path(url.toLocalFile()))
        if paths:
            self._add_attachments(paths)
        event.acceptProposedAction()

    def _add_attachments(self, paths: list[Path]) -> None:
        """Add file paths to attachments and update chip UI."""
        for path in paths:
            if path not in self._attachments:
                self._attachments.append(path)
        self._update_attach_chips()

    def _remove_attachment(self, path: Path) -> None:
        """Remove an attachment and update chip UI."""
        if path in self._attachments:
            self._attachments.remove(path)
        self._update_attach_chips()

    def _update_attach_chips(self) -> None:
        """Rebuild the attachment chips row."""
        # Clear existing chips
        while self.attach_chips_layout.count() > 1:  # Keep the stretch at the end
            item = self.attach_chips_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Add new chips
        for path in self._attachments:
            chip = self._create_attachment_chip(path)
            self.attach_chips_layout.insertWidget(self.attach_chips_layout.count() - 1, chip)

        self.attach_chips_container.setVisible(len(self._attachments) > 0)

    def _create_attachment_chip(self, path: Path) -> QWidget:
        """Create a chip widget for an attached file."""
        tokens = get_current_theme_tokens()
        chip_background = tokens.get("sys_color_surface_raised", "#0e1626")
        chip_hover = tokens.get("sys_color_primary_dim", "rgba(0, 217, 255, 0.08)")
        chip_text = tokens.get("sys_color_text_main", "#d6e7ff")
        alert = tokens.get("sys_color_alert", "#ff6b8a")

        chip = QFrame()
        chip.setObjectName("attachmentChip")
        chip.setFrameShape(QFrame.StyledPanel)
        chip.setStyleSheet(f"""
            QFrame#attachmentChip {{
                background-color: {chip_background};
                border-radius: 12px;
                padding: 2px 8px;
            }}
            QFrame#attachmentChip:hover {{
                background-color: {chip_hover};
            }}
        """)
        layout = QHBoxLayout(chip)
        layout.setContentsMargins(8, 2, 4, 2)
        layout.setSpacing(4)

        # File name with size
        size_kb = path.stat().st_size / 1024
        if size_kb >= 1024:
            size_str = f"{size_kb / 1024:.1f} MB"
        else:
            size_str = f"{size_kb:.0f} KB"
        label = QLabel(f"{path.name} ({size_str})")
        label.setStyleSheet(f"color: {chip_text}; font-size: 11px;")
        layout.addWidget(label)

        # Remove button
        remove_btn = QPushButton()
        remove_btn.setIcon(get_icon("remove", role="sys_color_text_secondary", size=14))
        remove_btn.setFixedSize(18, 18)
        remove_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 9px;
            }}
            QPushButton:hover {{
                background: {alert};
            }}
        """)
        remove_btn.setToolTip("Remove attachment")
        remove_btn.setAccessibleName("Remove attachment")
        remove_btn.setAccessibleDescription(f"Remove the attached file {path.name}")
        remove_btn.clicked.connect(lambda: self._remove_attachment(path))
        layout.addWidget(remove_btn)

        return chip

    def _clear_attachments(self) -> None:
        """Clear all attachments."""
        self._attachments.clear()
        self._update_attach_chips()

    # ----- Copy & Quote -----

    def _on_transcript_context_menu(self, position) -> None:
        """Offer copy/quote for the message (or selection) under the pointer."""
        cursor = self.transcript.cursorForPosition(position)
        message_id = self._message_id_at(cursor.position())
        selected = self.transcript.textCursor()
        selection = selected.selection().toPlainText() if selected.hasSelection() else ""

        menu, entries = self._build_transcript_menu(
            message_id, selection, selected.selectionStart()
        )
        if menu is None:
            return

        chosen = menu.exec(self.transcript.mapToGlobal(position))
        if chosen is None:
            return
        for action, handler in entries:
            if chosen is action:
                handler()
                return

    def _build_transcript_menu(
        self, message_id: str | None, selection: str, selection_start: int = 0
    ):
        """Build the right-click menu for a message, or None when it has none.

        Returns the menu and a list of ``(action, handler)`` pairs; executing
        the menu is the caller's job, which keeps the menu testable without a
        running event loop.
        """
        from system_sl.chatbot.transcript import ALERT, ASSISTANT, USER

        message = self._messages.get(message_id) if message_id else None
        has_selection = bool((selection or "").strip())

        # Notes and alerts are the app talking to itself; only what a
        # participant said can be quoted back to the model.
        quoteable = message is not None and message.role in (USER, ASSISTANT)
        # A failed reply is shown as an alert; offer Retry there too.
        retryable = (
            message is not None
            and self._last_failed_user_id is not None
            and (message.role == ALERT or (message.role == ASSISTANT and message.error))
            and self._messages.index_of(message.id)
            > self._messages.index_of(self._last_failed_user_id)
        )
        if not has_selection and not quoteable and not retryable:
            return None, []

        menu = QMenu(self.transcript)
        entries: list[tuple[object, Callable[[], None]]] = []

        if has_selection:
            copy_action = menu.addAction("Copy")
            quote_action = menu.addAction("Quote selection")
            entries.append((copy_action, lambda: self._copy_to_clipboard(selection)))

            def quote_selection() -> None:
                # The speaker is the message the selection started in, which
                # may not be the one under the pointer.
                start_id = self._message_id_at(selection_start)
                start_message = self._messages.get(start_id) if start_id else None
                role = (
                    start_message.role
                    if start_message
                    else (message.role if message else USER)
                )
                self._quote_text(selection, role)

            entries.append((quote_action, quote_selection))

        if quoteable:
            if has_selection:
                menu.addSeparator()
            copy_message_action = menu.addAction("Copy message")
            quote_message_action = menu.addAction("Quote in reply")
            entries.append(
                (copy_message_action, lambda: self._copy_to_clipboard(message.plain_text()))
            )
            entries.append(
                (quote_message_action, lambda: self._quote_text(message.plain_text(), message.role))
            )

            # Message-specific actions
            if message.role == ASSISTANT:
                menu.addSeparator()
                regenerate_action = menu.addAction("Regenerate reply")
                entries.append((regenerate_action, lambda: self._regenerate_reply(message_id)))
            elif message.role == USER:
                menu.addSeparator()
                edit_resend_action = menu.addAction("Edit and resend")
                entries.append((edit_resend_action, lambda: self._edit_and_resend(message_id)))

        if retryable:
            menu.addSeparator()
            retry_action = menu.addAction("Retry")
            entries.append((retry_action, lambda: self._retry_message(message_id)))

        return menu, entries

    @staticmethod
    def _copy_to_clipboard(text: str) -> None:
        """Put plain text on the clipboard (never the rendered HTML)."""
        QApplication.clipboard().setText(text or "")

    def _on_anchor_clicked(self, url) -> None:
        """Handle anchor clicks in the transcript."""
        url_str = url.toString()
        if url_str.startswith("alert-detail:"):
            message_id = url_str.split(":", 1)[1]
            self._handle_alert_detail_link(message_id)
        else:
            # External link - open in browser
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(url)

    def _quote_text(self, text: str, role: str) -> None:
        """Add an excerpt to the next message's quotes."""
        from system_sl.chatbot.quotes import Quote, speaker_for_role

        text = (text or "").strip()
        if not text:
            return
        self._quotes.append(Quote(speaker=speaker_for_role(role), text=text))
        self._update_quote_chips()
        self.input_box.setFocus()

    def _remove_quote(self, quote) -> None:
        """Drop one quote chip."""
        try:
            self._quotes.remove(quote)
        except ValueError:
            return
        self._update_quote_chips()

    def _clear_quotes(self) -> None:
        """Drop every quote waiting to be sent."""
        self._quotes.clear()
        self._update_quote_chips()

    def _update_quote_chips(self) -> None:
        """Rebuild the quote chips row above the input."""
        while self.quote_chips_layout.count() > 1:  # keep the trailing stretch
            item = self.quote_chips_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for quote in self._quotes:
            chip = self._create_quote_chip(quote)
            self.quote_chips_layout.insertWidget(
                self.quote_chips_layout.count() - 1, chip
            )

        self.quote_chips_container.setVisible(bool(self._quotes))

    def _create_quote_chip(self, quote) -> QWidget:
        """Create a preview chip for one quoted excerpt."""
        tokens = get_current_theme_tokens()
        chip_background = tokens.get("sys_color_surface_raised", "#0e1626")
        chip_hover = tokens.get("sys_color_primary_dim", "rgba(0, 217, 255, 0.08)")
        chip_text = tokens.get("sys_color_text_main", "#d6e7ff")
        alert = tokens.get("sys_color_alert", "#ff6b8a")

        chip = QFrame()
        chip.setObjectName("quoteChip")
        chip.setFrameShape(QFrame.StyledPanel)
        chip.setStyleSheet(f"""
            QFrame#quoteChip {{
                background-color: {chip_background};
                border-radius: 12px;
                border-left: 3px solid {tokens.get("sys_color_primary", "#00d9ff")};
                padding: 2px 8px;
            }}
            QFrame#quoteChip:hover {{
                background-color: {chip_hover};
            }}
        """)
        layout = QHBoxLayout(chip)
        layout.setContentsMargins(8, 2, 4, 2)
        layout.setSpacing(4)

        preview = f'↩ {quote.label}: “{_truncate_preview(quote.text)}”'
        label = QLabel(preview)
        label.setStyleSheet(f"color: {chip_text}; font-size: 11px;")
        label.setToolTip(quote.text)
        layout.addWidget(label)

        remove_btn = QPushButton()
        remove_btn.setIcon(get_icon("remove", role="sys_color_text_secondary", size=14))
        remove_btn.setFixedSize(18, 18)
        remove_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 9px;
            }}
            QPushButton:hover {{
                background: {alert};
            }}
        """)
        remove_btn.setToolTip("Remove this quote")
        remove_btn.setAccessibleName("Remove quote")
        remove_btn.setAccessibleDescription("Remove this quoted excerpt from the message")
        remove_btn.clicked.connect(lambda: self._remove_quote(quote))
        layout.addWidget(remove_btn)

        return chip

    # ----- Session Management -----

    def _get_session(self) -> "ChatSession | None":
        """Return the session for the open conversation, built for the model.

        The conversation is one thread id regardless of model. When the
        dropdown changes, the same session is pointed at a different agent so
        the new model continues the existing history.
        """
        from system_sl.chatbot import ChatSession, ChatConfigError, load_config
        from system_sl.chatbot.llm_factory import LLMFactoryError
        from system_sl.chatbot.system import build_agent_for_model

        model_name = self._current_model or load_config().system_model

        try:
            if self._session is not None and self._session.thread_id == self._thread_id:
                # Asked every time: the agent is rebuilt when the config file
                # changed, and the open session must pick that up too.
                agent, llm = build_agent_for_model(model_name)
                if self._session.model_name != model_name or self._session.agent is not agent:
                    self._session.switch_model(model_name, agent=agent, llm=llm)
                return self._session

            agent, llm = build_agent_for_model(model_name)
            self._session = ChatSession(
                thread_id=self._thread_id,
                model_name=model_name,
                agent=agent,
                llm=llm,
            )
            return self._session
        except (ChatConfigError, LLMFactoryError) as e:
            # Config missing/invalid — offer to configure
            self._prompt_llm_config(str(e))
            return None
        except Exception as e:
            self._append_alert(f"Could not open channel: {e}")
            return None

    def _prompt_llm_config(self, error: str) -> None:
        """Prompt user to configure LLM when chat is used without valid config."""
        msg = QMessageBox(self)
        msg.setWindowTitle("LLM Not Configured")
        msg.setText("Chat features require an LLM provider.")
        msg.setInformativeText(
            f"{error}\n\n"
            "Configure an API key in Settings to enable the AI assistant."
        )
        configure_btn = msg.addButton("Open Settings", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        msg.setDefaultButton(configure_btn)
        msg.exec()

        if msg.clickedButton() == configure_btn:
            self._open_settings()

    # ----- Transcript Rendering -----

    def _get_live_tokens(self) -> dict:
        """Fetches the live theme colors based on the selected theme mode.

        Respects the player's theme choice (System / Light / Dark / Dynamic)
        stored in QSettings. Falls back to default tokens when unavailable.
        """
        return get_current_theme_tokens()

    def refresh_theme(self, tokens: dict | None = None) -> None:
        """Recolour the panel and the transcript for a new palette.

        Bubbles bake their colours in when drawn, so the transcript is redrawn
        from the message model; otherwise switching Light/Dark (or to a very
        different wallpaper palette) left old bubbles unreadable. A reply that
        is still streaming holds positions in the document, so the redraw
        waits until the turn ends; that also repaints the part of the reply
        streamed in the old colours.
        """
        tokens = tokens or self._get_live_tokens()
        if self.is_busy():
            self._theme_redraw_pending = True
        else:
            self._redraw_for_theme()
        from system_sl.frontend.gui.popup_windows import BatchProposalDialog

        for dialog in self.findChildren(BatchProposalDialog):
            dialog.refresh_theme(tokens)
        self._dots_bright = tokens.get("sys_color_primary", "#00d9ff")
        self._dots_dim = tokens.get("sys_color_text_secondary", "#9ab8e5")
        if self.reasoning_view.isVisible():
            dim = tokens.get("sys_color_text_secondary", "#9ab8e5")
            self.reasoning_view.setStyleSheet(
                f"QTextEdit {{ color: {dim}; font-style: italic;"
                f" border: none; border-left: 2px solid {dim};"
                f" background: transparent; padding-left: 6px; }}"
            )

    def _is_at_bottom(self, threshold: int = 48) -> bool:
        """Return True when the transcript is scrolled to (or near) the bottom.

        Auto-scroll must not fight the user: when they scroll up to read
        earlier messages, a streamed render must not yank them back down.
        A small threshold covers the rounding jitter of the scrollbar range
        while content is being appended.
        """
        bar = self.transcript.verticalScrollBar()
        return bar.value() >= bar.maximum() - threshold

    def _scroll_to_bottom(self) -> None:
        """Force the transcript to the end (a deliberate user action)."""
        self.transcript.moveCursor(QTextCursor.MoveOperation.End)
        self.transcript.ensureCursorVisible()

    @staticmethod
    def _format_timestamp(timestamp: str) -> tuple[str, str]:
        """Return (short_time, full_datetime) from ISO timestamp string."""
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local_dt = dt.astimezone()
            short = local_dt.strftime("%H:%M")
            full = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
            return short, full
        except Exception:
            return "", ""

    def _append_player(self, message: "ChatMessage") -> None:
        """Append a user (PLAYER) message as a right-aligned bubble, no label."""
        tokens = self._get_live_tokens()
        bg = tokens.get("sys_color_primary", "#00d9ff")
        fg = tokens.get("sys_color_on_primary", "#04121a")
        self._insert_bubble_html(
            self._player_html(message),
            align_right=True,
            bg_color=bg,
            text_color=fg,
            message_id=message.id,
            timestamp=message.timestamp,
        )

    def _player_html(self, message: "ChatMessage") -> str:
        """Bubble contents for a Player message: attachments, quotes, text."""
        parts: list[str] = []
        if message.attachments or message.documents:
            parts.append(
                self._attachment_summary_html(message.attachments, message.documents)
            )
        for quote in message.quotes:
            parts.append(self._quote_block_html(quote))
        parts.append(html.escape(message.body or "").replace("\n", "<br>"))
        return "".join(parts)

    @staticmethod
    def _attachment_summary_html(paths: list[str], documents: list[str] = ()) -> str:
        """The "📎 Attached:" preview shown inside a Player message.

        ``documents`` are names only (a message reopened from history: the
        text was read when it was sent).
        """
        attach_lines = ["📎 Attached:"]
        for path_str in paths:
            path = Path(path_str)
            try:
                size_kb = path.stat().st_size / 1024
                size_str = (
                    f" ({size_kb / 1024:.1f} MB)" if size_kb >= 1024 else f" ({size_kb:.0f} KB)"
                )
            except OSError:
                # The file is only decoration here; the text was already read.
                size_str = ""
            attach_lines.append(f"  • {path.name}{size_str}")
        for name in documents:
            attach_lines.append(f"  • {name}")
        return html.escape("\n".join(attach_lines)).replace("\n", "<br>") + "<br><br>"

    @staticmethod
    def _quote_block_html(quote) -> str:
        """One quoted excerpt, styled as a block-quote inside the bubble.

        Qt's rich text support for borders on block elements is unreliable, so
        the quote gets a vertical rule per line instead — the same look, and it
        survives every theme.
        """
        safe = html.escape(quote.text).replace("\n", "<br>│ ")
        label = html.escape(quote.label)
        return (
            '<div style="margin-bottom:4px; font-style:italic;">'
            f"│ <b>{label}:</b> {safe}</div>"
        )

    def _append_bot(self, message: "ChatMessage") -> None:
        """Append a complete SYSTEM message as a left-aligned bubble, no label."""
        tokens = self._get_live_tokens() or self._stream_tokens
        body_color = tokens.get("sys_color_text_main", "#d6e7ff")
        bg = tokens.get("sys_color_surface", "#2a2a40")
        self._append_markdown_bubble(
            message.text,
            align_right=False,
            bg_color=bg,
            text_color=body_color,
            message_id=message.id,
            timestamp=message.timestamp,
        )

    def _append_bubble(
        self,
        text: str,
        align_right: bool,
        bg_color: str,
        text_color: str,
        escape: bool = True,
        message_id: str | None = None,
        timestamp: str = "",
    ) -> None:
        """Insert an escaped chat bubble (one line per newline)."""
        safe = html.escape(text).replace("\n", "<br>") if escape else text.replace("\n", "<br>")
        self._insert_bubble_html(
            safe, align_right, bg_color, text_color, message_id, timestamp=timestamp
        )

    def _insert_bubble_html(
        self,
        body_html: str,
        align_right: bool,
        bg_color: str,
        text_color: str,
        message_id: str | None = None,
        timestamp: str = "",
    ) -> None:
        """Insert an already-rendered bubble, tagged with its message id."""
        # Respect the user's scroll position: only follow the tail if they
        # were already there. A bubble appended while they read history must
        # not yank the viewport back down.
        follow_tail = self._is_at_bottom()

        cursor = QTextCursor(self.transcript.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)

        # Block format for alignment (right for user, left for system)
        block_fmt = QTextBlockFormat()
        block_fmt.setAlignment(Qt.AlignmentFlag.AlignRight if align_right else Qt.AlignmentFlag.AlignLeft)
        block_fmt.setTopMargin(4)
        block_fmt.setBottomMargin(4)
        cursor.insertBlock(block_fmt)
        start = cursor.block().position()

        # Char format for bubble background + text color
        char_fmt = QTextCharFormat()
        char_fmt.setBackground(QColor(bg_color))
        char_fmt.setForeground(QColor(text_color))
        char_fmt.setFontFamily("Segoe UI, Helvetica Neue, DejaVu Sans, sans-serif")
        char_fmt.setFontPointSize(13)

        # Set char format on cursor, then insert HTML
        cursor.setCharFormat(char_fmt)
        
        cursor.insertHtml(body_html)

        # Qt's rich text ignores float/float-based corner tricks and the
        # ``title`` attribute on a span, so the timestamp gets its own small,
        # dim block. A char-format tooltip is the one hover hint Qt honours.
        if timestamp:
            short_time, full_time = self._format_timestamp(timestamp)
            if short_time:
                ts_color = self._get_live_tokens().get(
                    "sys_color_text_secondary", "#9ab8e5"
                )
                cursor.insertBlock()
                ts_block = QTextBlockFormat()
                ts_block.setAlignment(
                    Qt.AlignmentFlag.AlignRight
                    if align_right
                    else Qt.AlignmentFlag.AlignLeft
                )
                ts_block.setTopMargin(0)
                ts_block.setBottomMargin(4)
                cursor.setBlockFormat(ts_block)
                ts_fmt = QTextCharFormat()
                ts_fmt.setForeground(QColor(ts_color))
                ts_fmt.setFontPointSize(10)
                if full_time:
                    ts_fmt.setToolTip(full_time)
                cursor.setCharFormat(ts_fmt)
                cursor.insertText(short_time)

        self._tag_blocks(start, message_id)

        if follow_tail:
            self.transcript.moveCursor(QTextCursor.MoveOperation.End)
            self.transcript.ensureCursorVisible()

    def _append_markdown_bubble(
        self,
        markdown_text: str,
        align_right: bool,
        bg_color: str,
        text_color: str,
        escape: bool = False,
        message_id: str | None = None,
        timestamp: str = "",
    ) -> None:
        """Render markdown and insert as a styled bubble block."""
        if not markdown_text.strip():
            self._append_bubble(
                "", align_right, bg_color, text_color, escape=False, message_id=message_id, timestamp=timestamp
            )
            return
        
        # Convert markdown to HTML
        fragment = QTextDocumentFragment.fromMarkdown(_preserve_line_breaks(markdown_text)).toHtml()
        body = fragment.split("<body>", 1)[-1].rsplit("</body>", 1)[0]
        body = (
            body.replace("<!--StartFragment-->", "")
            .replace("<!--EndFragment-->", "")
            .strip()
        )
        if not body:
            self._append_bubble(
                "", align_right, bg_color, text_color, escape=False, message_id=message_id, timestamp=timestamp
            )
            return
        
        body = _colorize_markdown_html(body, text_color)
        body = self._enhance_markdown_html(body, text_color, bg_color)

        self._insert_bubble_html(body, align_right, bg_color, text_color, message_id, timestamp)

    def _enhance_markdown_html(self, html: str, text_color: str, bg_color: str) -> str:
        """Post-process markdown HTML to improve code blocks and tables."""
        import re
        
        # Enhance code blocks: add monospace, background, padding
        # Qt markdown produces <pre><code>...</code></pre>
        def enhance_code_block(match):
            code_content = match.group(1)
            return (
                f'<pre style="background:#1e1e2e; color:#d6e7ff; padding:8px 12px; '
                f'overflow-x:auto; font-family:\'JetBrains Mono\',\'Fira Code\',\'Consolas\',monospace; '
                f'font-size:12px; line-height:1.5; margin:4px 0;">{code_content}</pre>'
            )
        
        # Match <pre><code>...</code></pre> patterns
        html = re.sub(r'<pre><code>(.*?)</code></pre>', enhance_code_block, html, flags=re.DOTALL)
        # Also match <pre>...</pre> without inner <code>
        html = re.sub(r'<pre>(.*?)</pre>', enhance_code_block, html, flags=re.DOTALL)
        
        # Enhance inline code: <code>...</code>
        inline_code_style = (
            f'background:rgba(128,128,128,0.2); color:{text_color}; '
            f'padding:1px 4px; font-family:monospace; font-size:12px;'
        )
        html = re.sub(
            r'<code>(.*?)</code>',
            lambda m: f'<code style="{inline_code_style}">{m.group(1)}</code>',
            html
        )
        
        # Enhance tables: add borders and padding
        # Qt markdown produces basic <table> without styling
        table_style = (
            'border-collapse:collapse; width:100%; margin:8px 0; font-size:12px;'
        )
        th_style = (
            f'background:rgba(128,128,128,0.15); color:{text_color}; '
            f'border:1px solid rgba(128,128,128,0.3); padding:6px 10px; text-align:left; font-weight:bold;'
        )
        td_style = (
            f'color:{text_color}; '
            f'border:1px solid rgba(128,128,128,0.3); padding:6px 10px;'
        )
        
        html = re.sub(r'<table>', f'<table style="{table_style}">', html)
        html = re.sub(r'<th>', f'<th style="{th_style}">', html)
        html = re.sub(r'<td>', f'<td style="{td_style}">', html)
        
        return html

    def _append_alert(self, text: str) -> None:
        """Append an alert bubble and keep the transcript model in step."""
        from system_sl.chatbot.transcript import ALERT

        message = self._messages.append(ALERT, text)
        self._append_alert_bubble(text, message_id=message.id, timestamp=message.timestamp)

    def _append_alert_bubble(self, text: str, message_id: str | None = None, timestamp: str = "") -> None:
        safe = html.escape(text).replace("\n", "<br>")
        tokens = self._get_live_tokens()
        alert_color = tokens.get("sys_color_alert", "#ff6b8a")
        alert_text = tokens.get("sys_color_on_alert", "#ffffff")
        self._append_bubble(
            safe,
            align_right=False,
            bg_color=alert_color,
            text_color=alert_text,
            escape=False,
            message_id=message_id,
            timestamp=timestamp,
        )

    def _append_alert_with_details(self, friendly: str, detail: str) -> None:
        """Append an alert with a 'Details' link that opens a dialog with the raw error."""
        from system_sl.chatbot.transcript import ALERT

        message = self._messages.append(ALERT, friendly, error=detail)
        self._render_alert_with_details(message)

    def _render_alert_with_details(self, message) -> None:
        """Draw an alert whose raw error is kept on the message."""
        friendly = message.text
        tokens = self._get_live_tokens()
        alert_color = tokens.get("sys_color_alert", "#ff6b8a")
        alert_text = tokens.get("sys_color_on_alert", "#ffffff")
        dim_text = tokens.get("sys_color_text_secondary", "#9ab8e5")

        safe_friendly = html.escape(friendly).replace("\n", "<br>")
        # Use a custom anchor scheme that we can intercept
        detail_link = f'<a href="alert-detail:{message.id}" style="color:{dim_text}; font-size:11px; text-decoration:underline;">Show details ▸</a>'
        
        html_content = f"{safe_friendly}<br>{detail_link}"

        self._append_bubble(
            html_content,
            align_right=False,
            bg_color=alert_color,
            text_color=alert_text,
            escape=False,
            message_id=message.id,
            timestamp=message.timestamp,
        )


    def _handle_alert_detail_link(self, message_id: str) -> None:
        """Show the raw error detail in a dialog."""
        message = self._messages.get(message_id)
        detail = message.error if message is not None else ""
        if not detail:
            return
        from PySide6.QtWidgets import QMessageBox, QTextEdit
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Error Details")
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setText("Raw error details:")
        
        detail_edit = QTextEdit()
        detail_edit.setPlainText(detail)
        detail_edit.setReadOnly(True)
        detail_edit.setMinimumWidth(500)
        detail_edit.setMinimumHeight(200)
        detail_edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        dialog.layout().addWidget(detail_edit, 0, 0, 1, dialog.layout().columnCount())
        
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.exec()

    def _reset_stream_state(self) -> None:
        self._answer_open = False
        self._streamed_any = False
        self._reasoning_shown = False
        self._halted = False
        self._answer_text = ""
        self._answer_stopped = False
        self._answer_start = None
        self._last_rendered_html = ""
        self._stream_tokens = {}
        self._answer_committed_chars = 0
        self._stream_tail_start = None
        self._stream_message_id = None
        self._dots_frame = 0
        self._md_timer.stop()
        self._stop_dots()
        self.reasoning_view.clear()
        self.reasoning_view.setVisible(False)

    @staticmethod
    def _stable_answer_boundary(text: str) -> int:
        """Character offset just after the last completed markdown block.

        A block is complete once a blank line outside a code fence follows it.
        Everything after that offset is still growing and is re-rendered; the
        completed part is written to the document once and never rewritten.
        """
        boundary = 0
        position = 0
        in_fence = False
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
            elif not in_fence and not stripped:
                boundary = position + len(line)
            position += len(line)
        return boundary

    @staticmethod
    def _markdown_body(text: str) -> str:
        """Render markdown to the fragment Qt would insert (no wrapper)."""
        fragment = QTextDocumentFragment.fromMarkdown(
            _preserve_line_breaks(text)
        ).toHtml()
        body = fragment.split("<body>", 1)[-1].rsplit("</body>", 1)[0]
        return (
            body.replace("<!--StartFragment-->", "")
            .replace("<!--EndFragment-->", "")
            .strip()
        )

    def _answer_region_end(self) -> int:
        """Document position where the streaming answer stops.

        Normally that is the end of the document; while a note or alert was
        appended during the turn it is the start of the first such message,
        which must not be swallowed by the answer rewrite.
        """
        trailing = (
            self._messages.after(self._stream_message_id)
            if self._stream_message_id
            else []
        )
        if trailing:
            doc = self.transcript.document()
            block = doc.firstBlock()
            wanted = trailing[0].id
            while block.isValid():
                data = block.userData()
                if isinstance(data, _MessageBlockData) and data.message_id == wanted:
                    # End of the previous block: deleting up to the trailing
                    # block's start would also remove the separator and
                    # merge that message into the answer.
                    return max(0, block.position() - 1)
                block = block.next()
        return self.transcript.document().characterCount() - 1

    def _stream_char_format(self, bg: str, body_color: str) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(bg))
        fmt.setForeground(QColor(body_color))
        fmt.setFontFamily("Segoe UI, Helvetica Neue, DejaVu Sans, sans-serif")
        fmt.setFontPointSize(13)
        return fmt

    def _replace_answer_region(self, committed_html: str, tail_html: str) -> None:
        """Append completed markdown once, then replace only the tail.

        The completed markdown is inserted once. The tail (the paragraph the
        model is still writing) is deleted and re-rendered, so a long reply
        does not pay for a full-document rewrite on every token. The user's
        selection is preserved when it lies inside the answer, and the view
        follows the tail only if it was already at the bottom.
        """
        if self._stream_tail_start is None:
            return

        doc = self.transcript.document()
        start = min(self._stream_tail_start, doc.characterCount() - 1)
        end = self._answer_region_end()
        if end < start:
            end = start

        # Selection inside the answer is shifted by the length difference, so
        # reading a growing reply does not lose the highlighted words.
        saved_cursor = self.transcript.textCursor()
        had_selection = saved_cursor.hasSelection()
        sel_start = saved_cursor.selectionStart() if had_selection else -1
        sel_end = saved_cursor.selectionEnd() if had_selection else -1

        follow_tail = self._is_at_bottom()
        removed = max(0, end - start)
        if removed:
            cursor = QTextCursor(doc)
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()

        fmt = self._stream_char_format(
            self._stream_tokens.get("sys_color_surface", "#2a2a40"),
            self._stream_tokens.get("sys_color_text_main", "#d6e7ff"),
        )

        cursor = QTextCursor(doc)
        cursor.setPosition(start)
        cursor.setCharFormat(fmt)
        if committed_html:
            cursor.insertHtml(committed_html)
            start = cursor.position()

        if tail_html:
            block = doc.findBlock(start)
            # A tail needs a block of its own; otherwise it would be appended
            # to the paragraph that was just completed (``start`` is then in
            # the middle or at the end of that paragraph's block).
            if (
                not block.isValid()
                or start > block.position()
                or (block.position() == start and block.text())
            ):
                block_fmt = QTextBlockFormat()
                block_fmt.setAlignment(Qt.AlignmentFlag.AlignLeft)
                block_fmt.setTopMargin(4)
                block_fmt.setBottomMargin(4)
                block_fmt.setLeftMargin(8)
                block_fmt.setRightMargin(8)
                cursor.setBlockFormat(block_fmt)
                cursor.insertBlock()
                start = cursor.block().position()
            cursor.setCharFormat(fmt)
            cursor.insertHtml(tail_html)

        self._stream_tail_start = start
        new_end = cursor.position()
        self._tag_blocks(self._answer_start or start, self._stream_message_id, new_end)

        if had_selection:
            # Never yank the view to the tail while the Player is reading or
            # has text selected. A selection inside the rewritten region is
            # shifted by the length difference; one outside it is untouched.
            if sel_start >= start:
                restore = QTextCursor(doc)
                if sel_start >= end:
                    pos_start = sel_start - removed + (new_end - start)
                    pos_end = sel_end - removed + (new_end - start)
                else:
                    pos_start, pos_end = sel_start, sel_end
                restore.setPosition(max(0, pos_start))
                restore.setPosition(max(0, pos_end), QTextCursor.KeepAnchor)
                self.transcript.setTextCursor(restore)
        elif follow_tail:
            self.transcript.moveCursor(QTextCursor.MoveOperation.End)
            self.transcript.ensureCursorVisible()

    def _render_answer(self, final: bool = False) -> None:
        """Write newly completed markdown, then re-render the tail only.

        ``final`` re-renders the whole answer once from its full text. The
        incremental pass splits at blank lines, which is right for the live
        view but can't know about loose lists or indented code spanning a
        blank line, and the reply may differ from what streamed; the finished
        answer is always rendered from the complete markdown.
        """
        tokens = self._stream_tokens or self._get_live_tokens()
        body_color = tokens.get("sys_color_text_main", "#d6e7ff")
        self._stream_tokens = tokens
        if final and self._answer_start is not None:
            self._stream_tail_start = self._answer_start
            self._answer_committed_chars = 0
            self._last_rendered_html = ""

        if not self._answer_text.strip():
            committed_html = " "
            tail_html = ""
        else:
            boundary = (
                len(self._answer_text)
                if final
                else self._stable_answer_boundary(self._answer_text)
            )
            if boundary < self._answer_committed_chars:
                boundary = self._answer_committed_chars
            committed = self._answer_text[self._answer_committed_chars:boundary]
            tail = self._answer_text[boundary:]

            committed_html = ""
            if committed:
                body = self._markdown_body(committed)
                if body:
                    committed_html = _colorize_markdown_html(body, body_color)
            tail_html = ""
            if tail:
                body = self._markdown_body(tail)
                if body:
                    tail_html = _colorize_markdown_html(body, body_color)

        if self._answer_stopped:
            alert = tokens.get("sys_color_alert", "#ff6b8a")
            on_alert = tokens.get("sys_color_on_alert", "#ffffff")
            tail_html += (
                '<div style="margin-top:4px;">'
                f'<span style="background:{alert}; color:{on_alert};'
                ' padding:1px 8px; font-size:11px;">'
                "stopped</span></div>"
            )

        signature = (committed_html, tail_html)
        if signature == self._last_rendered_html:
            return
        self._last_rendered_html = signature
        self._replace_answer_region(committed_html, tail_html)
        if self._answer_text.strip():
            boundary = (
                len(self._answer_text)
                if final
                else self._stable_answer_boundary(self._answer_text)
            )
            self._answer_committed_chars = max(
                self._answer_committed_chars, boundary
            )

    def _start_answer_line(self) -> None:
        """Insert the answer block and start the loading dots."""
        self._stream_tokens = self._get_live_tokens()
        tokens = self._stream_tokens

        cursor = QTextCursor(self.transcript.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)

        block_fmt = QTextBlockFormat()
        block_fmt.setAlignment(Qt.AlignmentFlag.AlignLeft)
        block_fmt.setTopMargin(4)
        block_fmt.setBottomMargin(4)
        block_fmt.setLeftMargin(8)
        block_fmt.setRightMargin(8)
        cursor.insertBlock(block_fmt)

        self._answer_start = cursor.block().position()
        self._stream_tail_start = self._answer_start
        self._answer_committed_chars = 0
        self._last_rendered_html = ""
        self._answer_open = True

        from system_sl.chatbot.transcript import ASSISTANT

        self._stream_message_id = self._messages.append(
            ASSISTANT, "", streaming=True
        ).id

        self._dots_bright = tokens.get("sys_color_primary", "#00d9ff")
        self._dots_dim = tokens.get("sys_color_text_secondary", "#9ab8e5")

        # Loading dots live in the answer block until the first token arrives.
        cursor.setCharFormat(self._stream_char_format(
            tokens.get("sys_color_surface", "#2a2a40"),
            tokens.get("sys_color_text_main", "#d6e7ff"),
        ))
        cursor.insertText(" " + _DOT_CHAR * _DOT_COUNT)

        self._tag_blocks(self._answer_start, self._stream_message_id)

        self._dots_start = self._answer_start + 1  # after block position
        self._dots_frame = 0
        self._tick_dots()  # paint the first frame without waiting 200 ms
        self._dots_timer.start()

    # --- loading dots ---

    def _tick_dots(self) -> None:
        """Advance the bright dot one step along the fixed-width dot field."""
        if self._dots_start is None:
            return

        doc = self.transcript.document()
        if self._dots_start + _DOT_COUNT > doc.characterCount() - 1:
            self._stop_dots()
            return

        bright = QColor(self._dots_bright)
        dim = QColor(self._dots_dim)
        for offset in range(_DOT_COUNT):
            cursor = QTextCursor(doc)
            cursor.setPosition(self._dots_start + offset)
            cursor.setPosition(self._dots_start + offset + 1, QTextCursor.KeepAnchor)
            fmt = QTextCharFormat()
            fmt.setForeground(bright if offset == self._dots_frame else dim)
            cursor.mergeCharFormat(fmt)

        self._dots_frame = (self._dots_frame + 1) % _DOT_COUNT

    def _stop_dots(self) -> None:
        self._dots_timer.stop()
        self._dots_start = None

    # --- reasoning ---

    def _show_reasoning(self) -> None:
        if self._reasoning_shown:
            return
        self._reasoning_shown = True
        tokens = self._stream_tokens or self._get_live_tokens()
        dim = tokens.get("sys_color_text_secondary", "#9ab8e5")
        self.reasoning_view.setStyleSheet(
            f"QTextEdit {{ color: {dim}; font-style: italic;"
            f" border: none; border-left: 2px solid {dim};"
            f" background: transparent; padding-left: 6px; }}"
        )
        self.reasoning_view.setVisible(True)

    def _on_reasoning(self, delta: str) -> None:
        """Stream reasoning text into the dimmed thinking view."""
        if self._halted:
            return
        self._show_reasoning()
        # Same rule as the transcript: do not fight the user's scroll position.
        bar = self.reasoning_view.verticalScrollBar()
        follow_tail = bar.value() >= bar.maximum() - 48
        cursor = self.reasoning_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(delta)
        self.reasoning_view.setTextCursor(cursor)
        if follow_tail:
            self.reasoning_view.ensureCursorVisible()

    def _on_tool_activity(self, tool_name: str, status: str, detail: str) -> None:
        """Display tool activity in the reasoning view."""
        if self._halted:
            return
        
        # Map tool names to friendly descriptions
        tool_descriptions = {
            "load_tasks": "Reading your tasks…",
            "load_completed_tasks": "Reading completed tasks…",
            "add_task": "Adding task…",
            "remove_task": "Removing task…",
            "mark_task_completed": "Marking task complete…",
            "get_random_task": "Picking a focus task…",
            "propose_tasks_batch": "Proposing tasks from document…",
            "read_persona": "Reading your profile…",
        }
        
        friendly = tool_descriptions.get(tool_name, f"Running {tool_name}…")
        
        if status == "start":
            self._show_reasoning()
            # Replace the reasoning view content with the tool activity
            self.reasoning_view.clear()
            self.reasoning_view.setPlainText(friendly)
        elif status == "end":
            # Tool finished: show a brief tick, then hide the view unless
            # something else (reasoning, another tool) has replaced it.
            done = f"✓ {friendly.rstrip('…')}"
            self.reasoning_view.setPlainText(done)

            def clear_if_unchanged() -> None:
                if self.reasoning_view.toPlainText() == done:
                    self.reasoning_view.clear()
                    self.reasoning_view.setVisible(False)
                    self._reasoning_shown = False

            QTimer.singleShot(1500, clear_if_unchanged)

    # --- answer ---

    def _on_chunk(self, delta: str) -> None:
        """Append one streamed answer delta and schedule a re-render."""
        if self._halted:
            return

        first = not self._answer_text
        self._answer_text += delta
        self._streamed_any = True
        if self._stream_message_id:
            self._messages.set_text(self._stream_message_id, self._answer_text)

        if first:
            # The answer has started: drop the loading dots and the reasoning
            # pane, show the first token immediately, then throttle renders.
            self._stop_dots()
            self.reasoning_view.clear()
            self.reasoning_view.setVisible(False)
            self._reasoning_shown = False
            self._render_answer()
        elif not self._md_timer.isActive():
            self._md_timer.start()

    def _mark_stopped(self) -> None:
        if not self._answer_open:
            return
        # Keep the reply text clean. Anything that later stores or quotes it
        # must not inherit a "[stopped]" suffix, so the marker is rendered as
        # part of the bubble instead and only recorded as a flag on the model.
        self._answer_stopped = True
        if self._stream_message_id:
            self._messages.set_flag(
                self._stream_message_id, stopped=True, streaming=False
            )
        self._render_answer(final=True)
        self._answer_open = False

    # ----- Task Confirmation -----

    def _on_proposal(self, proposal: dict) -> None:
        """Queue a task the agent tried to add without being asked."""
        if self._halted:
            return  # the Player abandoned this turn; drop late proposals

        # The answer belongs to the conversation that proposed it, even if the
        # Player has switched conversation (or pressed Stop) before answering.
        session = self._worker.session if self._worker is not None else self._session

        # Handle batch proposals from document attachments
        if proposal.get("type") == "batch":
            self._show_batch_proposal_dialog(proposal, session)
            return

        queued = dict(proposal)
        queued["_session"] = session
        self._pending_proposals.append(queued)
        self._show_next_proposal()

    def _on_attachment_notice(self, notice: str) -> None:
        """Tell the Player that an attachment was cut or could not be read."""
        if not notice:
            return
        self._append_alert(f"attachment: {notice}")

    def _show_batch_proposal_dialog(self, proposal: dict, session=None) -> None:
        """Show the batch task proposal dialog (non-modal)."""
        from system_sl.frontend.gui.popup_windows import BatchProposalDialog

        tasks = proposal.get("tasks", [])
        if not tasks:
            return

        def as_pairs(items: list[dict]) -> list[tuple[str, str]]:
            pairs = []
            for task in items:
                title = str(task.get("title") or "").strip()
                category = str(task.get("category") or "study").strip().lower()
                if title:
                    pairs.append((title, category))
            return pairs

        dialog = BatchProposalDialog(tasks, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.confirmed.connect(
            lambda selected_tasks: self._on_batch_confirmed(
                selected_tasks, tasks, as_pairs, session
            )
        )
        dialog.cancelled.connect(
            lambda: self._on_batch_cancelled(tasks, as_pairs, session)
        )
        dialog.show()

    def _on_batch_confirmed(
        self, selected_tasks: list[dict], all_tasks: list[dict], as_pairs, session=None
    ) -> None:
        """Handle batch dialog confirmation."""
        # Matched by row, not title: titles can be edited in the dialog and
        # two proposed tasks can share one.
        chosen = {task.get("source_index") for task in selected_tasks}
        skipped = [task for i, task in enumerate(all_tasks) if i not in chosen]
        self._add_batch_tasks(selected_tasks, declined=as_pairs(skipped), session=session)

    def _on_batch_cancelled(self, all_tasks: list[dict], as_pairs, session=None) -> None:
        """Handle batch dialog cancellation (Cancel, Esc or the window's X)."""
        self._record_task_confirmation(declined=as_pairs(all_tasks), session=session)

    def _show_next_proposal(self) -> None:
        """Show the next queued proposal, or hide the bar when none remain."""
        if self._current_proposal is not None:
            return  # one is already on screen; the new one is queued behind it
        if not self._pending_proposals:
            self.confirm_bar.setVisible(False)
            return

        proposal = self._pending_proposals.pop(0)
        self._current_proposal = proposal

        title = str(proposal.get("title") or "(untitled)")
        category = str(proposal.get("category") or "none")
        queued = len(self._pending_proposals)
        suffix = f"   (+{queued} more queued)" if queued else ""
        self.confirm_label.setText(f'Add "{title}" to {category}?{suffix}')
        self.confirm_bar.setVisible(True)

    def _on_confirm_yes(self) -> None:
        proposal = self._current_proposal
        self._current_proposal = None

        if proposal is not None:
            session = proposal.get("_session")
            title = str(proposal.get("title") or "").strip()
            category = str(proposal.get("category") or "none")
            deadline = proposal.get("deadline")

            existing = {str(task.get("title", "")) for task in load_tasks()}
            if not title:
                self._show_after_turn(self._append_alert, "cannot add a task without a title")
            elif title in existing:
                self._show_after_turn(self._append_alert, f"task '{title}' already exists")
                self._record_task_confirmation(failed=[(title, category)], session=session)
            else:
                try:
                    add_tasks(category, title, deadline)
                except ValueError as e:
                    self._show_after_turn(self._append_alert, str(e))
                    self._record_task_confirmation(failed=[(title, category)], session=session)
                except OSError as e:
                    self._show_after_turn(
                        self._append_alert, f"could not save the new task: {e}"
                    )
                    self._record_task_confirmation(failed=[(title, category)], session=session)
                else:
                    self._show_after_turn(
                        self._append_confirmation, f'Added "{title}" to {category}.'
                    )
                    self._record_task_confirmation(added=[(title, category)], session=session)

        self._show_next_proposal()

    def _on_confirm_no(self) -> None:
        proposal = self._current_proposal
        if proposal is not None:
            title = str(proposal.get("title") or "").strip()
            category = str(proposal.get("category") or "none")
            if title:
                self._record_task_confirmation(
                    declined=[(title, category)], session=proposal.get("_session")
                )
        self._current_proposal = None
        self._show_next_proposal()

    def _record_task_confirmation(
        self,
        added: list[tuple[str, str]] | None = None,
        declined: list[tuple[str, str]] | None = None,
        failed: list[tuple[str, str]] | None = None,
        session=None,
    ) -> None:
        """Tell the conversation what happened to proposed tasks.

        ``session`` is the conversation that proposed them; it defaults to the
        open one. Passing it keeps an answer given after a conversation switch
        or a Stop from being dropped or written into the wrong conversation.

        While a reply is streaming, the write waits for the turn to finish: the
        agent is still running on this thread, and writing a checkpoint
        underneath it would race. Everything queued in one 'event' (a batch
        dialog, clearing the proposal queue) becomes one summarizing message.
        """
        added = list(added or [])
        declined = list(declined or [])
        failed = list(failed or [])
        if not (added or declined or failed):
            return

        session = session if session is not None else self._session
        if session is None:
            logger.warning("task outcome dropped: no conversation to record it in")
            return

        if self.is_busy():
            self._pending_confirmations.append((session, added, declined, failed))
            return
        session.record_task_confirmation(added=added, declined=declined, failed=failed)

    def _flush_pending_confirmations(self) -> None:
        """Write the task outcomes queued while the reply was streaming."""
        pending, self._pending_confirmations = self._pending_confirmations, []
        for session, added, declined, failed in pending:
            session.record_task_confirmation(
                added=added, declined=declined, failed=failed
            )

    def _repair_session_after_turn(self, session) -> None:
        """Close a graph step the finished worker left pending (Stop, crash).

        Repairing here keeps the checkpoint valid as soon as the turn ends, so
        an already-open second window, or the next launch, reads a clean state.
        ChatSession also repairs before every turn as a safety net.
        """
        from system_sl.chatbot.memory import repair_interrupted_turn

        try:
            repair_interrupted_turn(session.agent, session.config)
        except Exception:
            logger.warning(
                "could not close the interrupted turn in conversation %s",
                session.thread_id,
                exc_info=True,
            )

    def _clear_proposals(self) -> None:
        """Drop queued proposals and record them as declined, in one note."""
        queue = list(self._pending_proposals)
        if self._current_proposal is not None:
            queue.append(self._current_proposal)

        # One note per conversation that proposed something.
        by_session: dict[int, tuple[object, list[tuple[str, str]]]] = {}
        for proposal in queue:
            title = str(proposal.get("title") or "").strip()
            category = str(proposal.get("category") or "none")
            if title:
                session = proposal.get("_session") or self._session
                by_session.setdefault(id(session), (session, []))[1].append((title, category))
        for session, declined in by_session.values():
            self._record_task_confirmation(declined=declined, session=session)

        self._pending_proposals.clear()
        self._current_proposal = None
        self.confirm_bar.setVisible(False)

    def _add_batch_tasks(
        self,
        tasks: list[dict],
        declined: list[tuple[str, str]] | None = None,
        session=None,
    ) -> None:
        """Add the tasks the Player kept in the batch dialog."""
        added_tasks = []  # Track (title, category) for confirmation feedback
        failed_tasks: list[tuple[str, str]] = []
        added = 0
        errors = []
        for task in tasks:
            title = task.get("title", "").strip()
            category = task.get("category", "study").strip().lower()
            deadline = task.get("deadline")
            if not title:
                continue
            try:
                add_tasks(category, title, deadline)
                added += 1
                added_tasks.append((title, category))
            except ValueError as e:
                errors.append(f"{title}: {e}")
                failed_tasks.append((title, category))
            except OSError as e:
                errors.append(f"{title}: could not save: {e}")
                failed_tasks.append((title, category))

        # No re-ranking here: it is slow (spaCy, spell checking) and would
        # freeze the chat. The Tasks window ranks off the GUI thread when it
        # refreshes, as it does for a single confirmed task.

        if added:
            self._show_after_turn(
                self._append_confirmation, f"Added {added} task(s) from document."
            )
        # One summarizing note for the whole batch: which were added, which
        # were skipped in the dialog, and which failed (duplicates, save errors).
        self._record_task_confirmation(
            added=added_tasks,
            declined=list(declined or []),
            failed=failed_tasks,
            session=session,
        )
        for err in errors:
            self._show_after_turn(self._append_alert, err)

    def _append_confirmation(self, text: str) -> None:
        from system_sl.chatbot.transcript import CONFIRMATION

        message = self._messages.append(CONFIRMATION, text)
        self._append_confirmation_bubble(text, message_id=message.id, timestamp=message.timestamp)

    def _append_confirmation_bubble(self, text: str, message_id: str | None = None, timestamp: str = "") -> None:
        tokens = self._get_live_tokens()
        self._append_bubble(
            text,
            align_right=False,
            bg_color=tokens.get("sys_color_surface", "#2a2a40"),
            text_color=tokens.get("sys_color_primary", "#00d9ff"),
            message_id=message_id,
            timestamp=timestamp,
        )

    # ----- Message Actions -----

    # ----- Regenerate / retry / edit-and-resend -----

    def _preceding_user_message(self, message_id: str):
        """The Player message a reply (or alert) belongs to."""
        from system_sl.chatbot.transcript import USER

        index = self._messages.index_of(message_id)
        for message in reversed(self._messages.messages[: max(index, 0)]):
            if message.role == USER:
                return message
        return None

    def _rewind_to(self, user_message) -> bool:
        """Rewind the conversation to just before ``user_message``.

        The stored history (what the model remembers) is cut first and the
        transcript is rebuilt from it, so the screen and the model always agree.
        Returns False, with an alert, when it could not be done.
        """
        from system_sl.chatbot.memory import update_conversation
        from system_sl.chatbot.transcript import USER

        if self.is_busy():
            self._append_alert("wait for the reply to finish first")
            return False
        session = self._get_session()
        if session is None:
            return False

        # Proposals belong to the turn being discarded; settle them first so
        # their notes are written (and then rewound) in order.
        self._clear_proposals()
        self._flush_pending_confirmations()

        is_last = self._messages.last(USER) is user_message
        try:
            if not user_message.source_id:
                raise LookupError("message has no stored id")
            session.rewind_before(user_message.source_id)
        except LookupError:
            # Never stored (the turn failed before it reached the
            # conversation). Only the latest message can be resent safely.
            if not is_last:
                self._append_alert("this message is not in the saved conversation")
                return False
        except Exception:
            logger.warning("could not rewind conversation %s", self._thread_id, exc_info=True)
            self._append_alert("could not rewind the conversation; nothing was changed")
            return False

        messages = self._messages_for_thread(self._thread_id)
        self._render_transcript(messages)
        # The rewound turns no longer count towards the context.
        self._estimate_context(messages)
        if self._messages.last(USER) is None:
            # Back to an empty conversation: let the resent message title it.
            update_conversation(self._thread_id, title="")
            self._refresh_history_combo()
        self._last_failed_user_id = None
        return True

    def _resend(self, user_message) -> None:
        """Send a Player message again exactly as it was first sent."""
        attachments = [p for p in (user_message.attachments or []) if Path(p).exists()]
        missing = len(user_message.attachments or []) - len(attachments)
        if missing:
            self._append_alert(f"{missing} attached file(s) no longer exist and were left out")
        # ``text`` is what was sent: the quote blocks and the Player's words.
        self._send_now(user_message.text, attachments)

    def _confirm_discard(self, user_message) -> bool:
        """Ask before discarding later turns of the conversation."""
        from system_sl.chatbot.transcript import USER

        later = [
            m for m in self._messages.after(user_message.id) if m.role == USER
        ]
        if not later:
            return True
        answer = QMessageBox.question(
            self,
            "Discard later messages?",
            f"This also removes the {len(later)} message(s) you sent after it, "
            "and their replies, from the conversation.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _regenerate_reply(self, message_id: str) -> None:
        """Ask again for the reply to a Player message, forgetting the old one."""
        user_message = self._preceding_user_message(message_id)
        if user_message is None:
            self._append_alert("no message found to regenerate from")
            return
        if not self._confirm_discard(user_message):
            return
        if self._rewind_to(user_message):
            self._resend(user_message)

    def _retry_message(self, message_id: str) -> None:
        """Send the Player message whose reply failed again."""
        user_message = self._messages.get(self._last_failed_user_id or "") or (
            self._preceding_user_message(message_id)
        )
        if user_message is None:
            self._append_alert("no message found to retry")
            return
        if self._rewind_to(user_message):
            self._resend(user_message)

    def _edit_and_resend(self, message_id: str) -> None:
        """Load an earlier Player message into the input to edit it.

        Nothing changes until the edit is sent; Esc or Cancel leaves the
        conversation as it was.
        """
        from system_sl.chatbot.transcript import USER

        if self.is_busy():
            self._append_alert("cannot edit while a reply is in progress")
            return
        message = self._messages.get(message_id)
        if not message or message.role != USER:
            return

        self._editing_message = message
        self.input_box.setPlainText(message.body or message.text)
        self._quotes = list(message.quotes or [])
        self._update_quote_chips()
        self._attachments = [Path(p) for p in (message.attachments or []) if Path(p).exists()]
        self._update_attach_chips()
        later = len([m for m in self._messages.after(message.id) if m.role == USER])
        note = ""
        # A reopened conversation only kept the document names; the text was
        # compacted out of the stored history. Editing and resending cannot
        # recover it, so say so instead of silently dropping the context.
        if message.documents and not self._attachments:
            count = len(message.documents)
            note = (
                f" {count} attached document(s) are only names now; "
                "their text is not resent."
            )
        self.edit_label.setText(
            tr("Editing an earlier message — sending replaces it")
            + (tr(f" and the {later} message(s) after it.") if later else ".")
            + note
            + tr(" Esc to cancel.")
        )
        self.edit_bar.setVisible(True)
        self.input_box.setFocus()
        self.input_box.moveCursor(QTextCursor.MoveOperation.End)

    def _cancel_edit(self, clear_input: bool = True) -> None:
        """Leave edit mode without changing the conversation."""
        if self._editing_message is None:
            return
        self._editing_message = None
        self.edit_bar.setVisible(False)
        if clear_input:
            self.input_box.clear()
            self._clear_quotes()
            self._clear_attachments()

    def _show_after_turn(self, append: Callable[[str], None], text: str) -> None:
        """Add a note to the transcript once the running reply has finished.

        Agents propose tasks mid-reply, so the Player can respond while it is
        still streaming. Each render rewrites the transcript from the start of
        the answer to its end, which would erase a note added in the meantime.
        """
        if self.is_busy():
            self._notes_after_turn.append((append, text))
        else:
            append(text)

    # ----- Input Flow -----

    def _set_busy(self, busy: bool) -> None:
        self.send_button.setEnabled(not busy)
        # The input stays enabled while the assistant works: the Player can
        # compose the next message and queue it.
        self.input_box.setEnabled(True)
        self.model_combo.setEnabled(not busy)
        self.refresh_btn.setEnabled(not busy)
        self.settings_btn.setEnabled(not busy)
        # Switching conversations mid-reply would orphan the running turn.
        self.history_combo.setEnabled(not busy)
        self.new_chat_btn.setEnabled(not busy)
        self.delete_chat_btn.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.busy_changed.emit(busy)

    def is_busy(self) -> bool:
        return self._thread is not None

    def _update_queue_badge(self) -> None:
        """Show or hide the badge that counts queued messages."""
        count = len(self._message_queue)
        if count <= 0:
            self.queue_badge.setVisible(False)
            self.queue_badge.setText("")
            return
        noun = "message" if count == 1 else "messages"
        self.queue_badge.setText(f"⏳ {count} {noun} queued — will send when the reply finishes")
        self.queue_badge.setVisible(True)

    def _on_queue_badge_clicked(self) -> None:
        """Clear the queued messages when the badge is clicked."""
        if not self._message_queue:
            return
        dropped = len(self._message_queue)
        self._message_queue.clear()
        self._update_queue_badge()
        self._append_alert(f"cleared {dropped} queued message(s)")

    def _on_send(self) -> None:
        """Send the composed message, or queue it if a reply is in flight."""
        text = self.input_box.toPlainText().strip()
        if not text:
            return

        if self._editing_message is not None:
            # Edit-and-resend: the conversation is rewound now, on send, so
            # cancelling an edit never loses anything.
            if self.is_busy():
                self._append_alert("wait for the reply to finish before sending the edit")
                return
            if not self._rewind_to(self._editing_message):
                return
            self._cancel_edit(clear_input=False)

        # Capture attachments and quotes before clearing
        attachment_paths = [str(p) for p in self._attachments]
        quotes = list(self._quotes)
        self.input_box.clear()
        self._clear_attachments()
        self._clear_quotes()

        if quotes:
            # The quotes become delimited blocks above the Player's own text,
            # within this message's share of the context budget.
            text = self._compose_turn(text, quotes)

        if self.is_busy():
            # A turn is already running: queue this one instead of dropping it.
            self._message_queue.append((text, attachment_paths))
            self._update_queue_badge()
            self._append_alert("queued — will send when the reply finishes")
            return

        self._send_now(text, attachment_paths)

    def _compose_turn(self, body: str, quotes: list) -> str:
        """Fit the quotes into the budget and put them above ``body``.

        The budget is derived from the model's context window (session 2's
        attachment budget), so quoting several long messages cannot push the
        real question out of the window. A cut quote says so in the transcript.
        """
        from system_sl.chatbot.context_window import (
            context_window_for_llm,
            context_window_for_model,
        )
        from system_sl.chatbot.quotes import fit_quotes, format_quotes, quote_budget_chars

        # The model's own profile is the best source, as for attachments.
        llm = getattr(self._session, "llm", None)
        window = (
            context_window_for_llm(llm)
            if llm is not None
            else context_window_for_model(self._current_model)
        )
        fitted, notices = fit_quotes(quotes, quote_budget_chars(window))
        for notice in notices:
            self._append_alert(f"quote: {notice}")
        return format_quotes(fitted, body)

    def _send_now(self, text: str, attachment_paths: list[str]) -> None:
        """Run a turn immediately with the given message and attachments."""
        from system_sl.chatbot import load_config
        from system_sl.chatbot.llm_factory import (
            get_cached_llm_clients,
            register_active_llm,
            _set_current_llm_clients,
        )
        from system_sl.chatbot.memory import (
            ensure_conversation,
            title_from_message,
        )

        model = self._current_model or load_config().system_model
        session = self._get_session()
        if session is None:
            # No usable session (LLM not configured, or the provider
            # rejected the credentials). The message was already taken out
            # of the input box, so put it back rather than losing it.
            self.input_box.setPlainText(text)
            self.input_box.setFocus()
            return

        # The Player's turn joins the transcript model first; the view and the
        # conversation title both read from it.
        from system_sl.chatbot.transcript import USER

        import uuid

        player_message = self._messages.append(
            USER,
            text,
            attachments=list(attachment_paths),
            source_id=f"player-{uuid.uuid4().hex}",
        )
        self._turn_user_id = player_message.id
        self._last_failed_user_id = None

        # Title the conversation from its first message and remember which
        # model is answering; both are read by the history dropdown. The title
        # comes from what the Player typed, not from the quotes above it.
        # The conversation may still be local-only (New chat was never used);
        # the first message is what makes it appear in history.
        ensure_conversation(
            self._thread_id,
            model_name=model,
            title=title_from_message(player_message.body or text),
        )
        self._refresh_history_combo()

        self._render_message(player_message)
        # The previous reply's numbers no longer describe this turn; the
        # context fill stays visible until the new reply reports its own.
        self._reply_usage_line = ""
        self._refresh_usage_label()
        # Sending is a deliberate action: bring the new message into view.
        self._scroll_to_bottom()
        self._reset_stream_state()
        self._set_busy(True)
        self._start_answer_line()

        # Track the HTTP clients of the LLM that will actually answer, so
        # Stop cancels this stream and not another model's.
        register_active_llm(session.llm)
        sync_client, async_client = get_cached_llm_clients()
        _set_current_llm_clients(sync_client, async_client)

        self._thread = QThread(self)
        self._worker = _ChatWorker(
            session,
            text,
            attachment_paths,
            typed_text=player_message.body,
            message_id=player_message.source_id,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.reasoning.connect(self._on_reasoning)
        self._worker.chunk.connect(self._on_chunk)
        self._worker.usage.connect(self._on_usage)
        self._worker.finished.connect(self._on_reply)
        self._worker.failed.connect(self._on_failure)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.proposal.connect(self._on_proposal)
        self._worker.attachment_notice.connect(self._on_attachment_notice)
        self._worker.tool_activity.connect(self._on_tool_activity)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._worker.cancelled.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()

    def _on_stop(self) -> None:
        """Stop showing the reply immediately and abandon it in the worker."""
        if self._thread is None or self._halted:
            return
        had_output = self._streamed_any
        self._halted = True
        self._md_timer.stop()
        self._stop_dots()
        self._mark_stopped()
        self.reasoning_view.clear()
        self.reasoning_view.setVisible(False)
        self._reasoning_shown = False
        if not had_output:
            self._append_alert("stopped before any reply")
        # Stop is an abort gesture: drop anything that was queued behind the
        # reply the Player just cancelled. Otherwise the next queued message
        # would fire the moment this turn unwinds, which reads as "my Stop
        # button started another reply".
        if self._message_queue:
            dropped = len(self._message_queue)
            self._message_queue.clear()
            self._update_queue_badge()
            self._append_alert(f"stopped — dropped {dropped} queued message(s)")
        # Clear pending proposals and feed back "declined" to the model
        self._clear_proposals()
        # Cancel the in-flight HTTP request by closing the LLM's HTTP client(s).
        from system_sl.chatbot.llm_factory import cancel_current_stream
        cancel_current_stream()
        # Reset the agent cache so the next request gets a fresh LLM with new HTTP clients.
        from system_sl.chatbot import reset_agent_cache
        reset_agent_cache()
        # Drop the session object; the next turn rebuilds it for the same
        # conversation on a fresh agent. The checkpoint is untouched, and any
        # dangling tool call is repaired before the following turn.
        self._session = None
        self._thread.requestInterruption()
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip("Stopping...")

    def _on_reply(self, reply: str) -> None:
        self._md_timer.stop()
        self._stop_dots()
        if self._halted:
            return

        # Prefer the authoritative reply text when the fallback path produced it.
        if reply and reply != self._answer_text:
            self._answer_text = reply
            self._last_rendered_html = ""
        if not self._answer_text:
            self._answer_text = "[empty reply]"
            self._last_rendered_html = ""
        if self._stream_message_id:
            self._messages.set_text(self._stream_message_id, self._answer_text)
            self._messages.set_flag(self._stream_message_id, streaming=False)

        self._render_answer(final=True)
        self._answer_open = False
        self.reasoning_view.clear()
        self.reasoning_view.setVisible(False)
        self._reasoning_shown = False
        self._stop_dots()

    def _on_usage(self, usage: dict) -> None:
        """Record the context fill and the cost of the reply that ended."""
        from system_sl.chatbot.usage import TokenUsage, format_usage

        if not isinstance(usage, dict) or not usage:
            return
        reply = TokenUsage(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
        )
        if not reply.is_empty:
            self._turn_usage = reply
        context_tokens = int(usage.get("context_tokens") or 0)
        if context_tokens:
            self._context_tokens = context_tokens
            self._context_estimated = False
        line = format_usage(reply, self._current_model)
        if line:
            self._reply_usage_line = f"last reply {line}"
        self._refresh_usage_label()

    def _estimate_context(self, messages) -> None:
        """Approximate the context fill of a conversation just opened.

        Real counts arrive with the next reply; until then the stored history
        is counted locally (marked "~"). The system prompt and tool schemas
        are not included, so this is a lower bound.
        """
        from system_sl.chatbot.context_window import count_message_tokens

        try:
            self._context_tokens = count_message_tokens(messages or [])
        except Exception:
            logger.debug("could not estimate the context size", exc_info=True)
            self._context_tokens = 0
        self._context_estimated = True
        self._reply_usage_line = ""
        self._refresh_usage_label()

    def _conversation_total_line(self) -> str:
        from system_sl.chatbot.memory import get_conversation
        from system_sl.chatbot.usage import format_conversation_total

        if not self._thread_id:
            return ""
        try:
            entry = get_conversation(self._thread_id) or {}
        except Exception:
            return ""
        return format_conversation_total(entry.get("usage"))

    def _record_turn_usage(self, thread_id: str | None) -> None:
        """Add the turn that just ended to its conversation's totals."""
        usage, self._turn_usage = self._turn_usage, None
        if usage is None or not thread_id:
            return
        from system_sl.chatbot.memory import add_conversation_usage
        from system_sl.chatbot.usage import estimate_cost

        try:
            add_conversation_usage(
                thread_id,
                usage.input_tokens,
                usage.output_tokens,
                estimate_cost(usage, self._current_model or ""),
            )
        except Exception:
            logger.warning("could not record the conversation's usage", exc_info=True)
        if thread_id == self._thread_id:
            self._refresh_usage_label()

    def _refresh_usage_label(self) -> None:
        from system_sl.chatbot.context_window import context_window_for_model
        from system_sl.chatbot.usage import format_context

        parts = []
        if self._context_tokens:
            try:
                window = context_window_for_model(self._current_model or "")
            except Exception:
                window = 0
            context = format_context(
                self._context_tokens, window, estimated=self._context_estimated
            )
            if context:
                parts.append(context)
                self._usage_context_ratio = self._context_tokens / window
        if self._reply_usage_line:
            parts.append(self._reply_usage_line)
        total = self._conversation_total_line()
        if total:
            parts.append(total)
        if not parts:
            self.usage_label.clear()
            self.usage_label.setVisible(False)
            return
        tokens = self._stream_tokens or self._get_live_tokens()
        colour = tokens.get("sys_color_text_secondary", "#9ab8e5")
        if self._context_tokens and getattr(self, "_usage_context_ratio", 0) >= 0.8:
            # Nearly full: older turns are about to be trimmed or summarised.
            colour = tokens.get("sys_color_alert", "#ff6b8a")
        self.usage_label.setStyleSheet(f"color: {colour}; font-size: 11px;")
        self.usage_label.setText("  ·  ".join(parts))
        self.usage_label.setVisible(True)

    @staticmethod
    def _friendly_error(message: str) -> tuple[str, str | None]:
        """Map a worker's raw error ("ExcType: text") to a plain message.

        Returns ``(friendly, detail)``. ``detail`` is the raw error, shown on
        demand, or None when ``friendly`` already is the whole story. See
        ``chatbot.errors.classify_error`` for how errors are recognised.
        """
        from system_sl.chatbot.errors import classify_error

        return classify_error(message)

    def _on_failure(self, message: str) -> None:
        self._last_failed_user_id = self._turn_user_id
        self._md_timer.stop()
        self._stop_dots()
        self._render_answer(final=True)  # clears the loading dots if no token ever arrived
        if self._stream_message_id:
            self._messages.set_flag(
                self._stream_message_id, error=message, streaming=False
            )
        self.reasoning_view.clear()
        self.reasoning_view.setVisible(False)
        self._reasoning_shown = False
        self._answer_open = False
        if self._halted:
            return

        friendly, detail = self._friendly_error(message)
        if detail:
            # Log the raw error for debugging
            logger.error("LLM error: %s", detail)
            # Show friendly message with expandable details
            self._append_alert_with_details(friendly, detail)
        else:
            self._append_alert(friendly)

    def _on_cancelled(self) -> None:
        self._md_timer.stop()
        self._stop_dots()
        if not self._halted:
            # Interrupted without a Stop press (e.g. shutdown).
            self._mark_stopped()
        # Clear pending proposals and feed back "declined" to the model
        self._clear_proposals()
        self.reasoning_view.clear()
        self.reasoning_view.setVisible(False)
        self._reasoning_shown = False

    def _cleanup_thread(self) -> None:
        self._md_timer.stop()
        self._stop_dots()
        # Keep the session of the turn that just ended: it is needed to close a
        # graph step the worker abandoned.
        finished_session = (
            getattr(self._worker, "session", None) if self._worker is not None else None
        )
        had_attachments = bool(getattr(self._worker, "attachment_paths", None))
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None
        # Clear tracked LLM HTTP clients so they can be re-acquired for next request.
        from system_sl.chatbot.llm_factory import _clear_current_llm_clients
        _clear_current_llm_clients()
        # The worker has stopped, so the checkpoint is safe to write again:
        # close a turn that was cancelled mid-tool, then record any task
        # outcome the Player answered while it was streaming.
        self._record_turn_usage(
            getattr(finished_session, "thread_id", None) or self._thread_id
        )
        if finished_session is not None:
            self._repair_session_after_turn(finished_session)
            if had_attachments:
                # The attachment text only mattered for this turn: shrink it
                # in the stored history so later turns do not pay for it
                # again. Also after Stop or an error, or it would stay forever.
                try:
                    finished_session.compact_attachment_references()
                except Exception:
                    logger.warning("could not compact the attachments", exc_info=True)
        notes, self._notes_after_turn = self._notes_after_turn, []
        for append, text in notes:
            append(text)
        self._flush_pending_confirmations()
        if self._theme_redraw_pending:
            self._redraw_for_theme()
        self.stop_button.setToolTip("Interrupt the reply")
        self._set_busy(False)
        self.input_box.setFocus()

        # The turn is over: send the next queued message, if any.
        if self._message_queue:
            queued_text, queued_attachments = self._message_queue.pop(0)
            self._update_queue_badge()
            # Defer so the finished turn's widgets settle before the next
            # one starts; a direct call would re-enter the worker setup
            # while this signal is still being delivered.
            QTimer.singleShot(0, lambda: self._send_now(queued_text, queued_attachments))

    def shutdown(self, wait_ms: int = 3000) -> None:
        """End a running reply and save its outcome before the app quits.

        Connected to ``QApplication.aboutToQuit`` (the panel is a child
        widget, so it gets no closeEvent). Task outcomes the Player answered
        while the reply streamed are written to the conversation only once
        the worker has stopped: writing the checkpoint underneath a running
        agent would race with it. If the worker does not stop in time, the
        conversation is left as is and repaired on the next launch.
        """
        if self._shut_down:
            return
        self._shut_down = True
        self._message_queue.clear()
        self._cancel_edit(clear_input=False)

        thread = self._thread
        if thread is not None:
            self._on_stop()
            # The worker's "done" signals reach thread.quit through the GUI
            # event loop, which is blocked in wait() below; quit() itself is
            # thread-safe and takes effect once the worker returns.
            thread.quit()
            if not thread.wait(wait_ms):
                logger.warning(
                    "the reply did not stop within %d ms; its task outcomes "
                    "will not be recorded",
                    wait_ms,
                )
                self._worker_outlived_shutdown = True
                return
            # thread.finished is queued and will not be delivered after quit,
            # so finish the turn here: repair, then flush the outcomes.
            self._cleanup_thread()
        else:
            self._flush_pending_confirmations()

    def worker_stopped(self) -> bool:
        """False when a reply worker is still running after ``shutdown``.

        The checkpointer must then stay open: the worker may still write
        through it, and closing the connection underneath it raises.
        """
        return not getattr(self, "_worker_outlived_shutdown", False)

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)
