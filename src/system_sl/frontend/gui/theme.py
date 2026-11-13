"""Theme tokens, theme-mode resolution, and the application stylesheet.

The app has four modes:

* ``system``  – follow the operating system's light/dark appearance; its dark
  side uses THE SYSTEM's Solo Leveling navy palette
* ``light``   – the built-in light palette
* ``dark``    – a darker, OLED-friendly near-black palette
* ``dynamic`` – colours extracted from the wallpaper by wallust

Every UI target reads :func:`get_current_theme_tokens`, so switching modes (or
a wallpaper change in Dynamic mode) restyles the whole app, not one window.
"""

import base64
import logging

# --- Light theme ---
LIGHT_TOKENS = {
    "sys_color_background": "#ffffff",
    "sys_color_surface": "#f8fafc",
    "sys_color_surface_raised": "#eef2f7",
    "sys_color_border": "#cbd5e1",
    "sys_color_border_muted": "#e2e8f0",
    "sys_color_primary": "#0891b2",
    "sys_color_primary_light": "#0e7490",
    "sys_color_primary_dim": "rgba(8, 145, 178, 0.12)",
    "sys_color_primary_hover": "rgba(8, 145, 178, 0.20)",
    "sys_color_primary_pressed": "rgba(8, 145, 178, 0.30)",
    "sys_color_text_main": "#0b1220",
    "sys_color_text_secondary": "#334155",
    "sys_color_text_highlight": "#0e7490",
    "sys_color_text_muted": "#64748b",
    "sys_color_on_primary": "#ffffff",
    "sys_color_button_text": "#0e7490",
    "sys_color_success": "#15803d",
    "sys_color_alert": "#dc2626",
    "sys_color_on_alert": "#ffffff",
    "sys_color_disabled_surface": "rgba(15, 23, 42, 0.04)",
    "sys_color_selection_text": "#0b1220",
}

# --- Dark theme: near-black, so it is visibly darker than the navy default ---
DARK_TOKENS = {
    "sys_color_background": "#000000",
    "sys_color_surface": "#050505",
    "sys_color_surface_raised": "#0b0b0b",
    "sys_color_border": "#262626",
    "sys_color_border_muted": "#141414",
    "sys_color_primary": "#00d9ff",
    "sys_color_primary_light": "#67e8ff",
    "sys_color_primary_dim": "rgba(0, 217, 255, 0.10)",
    "sys_color_primary_hover": "rgba(0, 217, 255, 0.18)",
    "sys_color_primary_pressed": "rgba(0, 217, 255, 0.30)",
    "sys_color_text_main": "#f5f5f5",
    "sys_color_text_secondary": "#c7c7c7",
    "sys_color_text_highlight": "#67e8ff",
    "sys_color_text_muted": "#7a7a7a",
    "sys_color_on_primary": "#04121a",
    "sys_color_button_text": "#67e8ff",
    "sys_color_success": "#4ade80",
    "sys_color_alert": "#ff6b8a",
    "sys_color_on_alert": "#ffffff",
    "sys_color_disabled_surface": "rgba(255, 255, 255, 0.02)",
    "sys_color_selection_text": "#ffffff",
}

THEME_MODES = ("system", "light", "dark", "dynamic")
THEME_MODE_LABELS = {
    "system": "System",
    "light": "Light",
    "dark": "Dark",
    "dynamic": "Dynamic",
}
DEFAULT_THEME_MODE = "system"

log = logging.getLogger(__name__)

_active_tokens = None


def _default_tokens() -> dict:
    """Lazily load the navy DEFAULT_TOKENS to avoid circular imports."""
    from system_sl.utils.theme_gen import DEFAULT_TOKENS

    return dict(DEFAULT_TOKENS)


def normalize_theme_mode(mode) -> str:
    """Map legacy values to one of :data:`THEME_MODES`."""
    if mode in THEME_MODES:
        return mode
    if mode == "default":
        return "dark"
    return DEFAULT_THEME_MODE


def get_saved_theme_mode(settings=None) -> str:
    """Read ``theme_mode``; never writes (see :func:`migrate_saved_theme_mode`).

    Settings saved before ``theme_mode`` existed hold a ``dynamic_theming``
    boolean instead, which is honoured here.
    """
    from PySide6.QtCore import QSettings

    settings = settings or QSettings("system-sl", "AppConfig")
    raw = settings.value("theme_mode", None, type=str)
    if not raw:
        legacy_dynamic = settings.value("dynamic_theming", False, type=bool)
        return "dynamic" if legacy_dynamic else DEFAULT_THEME_MODE
    return normalize_theme_mode(raw)


def migrate_saved_theme_mode(settings=None) -> None:
    """Rewrite a legacy or unknown saved mode as a current one, once.

    Kept out of :func:`get_saved_theme_mode` so reading the mode has no side
    effects and works on a read-only config directory.
    """
    from PySide6.QtCore import QSettings

    settings = settings or QSettings("system-sl", "AppConfig")
    raw = settings.value("theme_mode", None, type=str)
    mode = get_saved_theme_mode(settings)
    if raw == mode:
        return
    settings.setValue("theme_mode", mode)
    settings.sync()
    if settings.status() != QSettings.Status.NoError:
        log.warning("Could not save the migrated theme mode")


def get_tokens_for_mode(mode: str = DEFAULT_THEME_MODE) -> dict:
    """Return a complete token dict for *mode*."""
    mode = normalize_theme_mode(mode)
    if mode == "light":
        return dict(LIGHT_TOKENS)
    if mode == "dark":
        return dict(DARK_TOKENS)
    if mode == "dynamic":
        from system_sl.utils.wallust_theme import cached_wallust_tokens

        return cached_wallust_tokens()

    from system_sl.utils.system_theme import detect_system_color_scheme

    if detect_system_color_scheme() == "light":
        return dict(LIGHT_TOKENS)
    return _default_tokens()


def set_active_tokens(tokens=None) -> None:
    """Record the tokens the ThemeManager applied; used by every UI target."""
    global _active_tokens
    _active_tokens = dict(tokens) if tokens else None


def get_current_theme_tokens() -> dict:
    """Return the tokens currently painted, resolving the saved mode if needed."""
    if _active_tokens is not None:
        return dict(_active_tokens)
    return get_tokens_for_mode(get_saved_theme_mode())


def apply_theme(app, tokens=None):
    """Restyle the whole application, including icons, with the given tokens."""
    # Imported lazily: icons -> theme_gen would otherwise be a circular import
    # at startup.
    from system_sl.frontend.gui.icons import set_icon_theme

    if tokens is None:
        tokens = _default_tokens()
    set_active_tokens(tokens)
    # Icons first: restyling repaints every widget, which is what recolours them.
    set_icon_theme(tokens)
    # The palette covers what the stylesheet does not reach: Qt's own file
    # and message dialogs, standard icons and some popups, which otherwise
    # follow the OS (a light file picker over a dark app).
    app.setPalette(build_palette(tokens))
    app.setStyleSheet(get_stylesheet(tokens))


def _qcolor(tokens: dict, key: str):
    """Return the token as a QColor, falling back to the built-in palette."""
    from PySide6.QtGui import QColor

    color = QColor(str(tokens.get(key, "")))
    if not color.isValid():
        color = QColor(_default_tokens()[key])
    return color


def build_palette(tokens: dict):
    """Build a QPalette that matches the stylesheet colours."""
    from PySide6.QtGui import QPalette

    role = QPalette.ColorRole
    palette = QPalette()
    roles = {
        role.Window: "sys_color_background",
        role.WindowText: "sys_color_text_main",
        role.Base: "sys_color_surface",
        role.AlternateBase: "sys_color_surface_raised",
        role.ToolTipBase: "sys_color_surface_raised",
        role.ToolTipText: "sys_color_text_main",
        role.PlaceholderText: "sys_color_text_muted",
        role.Text: "sys_color_text_main",
        role.Button: "sys_color_surface_raised",
        role.ButtonText: "sys_color_text_main",
        role.BrightText: "sys_color_on_alert",
        role.Highlight: "sys_color_primary",
        role.HighlightedText: "sys_color_on_primary",
        role.Link: "sys_color_primary",
        role.LinkVisited: "sys_color_primary_light",
        role.Light: "sys_color_border",
        role.Midlight: "sys_color_border_muted",
        role.Mid: "sys_color_border_muted",
        role.Dark: "sys_color_background",
        role.Shadow: "sys_color_background",
    }
    for palette_role, key in roles.items():
        palette.setColor(palette_role, _qcolor(tokens, key))
    disabled = QPalette.ColorGroup.Disabled
    for palette_role in (role.WindowText, role.Text, role.ButtonText):
        palette.setColor(disabled, palette_role, _qcolor(tokens, "sys_color_text_muted"))
    return palette


def _checkmark_uri(color: str) -> str:
    svg = (
        '<svg width="12" height="9" viewBox="0 0 12 9" fill="none" '
        'xmlns="http://www.w3.org/2000/svg">'
        '<path d="M1 4.5L4.5 8L11 1" stroke="' + color + '" stroke-width="2.5" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"url(data:image/svg+xml;base64,{encoded})"


def get_stylesheet(t=None):
    """Generate the QSS string for a token dictionary."""
    if t is None:
        t = _default_tokens()

    checked_mark = _checkmark_uri(t["sys_color_on_primary"])
    disabled_mark = _checkmark_uri(t["sys_color_text_muted"])

    return f"""
/* === THE SYSTEM theme === */

QWidget {{
    background-color: {t['sys_color_surface']};
    color: {t['sys_color_text_main']};
    font-family: "Segoe UI", "Helvetica Neue", "DejaVu Sans", sans-serif;
    font-size: 13px;
}}

QMainWindow, QDialog {{
    background-color: {t['sys_color_background']};
}}

QLabel {{
    background-color: transparent;
    color: {t['sys_color_text_secondary']};
}}

/* --- Standing notice: explains why ranking is degraded --- */
QLabel#profileHint {{
    color: {t['sys_color_text_secondary']};
    font-size: 11px;
    padding: 4px 6px;
    border-left: 2px solid {t['sys_color_primary_dim']};
}}

/* --- Buttons --- */
QPushButton {{
    background-color: {t['sys_color_primary_dim']};
    color: {t['sys_color_button_text']};
    border: 1px solid {t['sys_color_border']};
    border-radius: 0px;
    padding: 6px 14px;
    font-weight: bold;
}}

QPushButton:hover {{
    background-color: {t['sys_color_primary_hover']};
    color: {t['sys_color_text_highlight']};
    border: 1px solid {t['sys_color_primary']};
}}

QPushButton:pressed {{
    background-color: {t['sys_color_primary_pressed']};
    border: 1px solid {t['sys_color_primary_light']};
}}

QPushButton:checked {{
    background-color: {t['sys_color_primary_pressed']};
    color: {t['sys_color_text_highlight']};
    border: 1px solid {t['sys_color_primary']};
}}

QPushButton:disabled {{
    background-color: {t['sys_color_disabled_surface']};
    color: {t['sys_color_text_muted']};
    border: 1px solid {t['sys_color_border_muted']};
}}

/* --- Sidebar collapse button: outlined so its edge lines up with the column --- */
QPushButton#sidebarToggle {{
    background-color: transparent;
    border: 1px solid {t['sys_color_border']};
    padding: 0px;
}}

QPushButton#sidebarToggle:hover {{
    background-color: {t['sys_color_primary_hover']};
    border: 1px solid {t['sys_color_primary']};
}}

QPushButton#sidebarToggle:pressed {{
    background-color: {t['sys_color_primary_pressed']};
    border: 1px solid {t['sys_color_primary_light']};
}}

/* --- Settings window --- */
QTabWidget::pane {{
    border: 1px solid {t['sys_color_border']};
}}

QTabBar::tab {{
    background-color: transparent;
    color: {t['sys_color_text_secondary']};
    border: 1px solid {t['sys_color_border_muted']};
    padding: 7px 18px;
    margin-right: 4px;
}}

QTabBar::tab:hover {{
    background-color: {t['sys_color_primary_hover']};
}}

QTabBar::tab:selected {{
    background-color: {t['sys_color_primary_dim']};
    color: {t['sys_color_button_text']};
    border: 1px solid {t['sys_color_border']};
}}

QTabBar::tab:disabled {{
    color: {t['sys_color_text_muted']};
}}

QLabel#settingsHeading {{
    color: {t['sys_color_button_text']};
    font-weight: bold;
}}

QLabel#themeStatus {{
    color: {t['sys_color_alert']};
    font-size: 11px;
}}

/* --- Checkbox --- */
QCheckBox {{
    color: {t['sys_color_text_secondary']};
    spacing: 10px;
    background-color: transparent;
    font-size: 13px;
}}

QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border: 2px solid {t['sys_color_border']};
    border-radius: 3px;
    background-color: {t['sys_color_surface']};
    image: none;
}}

QCheckBox::indicator:hover {{
    border: 2px solid {t['sys_color_primary']};
}}

QCheckBox::indicator:checked {{
    background-color: {t['sys_color_primary']};
    border: 2px solid {t['sys_color_primary']};
    image: {checked_mark};
}}

QCheckBox::indicator:disabled {{
    background-color: {t['sys_color_background']};
    border: 2px solid {t['sys_color_border_muted']};
    color: {t['sys_color_text_muted']};
}}

QCheckBox::indicator:checked:disabled {{
    background-color: {t['sys_color_primary_dim']};
    border: 2px solid {t['sys_color_primary_dim']};
    image: {disabled_mark};
}}

/* --- Text inputs --- */
QLineEdit, QDateEdit, QTextEdit, QComboBox, QSpinBox, QPlainTextEdit {{
    background-color: {t['sys_color_surface_raised']};
    color: {t['sys_color_text_main']};
    border: 1px solid {t['sys_color_border']};
    border-radius: 0px;
    padding: 4px 6px;
    selection-background-color: {t['sys_color_border']};
    selection-color: {t['sys_color_selection_text']};
}}

QLineEdit:focus, QDateEdit:focus, QTextEdit:focus,
QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{
    border: 1px solid {t['sys_color_primary']};
}}

QLineEdit:disabled, QDateEdit:disabled, QComboBox:disabled {{
    background-color: {t['sys_color_background']};
    color: {t['sys_color_text_muted']};
    border: 1px solid {t['sys_color_border_muted']};
}}

/* --- Inline task confirmation bar --- */
QFrame#taskConfirmBar {{
    background-color: {t['sys_color_surface_raised']};
    border: 1px solid {t['sys_color_primary']};
}}
QFrame#taskConfirmBar QLabel {{
    color: {t['sys_color_text_main']};
    border: none;
    background: transparent;
}}

/* --- Queued messages badge --- */
QLabel#queueBadge {{
    color: {t['sys_color_text_secondary']};
    background: transparent;
    border: none;
    padding: 2px 6px 0px 6px;
    font-size: 11px;
}}
QLabel#queueBadge:hover {{
    color: {t['sys_color_primary']};
}}

/* --- List widget (task list) --- */
QListWidget {{
    background-color: {t['sys_color_surface_raised']};
    color: {t['sys_color_text_main']};
    border: 1px solid {t['sys_color_border']};
    border-radius: 0px;
    outline: 0;
}}

QListWidget::item {{
    padding: 6px 8px;
    border-bottom: 1px solid {t['sys_color_background']};
}}

QListWidget::item:hover {{
    background-color: {t['sys_color_primary_dim']};
}}

QListWidget::item:selected {{
    background-color: {t['sys_color_primary_hover']};
    color: {t['sys_color_text_highlight']};
    border-left: 2px solid {t['sys_color_primary']};
}}

/* --- Progress bar --- */
QProgressBar {{
    background-color: {t['sys_color_surface_raised']};
    border: 1px solid {t['sys_color_border']};
    border-radius: 0px;
    text-align: center;
    color: {t['sys_color_text_secondary']};
    height: 22px;
    font-size: 12px;
}}

QProgressBar::chunk {{
    background-color: {t['sys_color_primary']};
}}

/* --- Combo box popup --- */
QComboBox::drop-down {{
    border: none;
    width: 18px;
    background-color: transparent;
}}

QComboBox::down-arrow {{
    image: none;
    width: 6px;
    height: 6px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {t['sys_color_button_text']};
    margin-right: 6px;
}}

QComboBox QAbstractItemView {{
    background-color: {t['sys_color_surface_raised']};
    color: {t['sys_color_text_main']};
    border: 1px solid {t['sys_color_border']};
    selection-background-color: {t['sys_color_primary_dim']};
    selection-color: {t['sys_color_text_highlight']};
}}

/* --- Date edit / calendar popup --- */
QDateEdit::drop-down {{
    border: none;
    width: 18px;
}}

QCalendarWidget {{
    background-color: {t['sys_color_surface_raised']};
}}

QCalendarWidget QWidget {{
    background-color: {t['sys_color_surface_raised']};
    color: {t['sys_color_text_main']};
}}

QCalendarWidget QWidget#qt_calendar_navigationbar {{
    background-color: {t['sys_color_background']};
}}

QCalendarWidget QToolButton {{
    color: {t['sys_color_text_highlight']};
    background-color: transparent;
    border: 1px solid transparent;
    padding: 4px 8px;
}}

QCalendarWidget QToolButton:hover {{
    background-color: {t['sys_color_primary_hover']};
    border: 1px solid {t['sys_color_primary']};
}}

QCalendarWidget QSpinBox {{
    background-color: {t['sys_color_surface_raised']};
    color: {t['sys_color_text_main']};
    border: 1px solid {t['sys_color_border']};
}}

QCalendarWidget QAbstractItemView:enabled {{
    background-color: {t['sys_color_surface_raised']};
    color: {t['sys_color_text_main']};
    selection-background-color: {t['sys_color_primary_hover']};
    selection-color: {t['sys_color_selection_text']};
}}

QCalendarWidget QAbstractItemView:disabled {{
    color: {t['sys_color_text_muted']};
}}

/* --- Scrollbars --- */
QScrollBar:vertical {{
    background-color: {t['sys_color_surface']};
    width: 10px;
    border: none;
    margin: 0px;
}}

QScrollBar::handle:vertical {{
    background-color: {t['sys_color_border']};
    min-height: 24px;
    border: none;
}}

QScrollBar::handle:vertical:hover {{
    background-color: {t['sys_color_primary']};
}}

QScrollBar:horizontal {{
    background-color: {t['sys_color_surface']};
    height: 10px;
    border: none;
}}

QScrollBar::handle:horizontal {{
    background-color: {t['sys_color_border']};
    min-width: 24px;
}}

QScrollBar::handle:horizontal:hover {{
    background-color: {t['sys_color_primary']};
}}

QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    width: 0;
    height: 0;
    background: none;
    border: none;
}}

/* --- Message boxes --- */
QMessageBox {{
    background-color: {t['sys_color_background']};
}}

QMessageBox QLabel {{
    color: {t['sys_color_text_main']};
    background-color: transparent;
}}

/* --- Menus / context menus --- */
QMenu {{
    background-color: {t['sys_color_surface_raised']};
    color: {t['sys_color_text_main']};
    border: 1px solid {t['sys_color_border']};
}}

QMenu::item {{
    padding: 4px 18px;
}}

QMenu::item:selected {{
    background-color: {t['sys_color_primary_hover']};
    color: {t['sys_color_text_highlight']};
}}

/* --- Tooltips --- */
QToolTip {{
    background-color: {t['sys_color_surface_raised']};
    color: {t['sys_color_text_main']};
    border: 1px solid {t['sys_color_border']};
    padding: 4px 6px;
}}
"""

