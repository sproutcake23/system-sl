#!/usr/bin/env python3
"""
Minimal entry point for the background notifier (--bg mode).

This avoids importing the full GUI stack (chat, settings, etc.) and only
loads what's needed for hourly task notifications.
"""

import os
import sys
from pathlib import Path

# Add src to path for frozen builds
if getattr(sys, "frozen", False):
    bundle_dir = Path(sys._MEIPASS)
    src_path = bundle_dir / "src"
    if src_path.exists():
        sys.path.insert(0, str(src_path))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

from system_sl.utils import SystemNotification
from system_sl.services import BackgroundServiceController
from system_sl.utils.notification_settings import load_notifications_enabled
from system_sl.utils.paths import get_tasks_file_path
from system_sl.utils.autostart_migration import initialize_application_autostart
from system_sl.utils.auto_updater import apply_pending_update
from system_sl.utils.worker_threads import shutdown_workers


def main():
    # Handle --install-desktop before Qt initialization
    if "--install-desktop" in sys.argv:
        from system_sl.frontend.gui.desktop_entry import install_desktop_entry
        return install_desktop_entry()

    # Swap in a staged AppImage replacement before anything else touches disk.
    apply_pending_update()

    # Reuse an existing QApplication when one is already alive. Creating a
    # second one aborts with "Please destroy the QApplication singleton".
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    initialize_application_autostart()

    # An autostart entry can outlive the preference: switching reminders
    # off does not remove it. Exit instead of idling for the whole session
    # and holding the lock the GUI needs.
    if not load_notifications_enabled():
        print(
            "[INFO] Notifications are disabled; "
            "not starting the background notifier."
        )
        return 0

    print("[INFO] Running in background notifier mode...")

    view = SystemNotification()
    controller = BackgroundServiceController(view)
    # Let a ranking pass in flight finish before teardown destroys its thread.
    app.aboutToQuit.connect(controller.stop)
    app.aboutToQuit.connect(shutdown_workers)
    controller.poll_and_render_task()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())