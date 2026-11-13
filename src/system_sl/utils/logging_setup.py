"""One rotating log file for the whole application.

The GUI process and the ``--bg`` notifier each get their own file
(``system-sl.log`` and ``system-sl-bg.log``): RotatingFileHandler is not safe
across processes, and two processes rotating one file lose records (and fail
outright on Windows, where the open file can't be renamed). Configuring logging at
import time (as ``vector_converter`` and the priority engine used to) also
touched the disk and the root logger during test collection; this is called
explicitly from the entry points instead.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from system_sl.utils.paths import get_tasks_file_path

LOG_FILENAME = "system-sl.log"
BG_LOG_FILENAME = "system-sl-bg.log"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3

_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    """Attach the rotating file handler once."""
    global _configured
    if _configured:
        return

    filename = BG_LOG_FILENAME if "--bg" in sys.argv[1:] else LOG_FILENAME
    path = get_tasks_file_path(filename)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        # A read-only config directory must not stop the app from running.
        logging.basicConfig(level=level)
        _configured = True
        return

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    _configured = True