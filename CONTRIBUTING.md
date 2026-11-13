# 🤝 Contributing to SL — The SYSTEM

Thank you for your interest in contributing! This document provides an overview of the project structure and development guidelines to help you get started.

---

## 📂 Project Structure

This project follows a standard Python package layout. Please keep this structure organized as we add new features.

```text
System-SL
├── pyproject.toml        # Project metadata and dependencies
├── README.md             # User-facing documentation
├── CONTRIBUTING.md       # Developer documentation
├── docs/wiki/            # Wiki pages (copied to the GitHub wiki by hand)
├── .github/workflows/    # ci.yml: lint + tests on every PR; release.yml: the Linux and Windows build
├── packaging/            # PyInstaller spec, AppImage and Windows installer packaging
│
└── src/system_sl/        # The package; imports are system_sl.*
    ├── core/             # Tasks, onboarding, priority engine, vector fusion, Google sync
    ├── chatbot/          # LLM providers, config, chat tools and document reading
    ├── frontend/gui/     # PySide6 desktop app
    │   ├── entry.py      #   `system-sl` command: runs notifier.py for --bg, else main.py
    │   ├── main.py       #   Main window
    │   ├── notifier.py   #   Lightweight background notifier (--bg)
    │   └── ...           #   Chat panel, settings, popups, icons, theming
    ├── services/         # Background notification controller
    ├── utils/            # Paths, autostart, audio, notifications, auto-updater, build,
    │                     # theming, secure storage, Google API, install/cleanup
    └── assets/           # Sounds, GIFs, icons, .desktop files
```

User data lives in `~/.config/system-sl/` on Linux and `%APPDATA%\system-sl` on Windows. Anything that persists state must go through `src/system_sl/utils/paths.py`.

The version lives only in `src/system_sl/__init__.py` as `__version__`; `pyproject.toml` reads it dynamically.

---

## 🛠️ Development Setup

Requirements: Python >= 3.11 and < 3.14, and [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/sproutcake23/System-SL.git
cd System-SL
uv sync
uv run system-sl          # desktop app
uv run system-sl --bg     # background notifier
```

---

## 🧪 Verifying Changes

CI (`.github/workflows/ci.yml`) runs ruff and the test suite on every pull request and push to `main`. Run the same checks before pushing:

```bash
uvx ruff check src tests
QT_QPA_PLATFORM=offscreen uv run --extra test pytest -q
uv build --wheel --offline     # metadata and packaging still valid
```

For anything behavioural, build and launch the app. Several bugs have only shown up in the frozen build:

```bash
uv run build-sl
uv run python packaging/appimage/build-appimage.py
HOME=/tmp/sandbox QT_QPA_PLATFORM=offscreen ./dist/system-sl-*-x86_64.AppImage
```

Then check what it wrote to the config directory:

| File | Meaning |
|---|---|
| `prioritize.json` | Ranking succeeded; `all_tasks_ranked` should be non-empty |
| `vector_cache.json` | The spaCy model loaded from the bundle |
| `task_order.json` | Manual task order was persisted |
| Any `Traceback` on stdout | A failure, even if the UI looks fine |

Build flags live in `packaging/system-sl.spec` and `.github/workflows/release.yml`; `src/system_sl/utils/build.py` mirrors the workflow, so change them together.

---

## 🔀 Pull Requests and Releases

- `main` is protected: every PR needs one approving review. The repo uses merge commits, not squash.
- Never commit `dist/`, `build/`, or `src/system_sl/assets/google_oauth_client.json`.
- Desktop entries must pass `desktop-file-validate`.
- `docs/wiki/` is not published automatically; after merging, copy the pages to the GitHub wiki (leave out `docs/wiki/README.md`).
- To release, bump `__version__` in a `chore: bump version to X.Y.Z` PR, then push a `vX.Y.Z` tag. The tag triggers `release.yml`, which builds Linux and Windows and publishes the GitHub release.
