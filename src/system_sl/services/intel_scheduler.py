"""Background refresh scheduling for Daily Intel.

The feature uses the app's QTimer pattern, but runs its network work on daemon
Python threads rather than QThreads. Feed requests and LLM calls can outlive the
window; a daemon thread can be abandoned at process exit without Qt aborting on
a running QThread.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QTimer, Signal

from system_sl.intel.briefing import BriefingService, get_intel_service

CHECK_INTERVAL_MS = 10 * 60 * 1000


class _ThreadWorker(QObject):
    """QObject signal bridge around one daemon worker thread."""

    result = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.isRunning():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def isRunning(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def requestInterruption(self) -> None:
        self._stop.set()

    def isInterruptionRequested(self) -> bool:
        return self._stop.is_set()

    def wait(self, ms: int = -1) -> bool:
        thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        thread.join(None if ms < 0 else max(0, ms) / 1000.0)
        return not thread.is_alive()

    def _run(self) -> None:
        try:
            self._work()
        except Exception as exc:  # noqa: BLE001 - the UI needs a state, not a crash
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.finished.emit()

    def _work(self) -> None:
        raise NotImplementedError


class IntelRefreshWorker(_ThreadWorker):
    """Builds one briefing without touching the GUI thread."""

    def __init__(
        self,
        service: BriefingService,
        *,
        force: bool = False,
        allow_network: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.force = force
        self.allow_network = allow_network

    def _work(self) -> None:
        briefing = self.service.refresh(force=self.force, allow_network=self.allow_network)
        if not self.isInterruptionRequested():
            self.result.emit(briefing)


class IntelCollectWorker(_ThreadWorker):
    """Refreshes the article cache between daily briefings."""

    def __init__(self, service: BriefingService, parent=None) -> None:
        super().__init__(parent)
        self.service = service

    def _work(self) -> None:
        collected = bool(self.service.collect_if_due())
        if not self.isInterruptionRequested():
            self.result.emit(collected)


class DailyIntelScheduler(QObject):
    """Generates at most one briefing per local day, collecting feeds between."""

    briefingReady = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        service: BriefingService | None = None,
        parent: QObject | None = None,
        *,
        interval_ms: int = CHECK_INTERVAL_MS,
    ) -> None:
        super().__init__(parent)
        self.service = service or get_intel_service()
        self._worker: IntelRefreshWorker | None = None
        self._collect_worker: IntelCollectWorker | None = None

        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.check)
        self.timer.start()

    def is_running(self) -> bool:
        return bool(
            (self._worker is not None and self._worker.isRunning())
            or (self._collect_worker is not None and self._collect_worker.isRunning())
        )

    def check(self) -> None:
        """Start work only when enabled and something is actually due."""
        try:
            if not self.service.get_config().get("enabled", True):
                return
            if not self.service.has_briefing():
                self.start()
            elif self.service.fetch_due():
                self.start_collect()
        except Exception:
            return

    def start(self) -> None:
        if self.is_running():
            return
        worker = IntelRefreshWorker(self.service)
        worker.result.connect(self._on_result)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_finished)
        self._worker = worker
        worker.start()

    def start_collect(self) -> None:
        if self.is_running():
            return
        worker = IntelCollectWorker(self.service)
        worker.result.connect(self._on_collected)
        worker.finished.connect(self._on_collect_finished)
        self._collect_worker = worker
        worker.start()

    def stop(self) -> None:
        self.timer.stop()
        for worker in (self._worker, self._collect_worker):
            if worker is not None and worker.isRunning():
                # Network calls cannot be interrupted safely; give the worker
                # a short grace period. It is a daemon thread, so an unfinished
                # request cannot keep the process alive or crash Qt on exit.
                worker.requestInterruption()
                worker.wait(2000)

    def _on_result(self, briefing) -> None:
        self.briefingReady.emit(briefing)

    def _on_collected(self, _collected) -> None:
        pass

    def _on_failed(self, message: str) -> None:
        self.failed.emit(message)

    def _on_finished(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()

    def _on_collect_finished(self) -> None:
        worker = self._collect_worker
        self._collect_worker = None
        if worker is not None:
            worker.deleteLater()
