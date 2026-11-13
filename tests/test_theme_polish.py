"""Theme cache, palette, contrast and settings behaviour."""

import os
import tomllib
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def wallust(tmp_path, monkeypatch):
    import system_sl.utils.wallust_theme as wt

    monkeypatch.setenv("SYSTEM_SL_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(wt, "_memory_cache", None)
    monkeypatch.setattr(wt, "find_wallust", lambda: None)
    return wt


def _image(tmp_path, name="wall.png"):
    from PIL import Image

    path = tmp_path / name
    Image.new("RGB", (8, 8), (30, 60, 90)).save(path)
    return str(path)


def test_cache_is_used_only_for_the_same_image(wallust, tmp_path):
    from system_sl.utils.theme_gen import DEFAULT_TOKENS

    image = _image(tmp_path)
    tokens = dict(DEFAULT_TOKENS, sys_color_surface="#123456")
    wallust._write_cache(wallust._file_fingerprint(image), image, tokens)

    assert wallust.cached_tokens_for(image) == tokens
    assert wallust.cached_tokens_for(_image(tmp_path, "other.png")) is None

    os.utime(image, ns=(1, 1))
    assert wallust.cached_tokens_for(image) is None


def test_new_mapping_version_or_wallust_binary_invalidates_the_cache(
    wallust, tmp_path, monkeypatch
):
    from system_sl.utils.theme_gen import DEFAULT_TOKENS

    image = _image(tmp_path)
    wallust._write_cache(wallust._file_fingerprint(image), image, dict(DEFAULT_TOKENS))

    monkeypatch.setattr(wallust, "_memory_cache", None)
    monkeypatch.setattr(wallust, "PALETTE_MAPPING_VERSION", "999")
    assert wallust.cached_wallust_tokens_or_none() is None

    monkeypatch.setattr(wallust, "PALETTE_MAPPING_VERSION", "2")
    before = wallust._file_fingerprint(image)
    fake = tmp_path / "wallust"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(wallust, "find_wallust", lambda: str(fake))
    assert wallust._file_fingerprint(image) != before


def test_failed_cache_write_leaves_no_temp_file(wallust, tmp_path):
    wallust._write_cache("fp", "img", {"bad": object()})
    leftovers = list((tmp_path / "cache" / "system-sl").glob("*.tmp"))
    assert leftovers == []


def test_private_config_survives_quotes_in_the_path(wallust, tmp_path):
    target = Path(tmp_path / "it's \"here\"" / "palette.json")
    wallust._write_private_config(tmp_path / "config", target)
    config = tomllib.loads((tmp_path / "config" / "wallust.toml").read_text())
    assert config["templates"]["system_sl"]["target"] == target.as_posix()


@pytest.mark.parametrize(
    "accent",
    ["#ff2d95", "#f5d90a", "#13c4a3", "#5b6cff", "#ff7a00", "#9b9b9b"],
)
def test_text_on_tinted_fills_is_readable(wallust, accent):
    palette = {key: "#101418" for key in wallust.PALETTE_KEYS}
    palette.update({f"color{i}": accent for i in range(1, 7)}, foreground=accent)
    tokens = wallust.palette_to_tokens(palette)

    surface, primary = tokens["sys_color_surface"], tokens["sys_color_primary"]
    dim = wallust._mix(surface, primary, 0.10)
    hover = wallust._mix(surface, primary, 0.20)
    assert wallust._contrast(tokens["sys_color_button_text"], dim) >= 4.5
    assert wallust._contrast(tokens["sys_color_text_highlight"], hover) >= 4.5
    assert wallust._contrast(tokens["sys_color_selection_text"], hover) >= 4.5


def _settings(tmp_path):
    from PySide6.QtCore import QSettings

    return QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)


def test_reading_the_theme_mode_never_writes(tmp_path):
    from system_sl.frontend.gui.theme import (
        get_saved_theme_mode,
        migrate_saved_theme_mode,
    )

    settings = _settings(tmp_path)
    settings.setValue("dynamic_theming", True)
    settings.setValue("theme_mode", "")
    settings.sync()

    assert get_saved_theme_mode(settings) == "dynamic"
    assert not settings.value("theme_mode")

    migrate_saved_theme_mode(settings)
    assert settings.value("theme_mode") == "dynamic"

    settings.setValue("theme_mode", "default")
    assert get_saved_theme_mode(settings) == "dark"
    assert settings.value("theme_mode") == "default"


def test_palette_matches_the_tokens():
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from system_sl.frontend.gui.theme import LIGHT_TOKENS, build_palette

    palette = build_palette(LIGHT_TOKENS)
    role = QPalette.ColorRole
    assert palette.color(role.Window).name() == LIGHT_TOKENS["sys_color_background"].lower()
    assert palette.color(role.Text).name() == LIGHT_TOKENS["sys_color_text_main"].lower()
    # Translucent tokens fall back to a valid colour instead of black.
    assert build_palette({"sys_color_primary": "rgba(1, 2, 3, 0.1)"}).color(
        role.Highlight
    ).isValid()

