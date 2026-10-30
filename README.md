# SL — The SYSTEM

> _"Arise, Player."_

SL is a personal progression system inspired by Solo Leveling. You define your daily quests, and the SYSTEM keeps you on track — on your desktop or from the terminal.

## Preview

<table>
  <tr>
    <td align="center"><img src="src/system_sl/assets/main.gif" width="240" alt="Main menu"/><br/><em>Main menu</em></td>
    <td align="center"><img src="src/system_sl/assets/tasks.gif" width="240" alt="Task manager"/><br/><em>Task manager</em></td>
    <td align="center"><img src="src/system_sl/assets/onboarding.gif" width="240" alt="Onboarding wizard"/><br/><em>Onboarding wizard</em></td>
  </tr>
  <tr>
    <td align="center"><img src="src/system_sl/assets/chat.gif" width="240" alt="Chat"/><br/><em>Chat</em></td>
    <td align="center"><img src="src/system_sl/assets/notification.gif" width="240" alt="System notification"/><br/><em>System notification</em></td>
  </tr>
</table>

## Features

- **Persona Builder** — A 10-step onboarding process that builds your Player Profile from your real-world ambitions.
- **Task Management** — Add, complete, and reorder quests with deadlines and categories.
- **Google Tasks Sync** — Import your Google Tasks as quests. Opt-in from the main window.
- **Native Notifications** — Desktop alerts styled like the System, with custom notification sounds.
- **AI Chatbot** — Talk to the System, an AI mentor powered by Gemini with task-aware tools, plus a friendly companion persona.
- **Priority Engine** — Context-aware scoring (PRESSURE / GROWTH / RECOVERY) of what to work on next.
- **NLP Vectorizer** — spaCy-powered persona vectors built from your responses and task history, with temporal decay.
- **Background Reminder** — A persistent hourly notifier that keeps you on track.
- **Cross-Platform Autostart** — Register the notifier on Linux, Windows, or macOS.

## Installation

### Option 1: AppImage (Linux — Recommended)

Download the latest `system-sl-<version>-x86_64.AppImage` from [GitHub Releases](https://github.com/sproutcake23/System-SL/releases).

```bash
chmod +x system-sl-*.AppImage
./system-sl-*.AppImage
```

**Desktop integration (choose one):**

| Method | Command | Notes |
|--------|---------|-------|
| **Built-in (Recommended)** | `./system-sl-*.AppImage --install-desktop` | Creates `.desktop` entry automatically, no extra tools |
| **AppImageLauncher** | Download from [GitHub](https://github.com/TheAssassin/AppImageLauncher/releases) | GUI tool, file manager integration, update management |
| **Gear Lever** | `flatpak install flathub it.mijorus.gearlever` | Modern AppImage manager, requires Flatpak |

The built-in `--install-desktop` flag creates a menu entry pointing to your AppImage location. Run it once after downloading:

```bash
chmod +x system-sl-*.AppImage
./system-sl-*.AppImage --install-desktop
```

### Option 2: Windows Installer

Download `system-sl-setup.exe` from [GitHub Releases](https://github.com/sproutcake23/System-SL/releases) and run it.

This creates:
- Start Menu entry
- Desktop shortcut
- Uninstaller in Settings → Apps

### Option 3: Portable EXE (Windows/Linux)

Download `system-sl-windows.zip` (Windows) or `system-sl-linux.tar.xz` (Linux) from [GitHub Releases](https://github.com/sproutcake23/System-SL/releases).

Extract and run `system-sl.exe` / `system-sl` directly.

### Option 4: From Source (Development)

Requirements: Python >= 3.11 and < 3.14, and [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/sproutcake23/System-SL.git
cd System-SL
uv sync
```

Launch the desktop app:
```bash
uv run system-sl
```

Run the background notifier:
```bash
uv run system-sl --bg
```

Build a standalone executable:
```bash
uv run build-sl
```

Source installs don't include the app's Google OAuth client. To use Google Tasks Sync from source, set up your own; see [Google Tasks Sync](https://github.com/sproutcake23/System-SL/wiki/Google-Tasks-Sync#using-your-own-oauth-client).

## Documentation

Full documentation lives in the wiki:

- [Installation](https://github.com/sproutcake23/System-SL/wiki/Installation)
- [Features](https://github.com/sproutcake23/System-SL/wiki/Features)
- [Google Tasks Sync](https://github.com/sproutcake23/System-SL/wiki/Google-Tasks-Sync)
- [CLI Usage](https://github.com/sproutcake23/System-SL/wiki/CLI-Usage)
- [Background Service](https://github.com/sproutcake23/System-SL/wiki/Background-Service)
- [Building and Releasing](https://github.com/sproutcake23/System-SL/wiki/Building-and-Releasing)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

GPL-3.0-or-later — see [LICENSE](LICENSE) for details.