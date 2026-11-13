"""Settings preview of the active palette, with Dynamic-mode controls.

Shows a row of colour swatches for the palette on screen, so the player can
see what Dynamic picked up. In Dynamic mode it adds:

* Refresh from wallpaper: look at the wallpaper again now;
* Choose wallpaper…: take colours from a picked image instead of the
  detected wallpaper (for desktops where detection fails);
* Use detected wallpaper: drop that choice.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from system_sl.frontend.gui.icons import get_icon

# (token, label) for each swatch, in display order.
SWATCHES = (
    ("sys_color_background", "Background"),
    ("sys_color_surface", "Surface"),
    ("sys_color_primary", "Accent"),
    ("sys_color_text_main", "Text"),
    ("sys_color_success", "Success"),
    ("sys_color_alert", "Alert"),
)

IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.jxl *.avif *.heic *.heif)"
SWATCH_SIZE = 22


class ThemePreview(QWidget):
    """Palette swatches plus the Dynamic wallpaper controls."""

    def __init__(self, manager, button_height: int | None = None, parent=None):
        super().__init__(parent)
        self._manager = manager

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(4)
        self._swatches: dict[str, QFrame] = {}
        for token, label in SWATCHES:
            swatch = QFrame()
            swatch.setObjectName("themeSwatch")
            swatch.setFixedSize(SWATCH_SIZE, SWATCH_SIZE)
            swatch.setAccessibleName(f"{label} colour")
            self._swatches[token] = swatch
            swatch_row.addWidget(swatch)
        swatch_row.addStretch(1)
        root.addLayout(swatch_row)

        self.refresh_button = QPushButton("Refresh from wallpaper")
        self.refresh_button.setIcon(get_icon("refresh"))
        self.refresh_button.setToolTip("Read the wallpaper's colours again now.")
        self.refresh_button.clicked.connect(self._manager.refresh_wallpaper)

        self.choose_button = QPushButton("Choose wallpaper…")
        self.choose_button.setToolTip(
            "Take the colours from an image you pick instead of the detected "
            "wallpaper. Useful when your desktop's wallpaper can't be detected."
        )
        self.choose_button.clicked.connect(self._choose_wallpaper)

        self.detected_button = QPushButton("Use detected wallpaper")
        self.detected_button.setToolTip("Go back to following your desktop wallpaper.")
        self.detected_button.clicked.connect(
            lambda: self._manager.set_wallpaper_override(None)
        )

        self._dynamic_controls = QWidget()
        controls = QVBoxLayout(self._dynamic_controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)
        for button in (self.refresh_button, self.choose_button, self.detected_button):
            button.setCursor(Qt.PointingHandCursor)
            if button_height:
                button.setFixedHeight(button_height)
            controls.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
        root.addWidget(self._dynamic_controls)

        manager.themeChanged.connect(self._paint)
        manager.modeChanged.connect(self._update_controls)
        manager.overrideChanged.connect(lambda _path: self._update_controls())
        self._paint(manager.tokens)
        self._update_controls()

    def _paint(self, tokens: dict) -> None:
        border = tokens.get("sys_color_border_muted", "#1a2638")
        for token, label in SWATCHES:
            value = tokens.get(token, "")
            swatch = self._swatches[token]
            swatch.setStyleSheet(
                f"QFrame#themeSwatch {{ background: {value};"
                f" border: 1px solid {border}; border-radius: 4px; }}"
            )
            swatch.setToolTip(f"{label}: {value}")

    def _update_controls(self, *_args) -> None:
        dynamic = self._manager.mode == "dynamic"
        self._dynamic_controls.setVisible(dynamic)
        self.detected_button.setVisible(dynamic and bool(self._manager.wallpaper_override))

    def _choose_wallpaper(self) -> None:
        current = self._manager.wallpaper_override
        start = os.path.dirname(current) if current else os.path.expanduser("~/Pictures")
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a wallpaper for Dynamic colours", start, IMAGE_FILTER
        )
        if path:
            self._manager.set_wallpaper_override(path)
