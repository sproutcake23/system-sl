"""Tunable constants for the deadline-first scoring layer.

Logic lives in :mod:`system_sl.core.scoring`; every number that may reasonably
be tuned lives here.
"""

WEIGHTS = {"urgency": 0.50, "importance": 0.35, "quick_win": 0.15}

URGENT_THRESHOLD = 0.25
IMPORTANT_THRESHOLD = 3

URGENCY_DUE_SCALE = 0.8
OVERDUE_CAP_DAYS = 7

NO_DEADLINE_URGENCY_MIN = 0.05
NO_DEADLINE_URGENCY_MAX = 0.20
NO_DEADLINE_RAMP_DAYS = 14

QUICK_WIN_FULL_MINUTES = 30
QUICK_WIN_ZERO_MINUTES = 240
QUICK_WIN_NEUTRAL = 0.5

# A task is called out as "quick win" in its reason at or above this value.
QUICK_WIN_REASON_THRESHOLD = 0.8

DEFAULT_IMPORTANCE = 3

# Untuned placeholders on weighted relevance; calibrate before relying on them.
# relevance < 0.10 -> 1, 0.10-0.20 -> 2, 0.20-0.30 -> 3, 0.30-0.40 -> 4, >= 0.40 -> 5
IMPORTANCE_BANDS = [0.10, 0.20, 0.30, 0.40]
IMPACT_WEIGHTS = {1: 0.33, 2: 0.67, 3: 1.0}

QUADRANTS = {
    "Q1": {
        "name": "DO NOW",
        "icon": "⚡",
        "subtitle": "High pull, High importance",
    },
    "Q2": {
        "name": "SCHEDULE",
        "icon": "📅",
        "subtitle": "Low pull, High importance",
    },
    "Q3": {
        "name": "DEFER",
        "icon": "⏩",
        "subtitle": "High pull, Low importance",
    },
    "Q4": {
        "name": "CONSIDER DROPPING",
        "icon": "🗑",
        "subtitle": "Low pull, Low importance",
    },
}