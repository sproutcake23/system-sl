"""Small, non-blocking task-reminder toast.

This deliberately is not a modal window: a reminder must never steal focus
from what the Player is doing. It is a compact frameless toast in the corner
of the screen that dismisses itself, and can also be dismissed with Esc or a
click.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QFrame,
)

from system_sl.utils.audio_manager import play_sound, get_current_sound
from system_sl.frontend.gui.theme import get_current_theme_tokens


_TOAST_WIDTH = 360
_AUTO_DISMISS_MS = 8000
_SCREEN_MARGIN = 24


class SystemNotification(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedWidth(_TOAST_WIDTH)
        self._auto_dismiss = QTimer(self)
        self._auto_dismiss.setSingleShot(True)
        self._auto_dismiss.timeout.connect(self.hide)
        self._setup_ui()
        self.refresh_theme()

    def _setup_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        self.container = QFrame()
        self.container.setObjectName("systemWindow")
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(14, 10, 14, 12)
        self.container_layout.setSpacing(6)

        header_layout = QVBoxLayout()
        self.header_text = QLabel("THE SYSTEM")
        self.header_text.setObjectName("headerText")
        header_layout.addWidget(self.header_text)
        self.container_layout.addLayout(header_layout)

        self.content_area = QFrame()
        self.content_layout = QVBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(4)
        self.container_layout.addWidget(self.content_area)

        self.main_layout.addWidget(self.container)

    def _position_toast(self):
        screen = self.screen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        self.move(
            geometry.right() - self.width() - _SCREEN_MARGIN,
            geometry.bottom() - self.height() - _SCREEN_MARGIN,
        )

    def show_system_style(self):
        self.refresh_theme()
        self.adjustSize()
        self._position_toast()
        self.show()
        self.raise_()
        # Deliberately no activateWindow()/setFocus(): a reminder must not
        # take keyboard focus away from the Player.

    def mousePressEvent(self, event):
        self.hide()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)

    def hide(self):
        self._auto_dismiss.stop()
        super().hide()

    def refresh_theme(self, tokens=None):
        """Re-apply the stylesheet using the current theme tokens."""
        if tokens is None:
            tokens = get_current_theme_tokens()

        bg = tokens.get("sys_color_background", "#060912")
        primary = tokens.get("sys_color_primary", "#00d9ff")
        text_main = tokens.get("sys_color_text_main", "#d6e7ff")
        surface = tokens.get("sys_color_surface", "#0b0f1a")

        self.setStyleSheet(f"""
            QWidget {{
                background: transparent;
                font-family: 'Montserrat', sans-serif;
            }}
            #systemWindow {{
                background-color: {surface};
                border: 1px solid {primary};
                border-radius: 10px;
            }}
            #headerText {{
                color: {primary};
                font-size: 11px;
                font-weight: 700;
                letter-spacing: 3px;
            }}
            #notifMessage {{
                color: {text_main};
                font-size: 14px;
                qproperty-wordWrap: true;
            }}
        """)

    def display_message(self, message: list) -> None:
        if not message or not any(str(m).strip() for m in message):
            return

        self.refresh_theme()
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for text in message:
            label = QLabel(str(text))
            label.setObjectName("notifMessage")
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            self.content_layout.addWidget(label)

        play_sound(get_current_sound())
        self.show_system_style()
        self._auto_dismiss.start(_AUTO_DISMISS_MS)