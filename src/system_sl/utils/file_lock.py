"""Cross-process file locking for task data mutations.

The GUI process and the ``--bg`` notifier process both mutate
``tasks.json`` / ``task_order.json``. Neither used to coordinate, so a
read-modify-write in one could clobber the other's update. This module
provides a single advisory lock keyed on a dedicated lock file that both
processes acquire around read-modify-write sequences.

POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking``.

The lock is reentrant *within a process*: nesting ``file_lock`` on the same
lock path (e.g. ``mark_task_completed`` calling ``remove_tasks``) only takes
the OS lock once. The OS lock itself is per-process on POSIX, so a second
``flock`` would deadlock a single-threaded caller otherwise.
"""

import os
import threading
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


# Per-thread nesting depth per lock path. The OS lock is held by the
# innermost (depth 0 → 1) acquisition and released when depth returns to 0.
_local = threading.local()


def _depths():
    depths = getattr(_local, "depths", None)
    if depths is None:
        depths = {}
        _local.depths = depths
    return depths


@contextmanager
def file_lock(lock_path: str | Path):
    """Acquire an exclusive advisory lock for the duration of the context.

    Reentrant per process/thread: nested acquisitions of the same path reuse
    the already-held OS lock instead of blocking on it.

    Args:
        lock_path: Path to a lock file (created if missing). The file's
            contents are never read or written; only the OS lock matters.
    """
    key = os.path.abspath(str(lock_path))
    depths = _depths()
    depth = depths.get(key, 0)

    if depth > 0:
        # Already held by this thread — just bump the depth.
        depths[key] = depth + 1
        try:
            yield
        finally:
            depths[key] -= 1
        return

    path = Path(key)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    depths[key] = 1
    try:
        if os.name == "nt":
            # msvcrt.locking locks a byte range; lock the first byte.
            # LK_LOCK blocks and retries for ~10 s then raises.
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            depths[key] = 0
            os.close(fd)