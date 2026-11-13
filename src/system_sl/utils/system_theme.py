"""Detect whether the operating system is currently using a light or dark theme.

Qt reports this reliably on Windows, macOS and most Linux desktops, and emits a
signal when it changes. The platform probes below are fallbacks for sessions
where Qt reports ``Unknown`` (headless sessions, some tiling WMs, KDE builds
without the portal, …).
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from typing import Optional

SCHEME_EXPORT = "SYSTEM_SL_COLOR_SCHEME"


def _run(command: list[str], timeout: float = 3.0) -> Optional[str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _qt_scheme() -> Optional[str]:
    """Ask Qt first: it is cheap and tracks live OS changes."""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication
    except Exception:
        return None

    app = QGuiApplication.instance()
    if app is None:
        return None
    try:
        scheme = app.styleHints().colorScheme()
    except Exception:
        return None
    if scheme == Qt.ColorScheme.Dark:
        return "dark"
    if scheme == Qt.ColorScheme.Light:
        return "light"
    return None


def qt_color_scheme() -> Optional[str]:
    """Return Qt's own answer, or None when it reports Unknown.

    Cheap (no subprocess), so it is safe on the GUI thread; the other probes
    may run gsettings and belong on a worker thread.
    """
    return _qt_scheme()


def _windows_scheme() -> Optional[str]:
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            apps_light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
    except OSError:
        return None
    return "light" if apps_light else "dark"


def _macos_scheme() -> Optional[str]:
    value = _run(["defaults", "read", "-g", "AppleInterfaceStyle"])
    if value is None:
        # The key is absent when Light mode is active.
        return "light"
    return "dark" if "dark" in value.lower() else "light"


def _gtk_theme_is_dark(name: str) -> bool:
    name = name.lower()
    return "dark" in name or "black" in name or "night" in name


def _linux_scheme() -> Optional[str]:
    # GNOME / libadwaita desktops.
    value = _run(
        ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"]
    )
    if value:
        value = value.strip("'\"").lower()
        if "prefer-dark" in value or value == "dark":
            return "dark"
        if "prefer-light" in value or value == "light":
            return "light"

    value = _run(["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"])
    if value and _gtk_theme_is_dark(value):
        return "dark"

    # Omarchy stores the active mode in its applied theme.
    omarchy = os.path.expanduser("~/.local/state/omarchy/current/theme/colors.toml")
    try:
        with open(omarchy, "r", encoding="utf-8", errors="replace") as handle:
            match = re.search(r'^\s*mode\s*=\s*["\'](dark|light)["\']', handle.read(), re.M)
        if match:
            return match.group(1)
    except OSError:
        pass

    # KDE keeps the window background in kdeglobals.
    kdeglobals = os.path.expanduser("~/.config/kdeglobals")
    try:
        section = None
        with open(kdeglobals, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1]
                    continue
                if section == "Colors:Window" and line.lower().startswith("backgroundnormal="):
                    r, g, b = (int(part) for part in line.split("=", 1)[1].split(",")[:3])
                    return "dark" if (r + g + b) / 3 < 128 else "light"
    except (OSError, ValueError):
        pass

    # GTK settings file used by many minimal sessions.
    for path in ("~/.config/gtk-4.0/settings.ini", "~/.config/gtk-3.0/settings.ini"):
        try:
            with open(os.path.expanduser(path), "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.strip().lower().startswith("gtk-theme-name"):
                        name = line.split("=", 1)[-1].strip()
                        return "dark" if _gtk_theme_is_dark(name) else "light"
        except OSError:
            continue
    return None


def detect_system_color_scheme() -> str:
    """Return ``"light"`` or ``"dark"`` for the current OS appearance.

    An explicit ``SYSTEM_SL_COLOR_SCHEME`` environment variable overrides every
    probe, which makes the behaviour testable and gives users an escape hatch.
    """
    override = os.environ.get(SCHEME_EXPORT, "").strip().lower()
    if override in ("light", "dark"):
        return override

    scheme = _qt_scheme()
    if scheme:
        return scheme

    system = platform.system().lower()
    if system == "windows":
        scheme = _windows_scheme()
    elif system == "darwin":
        scheme = _macos_scheme()
    elif system == "linux":
        scheme = _linux_scheme()
    else:
        scheme = None

    # Unknown or headless sessions keep the app's dark identity.
    return scheme or "dark"
