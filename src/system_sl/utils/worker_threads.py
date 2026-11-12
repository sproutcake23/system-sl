"""Keep worker QThreads alive until they finish, and wait for them at exit.

A ranking pass is one blocking call, so its thread cannot be interrupted.
When a thread was parented to a window or controller, deleting that owner
(or tearing the app down at exit) mid-pass destroyed a running QThread,
which aborts the process with "QThread: Destroyed while thread is still
running". Threads created with :func:`new_worker_thread` have no Qt parent and
are referenced here until they have really finished, so their owners can be
closed or deleted at any time; a result arriving after that is dropped
because Qt disconnects signals to destroyed receivers.
"""

import logging
import time

from PySide6.QtCore import QObject, QThread, Slot


log = logging.getLogger(__name__)

# Running thread -> its worker. Holding both keeps them alive until finished.
_live: dict[QThread, QObject] = {}


class _Releaser(QObject):
    """Receives ``QThread.finished`` on the thread that started the worker.

    ``finished`` is emitted from the worker thread itself; a QObject receiver
    makes the connection queued, so the release (which waits on the thread)
    runs on the GUI thread and never on the thread being waited for.
    """

    @Slot()
    def on_finished(self) -> None:
        thread = self.sender()
        if isinstance(thread, QThread):
            _release(thread)


_releaser: _Releaser | None = None


def _get_releaser() -> _Releaser:
    global _releaser
    if _releaser is None:
        _releaser = _Releaser()
    return _releaser


class _WorkerThread(QThread):
    """Runs one blocking ``worker.run()`` call, with no event loop.

    Without an event loop the thread ends as soon as ``run`` returns, so it
    never depends on a queued ``quit()`` reaching it. That matters at exit,
    where the GUI thread blocks in :func:`wait_for_workers` and could not
    deliver one. The worker keeps its GUI-thread affinity; the signals it
    emits from here reach GUI receivers as queued calls.
    """

    def __init__(self, worker: QObject) -> None:
        super().__init__()
        self._worker = worker

    def run(self) -> None:
        self._worker.run()


def new_worker_thread(worker: QObject) -> QThread:
    """Return an unstarted, tracked thread that calls ``worker.run()`` once.

    The caller connects the worker's result signals, then calls
    ``thread.start()``. The thread and worker stay referenced here until the
    thread has finished, then are released; callers must not delete them.
    """
    thread = _WorkerThread(worker)
    _live[thread] = worker
    thread.finished.connect(_get_releaser().on_finished)
    return thread


def _release(thread: QThread) -> None:
    """Drop a finished thread and its worker."""
    if _live.pop(thread, None) is None:
        return
    # finished() is emitted just before the thread exits; make sure it is
    # really gone before the last reference can delete the object. Both are
    # owned by Python, so dropping the references frees them.
    thread.wait()


def running_count() -> int:
    """How many tracked threads have not been released yet."""
    return sum(1 for t in _live if t.isRunning())


def wait_for_workers(timeout_ms: int | None = None) -> bool:
    """Block until every tracked thread finishes, or ``timeout_ms`` passes.

    Returns True when no tracked thread is still running. ``None`` waits
    without a limit.
    """
    deadline = None if timeout_ms is None else time.monotonic() + timeout_ms / 1000
    for thread in list(_live):
        if deadline is None:
            thread.wait()
            continue
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0 or not thread.wait(remaining):
            break
    return running_count() == 0


def shutdown_workers(grace_ms: int = 3000) -> None:
    """Exit-path helper: wait for running workers so none is destroyed live.

    Called when the application is quitting, after its windows are hidden, so
    the wait is not a visible freeze. After ``grace_ms`` it logs and keeps
    waiting: destroying a running QThread would abort the process, which is
    worse than finishing a ranking pass nobody sees.
    """
    if wait_for_workers(grace_ms):
        return
    log.warning(
        "Waiting for %d background task(s) to finish before exiting",
        running_count(),
    )
    wait_for_workers(None)
