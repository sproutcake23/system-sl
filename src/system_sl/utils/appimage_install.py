"""Install the running AppImage: move it to ~/Applications, add a menu entry
and an icon, and keep both pointing at the file when it moves or is renamed.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from system_sl.utils.autostart import CrossPlatformAutostart
from system_sl.utils.host_env import host_env

APPLICATIONS_DIR = Path.home() / "Applications"
DESKTOP_FILE = Path.home() / ".local" / "share" / "applications" / "system-sl.desktop"
ICON_FILE = (
    Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "system-sl.svg"
)


def _quote_exec_arg(arg: str) -> str:
    """Quote one Exec argument as the desktop entry spec requires.

    Inside quotes, ", `, $ and \\ are escaped with a backslash. Exec is also
    a string value, so every backslash is then escaped again, and a literal
    % must be written as %% because it starts a field code.
    """
    escaped = re.sub(r'(["`$\\])', r"\\\1", arg)
    return '"' + escaped.replace("\\", "\\\\").replace("%", "%%") + '"'


def _find_bundled_icon() -> Path | None:
    candidates = []
    if os.environ.get("APPDIR"):
        candidates.append(Path(os.environ["APPDIR"]) / "system-sl.svg")
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "assets" / "system-sl.svg")
    candidates.append(Path(__file__).resolve().parents[1] / "assets" / "system-sl.svg")
    return next((path for path in candidates if path.is_file()), None)


def _install_icon() -> bool:
    icon = _find_bundled_icon()
    if icon is None:
        return False
    ICON_FILE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(icon, ICON_FILE)
    return True


def _refresh_kde_menu_cache() -> None:
    """Refresh KDE's application menu cache if running on KDE Plasma."""
    # Detect KDE Plasma
    xdg_current_desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    kde_session_version = os.environ.get("KDE_SESSION_VERSION", "")

    is_kde = (
        "kde" in xdg_current_desktop
        or kde_session_version in ("5", "6")
    )

    if not is_kde:
        return

    # Try kbuildsycoca6 first (KDE Plasma 6), then kbuildsycoca5 (Plasma 5)
    for cmd in ("kbuildsycoca6", "kbuildsycoca5"):
        try:
            # Run in background, don't wait for it. kbuildsycoca is a Qt
            # program, so it must not see our bundled Qt (see host_env).
            subprocess.Popen(
                [cmd],
                env=host_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            break
        except FileNotFoundError:
            continue


def write_desktop_entry(appimage: Path) -> None:
    """Write the menu entry that launches ``appimage``."""
    icon = "system-sl" if ICON_FILE.is_file() else "utilities-terminal"
    DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP_FILE.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=THE SYSTEM\n"
        f"Exec={_quote_exec_arg(str(appimage))}\n"
        "Terminal=false\n"
        f"Icon={icon}\n"
        "Categories=Utility;\n"
        "Comment=Arise, Player.\n"
        "StartupNotify=true\n"
    )
    _refresh_kde_menu_cache()


def appimage_moved(new_path: Path, *, create_entry: bool = False) -> None:
    """Point this process, the menu entry and autostart at ``new_path``."""
    os.environ["APPIMAGE"] = str(new_path)
    if create_entry or DESKTOP_FILE.is_file():
        write_desktop_entry(new_path)
    autostart = CrossPlatformAutostart("SL-Notifier")
    if autostart.is_enabled():
        autostart.enable()


def install_appimage(appimage: Path) -> Path:
    """Move ``appimage`` into ~/Applications and add a menu entry and icon.

    Moving is safe while it runs: the mounted image keeps its open inode.
    Returns the installed path.
    """
    target = APPLICATIONS_DIR / appimage.name
    if appimage.resolve() != target.resolve():
        APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(appimage, target)
    os.chmod(target, 0o755)
    _install_icon()
    appimage_moved(target, create_entry=True)
    return target
