# Installation

## Requirements

- **Operating systems**: Linux, Windows, macOS (tested on Fedora KDE Plasma and Windows 11)
- **Python**: >= 3.11 and < 3.14
- **Package manager**: [uv](https://github.com/astral-sh/uv) (recommended)

## Option A: Install from releases

1. Download the ZIP for your operating system from the [Latest Release](https://github.com/sproutcake23/System-SL/releases):
   - `system-sl-windows.zip`
   - `system-sl-linux.zip`
2. Unzip it to a location of your choice.
3. Run the packaged executable (`system-sl`). On Windows you can double-click it or run it from a terminal.

The bundled binary is self-contained; no Python installation is required.

## Option B: Install from source

```bash
git clone https://github.com/sproutcake23/System-SL.git
cd System-SL

# Sync the environment and dependencies
uv sync

# Launch the desktop application
uv run system-sl
```

`uv sync` installs all dependencies automatically, including the spaCy model `en_core_web_md` pinned in `pyproject.toml`.

## Where data is stored

All local data lives in a platform-native directory:

| Platform  | Path                          |
|-----------|-------------------------------|
| Linux/macOS | `~/.config/system-sl/`        |
| Windows   | `%APPDATA%\system-sl\`        |

This directory holds your tasks (`tasks.json`), persona (`persona.json`), completed tasks, notifications sounds (`sounds/`), and your Google OAuth client (`credentials.json`).

## Google integration

For Google Tasks sync, see [Google Tasks Sync](Google-Tasks-Sync).
