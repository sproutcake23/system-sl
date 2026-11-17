"""Central icon registry using QtAwesome (FontAwesome 6).

All icons are sourced from FontAwesome 6 Free (Solid/Brands/Regular).
Icons follow the active theme: they look up their colour each time they are
painted, so restyling the application recolours the ones already on screen.
"""

from typing import Optional

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QIcon, QIconEngine, QPainter, QPixmap
import qtawesome as qta

from system_sl.utils.theme_gen import DEFAULT_TOKENS


# Semantic name → FontAwesome 6 icon identifier
# Prefixes: fa6s = Solid, fa6b = Brands, fa6r = Regular
# Verified against FontAwesome 6 Free icon set
ICON_MAP = {
    # Main window left sidebar
    "tasks": "fa6s.list-check",
    "intel": "fa6s.newspaper",
    "google": "fa6b.google",
    "sound": "fa6s.bell",
    "install": "fa6s.download",
    "profile": "fa6s.user",
    "sidebar_collapse": "fa6s.angles-left",
    "sidebar_expand": "fa6s.angles-right",

    # Checkboxes / toggles
    "autostart": "fa6s.clock-rotate-left",
    "notifications": "fa6s.bell",
    "dynamic_theme": "fa6s.palette",
    "close_tray": "fa6s.window-minimize",
    "google_sync": "fa6s.cloud-arrow-up",
    "auto_update": "fa6s.arrows-rotate",

    # Chat panel header
    "refresh": "fa6s.arrows-rotate",
    "settings": "fa6s.gear",
    "model": "fa6s.robot",
    "delete_chat": "fa6s.trash",

    # Chat input row
    "attach": "fa6s.paperclip",
    "send": "fa6s.paper-plane",
    "stop": "fa6s.stop",

    # Attachment chips
    "remove": "fa6s.xmark",

    # Task window
    "complete": "fa6s.check",
    "remove_task": "fa6s.trash",
    "edit": "fa6s.pen-to-square",
    "add": "fa6s.plus",
    "deadline": "fa6s.calendar-days",

    # Onboarding / dialogs
    "begin": "fa6s.arrow-right",
    "skip": "fa6s.forward-step",
    "next": "fa6s.chevron-right",
    "finish": "fa6s.flag-checkered",
    "back": "fa6s.chevron-left",

    # Batch proposal dialog
    "select_all": "fa6s.check-double",
    "deselect_all": "fa6s.xmark",
    "confirm_add": "fa6s.check",
    "cancel": "fa6s.xmark",

    # Daily Intel
    "like": "fa6s.thumbs-up",
    "dislike": "fa6s.thumbs-down",
    "save": "fa6s.bookmark",
    "known": "fa6s.eye",
    "external_link": "fa6s.arrow-up-right-from-square",
    "topic": "fa6s.tags",
    "saved_filter": "fa6s.bookmark",

    # General UI
    "chevron_down": "fa6s.chevron-down",
    "chevron_up": "fa6s.chevron-up",
    "search": "fa6s.magnifying-glass",
    "filter": "fa6s.filter",
    "info": "fa6s.circle-info",
    "warning": "fa6s.triangle-exclamation",
    "error": "fa6s.circle-exclamation",
    "success": "fa6s.circle-check",
    "close": "fa6s.xmark",
    "menu": "fa6s.bars",
    "more": "fa6s.ellipsis",
}


_theme_tokens = DEFAULT_TOKENS


def set_icon_theme(tokens: dict) -> None:
    """Set the colours icons use from now on. Takes effect on the next repaint."""
    global _theme_tokens
    _theme_tokens = tokens


class _ThemedIconEngine(QIconEngine):
    """Draws a font icon in the active theme's colour for the given token."""

    def __init__(self, fa_name: str, role: str, options: dict):
        super().__init__()
        self._fa_name = fa_name
        self._role = role
        self._options = options
        self._color: Optional[str] = None
        self._icon: Optional[QIcon] = None

    def _current_icon(self) -> QIcon:
        color = _theme_tokens[self._role]
        if color != self._color:
            self._color = color
            self._icon = qta.icon(self._fa_name, color=color, **self._options)
        return self._icon

    def paint(self, painter, rect, mode, state):
        self._current_icon().paint(painter, rect, Qt.AlignmentFlag.AlignCenter, mode, state)

    def pixmap(self, size, mode, state):
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, QRect(QPoint(0, 0), size), mode, state)
        painter.end()
        return pixmap

    def clone(self):
        return _ThemedIconEngine(self._fa_name, self._role, self._options)


_icon_cache: dict[tuple[str, str, int], QIcon] = {}


def get_icon(
    name: str,
    color: Optional[str] = None,
    size: int = 18,
    role: str = "sys_color_primary",
    **kwargs
) -> QIcon:
    """Get a QIcon for the given semantic name.

    Args:
        name: Semantic key from ICON_MAP (e.g., "tasks", "send", "settings").
        color: Fixed CSS color string, for an icon that ignores the theme.
        size: Icon pixel size (default 18).
        role: Theme token supplying the colour when no fixed color is given.
        **kwargs: "extra" holds further qtawesome.icon() options.

    Returns:
        QIcon ready to set on QPushButton, QAction, QLabel, etc.
    """
    if name not in ICON_MAP:
        # Fallback: empty icon with warning
        import warnings
        warnings.warn(f"Icon '{name}' not found in ICON_MAP", stacklevel=2)
        return QIcon()

    fa_name = ICON_MAP[name]
    cache_key = (fa_name, color or role, size)

    if cache_key in _icon_cache:
        return _icon_cache[cache_key]

    options = {
        "scale_factor": 1.0,
        "font_size": f"{size}pt",  # qtawesome uses pt, not px
    }
    options.update(kwargs.get("extra", {}))

    if color:
        icon = qta.icon(fa_name, color=color, **options)
    else:
        icon = QIcon(_ThemedIconEngine(fa_name, role, options))
    _icon_cache[cache_key] = icon
    return icon


def initialize_icons() -> None:
    """Initialize qtawesome font loading. Call once at app startup."""
    # Force font loading by requesting a dummy icon
    _ = qta.icon("fa6s.circle")
