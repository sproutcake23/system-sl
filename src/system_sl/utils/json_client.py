"""Module for raw JSON serialization and deserialization file operations.

Uses atomic writes (temp file + fsync + rename) and safe loads that
return None on corruption instead of silently returning {}.
"""

from system_sl.utils.atomic_json import atomic_write_json, safe_load_json


def load_data(filepath: str) -> dict | list:
    """Reads and parses raw JSON contents from a specified target file.

    Args:
        filepath (str): The absolute path of the file to load.

    Returns:
        dict | list: The parsed data (dict or list). Returns an empty dict if
            the file is missing, empty, or corrupt. Corrupt files are backed
            up to ``<file>.corrupt`` before an empty dict is returned.
    """
    try:
        data = safe_load_json(filepath)
    except OSError as e:
        # Unreadable file (permissions, etc). Reads are non-destructive, so
        # degrade to empty rather than crashing the caller.
        print(f"Could not read {filepath}: {e}")
        return {}
    if data is None:
        # File missing, empty, or corrupt (backed up)
        return {}
    return data


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
