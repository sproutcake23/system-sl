"""Chat panel widget for System-SL GUI."""

import re
import html
from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QTextBlockFormat,
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

# Lazy-loaded chatbot imports (moved to functions to avoid startup cost)
# from system_sl.chatbot import ...
# from system_sl.chatbot.llm_factory import LLMFactoryError

if TYPE_CHECKING:
    from system_sl.chatbot import ChatConfig, ChatSession, ModelInfo
    from system_sl.chatbot.llm_factory import ProviderPreset


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


# =============================================================================
# SETTINGS DIALOG
# =============================================================================

class LLMSettingsPage(QWidget):
    """LLM provider and model settings form with model fetching."""

    saved = Signal()
    finished = Signal(bool)  # True for OK after a successful save, False for Cancel

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        from system_sl.chatbot import load_config, ModelInfo
        self._config = load_config()
        self._fetched_models: "list[ModelInfo]" = []
        self._fetch_thread: QThread | None = None
        self._fetch_worker: _ModelFetchWorker | None = None

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
        self.fetch_btn.clicked.connect(self._fetch_models)
        key_row.addWidget(self.fetch_btn)

        form.addRow("API Key:", key_row)

        # Model selection (manual entry, used if no model selected from table)
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("e.g., llama3.1, gemini-3.5-flash-lite (or select from table below)")
        form.addRow("Model:", self.model_edit)

        # Test Connection Button
        self.test_btn = QPushButton()
        self.test_btn.setIcon(get_icon("info"))
        self.test_btn.setText("Test Connection")
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
        self.search_edit.textChanged.connect(self._filter_models)
        search_row.addWidget(self.search_edit, stretch=1)

        self.select_free_btn = QPushButton()
        self.select_free_btn.setIcon(get_icon("select_all"))
        self.select_free_btn.setText("Select All Free")
        self.select_free_btn.clicked.connect(self._select_all_free)
        self.select_free_btn.setEnabled(False)
        search_row.addWidget(self.select_free_btn)

        self.refresh_cache_btn = QPushButton()
        self.refresh_cache_btn.setIcon(get_icon("refresh"))
        self.refresh_cache_btn.setText("Refresh Cache")
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
        self._fetch_thread.finished.connect(self._cleanup_fetch_thread)
        self._fetch_thread.start()

    def _on_fetch_progress(self, message: str) -> None:
        self.fetch_btn.setText(message)

    def _on_models_fetched(self, models: "list[ModelInfo]") -> None:
        """Handle successful model fetch."""
        from system_sl.chatbot import set_model_cache
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
        QMessageBox.critical(self, "Fetch Failed", f"Could not fetch models:\n{error}")

    def _cleanup_fetch_thread(self) -> None:
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
        )

    def _test_connection(self) -> None:
        """Test the current configuration by creating an LLM and sending a ping."""
        from system_sl.chatbot import validate_config
        from system_sl.chatbot.llm_factory import create_llm

        config = self._collect_config()

        errors = validate_config(config)
        if errors:
            QMessageBox.warning(self, "Invalid Config", "\n".join(errors))
            return

        self.test_btn.setEnabled(False)
        self.test_btn.setText("Testing...")

        try:
            llm = create_llm(
                provider=config.provider,
                model=config.system_model,
                base_url=config.base_url,
                api_key=config.api_key,
            )
            resp = llm.invoke([{"role": "user", "content": "ping"}])
            QMessageBox.information(self, "Success", f"Connection OK!\nResponse: {resp.content[:100]}")
        except Exception as e:
            QMessageBox.critical(self, "Connection Failed", f"{type(e).__name__}: {e}")
        finally:
            self.test_btn.setEnabled(True)
            self.test_btn.setText("Test Connection")

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


# =============================================================================
# CHAT WORKER
# =============================================================================

class _ChatWorker(QObject):
    reasoning = Signal(str)
    chunk = Signal(str)
    finished = Signal(str)
    failed = Signal(str)
    cancelled = Signal()
    proposal = Signal(dict)

    def __init__(self, session: "ChatSession", user_msg: str, attachment_paths: list[str] | None = None):
        super().__init__()
        self.session = session
        self.user_msg = user_msg
        self.attachment_paths = attachment_paths or []

    def run(self):
        from system_sl.chatbot.documents import extract_document, format_for_chat, DocumentText, UnsupportedFileError, ExtractionError
        thread = QThread.currentThread()
        collected: list[str] = []

        # Extract text from attachments first
        augmented_msg = self.user_msg
        if self.attachment_paths:
            doc_parts = []
            for path_str in self.attachment_paths:
                try:
                    path = Path(path_str)
                    doc: DocumentText = extract_document(path)
                    doc_parts.append(format_for_chat(doc, path.name))
                except UnsupportedFileError as e:
                    doc_parts.append(f"[uploaded document: {Path(path_str).name} — unsupported file type: {e}]")
                except ExtractionError as e:
                    doc_parts.append(f"[uploaded document: {Path(path_str).name} — extraction failed: {e}]")
                except Exception as e:
                    doc_parts.append(f"[uploaded document: {Path(path_str).name} — error: {type(e).__name__}: {e}]")

            if doc_parts:
                augmented_msg = "\n\n".join(doc_parts) + "\n\n" + self.user_msg

        try:
            for kind, delta in self.session.stream_turn(
                augmented_msg, on_proposal=self.proposal.emit
            ):
                if thread.isInterruptionRequested():
                    self.cancelled.emit()
                    return
                if kind == "reasoning":
                    self.reasoning.emit(delta)
                else:
                    collected.append(delta)
                    self.chunk.emit(delta)
        except NotImplementedError:
            # Provider or model cannot stream; fall back to a blocking turn.
            try:
                reply = self.session.run_turn(
                    augmented_msg, on_proposal=self.proposal.emit
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

        self.finished.emit("".join(collected))


# =============================================================================
# CHAT PANEL
# =============================================================================

class ChatPanel(QWidget):
    settings_requested = Signal()
    busy_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._sessions: "dict[str, ChatSession]" = {}  # model_name -> ChatSession
        self._current_model: str = ""
        self._thread: QThread | None = None
        self._worker: _ChatWorker | None = None

        # Attachment state
        self._attachments: list[Path] = []

        # Live-streaming state
        self._answer_open: bool = False       # the [SYSTEM] line exists
        self._streamed_any: bool = False      # any answer text arrived
        self._reasoning_shown: bool = False   # the thinking view is populated
        self._halted: bool = False            # user pressed Stop

        self._answer_text: str = ""           # raw markdown accumulated this turn
        self._answer_start: int | None = None # doc position of the [SYSTEM] line
        self._last_rendered_html: str = ""
        self._stream_tokens: dict = {}        # theme captured once per turn

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
        self._notes_after_turn: list[tuple[Callable[[str], None], str]] = []

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
        self.refresh_btn.clicked.connect(self._refresh_models)
        header.addWidget(self.refresh_btn)

        # Settings button
        self.settings_btn = QPushButton()
        self.settings_btn.setIcon(get_icon("settings"))
        self.settings_btn.setFixedWidth(36)
        self.settings_btn.setToolTip("LLM Settings")
        self.settings_btn.clicked.connect(self._open_settings)
        header.addWidget(self.settings_btn)

        root.addLayout(header)

        # --- Transcript ---
        self.transcript = QTextBrowser()
        self.transcript.setReadOnly(True)
        self.transcript.setOpenExternalLinks(True)
        self.transcript.setPlaceholderText("[ awaiting Player input — channel: SYSTEM ]")
        # Enable drag & drop on transcript
        self.transcript.setAcceptDrops(True)
        root.addWidget(self.transcript, stretch=1)

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
        # Paperclip button for attachments
        self.attach_btn = QPushButton()
        self.attach_btn.setIcon(get_icon("attach"))
        self.attach_btn.setFixedWidth(36)
        self.attach_btn.setFixedHeight(36)
        self.attach_btn.setToolTip("Attach document (PDF, DOCX, HTML, TXT, MD, CSV...)")
        self.attach_btn.clicked.connect(self._on_attach_clicked)
        self.send_button = QPushButton()
        self.send_button.setIcon(get_icon("send"))
        self.send_button.setFixedWidth(36)
        self.send_button.setFixedHeight(36)
        self.send_button.setToolTip("Send message (Enter)")
        self.send_button.clicked.connect(self._on_send)
        self.stop_button = QPushButton()
        self.stop_button.setIcon(get_icon("stop"))
        self.stop_button.setFixedWidth(36)
        self.stop_button.setFixedHeight(36)
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip("Interrupt the reply")
        self.stop_button.clicked.connect(self._on_stop)
        input_row.addWidget(self.input_box, stretch=1)
        input_row.addWidget(self.attach_btn)
        input_row.addWidget(self.send_button)
        input_row.addWidget(self.stop_button)
        input_row.setAlignment(Qt.AlignmentFlag.AlignBottom)
        root.addLayout(input_row)

        # Enable drag & drop on the panel itself
        self.setAcceptDrops(True)

        # Initialize model list
        self._populate_model_list()

    # ----- Model Management -----

    def _populate_model_list(self) -> None:
        """Populate the model dropdown with configured + discovered + cached models."""
        from system_sl.chatbot import load_config, discover_local_models, get_model_cache, get_preset_models
        self.model_combo.blockSignals(True)
        self.model_combo.clear()

        config = load_config()
        current_model = config.system_model

        has_config = bool(current_model and config.provider)

        if has_config:
            # 1. Current configured model (first, bold)
            self.model_combo.addItem(f"✦ {current_model}", current_model)
        else:
            # Placeholder when no config
            self.model_combo.addItem("⚙ Configure LLM in Settings…", "")
            self.model_combo.model().item(0).setEnabled(False)

        # 2. Separator
        self.model_combo.insertSeparator(1)

        # 3. Discovered local models
        local_models = discover_local_models()
        for display, model_id in local_models:
            self.model_combo.addItem(display, model_id)

        # 4. Cached models from OpenRouter (if any)
        # provider is only ever "google" or "openai"; the endpoint is what
        # identifies OpenRouter.
        if config.base_url and "openrouter.ai" in config.base_url.lower():
            cached = get_model_cache("openrouter")
            if cached:
                self.model_combo.insertSeparator(self.model_combo.count())
                for model in cached:
                    badge = "✨ " if model.is_free else ""
                    self.model_combo.addItem(f"☁️ {badge}{model.name} ({model.id})", model.id)

        # 5. Separator if we have cloud preset models
        cloud_models = get_preset_models()
        if cloud_models:
            self.model_combo.insertSeparator(self.model_combo.count())

        # 6. Cloud preset models
        for display, model_id in cloud_models:
            self.model_combo.addItem(display, model_id)

        # Select current model
        if has_config:
            idx = self.model_combo.findData(current_model)
            if idx >= 0:
                self.model_combo.setCurrentIndex(idx)
            else:
                self.model_combo.setCurrentIndex(0)
        else:
            self.model_combo.setCurrentIndex(0)

        self._current_model = current_model
        self.model_combo.blockSignals(False)

    def _refresh_models(self) -> None:
        """Refresh the model list from local servers."""
        self._populate_model_list()

    def _on_model_changed(self, index: int) -> None:
        """Handle model selection change."""
        if index < 0:
            return

        model_id = self.model_combo.itemData(index)
        if not model_id or model_id == self._current_model:
            return

        from system_sl.chatbot import load_config, save_config, reload_agent
        # Update config with new model
        config = load_config()
        config.system_model = model_id
        save_config(config)

        # Reload agent with new model
        reload_agent()

        # Clear session cache for new model
        self._sessions.clear()
        self._current_model = model_id
        self._clear_proposals()

        self._append_alert(f"Switched to model: {model_id}")

    def _open_settings(self) -> None:
        self.settings_requested.emit()

    def reload_llm_settings(self) -> None:
        """Pick up a newly saved LLM config."""
        from system_sl.chatbot import reload_agent
        reload_agent()
        self._populate_model_list()

    def eventFilter(self, obj: QObject, event) -> bool:
        """Handle Enter/Shift+Enter in the input box."""
        if obj is self.input_box and event.type() == event.Type.KeyPress:
            from PySide6.QtCore import Qt
            from PySide6.QtGui import QKeyEvent
            key_event: QKeyEvent = event
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
        remove_btn.clicked.connect(lambda: self._remove_attachment(path))
        layout.addWidget(remove_btn)

        return chip

    def _clear_attachments(self) -> None:
        """Clear all attachments."""
        self._attachments.clear()
        self._update_attach_chips()

    # ----- Session Management -----

    def _get_session(self, model_name: str) -> "ChatSession | None":
        """Get or create a ChatSession for the given model."""
        if model_name not in self._sessions:
            # Imported here, not at module level: the chatbot package pulls in
            # langchain, and the panel is built during GUI startup.
            from system_sl.chatbot import ChatSession, ChatConfigError
            from system_sl.chatbot.llm_factory import LLMFactoryError
            try:
                self._sessions[model_name] = ChatSession(model_name=model_name)
            except (ChatConfigError, LLMFactoryError) as e:
                # Config missing/invalid — offer to configure
                self._prompt_llm_config(str(e))
                return None
            except Exception as e:
                self._append_alert(f"Could not open channel: {e}")
                return None
        return self._sessions[model_name]

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
        """Refresh colours captured at construction time.

        Transcript bubbles already on screen keep their colour, but the loading
        dots and the reasoning panel pick up the new palette.
        """
        tokens = tokens or self._get_live_tokens()
        self._dots_bright = tokens.get("sys_color_primary", "#00d9ff")
        self._dots_dim = tokens.get("sys_color_text_secondary", "#9ab8e5")
        if self.reasoning_view.isVisible():
            dim = tokens.get("sys_color_text_secondary", "#9ab8e5")
            self.reasoning_view.setStyleSheet(
                f"QTextEdit {{ color: {dim}; font-style: italic;"
                f" border: none; border-left: 2px solid {dim};"
                f" background: transparent; padding-left: 6px; }}"
            )

    def _append_player(self, text: str) -> None:
        """Append a user (PLAYER) message as a right-aligned bubble, no label."""
        tokens = self._get_live_tokens()
        bg = tokens.get("sys_color_primary", "#00d9ff")
        fg = tokens.get("sys_color_on_primary", "#04121a")
        self._append_bubble(text, align_right=True, bg_color=bg, text_color=fg, escape=True)

    def _append_player_with_attachments(self, text: str, attachment_paths: list[str]) -> None:
        """Append a user message with attachment info as a right-aligned bubble."""
        tokens = self._get_live_tokens()
        bg = tokens.get("sys_color_primary", "#00d9ff")
        fg = tokens.get("sys_color_on_primary", "#04121a")

        # Build attachment summary
        attach_lines = ["📎 Attached:"]
        for path_str in attachment_paths:
            path = Path(path_str)
            size_kb = path.stat().st_size / 1024
            size_str = f"{size_kb / 1024:.1f} MB" if size_kb >= 1024 else f"{size_kb:.0f} KB"
            attach_lines.append(f"  • {path.name} ({size_str})")
        attach_summary = "\n".join(attach_lines)

        combined = f"{attach_summary}\n\n{text}" if text else attach_summary
        self._append_bubble(combined, align_right=True, bg_color=bg, text_color=fg, escape=True)

    def _append_bot(self, text: str) -> None:
        """Append a complete SYSTEM message as a left-aligned bubble, no label."""
        tokens = self._get_live_tokens() or self._stream_tokens
        body_color = tokens.get("sys_color_text_main", "#d6e7ff")
        bg = tokens.get("sys_color_surface", "#2a2a40")
        self._append_markdown_bubble(text, align_right=False, bg_color=bg, text_color=body_color)

    def _append_bubble(self, text: str, align_right: bool, bg_color: str, text_color: str, escape: bool = True) -> None:
        """Insert a chat bubble using QTextCursor + QTextBlockFormat for proper alignment."""
        safe = html.escape(text).replace("\n", "<br>") if escape else text.replace("\n", "<br>")
        
        cursor = QTextCursor(self.transcript.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        
        # Block format for alignment (right for user, left for system)
        block_fmt = QTextBlockFormat()
        block_fmt.setAlignment(Qt.AlignmentFlag.AlignRight if align_right else Qt.AlignmentFlag.AlignLeft)
        block_fmt.setTopMargin(4)
        block_fmt.setBottomMargin(4)
        cursor.insertBlock(block_fmt)
        
        # Char format for bubble background + text color
        char_fmt = QTextCharFormat()
        char_fmt.setBackground(QColor(bg_color))
        char_fmt.setForeground(QColor(text_color))
        char_fmt.setFontFamily("Segoe UI, Helvetica Neue, DejaVu Sans, sans-serif")
        char_fmt.setFontPointSize(13)
        
        # Set char format on cursor, then insert HTML
        cursor.setCharFormat(char_fmt)
        cursor.insertHtml(safe)
        
        # Ensure scroll to bottom
        self.transcript.moveCursor(QTextCursor.MoveOperation.End)
        self.transcript.ensureCursorVisible()

    def _append_markdown_bubble(self, markdown_text: str, align_right: bool, bg_color: str, text_color: str, escape: bool = False) -> None:
        """Render markdown and insert as a styled bubble block."""
        if not markdown_text.strip():
            self._append_bubble("", align_right, bg_color, text_color, escape=False)
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
            self._append_bubble("", align_right, bg_color, text_color, escape=False)
            return
        
        body = _colorize_markdown_html(body, text_color)
        
        cursor = QTextCursor(self.transcript.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        
        block_fmt = QTextBlockFormat()
        block_fmt.setAlignment(Qt.AlignmentFlag.AlignRight if align_right else Qt.AlignmentFlag.AlignLeft)
        block_fmt.setTopMargin(4)
        block_fmt.setBottomMargin(4)
        cursor.insertBlock(block_fmt)
        
        char_fmt = QTextCharFormat()
        char_fmt.setBackground(QColor(bg_color))
        char_fmt.setForeground(QColor(text_color))
        char_fmt.setFontFamily("Segoe UI, Helvetica Neue, DejaVu Sans, sans-serif")
        char_fmt.setFontPointSize(13)
        
        cursor.setCharFormat(char_fmt)
        cursor.insertHtml(body)
        
        self.transcript.moveCursor(QTextCursor.MoveOperation.End)
        self.transcript.ensureCursorVisible()

    def _append_alert(self, text: str) -> None:
        safe = html.escape(text).replace("\n", "<br>")
        tokens = self._get_live_tokens()
        alert_color = tokens.get("sys_color_alert", "#ff6b8a")
        alert_text = tokens.get("sys_color_on_alert", "#ffffff")
        self._append_bubble(safe, align_right=False, bg_color=alert_color, text_color=alert_text, escape=False)

    def _reset_stream_state(self) -> None:
        self._answer_open = False
        self._streamed_any = False
        self._reasoning_shown = False
        self._halted = False
        self._answer_text = ""
        self._answer_start = None
        self._last_rendered_html = ""
        self._stream_tokens = {}
        self._dots_frame = 0
        self._md_timer.stop()
        self._stop_dots()
        self.reasoning_view.clear()
        self.reasoning_view.setVisible(False)

    def _replace_answer_region(self, rendered_html: str) -> None:
        """Rewrite the streaming answer block with freshly rendered HTML."""
        if self._answer_start is None:
            return

        doc = self.transcript.document()
        end = doc.characterCount() - 1
        if end <= self._answer_start:
            return

        cursor = QTextCursor(doc)
        cursor.setPosition(self._answer_start)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()

        # Reset block format to avoid inheriting list formatting
        block = doc.findBlock(self._answer_start)
        text_list = block.textList()
        if text_list is not None:
            text_list.remove(block)

        cursor = QTextCursor(doc)
        cursor.setPosition(self._answer_start)
        cursor.setBlockFormat(QTextBlockFormat())
        cursor.insertHtml(rendered_html)

        self.transcript.moveCursor(QTextCursor.MoveOperation.End)
        self.transcript.ensureCursorVisible()

    def _render_answer(self) -> None:
        """Re-render the streamed answer, skipping no-op rewrites."""
        tokens = self._stream_tokens or self._get_live_tokens()
        body_color = tokens.get("sys_color_text_main", "#d6e7ff")
        bg = tokens.get("sys_color_surface", "#2a2a40")
        
        if not self._answer_text.strip():
            rendered = self._empty_answer_html(bg, body_color)
        else:
            fragment = QTextDocumentFragment.fromMarkdown(_preserve_line_breaks(self._answer_text)).toHtml()
            body = fragment.split("<body>", 1)[-1].rsplit("</body>", 1)[0]
            body = (
                body.replace("<!--StartFragment-->", "")
                .replace("<!--EndFragment-->", "")
                .strip()
            )
            if not body:
                rendered = self._empty_answer_html(bg, body_color)
            else:
                body = _colorize_markdown_html(body, body_color)
                rendered = self._wrap_answer_html(body, bg, body_color)
        
        if rendered == self._last_rendered_html:
            return
        self._last_rendered_html = rendered
        self._replace_answer_region(rendered)

    def _empty_answer_html(self, bg: str, body_color: str) -> str:
        """HTML for an empty answer bubble."""
        return (
            f'<div style="background:{bg}; color:{body_color}; border-radius:12px; '
            f'padding:6px 12px; max-width:75%; text-align:left; word-wrap:break-word;">'
            f'<span style="color:{body_color}"> </span></div>'
        )

    def _wrap_answer_html(self, body: str, bg: str, body_color: str) -> str:
        """Wrap rendered markdown in a left-aligned bubble div."""
        return (
            f'<div style="background:{bg}; color:{body_color}; border-radius:12px; '
            f'padding:6px 12px; max-width:75%; text-align:left; word-wrap:break-word;">'
            f'{body}</div>'
        )

    def _start_answer_line(self) -> None:
        """Insert an empty left-aligned block for the streaming SYSTEM answer and start dots."""
        self._stream_tokens = self._get_live_tokens()
        tokens = self._stream_tokens
        
        # Insert an empty left-aligned block for the answer
        cursor = QTextCursor(self.transcript.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        
        block_fmt = QTextBlockFormat()
        block_fmt.setAlignment(Qt.AlignmentFlag.AlignLeft)
        block_fmt.setTopMargin(4)
        block_fmt.setBottomMargin(4)
        cursor.insertBlock(block_fmt)
        
        self._answer_start = cursor.block().position()
        self._last_rendered_html = ""
        self._answer_open = True

        self._dots_bright = tokens.get("sys_color_primary", "#00d9ff")
        self._dots_dim = tokens.get("sys_color_text_secondary", "#9ab8e5")

        # Insert loading dots in the answer block
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self._dots_dim))
        cursor.insertText(" " + _DOT_CHAR * _DOT_COUNT, fmt)

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
        cursor = self.reasoning_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(delta)
        self.reasoning_view.setTextCursor(cursor)
        self.reasoning_view.ensureCursorVisible()

    # --- answer ---

    def _on_chunk(self, delta: str) -> None:
        """Append one streamed answer delta and schedule a re-render."""
        if self._halted:
            return

        first = not self._answer_text
        self._answer_text += delta
        self._streamed_any = True

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
        self._answer_text += " [stopped]"
        self._render_answer()
        self._answer_open = False

    # ----- Task Confirmation -----

    def _on_proposal(self, proposal: dict) -> None:
        """Queue a task the agent tried to add without being asked."""
        if self._halted:
            return  # the Player abandoned this turn; drop late proposals

        # Handle batch proposals from document attachments
        if proposal.get("type") == "batch":
            self._show_batch_proposal_dialog(proposal)
            return

        self._pending_proposals.append(dict(proposal))
        self._show_next_proposal()

    def _show_batch_proposal_dialog(self, proposal: dict) -> None:
        """Show the batch task proposal dialog."""
        from system_sl.frontend.gui.popup_windows import BatchProposalDialog

        tasks = proposal.get("tasks", [])
        if not tasks:
            return

        dialog = BatchProposalDialog(tasks, self)
        if dialog.exec() == QDialog.Accepted:
            # User confirmed - add the selected tasks
            selected_tasks = [task for _row, task in dialog.get_selected_tasks()]
            if selected_tasks:
                self._add_batch_tasks(selected_tasks)
        # Dialog handles its own cleanup

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
            title = str(proposal.get("title") or "").strip()
            category = str(proposal.get("category") or "none")
            deadline = proposal.get("deadline")

            existing = {str(task.get("title", "")) for task in load_tasks()}
            if not title:
                self._show_after_turn(self._append_alert, "cannot add a task without a title")
            elif title in existing:
                self._show_after_turn(self._append_alert, f"task '{title}' already exists")
            else:
                try:
                    add_tasks(category, title, deadline)
                except ValueError as e:
                    self._show_after_turn(self._append_alert, str(e))
                else:
                    self._show_after_turn(
                        self._append_confirmation, f'Added "{title}" to {category}.'
                    )

        self._show_next_proposal()

    def _on_confirm_no(self) -> None:
        self._current_proposal = None
        self._show_next_proposal()

    def _clear_proposals(self) -> None:
        self._pending_proposals.clear()
        self._current_proposal = None
        self.confirm_bar.setVisible(False)

    def _add_batch_tasks(self, tasks: list[dict]) -> None:
        """Add multiple tasks from batch proposal and re-prioritize."""
        from system_sl.core.priority_engine_new import run_prioritization

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
            except ValueError as e:
                errors.append(f"{title}: {e}")

        if added > 0:
            # Re-prioritize after adding tasks
            try:
                run_prioritization()
            except Exception as e:
                errors.append(f"prioritization failed: {e}")

        if added:
            self._show_after_turn(
                self._append_confirmation, f"Added {added} task(s) from document."
            )
        for err in errors:
            self._show_after_turn(self._append_alert, err)

    def _append_confirmation(self, text: str) -> None:
        tokens = self._get_live_tokens()
        self._append_bubble(
            text,
            align_right=False,
            bg_color=tokens.get("sys_color_surface", "#2a2a40"),
            text_color=tokens.get("sys_color_primary", "#00d9ff"),
        )

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
        self.input_box.setEnabled(not busy)
        self.model_combo.setEnabled(not busy)
        self.refresh_btn.setEnabled(not busy)
        self.settings_btn.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.busy_changed.emit(busy)

    def is_busy(self) -> bool:
        return self._thread is not None

    def _on_send(self) -> None:
        from system_sl.chatbot import load_config
        if self._thread is not None:
            return
        text = self.input_box.toPlainText().strip()
        if not text:
            return

        model = self._current_model or load_config().system_model
        session = self._get_session(model)
        if session is None:
            return

        # Capture attachments before clearing
        attachment_paths = [str(p) for p in self._attachments]

        self.input_box.clear()
        # Show attachments in the player message if any
        if attachment_paths:
            self._append_player_with_attachments(text, attachment_paths)
        else:
            self._append_player(text)
        self._reset_stream_state()
        self._set_busy(True)
        self._start_answer_line()

        # Clear attachments after sending
        self._clear_attachments()

        self._thread = QThread(self)
        self._worker = _ChatWorker(session, text, attachment_paths)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.reasoning.connect(self._on_reasoning)
        self._worker.chunk.connect(self._on_chunk)
        self._worker.finished.connect(self._on_reply)
        self._worker.failed.connect(self._on_failure)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.proposal.connect(self._on_proposal)
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

        self._render_answer()
        self._answer_open = False
        self.reasoning_view.clear()
        self.reasoning_view.setVisible(False)
        self._reasoning_shown = False
        self._stop_dots()

    def _on_failure(self, message: str) -> None:
        self._md_timer.stop()
        self._stop_dots()
        self._render_answer()  # clears the loading dots if no token ever arrived
        self.reasoning_view.clear()
        self.reasoning_view.setVisible(False)
        self._reasoning_shown = False
        self._answer_open = False
        if self._halted:
            return
        self._append_alert(message)

    def _on_cancelled(self) -> None:
        self._md_timer.stop()
        self._stop_dots()
        if not self._halted:
            # Interrupted without a Stop press (e.g. shutdown).
            self._mark_stopped()
        self.reasoning_view.clear()
        self.reasoning_view.setVisible(False)
        self._reasoning_shown = False

    def _cleanup_thread(self) -> None:
        self._md_timer.stop()
        self._stop_dots()
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None
        notes, self._notes_after_turn = self._notes_after_turn, []
        for append, text in notes:
            append(text)
        self.stop_button.setToolTip("Interrupt the reply")
        self._set_busy(False)
        self.input_box.setFocus()
