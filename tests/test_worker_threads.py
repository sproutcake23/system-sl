"""Worker threads: never destroyed while running, never block the GUI to stop."""

import threading
import time

import pytest
from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication

from system_sl.utils import worker_threads


@pytest.fixture
def qapp(tmp_path, monkeypatch):
    # Keep any data-file access inside the test directory.
    monkeypatch.setenv("HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    yield app
    assert worker_threads.wait_for_workers(5000)
    _pump(lambda: not worker_threads._live, 2.0)


def _pump(cond, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        QApplication.processEvents()
        # processEvents() alone never runs deleteLater() outside a loop.
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class _BlockingWorker(QObject):
    finished = Signal(object)

    def __init__(self, gate: threading.Event):
        super().__init__()
        self.gate = gate

    def run(self):
        self.gate.wait(10)
        self.finished.emit("done")


def test_tracked_thread_survives_owner_and_is_released(qapp):
    gate = threading.Event()
    received = []

    owner = QObject()
    worker = _BlockingWorker(gate)
    thread = worker_threads.new_worker_thread(worker)

    class Receiver(QObject):
        def on_done(self, value):
            received.append(value)

    receiver = Receiver(owner)
    worker.finished.connect(receiver.on_done)
    thread.start()
    del worker, thread

    # The owner (think: a window) goes away while the pass is running.
    owner.deleteLater()
    _pump(lambda: False, 0.05)
    assert worker_threads.running_count() == 1

    # A bounded wait returns without the pass finishing.
    assert worker_threads.wait_for_workers(50) is False

    gate.set()
    assert worker_threads.wait_for_workers(5000)
    assert _pump(lambda: not worker_threads._live, 2.0)
    _pump(lambda: False, 0.05)
    assert received == [], "result for a deleted receiver must be dropped"


def test_wait_without_event_loop_does_not_deadlock(qapp):
    """The exit path blocks the GUI thread; the thread must still end."""
    gate = threading.Event()
    thread = worker_threads.new_worker_thread(_BlockingWorker(gate))
    thread.start()
    gate.set()
    # No processEvents here: nothing can deliver a queued quit().
    assert worker_threads.wait_for_workers(5000)


def test_tasks_window_can_be_deleted_mid_rank(qapp, monkeypatch):
    from system_sl.frontend.gui import popup_windows

    gate = threading.Event()

    def slow_ranking(display=False):
        gate.wait(10)
        return {"all_tasks_ranked": []}

    rendered = []
    monkeypatch.setattr(popup_windows, "run_prioritization", slow_ranking)
    monkeypatch.setattr(
        popup_windows.TasksWindow, "_render_ranking", lambda self, r: rendered.append(r)
    )

    window = popup_windows.TasksWindow()
    assert window.is_ranking()
    # Bounded: does not wait for the pass to end.
    started = time.monotonic()
    assert window.shutdown(timeout_ms=20) is False
    assert time.monotonic() - started < 2

    window.deleteLater()
    _pump(lambda: False, 0.05)

    gate.set()
    assert worker_threads.wait_for_workers(5000)
    _pump(lambda: not worker_threads._live, 2.0)
    assert rendered == []


def test_tasks_window_shutdown_ignores_late_result(qapp, monkeypatch):
    from system_sl.frontend.gui import popup_windows

    gate = threading.Event()
    monkeypatch.setattr(
        popup_windows,
        "run_prioritization",
        lambda display=False: (gate.wait(10), {"all_tasks_ranked": []})[1],
    )
    rendered = []
    monkeypatch.setattr(
        popup_windows.TasksWindow, "_render_ranking", lambda self, r: rendered.append(r)
    )

    window = popup_windows.TasksWindow()
    window.shutdown()
    gate.set()
    assert worker_threads.wait_for_workers(5000)
    assert _pump(lambda: not window.is_ranking() and window._ranking_thread is None, 2.0)
    assert rendered == []
    window.deleteLater()
