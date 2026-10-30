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
├── packaging/            # AppImage and Windows installer packaging
│
└── src/system_sl/        # The package; imports are system_sl.*
    ├── core/             # Tasks, onboarding, priority engine, vector fusion, Google sync
    ├── chatbot/          # LLM providers, config and chat tools
    ├── frontend/gui/     # PySide6 desktop app (entry point: main.py)
    ├── services/         # Background notification controller
    ├── utils/            # Paths, autostart, audio, notifications, auto-updater, build
    └── assets/           # Sounds, GIFs, icons, .desktop files
```
