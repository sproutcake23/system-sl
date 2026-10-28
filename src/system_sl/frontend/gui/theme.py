from system_sl.utils.theme_gen import DEFAULT_TOKENS

def get_stylesheet(t=DEFAULT_TOKENS):
    """Generates the QSS string dynamically based on the provided token dictionary."""
    return f"""
/* === Solo Leveling System Dynamic Theme === */

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
    color: {t['sys_color_primary_light']};
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
    background-color: rgba(255, 255, 255, 0.02);
    color: {t['sys_color_text_muted']};
    border: 1px solid {t['sys_color_border_muted']};
}}

/* --- Checkbox --- */
QCheckBox {{
    color: {t['sys_color_text_secondary']};
    spacing: 8px;
    background-color: transparent;
}}

QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {t['sys_color_border']};
    background-color: {t['sys_color_surface']};
}}

QCheckBox::indicator:hover {{
    border: 1px solid {t['sys_color_primary']};
}}

QCheckBox::indicator:checked {{
    background-color: {t['sys_color_primary']};
    border: 1px solid {t['sys_color_primary']};
}}

/* --- Text inputs --- */
QLineEdit, QDateEdit, QTextEdit, QComboBox, QSpinBox, QPlainTextEdit {{
    background-color: {t['sys_color_surface_raised']};
    color: {t['sys_color_text_main']};
    border: 1px solid {t['sys_color_border']};
    border-radius: 0px;
    padding: 4px 6px;
    selection-background-color: {t['sys_color_border']};
    selection-color: #ffffff;
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
    border-top: 5px solid {t['sys_color_primary_light']};
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
    selection-color: #ffffff;
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
    color: {t['sys_color_text_highlight']};
    border: 1px solid {t['sys_color_primary']};
    padding: 4px 6px;
}}
"""

# --- BACKWARD COMPATIBILITY ---
# Any other file still doing `from system_sl.frontend.gui.theme import SOLO_LEVELING_QSS` 
# will gracefully receive the default Solo Leveling theme generated by your new function!
SOLO_LEVELING_QSS = get_stylesheet(DEFAULT_TOKENS)