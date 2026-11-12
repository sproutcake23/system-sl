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


# What each data file holds, and what the app falls back to when it cannot be
# read: (label, consequence). Files not listed get a generic wording.
_FILE_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "tasks.json": ("Task data", "Starting from an empty task list."),
    "completed_tasks.json": (
        "Completed-task history",
        "Starting with no completed tasks.",
    ),
    "task_order.json": ("Task order", "Your manual task order was reset."),
    "chat_conversations.json": (
        "Chat history",
        "Starting with an empty chat history.",
    ),
    "llm_config.json": ("Chat settings", "Default chat settings are being used."),
    "settings.json": ("Settings", "Default settings are being used."),
    "persona.json": (
        "Player profile",
        "Tasks are ranked without a player profile until it is restored.",
    ),
    "user_info.json": (
        "Player profile",
        "Tasks are ranked without a player profile until it is restored.",
    ),
}
_GENERIC_DESCRIPTION = ("Data file", "Default values are being used instead.")


def describe_file(path: Path) -> tuple[str, str]:
    """Return ``(label, consequence)`` describing what ``path`` holds."""
    return _FILE_DESCRIPTIONS.get(path.name, _GENERIC_DESCRIPTION)


@dataclass(frozen=True)
class DataIssue:
    """One problem encountered while reading or writing a data file."""

    kind: str  # "corrupt" | "unreadable" | "write_failed"
    path: Path
    # Set only when a quarantined copy really exists at this path.
    corrupt_path: Path | None = None
    detail: str = ""

    def title(self) -> str:
        """Return a short heading for a dialog about this issue."""
        label, _ = describe_file(self.path)
        if self.kind == "corrupt":
            return f"{label} recovered" if self.corrupt_path else f"{label} damaged"
        if self.kind == "unreadable":
            return f"{label} unreadable"
        return f"{label} not saved"

    def message(self) -> str:
        """Return a sentence the interface can show to the player."""
        _, consequence = describe_file(self.path)
        name = self.path.name
        if self.kind == "corrupt":
            if self.corrupt_path is not None:
                return (
                    f"{name} was damaged, so it was moved aside to "
                    f"{self.corrupt_path}. {consequence}"
                )
            reason = f" ({self.detail})" if self.detail else ""
            return (
                f"{name} is damaged and could not be moved aside{reason}; "
                f"it was left at {self.path}. {consequence}"
            )
        if self.kind == "unreadable":
            return f"Could not read {name}: {self.detail}. {consequence}"
        return f"Could not save {name}: {self.detail}"

    def advice(self) -> str:
        """Return follow-up text explaining how the player can recover."""
        if self.kind == "corrupt":
            if self.corrupt_path is not None:
                return (
                    "The damaged copy was kept so you can inspect or repair it. "
                    f"Move it back to {self.path.name} to restore it."
                )
            return "Repair or remove the file to stop seeing this message."
        if self.kind == "unreadable":
            return (
                "The file was not changed. Check its permissions, then restart "
                "the app."
            )
        return "Your latest change may be lost when the app closes."

    def dedupe_key(self) -> str | None:
        """Identify this event so a dialog is shown once per occurrence.

        Each quarantine has its own backup path, so a repeat corruption of the
        same file is a new event. A file that could not be moved, or cannot be
        read, is reported again on every load; those are keyed on the file so
        the player is not shown the same dialog over and over. Write failures
        are reported where they happen and get no dialog (None).
        """
        if self.kind == "corrupt":
            if self.corrupt_path is not None:
                return f"corrupt:{self.corrupt_path}"
            return f"corrupt-in-place:{self.path}"
        if self.kind == "unreadable":
            return f"unreadable:{self.path}:{self.detail}"
        return None


_issues: deque[DataIssue] = deque(maxlen=50)
_lock = threading.Lock()


def record_issue(issue: DataIssue) -> None:
    """Queue an issue for the next GUI drain."""
    with _lock:
        _issues.append(issue)


def has_issues() -> bool:
    """True when at least one issue is waiting to be drained."""
    with _lock:
        return bool(_issues)


def pop_issues() -> list[DataIssue]:
    """Take every queued issue, leaving the queue empty."""
    with _lock:
        issues = list(_issues)
        _issues.clear()
    return issues


def combine_messages(issues: list[DataIssue]) -> str:
    """Join the messages of ``issues`` into one status text, without repeats."""
    seen: list[str] = []
    for issue in issues:
        text = issue.message()
        if text not in seen:
            seen.append(text)
    return "\n".join(seen)
