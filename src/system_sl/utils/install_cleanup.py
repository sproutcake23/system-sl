"""Remove what earlier versions left behind and repoint stale entries.

Runs on every launch, after any staged update has been applied. Each step is
best-effort: a cleanup that fails must never stop the app from starting.
"""

import os
import shlex
import sys
from pathlib import Path

from system_sl.utils.autostart import CrossPlatformAutostart


def cleanup_previous_installs() -> None:
    for step in (
        _remove_old_appimages,
        _remove_stranded_update_files,
        _remove_stray_windows_copy,
        _repair_autostart,
    ):
        try:
            step()
        except Exception as e:
            print(f"[cleanup] {step.__name__} failed: {e}")


def _remove_old_appimages() -> None:
    """Delete older release AppImages and repoint a menu entry left dangling.

    Installing a new version by hand (download, run) left the old AppImage in
    place, and the menu entry kept launching it.
    """
    from system_sl.utils.appimage_install import APPLICATIONS_DIR, DESKTOP_FILE, write_desktop_entry
    from system_sl.utils.auto_updater import appimage_version, running_appimage

    current = running_appimage()
    if current is None:
        return
    current_version = appimage_version(current.name)
    if current_version is None:
        # Renamed by the user, so there is no telling which copies are older.
        return

    for folder in {current.parent, APPLICATIONS_DIR}:
        if not folder.is_dir():
            continue
        for path in folder.glob("system-sl-*-x86_64.AppImage"):
            version = appimage_version(path.name)
            if version is not None and version < current_version:
                path.unlink(missing_ok=True)
                print(f"[cleanup] Removed old version {path}")

    if DESKTOP_FILE.is_file():
        target = _desktop_entry_executable(DESKTOP_FILE)
        if target is None or not target.is_file():
            write_desktop_entry(current)


def _remove_stranded_update_files() -> None:
    """Delete update leftovers that nothing will ever use again."""
    from system_sl.utils.auto_updater import _load_pending_update, running_appimage

    # A staged AppImage that is not recorded as pending will never be applied.
    appimage = running_appimage()
    if appimage is not None:
        staged = appimage.with_name(appimage.name + ".new")
        if staged.is_file() and _load_pending_update()[0] is None:
            staged.unlink()

    # Windows renames the running .exe aside during an update.
    if os.name == "nt" and getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        exe.with_name(exe.name + ".old").unlink(missing_ok=True)


def _remove_stray_windows_copy() -> None:
    """Delete the copy that older updaters installed into %APPDATA%.

    The installer puts the app in %LOCALAPPDATA%, but updates up to 1.4.5
    went to %APPDATA%\\system-sl (the data folder) and pointed the desktop
    shortcut there, leaving two copies of the app.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    stray = Path(os.environ["APPDATA"]) / "system-sl" / "system-sl.exe"
    running = Path(sys.executable).resolve()
    if not stray.is_file() or stray.resolve() == running:
        return

    stray.unlink()
    stray.with_name(stray.name + ".old").unlink(missing_ok=True)
    print(f"[cleanup] Removed stray copy {stray}")

    from system_sl.utils.auto_updater import write_windows_shortcut
    shortcut = Path(os.environ["USERPROFILE"]) / "Desktop" / "THE SYSTEM.lnk"
    if shortcut.is_file():
        write_windows_shortcut(shortcut, running)


def _repair_autostart() -> None:
    """Point the autostart entry at this copy if it launches nothing useful.

    The entry used to keep launching a deleted or older copy after a manual
    install, so the notifier silently stopped or ran the old version.
    """
    from system_sl.utils.auto_updater import appimage_version, running_appimage

    autostart = CrossPlatformAutostart("SL-Notifier")
    if not autostart.is_enabled():
        return

    recorded = autostart.recorded_executable()
    if recorded is not None and recorded.is_file():
        current = running_appimage()
        recorded_version = appimage_version(recorded.name)
        current_version = appimage_version(current.name) if current else None
        if recorded_version is None or current_version is None or recorded_version >= current_version:
            return

    autostart.enable()
    print("[cleanup] Repointed autostart at this copy")


def _desktop_entry_executable(desktop_file: Path) -> Path | None:
    for line in desktop_file.read_text().splitlines():
        if line.startswith("Exec="):
            try:
                parts = shlex.split(line[len("Exec="):])
            except ValueError:
                return None
            return Path(parts[0]) if parts else None
    return None
