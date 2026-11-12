"""The CONTEXT block that is injected into the System's prompt every turn.

The block carries the three things the model cannot guess: the current date
and time, who the Player is, and what the Player is working on. It is
rebuilt for every model call and appended to the *system* message — never to
the Player's message. Anything written into a user message is stored in the
conversation history forever, and this block changes every turn.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


# How many open tasks the block lists. Enough to answer "what should I do
# now?" without pasting the whole task list into every prompt.
TOP_TASK_COUNT = 5

# The persona answers, with the label each one gets in the summary.
_PERSONA_LABELS = {
    "IDENTITY": "ambition",
    "ACADEMIC": "studies",
    "SKILLS": "skills",
    "ACTIONS": "wants to spend time on",
    "PROJECTS": "likes building",
    "MILESTONE": "main objective",
    "COGNITION": "enjoys solving",
    "HABITS": "daily habits",
    "INDUSTRY": "target industries",
    "VALUES": "values",
}

# A persona summary is a summary: keep it short enough to read at a glance.
MAX_PERSONA_CHARS = 700

# Quadrant -> priority label, from the ranking engine's output.
_QUADRANT_PRIORITIES = {
    "Q1": "high",
    "Q2": "medium",
    "Q3": "medium",
    "Q4": "low",
}


# =============================================================================
# PERSONA
# =============================================================================

def summarize_persona(data: Any) -> str:
    """Turn the raw persona file into a short, readable summary.

    The persona file is a list of question/answer pairs. The model does not
    need the questionnaire, it needs the answers.
    """
    if not isinstance(data, dict) or not data:
        return "No player profile on file yet."

    name = str(data.get("player_name") or "").strip()
    responses = data.get("responses")
    parts: list[str] = []

    if isinstance(responses, list):
        for item in responses:
            if not isinstance(item, dict):
                continue
            answer = str(item.get("answer") or "").strip()
            if not answer:
                continue
            label = _PERSONA_LABELS.get(str(item.get("category") or "").strip().upper())
            parts.append(f"{label}: {answer}" if label else answer)

    if not parts:
        return f"{name} — no answers recorded yet." if name else "No player profile on file yet."

    summary = "; ".join(parts)
    if len(summary) > MAX_PERSONA_CHARS:
        summary = summary[: MAX_PERSONA_CHARS - 1].rstrip() + "…"
    return f"{name} — {summary}" if name else summary


def load_persona_summary() -> str:
    """Read the persona file and summarize it."""
    try:
        from system_sl.utils import load_data, get_tasks_file_path

        return summarize_persona(load_data(get_tasks_file_path("persona.json")))
    except Exception:
        logger.debug("could not read the persona file", exc_info=True)
        return "No player profile on file yet."


# =============================================================================
# TASKS
# =============================================================================

def _priority_index() -> dict[str, dict]:
    """Return the ranking engine's scores, keyed by task title."""
    try:
        from system_sl.utils import load_data, get_tasks_file_path

        data = load_data(get_tasks_file_path("prioritize.json"))
    except Exception:
        logger.debug("could not read the ranking file", exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    index: dict[str, dict] = {}
    for entry in data.get("all_tasks_ranked") or []:
        if isinstance(entry, dict) and entry.get("title"):
            index[str(entry["title"])] = entry
    return index


def _parse_date(value: Any) -> Optional[date]:
    """Parse a YYYY-MM-DD string; anything else is no deadline."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def load_top_tasks(top_n: int = TOP_TASK_COUNT) -> list[dict]:
    """Return the Player's most relevant open tasks.

    The ranking engine's scores are used when they exist, because they
    already combine urgency, importance and momentum. Without them the tasks
    are ordered by how soon they are due.
    """
    try:
        from system_sl.core import load_tasks

        raw = load_tasks()
    except Exception:
        logger.debug("could not read the task list", exc_info=True)
        return []
    if not isinstance(raw, list):
        return []

    tasks = [t for t in raw if isinstance(t, dict) and t.get("title")]
    if not tasks:
        return []

    priorities = _priority_index()
    today = datetime.now().date()

    def sort_key(task: dict) -> tuple:
        title = str(task.get("title") or "")
        ranked = priorities.get(title)
        if ranked:
            try:
                score = float(ranked.get("P_final") or 0)
            except (TypeError, ValueError):
                score = 0.0
            return (0, -score, title)
        deadline = _parse_date(task.get("deadline"))
        if deadline is None:
            return (1, 0.0, title)
        return (0, float((deadline - today).days), title)

    return sorted(tasks, key=sort_key)[:top_n]


def _task_priority(task: dict, priorities: dict[str, dict]) -> str:
    """Return the priority label of one task."""
    ranked = priorities.get(str(task.get("title") or ""))
    if not ranked:
        return "unranked"
    quadrant = str(ranked.get("quadrant") or "").strip().upper()
    return _QUADRANT_PRIORITIES.get(quadrant, "unranked")


# =============================================================================
# THE BLOCK
# =============================================================================

def build_context_block(
    now: Optional[datetime] = None,
    top_n: int = TOP_TASK_COUNT,
) -> str:
    """Build the CONTEXT block for this turn.

    The block is plain text on purpose: it is read by every model, including
    local ones that follow instructions literally.
    """
    moment = now or datetime.now()
    lines = [
        "[CONTEXT — the Player's current state, refreshed every turn]",
        f"Now: {moment.strftime('%Y-%m-%d %H:%M')} ({moment.strftime('%A')})",
        f"Persona: {load_persona_summary()}",
    ]

    priorities = _priority_index()
    tasks = load_top_tasks(top_n)
    if tasks:
        lines.append(f"Open tasks (top {len(tasks)}):")
        for index, task in enumerate(tasks, start=1):
            title = str(task.get("title") or "")
            deadline = task.get("deadline")
            when = f"deadline {deadline}" if deadline else "no deadline"
            priority = _task_priority(task, priorities)
            lines.append(f"{index}. {title} — {when} — priority {priority}")
    else:
        lines.append("Open tasks: none.")

    lines.append("[/CONTEXT]")
    return "\n".join(lines)
