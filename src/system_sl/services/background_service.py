"""Module for managing the background service orchestration engine."""

import logging

from PySide6.QtCore import QLockFile, QObject, QThread, QTimer, Signal
from system_sl.utils import SystemNotification
from system_sl.core.tasks import get_random_task, get_topn_task
from system_sl.core.priority_engine_new import run_prioritization
from system_sl.utils.paths import get_tasks_file_path
from system_sl.utils.notification_settings import load_notifications_enabled
from system_sl.utils.worker_threads import new_worker_thread


log = logging.getLogger(__name__)


class _NotificationWorker(QObject):
    """Ranks tasks off the event loop, then reports what to show."""

    finished = Signal(list)
    failed = Signal(str)

    def run(self) -> None:
        try:
            run_prioritization(display=False)
            titles = get_topn_task()
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")
            return
        self.finished.emit(titles)


class BackgroundServiceController(QObject):
    """Orchestrates the background monitoring lifecycle and links business logic to the view layer."""

    def __init__(self, view: SystemNotification) -> None:
        """Configures the automation timer loop and stores the targeted view window interface.

        Args:
            view (SystemNotification): The UI window element instance to manipulate.
        """

        super().__init__()
        self.view: SystemNotification = view

        # Only one notifier may run per user. The autostart --bg process and
        # the main window each start one, and with close-to-tray both stay
        # alive, so every notification used to arrive twice. Whichever
        # process holds the lock notifies; the other takes over if it exits.
        self.lock = QLockFile(get_tasks_file_path("notifier.lock"))
        # A held lock is stale only when its process is gone, never by age.
        self.lock.setStaleLockTime(0)

        self._worker_thread: QThread | None = None
        self._worker: _NotificationWorker | None = None
        self._stopped = False

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_and_render_task)
        self.timer.start(3600000)


    def poll_and_render_task(self) -> None:
        """Queries core task modules and passes data changes directly to the UI panel."""
        if self._stopped:
            return
        # The preference can change while this process is alive (the GUI
        # writes it), so re-read it on every tick rather than only at startup.
        # Otherwise a notifier started at login would keep interrupting a user
        # who has since switched reminders off.
        if not load_notifications_enabled():
            return
        if not self.lock.isLocked() and not self.lock.tryLock(0):
            return
        if self._worker_thread is not None:
            # The previous tick is still ranking; do not stack workers.
            return
        self._start_worker()

    def _start_worker(self) -> None:
        """Run ranking and lookup in a worker so the event loop stays free.

        The thread has no Qt parent and is kept alive by ``worker_threads``
        until the pass ends, so this controller can be stopped and deleted
        mid-pass without destroying a running QThread.
        """
        self._worker = _NotificationWorker()
        self._worker_thread = new_worker_thread(self._worker)
        self._worker.finished.connect(self._on_tasks_ready)
        self._worker.failed.connect(self._on_ranking_failed)
        self._worker_thread.finished.connect(self._cleanup_worker)
        self._worker_thread.start()

    def _on_tasks_ready(self, titles: list) -> None:
        if self._stopped:
            return  # stopped mid-pass: the result is no longer wanted
        if len(titles) > 0:
            self.view.display_message(titles)

    def _on_ranking_failed(self, message: str) -> None:
        # The next tick retries. A notifier must not crash over a bad read.
        log.warning("Ranking failed: %s", message)

    def _cleanup_worker(self) -> None:
        # The thread and worker are released by worker_threads once finished.
        self._worker = None
        self._worker_thread = None

    def is_running(self) -> bool:
        """True while a ranking pass started by this controller is running."""
        return self._worker_thread is not None and self._worker_thread.isRunning()

    def stop(self) -> None:
        """Stop the reminder loop and release the lock for another notifier.

        Returns immediately. A ranking pass is one blocking call that cannot be
        interrupted, and waiting for it here froze the GUI until it finished.
        Instead the pass finishes in the background and its result is
        discarded; ``worker_threads`` keeps the thread alive until then, and
        the application's quit path waits for it.
        """
        self._stopped = True
        self.timer.stop()
        if self._worker is not None:
            try:
                self._worker.finished.disconnect(self._on_tasks_ready)
            except (RuntimeError, TypeError):
                pass
        if self.lock.isLocked():
            self.lock.unlock()
