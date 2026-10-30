# CLI Usage

The SYSTEM is a desktop application. These are the commands and flags for launching it.

## Launching the desktop app

```bash
uv run system-sl
```

Or, if installed globally:

```bash
system-sl
```

On first launch an **LLM Settings** dialog asks for the chatbot provider, model and API key. The settings are saved to `llm_config.json` in the system-sl data directory, and the API key to your system keyring.

## Background notifier mode

```bash
uv run system-sl --bg
```

This runs only the hourly task notifier — it never opens the main menu. This is the mode used by the autostart entry. See [Background Service](Background-Service).

## Desktop integration (AppImage)

```bash
./system-sl-*.AppImage --install-desktop
```

Moves the AppImage to `~/Applications` and adds a menu entry for it.

## Building a standalone executable

```bash
uv run build-sl
```

Produces a one-file executable in `dist/`. See [Building and Releasing](Building-and-Releasing).
