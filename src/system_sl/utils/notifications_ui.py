from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
)
from PySide6.QtCore import Qt
from system_sl.utils.audio_manager import play_sound, get_current_sound
from system_sl.frontend.gui.theme import get_current_theme_tokens


class SystemNotification(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(800, 500)
        self._center_window()
        self._setup_ui()
        self.refresh_theme()

    def _center_window(self):
        screen = self.screen()
        if screen:
            screen_geo = screen.geometry() # resolution of the whole screen
            x = (screen_geo.width() - self.width()) // 2
            y = (screen_geo.height() - self.height()) // 2
            self.move(x,y)

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
        self.header_text = QLabel("NOTIFICATION")
        self.header_text.setObjectName("headerText")
        header_layout.addStretch()
        header_layout.addWidget(self.header_text)
        header_layout.addStretch()

        self.content_area = QFrame()
        self.content_layout = QVBoxLayout(self.content_area)
        # self.cat_label = QLabel("[ CATEGORY ]")
        # self.cat_label.setObjectName("notifCategory")

        self.msg_labels = []


        self.dismiss_btn = QPushButton("DISMISS")
        self.dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dismiss_btn.clicked.connect(self.hide)

        self.container_layout.addWidget(self.header_frame)
        self.container_layout.addStretch()
        self.container_layout.addWidget(self.content_area)
        self.container_layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(self.dismiss_btn)
        btn_layout.addStretch()
        self.container_layout.addLayout(btn_layout)

        self.main_layout.addWidget(self.container)

    def display_message(self,message: list) -> None:
        if not message or not any(m.strip() for m in message):
            return
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        text = "Loading task"

        for i in range(len(message)):
            label = QLabel(f"{text}{i}")
            label.setObjectName("notifMessage")  # Keeps your QSS styling consistent

            self.content_layout.addWidget(label)  # Assuming self.layout is your QVBoxLayout
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.content_layout.addWidget(label)
            label.setText(message[i])

        current_sound = get_current_sound()
        play_sound(current_sound)
        self.show_system_style()

    def show_system_style(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def keyPressEvent(self, event):
        self.hide()

    def refresh_theme(self, tokens=None):
        """Re-apply the stylesheet using the current dynamic tokens.

        Called on init and before every notification so the notification
        always matches the live Aether palette.
        """
        if tokens is None:
            tokens = get_current_theme_tokens()

        bg = tokens.get("sys_color_background", "#060912")
        primary = tokens.get("sys_color_primary", "#00d9ff")
        text_main = tokens.get("sys_color_text_main", "#d6e7ff")
        text_secondary = tokens.get("sys_color_text_secondary", "#b3d4ff")
        text_muted = tokens.get("sys_color_text_muted", "#3d4d68")
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
            #notifCategory {{ color: {text_secondary}; font-weight: 700; font-size: 18px; }}
            #notifMessage {{ font-size: 22px; color: {text_main}; }}
            QPushButton {{
                border: 1px solid {primary};
                color: {primary};
                background: {surface};
                padding: 8px 35px;
                font-weight: 700;
            }}
            QPushButton:hover {{ background: {primary}; color: {text_main}; }}
        """)

    def display_message(self,message: list) -> None:
        if not message or not any(m.strip() for m in message):
            return
        # Re-apply the live theme before showing each notification.
        self.refresh_theme()
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        text = "Loading task"

        for i in range(len(message)):
            label = QLabel(f"{text}{i}")
            label.setObjectName("notifMessage")  # Keeps your QSS styling consistent

            self.content_layout.addWidget(label)  # Assuming self.layout is your QVBoxLayout
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.content_layout.addWidget(label)
            label.setText(message[i])

        current_sound = get_current_sound()
        play_sound(current_sound)
        self.show_system_style()
