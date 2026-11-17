"""Task-reminder window: a centred panel that stays until dismissed.

This is the original notification window rather than the corner toast. It is
a fixed-size panel in the middle of the screen with a DISMISS button, and it
does not disappear on a timer. It still avoids stealing keyboard focus, so a
reminder never interrupts what the Player is doing.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
)

from system_sl.utils.audio_manager import play_sound, get_current_sound
from system_sl.frontend.gui.theme import get_current_theme_tokens
from system_sl.utils.i18n import tr


_WINDOW_WIDTH = 800
_WINDOW_HEIGHT = 500


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
        self.setFixedSize(_WINDOW_WIDTH, _WINDOW_HEIGHT)
        self._center_window()
        self._setup_ui()
        self.refresh_theme()

    def _center_window(self):
        screen = self.screen()
        if screen is None:
            return
        geometry = screen.geometry()
        self.move(
            (geometry.width() - self.width()) // 2,
            (geometry.height() - self.height()) // 2,
        )

    def _setup_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        self.container = QFrame()
        self.container.setObjectName("systemWindow")
        self.container_layout = QVBoxLayout(self.container)

        self.header_frame = QFrame()
        self.header_frame.setObjectName("header")
        header_layout = QHBoxLayout(self.header_frame)
        self.header_text = QLabel(tr("NOTIFICATION"))
        self.header_text.setObjectName("headerText")
        header_layout.addStretch()
        header_layout.addWidget(self.header_text)
        header_layout.addStretch()

        self.content_area = QFrame()
        self.content_layout = QVBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(8)

        self.container_layout.addWidget(self.header_frame)
        self.container_layout.addStretch()
        self.container_layout.addWidget(self.content_area)
        self.container_layout.addStretch()

        button_layout = QHBoxLayout()
        button_layout.addStretch()
        self.dismiss_btn = QPushButton(tr("DISMISS"))
        self.dismiss_btn.setObjectName("dismissButton")
        self.dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dismiss_btn.clicked.connect(self.hide)
        button_layout.addWidget(self.dismiss_btn)
        button_layout.addStretch()
        self.container_layout.addLayout(button_layout)

        self.main_layout.addWidget(self.container)

    def show_system_style(self):
        self.refresh_theme()
        self._center_window()
        self.show()
        self.raise_()
        # Deliberately no activateWindow(): a reminder must not take focus.

    def refresh_theme(self, tokens=None):
        """Re-apply the stylesheet using the current theme tokens."""
        if tokens is None:
            tokens = get_current_theme_tokens()

        bg = tokens.get("sys_color_background", "#060912")
        primary = tokens.get("sys_color_primary", "#00d9ff")
        on_primary = tokens.get("sys_color_on_primary", "#04121a")
        text_main = tokens.get("sys_color_text_main", "#d6e7ff")
        surface = tokens.get("sys_color_surface", "#0b0f1a")

        self.setStyleSheet(f"""
            QWidget {{
                background: transparent;
                font-family: 'Montserrat', sans-serif;
            }}
            #systemWindow {{
                background-color: {bg};
                border: 1px solid {primary};
                border-radius: 8px;
            }}
            #header {{
                border: 1px solid {primary};
                margin: 40px 100px 0px 100px;
                max-height: 50px;
            }}
            #headerText {{ color: {text_main}; font-size: 20px; letter-spacing: 4px; }}
            #notifMessage {{ font-size: 22px; color: {text_main}; }}
            QPushButton {{
                border: 1px solid {primary};
                color: {primary};
                background: {surface};
                padding: 8px 35px;
                font-weight: 700;
            }}
            QPushButton:hover {{ background: {primary}; color: {on_primary}; }}
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
            # Task titles are literal text; never render one as HTML.
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setObjectName("notifMessage")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.content_layout.addWidget(label)

        play_sound(get_current_sound())
        self.show_system_style()
