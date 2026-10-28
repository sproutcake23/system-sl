"""Chat panel widget for System-SL GUI."""

import os
import html
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QMenu,
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
    QSplitter,
    QFrame,
)
from PySide6.QtGui import QColor, QFont

from system_sl.chatbot import (
    ChatSession,
    ChatConfig,
    ChatConfigError,
    load_config,
    save_config,
    validate_config,
    discover_local_models,
    get_preset_models,
    get_preset,
    ProviderPreset,
    list_presets,
    get_model_cache,
    set_model_cache,
    invalidate_model_cache,
    ModelInfo,
    ModelFetcherError,
    fetch_models_for_provider,
)
# --- Dynamic Theming Utilities ---
from system_sl.utils import get_wallpaper_path
from system_sl.utils.theme_gen import generate_dynamic_tokens, DEFAULT_TOKENS


# =============================================================================
# MODEL FETCH WORKER
# =============================================================================

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

class SettingsDialog(QDialog):
    """Configuration dialog for LLM provider and model settings with model fetching."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("LLM Settings")
        self.setModal(True)
        self.resize(700, 550)

        self._config = load_config()
        self._fetched_models: list[ModelInfo] = []
        self._fetch_thread: QThread | None = None
        self._fetch_worker: _ModelFetchWorker | None = None

        self._build_ui()
        self._load_current_config()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # --- Provider & API Key Section ---
        form = QFormLayout()

        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Custom", "custom")
        for preset in list_presets():
            self.preset_combo.addItem(preset.name, preset.name.lower())
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

        self.fetch_btn = QPushButton("Fetch Models")
        self.fetch_btn.setToolTip("Fetch available models from provider API")
        self.fetch_btn.clicked.connect(self._fetch_models)
        key_row.addWidget(self.fetch_btn)

        form.addRow("API Key:", key_row)

        # Model selection (manual entry, used if no model selected from table)
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("e.g., llama3.1, gemini-1.5-flash (or select from table below)")
        form.addRow("Model:", self.model_edit)

        # Test Connection Button
        self.test_btn = QPushButton("Test Connection")
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

        self.select_free_btn = QPushButton("Select All Free")
        self.select_free_btn.clicked.connect(self._select_all_free)
        self.select_free_btn.setEnabled(False)
        search_row.addWidget(self.select_free_btn)

        self.refresh_cache_btn = QPushButton("Refresh Cache")
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

        # Progress bar (hidden by default)
        self.fetch_progress = QProgressBar()
        self.fetch_progress.setRange(0, 0)  # Indeterminate
        self.fetch_progress.setVisible(False)
        table_layout.addWidget(self.fetch_progress)

        layout.addWidget(self.model_table_frame, stretch=1)
        self.model_table_frame.setVisible(False)  # Hidden until fetch

        # --- Buttons ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Apply
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self._on_apply)
        layout.addWidget(buttons)

    def _load_current_config(self) -> None:
        """Populate fields from current config."""
        # Find matching preset
        preset_key = "custom"
        for preset in list_presets():
            if (preset.provider == self._config.provider and
                preset.base_url == self._config.base_url):
                preset_key = preset.name.lower()
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
        preset_key = self.preset_combo.currentData()
        if preset_key == "custom":
            self.base_url_edit.setEnabled(True)
            self.api_key_edit.setEnabled(True)
            self.model_edit.setEnabled(True)
            self.fetch_btn.setEnabled(True)
            return

        preset = None
        for p in list_presets():
            if p.name.lower() == preset_key:
                preset = p
                break

        if preset:
            self.base_url_edit.setText(preset.base_url or "")
            self.base_url_edit.setEnabled(preset.base_url == "")
            self.api_key_edit.setText(preset.api_key or "")
            self.api_key_edit.setEnabled(preset.api_key == "")
            self.model_edit.setText(preset.default_model or "")
            self.model_edit.setEnabled(True)
            self.fetch_btn.setEnabled(preset.api_key == "" or preset.provider == "openrouter")

    def _get_current_preset(self) -> Optional[ProviderPreset]:
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
        self._fetch_worker.progress.connect(lambda msg: self.fetch_btn.setText(msg))
        self._fetch_worker.finished.connect(self._fetch_thread.quit)
        self._fetch_worker.failed.connect(self._fetch_thread.quit)
        self._fetch_thread.finished.connect(self._cleanup_fetch_thread)
        self._fetch_thread.start()

    def _on_models_fetched(self, models: list[ModelInfo]) -> None:
        """Handle successful model fetch."""
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
                name_item.setForeground(QColor("#4ade80"))  # Green for free
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
                price_item.setForeground(QColor("#4ade80"))
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
        preset = self._get_current_preset()
        if preset:
            preset_key = preset.name.lower()
            if "openrouter" in preset_key:
                return "openrouter"
            elif "ollama" in preset_key:
                return "ollama"
            elif "lm_studio" in preset_key or "lm studio" in preset_key:
                return "lm_studio"
        # Custom: infer from base_url
        base_url = self.base_url_edit.text().strip().lower()
        if "openrouter.ai" in base_url:
            return "openrouter"
        elif "localhost:11434" in base_url or "127.0.0.1:11434" in base_url:
            return "ollama"
        elif "localhost:1234" in base_url or "127.0.0.1:1234" in base_url:
            return "lm_studio"
        return "google"

    def _collect_config(self) -> ChatConfig:
        return ChatConfig(
            provider=self._get_provider_from_ui(),
            base_url=self.base_url_edit.text().strip() or None,
            api_key=self.api_key_edit.text().strip(),
            system_model=self.model_edit.text().strip(),
        )

    def _test_connection(self) -> None:
        """Test the current configuration by creating an LLM and sending a ping."""
        from system_sl.chatbot.llm_factory import create_llm, LLMFactoryError

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

    def _on_apply(self) -> None:
        config = self._collect_config()
        errors = validate_config(config)
        if errors:
            QMessageBox.warning(self, "Invalid Config", "\n".join(errors))
            return
        save_config(config)
        self._config = config

    def _on_accept(self) -> None:
        self._on_apply()
        self.accept()


# =============================================================================
# CHAT WORKER
# =============================================================================

class _ChatWorker(QObject):
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, session: ChatSession, user_msg: str):
        super().__init__()
        self.session = session
        self.user_msg = user_msg

    def run(self):
        try:
            reply = self.session.run_turn(self.user_msg)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")
            return
        self.finished.emit(reply)


# =============================================================================
# CHAT PANEL
# =============================================================================

class ChatPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self._sessions: dict[str, ChatSession] = {}  # model_name -> ChatSession
        self._current_model: str = ""
        self._thread: QThread | None = None
        self._worker: _ChatWorker | None = None

        root = QVBoxLayout(self)

        # --- Header with Model Selector ---
        header = QHBoxLayout()
        header.addWidget(QLabel("Model:"))

        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(250)
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        header.addWidget(self.model_combo, stretch=1)

        # Refresh button
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setFixedWidth(32)
        self.refresh_btn.setToolTip("Refresh model list")
        self.refresh_btn.clicked.connect(self._refresh_models)
        header.addWidget(self.refresh_btn)

        # Settings button
        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setFixedWidth(32)
        self.settings_btn.setToolTip("LLM Settings")
        self.settings_btn.clicked.connect(self._open_settings)
        header.addWidget(self.settings_btn)

        root.addLayout(header)

        # --- Transcript ---
        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText("[ awaiting Player input — channel: SYSTEM ]")
        root.addWidget(self.transcript, stretch=1)

        # --- Input Row ---
        input_row = QHBoxLayout()
        self.input_box = QLineEdit()
        self.input_box.setPlaceholderText("Speak, Player...")
        self.input_box.returnPressed.connect(self._on_send)
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._on_send)
        input_row.addWidget(self.input_box, stretch=1)
        input_row.addWidget(self.send_button)
        root.addLayout(input_row)

        # Initialize model list
        self._populate_model_list()

    # ----- Model Management -----

    def _populate_model_list(self) -> None:
        """Populate the model dropdown with configured + discovered + cached models."""
        self.model_combo.blockSignals(True)
        self.model_combo.clear()

        config = load_config()
        current_model = config.system_model

        # 1. Current configured model (first, bold)
        self.model_combo.addItem(f"✦ {current_model}", current_model)

        # 2. Separator
        self.model_combo.insertSeparator(1)

        # 3. Discovered local models
        local_models = discover_local_models()
        for display, model_id in local_models:
            self.model_combo.addItem(display, model_id)

        # 4. Cached models from OpenRouter (if any)
        if config.provider == "openrouter":
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
        idx = self.model_combo.findData(current_model)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
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

        # Update config with new model
        config = load_config()
        config.system_model = model_id
        save_config(config)

        # Reload agent with new model
        from system_sl.chatbot import reload_agent
        reload_agent()

        # Clear session cache for new model
        self._sessions.clear()
        self._current_model = model_id

        self._append_alert(f"Switched to model: {model_id}")

    def _open_settings(self) -> None:
        """Open the settings dialog."""
        dialog = SettingsDialog(self)
        if dialog.exec() == QDialog.Accepted:
            # Config was saved, reload agent and refresh models
            from system_sl.chatbot import reload_agent
            reload_agent()
            self._populate_model_list()

    # ----- Session Management -----

    def _get_session(self, model_name: str) -> ChatSession | None:
        """Get or create a ChatSession for the given model."""
        if model_name not in self._sessions:
            try:
                self._sessions[model_name] = ChatSession(model_name=model_name)
            except ChatConfigError as e:
                self._append_alert(str(e))
                return None
            except Exception as e:
                self._append_alert(f"Could not open channel: {e}")
                return None
        return self._sessions[model_name]

    # ----- Transcript Rendering -----

    def _get_live_tokens(self) -> dict:
        """Fetches the live dynamic theme colors for the chat HTML."""
        try:
            path = get_wallpaper_path()
            if path and "Error" not in path:
                return generate_dynamic_tokens(os.path.expanduser(path))
        except Exception:
            pass
        return DEFAULT_TOKENS

    def _append_html(self, html_line: str) -> None:
        self.transcript.append(html_line)

    def _format_line(self, label: str, label_color: str, body_color: str, text: str) -> str:
        safe = html.escape(text).replace("\n", "<br>")
        return (
            f'<span style="color:{label_color}; font-weight:bold">[{label}]</span> '
            f'<span style="color:{body_color}">{safe}</span>'
        )

    def _append_player(self, text: str) -> None:
        tokens = self._get_live_tokens()
        label_color = tokens.get("sys_color_text_secondary", "#9ab8e5")
        body_color = tokens.get("sys_color_text_main", "#d6e7ff")
        self._append_html(self._format_line("PLAYER", label_color, body_color, text))

    def _append_bot(self, text: str) -> None:
        tokens = self._get_live_tokens()
        label_color = tokens.get("sys_color_primary", "#00d9ff")
        body_color = tokens.get("sys_color_text_main", "#d6e7ff")
        self._append_html(self._format_line("SYSTEM", label_color, body_color, text))

    def _append_alert(self, text: str) -> None:
        safe = html.escape(text).replace("\n", "<br>")
        alert_color = "#ff6b8a"
        self._append_html(
            f'<span style="color:{alert_color}; font-weight:bold">[ALERT]</span> '
            f'<span style="color:{alert_color}">{safe}</span>'
        )

    # ----- Input Flow -----

    def _set_busy(self, busy: bool) -> None:
        self.send_button.setEnabled(not busy)
        self.input_box.setEnabled(not busy)
        self.model_combo.setEnabled(not busy)
        self.refresh_btn.setEnabled(not busy)
        self.settings_btn.setEnabled(not busy)

    def _on_send(self) -> None:
        if self._thread is not None:
            return
        text = self.input_box.text().strip()
        if not text:
            return

        model = self._current_model or load_config().system_model
        session = self._get_session(model)
        if session is None:
            return

        self.input_box.clear()
        self._append_player(text)
        self._set_busy(True)

        self._thread = QThread(self)
        self._worker = _ChatWorker(session, text)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_reply)
        self._worker.failed.connect(self._on_failure)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()

    def _on_reply(self, reply: str) -> None:
        self._append_bot(reply or "[empty reply]")

    def _on_failure(self, message: str) -> None:
        self._append_alert(message)

    def _cleanup_thread(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None
        self._set_busy(False)
        self.input_box.setFocus()