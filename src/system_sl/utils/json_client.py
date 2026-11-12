"""Module for raw JSON serialization and deserialization file operations.

Uses atomic writes (temp file + fsync + rename) and safe loads that
report why a read produced nothing instead of silently returning {}.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from system_sl.utils.atomic_json import (
    LoadStatus,
    atomic_write_json,
    load_json_with_status,
)
from system_sl.utils.data_health import DataIssue, record_issue


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadResult:
    """What happened when a data file was read.

    Attributes:
        status: Whether the file was read, missing, empty, quarantined as
            corrupt, or unreadable.
        path: The file that was read.
        data: The parsed value, only for ``LoadStatus.OK``.
        error: The OS error text for ``LoadStatus.UNREADABLE``.
        corrupt_path: Where the quarantined copy was moved, if any.
    """

    status: LoadStatus
    path: Path
    data: dict | list | None = None
    error: str | None = None
    corrupt_path: Path | None = None

    @property
    def ok(self) -> bool:
        """True when the file was read successfully."""
        return self.status is LoadStatus.OK


def load_data_result(filepath: str) -> LoadResult:
    """Reads a JSON data file and reports the outcome.

    Unlike :func:`load_data`, this keeps the difference between a missing
    file, an empty one, a quarantined corrupt file, and an unreadable file,
    so callers can tell "nothing saved yet" apart from "your data broke".

    Args:
        filepath (str): The absolute path of the file to load.

    Returns:
        LoadResult: The status plus the parsed data, if any.
    """
    path = Path(filepath)
    try:
        status, data, backup = load_json_with_status(path)
    except OSError as e:
        # Unreadable file (permissions, etc). Reads are non-destructive, so
        # degrade to empty rather than crashing the caller.
        log.warning("Could not read %s: %s", path, e)
        record_issue(DataIssue("unreadable", path, detail=str(e)))
        return LoadResult(LoadStatus.UNREADABLE, path, error=str(e))

    if status is LoadStatus.CORRUPT and backup is not None:
        log.warning("Quarantined corrupt file %s to %s", path, backup)

    return LoadResult(status, path, data=data, corrupt_path=backup)


def load_data(filepath: str) -> dict | list:
    """Reads and parses raw JSON contents from a specified target file.

    Simple wrapper for callers that only need the data: a missing, empty,
    corrupt or unreadable file yields an empty dict. Use
    :func:`load_data_result` when the difference matters.

    Args:
        filepath (str): The absolute path of the file to load.

    Returns:
        dict | list: The parsed data (dict or list). Returns an empty dict if
            the file is missing, empty, corrupt, or unreadable. Corrupt files
            are backed up to ``<file>.corrupt-<timestamp>`` before this returns.
    """
    result = load_data_result(filepath)
    return result.data if result.data is not None else {}


def save_data(filepath: str, data: dict | list) -> None:
    """Serializes and saves a dictionary/list structure directly into a target JSON file.

    Uses atomic write: temp file + fsync + atomic rename.

    Args:
        filepath (str): The destination path where the data should be saved.
        data (dict | list): The data to serialize and write.

    Raises:
        OSError: On write/fsync/rename failure (not swallowed).
        TypeError: If data is not JSON-serializable.
    """
    atomic_write_json(filepath, data)
