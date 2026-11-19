"""
╔══════════════════════════════════════════════════════════════════════╗
║  src/system_sl/core/priority_engine.py                               ║
║  Mathematical Priority Engine (Refactored)                           ║
╠══════════════════════════════════════════════════════════════════════╣
║  Features:                                                           ║
║  1. No Repetition: Uses Setup() & Fusion() from vector_converter.    ║
║  2. Class-Based: Grouped by responsibility (Analyzer, Context, etc). ║
║  3. Pluggable Strategies: the weight optimiser can be swapped        ║
║     without breaking the core gravity logic.                         ║
║  4. Dev-Friendly: Clean separation of concerns and type hinting.     ║
╚══════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import json
import logging
import threading
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

# ── Import Shared Dependencies to avoid repetition ──────────────────────
from system_sl.core import scoring
from system_sl.core.importance import load_goals, suggest_importance
from system_sl.core.ranking_config import QUADRANTS
from system_sl.core.vector_converter import Setup, Fusion, encode, get_vector_dim
from system_sl.utils import get_tasks_file_path
from system_sl.utils.atomic_json import atomic_write_json

log = logging.getLogger(__name__)

# Where the newest ranking snapshot lives. Kept as a module constant so tests
# can point it at a temporary config directory without touching the real one.
PRIORITIZE_FILE_PATH = get_tasks_file_path("prioritize.json")


# ── Shared SpellChecker ─────────────────────────────────────────────────
# Building the dictionary is expensive, and PriorityPipeline is constructed
# on every ranking run, so the checker is process-wide like the embedding
# model in Setup.
# pyspellchecker only reads its word frequencies after construction, which
# makes one shared instance safe for the GUI and the ranking worker.
_CUSTOM_WORDS = [
    "api",
    "json",
    "frontend",
    "backend",
    "refactor",
    "repo",
    "github",
    "spacy",
    "ui",
    "ux",
    "sql",
    "css",
    "html",
]

_spellchecker = None
_spellchecker_lock = threading.Lock()


def get_spellchecker():
    """Return the shared SpellChecker, loading it on first use."""
    global _spellchecker
    if _spellchecker is None:
        with _spellchecker_lock:
            if _spellchecker is None:
                from spellchecker import SpellChecker

                checker = SpellChecker()
                checker.word_frequency.load_words(_CUSTOM_WORDS)
                _spellchecker = checker
    return _spellchecker


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — TASK ANALYZER (Text & Deadline Features)
# ══════════════════════════════════════════════════════════════════════════════


class TaskAnalyzer:
    """Handles parsing and NLP inferences for individual tasks."""

    _HEAVY_VERBS = {
        "research",
        "design",
        "architect",
        "implement",
        "analyze",
        "analyse",
        "debug",
        "optimize",
        "optimise",
        "write",
        "build",
        "learn",
        "study",
        "develop",
        "create",
        "refactor",
        "model",
        "investigate",
        "solve",
        "understand",
        "evaluate",
        "construct",
        "engineer",
        "plan",
    }

    _LIGHT_VERBS = {
        "check",
        "reply",
        "send",
        "update",
        "review",
        "read",
        "call",
        "schedule",
        "buy",
        "clean",
        "organize",
        "organise",
        "email",
        "message",
        "remind",
        "pay",
        "order",
        "book",
        "confirm",
        "notify",
        "post",
        "upload",
        "download",
        "print",
        "submit",
        "fill",
    }

    def __init__(self):
        self.setup = Setup()
        # The embedding model (all-MiniLM-L6-v2) is loaded lazily on first
        # use, so constructing the engine stays cheap until ranking runs.

    def _get_spell(self):
        """Return the process-wide SpellChecker (loaded once per process)."""
        return get_spellchecker()

    @staticmethod
    def _verb_forms(word: str) -> set[str]:
        """Return likely base forms for an inflected word.

        spaCy's lemmatizer is gone, so this folds the common English
        inflections back to the forms used by ``_HEAVY_VERBS`` and
        ``_LIGHT_VERBS``: ``writing -> write``, ``studies -> study``,
        ``submitting -> submit``, ``updates -> update``.
        """
        forms: set[str] = set()
        stems = []
        if word.endswith("ies") and len(word) > 4:
            forms.add(word[:-3] + "y")
        if word.endswith("ing") and len(word) > 5:
            stems.append(word[:-3])
        if word.endswith("ed") and len(word) > 4:
            stems.append(word[:-2])
        if word.endswith("es") and len(word) > 4:
            stems.append(word[:-2])
        if word.endswith("s") and len(word) > 3:
            stems.append(word[:-1])
        for stem in stems:
            forms.add(stem)
            forms.add(stem + "e")
            if len(stem) > 3 and stem[-1] == stem[-2]:
                forms.add(stem[:-1])
        return forms

    @classmethod
    def _tokenize(cls, title: str) -> set[str]:
        """Split a title into raw words and their likely base forms.

        This replaces spaCy's POS tagging and lemmatizer: the heavy/light verb
        sets only need to know which verbs appear, not their grammatical role.
        """
        words: set[str] = set()
        for raw in title.replace("-", " ").replace("_", " ").split():
            word = "".join(ch for ch in raw if ch.isalpha())
            if not word:
                continue
            words.add(word)
            words.update(cls._verb_forms(word))
        return words
   
    def correct_typos(self, text: str) -> str:
        """Fixes spelling mistakes in a string before NLP processing."""
        if not text:
            return ""

        words = text.split()
        corrected_words = []

        for word in words:
            # Get the single most likely correction
            correction = self._get_spell().correction(word)

            # If spellchecker returns None (word completely unknown) or the correction,
            # we use it, otherwise fallback to original word
            if correction:
                corrected_words.append(correction)
            else:
                corrected_words.append(word)

        return " ".join(corrected_words)

    def parse_deadline(self, deadline_str: Optional[str]) -> Optional[datetime]:
        """Robustly parse any deadline format into a datetime object."""
        # BUG: Check it works for google calender api fetch or not
        if not deadline_str:
            return None

        s = str(deadline_str).replace("Z", "").replace("+00:00", "").strip()
        for fmt in [
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d",
        ]:
            try:
                return datetime.strptime(s[: len(fmt) + 2].strip(), fmt)
            except ValueError:
                continue
        return None

    def infer_cognitive_weight(self, title: str) -> float:
        """Infer cognitive load of a task from its title.

        Replaces spaCy POS tagging with plain token matching against the
        heavy/light verb sets: the only thing POS tagging was used for was
        deciding which verbs appear in the title.
        """
        try:
            clean_title = self.correct_typos(title.lower())  # Typo correction
            words = self._tokenize(clean_title)

            heavy_hits = len(words & self._HEAVY_VERBS)
            light_hits = len(words & self._LIGHT_VERBS)

            W = 0.5 + (heavy_hits * 0.15) - (light_hits * 0.10)
            return round(max(0.1, min(1.0, W)), 4)
        except Exception as e:
            log.debug(f"Cognitive weight inference failed: {e}")
            return 0.5

    def compute_intrinsic_importance(
        self, title: str, user_vector: np.ndarray
    ) -> float:
        """Compute semantic alignment with the user's vector."""
        try:
            clean_title = self.correct_typos(title.lower())  # typo correction
            task_vec = encode(clean_title).astype(np.float64)

            if not task_vec.any():
                return 0.5

            user_vec = user_vector.astype(np.float64)

            magnitude = np.linalg.norm(task_vec) * np.linalg.norm(user_vec)
            if magnitude == 0:
                return 0.5

            sim = float(np.dot(task_vec, user_vec) / magnitude)
            I = 0.1 + (sim + 1.0) / 2.0 * 0.9  # Map [-1, 1] to [0.1, 1.0]
            return round(float(I), 6)
        except Exception:
            return 0.5

    def compute_time_remaining(self, task: dict, W: float) -> float:
        """Compute days remaining until deadline (or soft window if no deadline)."""
        dl = self.parse_deadline(task.get("deadline"))
        now = datetime.now()

        if dl is not None:
            delta = (dl - now).total_seconds() / 86400
            return max(0.0, delta)

        # Soft deadline logic
        created_str = task.get("created_at", "")
        created_dt = self.parse_deadline(created_str) or now
        days_since = max(0, (now - created_dt).days)

        natural_window = 3 + (W * 2)  # natural window for the non deadline task is 5.
        D = natural_window - days_since
        return max(0.0, D)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — CONTEXT ENGINE (System State Detection)
# ══════════════════════════════════════════════════════════════════════════════


class ContextEngine:
    """tries toDetects user's state to shift the manupluate the claculated W, I AND Dmultipliers."""

    PROFILES = {
        "PRESSURE": {
            "W_mult": 1.0,
            "I_mult": 0.6,
            "D_compress": 0.5,
            "desc": "2+ tasks due soon",
        },
        "GROWTH": {
            "W_mult": 1.0,
            "I_mult": 1.8,
            "D_compress": 2.0,
            "desc": "Clear runway",
        },
        "RECOVERY": {
            "W_mult": 0.6,
            "I_mult": 1.0,
            "D_compress": 1.0,
            "desc": "Backlog clearing",
        },
    }

    def __init__(self):
        self.setup = Setup()
        self.completed_file = Path(self.setup._get_file_path("completed_tasks.json"))
        self.analyzer = TaskAnalyzer()

    def detect_context(self, all_tasks: List[dict]) -> str:
        now = datetime.now()
        due_24h, overdue = 0, 0

        for task in all_tasks:
            dl = self.analyzer.parse_deadline(task.get("deadline"))
            if not dl:
                continue

            delta_days = (dl - now).total_seconds() / 86400
            if delta_days <= 1:  # changes to 1
                due_24h += 1
            if dl < now:
                overdue += 1

        if due_24h >= 2:
            return "PRESSURE"
        if overdue >= 3:
            return "RECOVERY"

        completed_today = self._count_completions_today()
        if len(all_tasks) > 5 and completed_today == 0:
            return "RECOVERY"

        return "GROWTH"

    def _count_completions_today(self) -> int:
        if not self.completed_file.exists():
            return 0
        try:
            with open(self.completed_file, "r", encoding="utf-8") as f:
                completed = json.load(f)
            today_str = datetime.now().strftime("%Y-%m-%d")

            count = 0
            if isinstance(completed, list):
                count += sum(
                    1
                    for e in completed
                    if isinstance(e, dict) and e.get("completed_at") == today_str
                )
            return count
        except Exception:
            return 0


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — PLUGGABLE OPTIMIZERS (Rule 3)
# ══════════════════════════════════════════════════════════════════════════════


class BaseWeightOptimizer:
    """Fixed, equal signal weights.

    A Thompson-sampling optimiser used to sit beside this one, but the
    ranking never consumed its weights and nothing trained it, so it was
    removed. A learning optimiser would have to feed ``get_weights()`` into
    the gravity blend to have any effect.
    """

    def get_confidence(self) -> float:
        return 1.0  # Default to full confidence in context without bandit

    def get_weights(self) -> Dict[str, float]:
        return {"semantic_w": 0.33, "urgency_w": 0.33, "momentum_w": 0.34}

    def record_feedback(self, task_title: str, ranked_tasks: List[dict]) -> None:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — GRAVITY CORE (Math Calculation)
# ══════════════════════════════════════════════════════════════════════════════


class GravityEngine:
    """Calculates physical gravitational pull of tasks."""

    def __init__(self, optimizer: BaseWeightOptimizer = None):
        self.analyzer = TaskAnalyzer()
        self.optimizer = optimizer or BaseWeightOptimizer()  # Pluggable!

    def score_task(
        self, task: dict, user_vector: np.ndarray, context_str: str, ctx_profile: dict
    ) -> dict:
        W = (
            float(task.get("cognitive_weight"))
            if task.get("cognitive_weight") is not None
            else self.analyzer.infer_cognitive_weight(task.get("title", ""))
        )
        I = self.analyzer.compute_intrinsic_importance(
            task.get("title", ""), user_vector
        )
        D = self.analyzer.compute_time_remaining(task, W)

        epsilon = 0.1
        P_base = (W * I) / (D**2 + epsilon)

        # Context Multipliers
        W_eff = W * ctx_profile["W_mult"]
        I_eff = I * ctx_profile["I_mult"]
        D_eff = D * ctx_profile["D_compress"]
        P_context = (W_eff * I_eff) / (D_eff**2 + epsilon)

        # Blend by the optimiser's confidence (always 1.0 with fixed weights)
        conf = self.optimizer.get_confidence()
        P_final = (P_base * (1 - conf)) + (P_context * conf)

        # Derived metrics
        created_dt = (
            self.analyzer.parse_deadline(task.get("created_at", "")) or datetime.now()
        )
        days_old = max(0, (datetime.now() - created_dt).days)

        return {
            "W": round(W, 4),
            "I": round(I, 4),
            "D": round(D, 2),
            "epsilon": epsilon,
            "P_base": round(P_base, 6),
            "P_context": round(P_context, 6),
            "P_final": round(P_final, 6),
            "pull_normalized": 0.0,  # Assigned later
            "semantic_score": round(I, 4),
            "urgency_score": round(1.0 / (1.0 + D), 4),
            "momentum_score": round(min(1.0, days_old / 30.0), 4),
            "context_applied": context_str,
        }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — PRIORITY ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════


class PriorityPipeline:
    """Manages the full flow: loading, scoring, mapping, and saving."""

    QUADRANTS = {
        "Q1": {"name": "DO NOW", "sub": "High pull, High importance", "icon": "⚡"},
        "Q2": {"name": "SCHEDULE", "sub": "Low pull, High importance", "icon": "📅"},
        "Q3": {"name": "DEFER", "sub": "High pull, Low importance", "icon": "⏩"},
        "Q4": {
            "name": "CONSIDER DROPPING",
            "sub": "Low pull, Low importance",
            "icon": "🗑",
        },
    }

    def __init__(self):
        self.setup = Setup()
        self.tasks_file = Path(self.setup._get_file_path("tasks.json"))
        self.output_file = Path(self.setup._get_file_path("prioritize.json"))

        self.context_engine = ContextEngine()

        self.optimizer = BaseWeightOptimizer()
        self.gravity_engine = GravityEngine(self.optimizer)

    def _assign_quadrant(self, scores: dict) -> str:
        high_pull = scores["pull_normalized"] > 0.5
        high_importance = scores["semantic_score"] > 0.5

        if high_pull and high_importance:
            return "Q1"
        if not high_pull and high_importance:
            return "Q2"
        if high_pull and not high_importance:
            return "Q3"
        return "Q4"

    def run(self, user_vector: np.ndarray) -> dict:
        """Execute full prioritization pipeline."""
        # Read through core.tasks so the one-time id/title migration has run
        # (under the shared lock) before anything is ranked or keyed.
        from system_sl.core.tasks import load_tasks

        try:
            all_tasks = [t for t in load_tasks() if isinstance(t, dict)]
        except Exception as e:
            return self._empty_result(f"Parse error: {e}")

        if not all_tasks:
            save_ranked_order(all_tasks)
            return self._empty_result("No tasks found")
        
        # Context & Strategy
        ctx_str = self.context_engine.detect_context(all_tasks)
        ctx_profile = self.context_engine.PROFILES[ctx_str]

        bandit_conf = self.optimizer.get_confidence()
        bandit_weights = self.optimizer.get_weights()

        # Score
        scored_tasks = []
        for task in all_tasks:
            scores = self.gravity_engine.score_task(
                task, user_vector, ctx_str, ctx_profile
            )

            scored_tasks.append(
                {
                    "id": task.get("id"),
                    "title": task.get("title", "untitled"),
                    # Tasks are stored with "category" (see core/tasks.py).
                    # Reading "_category" always yielded an empty string.
                    "category": task.get("category", ""),
                    "deadline": task.get("deadline"),
                    "created_at": task.get("created_at"),
                    **scores,
                }
            )

        # Normalize
        p_vals = [t["P_final"] for t in scored_tasks]
        p_min, p_range = min(p_vals), max(p_vals) - min(p_vals)
        for t in scored_tasks:
            t["pull_normalized"] = (
                round((t["P_final"] - p_min) / p_range, 6) if p_range > 0 else 0.5
            )

        # Sort & Map
        scored_tasks.sort(key=lambda t: t["P_final"], reverse=True)

        # The player's drag order wins over the score for the tasks they
        # dragged. Only the drag order is read back here, never this
        # function's own previous output, or the first ranking would stick.
        scored_tasks = apply_manual_order(scored_tasks, load_manual_order())
        task_title = []
        quadrants = {q: [] for q in self.QUADRANTS}
        for task in scored_tasks:
            task_title.append(task)
            q = self._assign_quadrant(task)
            task["quadrant"] = q
            quadrants[q].append(task)
        save_ranked_order(task_title)
        # Output payload
        output = {
            "generated_at": datetime.now().isoformat(),
            "context": ctx_str,
            "context_description": ctx_profile["desc"],
            "bandit_confidence": round(bandit_conf, 4),
            "bandit_weights": bandit_weights,
            "task_count": len(scored_tasks),
            "quadrant_counts": {q: len(tasks) for q, tasks in quadrants.items()},
            "quadrants": {
                q: {
                    "name": self.QUADRANTS[q]["name"],
                    "icon": self.QUADRANTS[q]["icon"],
                    "subtitle": self.QUADRANTS[q]["sub"],
                    "tasks": tasks,
                }
                for q, tasks in quadrants.items()
            },
            "all_tasks_ranked": scored_tasks,
        }
        atomic_write_json(self.output_file, output)
        return output

    def _empty_result(self, reason: str) -> dict:
        return empty_ranking(reason)


def empty_ranking(reason: str) -> dict:
    """Build the degraded-result payload without loading any model."""
    return {
        "generated_at": datetime.now().isoformat(),
        "task_count": 0,
        "quadrant_counts": {q: 0 for q in QUADRANTS},
        "quadrants": {
            q: {
                "name": QUADRANTS[q]["name"],
                "icon": QUADRANTS[q]["icon"],
                "subtitle": QUADRANTS[q]["subtitle"],
                "tasks": [],
            }
            for q in QUADRANTS
        },
        "all_tasks_ranked": [],
        "_reason": reason,
    }


def ensure_importance_suggestions(tasks: list[dict], *, model=None, goals=None) -> bool:
    """Fill in suggested importance for tasks still carrying the default.

    Only tasks whose ``importance_source`` is ``"default"`` are considered;
    values the player set, or suggestions already accepted by an earlier run,
    are never touched. A persona with no usable goals is a no-op. All changes
    are saved in a single write, and the function reports whether it wrote.

    ``model`` and ``goals`` exist so tests can inject a fake embedder and a
    fixed goal list. The app calls this with neither and uses the shared model.
    """
    try:
        goal_list = load_goals() if goals is None else list(goals)
    except Exception as exc:
        log.warning("Could not load persona goals: %s", exc)
        return False

    if not goal_list:
        return False

    changed = False
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if task.get("importance_source") != "default":
            continue
        title = task.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        try:
            importance, matched_goal, _relevance = suggest_importance(
                title, model=model, goals=goal_list
            )
        except Exception as exc:
            log.warning("Could not suggest importance for %r: %s", title, exc)
            continue

        task["importance"] = importance
        task["matched_goal"] = matched_goal
        task["importance_source"] = "suggested"
        changed = True

    if changed:
        from system_sl.core.tasks import save_tasks

        save_tasks(tasks)
    return changed


def _task_key(task: dict):
    return task.get("id") or task.get("title")


def _ranked_task_payload(task: dict, raw_importance: dict) -> dict:
    """Keep only the fields the ranking snapshot promises.

    ``scoring.rank_tasks`` returns a normalized ``importance``. The stored
    task's raw 1..5 value is looked up separately so the payload keeps the
    number the player actually sees.
    """
    return {
        "id": task.get("id"),
        "title": task.get("title", "untitled"),
        "category": task.get("category", ""),
        "deadline": task.get("deadline"),
        "created_at": task.get("created_at"),
        "importance": raw_importance.get(
            _task_key(task), task.get("importance")
        ),
        "importance_source": task.get("importance_source"),
        "matched_goal": task.get("matched_goal"),
        "effort_minutes": task.get("effort_minutes"),
        "urgency": task.get("urgency"),
        "quick_win": task.get("quick_win"),
        "score": task.get("score"),
        "quadrant": task.get("quadrant"),
        "reason": task.get("reason"),
    }


def _write_prioritization(payload: dict) -> None:
    atomic_write_json(PRIORITIZE_FILE_PATH, payload)


def load_latest_prioritization() -> dict:
    """Return the newest ranking snapshot, or a degraded payload.

    Read-only: no writes, no model loading. A missing or unreadable file is
    reported through the same shape every caller already knows.
    """
    try:
        raw = Path(PRIORITIZE_FILE_PATH).read_text(encoding="utf-8")
        data = json.loads(raw)
    except FileNotFoundError:
        return empty_ranking("No prioritization found")
    except (OSError, TypeError, ValueError):
        log.debug("could not read the prioritization file", exc_info=True)
        return empty_ranking("Could not read prioritize.json")

    if not isinstance(data, dict):
        return empty_ranking("Could not read prioritize.json")
    return data


def get_quadrant(task_id_or_title: str) -> str | None:
    """Return ``Q1``..``Q4`` for a task in the latest snapshot, if present."""
    if not isinstance(task_id_or_title, str) or not task_id_or_title:
        return None

    key = task_id_or_title
    ranked = load_latest_prioritization().get("all_tasks_ranked")
    if not isinstance(ranked, list):
        return None

    for task in ranked:
        if isinstance(task, dict) and str(task.get("id")) == key:
            return task.get("quadrant")
    for task in ranked:
        if isinstance(task, dict) and str(task.get("title")) == key:
            return task.get("quadrant")
    return None


def get_task_summary_for_chat(top_n: int = 5) -> str:
    """Return one plain-text line per top-ranked task, for the chat prompt."""
    ranked = load_latest_prioritization().get("all_tasks_ranked")
    if not isinstance(ranked, list):
        ranked = []
    tasks_list = [task for task in ranked if isinstance(task, dict)]
    lines = []
    for index, task in enumerate(tasks_list[:top_n], start=1):
        quadrant = str(task.get("quadrant") or "").strip()
        label = quadrant
        if quadrant in QUADRANTS:
            label = f"{quadrant} {QUADRANTS[quadrant]['name']}"
        title = str(task.get("title") or "untitled")
        reason = str(task.get("reason") or "")
        lines.append(f"{index}. [{label}] {title} | {reason}")

    if not lines:
        return "No ranked tasks are available yet."
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — PUBLIC API & CLI
# ══════════════════════════════════════════════════════════════════════════════
#
def apply_manual_order(ranked: list, manual: list) -> list:
    """Merge the player's drag order into a score-ranked list.

    Tasks in ``manual`` keep their dragged order relative to each other.
    Every other task (new ones, or ones never dragged) goes in at its score
    rank, so a new urgent task still appears near the top.
    """
    if not manual:
        return list(ranked)
    pos = {key: i for i, key in enumerate(manual)}

    def manual_pos(task):
        # Order files written before ids existed hold titles.
        if task.get("id") in pos:
            return pos[task["id"]]
        return pos.get(task.get("title", ""))

    dragged = sorted(
        (t for t in ranked if manual_pos(t) is not None), key=manual_pos
    )
    result = list(dragged)
    for rank, task in enumerate(ranked):
        if manual_pos(task) is None:
            result.insert(min(rank, len(result)), task)
    return result


def _order_quadrant(tasks: list, manual: list) -> list:
    """Put dragged tasks first in their saved order, then score order.

    ``apply_manual_order`` keeps the dragged tasks in their saved relative
    order; extracting that relative order first lets the untouched tasks keep
    the score order the ranker gave them.
    """
    if not manual:
        return list(tasks)

    manual_keys = set(manual)

    def dragged(task) -> bool:
        return task.get("id") in manual_keys or task.get("title") in manual_keys

    ordered = apply_manual_order(tasks, manual)
    return [t for t in ordered if dragged(t)] + [t for t in tasks if not dragged(t)]


def load_manual_order() -> list:
    """Return the player's drag order. See core.tasks for the file layout."""
    from system_sl.core.tasks import load_manual_order as _load

    return _load()


def save_manual_order(tasks: list) -> None:
    """Record an order the player set by dragging."""
    from system_sl.core.tasks import save_manual_order as _save

    _save(tasks)


def save_ranked_order(tasks: list) -> None:
    """Record the ranking's output without touching the drag order."""
    from system_sl.core.tasks import save_ranked_order as _save

    _save(tasks)


def run_prioritization(display: bool = True) -> dict:
    """Rank every task and save the result to ``prioritize.json``.

    Importance suggestions are filled in for untouched default tasks before
    scoring. The score order is global, while the player's drag order is
    applied separately inside each quadrant. Any failure still produces and
    writes the degraded payload so callers never receive a partial dict.
    """
    try:
        from system_sl.core.tasks import load_tasks

        tasks = [t for t in load_tasks() if isinstance(t, dict)]
        if not tasks:
            result = empty_ranking("No tasks found")
        else:
            ensure_importance_suggestions(tasks)

            # Scoring replaces ``importance`` with a normalized value, so keep
            # the raw 1..5 numbers separately for the payload.
            raw_importance = {
                _task_key(task): task.get("importance") for task in tasks
            }
            ranked = scoring.rank_tasks(tasks, now=datetime.now())
            payload_tasks = [
                _ranked_task_payload(task, raw_importance) for task in ranked
            ]

            quadrant_tasks = {q: [] for q in QUADRANTS}
            for task in payload_tasks:
                quadrant_tasks[task["quadrant"]].append(task)

            manual_order = load_manual_order()
            result = {
                "generated_at": datetime.now().isoformat(),
                "task_count": len(payload_tasks),
                "quadrant_counts": {
                    q: len(q_tasks) for q, q_tasks in quadrant_tasks.items()
                },
                "quadrants": {
                    q: {
                        "name": QUADRANTS[q]["name"],
                        "icon": QUADRANTS[q]["icon"],
                        "subtitle": QUADRANTS[q]["subtitle"],
                        "tasks": _order_quadrant(q_tasks, manual_order),
                    }
                    for q, q_tasks in quadrant_tasks.items()
                },
                # The global list is score order only; the drag order lives
                # inside the quadrant lists.
                "all_tasks_ranked": payload_tasks,
            }
    except Exception as exc:
        log.warning("Prioritization failed: %s", exc, exc_info=True)
        result = empty_ranking(f"Prioritization failed: {exc}")

    try:
        _write_prioritization(result)
    except Exception as exc:
        log.warning("Could not write prioritize.json: %s", exc, exc_info=True)

    if display:
        display_prioritized_tasks(result)

    return result


def display_prioritized_tasks(result: dict) -> None:
    log.info("\n══ ⚔ SYSTEM TASK PRIORITIZATION ══")
    for q_key, data in result.get("quadrants", {}).items():
        if not data["tasks"]:
            continue
        log.info(f"── {data['icon']} {data['name']} ──")
        for task in data["tasks"]:
            score = float(task.get("score") or 0.0)
            log.info(f" {task['title'][:40]} (score={score:.4f})")
    log.info("\n")
