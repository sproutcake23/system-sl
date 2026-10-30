"""Environment for launching host programs from the frozen app."""

import os
import sys


def host_env() -> dict[str, str]:
    """Return ``os.environ`` without what the PyInstaller bundle injected.

    The onefile bootloader points ``LD_LIBRARY_PATH`` at the unpacked bundle,
    and runtime hooks point ``QT_PLUGIN_PATH``, ``PATH``, ``XDG_DATA_DIRS``
    and friends into it. A host program that inherits them (a browser,
    ``xdg-open``, ``kbuildsycoca6``) loads our bundled Qt and libraries
    instead of its own and fails to start. From source this is a plain copy.
    """
    env = os.environ.copy()
    if not getattr(sys, "frozen", False):
        return env

    # The bootloader saves the user's own value before overriding it.
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
    else:
        env.pop("LD_LIBRARY_PATH", None)

    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        for key, value in list(env.items()):
            if bundle not in value:
                continue
            kept = [
                part for part in value.split(os.pathsep)
                if part and not part.startswith(bundle)
            ]
            if kept:
                env[key] = os.pathsep.join(kept)
            else:
                del env[key]
    return env
