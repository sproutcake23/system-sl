"""
╔══════════════════════════════════════════════════════════════════════╗
║  src/system_sl/core/priority_engine.py                               ║
║  Mathematical Priority Engine (Refactored)                           ║
╠══════════════════════════════════════════════════════════════════════╣
║  Features:                                                           ║
║  1. No Repetition: Uses Setup() & Fusion() from vector_converter.    ║
║  2. Class-Based: Grouped by responsibility (Analyzer, Context, etc). ║
║  3. Pluggable Strategies: Thompson Sampling can be swapped/removed   ║
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
from system_sl.core.vector_converter import Setup, Fusion
from system_sl.utils.atomic_json import atomic_write_json, safe_load_json
from system_sl.utils.paths import get_tasks_file_path
from system_sl.utils.file_lock import file_lock

log = logging.getLogger(__name__)


# ── Shared SpellChecker ─────────────────────────────────────────────────
# Building the dictionary is expensive, and PriorityPipeline is constructed
# on every ranking run, so the checker is process-wide like Setup._nlp.
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
        self._nlp = self.setup._get_model()

    def _get_spell(self):
        """Return the process-wide SpellChecker (loaded once per process)."""
        return get_spellchecker()
   
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
        """Infer cognitive load of a task from its title using spaCy POS tagging."""
        try:
            clean_title = self.correct_typos(title.lower())  # Typo correction

            nlp = self._nlp
            doc = nlp(clean_title)
            verbs = {token.lemma_ for token in doc if token.pos_ == "VERB"}

            heavy_hits = len(verbs & self._HEAVY_VERBS)
            light_hits = len(verbs & self._LIGHT_VERBS)

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
            nlp = self._nlp
            doc = nlp(clean_title)

            if not doc.has_vector:
                return 0.5

            task_vec = doc.vector.astype(np.float64)
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
    """Interface for optimization strategies. Pluggable architecture."""

    def get_confidence(self) -> float:
        return 1.0  # Default to full confidence in context without bandit

    def get_weights(self) -> Dict[str, float]:
        return {"semantic_w": 0.33, "urgency_w": 0.33, "momentum_w": 0.34}

    def record_feedback(self, task_title: str, ranked_tasks: List[dict]) -> None:
        pass


class ThompsonSampler(BaseWeightOptimizer):
    """Multi-Armed Bandit strategy, opt-in only.

    GravityEngine uses this optimiser's confidence but not the per-signal
    weights it reports, so feedback would shift timings without actually
    training the semantic/urgency/momentum mix. Leaving it off is honest;
    enabling it needs the scoring blend to consume ``get_weights()``.
    """

    _DEFAULT_STATE = {
        "semantic_w": {"alpha": 2, "beta": 2},
        "urgency_w": {"alpha": 2, "beta": 2},
        "momentum_w": {"alpha": 2, "beta": 2},
    }

    def __init__(self):
        self.setup = Setup()
        self.state_file = Path(self.setup._get_file_path("bandit_state.json"))
        self.state = self._load_state()

    def _load_state(self) -> dict:
        if self.state_file.exists():
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                return {
                    k: data.get(k, v.copy()) for k, v in self._DEFAULT_STATE.items()
                }
            except Exception:
                log.debug("could not read the bandit state", exc_info=True)
        return {k: v.copy() for k, v in self._DEFAULT_STATE.items()}

    def _save_state(self) -> None:
        atomic_write_json(self.state_file, self.state)

    def get_confidence(self) -> float:
        total = sum(
            p["alpha"] + p["beta"] - 4
            for k, p in self.state.items()
            if not k.startswith("_")
        )
        return min(1.0, max(0.0, total / 30.0))

    def get_weights(self) -> Dict[str, float]:
        # The posterior mean of each Beta, not a random draw. Thompson
        # sampling proper would sample, but a task list that reshuffles on
        # every refresh is unusable, so the estimate has to be stable.
        raw = {
            k: p["alpha"] / (p["alpha"] + p["beta"])
            for k, p in self.state.items()
            if not k.startswith("_")
        }
        total = sum(raw.values())
        return (
            {k: round(v / total, 6) for k, v in raw.items()}
            if total
            else super().get_weights()
        )

    def record_feedback(self, task_title: str, ranked_tasks: List[dict]) -> None:
        rank = next(
            (
                i
                for i, t in enumerate(ranked_tasks)
                if t.get("title", "").strip().lower() == task_title.strip().lower()
            ),
            None,
        )

        if rank is None:
            return

        if rank == 0:
            for key in ["semantic_w", "urgency_w", "momentum_w"]:
                self.state[key]["alpha"] += 1
        elif rank >= 2:
            self.state["urgency_w"]["beta"] += 1
            self.state["semantic_w"]["alpha"] += 1

        self._save_state()


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

        # Strategy Blending (falls back to pure context if Thompson is omitted)
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

    def __init__(self, use_thompson_sampling: bool = False):
        self.setup = Setup()
        self.tasks_file = Path(self.setup._get_file_path("tasks.json"))
        self.output_file = Path(self.setup._get_file_path("prioritize.json"))

        self.context_engine = ContextEngine()

        # Pluggable Optimizer Injection
        self.optimizer = (
            ThompsonSampler() if use_thompson_sampling else BaseWeightOptimizer()
        )
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
        if not self.tasks_file.exists():
            return self._empty_result("tasks.json not found")

        try:
            with open(self.tasks_file, "r", encoding="utf-8") as f:
                tasks_data = json.load(f)
        except Exception as e:
            return self._empty_result(f"Parse error: {e}")

        # Flatten tasks
        # NOTE: changes made such that it can read old data format also
        if isinstance(tasks_data, list):
            all_tasks = [t for t in tasks_data if isinstance(t, dict)]
        else:
            # for backward_compact
            all_tasks = [
                task
                for task_list in tasks_data.values()
                if isinstance(task_list, list)
                for task in task_list
                if isinstance(task, dict)
            ]

        if not all_tasks:
            save_manual_order(all_tasks)
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

        # The player's own drag order wins over the score. This is what
        # "reordering is applied to the ranking" means: the manual order is
        # read back and used to sort the ranked list.
        manual = load_manual_order()
        if manual:
            pos = {key: i for i, key in enumerate(manual)}
            scored_tasks.sort(
                key=lambda t: pos.get(
                    t.get("id") or t.get("title", ""), -1
                )
            )
        task_title = []
        quadrants = {q: [] for q in self.QUADRANTS}
        for task in scored_tasks:
            task_title.append(task)
            q = self._assign_quadrant(task)
            task["quadrant"] = q
            quadrants[q].append(task)
        save_manual_order(task_title)
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
    """Build the degraded-result payload without instantiating the pipeline.

    PriorityPipeline.__init__ eagerly loads the NLP model through
    TaskAnalyzer, so constructing one raises in precisely the situations where
    we still owe the caller a well-formed "no ranking" result.
    """
    return {
        "generated_at": datetime.now().isoformat(),
        "context": "GROWTH",
        "context_description": reason,
        "bandit_confidence": 0.0,
        "bandit_weights": {},
        "task_count": 0,
        "quadrant_counts": {"Q1": 0, "Q2": 0, "Q3": 0, "Q4": 0},
        "quadrants": {
            q: {**PriorityPipeline.QUADRANTS[q], "tasks": []}
            for q in PriorityPipeline.QUADRANTS
        },
        "all_tasks_ranked": [],
        "_reason": reason,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — PUBLIC API & CLI
# ══════════════════════════════════════════════════════════════════════════════
#
# NOTE: ADDED TO SAVE THE DRAG ORDER FROM THE USER AND
def load_manual_order() -> list:
    """Load manual task order from disk. Returns empty list if missing/corrupt."""
    file = Path(get_tasks_file_path("task_order.json"))
    data = safe_load_json(file)
    if data is None:
        return []
    if isinstance(data, dict) and "order" in data:
        return data["order"]
    return []


# NOTE: Changed and removed the category thing
def save_manual_order(tasks: list) -> None:
    """Persist the manual task order atomically under the shared task lock.

    Stable ids are preferred; titles are kept only for order files written
    before tasks had ids.
    """
    order = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        key = task.get("id") or task.get("title")
        if key:
            order.append(key)
    file = Path(get_tasks_file_path("task_order.json"))
    with file_lock(get_tasks_file_path("tasks.lock")):
        atomic_write_json(file, {"order": order})


def run_prioritization(display: bool = True, use_thompson: bool = False) -> dict:
    """Main application entry point.

    Thompson sampling is off by default. Its posterior weights are reported in
    the output but the gravity blend only uses its confidence, so feedback
    changed timings rather than the actual semantic/urgency/momentum mix.
    Enabling it would imply training that does not happen; see the note on
    ``ThompsonSampler`` before turning it back on.
    """
    degradation = None
    fusion = Fusion()

    # Checked explicitly rather than by catching FileNotFoundError, so that a
    # genuinely broken NLP model still surfaces as an error instead of being
    # mistaken for "this player skipped onboarding".
    if not fusion.persona_path.exists():
        # A player profile is an enhancement, not a prerequisite. Skipping
        # onboarding is a supported choice, so rank with a neutral vector
        # rather than refusing to produce any ranking at all: urgency and
        # momentum still order the list, only the semantic term is flat.
        log.info("No player profile at %s — ranking with a neutral vector.", fusion.persona_path)
        user_vec = np.zeros(300, dtype=np.float32)
        degradation = "No player profile — ranked by urgency only"
    else:
        try:
            # Prevent spacy overhead unless actually running
            user_vec = fusion.build_user_vector()
        except Exception as e:
            # Deliberately leave task_order.json untouched. Wiping it here
            # would throw away the player's drag order over a transient
            # failure such as an unreadable model. get_topn_task() rebuilds
            # the order from the active tasks whenever it is empty.
            log.warning("Could not build user vector: %s", e)
            return empty_ranking("Vector generation failed")

    try:
        # Constructed inside the guard on purpose: the constructor eagerly
        # loads the NLP model and can raise for a broken bundle.
        pipeline = PriorityPipeline(use_thompson_sampling=use_thompson)
        result = pipeline.run(user_vec)
    except Exception as e:
        # A failure inside the pipeline must not propagate into the Qt slot
        # that called us, or the caller keeps rendering whatever stale list it
        # already had. Report it as an empty ranking instead so the caller can
        # decide how to degrade.
        log.warning("Prioritization pipeline failed: %s", e)
        result = empty_ranking("Prioritization failed")

    # empty_ranking() already carries a reason for an empty ranking; never
    # overwrite it with the softer "no profile" note.
    if degradation and not result.get("_reason"):
        result["_reason"] = degradation

    if display:
        display_prioritized_tasks(result)

    return result


def display_prioritized_tasks(result: dict) -> None:
    log.info("\n══ ⚔ SYSTEM TASK PRIORITIZATION ══")
    log.info(f"Context : {result.get('context')} - {result.get('context_description')}")
    log.info(f"Bandit  : {result.get('bandit_confidence'):.1%} confidence\n")

    for q_key, data in result.get("quadrants", {}).items():
        if not data["tasks"]:
            continue
        log.info(f"── {data['icon']} {data['name']} ──")
        for t in data["tasks"]:
            bar = "█" * int(t["pull_normalized"] * 20)
            log.info(f" [{bar:<20}] {t['title'][:40]} (P={t['P_final']:.4f})")
    log.info("\n")


if __name__ == "__main__":
    import sys

    if "--record" in sys.argv:
        idx = sys.argv.index("--record")
        if idx + 1 < len(sys.argv):
            title = sys.argv[idx + 1]
            try:
                with open(Setup()._get_file_path("prioritize.json"), "r") as f:
                    ranked = json.load(f).get("all_tasks_ranked", [])
                ThompsonSampler().record_feedback(title, ranked)
                log.info(f"✓ Bandit updated for '{title}'")
            except Exception as e:
                log.info(f"✗ Error updating bandit: {e}")
    else:
        run_prioritization(use_thompson=False)
