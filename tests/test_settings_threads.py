"""Closing the settings dialog must neither block nor destroy a running thread."""

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QObject, QThread, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return QApplication.instance() or QApplication([])


class _BlockingWorker(QObject):
    """Stands in for an HTTP call that ignores interruption requests."""

    finished = Signal(list)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, release: threading.Event):
        super().__init__()
        self.release = release

    def run(self):
        self.release.wait(5)
        self.finished.emit([])


def _pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def test_done_retires_a_blocked_fetch_without_waiting(app):
    from system_sl.frontend.gui import chat_panel as cp
    from system_sl.frontend.gui.settings_window import AppSettingsDialog
    from PySide6.QtWidgets import QWidget

    dialog = AppSettingsDialog(QWidget())
    page = dialog.llm_page

    release = threading.Event()
    thread = QThread(page)
    worker = _BlockingWorker(release)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(page._on_models_fetched)
    worker.finished.connect(thread.quit)
    page._fetch_thread, page._fetch_worker = thread, worker
    thread.start()

    fetched = []
    page._populate_model_table = lambda: fetched.append(True)

    started = time.monotonic()
    dialog.reject()  # the Close button / Esc path, which gets no closeEvent
    assert time.monotonic() - started < 1.0, "closing waited for the request"
    assert page._fetch_thread is None
    assert thread in cp._retired_threads
    assert thread.parent() is None

    # Destroying the page while the request is still running must be safe.
    dialog.deleteLater()
    _pump(app, 0.1)

    release.set()
    _pump(app, 0.5)
    assert thread not in cp._retired_threads
    assert fetched == [], "a late result from an abandoned fetch was applied"
