# Background Service

The background service keeps you on track with hourly desktop notifications while the main window is closed.

## How it works

`BackgroundServiceController` (in `system_sl/services/background_service.py`) uses a `QTimer` that fires every hour (3,600,000 ms). On each tick it:

1. Re-runs the priority engine to refresh task scores.
2. Picks the top-priority task.
3. Displays it in a HUD-style `SystemNotification` window with its sound.

## Running it manually

```bash
uv run system-sl --bg
```

## Autostart

The GUI has a checkbox on the main menu that toggles autostart on login:

- **Linux**: a `.desktop` entry in `~/.config/autostart/` launching `system-sl --bg`
- **macOS**: a LaunchAgent plist
- **Windows**: a Registry `Run` key

Autostart is implemented for all three platforms but is currently tested on Linux.

Autostart only controls whether the notifier is launched at login. It does
**not** decide whether reminders are shown. The separate **Notifications**
checkbox on the main menu does that, and its value is stored as
`notifications_enabled` in `AppConfig.conf`:

- Reminders off: the GUI starts no notifier, and `--bg` exits immediately even
  if an autostart entry still exists.
- Reminders on: the GUI runs the notifier while it is open; `--bg` delivers the
  login reminder.
- `BackgroundServiceController` re-reads the preference on every tick, so a
  notifier that is already running stops as soon as reminders are switched off.

A user upgrading from a version without `notifications_enabled` falls back to
their autostart setting, so someone who had autostart off does not suddenly
start receiving reminders.

## Custom notification sounds

Place `.mp3` or `.wav` files in the `sounds/` folder inside the system-sl data directory:

- Linux/macOS: `~/.config/system-sl/sounds/`
- Windows: `%APPDATA%\system-sl\sounds\`

The GUI lets you preview sounds and set the one used for notifications. Files are validated for duration before use.
