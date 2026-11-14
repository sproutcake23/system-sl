"""Deadline-first, pure task scoring.

Nothing in this module reads the clock, loads a model, or touches disk. Every
function takes ``now`` as an argument so a caller can test or replay a ranking
exactly.
"""

from __future__ import annotations

import math
from datetime import datetime

from system_sl.core.ranking_config import (
    DEFAULT_IMPORTANCE,
    IMPORTANT_THRESHOLD,
    NO_DEADLINE_RAMP_DAYS,
    NO_DEADLINE_URGENCY_MAX,
    NO_DEADLINE_URGENCY_MIN,
    OVERDUE_CAP_DAYS,
    QUICK_WIN_FULL_MINUTES,
    QUICK_WIN_NEUTRAL,
    QUICK_WIN_REASON_THRESHOLD,
    QUICK_WIN_ZERO_MINUTES,
    URGENCY_DUE_SCALE,
    URGENT_THRESHOLD,
    WEIGHTS,
)

_SECONDS_PER_DAY = 86400.0
_IMPORTANCE_MIN = 1.0
_IMPORTANCE_MAX = 5.0
_IMPORTANCE_RANGE = _IMPORTANCE_MAX - _IMPORTANCE_MIN


def parse_deadline_local(value) -> datetime | None:
    """Parse a stored deadline into a local, naive ``datetime``.

    A date-only string means the end of that local day. ISO datetimes may carry
    a timezone offset or a trailing ``Z``; aware values are converted to local
    time. Anything unparseable returns ``None`` and never raises.
    """
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        date_only = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        date_only = None
    if date_only is not None:
        return date_only.replace(hour=23, minute=59, second=59)

    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _days(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / _SECONDS_PER_DAY


def urgency(deadline, created_at, now: datetime) -> float:
    """Return urgency in [0, 1] for a deadline, falling back to task age.

    Deadlines ramp down as they approach. Overdue tasks ramp up to 1 but stop
    growing after ``OVERDUE_CAP_DAYS``. Tasks with no usable deadline ramp up
    slowly with age and never cross ``URGENT_THRESHOLD``.
    """
    deadline_dt = parse_deadline_local(deadline)
    if deadline_dt is not None:
        days_left = _days(deadline_dt, now)
        if days_left >= 0:
            return URGENCY_DUE_SCALE / (days_left + 1.0)
        days_overdue = -days_left
        capped = min(days_overdue, OVERDUE_CAP_DAYS)
        return (
            URGENCY_DUE_SCALE
            + (1.0 - URGENCY_DUE_SCALE) * capped / OVERDUE_CAP_DAYS
        )

    created_dt = parse_deadline_local(created_at)
    age_days = 0.0 if created_dt is None else max(0.0, _days(now, created_dt))
    ramp = min(age_days / NO_DEADLINE_RAMP_DAYS, 1.0)
    return (
        NO_DEADLINE_URGENCY_MIN
        + (NO_DEADLINE_URGENCY_MAX - NO_DEADLINE_URGENCY_MIN) * ramp
    )


def quick_win(effort_minutes) -> float:
    """Return how much a task behaves like a quick win, in [0, 1].

    Missing effort is neutral; short tasks score 1 and long tasks score 0,
    with a straight line between the two configured boundaries.
    """
    if effort_minutes is None:
        return QUICK_WIN_NEUTRAL

    try:
        minutes = float(effort_minutes)
    except (TypeError, ValueError):
        return QUICK_WIN_NEUTRAL
    if not math.isfinite(minutes):
        return QUICK_WIN_NEUTRAL

    if minutes <= QUICK_WIN_FULL_MINUTES:
        return 1.0
    if minutes >= QUICK_WIN_ZERO_MINUTES:
        return 0.0

    span = QUICK_WIN_ZERO_MINUTES - QUICK_WIN_FULL_MINUTES
    return (QUICK_WIN_ZERO_MINUTES - minutes) / span


def _importance_value(importance) -> float:
    if importance is None:
        return float(DEFAULT_IMPORTANCE)

    try:
        value = float(importance)
    except (TypeError, ValueError):
        return float(DEFAULT_IMPORTANCE)
    if not math.isfinite(value):
        return float(DEFAULT_IMPORTANCE)

    return min(max(value, _IMPORTANCE_MIN), _IMPORTANCE_MAX)


def importance_norm(importance) -> float:
    """Return the raw 1..5 importance as a 0..1 value."""
    value = _importance_value(importance)
    return (value - _IMPORTANCE_MIN) / _IMPORTANCE_RANGE


def _round_days(value: float) -> int:
    return int(value + 0.5)


def _deadline_reason(deadline_dt: datetime | None, now: datetime) -> str:
    if deadline_dt is None:
        return "no deadline"

    days_left = _days(deadline_dt, now)
    if days_left >= 0:
        days = _round_days(days_left)
        return "due today" if days == 0 else f"due in {days}d"

    days = _round_days(-days_left)
    return "due today" if days == 0 else f"overdue by {days}d"


def _format_importance(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _importance_reason(task: dict, value: float) -> str:
    source = task.get("importance_source")
    if not source:
        label = "default"
    elif source == "suggested":
        label = "suggested"
    else:
        label = "set by you"
    return f"importance {_format_importance(value)} ({label})"


def _quadrant(urgent: bool, important: bool) -> str:
    if urgent and important:
        return "Q1"
    if important:
        return "Q2"
    if urgent:
        return "Q3"
    return "Q4"


def score_task(task: dict, now: datetime) -> dict:
    """Score one task and explain the result.

    The returned ``importance`` is normalized to 0..1; the raw value and its
    source appear in ``reason``.
    """
    deadline = task.get("deadline")
    deadline_dt = parse_deadline_local(deadline)

    urgency_value = urgency(deadline, task.get("created_at"), now)
    quick_win_value = quick_win(task.get("effort_minutes"))
    raw_importance = _importance_value(task.get("importance"))
    normalized_importance = importance_norm(task.get("importance"))

    score = (
        WEIGHTS["urgency"] * urgency_value
        + WEIGHTS["importance"] * normalized_importance
        + WEIGHTS["quick_win"] * quick_win_value
    )

    urgent = urgency_value >= URGENT_THRESHOLD
    important = raw_importance >= IMPORTANT_THRESHOLD

    parts = [
        _deadline_reason(deadline_dt, now),
        _importance_reason(task, raw_importance),
    ]
    if quick_win_value >= QUICK_WIN_REASON_THRESHOLD:
        parts.append("quick win")

    source = task.get("importance_source")
    matched_goal = task.get("matched_goal")
    if source == "suggested" and matched_goal:
        goal_text = str(matched_goal).strip()
        if goal_text:
            parts.append(f"matches goal: {goal_text}")

    return {
        "urgency": urgency_value,
        "importance": normalized_importance,
        "quick_win": quick_win_value,
        "score": score,
        "quadrant": _quadrant(urgent, important),
        "urgent": urgent,
        "important": important,
        "reason": ", ".join(parts),
    }


def _datetime_key(value: datetime) -> tuple:
    return (
        value.toordinal(),
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
    )


def _deadline_sort_key(task: dict) -> tuple:
    deadline_dt = parse_deadline_local(task.get("deadline"))
    if deadline_dt is None:
        return (1,)
    return (0, *_datetime_key(deadline_dt))


def _created_sort_key(task: dict) -> tuple:
    created_dt = parse_deadline_local(task.get("created_at"))
    if created_dt is None:
        return (0,)
    return (1, *_datetime_key(created_dt))


def rank_tasks(tasks: list[dict], now: datetime) -> list[dict]:
    """Return copies of ``tasks`` merged with their scores, best first.

    Equal scores prefer the earlier deadline (tasks without a deadline go
    last), then the older ``created_at`` (missing values count as oldest).
    """
    ranked = [{**task, **score_task(task, now)} for task in tasks]
    ranked.sort(
        key=lambda item: (
            -item["score"],
            _deadline_sort_key(item),
            _created_sort_key(item),
        )
    )
    return ranked