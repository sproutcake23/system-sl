import logging
import json
import os
import sys
import threading
from pathlib import Path


from system_sl.frontend.gui.desktop_entry import install_desktop_entry
from PySide6.QtCore import QSize, QTimer, QSettings, Qt, QObject, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QPushButton,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QMessageBox,
    QFileDialog,
    QSizePolicy,
    QSystemTrayIcon,
    QMenu,
)
from system_sl.frontend.gui.popup_windows import TasksWindow, OnboardingWindow
from system_sl.frontend.gui.data_issue_reporter import DataIssueMonitor
from system_sl.frontend.gui.settings_window import AppSettingsDialog
from system_sl.frontend.gui.theme import THEME_MODE_LABELS, THEME_MODES
from system_sl.frontend.gui.theme_manager import get_theme_manager
from system_sl.frontend.gui.icons import get_icon, initialize_icons
from system_sl import __version__
from system_sl.utils import (
    CrossPlatformAutostart,
    SystemNotification,
    GoogleSyncError,
    get_tasks_file_path,
    secure_storage,
)
from system_sl.core import GoogleSyncEngine, TasksProvider
from system_sl.core.onboarding import PersonaStorageHandler
from system_sl.services import BackgroundServiceController
from system_sl.services.aw_daemon import (
    disable_activitywatch,
    enable_activitywatch,
    is_aw_user_enabled,
)
from system_sl.utils.audio_manager import (
    play_sound,
    set_sound_setting,
    default_sounds_dir,
)
from system_sl.utils.autostart_migration import initialize_application_autostart
from system_sl.utils.worker_threads import new_worker_thread, shutdown_workers
from system_sl.utils.notification_settings import (
    load_notifications_enabled,
    save_notifications_enabled,
)
from system_sl.utils.i18n import tr
from system_sl.utils.auto_updater import (
    UpdateCheckThread,
    apply_pending_update,
    load_auto_update_preference,
    save_auto_update_preference,
)

logger = logging.getLogger(__name__)


class _GoogleSyncWorker(QObject):
    """Runs the Google Tasks sync off the UI thread, since sign-in can wait on the browser."""

    finished = Signal(int, int)
    failed = Signal(str)

    def __init__(self, cancel_event: threading.Event):
        super().__init__()
        self.cancel_event = cancel_event

    def run(self):
        from system_sl.utils.google_api import GoogleSyncCancelled

        try:
            engine = GoogleSyncEngine(cancel_event=self.cancel_event)
            imported, skipped = engine.execute_sync(TasksProvider(engine.client))
            self.finished.emit(imported, skipped)
        except GoogleSyncCancelled:
            return
        except GoogleSyncError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


SIDEBAR_BUTTON_SIZE = 34
SIDEBAR_RAIL_RIGHT_MARGIN = 4
SIDEBAR_VERTICAL_MARGIN = 9
SIDEBAR_SPACING = 10
SETTINGS_SECTION_GAP = 12
UNCONSTRAINED_WIDTH = 16777215  # QWIDGETSIZE_MAX


class _ClickableLabel(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Main Menu")
        self.setMinimumSize(QSize(700, 400))

        self.settings = QSettings("system-sl", "AppConfig")
        self.theme_manager = get_theme_manager()
        self.theme_manager.start()
        self._theme_status_text = self.theme_manager.status_message

        self.autostart = CrossPlatformAutostart()
        self.notifier_view = None
        self.notifier_controller = None
        self.tasks_window = None
        self.onboarding_window = None
        self.update_thread = None
        self._sync_thread = None
        self._sync_worker = None
        self._sync_cancel = threading.Event()
        self._allow_exit = False
        self.close_to_tray_enabled = self.settings.value(
            "close_to_tray", False, type=bool
        )
        self.sidebar_collapsed = self.settings.value(
            "sidebar_collapsed", False, type=bool
        )
        self.tray_icon = None
        self._setup_tray_icon()
        secure_storage.register_fallback_warning_callback(self._show_fallback_warning)
        self.theme_manager.themeChanged.connect(self._on_theme_changed)
        self.theme_manager.statusChanged.connect(self._on_theme_status)

        main_container = QWidget()
        self.setCentralWidget(main_container)
        layout = QHBoxLayout(main_container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # left box (collapsible sidebar)
        self._sidebar = QWidget()
        self._sidebar_layout = QVBoxLayout(self._sidebar)
        left_layout = self._sidebar_layout
        left_layout.setSpacing(SIDEBAR_SPACING)

        self.sidebar_toggle = QPushButton()
        self.sidebar_toggle.setObjectName("sidebarToggle")
        self.sidebar_toggle.setFixedSize(SIDEBAR_BUTTON_SIZE, SIDEBAR_BUTTON_SIZE)
        self.sidebar_toggle.setCursor(Qt.PointingHandCursor)
        self.sidebar_toggle.clicked.connect(self._toggle_sidebar)
        left_layout.addWidget(self.sidebar_toggle, alignment=Qt.AlignmentFlag.AlignLeft)

        QShortcut(QKeySequence("Ctrl+Shift+S"), self, activated=self._toggle_sidebar)

        self.task_button = QPushButton("Tasks")
        self.task_button.setIcon(get_icon("tasks"))
        self.task_button.clicked.connect(self.open_tasks_window)

        self.google_button = QPushButton("Google")
        self.google_button.setIcon(get_icon("google"))
        self.google_button.clicked.connect(self.sync_google_tasks)

        self.settings_button = QPushButton("Settings")
        self.settings_button.setIcon(get_icon("settings"))
        self.settings_button.clicked.connect(lambda: self.open_settings())

        self._sidebar_labels = {}
        for btn in (self.task_button, self.google_button, self.settings_button):
            btn.setFixedHeight(SIDEBAR_BUTTON_SIZE)
            btn.setCursor(Qt.PointingHandCursor)
            self._sidebar_labels[btn] = btn.text()
            btn.setAccessibleName(btn.text())

        left_layout.addWidget(self.task_button)
        left_layout.addWidget(self.google_button)

        self.google_button.setVisible(
            self.settings.value("google_sync_enabled", False, type=bool)
        )

        # Profile hint
        self.profile_hint = _ClickableLabel(
            "No player profile — tasks are ranked by urgency only. "
            "<u>Set up a profile</u>"
        )
        self.profile_hint.setTextFormat(Qt.TextFormat.RichText)
        self.profile_hint.setCursor(Qt.PointingHandCursor)
        self.profile_hint.setWordWrap(True)
        self.profile_hint.setObjectName("profileHint")
        self.profile_hint.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        self.profile_hint.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self.profile_hint.clicked.connect(self.open_onboarding)
        left_layout.addWidget(self.profile_hint)
        self._profile_missing = not PersonaStorageHandler().has_profile()

        left_layout.addStretch(1)
        left_layout.addWidget(self.settings_button)

        # right box — chatbot panel
        from system_sl.frontend.gui.chat_panel import ChatPanel
        self.chat_panel = ChatPanel(self)
        QApplication.instance().aboutToQuit.connect(self.chat_panel.shutdown)
        self.chat_panel.settings_requested.connect(
            lambda: self.open_settings(AppSettingsDialog.LLM_TAB)
        )

        layout.addWidget(self._sidebar, stretch=1)
        layout.addWidget(self.chat_panel, stretch=3)
        self._main_layout = layout

        self._general_settings_page = self._build_general_settings_page()
        self._settings_window = None

        self._apply_sidebar_state()

        if self.notifications_check.isChecked():
            self._start_notifier(notify_now=False)

        self._data_issue_monitor = DataIssueMonitor(self)

        QApplication.instance().aboutToQuit.connect(self._shutdown_for_quit)

    def _build_general_settings_page(self) -> QWidget:
        """Build the Settings window's General tab."""
        self.notifications_check = QCheckBox("Notifications")
        self.notifications_check.setChecked(load_notifications_enabled())
        self.notifications_check.setToolTip(
            "Show the hourly task reminder while THE SYSTEM is running.\n"
            "Turn this off to stop reminders without changing login autostart."
        )
        self.notifications_check.toggled.connect(self._toggle_notifications)

        self.check_box = QCheckBox("Notification AutoStart")
        self.check_box.setChecked(self.autostart.is_enabled())
        self.check_box.setToolTip(
            "Launch the hourly notifier at login.\n"
            "This is separate from the Notifications option: it does not turn "
            "reminders on or off while THE SYSTEM is running."
        )
        self.check_box.clicked.connect(self.notification_autostart)

        self.sound_button = QPushButton("Notification Sound")
        self.sound_button.setIcon(get_icon("sound"))
        self.sound_button.clicked.connect(self.change_notification_sound)

        self.activitywatch_check = QCheckBox("ActivityWatch Tracking")
        self.activitywatch_check.setChecked(is_aw_user_enabled())
        self.activitywatch_check.setToolTip(
            "Track active window and AFK time locally in the background.\n"
            "Continues running even when THE SYSTEM window is closed, and starts "
            "at login until you explicitly turn this off."
        )
        self.activitywatch_check.toggled.connect(self._toggle_activitywatch)

        # Theme picker: System, Light, Dark or Dynamic.
        self.theme_combo = QComboBox()
        self.theme_combo.setMinimumWidth(170)
        self.theme_combo.setCursor(Qt.PointingHandCursor)
        for mode in THEME_MODES:
            self.theme_combo.addItem(THEME_MODE_LABELS[mode], mode)
        self.theme_combo.setToolTip(
            "System follows your operating system's light/dark setting.\n"
            "Dark is a darker, near-black palette.\n"
            "Dynamic derives the palette from your wallpaper with wallust."
        )
        current_mode = self.theme_manager.mode
        index = self.theme_combo.findData(current_mode)
        if index >= 0:
            self.theme_combo.setCurrentIndex(index)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_combo_changed)

        self.theme_status = QLabel("")
        self.theme_status.setObjectName("themeStatus")
        self.theme_status.setWordWrap(True)
        self.theme_status.setText(self._theme_status_text)
        self.theme_status.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.theme_status.setVisible(bool(self._theme_status_text))

        from system_sl.frontend.gui.theme_preview import ThemePreview

        self.theme_preview = ThemePreview(
            self.theme_manager, button_height=SIDEBAR_BUTTON_SIZE
        )

        self.profile_button = QPushButton("Edit Player Profile")
        self.profile_button.setIcon(get_icon("profile"))
        self.profile_button.clicked.connect(self._edit_profile_from_settings)

        self.close_to_tray_toggle = QCheckBox("Close to System Tray")
        self.close_to_tray_toggle.setChecked(self.close_to_tray_enabled)
        self.close_to_tray_toggle.setEnabled(self.tray_icon is not None)
        if self.tray_icon is None:
            self.close_to_tray_toggle.setToolTip(
                "System tray is not available in this desktop session."
            )
        self.close_to_tray_toggle.toggled.connect(self._toggle_close_to_tray)

        self.google_sync_toggle = QCheckBox("Google Tasks Sync")
        self.google_sync_toggle.setChecked(
            self.settings.value("google_sync_enabled", False, type=bool)
        )
        self.google_sync_toggle.setToolTip(
            "Show the Google button to import Google Tasks as quests.\n"
            "Sign-in currently works only for Google accounts added as "
            "test users on the app's Google OAuth client."
        )
        self.google_sync_toggle.toggled.connect(self._toggle_google_sync)

        self.auto_update_check = QCheckBox("Auto Update")
        self.auto_update_check.setChecked(load_auto_update_preference())
        self.auto_update_check.clicked.connect(self._toggle_auto_update)

        self.install_desktop_btn = QPushButton("Install App")
        self.install_desktop_btn.setIcon(get_icon("install"))
        self.install_desktop_btn.clicked.connect(self.install_desktop_shortcut)

        self.about_button = QPushButton("About")
        self.about_button.setIcon(get_icon("info"))
        self.about_button.clicked.connect(self.show_about_dialog)

        self.reset_settings_btn = QPushButton(tr("Reset Settings"))
        self.reset_settings_btn.setIcon(get_icon("refresh"))
        self.reset_settings_btn.clicked.connect(self._reset_all_settings)

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setSpacing(SIDEBAR_SPACING)

        sections = (
            ("Notifications", (self.notifications_check, self.check_box, self.sound_button)),
            ("ActivityWatch", (self.activitywatch_check,)),
            ("Appearance", (self.theme_combo, self.theme_preview, self.theme_status)),
            ("Player", (self.profile_button,)),
            (
                "App",
                (
                    self.close_to_tray_toggle,
                    self.auto_update_check,
                    self.google_sync_toggle,
                    self.install_desktop_btn,
                    self.about_button,
                    self.reset_settings_btn,
                ),
            ),
        )
        for index, (title, widgets) in enumerate(sections):
            if index:
                page_layout.addSpacing(SETTINGS_SECTION_GAP)
            heading = QLabel(title)
            heading.setObjectName("settingsHeading")
            page_layout.addWidget(heading)
            for widget in widgets:
                if isinstance(widget, QPushButton):
                    widget.setFixedHeight(SIDEBAR_BUTTON_SIZE)
                    widget.setCursor(Qt.PointingHandCursor)
                page_layout.addWidget(widget, alignment=Qt.AlignmentFlag.AlignLeft)
        page_layout.addStretch(1)

        self.install_desktop_btn.setVisible(bool(os.environ.get("APPIMAGE")))

        return page

    def _reset_all_settings(self) -> None:
        """Clear QSettings, JSON settings and stored secrets, after asking."""
        answer = QMessageBox.question(
            self,
            tr("Reset all settings?"),
            tr(
                "This clears the app's settings, chat configuration and stored "
                "API keys, and forgets the notification sound. Tasks and chat "
                "history are not deleted. Continue?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.settings.clear()

        settings_path = Path(get_tasks_file_path("settings.json"))
        try:
            staged = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            staged = {}
        staged = {
            k: v for k, v in (staged if isinstance(staged, dict) else {}).items()
            if k.startswith("pending_appimage_")
        }
        for name in ("llm_config.json", "settings.json"):
            try:
                Path(get_tasks_file_path(name)).unlink(missing_ok=True)
            except OSError:
                logger.warning("could not remove %s", name, exc_info=True)
        if staged:
            try:
                settings_path.write_text(json.dumps(staged, indent=2), encoding="utf-8")
            except OSError:
                logger.warning("could not keep the staged update", exc_info=True)

        for secret in (
            secure_storage.SECRET_LLM_API_KEY,
            secure_storage.SECRET_GOOGLE_TOKEN,
        ):
            try:
                secure_storage.delete_secret(secret)
            except Exception:
                logger.warning("could not clear %s", secret, exc_info=True)

        self.notifications_check.setChecked(load_notifications_enabled())
        self.activitywatch_check.setChecked(is_aw_user_enabled())
        self.auto_update_check.setChecked(load_auto_update_preference())
        self.google_sync_toggle.setChecked(False)
        self.close_to_tray_enabled = False
        self.close_to_tray_toggle.setChecked(False)
        QMessageBox.information(
            self,
            tr("Settings reset"),
            tr(
                "Settings are back to their defaults. Restart the app to apply "
                "the theme and chat provider changes."
            ),
        )

    def open_settings(self, tab: int = AppSettingsDialog.GENERAL_TAB) -> None:
        if self._settings_window is None:
            self._settings_window = AppSettingsDialog(self._general_settings_page, self)
            self._settings_window.llm_saved.connect(self.chat_panel.reload_llm_settings)
            self.chat_panel.busy_changed.connect(
                lambda busy: self._settings_window.set_llm_available(not busy)
            )
        self._settings_window.set_llm_available(not self.chat_panel.is_busy())
        self._settings_window.show_tab(tab)

    def _edit_profile_from_settings(self) -> None:
        self._settings_window.accept()
        self.open_onboarding()

    def _toggle_sidebar(self) -> None:
        self.sidebar_collapsed = not self.sidebar_collapsed
        self.settings.setValue("sidebar_collapsed", self.sidebar_collapsed)
        self.settings.sync()
        self._apply_sidebar_state()

    def _apply_sidebar_state(self) -> None:
        """Show the full sidebar, or shrink it to a rail of icon buttons."""
        collapsed = self.sidebar_collapsed
        self._update_profile_hint()

        for button, label in self._sidebar_labels.items():
            self._set_sidebar_button_label(button, label)
            button.setStyleSheet("padding: 0px;" if collapsed else "")

        if collapsed:
            left_margin = (
                SIDEBAR_RAIL_RIGHT_MARGIN
                + self.chat_panel.layout().contentsMargins().left()
            )
            self._sidebar_layout.setContentsMargins(
                left_margin,
                SIDEBAR_VERTICAL_MARGIN,
                SIDEBAR_RAIL_RIGHT_MARGIN,
                SIDEBAR_VERTICAL_MARGIN,
            )
            self._sidebar.setFixedWidth(
                left_margin + SIDEBAR_BUTTON_SIZE + SIDEBAR_RAIL_RIGHT_MARGIN
            )
            self._main_layout.setStretch(0, 0)
            self._main_layout.setStretch(1, 1)
        else:
            self._sidebar_layout.setContentsMargins(
                16, SIDEBAR_VERTICAL_MARGIN, 12, SIDEBAR_VERTICAL_MARGIN
            )
            self._sidebar.setMinimumWidth(0)
            self._sidebar.setMaximumWidth(UNCONSTRAINED_WIDTH)
            self._main_layout.setStretch(0, 1)
            self._main_layout.setStretch(1, 3)

        action = "Expand" if collapsed else "Collapse"
        self.sidebar_toggle.setIcon(
            get_icon("sidebar_expand" if collapsed else "sidebar_collapse")
        )
        self.sidebar_toggle.setToolTip(f"{action} sidebar (Ctrl+Shift+S)")
        self.sidebar_toggle.setAccessibleName(f"{action} sidebar")

    def _update_profile_hint(self) -> None:
        self.profile_hint.setVisible(self._profile_missing and not self.sidebar_collapsed)

    def _set_sidebar_button_label(self, button: QPushButton, label: str) -> None:
        """Record a button's label; the collapsed rail shows it as a tooltip."""
        self._sidebar_labels[button] = label
        button.setText("" if self.sidebar_collapsed else label)
        button.setToolTip(label if self.sidebar_collapsed else "")

    def _tray_pixmap(self, tokens: dict) -> QPixmap:
        """Draw the tray mark with Qt so frozen builds need no SVG plugin."""
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        background = QColor(tokens.get("sys_color_surface_raised", "#0e1626"))
        accent = QColor(tokens.get("sys_color_primary", "#00d9ff"))
        painter.setBrush(background)
        painter.setPen(accent)
        painter.drawEllipse(3, 3, 58, 58)
        painter.setPen(accent)
        painter.setFont(QFont("Arial", 19, QFont.Weight.Bold))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "SL")
        painter.end()
        return pixmap

    def _on_theme_changed(self, tokens: dict) -> None:
        """Recolour the tray and any open wizard when the palette changes."""
        if self.tray_icon is not None:
            self.tray_icon.setIcon(QIcon(self._tray_pixmap(tokens)))
        chat = getattr(self, "chat_panel", None)
        if chat is not None:
            chat.refresh_theme(tokens)
        if self.onboarding_window is not None:
            self.onboarding_window.refresh_theme(tokens)

    def _on_theme_status(self, message: str) -> None:
        self._theme_status_text = message
        status = getattr(self, "theme_status", None)
        if status is not None:
            status.setText(message)
            status.setVisible(bool(message))

    def _setup_tray_icon(self):
        """Create the tray menu used to restore or fully exit the app."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        self.tray_icon = QSystemTrayIcon(self._tray_pixmap(self.theme_manager.tokens), self)
        self.tray_icon.setToolTip("THE SYSTEM")
        menu = QMenu()
        show_action = QAction("Open THE SYSTEM", self)
        show_action.triggered.connect(self._restore_from_tray)
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self._exit_from_tray)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _toggle_close_to_tray(self, enabled):
        self.close_to_tray_enabled = enabled
        self.settings.setValue("close_to_tray", enabled)
        self.settings.sync()

    def _toggle_google_sync(self, enabled):
        self.settings.setValue("google_sync_enabled", enabled)
        self.google_button.setVisible(enabled)

    def _restore_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _exit_from_tray(self):
        self._allow_exit = True
        self.tray_icon.hide()
        QApplication.instance().quit()

    def _on_tray_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._restore_from_tray()

    def _show_fallback_warning(self) -> None:
        """Show a tray notification when secure-storage fallback is activated."""
        if self.tray_icon is not None:
            self.tray_icon.showMessage(
                "THE SYSTEM — Security Notice",
                "Secrets stored in fallback encryption (machine-id based). "
                "Install gnome-keyring/kwallet for real security.",
                QSystemTrayIcon.MessageIcon.Warning,
                10000,
            )

    def closeEvent(self, event):
        if (
            not self._allow_exit
            and self.tray_icon is not None
            and self.close_to_tray_enabled
        ):
            self.hide()
            event.ignore()
            return
        event.accept()
        if not self._allow_exit:
            QTimer.singleShot(0, QApplication.instance().quit)

    def _shutdown_for_quit(self) -> None:
        """Stop UI background work; ActivityWatch (--aw-bg) stays alive independently."""
        self._data_issue_monitor.stop()
        if self.tasks_window is not None:
            self.tasks_window.shutdown()
            self.tasks_window.hide()
        self.hide()
        self._stop_notifier()
        self._sync_cancel.set()
        shutdown_workers()
        from system_sl.chatbot.memory import close_checkpointer

        if self.chat_panel.worker_stopped():
            close_checkpointer()

    def open_tasks_window(self):
        if self.tasks_window is None:
            self.tasks_window = TasksWindow()
        self.tasks_window.show()

    def open_onboarding(self):
        """Let a player create or replace their prioritization profile."""
        if self.onboarding_window is not None:
            self.onboarding_window.raise_()
            self.onboarding_window.activateWindow()
            return

        self.onboarding_window = OnboardingWindow()
        self.onboarding_window.onboarding_complete.connect(
            self._on_onboarding_closed
        )
        self.onboarding_window.show()

    def _on_onboarding_closed(self, completed):
        if self.onboarding_window is not None:
            self.onboarding_window.deleteLater()
            self.onboarding_window = None
        if completed:
            self._profile_missing = False
            self._update_profile_hint()

    def notification_autostart(self):
        self.autostart.toggle()
        message_box = QMessageBox()
        message_box.setWindowTitle("Auto Start")
        if self.check_box.isChecked():
            message_box.setText("Enabled AutoStart")
        else:
            message_box.setText("Disabled AutoStart")
        message_box.exec()

    def _start_notifier(self, notify_now: bool = True):
        """Run the hourly task reminder inside this process."""
        if self.notifier_controller is not None:
            return
        self.notifier_view = SystemNotification()
        self.notifier_controller = BackgroundServiceController(self.notifier_view)
        if notify_now:
            self.notifier_controller.poll_and_render_task()

    def _stop_notifier(self):
        """Stop reminding and tear down the HUD window."""
        if self.notifier_controller is not None:
            self.notifier_controller.stop()
            self.notifier_controller.deleteLater()
            self.notifier_controller = None
        if self.notifier_view is not None:
            self.notifier_view.hide()
            self.notifier_view.deleteLater()
            self.notifier_view = None

    def _toggle_notifications(self, enabled):
        save_notifications_enabled(enabled)
        if enabled:
            self._start_notifier(notify_now=True)
        else:
            self._stop_notifier()

    def _toggle_activitywatch(self, enabled: bool) -> None:
        if enabled:
            ext_status = enable_activitywatch()
            if ext_status == "needs_relogin":
                QMessageBox.information(
                    self,
                    "ActivityWatch — GNOME Wayland Setup",
                    "The Focused Window D-Bus extension has been installed and enabled.\n\n"
                    "Please log out and back in once so GNOME Shell activates window title tracking.",
                )
        else:
            disable_activitywatch()

    def sync_google_tasks(self):
        if self._sync_thread is not None:
            self._sync_cancel.set()
            self.google_button.setEnabled(False)
            self._set_sidebar_button_label(self.google_button, "Cancelling...")
            return
        self._set_sidebar_button_label(self.google_button, "Syncing... (cancel)")

        self._sync_cancel = threading.Event()
        self._sync_worker = _GoogleSyncWorker(self._sync_cancel)
        self._sync_thread = new_worker_thread(self._sync_worker)
        self._sync_worker.finished.connect(self._on_google_sync_finished)
        self._sync_worker.failed.connect(self._on_google_sync_failed)
        self._sync_thread.finished.connect(self._cleanup_google_sync)
        self._sync_thread.start()

    def _on_google_sync_finished(self, imported: int, skipped: int):
        if self._sync_cancel.is_set():
            return
        if imported == 0 and skipped == 0:
            text = "No upcoming Google Tasks found."
        else:
            text = f"Imported {imported} task(s) from Google Tasks."
            if skipped:
                text += f"\n{skipped} already in your quest list were skipped."
        QMessageBox.information(self, "Google Sync", text)

    def _on_google_sync_failed(self, error: str):
        if self._sync_cancel.is_set():
            return
        QMessageBox.warning(self, "Google Sync Failed", error)

    def _cleanup_google_sync(self):
        self.google_button.setEnabled(True)
        self._set_sidebar_button_label(self.google_button, "Google")
        self._sync_worker = None
        self._sync_thread = None

    def change_notification_sound(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Notification Sound",
            str(default_sounds_dir()),
            "Audio Files (*.wav *.mp3)",
        )
        if file_path:
            try:
                set_sound_setting(file_path)
                play_sound(file_path)
                QMessageBox.information(self, "Success", "Notification sound updated!")
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to set sound:\n{str(e)}")

    def install_desktop_shortcut(self):
        """Move the AppImage to ~/Applications and add a menu entry for it."""
        result = install_desktop_entry()
        if result == 0:
            QMessageBox.information(
                self,
                "Success",
                f"THE SYSTEM is installed at:\n{os.environ['APPIMAGE']}\n\n"
                "It now appears in your applications menu.",
            )
        else:
            QMessageBox.warning(
                self,
                "Failed",
                "Could not install the app.\nMake sure you're running from the AppImage.",
            )

    def show_about_dialog(self):
        """Show the About dialog with version information."""
        QMessageBox.about(
            self,
            "About THE SYSTEM",
            f"<b>THE SYSTEM</b><br>"
            f"Version: {__version__}<br>"
            f"<br>"
            f"A Solo Leveling-inspired task management system.<br>"
            f"<br>"
            f"© 2024–2026 sproutcake23<br>"
            f"Licensed under GPL-3.0",
        )

    def _on_theme_combo_changed(self, index: int) -> None:
        if index < 0:
            return
        mode = self.theme_combo.itemData(index)
        if isinstance(mode, str) and mode != self.theme_manager.mode:
            self.theme_manager.set_mode(mode, persist=True)

    def _toggle_auto_update(self):
        enabled = self.auto_update_check.isChecked()
        save_auto_update_preference(enabled)

    def _check_for_updates(self):
        if not load_auto_update_preference():
            return
        self.update_thread = UpdateCheckThread()
        self.update_thread.update_complete.connect(self._on_update_complete)
        self.update_thread.start()

    def _on_update_complete(self, success, message):
        if success:
            QMessageBox.information(self, "Auto Update", message)
        else:
            if "Updated to" not in message:
                QMessageBox.warning(self, "Auto Update", message)


def _finish_check(flag, ok, details):
    report = sys.argv[sys.argv.index(flag) + 1:]
    if report:
        with open(report[0], "w", encoding="utf-8") as report_file:
            report_file.write(details + "\n")
    print(details)
    sys.stdout and sys.stdout.flush()
    os._exit(0 if ok else 1)


def _run_ocr_check():
    from system_sl.chatbot.documents import check_ocr

    ok, details = check_ocr()
    _finish_check("--check-ocr", ok, details)


def _run_theme_check():
    """Verify the frozen build can run wallust and map its palette to tokens."""
    import tempfile

    from PIL import Image

    from system_sl.utils.theme_gen import DEFAULT_TOKENS
    from system_sl.utils.wallust_theme import find_wallust, generate_wallust_tokens

    problems = []
    binary = find_wallust()
    if not binary:
        problems.append("wallust is not bundled")
    else:
        try:
            with tempfile.TemporaryDirectory() as directory:
                image_path = os.path.join(directory, "check.png")
                image = Image.new("RGB", (48, 48))
                pixels = image.load()
                for y in range(48):
                    for x in range(48):
                        pixels[x, y] = (int(255 * x / 47), int(255 * y / 47), 128)
                image.save(image_path)

                tokens = generate_wallust_tokens(image_path, use_cache=False)
                missing = [key for key in DEFAULT_TOKENS if not tokens.get(key)]
                if missing:
                    problems.append("missing tokens: " + ", ".join(sorted(missing)))
                if tokens == DEFAULT_TOKENS:
                    problems.append("wallust fell back to the built-in palette")
        except Exception as exc:
            problems.append(f"{type(exc).__name__}: {exc}")

    details = "Theme check failed: " + "; ".join(problems) if problems else "Theme check passed"
    if binary:
        details += f" ({binary})"
    _finish_check("--check-theme", not problems, details)


def _run_gui_check():
    """Exercise what trimming the bundle could break, and exit with the result."""
    from googleapiclient.discovery_cache import get_static_doc
    from PySide6.QtCore import QElapsedTimer, QUrl
    from PySide6.QtMultimedia import QMediaPlayer
    from system_sl.utils.audio_manager import list_available_sounds

    problems = []
    try:
        app = QApplication(sys.argv)
        initialize_icons()
        get_theme_manager(app).start()
        window = MainWindow()
        window.show()
        window.open_tasks_window()
        window.tasks_window.close()
        app.processEvents()
        if get_icon("tasks").pixmap(16, 16).isNull():
            problems.append("icons do not render")

        sounds = list_available_sounds()
        if not sounds:
            problems.append("no bundled sounds")
        else:
            player = QMediaPlayer()
            player.setSource(QUrl.fromLocalFile(sounds[0]))
            waited = QElapsedTimer()
            waited.start()
            finished = (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.InvalidMedia)
            while player.mediaStatus() not in finished and waited.elapsed() < 20000:
                app.processEvents()
            if player.mediaStatus() != QMediaPlayer.MediaStatus.LoadedMedia:
                problems.append(
                    f"could not load {os.path.basename(sounds[0])}: "
                    f"{player.mediaStatus().name} {player.errorString()!r}"
                )

        if not get_static_doc("tasks", "v1"):
            problems.append("the Google Tasks API description is missing")
    except Exception as e:
        problems.append(f"{type(e).__name__}: {e}")

    details = "GUI check failed: " + "; ".join(problems) if problems else "GUI check passed"
    _finish_check("--check-gui", not problems, details)


def main():
    from system_sl.utils.logging_setup import configure_logging

    configure_logging()

    if "--aw-bg" in sys.argv:
        from system_sl.frontend.gui.aw_bg_main import main as aw_bg_main
        return aw_bg_main()

    from system_sl.utils.audio_manager import initialize_audio

    initialize_audio()
    if "--check-ocr" in sys.argv:
        _run_ocr_check()
    if "--check-gui" in sys.argv:
        _run_gui_check()
    if "--check-theme" in sys.argv:
        _run_theme_check()

    if "--bg" in sys.argv:
        from system_sl.frontend.gui.notifier import main as notifier_main
        return notifier_main()

    # Handle --install-desktop flag before Qt initialization
    if "--install-desktop" in sys.argv:
        return install_desktop_entry()

    # Swap in a staged AppImage replacement before anything else touches disk.
    apply_pending_update()

    app = QApplication(sys.argv)

    initialize_icons()
    get_theme_manager(app).start()
    app.setQuitOnLastWindowClosed(False)
    initialize_application_autostart()

    gnome_ext_status = "not_needed"
    if is_aw_user_enabled():
        gnome_ext_status = enable_activitywatch()

    try:
        storage = PersonaStorageHandler()
        if storage.is_first_time():
            onboarding = OnboardingWindow()
            main_window = None

            def _on_onboarding_done(result):
                nonlocal main_window
                main_window = MainWindow()
                main_window.show()
                main_window._check_for_updates()
                if gnome_ext_status == "needs_relogin":
                    QTimer.singleShot(
                        1500,
                        lambda: QMessageBox.information(
                            main_window,
                            "GNOME Wayland Window Tracking",
                            "ActivityWatch installed the Focused Window D-Bus GNOME extension.\n\n"
                            "Log out and back in once to activate window title tracking.",
                        ),
                    )

            onboarding.onboarding_complete.connect(_on_onboarding_done)
            onboarding.show()
        else:
            main_window = MainWindow()
            main_window.show()
            main_window._check_for_updates()
            if gnome_ext_status == "needs_relogin":
                QTimer.singleShot(
                    1500,
                    lambda: QMessageBox.information(
                        main_window,
                        "GNOME Wayland Window Tracking",
                        "ActivityWatch installed the Focused Window D-Bus GNOME extension.\n\n"
                        "Log out and back in once to activate window title tracking.",
                    ),
                )

    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
