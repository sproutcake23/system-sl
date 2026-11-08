import os
import sys
from pathlib import Path
import subprocess
from typing import Dict, Optional

from dotenv import load_dotenv, set_key


def install_desktop_entry() -> int:
    """Move the AppImage to ~/Applications and add a menu entry for it."""
    appimage_path = os.environ.get("APPIMAGE")
    if not appimage_path or not Path(appimage_path).is_file():
        print("❌ This command only works when running from an AppImage.")
        return 1

    from system_sl.utils.appimage_install import DESKTOP_FILE, install_appimage

    try:
        installed = install_appimage(Path(appimage_path))
    except OSError as e:
        print(f"❌ Failed to install the AppImage: {e}")
        return 1
    print(f"✅ Installed to: {installed}")
    print(f"   Menu entry: {DESKTOP_FILE}")
    return 0
from PySide6.QtCore import QSize, QTimer, QSettings, QFileSystemWatcher, Qt, Slot, QObject, QThread, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QPushButton,
    QCheckBox,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QMessageBox,
    QFileDialog,
    QInputDialog,
    QSizePolicy,
    QDialog,
    QSystemTrayIcon,
    QMenu,
)
from system_sl.frontend.gui.popup_windows import TasksWindow, OnboardingWindow
from system_sl.frontend.gui.chat_panel import ChatPanel
from system_sl.frontend.gui.settings_window import AppSettingsDialog
from system_sl.frontend.gui.theme import (
    apply_theme,
    get_current_theme_tokens,
    get_tokens_for_mode,
)
from system_sl.frontend.gui.icons import get_icon, initialize_icons
from system_sl.utils import (
    CrossPlatformAutostart,
    SystemNotification,
    get_tasks_file_path,
    get_wallpaper_path,
    GoogleSyncError,
)
from system_sl.utils.aether_theme import (
    generate_aether_tokens,
    find_aether_colors_toml,
)
from system_sl.core import GoogleSyncEngine, TasksProvider
from system_sl.core.onboarding import PersonaStorageHandler
from system_sl.services import BackgroundServiceController
from system_sl.utils.audio_manager import (
    play_sound,
    set_sound_setting,
    DEFAULT_SOUNDS_DIR,
)
from system_sl.utils.theme_gen import generate_dynamic_tokens
from system_sl.utils.autostart_migration import initialize_application_autostart
from system_sl.utils.notification_settings import (
    load_notifications_enabled,
    save_notifications_enabled,
)
from system_sl.utils.auto_updater import (
    UpdateCheckThread,
    apply_pending_update,
    load_auto_update_preference,
    save_auto_update_preference,
)


class _GoogleSyncWorker(QObject):
    """Runs the Google Tasks sync off the UI thread, since sign-in can wait on the browser."""
    finished = Signal(int, int)
    failed = Signal(str)

    def run(self):
        try:
            engine = GoogleSyncEngine()
            imported, skipped = engine.execute_sync(TasksProvider(engine.client))
            self.finished.emit(imported, skipped)
        except GoogleSyncError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


SIDEBAR_BUTTON_SIZE = 34
SIDEBAR_RAIL_RIGHT_MARGIN = 4
# Top and bottom match the chat panel, lining the toggle and the Settings button
# up with its header and input rows.
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

        # --- NEW: Theme Settings Initialization ---
        self.settings = QSettings("system-sl", "AppConfig")
        self.last_wallpaper_path = None
        self.wallpaper_timer = QTimer(self)
        self.wallpaper_timer.timeout.connect(self.check_wallpaper_update)

        # --- NEW: Aether theme file watcher ---
        # Aether writes the live palette to ~/.local/state/omarchy/current/theme/colors.toml.
        # We watch that file so SL recolors instantly when Aether applies a new theme.
        self.aether_theme_watcher = QFileSystemWatcher()
        self.aether_theme_watcher.fileChanged.connect(
            self.check_aether_theme_update
        )
        self.last_aether_tokens: Optional[Dict[str, str]] = None
        self._aether_theme_path: Optional[str] = None

        self.autostart = CrossPlatformAutostart()
        self.notifier_view = None
        self.notifier_controller = None
        self.tasks_window = None
        self.onboarding_window = None
        self.update_thread = None
        self._sync_thread = None
        self._sync_worker = None
        self._allow_exit = False
        self.close_to_tray_enabled = self.settings.value(
            "close_to_tray", False, type=bool
        )
        self.sidebar_collapsed = self.settings.value(
            "sidebar_collapsed", False, type=bool
        )
        self.tray_icon = None
        self._setup_tray_icon()

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

        # These stay visible in the collapsed rail, reduced to their icons.
        self._sidebar_labels = {}
        for btn in (self.task_button, self.google_button, self.settings_button):
            btn.setFixedHeight(SIDEBAR_BUTTON_SIZE)
            btn.setCursor(Qt.PointingHandCursor)
            self._sidebar_labels[btn] = btn.text()
            btn.setAccessibleName(btn.text())

        left_layout.addWidget(self.task_button)
        left_layout.addWidget(self.google_button)

        # Google sign-in only works for accounts allowed on the OAuth client,
        # so the sync button stays hidden until the player opts in.
        # Set after addWidget: re-parenting resets visibility to hidden.
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
        # A wrapped label in a QVBoxLayout otherwise expands to absorb all
        # spare vertical space and stretches the whole column.
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

        # right box — chatbot panel (system / friend modes)
        self.chat_panel = ChatPanel(self)
        self.chat_panel.settings_requested.connect(
            lambda: self.open_settings(AppSettingsDialog.LLM_TAB)
        )

        layout.addWidget(self._sidebar, stretch=1)
        layout.addWidget(self.chat_panel, stretch=3)
        self._main_layout = layout

        self._general_settings_page = self._build_general_settings_page()
        self._settings_window = None

        self._apply_sidebar_state()

        # Restore the saved theme mode and apply it. Older versions stored a
        # ``dynamic_theming`` boolean instead of ``theme_mode``; carry it over.
        saved_mode = self.settings.value("theme_mode", None, type=str)
        if not saved_mode:
            saved_mode = (
                "dynamic"
                if self.settings.value("dynamic_theming", False, type=bool)
                else "default"
            )
            self.settings.setValue("theme_mode", saved_mode)
        self._current_theme_mode = saved_mode
        self._apply_theme_mode(self._current_theme_mode, save=False)

        # The reminder used to start unconditionally, so merely opening the
        # app produced a popup even with autostart off. Start it only when the
        # user asked for reminders, and never fire at launch: the hourly timer
        # owns the schedule. (--bg still notifies immediately; that run exists
        # to deliver the login reminder.)
        if self.notifications_check.isChecked():
            self._start_notifier(notify_now=False)

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

        # Theme picker: a menu button because the four modes are mutually
        # exclusive (Default / Light / Dark / Dynamic).
        self.theme_button = QPushButton("Themes")
        self.theme_button.setIcon(get_icon("dynamic_theme"))
        self.theme_button.setToolTip(
            "Choose a theme: Default, Light, Dark or Dynamic.\n"
            "Dynamic follows the wallpaper via Aether, or the built-in palette."
        )
        self.theme_menu = QMenu(self)
        self.theme_action_group = QActionGroup(self)
        self.theme_action_group.setExclusive(True)
        self.theme_mode_actions = {}
        for mode, label in (
            ("default", "Default"),
            ("light", "Light"),
            ("dark", "Dark"),
            ("dynamic", "Dynamic"),
        ):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setData(mode)
            action.triggered.connect(self.on_theme_action_triggered)
            self.theme_action_group.addAction(action)
            self.theme_menu.addAction(action)
            self.theme_mode_actions[mode] = action
        self.theme_button.setMenu(self.theme_menu)

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

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setSpacing(SIDEBAR_SPACING)

        sections = (
            ("Notifications", (self.notifications_check, self.check_box, self.sound_button)),
            ("Appearance", (self.theme_button,)),
            ("Player", (self.profile_button,)),
            (
                "App",
                (
                    self.close_to_tray_toggle,
                    self.auto_update_check,
                    self.google_sync_toggle,
                    self.install_desktop_btn,
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

        # Installing only works from an AppImage; elsewhere it can only fail.
        # Set after addWidget: re-parenting resets visibility to hidden.
        self.install_desktop_btn.setVisible(bool(os.environ.get("APPIMAGE")))

        return page

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
        # Settings is application-modal, so it has to close for the wizard to be usable.
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
            # The theme's horizontal padding leaves no room for a rail icon.
            button.setStyleSheet("padding: 0px;" if collapsed else "")

        if collapsed:
            # The chat panel's own padding also separates the icons from its
            # content, so match it on the left to keep the gaps either side even.
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

    def _setup_tray_icon(self):
        """Create the tray menu used to restore or fully exit the app."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        # Draw the small tray mark with Qt so it works in frozen builds even
        # when the platform's SVG image plugin is not bundled.
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#10182b"))
        painter.setPen(QColor("#00d9ff"))
        painter.drawEllipse(3, 3, 58, 58)
        painter.setPen(QColor("#00d9ff"))
        painter.setFont(QFont("Arial", 19, QFont.Weight.Bold))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "SL")
        painter.end()
        self.tray_icon = QSystemTrayIcon(QIcon(pixmap), self)
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
            # The app disables Qt's last-window auto-quit to support tray mode,
            # so explicitly quit when the user has opted out of tray behavior.
            QTimer.singleShot(0, QApplication.instance().quit)

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
        # Keep no stale reference after either finishing, skipping, or closing
        # the wizard, so it can be opened again from the main menu.
        if self.onboarding_window is not None:
            self.onboarding_window.deleteLater()
            self.onboarding_window = None
        if completed:
            # A profile now exists, so drop the urgency-only explanation.
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
            # The user just asked for reminders, so confirm immediately
            # instead of leaving them waiting an hour for the first tick.
            self._start_notifier(notify_now=True)
        else:
            self._stop_notifier()

    def sync_google_tasks(self):
        if self._sync_thread is not None:
            return
        self.google_button.setEnabled(False)
        self._set_sidebar_button_label(self.google_button, "Syncing...")

        self._sync_thread = QThread(self)
        self._sync_worker = _GoogleSyncWorker()
        self._sync_worker.moveToThread(self._sync_thread)
        self._sync_thread.started.connect(self._sync_worker.run)
        self._sync_worker.finished.connect(self._on_google_sync_finished)
        self._sync_worker.failed.connect(self._on_google_sync_failed)
        self._sync_worker.finished.connect(self._sync_thread.quit)
        self._sync_worker.failed.connect(self._sync_thread.quit)
        self._sync_thread.finished.connect(self._cleanup_google_sync)
        self._sync_thread.start()

    def _on_google_sync_finished(self, imported: int, skipped: int):
        if imported == 0 and skipped == 0:
            text = "No upcoming Google Tasks found."
        else:
            text = f"Imported {imported} task(s) from Google Tasks."
            if skipped:
                text += f"\n{skipped} already in your quest list were skipped."
        QMessageBox.information(self, "Google Sync", text)

    def _on_google_sync_failed(self, error: str):
        QMessageBox.warning(self, "Google Sync Failed", error)

    def _cleanup_google_sync(self):
        self.google_button.setEnabled(True)
        self._set_sidebar_button_label(self.google_button, "Google")
        self._sync_worker.deleteLater()
        self._sync_thread.deleteLater()
        self._sync_worker = None
        self._sync_thread = None

    def change_notification_sound(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Notification Sound",
            str(DEFAULT_SOUNDS_DIR),
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
            QMessageBox.warning(self, "Failed", "Could not install the app.\nMake sure you're running from the AppImage.")

    # --- NEW: Theme Manager Methods ---
    def on_theme_action_triggered(self):
        """Handle a theme menu action being triggered."""
        action = self.sender()
        if not action or not isinstance(action, QAction):
            return
        mode = action.data()
        if not isinstance(mode, str):
            return
        self._apply_theme_mode(mode, save=True)

    def _apply_theme_mode(self, mode: str, save: bool = True):
        """Apply the given theme mode and persist it if requested.

        Modes: ``default``, ``dark``, ``light``, ``dynamic``.
        """
        # Update the checkable action group so the UI reflects the choice.
        for action in self.theme_action_group.actions():
            if action.data() == mode:
                action.setChecked(True)

        self._current_theme_mode = mode

        if save:
            self.settings.setValue("theme_mode", mode)
            self.settings.sync()

        if mode == "dynamic":
            # Dynamic theming owns the stylesheet via its watchers/poller.
            self.start_dynamic_theming()
        else:
            # Static theme: stop any running dynamic watchers, then apply the
            # matching stylesheet (and icon colours) immediately.
            self.stop_dynamic_theming()
            apply_theme(QApplication.instance(), get_tokens_for_mode(mode))

        # Keep an open onboarding wizard in sync with the new palette.
        onboarding = getattr(self, "onboarding_window", None)
        if onboarding is not None:
            onboarding.refresh_theme()

    def start_dynamic_theming(self):
        self.wallpaper_timer.start(5000) # Poll every 5 seconds
        self.check_wallpaper_update() # Instantly check when toggled on

        # Start watching Aether's live theme file for instant updates.
        self._refresh_aether_watch()

    def stop_dynamic_theming(self):
        self.wallpaper_timer.stop()
        self.last_wallpaper_path = None
        if self.aether_theme_watcher.files():
            self.aether_theme_watcher.removePaths(
                self.aether_theme_watcher.files()
            )
        self._aether_theme_path = None
        self.last_aether_tokens = None

    def check_wallpaper_update(self):
        # Only the Dynamic mode should ever rewrite the stylesheet.
        if self.settings.value("theme_mode", "default", type=str) != "dynamic":
            return

        # Aether writes the live palette to colors.toml when you change the wallpaper.
        # If Aether is active, its QFileSystemWatcher handles updates instantly — do NOT
        # fall through to the wallpaper-polling fallback here, otherwise it would overwrite
        # the Aether-applied colors with locally-extracted ColorThief colors every 5s
        # (causing the "rolls back to defaults" behavior).
        if find_aether_colors_toml():
            self.check_aether_theme_update()  # apply once if watcher missed an event
            return

        # Fallback: poll the wallpaper path and extract colors locally (for systems
        # where Aether is not running or does not write a colors.toml).
        current_path = get_wallpaper_path()
        if current_path and current_path != self.last_wallpaper_path and "Error" not in current_path:
            self.last_wallpaper_path = current_path

            # Safely expand paths like ~/Pictures/wallpaper.jpg
            expanded_path = os.path.expanduser(current_path)

            # Generate new semantic tokens and inject them into the stylesheet
            new_tokens = generate_dynamic_tokens(expanded_path)
            apply_theme(QApplication.instance(), new_tokens)

    def check_aether_theme_update(self) -> bool:
        """Read Aether's live colors.toml and apply it to SL if it changed.

        Returns ``True`` if a new theme was applied, ``False`` otherwise.
        """
        if self.settings.value("theme_mode", "default", type=str) != "dynamic":
            return False

        theme_path = find_aether_colors_toml()
        if not theme_path:
            return False

        # Re-watch the file if Aether moved it. Aether rewrites colors.toml
        # in place, and some editors replace the file, which silently drops
        # Qt's watch — so re-add the path whenever it is not being watched.
        if theme_path != self._aether_theme_path or theme_path not in self.aether_theme_watcher.files():
            if self.aether_theme_watcher.files():
                self.aether_theme_watcher.removePaths(
                    self.aether_theme_watcher.files()
                )
            self.aether_theme_watcher.addPath(theme_path)
            self._aether_theme_path = theme_path

        new_tokens = generate_aether_tokens(theme_path)
        if new_tokens == self.last_aether_tokens:
            return False

        self.last_aether_tokens = new_tokens
        apply_theme(QApplication.instance(), new_tokens)
        return True

    def _refresh_aether_watch(self):
        """(Re)start watching Aether's live theme file if it exists."""
        theme_path = find_aether_colors_toml()
        if not theme_path:
            return
        if theme_path not in self.aether_theme_watcher.files():
            self.aether_theme_watcher.addPath(theme_path)
        self._aether_theme_path = theme_path


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


# # ─── ADD THIS RELOADER CLASS AT THE TOP OF YOUR FILE ──────────────────
# class DevelopmentAutoReloader(QObject):
#     """Watches the source file and instantly swaps the process in RAM when you save."""
#     def __init__(self, app_instance: QApplication):
#         super().__init__()
#         self.app = app_instance
#         self.watcher = QFileSystemWatcher()
        
#         # Monitor the absolute path of this main.py file
#         self.script_path = os.path.abspath(sys.argv[0])
#         self.watcher.addPath(self.script_path)
#         self.watcher.fileChanged.connect(self.trigger_hot_reload)
#         print(f"[RELOADER] Monitoring file for live RAM updates: {self.script_path}")

#     @Slot(str)
#     def trigger_hot_reload(self, path: str):
#         print("\n[RELOADER] Change detected! Swapping process in RAM...")
        
#         # Cross-platform check for compiled executable deployment vs source development
#         if getattr(sys, "frozen", False):
#             # Deployed Mode: The executable spawns itself directly
#             subprocess.Popen([sys.executable] + sys.argv[1:])
#         else:
#             # Development Mode: Python spawns the live script file
#             subprocess.Popen([sys.executable, self.script_path] + sys.argv[1:])
        
#         # Kill this old instance out of memory instantly
#         self.app.quit()
#         os._exit(0)

def _finish_check(flag, ok, details):
    """Report a --check-* result and exit with it.

    Windows release builds have no console, so the workflow reads the report
    file named after the flag, and the exit code.
    """
    report = sys.argv[sys.argv.index(flag) + 1:]
    if report:
        with open(report[0], "w", encoding="utf-8") as report_file:
            report_file.write(details + "\n")
    print(details)
    sys.stdout and sys.stdout.flush()
    # Skip Qt teardown: the check is over, and a crash there would hide its result.
    os._exit(0 if ok else 1)


def _run_ocr_check():
    from system_sl.chatbot.documents import check_ocr

    ok, details = check_ocr()
    _finish_check("--check-ocr", ok, details)


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
        apply_theme(app)
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
            # Longer than check_audio_duration() waits: a cold start is slow.
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
    if "--check-ocr" in sys.argv:
        _run_ocr_check()
    if "--check-gui" in sys.argv:
        _run_gui_check()

    # Handle --install-desktop flag before Qt initialization
    if "--install-desktop" in sys.argv:
        return install_desktop_entry()

    # Swap in a staged AppImage replacement before anything else touches disk.
    apply_pending_update()

    app = QApplication(sys.argv)

    # Initialize icon system (loads FontAwesome fonts)
    initialize_icons()

    # --- CHANGED: Inject the saved theme (and icon colours) on launch ---
    apply_theme(app, get_current_theme_tokens())
    app.setQuitOnLastWindowClosed(False)
    initialize_application_autostart()

    # reload = DevelopmentAutoReloader(app)

    view = None
    controller = None
    
    if "--bg" in sys.argv:
        # An autostart entry can outlive the preference: switching reminders
        # off does not remove it. Exit instead of idling for the whole session
        # and holding the lock the GUI needs.
        if not load_notifications_enabled():
            print(
                "[INFO] Notifications are disabled; "
                "not starting the background notifier."
            )
            sys.exit(0)
        print("[INFO] Running in background notifier mode...")
        # The notification widget re-reads the saved theme mode itself, but
        # applying it here also keeps any dialogs it opens consistent.
        apply_theme(app, get_current_theme_tokens())

        view = SystemNotification()
        controller = BackgroundServiceController(view)
        controller.poll_and_render_task()

        sys.exit(app.exec())
    # Background notifier mode: the autostart systemd unit launches the app with
    # `--bg`. In this mode we run ONLY the hourly task notifier, never the main
    # menu. Opening the menu here would make the always-restarting service
    # reopen it every time it was closed.


    # Load LLM configuration
    from system_sl.chatbot import load_config, validate_config, ChatConfig
    from system_sl.chatbot.llm_factory import ProviderPreset

    config = load_config()
    errors = validate_config(config)

    # If config is invalid or missing, show settings dialog
    if errors or not config.system_model:
        from system_sl.frontend.gui.chat_panel import SettingsDialog
        dialog = SettingsDialog()
        if dialog.exec() != QDialog.Accepted:
            QMessageBox.critical(
                None,
                "Configuration Required",
                "LLM configuration is required to continue.",
            )
            sys.exit(1)
        # Reload config after dialog
        config = load_config()
        errors = validate_config(config)
        if errors:
            QMessageBox.critical(
                None,
                "Invalid Configuration",
                "\n".join(errors),
            )
            sys.exit(1)

    # Do not copy the API key into os.environ. create_llm() is handed the key
    # explicitly, and exporting it here froze the key loaded at startup: a key
    # entered later in the settings dialog was ignored for the rest of the
    # process and then written back over the stored one.

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

            onboarding.onboarding_complete.connect(_on_onboarding_done)
            onboarding.show()
        else:
            main_window = MainWindow()
            main_window.show()
            main_window._check_for_updates()

    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
