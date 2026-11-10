"""Module for handling cross-platform application data storage directories."""

import os
import sys
from pathlib import Path


def get_tasks_file_path(filename: str) -> str:
    """Resolves a cross-platform configuration file path based on the host operating system.

    Args:
        filename (str): The name of the file to resolve.

    Returns:
        str: The absolute path pointing to the file inside the system-sl data folder.
    """
    prog_name = 'system-sl'

    if os.name == "nt":
        base_dir = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        config_dir = base_dir / prog_name
    else:
        config_dir = Path.home() / ".config" / prog_name

    config_dir.mkdir(parents=True, exist_ok=True)
    return str(config_dir / filename)


def get_system_data_path() -> Path | None:
    """Returns the system-wide data directory for AppImage/Flatpak/system installs.

    Checks common locations where assets/sounds might be installed system-wide.
    Returns None if not found (e.g., pip install without system data).
    """
    if os.name == "nt":
        return None

    # Common system data locations for AppImage, .deb, etc.
    candidates = [
        Path("/usr/share/system-sl"),
        Path("/usr/local/share/system-sl"),
        Path("/opt/system-sl/share"),
    ]

    # Also check if running from AppImage (APPIMAGE env var)
    if "APPIMAGE" in os.environ:
        # AppImage mounts at $APPIMAGE, but we can also check the mount point
        # The AppDir structure typically has usr/share/
        appimage_dir = Path(os.environ["APPIMAGE"]).parent / "usr" / "share" / "system-sl"
        candidates.insert(0, appimage_dir)

    for candidate in candidates:
        if candidate.exists() and (candidate / "sounds").exists():
            return candidate

    return None


def get_sounds_dir() -> Path:
    """Returns the sounds directory, with fallback to system-wide location.

    Priority:
    1. User config directory (~/.config/system-sl/sounds/)
    2. System-wide data directory (/usr/share/system-sl/sounds/, etc.)
    3. Package directory (for development/frozen)
    """
    user_dir = Path(get_tasks_file_path("sounds"))

    # If user directory exists and has sounds, use it
    if user_dir.exists() and any(user_dir.glob("*.mp3")):
        return user_dir

    # Fallback to system-wide data
    system_data = get_system_data_path()
    if system_data:
        sounds_dir = system_data / "sounds"
        if sounds_dir.exists() and any(sounds_dir.glob("*.mp3")):
            return sounds_dir

    # Fallback to package data (development/PyInstaller)
    if getattr(sys, "frozen", False):
        # PyInstaller bundle. The build copies assets to different locations
        # depending on build mode:
        # - One-file: <bundle>/assets/sounds/
        # - One-folder (AppImage): <bundle>/_internal/assets/sounds/
        bundle_dir = Path(sys._MEIPASS)
        for pkg_sounds in (
            bundle_dir / "_internal" / "assets" / "sounds",  # One-folder (AppImage)
            bundle_dir / "assets" / "sounds",                 # One-file
        ):
            if pkg_sounds.exists():
                return pkg_sounds
    else:
        # Development/source install. __file__ is system_sl/utils/paths.py, so
        # the package root is two levels up. Three levels up pointed at src/,
        # where there is no assets directory, so a source checkout silently
        # fell through to an empty user directory with no sounds at all.
        pkg_dir = Path(__file__).parent.parent
        pkg_sounds = pkg_dir / "assets" / "sounds"
        if pkg_sounds.exists():
            return pkg_sounds

    # Last resort: return user dir (will be created on demand)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir
