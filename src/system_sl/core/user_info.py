"""Storage helpers for the Player's free-form goal keywords.

These are library functions, not an interactive CLI: the GUI owns any prompts.
The old version called ``input()`` at import/use time, which froze headless
builds and the GUI process.
"""

from __future__ import annotations

from typing import Iterable

from system_sl.core.tasks import load_data, save_data
from system_sl.utils import get_tasks_file_path

USER_INFO_FILE_PATH = get_tasks_file_path("user_info.json")


def load_user_goals(user_info_path: str = USER_INFO_FILE_PATH) -> list[str]:
    """Return the stored goal keywords (empty when there are none)."""
    data = load_data(user_info_path)
    if not isinstance(data, dict):
        return []
    goals = data.get("goal")
    if not isinstance(goals, list):
        return []
    return [str(goal) for goal in goals if str(goal).strip()]


def save_user_goals(
    goals: Iterable[str], user_info_path: str = USER_INFO_FILE_PATH
) -> list[str]:
    """Persist goal keywords, dropping blanks and duplicates."""
    cleaned: list[str] = []
    for goal in goals:
        text = str(goal).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    save_data(user_info_path, {"goal": cleaned})
    return cleaned


def user_goal_check(user_info_path: str = USER_INFO_FILE_PATH) -> list[str]:
    """Return stored goals without prompting."""
    return load_user_goals(user_info_path)


def user_edit_goal(
    user_info_path: str = USER_INFO_FILE_PATH,
    *,
    add: str | None = None,
    remove: str | None = None,
) -> list[str]:
    """Add/remove one keyword and return the resulting list."""
    goals = load_user_goals(user_info_path)
    if remove:
        goals = [goal for goal in goals if goal != remove]
    if add and add not in goals:
        goals.append(add)
    return save_user_goals(goals, user_info_path)