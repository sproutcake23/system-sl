"""Module for managing the background service orchestration engine."""

from PySide6.QtCore import QLockFile, QObject, QTimer
from system_sl.utils import SystemNotification
from system_sl.core.tasks import get_random_task, get_topn_task
from system_sl.core.priority_engine_new import run_prioritization
from system_sl.utils.paths import get_tasks_file_path
from system_sl.utils.notification_settings import load_notifications_enabled

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
        run_prioritization(display=False)
        ntask = get_topn_task()
        if len(ntask) > 0:
            self.view.display_message(ntask)

    def stop(self) -> None:
        """Stop the reminder loop and release the lock for another notifier."""
        self.timer.stop()
        if self.lock.isLocked():
            self.lock.unlock()
