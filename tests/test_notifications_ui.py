"""The reminder is a small toast that does not steal focus."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from system_sl.utils import notifications_ui  # noqa: E402


def test_toast_adds_each_line_once_and_does_not_activate():
    app = QApplication.instance() or QApplication([])
    notifications_ui.play_sound = lambda *a, **k: None

    toast = notifications_ui.SystemNotification()
    toast.display_message(["First task", "Second task"])

    assert toast.content_layout.count() == 2
    assert toast.windowFlags() & notifications_ui.Qt.WindowType.WindowDoesNotAcceptFocus
    assert not toast.isActiveWindow()
    # It must not be the old 800x500 centred window.
    assert toast.width() <= 500
    assert toast.height() <= 300

    toast.display_message(["First task", "Second task"])
    assert toast.content_layout.count() == 2  # rebuilt, not appended twice
    toast.close()
    app.processEvents()