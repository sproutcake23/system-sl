# Installation

## Requirements

- **Operating systems**: Linux and Windows have release builds (tested on Fedora KDE Plasma and Windows 11). On macOS, install from source.
- **From source only**: Python >= 3.11 and < 3.14, and [uv](https://github.com/astral-sh/uv) (recommended)

Release builds are self-contained, so they don't need Python.

## Option A: Install from releases

Download one of these from the [Latest Release](https://github.com/sproutcake23/System-SL/releases):

| Platform | File | How to use it |
|----------|------|---------------|
| Linux (recommended) | `system-sl-<version>-x86_64.AppImage` | Make it executable and run it |
| Windows (recommended) | `system-sl-setup.exe` | Run the installer |
| Linux, portable | `system-sl-linux.tar.xz` | Extract it and run `system-sl` |
| Windows, portable | `system-sl-windows.zip` | Extract it and run `system-sl.exe` |

Each release also publishes `SHA256SUMS.txt` for verifying your download.

### AppImage

```bash
chmod +x system-sl-*.AppImage
./system-sl-*.AppImage --install-desktop
```

`--install-desktop` moves the AppImage to `~/Applications` and adds a menu entry for it. You can also do this later with **Settings → Install App**, or just run the AppImage from wherever it is.

### Windows installer

The installer adds a Start Menu entry, a desktop shortcut, and an uninstaller in **Settings → Apps**.

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

To read scanned PDFs attached in chat, also install `tesseract` and `poppler` with your system's package manager. Release builds already include them.

## First launch

The app asks for your chatbot provider, model, and API key, then offers the optional Player Profile wizard. See [Features](Features).

## Where data is stored

All local data lives in a platform-native directory:

| Platform  | Path                          |
|-----------|-------------------------------|
| Linux/macOS | `~/.config/system-sl/`        |
| Windows   | `%APPDATA%\system-sl\`        |

This directory holds your tasks (`tasks.json`), persona (`persona.json`), completed tasks, notifications sounds (`sounds/`), and, if you set up your own Google OAuth client, `credentials.json`.

API keys and Google sign-in tokens go in your system keyring instead. If no keyring is available, they're kept in an encrypted file in this directory.

## Google integration

For Google Tasks sync, see [Google Tasks Sync](Google-Tasks-Sync).
