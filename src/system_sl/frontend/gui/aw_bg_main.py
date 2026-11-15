#!/usr/bin/env python3
"""
Minimal entry point for the ActivityWatch background daemon (--aw-bg mode).

Avoids importing PySide6 or the GUI stack so the supervisor stays lightweight
while managing aw-server-rust, aw-awatcher, and GNOME Wayland D-Bus setup.
"""

import logging
import sys
from pathlib import Path

# Add src to path for frozen builds
if getattr(sys, "frozen", False):
    bundle_dir = Path(sys._MEIPASS)
    src_path = bundle_dir / "src"
    if src_path.exists():
        sys.path.insert(0, str(src_path))

from system_sl.services.aw_daemon import (
    is_aw_user_enabled,
    run_activitywatch_daemon,
)
from system_sl.utils.auto_updater import apply_pending_update
from system_sl.utils.logging_setup import configure_logging

log = logging.getLogger(__name__)


def main() -> int:
    configure_logging()

    # Swap in a staged AppImage replacement before spawning child binaries.
    apply_pending_update()

    # Guard against stale autostart entries if the user explicitly turned AW off.
    if not is_aw_user_enabled():
        log.info("ActivityWatch is disabled; not starting the AW daemon.")
        return 0

    log.info("Running in ActivityWatch background daemon mode (--aw-bg)")
    run_activitywatch_daemon()
    return 0


if __name__ == "__main__":
    sys.exit(main())
