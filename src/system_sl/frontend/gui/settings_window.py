"""The app-wide Settings window: general options and LLM settings, in tabs."""

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from system_sl.frontend.gui.chat_panel import LLMSettingsPage


class AppSettingsDialog(QDialog):
    """General options apply as they change; the LLM tab has its own OK/Apply."""

    GENERAL_TAB = 0
    LLM_TAB = 1

    llm_saved = Signal()

    def __init__(self, general_page: QWidget, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(700, 550)

        general_tab = QWidget()
        general_layout = QVBoxLayout(general_tab)
        general_layout.addWidget(general_page, stretch=1)
        close_box = QDialogButtonBox(QDialogButtonBox.Close)
        close_box.rejected.connect(self.reject)
        general_layout.addWidget(close_box)

        self.llm_page = LLMSettingsPage()
        self.llm_page.saved.connect(self.llm_saved)
        self.llm_page.finished.connect(lambda ok: self.accept() if ok else self.reject())

        self.tabs = QTabWidget()
        self.tabs.addTab(general_tab, "General")
        self.tabs.addTab(self.llm_page, "LLM")

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)

    def show_tab(self, index: int) -> None:
        """Run the window on the given tab, with the LLM form freshly loaded."""
        self.llm_page.reload()
        self.tabs.setCurrentIndex(index)
        self.exec()

    def set_llm_available(self, available: bool) -> None:
        """Saving reloads the agent, so the LLM tab must wait out a running reply."""
        self.tabs.setTabEnabled(self.LLM_TAB, available)
        self.tabs.setTabToolTip(
            self.LLM_TAB, "" if available else "Available once the reply finishes"
        )

    def done(self, result: int) -> None:
        """Abandon the LLM page's background work however the dialog closes.

        Close, Esc, accept, reject and the window's X all end here; a dialog
        closed that way does not get a closeEvent.
        """
        self.llm_page.shutdown_threads()
        super().done(result)
