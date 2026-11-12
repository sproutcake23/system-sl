"""Process-wide record of data-file problems, for the GUI to surface.

A corrupt file is quarantined deep inside the load path, where there is no
user to tell. Write failures are raised at the GUI call site instead. Both
end up here so the interface can explain what happened without a modal
blocking the event loop.
"""

import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DataIssue:
    """One problem encountered while reading or writing a data file."""

    kind: str  # "corrupt" | "unreadable" | "write_failed"
    path: Path
    corrupt_path: Path | None = None
    detail: str = ""

    def message(self) -> str:
        """Return a sentence the interface can show to the player."""
        if self.kind == "corrupt":
            kept = self.corrupt_path or self.path
            return (
                f"{self.path.name} was unreadable, so a copy was kept at {kept}. "
                "Starting from an empty list."
            )
        if self.kind == "unreadable":
            return f"Could not read {self.path.name}: {self.detail}"
        return f"Could not save {self.path.name}: {self.detail}"


_issues: deque[DataIssue] = deque(maxlen=50)
_lock = threading.Lock()


def record_issue(issue: DataIssue) -> None:
    """Queue an issue for the next GUI drain."""
    with _lock:
        _issues.append(issue)


def pop_issues() -> list[DataIssue]:
    """Take every queued issue, leaving the queue empty."""
    with _lock:
        issues = list(_issues)
        _issues.clear()
    return issues
