"""Central icon registry using QtAwesome (FontAwesome 6).

All icons are sourced from FontAwesome 6 Free (Solid/Brands/Regular).
Colors default to the theme's primary color (cyan #00d9ff) but can be overridden.
"""

from typing import Optional
from PySide6.QtGui import QIcon, QColor
import qtawesome as qta


# Semantic name → FontAwesome 6 icon identifier
# Prefixes: fa6s = Solid, fa6b = Brands, fa6r = Regular
# Verified against FontAwesome 6 Free icon set
ICON_MAP = {
    # Main window left sidebar
    "tasks": "fa6s.list-check",
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


# Cache for colored icons to avoid regenerating
_icon_cache: dict[tuple[str, Optional[str], int], QIcon] = {}


def get_icon(
    name: str,
    color: Optional[str] = None,
    size: int = 18,
    **kwargs
) -> QIcon:
    """Get a QIcon for the given semantic name.

    Args:
        name: Semantic key from ICON_MAP (e.g., "tasks", "send", "settings").
        color: CSS color string (e.g., "#00d9ff", "white", "currentColor").
               If None, uses the default theme primary color via qtawesome's
               "active" state coloring.
        size: Icon pixel size (default 18).
        **kwargs: Passed to qtawesome.icon() for advanced options
                  (e.g., rotation, animation, opacity).

    Returns:
        QIcon ready to set on QPushButton, QAction, QLabel, etc.
    """
    if name not in ICON_MAP:
        # Fallback: empty icon with warning
        import warnings
        warnings.warn(f"Icon '{name}' not found in ICON_MAP", stacklevel=2)
        return QIcon()

    fa_name = ICON_MAP[name]
    cache_key = (fa_name, color, size)

    if cache_key in _icon_cache:
        return _icon_cache[cache_key]

    # Build qtawesome options
    kwargs_qta = {
        "scale_factor": 1.0,
        "font_size": f"{size}pt",  # qtawesome uses pt, not px
    }

    if color:
        kwargs_qta["color"] = color
    else:
        # Default to primary cyan
        kwargs_qta["color"] = "#00d9ff"

    # Merge any extra kwargs
    kwargs_qta.update(kwargs.get("extra", {}))

    icon = qta.icon(fa_name, **kwargs_qta)
    _icon_cache[cache_key] = icon
    return icon


def get_icon_char(name: str, color: Optional[str] = None, size: int = 18) -> str:
    """Get a HTML entity / unicode char for inline use in QLabel HTML.

    Note: This is a fallback for cases where QIcon can't be used directly
    in rich text. For buttons, prefer get_icon().
    """
    # FontAwesome doesn't work in Qt rich text without font embedding.
    # Return empty string - use QIcon on widgets instead.
    return ""


# Convenience: pre-configured icon getters for common colors
def get_icon_primary(name: str, size: int = 18) -> QIcon:
    """Icon in primary cyan (#00d9ff)."""
    return get_icon(name, color="#00d9ff", size=size)


def get_icon_text_main(name: str, size: int = 18) -> QIcon:
    """Icon in main text color (#d6e7ff)."""
    return get_icon(name, color="#d6e7ff", size=size)


def get_icon_text_secondary(name: str, size: int = 18) -> QIcon:
    """Icon in secondary text color (#9ab8e5)."""
    return get_icon(name, color="#9ab8e5", size=size)


def get_icon_muted(name: str, size: int = 18) -> QIcon:
    """Icon in muted color (#5a5a7a)."""
    return get_icon(name, color="#5a5a7a", size=size)


def get_icon_error(name: str, size: int = 18) -> QIcon:
    """Icon in error red (#ff6b8a)."""
    return get_icon(name, color="#ff6b8a", size=size)


def get_icon_success(name: str, size: int = 18) -> QIcon:
    """Icon in success green (#4ade80)."""
    return get_icon(name, color="#4ade80", size=size)


# For dynamic theming: refresh cache when theme colors change
def clear_icon_cache() -> None:
    """Clear the icon cache - call when theme colors change."""
    global _icon_cache
    _icon_cache.clear()


# Initialize qtawesome with FontAwesome 6
def initialize_icons() -> None:
    """Initialize qtawesome font loading. Call once at app startup."""
    # Force font loading by requesting a dummy icon
    _ = qta.icon("fa6s.circle", color="#00d9ff")