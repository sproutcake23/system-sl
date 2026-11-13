"""Tiny translation helper.

There are no catalogues yet; this establishes the call sites so adding one
later does not mean touching every string. Qt's own translation machinery is
used when an application exists, and the source text is returned otherwise
(headless code, tests, the --bg notifier before QApplication exists).
"""

from __future__ import annotations


def tr(text: str, context: str = "SystemSL") -> str:
    """Return the translation of ``text``, or ``text`` when none is loaded."""
    if not text:
        return text
    try:
        from PySide6.QtCore import QCoreApplication

        app = QCoreApplication.instance()
        if app is not None:
            return QCoreApplication.translate(context, text)
    except Exception:
        pass
    return text