"""Atomic JSON file operations with corruption recovery.

Provides atomic write (temp file + fsync + rename) and safe load
that returns None on corruption instead of silently returning {}.
"""

import json
import logging
import os
import tempfile
import shutil
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from system_sl.utils.data_health import DataIssue, record_issue


log = logging.getLogger(__name__)


class LoadStatus(str, Enum):
    """Outcome of reading a JSON data file."""

    OK = "ok"
    MISSING = "missing"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    UNREADABLE = "unreadable"


def atomic_write_json(filepath: str | Path, data: dict | list) -> None:
    """Write JSON atomically: temp file + fsync + rename.

    Args:
        filepath: Destination file path.
        data: Dictionary or list to serialize.

    Raises:
        OSError: On write/fsync/rename failure (caller should handle).
        TypeError: If data is not JSON-serializable.
    """
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write to temp file in same directory (ensures same filesystem for atomic rename)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as tf:
        temp_name = tf.name
        try:
            json.dump(data, tf, indent=4, ensure_ascii=False)
            tf.flush()
            os.fsync(tf.fileno())  # Force to disk
        except BaseException:
            tf.close()
            _discard(temp_name)
            raise

    try:
        # Atomic replace on POSIX; on Windows, replace() is also atomic for same-volume
        os.replace(temp_name, path)
    except BaseException:
        _discard(temp_name)
        raise
    _fsync_dir(path.parent)


def _discard(name: str) -> None:
    """Remove a leftover temp file, ignoring one that is already gone."""
    try:
        os.unlink(name)
    except OSError:
        pass


def _fsync_dir(directory: Path) -> None:
    """Flush the rename itself to disk, where the platform supports it.

    Without this a power cut right after the write can bring back the old
    file. Windows cannot open directories, so it is skipped there.
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def corrupt_backup_path(path: Path, now: datetime | None = None) -> Path:
    """Return an unused ``<file>.corrupt-YYYYmmdd-HHMMSS[-n]`` path for ``path``.

    A fixed ``<file>.corrupt`` name meant a second corruption overwrote the
    first backup, so every quarantine gets its own timestamped name.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    base = path.with_name(f"{path.name}.corrupt-{stamp}")
    candidate = base
    n = 1
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{n}")
        n += 1
    return candidate


def load_json_with_status(
    filepath: str | Path,
) -> tuple[LoadStatus, Any, Path | None]:
    """Load JSON and report why a read produced no data.

    Does NOT catch OSError (permission denied, etc.) — those propagate so the
    caller can report an unreadable file.

    On JSONDecodeError or bytes that are not UTF-8, moves the corrupt file to a unique
    ``<filepath>.corrupt-<timestamp>`` before returning ``LoadStatus.CORRUPT``,
    so the user can recover manually.

    Returns:
        ``(status, data, corrupt_backup)``. ``data`` is the parsed value for
        ``OK`` and None otherwise; ``corrupt_backup`` is the quarantine path
        only for ``CORRUPT`` and only when the backup really exists (None when
        the move failed and the original was left in place).
    """
    path = Path(filepath)

    if not path.exists():
        return LoadStatus.MISSING, None, None

    with open(path, "rb") as f:
        raw = f.read()

    try:
        content = raw.decode("utf-8").strip()
        if not content:
            return LoadStatus.EMPTY, None, None
        return LoadStatus.OK, json.loads(content), None
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Bytes that are not UTF-8 are as broken as bad JSON: quarantine them
        # too, rather than letting the error escape every reader.
        # Back up corrupt file for recovery
        corrupt_backup: Path | None = corrupt_backup_path(path)
        detail = ""
        try:
            shutil.move(str(path), str(corrupt_backup))
        except OSError as e:
            # The original stays in place. Never claim a copy that does not
            # exist: report the file as left where it was instead.
            log.warning("Could not quarantine corrupt file %s: %s", path, e)
            corrupt_backup = None
            detail = str(e)
        if corrupt_backup is not None and not corrupt_backup.exists():
            corrupt_backup = None
        record_issue(DataIssue("corrupt", path, corrupt_backup, detail=detail))
        return LoadStatus.CORRUPT, None, corrupt_backup


def safe_load_json(filepath: str | Path) -> dict | list | None:
    """Load JSON from file; return None on corruption or missing file.

    Does NOT catch OSError (permission denied, etc.) — those propagate.
    Only catches JSONDecodeError and empty-file cases.

    On JSONDecodeError, backs up the corrupt file to
    ``<filepath>.corrupt-<timestamp>`` before returning None, so user can recover manually.

    Args:
        filepath: Source file path.

    Returns:
        Parsed dict/list, or None if file missing/empty/corrupt.
    """
    status, data, _ = load_json_with_status(filepath)
    return data if status is LoadStatus.OK else None


def load_json_or_default(filepath: str | Path, default: dict | list) -> dict | list:
    """Load JSON, returning default if missing/empty/corrupt.

    Convenience wrapper for callers that want a default value.
    """
    data = safe_load_json(filepath)
    return default if data is None else data