"""The reminder is a centred window that stays until the player dismisses it."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from system_sl.utils import notifications_ui  # noqa: E402


def test_notification_is_the_centred_window_with_a_dismiss_button():
    app = QApplication.instance() or QApplication([])
    notifications_ui.play_sound = lambda *a, **k: None
    notifications_ui.get_current_sound = lambda *a, **k: "dummy.wav"

    window = notifications_ui.SystemNotification()
    window.display_message(["First task", "Second task"])

    # The old window, not the corner toast.
    assert window.width() == 800
    assert window.height() == 500
    assert window.header_text.text() == "NOTIFICATION"
    assert window.dismiss_btn.text() == "DISMISS"
    assert window.dismiss_btn.isVisible()
    # It stays open until dismissed: no auto-dismiss timer.
    assert not hasattr(window, "_auto_dismiss")

    # It never takes focus away from what the player is doing.
    assert window.windowFlags() & notifications_ui.Qt.WindowType.WindowDoesNotAcceptFocus
    assert not window.isActiveWindow()

    assert window.content_layout.count() == 2
    window.display_message(["First task", "Second task"])
    assert window.content_layout.count() == 2  # rebuilt, not appended twice
    window.close()
    app.processEvents()
