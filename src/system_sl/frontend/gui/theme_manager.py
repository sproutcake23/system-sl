"""Owns the active theme and keeps Dynamic mode in sync with the wallpaper.

The manager is the only place that changes the application stylesheet. It:

* resolves the saved mode and applies it before windows are built;
* follows OS light/dark changes in System mode;
* watches the wallpaper in Dynamic mode and refreshes wallust off the GUI
  thread, keeping the last good palette when detection fails;
* emits ``themeChanged`` so chat, onboarding, notifications and the tray can
  update without re-reading settings themselves.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from PySide6.QtCore import QFileSystemWatcher, QObject, QSettings, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication

from system_sl.frontend.gui.theme import (
    apply_theme,
    get_saved_theme_mode,
    get_tokens_for_mode,
    normalize_theme_mode,
    set_active_tokens,
)
from system_sl.utils.wallpaper_path import WallpaperState, get_wallpaper_state, watch_paths
from system_sl.utils.wallust_theme import (
    cached_wallust_tokens,
    find_wallust,
    generate_wallust_tokens,
    last_error,
    missing_wallust_message,
)

LOG = logging.getLogger(__name__)

WALLPAPER_POLL_MS = 12_000
SYSTEM_POLL_MS = 30_000
DEBOUNCE_MS = 450


class _ProbeThread(QThread):
    """Reads the active wallpaper off the GUI thread (it may call subprocesses)."""

    stateReady = Signal(object)

    def run(self):
        if self.isInterruptionRequested():
            return
        try:
            state = get_wallpaper_state()
        except Exception:
            LOG.exception("Could not detect the wallpaper")
            state = None
        if self.isInterruptionRequested():
            return
        self.stateReady.emit(state)


class _ExtractThread(QThread):
    """Runs wallust off the GUI thread."""

    tokensReady = Signal(str, object)

    def __init__(self, state: WallpaperState, parent=None):
        super().__init__(parent)
        self._state = state

    def run(self):
        if self.isInterruptionRequested():
            return
        try:
            tokens = generate_wallust_tokens(self._state.image_path or "")
        except Exception:
            LOG.exception("Could not generate wallust tokens")
            tokens = None
        if self.isInterruptionRequested():
            return
        self.tokensReady.emit(self._state.fingerprint, tokens)


class ThemeManager(QObject):
    themeChanged = Signal(dict)
    statusChanged = Signal(str)

    def __init__(self, app: QApplication):
        super().__init__()
        self._app = app
        self._settings = QSettings("system-sl", "AppConfig")
        self._mode = get_saved_theme_mode(self._settings)
        self._tokens = get_tokens_for_mode(self._mode)
        self._started = False
        self._status_message = ""
        self._last_good_tokens: Optional[dict] = None
        self._fingerprint: Optional[str] = None
        self._detected_path: str = ""

        self._probe_thread: Optional[_ProbeThread] = None
        self._extract_thread: Optional[_ExtractThread] = None
        self._probe_running = False
        self._extract_running = False
        self._pending_state: Optional[WallpaperState] = None

        self._timer = QTimer(self)
        self._timer.setInterval(WALLPAPER_POLL_MS)
        self._timer.timeout.connect(self._probe)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._probe)

        self._scheme_timer = QTimer(self)
        self._scheme_timer.setInterval(SYSTEM_POLL_MS)
        self._scheme_timer.timeout.connect(self._check_system_scheme)

        self._scheme_connected = False

        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_watch_event)
        self._watcher.directoryChanged.connect(self._on_watch_event)

        self._app.aboutToQuit.connect(self.shutdown)

    # ------------------------------------------------------------------
    # Lifetime
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Stop watchers and let in-flight detection finish before exiting.

        QThread.terminate() is deliberately not used: it kills the thread at an
        arbitrary point, which can land inside a subprocess call or a file
        write and leave the process in a broken state on exit. Instead the
        workers are asked to stop and given time to return on their own.
        """
        self._timer.stop()
        self._scheme_timer.stop()
        self._debounce.stop()
        for attribute in ("_probe_thread", "_extract_thread"):
            thread = getattr(self, attribute, None)
            if thread is None or not thread.isRunning():
                continue
            thread.requestInterruption()
            # The workers poll isInterruptionRequested() between steps, so
            # this returns promptly without killing anything mid-flight.
            if not thread.wait(5000):
                LOG.warning("%s did not stop in time; leaving it to exit", attribute)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def tokens(self) -> dict:
        return dict(self._tokens)

    @property
    def status_message(self) -> str:
        return self._status_message

    def _set_status(self, message: str) -> None:
        self._status_message = message
        self.statusChanged.emit(message)

    def start(self) -> None:
        """Apply the saved theme now. Safe to call more than once."""
        if self._started:
            if self._mode != get_saved_theme_mode(self._settings):
                self.set_mode(get_saved_theme_mode(self._settings), persist=False)
            return
        self._started = True
        self.set_mode(get_saved_theme_mode(self._settings), persist=False)

    def set_mode(self, mode: str, persist: bool = True) -> None:
        mode = normalize_theme_mode(mode)
        self._mode = mode

        if persist:
            self._settings.setValue("theme_mode", mode)
            self._settings.sync()

        self._stop_dynamic()
        self._scheme_timer.stop()

        if mode == "dynamic":
            self._start_dynamic()
        else:
            if mode == "system":
                self._connect_scheme_signal()
                self._scheme_timer.start()
            self._set_status("")
            self._apply(get_tokens_for_mode(mode))

    # ------------------------------------------------------------------
    # System mode
    # ------------------------------------------------------------------

    def _connect_scheme_signal(self) -> None:
        if self._scheme_connected:
            return
        try:
            hints = self._app.styleHints()
            signal = getattr(hints, "colorSchemeChanged", None)
            if signal is not None:
                signal.connect(self._on_scheme_changed)
                self._scheme_connected = True
        except Exception:
            LOG.debug("Could not connect to Qt's colorSchemeChanged", exc_info=True)

    def _on_scheme_changed(self, *_args) -> None:
        self._check_system_scheme()

    def _check_system_scheme(self) -> None:
        if self._mode != "system":
            return
        tokens = get_tokens_for_mode("system")
        if tokens != self._tokens:
            self._apply(tokens)

    # ------------------------------------------------------------------
    # Dynamic mode
    # ------------------------------------------------------------------

    def _start_dynamic(self) -> None:
        if not find_wallust():
            LOG.warning(missing_wallust_message())
            self._set_status(missing_wallust_message())
            self._apply(dict(self._builtin_tokens()))
            return

        self._set_status("")
        # Paint the last palette immediately; the probe refreshes it if needed.
        cached = cached_wallust_tokens()
        self._last_good_tokens = cached
        self._fingerprint = None
        self._apply(cached)

        self._refresh_watch_paths()
        self._timer.start()
        self._probe()

    def _builtin_tokens(self) -> dict:
        from system_sl.utils.theme_gen import DEFAULT_TOKENS

        return DEFAULT_TOKENS

    def _stop_dynamic(self) -> None:
        self._timer.stop()
        self._debounce.stop()
        self._pending_state = None
        if self._watcher.files() or self._watcher.directories():
            paths = self._watcher.files() + self._watcher.directories()
            if paths:
                self._watcher.removePaths(paths)

    def _probe(self) -> None:
        if self._mode != "dynamic" or self._probe_running:
            return
        self._probe_running = True
        thread = _ProbeThread(self)
        self._probe_thread = thread
        thread.stateReady.connect(self._on_state)
        thread.finished.connect(lambda t=thread: self._on_probe_finished(t))
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_probe_finished(self, thread: _ProbeThread) -> None:
        if self._probe_thread is thread:
            self._probe_thread = None

    def _on_state(self, state: Optional[WallpaperState]) -> None:
        self._probe_running = False
        if self._mode != "dynamic":
            return

        extra = []
        if state is not None and state.image_path:
            extra.append(state.image_path)
        self._refresh_watch_paths(extra)

        # A transient detection failure must not wipe a good palette, but it
        # must not be silent either: the status line is the only place the
        # player can learn that detection failed.
        if state is None or not state.ok:
            error = state.error if state is not None else None
            self._set_status(error or "Could not detect the wallpaper.")
            return
        self._detected_path = state.image_path or ""
        if state.fingerprint == self._fingerprint and self._last_good_tokens is not None:
            return

        if self._extract_running:
            self._pending_state = state
            return
        self._start_extract(state)

    def _start_extract(self, state: WallpaperState) -> None:
        self._extract_running = True
        thread = _ExtractThread(state, self)
        self._extract_thread = thread
        thread.tokensReady.connect(self._on_tokens)
        thread.finished.connect(lambda t=thread: self._on_extract_finished(t))
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_extract_finished(self, thread: _ExtractThread) -> None:
        if self._extract_thread is thread:
            self._extract_thread = None

    def _on_tokens(self, fingerprint: str, tokens: Optional[dict]) -> None:
        self._extract_running = False
        if self._mode != "dynamic":
            return

        if tokens:
            self._fingerprint = fingerprint
            self._last_good_tokens = tokens
            self._apply(tokens)
            self._set_status(last_error() or self._wallpaper_status())
        else:
            # A crash in the worker: keep the last good palette but remember the
            # wallpaper so the same failure is not retried every poll.
            self._fingerprint = fingerprint
            self._set_status(
                "Could not refresh the wallpaper palette; keeping the previous colours."
            )

        pending = self._pending_state
        self._pending_state = None
        if pending is not None and pending.fingerprint != fingerprint:
            self._start_extract(pending)

    def _wallpaper_status(self) -> str:
        """Describe the wallpaper the palette was taken from.

        Without this the Settings panel shows a blank status in Dynamic mode,
        which is indistinguishable from Dark mode and leaves the player
        guessing which wallpaper was picked up.
        """
        path = self._detected_path
        if not path:
            return ""
        return f"Using wallpaper: {path}"

    def _on_watch_event(self, _path: str) -> None:
        if self._mode == "dynamic":
            # Config files are often written non-atomically; wait for the write
            # to settle before probing.
            self._debounce.start()

    def _refresh_watch_paths(self, extra=()) -> None:
        desired = set(watch_paths())
        for path in extra:
            if path and os.path.exists(path):
                desired.add(path)

        current = set(self._watcher.files()) | set(self._watcher.directories())
        for path in current - desired:
            self._watcher.removePath(path)
        for path in desired - current:
            if os.path.exists(path):
                self._watcher.addPath(path)

    # ------------------------------------------------------------------
    # Applying colours
    # ------------------------------------------------------------------

    def _apply(self, tokens: dict) -> None:
        self._tokens = dict(tokens)
        set_active_tokens(self._tokens)
        try:
            apply_theme(self._app, self._tokens)
        except Exception:
            LOG.exception("Could not apply the theme")
        self.themeChanged.emit(dict(self._tokens))


_manager: Optional[ThemeManager] = None


def get_theme_manager(app: Optional[QApplication] = None) -> ThemeManager:
    """Return the process-wide :class:`ThemeManager`, creating it if needed."""
    global _manager
    if _manager is None:
        application = app or QApplication.instance()
        if application is None:
            raise RuntimeError("A QApplication is required before creating the theme manager")
        _manager = ThemeManager(application)
    return _manager
