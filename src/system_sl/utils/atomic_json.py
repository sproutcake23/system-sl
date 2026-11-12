"""Atomic JSON file operations with corruption recovery.

Provides atomic write (temp file + fsync + rename) and safe load
that returns None on corruption instead of silently returning {}.
"""

import json
import os
import tempfile
import shutil
from pathlib import Path
from typing import Any


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
        json.dump(data, tf, indent=4, ensure_ascii=False)
        tf.flush()
        os.fsync(tf.fileno())  # Force to disk
        temp_name = tf.name

    # Atomic replace on POSIX; on Windows, replace() is also atomic for same-volume
    os.replace(temp_name, path)


def safe_load_json(filepath: str | Path) -> dict | list | None:
    """Load JSON from file; return None on corruption or missing file.

    Does NOT catch OSError (permission denied, etc.) — those propagate.
    Only catches JSONDecodeError and empty-file cases.

    On JSONDecodeError, backs up the corrupt file to <filepath>.corrupt
    before returning None, so user can recover manually.

    Args:
        filepath: Source file path.

    Returns:
        Parsed dict/list, or None if file missing/empty/corrupt.
    """
    path = Path(filepath)

    if not path.exists():
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except OSError:
        # Permission error, etc. — let caller decide
        raise

    if not content:
        return None

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Back up corrupt file for recovery
        corrupt_backup = path.with_suffix(path.suffix + ".corrupt")
        try:
            shutil.move(str(path), str(corrupt_backup))
        except OSError:
            pass  # Best effort; original stays in place
        return None


def load_json_or_default(filepath: str | Path, default: dict | list) -> dict | list:
    """Load JSON, returning default if missing/empty/corrupt.

    Convenience wrapper for callers that want a default value.
    """
    data = safe_load_json(filepath)
    return default if data is None else data