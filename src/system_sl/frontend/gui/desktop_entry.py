"""The desktop entry installer.

Kept out of ``main`` so that both entry points — the GUI and the background
notifier — can install it without importing the rest of the GUI stack, and
without importing each other in a cycle.
"""

import os
from pathlib import Path


def install_desktop_entry() -> int:
    """Move the AppImage to ~/Applications and add a menu entry for it."""
    appimage_path = os.environ.get("APPIMAGE")
    if not appimage_path or not Path(appimage_path).is_file():
        print("❌ This command only works when running from an AppImage.")
        return 1

    from system_sl.utils.appimage_install import DESKTOP_FILE, install_appimage

    try:
        installed = install_appimage(Path(appimage_path))
    except OSError as e:
        print(f"❌ Failed to install the AppImage: {e}")
        return 1
    print(f"✅ Installed to: {installed}")
    print(f"   Menu entry: {DESKTOP_FILE}")
    return 0
