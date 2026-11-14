"""Tests for the deadline-first scoring layer.

Every case uses a fixed ``now`` so the expected numbers never depend on when
the suite runs.
"""

from datetime import datetime, timedelta

import pytest

from system_sl.core.ranking_config import URGENT_THRESHOLD
from system_sl.core.scoring import (
    importance_norm,
    parse_deadline_local,
    quick_win,
    rank_tasks,
    score_task,
    urgency,
)

NOW = datetime(2026, 5, 20, 12, 0, 0)


def _at(**kwargs) -> str:
    return (NOW + timedelta(**kwargs)).isoformat()


# ── Deadlines ────────────────────────────────────────────────────────────────


def test_date_only_deadline_means_end_of_local_day():
    assert parse_deadline_local("2026-05-20") == datetime(2026, 5, 20, 23, 59, 59)


def test_trailing_z_is_utc_converted_to_local_naive_time():
    expected = (
        datetime.fromisoformat("2026-05-20T12:00:00+00:00")
        .astimezone()
        .replace(tzinfo=None)
    )
    assert parse_deadline_local("2026-05-20T12:00:00Z") == expected


def test_offset_datetime_is_converted_to_local_naive_time():
    expected = (
        datetime.fromisoformat("2026-05-20T12:00:00+05:30")
        .astimezone()
        .replace(tzinfo=None)
    )
    assert parse_deadline_local("2026-05-20T12:00:00+05:30") == expected


def test_naive_datetime_is_kept_as_local():
    assert parse_deadline_local("2026-05-20T12:00:00") == datetime(2026, 5, 20, 12)
    assert parse_deadline_local("2026-05-20 12:00") == datetime(2026, 5, 20, 12)


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "garbage", "2026-02-30", object()],
)
def test_unparseable_deadlines_return_none_without_raising(value):
    assert parse_deadline_local(value) is None


# ── Urgency ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("days_left", "expected"),
    [
        (0, 0.8),
        (1, 0.4),
        (3, 0.2),
        (7, 0.1),
    ],
)
def test_urgency_counts_down_to_the_deadline(days_left, expected):
    assert urgency(_at(days=days_left), None, NOW) == pytest.approx(expected)


def test_urgency_uses_fractional_days():
    assert urgency(_at(hours=12), None, NOW) == pytest.approx(0.8 / 1.5)


@pytest.mark.parametrize(
    ("days_overdue", "expected"),
    [
        (1, 0.8 + 0.2 / 7),
        (30, 1.0),
    ],
)
def test_urgency_ramps_up_when_overdue_and_stops_at_the_cap(
    days_overdue, expected
):
    assert urgency(_at(days=-days_overdue), None, NOW) == pytest.approx(expected)


@pytest.mark.parametrize("age_days", [0, 7, 14, 100])
def test_no_deadline_urgency_ramps_with_age_and_stays_below_urgent(age_days):
    value = urgency(None, _at(days=-age_days), NOW)
    assert 0.05 <= value <= 0.20
    assert value == pytest.approx(min(0.05 + 0.15 * min(age_days / 14, 1), 0.20))
    assert value < URGENT_THRESHOLD


def test_missing_created_at_and_garbage_deadline_behave_like_no_deadline():
    assert urgency(None, None, NOW) == pytest.approx(0.05)
    assert urgency("garbage", None, NOW) == pytest.approx(0.05)
    assert urgency("garbage", _at(days=-14), NOW) == pytest.approx(0.20)


# ── Quick win and importance ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("effort_minutes", "expected"),
    [
        (None, 0.5),
        (0, 1.0),
        (30, 1.0),
        (135, 0.5),
        (240, 0.0),
        (500, 0.0),
    ],
)
def test_quick_win_boundaries(effort_minutes, expected):
    assert quick_win(effort_minutes) == pytest.approx(expected)


def test_quick_win_is_linear_between_the_boundaries():
    assert quick_win(72) == pytest.approx(0.8)


@pytest.mark.parametrize(
    ("importance", "expected"),
    [
        (None, 0.5),
        (1, 0.0),
        (2, 0.25),
        (3, 0.5),
        (5, 1.0),
        (0, 0.0),
        (7, 1.0),
    ],
)
def test_importance_is_clamped_and_normalized(importance, expected):
    assert importance_norm(importance) == pytest.approx(expected)


# ── Task scores ──────────────────────────────────────────────────────────────


def test_empty_task_uses_all_defaults():
    result = score_task({}, NOW)

    assert set(result) == {
        "urgency",
        "importance",
        "quick_win",
        "score",
        "quadrant",
        "urgent",
        "important",
        "reason",
    }
    assert result["urgency"] == pytest.approx(0.05)
    assert result["importance"] == pytest.approx(0.5)
    assert result["quick_win"] == pytest.approx(0.5)
    assert result["score"] == pytest.approx(0.275)
    assert result["quadrant"] == "Q2"
    assert result["urgent"] is False
    assert result["important"] is True
    assert result["reason"] == "no deadline, importance 3 (default)"


@pytest.mark.parametrize(
    ("task", "quadrant", "urgent", "important"),
    [
        (
            {"deadline": _at(), "importance": 4, "effort_minutes": 30},
            "Q1",
            True,
            True,
        ),
        (
            {"deadline": _at(days=7), "importance": 4, "effort_minutes": 240},
            "Q2",
            False,
            True,
        ),
        (
            {"deadline": _at(), "importance": 1, "effort_minutes": 30},
            "Q3",
            True,
            False,
        ),
        (
            {"deadline": _at(days=7), "importance": 1, "effort_minutes": 240},
            "Q4",
            False,
            False,
        ),
    ],
)
def test_each_quadrant(task, quadrant, urgent, important):
    result = score_task(task, NOW)
    assert result["quadrant"] == quadrant
    assert result["urgent"] is urgent
    assert result["important"] is important


# ── Reasons ──────────────────────────────────────────────────────────────────


def test_reason_overdue_and_set_by_you():
    task = {
        "deadline": _at(days=-2),
        "importance": 4,
        "importance_source": "user",
        "effort_minutes": 240,
    }
    assert score_task(task, NOW)["reason"] == (
        "overdue by 2d, importance 4 (set by you)"
    )


def test_reason_due_today():
    task = {
        "deadline": _at(hours=2),
        "importance": 4,
        "importance_source": "user",
        "effort_minutes": 240,
    }
    assert score_task(task, NOW)["reason"] == (
        "due today, importance 4 (set by you)"
    )


def test_reason_due_in_three_days():
    task = {
        "deadline": _at(days=3),
        "importance": 4,
        "importance_source": "user",
        "effort_minutes": 240,
    }
    assert score_task(task, NOW)["reason"] == (
        "due in 3d, importance 4 (set by you)"
    )


def test_reason_default_label_when_source_is_missing():
    task = {"importance": 4, "effort_minutes": 240}
    assert score_task(task, NOW)["reason"] == (
        "no deadline, importance 4 (default)"
    )


def test_reason_suggested_goal_and_quick_win():
    task = {
        "deadline": _at(days=3),
        "importance": 4,
        "importance_source": "suggested",
        "matched_goal": "finish thesis",
        "effort_minutes": 30,
    }
    assert score_task(task, NOW)["reason"] == (
        "due in 3d, importance 4 (suggested), quick win, "
        "matches goal: finish thesis"
    )


def test_matches_goal_is_only_shown_for_suggested_importance():
    task = {
        "importance_source": "user",
        "matched_goal": "finish thesis",
        "effort_minutes": 240,
    }
    assert "matches goal" not in score_task(task, NOW)["reason"]


def test_quick_win_reason_starts_at_the_threshold():
    at_threshold = {"importance": 3, "effort_minutes": 72}
    below_threshold = {"importance": 3, "effort_minutes": 73}

    assert "quick win" in score_task(at_threshold, NOW)["reason"]
    assert "quick win" not in score_task(below_threshold, NOW)["reason"]


def test_garbage_deadline_reports_no_deadline():
    task = {"deadline": "garbage", "importance": 3, "effort_minutes": 240}
    assert score_task(task, NOW)["reason"] == (
        "no deadline, importance 3 (default)"
    )


# ── Ranking and ties ─────────────────────────────────────────────────────────


def test_equal_scores_prefer_the_earlier_deadline():
    early = {
        "name": "early",
        "deadline": _at(days=1),
        "importance": 1,
        "effort_minutes": 240,
    }
    late = {
        "name": "late",
        "deadline": _at(days=3),
        "importance": 1,
        "effort_minutes": 100,
    }
    assert score_task(early, NOW)["score"] == score_task(late, NOW)["score"]

    ranked = rank_tasks([late, early], NOW)

    assert [task["name"] for task in ranked] == ["early", "late"]


def test_equal_scores_put_tasks_without_a_deadline_last():
    dated = {
        "name": "dated",
        "deadline": _at(),
        "importance": 1,
        "effort_minutes": 240,
    }
    undated = {
        "name": "undated",
        "created_at": _at(),
        "importance": 5,
        "effort_minutes": 205,
    }
    assert score_task(dated, NOW)["score"] == score_task(undated, NOW)["score"]

    ranked = rank_tasks([undated, dated], NOW)

    assert [task["name"] for task in ranked] == ["dated", "undated"]


def test_equal_scores_and_deadlines_prefer_the_older_created_at():
    older = {
        "name": "older",
        "deadline": _at(days=3),
        "created_at": _at(days=-10),
        "importance": 4,
        "effort_minutes": 240,
    }
    newer = {
        "name": "newer",
        "deadline": _at(days=3),
        "created_at": _at(days=-2),
        "importance": 4,
        "effort_minutes": 240,
    }
    assert score_task(older, NOW)["score"] == score_task(newer, NOW)["score"]

    ranked = rank_tasks([newer, older], NOW)

    assert [task["name"] for task in ranked] == ["older", "newer"]


def test_missing_created_at_counts_as_oldest():
    missing = {
        "name": "missing",
        "deadline": _at(days=3),
        "importance": 4,
        "effort_minutes": 240,
    }
    present = {
        "name": "present",
        "deadline": _at(days=3),
        "created_at": _at(),
        "importance": 4,
        "effort_minutes": 240,
    }
    assert score_task(missing, NOW)["score"] == score_task(present, NOW)["score"]

    ranked = rank_tasks([present, missing], NOW)

    assert [task["name"] for task in ranked] == ["missing", "present"]


def test_rank_tasks_merges_scores_without_mutating_inputs():
    task = {"name": "one", "importance": 4, "effort_minutes": 30}
    original = dict(task)

    ranked = rank_tasks([task], NOW)

    assert task == original
    assert ranked[0] is not task
    assert ranked[0]["name"] == "one"
    assert ranked[0]["quadrant"] == "Q2"
    assert ranked[0]["score"] == score_task(task, NOW)["score"]