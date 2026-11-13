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
import time
from typing import Optional

from PySide6.QtCore import QFileSystemWatcher, QObject, QSettings, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication

from system_sl.frontend.gui.theme import (
    apply_theme,
    get_saved_theme_mode,
    get_tokens_for_mode,
    migrate_saved_theme_mode,
    normalize_theme_mode,
    set_active_tokens,
)
from system_sl.utils.system_theme import qt_color_scheme
from system_sl.utils.wallpaper_path import (
    WallpaperState,
    get_wallpaper_state,
    state_for_image,
    watch_paths,
)
from system_sl.utils.wallust_theme import (
    cancel_extractions,
    ExtractionResult,
    cached_tokens_for,
    cached_wallust_tokens_or_none,
    extract_wallust_tokens,
    find_wallust,
    missing_wallust_message,
)

LOG = logging.getLogger(__name__)

# The file watcher catches most wallpaper changes at once; the poll is only a
# fallback for desktops that change it without touching a watched file. Each
# poll may run a subprocess, so it is kept slow.
WALLPAPER_POLL_MS = 60_000
SYSTEM_POLL_MS = 30_000
# How often to look for wallust after it was found missing, so installing it
# takes effect without restarting the app.
WALLUST_RECHECK_MS = 60_000
READING_STATUS = "Reading colours from your wallpaper…"
# QSettings key for a wallpaper the player picked by hand in Settings.
WALLPAPER_OVERRIDE_KEY = "dynamic_wallpaper_override"
DEBOUNCE_MS = 450
# Waits before re-trying a wallpaper whose extraction failed: 15s, 30s, 60s...
# up to 10 minutes. A transient failure (a timeout, a file mid-write) recovers
# quickly; a wallpaper wallust can never read is not hammered every poll.
RETRY_BASE_MS = 15_000
RETRY_MAX_MS = 600_000


def retry_delay_ms(failures: int) -> int:
    """Return the wait after ``failures`` failed extractions in a row."""
    if failures < 1:
        return 0
    return min(RETRY_BASE_MS * 2 ** (failures - 1), RETRY_MAX_MS)


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


class _SchemeThread(QThread):
    """Works out the OS light/dark scheme when Qt can't (may run gsettings)."""

    tokensReady = Signal(object)

    def run(self):
        try:
            tokens = get_tokens_for_mode("system")
        except Exception:
            LOG.exception("Could not detect the system colour scheme")
            tokens = None
        if not self.isInterruptionRequested():
            self.tokensReady.emit(tokens)


class _ExtractThread(QThread):
    """Runs wallust off the GUI thread."""

    # (fingerprint, ExtractionResult)
    resultReady = Signal(str, object)

    def __init__(self, state: WallpaperState, parent=None):
        super().__init__(parent)
        self._state = state

    def run(self):
        if self.isInterruptionRequested():
            return
        try:
            result = extract_wallust_tokens(self._state.image_path or "")
        except Exception as exc:
            LOG.exception("Could not generate wallust tokens")
            result = ExtractionResult(error=f"Palette extraction crashed: {exc}")
        if self.isInterruptionRequested():
            return
        self.resultReady.emit(self._state.fingerprint, result)


class ThemeManager(QObject):
    themeChanged = Signal(dict)
    statusChanged = Signal(str)
    modeChanged = Signal(str)
    overrideChanged = Signal(str)  # "" when the detected wallpaper is used

    def __init__(self, app: QApplication):
        super().__init__()
        self._app = app
        self._settings = QSettings("system-sl", "AppConfig")
        try:
            migrate_saved_theme_mode(self._settings)
        except Exception:
            LOG.warning("Could not migrate the saved theme mode", exc_info=True)
        self._mode = get_saved_theme_mode(self._settings)
        self._tokens = get_tokens_for_mode(self._mode)
        self._started = False
        self._status_message = ""
        self._last_good_tokens: Optional[dict] = None
        self._fingerprint: Optional[str] = None
        self._detected_path: str = ""
        # The status the last extraction reported, restored when detection
        # recovers after a failed poll.
        self._palette_status: str = ""

        self._probe_thread: Optional[_ProbeThread] = None
        self._extract_thread: Optional[_ExtractThread] = None
        self._probe_running = False
        self._extract_running = False
        self._pending_state: Optional[WallpaperState] = None
        # The wallpaper whose extraction last failed, how many times in a row,
        # and when it may be tried again (time.monotonic()).
        self._failed_fingerprint: Optional[str] = None
        self._failures = 0
        self._retry_at = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(WALLPAPER_POLL_MS)
        self._timer.timeout.connect(self._probe)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._probe)

        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._probe)

        self._scheme_timer = QTimer(self)
        self._scheme_timer.setInterval(SYSTEM_POLL_MS)
        self._scheme_timer.timeout.connect(self._check_system_scheme)

        self._scheme_connected = False
        self._scheme_thread: Optional[_SchemeThread] = None

        self._wallust_timer = QTimer(self)
        self._wallust_timer.setInterval(WALLUST_RECHECK_MS)
        self._wallust_timer.timeout.connect(self._recheck_wallust)

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
        workers are asked to stop, any running wallust process is killed so
        the extractor is not left waiting on it, and the threads return on
        their own.
        """
        self._timer.stop()
        self._scheme_timer.stop()
        self._debounce.stop()
        self._retry_timer.stop()
        self._wallust_timer.stop()
        cancel_extractions()
        for attribute in ("_probe_thread", "_extract_thread", "_scheme_thread"):
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

    @property
    def wallpaper_override(self) -> str:
        """The image picked by hand in Settings, or "" to use detection."""
        value = self._settings.value(WALLPAPER_OVERRIDE_KEY, "", type=str)
        return value or ""

    def set_wallpaper_override(self, path: str | None) -> None:
        """Use ``path`` for Dynamic colours instead of the detected wallpaper.

        ``None`` or "" goes back to detection. Useful on desktops where the
        wallpaper can't be detected, or to try a palette from another image.
        """
        path = path or ""
        if path:
            self._settings.setValue(WALLPAPER_OVERRIDE_KEY, path)
        else:
            self._settings.remove(WALLPAPER_OVERRIDE_KEY)
        self._settings.sync()
        self.overrideChanged.emit(path)
        self.refresh_wallpaper()

    def refresh_wallpaper(self) -> None:
        """Look at the wallpaper again now, forgetting any failure backoff."""
        if self._mode != "dynamic":
            return
        self._fingerprint = None
        self._failed_fingerprint = None
        self._failures = 0
        self._retry_timer.stop()
        self._probe()

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
        self.modeChanged.emit(mode)

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
        if qt_color_scheme() is not None:
            # Qt knows the scheme: answering is cheap, stay on this thread.
            self._on_scheme_tokens(get_tokens_for_mode("system"))
            return
        # Qt reports Unknown, so detection falls back to probes that may run
        # gsettings; keep those off the GUI thread.
        if self._scheme_thread is not None:
            return
        thread = _SchemeThread(self)
        self._scheme_thread = thread
        thread.tokensReady.connect(self._on_scheme_tokens)
        thread.finished.connect(lambda t=thread: self._on_scheme_finished(t))
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_scheme_finished(self, thread: _SchemeThread) -> None:
        if self._scheme_thread is thread:
            self._scheme_thread = None

    def _on_scheme_tokens(self, tokens: Optional[dict]) -> None:
        if self._mode != "system" or not tokens:
            return
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
            # Keep looking, so installing wallust works without a restart.
            self._wallust_timer.start()
            return

        self._fingerprint = None
        cached = cached_wallust_tokens_or_none()
        self._last_good_tokens = cached
        if cached is not None:
            # Paint the last palette immediately; the probe refreshes it only
            # if the wallpaper has changed since.
            self._set_status("")
            self._apply(cached)
        else:
            # Nothing cached yet: keep the colours already on screen rather
            # than flashing the built-in palette, and say what is happening.
            self._set_status(READING_STATUS)
            self._apply(self._tokens)

        self._refresh_watch_paths()
        self._timer.start()
        self._probe()

    def _builtin_tokens(self) -> dict:
        from system_sl.utils.theme_gen import DEFAULT_TOKENS

        return DEFAULT_TOKENS

    def _recheck_wallust(self) -> None:
        if self._mode != "dynamic":
            self._wallust_timer.stop()
            return
        if find_wallust():
            self._wallust_timer.stop()
            self._start_dynamic()

    def _stop_dynamic(self) -> None:
        self._timer.stop()
        self._debounce.stop()
        self._retry_timer.stop()
        self._wallust_timer.stop()
        self._pending_state = None
        self._failed_fingerprint = None
        self._failures = 0
        if self._watcher.files() or self._watcher.directories():
            paths = self._watcher.files() + self._watcher.directories()
            if paths:
                self._watcher.removePaths(paths)

    def _probe(self) -> None:
        if self._mode != "dynamic" or self._probe_running:
            return
        override = self.wallpaper_override
        if override:
            # A chosen file needs only a stat, not the desktop probes.
            self._probe_running = True
            self._on_state(state_for_image(override))
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
            # Same wallpaper, palette already applied. Detection may have
            # failed on an earlier poll, so put back the status the last
            # extraction reported instead of leaving that error showing.
            self._set_status(self._palette_status)
            return
        if (
            state.fingerprint == self._failed_fingerprint
            # Half a second of slack: the retry timer can fire a little early.
            and time.monotonic() + 0.5 < self._retry_at
        ):
            # This wallpaper failed recently; the retry timer will try again.
            self._set_status(self._palette_status)
            return

        cached = cached_tokens_for(state.image_path or "")
        if cached is not None:
            # The disk cache was made from this exact image: use it without
            # starting wallust, and only restyle if the colours differ.
            self._fingerprint = state.fingerprint
            self._last_good_tokens = cached
            self._failed_fingerprint = None
            self._failures = 0
            if cached != self._tokens:
                self._apply(cached)
            self._palette_status = ""
            self._set_status(self._palette_status)
            return

        if self._extract_running:
            self._pending_state = state
            return
        self._start_extract(state)

    def _start_extract(self, state: WallpaperState) -> None:
        self._extract_running = True
        if self._last_good_tokens is None:
            self._set_status(READING_STATUS)
        thread = _ExtractThread(state, self)
        self._extract_thread = thread
        thread.resultReady.connect(self._on_result)
        thread.finished.connect(lambda t=thread: self._on_extract_finished(t))
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_extract_finished(self, thread: _ExtractThread) -> None:
        if self._extract_thread is thread:
            self._extract_thread = None

    def _on_result(self, fingerprint: str, result: ExtractionResult) -> None:
        self._extract_running = False
        if self._mode != "dynamic" or result.cancelled:
            return

        if result.ok:
            # Only a real palette is committed: the fingerprint, the last good
            # tokens and the colours on screen all move together.
            self._fingerprint = fingerprint
            self._last_good_tokens = result.tokens
            self._failed_fingerprint = None
            self._failures = 0
            self._retry_timer.stop()
            self._apply(result.tokens)
            # Success is shown by the preview caption, not the status line,
            # which is kept for problems and progress.
            self._palette_status = ""
        else:
            # Keep whatever is on screen and try this wallpaper again later.
            # Committing the fingerprint here used to pin the built-in navy
            # palette until the wallpaper changed or the app restarted.
            if fingerprint == self._failed_fingerprint:
                self._failures += 1
            else:
                self._failed_fingerprint = fingerprint
                self._failures = 1
            delay = retry_delay_ms(self._failures)
            self._retry_at = time.monotonic() + delay / 1000
            self._retry_timer.start(delay)
            reason = result.error or "Could not refresh the wallpaper palette."
            self._palette_status = (
                f"{reason} Keeping the current colours; "
                f"trying again in {_describe_delay(delay)}."
            )
        self._set_status(self._palette_status)

        pending = self._pending_state
        self._pending_state = None
        if pending is not None and pending.fingerprint != fingerprint:
            self._start_extract(pending)

    @property
    def palette_source(self) -> str:
        """Describe the wallpaper the Dynamic palette was taken from, or ""."""
        path = self._detected_path
        if not path:
            return ""
        if self.wallpaper_override:
            return f"Using your chosen wallpaper: {path}"
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
                # Editors and wallpaper tools often replace the file (write a
                # new one, rename it over the old). The watch on the old file
                # is then lost, but the folder still reports the change.
                parent = os.path.dirname(path)
                if parent:
                    desired.add(parent)

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


def _describe_delay(delay_ms: int) -> str:
    seconds = round(delay_ms / 1000)
    if seconds < 60:
        return f"{seconds} seconds"
    minutes = round(seconds / 60)
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


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
