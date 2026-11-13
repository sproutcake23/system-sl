"""Shared helpers.

Names are loaded on first use rather than at import: importing any
``system_sl.utils`` submodule runs this file, and loading everything here
pulled the Google API client, keyring and cryptography into the background
notifier, which never uses them.
"""

from importlib import import_module

_EXPORTS = {
    "GoogleAPIClient": "system_sl.utils.google_api",
    "GoogleSyncError": "system_sl.utils.google_api",
    "SystemNotification": "system_sl.utils.notifications_ui",
    "CrossPlatformAutostart": "system_sl.utils.autostart",
    "load_data": "system_sl.utils.json_client",
    "save_data": "system_sl.utils.json_client",
    "get_tasks_file_path": "system_sl.utils.paths",
    "WallpaperState": "system_sl.utils.wallpaper_path",
    "get_wallpaper_path": "system_sl.utils.wallpaper_path",
    "get_wallpaper_state": "system_sl.utils.wallpaper_path",
    "watch_paths": "system_sl.utils.wallpaper_path",
    "cached_wallust_tokens": "system_sl.utils.wallust_theme",
    "find_wallust": "system_sl.utils.wallust_theme",
    "generate_wallust_tokens": "system_sl.utils.wallust_theme",
    "palette_to_tokens": "system_sl.utils.wallust_theme",
    "wallust_available": "system_sl.utils.wallust_theme",
}

__all__ = [*_EXPORTS, "secure_storage"]


def __getattr__(name):
    if name == "secure_storage":
        return import_module("system_sl.utils.secure_storage")
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value  # later lookups skip __getattr__
    return value
