"""Module for managing the background service orchestration engine."""

import logging

from PySide6.QtCore import QLockFile, QObject, QThread, QTimer, Signal
from system_sl.utils import SystemNotification
from system_sl.core.tasks import get_random_task, get_topn_task
from system_sl.core.priority_engine_new import run_prioritization
from system_sl.utils.paths import get_tasks_file_path
from system_sl.utils.notification_settings import load_notifications_enabled


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

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_and_render_task)
        self.timer.start(3600000)


    def poll_and_render_task(self) -> None:
        """Queries core task modules and passes data changes directly to the UI panel."""
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
        """Run ranking and lookup in a worker so the event loop stays free."""
        self._worker_thread = QThread(self)
        self._worker = _NotificationWorker()
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_tasks_ready)
        self._worker.failed.connect(self._on_ranking_failed)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.failed.connect(self._worker_thread.quit)
        self._worker_thread.finished.connect(self._cleanup_worker)
        self._worker_thread.start()

    def _on_tasks_ready(self, titles: list) -> None:
        if len(titles) > 0:
            self.view.display_message(titles)

    def _on_ranking_failed(self, message: str) -> None:
        # The next tick retries. A notifier must not crash over a bad read.
        log.warning("Ranking failed: %s", message)

    def _cleanup_worker(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        if self._worker_thread is not None:
            self._worker_thread.deleteLater()
            self._worker_thread = None

    def stop(self) -> None:
        """Stop the reminder loop and release the lock for another notifier."""
        self.timer.stop()
        if self._worker_thread is not None:
            self._worker_thread.quit()
            self._worker_thread.wait()
        if self.lock.isLocked():
            self.lock.unlock()
