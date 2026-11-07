"""Single source of truth for whether task reminders may be shown.

"Notification AutoStart" only controls whether the notifier is launched at
login; it says nothing about whether reminders are wanted while the app runs.
This preference does, and both the GUI and the ``--bg`` process consult it
before notifying.
"""

from PySide6.QtCore import QSettings

from system_sl.utils.autostart import CrossPlatformAutostart

_SETTINGS_ORG = "system-sl"
_SETTINGS_APP = "AppConfig"
_KEY = "notifications_enabled"


def load_notifications_enabled() -> bool:
    """Return the reminder preference.

    Users upgrading from a version without this key fall back to the autostart
    state, which is what used to decide whether a popup appeared. Without the
    fallback, an upgrade would silently turn reminders on for everyone who had
    deliberately switched autostart off.
    """
    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    if settings.contains(_KEY):
        return settings.value(_KEY, False, type=bool)
    return CrossPlatformAutostart().is_enabled()


def save_notifications_enabled(enabled: bool) -> None:
    """Persist the reminder preference, including to other running processes."""
    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    settings.setValue(_KEY, bool(enabled))
    settings.sync()
