"""Dynamic theming: a failed extraction is not a palette, and it is retried."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    from system_sl.frontend.gui import theme_manager as tm

    m = tm.ThemeManager(app)
    m._mode = "dynamic"
    yield m
    m._timer.stop()
    m._retry_timer.stop()
    m.deleteLater()


def _state(fingerprint="fp-1", path="/walls/a.png"):
    from system_sl.utils.wallpaper_path import WallpaperState

    return WallpaperState(source="test", image_path=path, fingerprint=fingerprint)


def test_retry_delay_backs_off_and_caps():
    from system_sl.frontend.gui.theme_manager import RETRY_MAX_MS, retry_delay_ms

    assert retry_delay_ms(1) == 15_000
    assert retry_delay_ms(2) == 30_000
    assert retry_delay_ms(3) == 60_000
    assert retry_delay_ms(20) == RETRY_MAX_MS


def test_failure_keeps_colours_and_does_not_pin_the_wallpaper(manager):
    from system_sl.utils.wallust_theme import ExtractionResult

    before = manager.tokens
    manager._last_good_tokens = before
    manager._on_result("fp-1", ExtractionResult(error="Could not read colours from the wallpaper."))

    assert manager._fingerprint is None
    assert manager.tokens == before
    assert manager._last_good_tokens == before
    assert manager._retry_timer.isActive()
    assert "trying again in 15 seconds" in manager.status_message

    manager._on_result("fp-1", ExtractionResult(error="again"))
    assert manager._failures == 2
    assert manager._retry_timer.remainingTime() > 15_000


def test_polls_inside_the_backoff_do_not_re_extract(manager, monkeypatch):
    from system_sl.utils.wallust_theme import ExtractionResult

    started = []
    monkeypatch.setattr(manager, "_start_extract", lambda state: started.append(state))
    manager._on_result("fp-1", ExtractionResult(error="boom"))

    manager._on_state(_state("fp-1"))
    assert started == []

    # Once the wait is over the same wallpaper is tried again.
    manager._retry_at = 0
    manager._on_state(_state("fp-1"))
    assert len(started) == 1

    # A different wallpaper is never held back by another one's failure.
    manager._retry_at = 10**12
    manager._on_state(_state("fp-2"))
    assert len(started) == 2


def test_success_commits_and_clears_the_failure(manager):
    from system_sl.utils.theme_gen import DEFAULT_TOKENS
    from system_sl.utils.wallust_theme import ExtractionResult

    manager._on_result("fp-1", ExtractionResult(error="boom"))
    palette = dict(DEFAULT_TOKENS, sys_color_surface="#123456")
    manager._detected_path = "/walls/a.png"
    manager._on_result("fp-1", ExtractionResult(tokens=palette))

    assert manager._fingerprint == "fp-1"
    assert manager._last_good_tokens == palette
    assert manager.tokens["sys_color_surface"] == "#123456"
    assert manager._failed_fingerprint is None
    assert not manager._retry_timer.isActive()
    # Success isn't an alert: the path moves to the preview caption's tooltip.
    assert manager.status_message == ""
    assert manager.palette_source == "Using wallpaper: /walls/a.png"


def test_cancelled_extraction_changes_nothing(manager):
    from system_sl.utils.wallust_theme import ExtractionResult

    manager._on_result("fp-1", ExtractionResult(cancelled=True))
    assert manager._failed_fingerprint is None
    assert not manager._retry_timer.isActive()


def test_unreadable_image_is_a_failure_not_the_default_palette(tmp_path):
    from system_sl.utils.wallust_theme import extract_wallust_tokens

    result = extract_wallust_tokens(str(tmp_path / "missing.png"))
    assert not result.ok
    assert result.error


def test_unopenable_wallpaper_reports_an_error_not_a_bare_path(tmp_path):
    from system_sl.utils.wallpaper_path import _state as wallpaper_state

    state = wallpaper_state("gnome", str(tmp_path / "gone.png"))
    assert not state.ok
    assert state.error != str(tmp_path / "gone.png")
    assert "could not be opened" in state.error


def test_cached_palette_for_the_same_image_skips_wallust(manager, monkeypatch):
    from system_sl.frontend.gui import theme_manager as tm
    from system_sl.utils.theme_gen import DEFAULT_TOKENS

    palette = dict(DEFAULT_TOKENS, sys_color_surface="#123456")
    monkeypatch.setattr(tm, "cached_tokens_for", lambda path: palette)
    started = []
    monkeypatch.setattr(manager, "_start_extract", lambda state: started.append(state))

    manager._on_state(_state("fp-9"))

    assert started == []
    assert manager._fingerprint == "fp-9"
    assert manager.tokens["sys_color_surface"] == "#123456"


def test_first_switch_keeps_colours_and_says_it_is_working(manager, monkeypatch):
    from system_sl.frontend.gui import theme_manager as tm

    monkeypatch.setattr(tm, "find_wallust", lambda: "/usr/bin/wallust")
    monkeypatch.setattr(tm, "cached_wallust_tokens_or_none", lambda: None)
    monkeypatch.setattr(manager, "_refresh_watch_paths", lambda extra=(): None)
    monkeypatch.setattr(manager, "_probe", lambda: None)
    before = dict(manager.tokens, sys_color_surface="#abcdef")
    manager._tokens = before

    manager._start_dynamic()

    assert manager.tokens == before
    assert manager.status_message == tm.READING_STATUS


def test_installing_wallust_later_starts_dynamic(manager, monkeypatch):
    from system_sl.frontend.gui import theme_manager as tm

    found = {"path": None}
    monkeypatch.setattr(tm, "find_wallust", lambda: found["path"])
    monkeypatch.setattr(tm, "cached_wallust_tokens_or_none", lambda: None)
    monkeypatch.setattr(manager, "_refresh_watch_paths", lambda extra=(): None)
    probes = []
    monkeypatch.setattr(manager, "_probe", lambda: probes.append(1))

    manager._start_dynamic()
    assert manager._wallust_timer.isActive()

    found["path"] = "/usr/bin/wallust"
    manager._recheck_wallust()
    assert not manager._wallust_timer.isActive()
    assert probes == [1]


def test_unknown_system_scheme_is_probed_off_the_gui_thread(manager, monkeypatch):
    from system_sl.frontend.gui import theme_manager as tm

    manager._mode = "system"
    monkeypatch.setattr(tm, "qt_color_scheme", lambda: None)
    called = []
    monkeypatch.setattr(tm, "get_tokens_for_mode", lambda mode: called.append(mode))
    started = []

    class FakeThread:
        def __init__(self, parent):
            self.tokensReady = self.finished = self

        def connect(self, slot):
            pass

        def deleteLater(self):
            pass

        def start(self):
            started.append(1)

    monkeypatch.setattr(tm, "_SchemeThread", FakeThread)
    manager._check_system_scheme()

    assert called == []
    assert started == [1]


def test_chosen_wallpaper_is_used_without_detection(manager, tmp_path, monkeypatch):
    from PIL import Image

    from system_sl.frontend.gui import theme_manager as tm

    image = tmp_path / "pick.png"
    Image.new("RGB", (4, 4), (200, 30, 30)).save(image)
    monkeypatch.setattr(tm, "_ProbeThread", None)  # detection must not start
    started = []
    monkeypatch.setattr(manager, "_start_extract", lambda state: started.append(state))
    monkeypatch.setattr(tm, "cached_tokens_for", lambda path: None)
    seen = []
    manager.overrideChanged.connect(seen.append)

    manager.set_wallpaper_override(str(image))

    assert manager.wallpaper_override == str(image)
    assert seen == [str(image)]
    assert started and started[0].image_path == str(image.resolve())
    assert started[0].source == "manual"

    # Clearing the choice goes back to detection.
    probes = []
    monkeypatch.setattr(manager, "_probe", lambda: probes.append(1))
    manager.set_wallpaper_override(None)
    assert manager.wallpaper_override == ""
    assert probes == [1]


def test_refresh_forgets_the_failure_backoff(manager, monkeypatch):
    from system_sl.utils.wallust_theme import ExtractionResult

    probes = []
    monkeypatch.setattr(manager, "_probe", lambda: probes.append(1))
    manager._on_result("fp-1", ExtractionResult(error="boom"))
    assert manager._retry_timer.isActive()

    manager.refresh_wallpaper()

    assert not manager._retry_timer.isActive()
    assert manager._failed_fingerprint is None
    assert probes == [1]


def test_preview_follows_the_palette_and_mode(manager):
    from system_sl.frontend.gui.theme_preview import ThemePreview
    from system_sl.utils.theme_gen import DEFAULT_TOKENS

    manager._mode = "dark"
    preview = ThemePreview(manager)
    assert preview.isHidden()  # the whole preview is Dynamic-only

    manager._apply(dict(DEFAULT_TOKENS, sys_color_primary="#123456"))
    assert preview._swatches["sys_color_primary"].toolTip() == "Accent: #123456"

    manager._mode = "dynamic"
    manager.modeChanged.emit("dynamic")
    assert not preview.isHidden()
    assert preview.caption.text() == "Colours from your wallpaper"
    assert preview.detected_button.isHidden()  # no chosen wallpaper yet
    preview.deleteLater()
