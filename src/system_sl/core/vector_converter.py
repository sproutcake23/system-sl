"""
INPUT :
It takes persona.json and completed_tasks.json

Implements -
It implements spacy md model and

"""

from __future__ import annotations

import os
import math
import numpy as np
from datetime import datetime
from pathlib import Path
import logging  # important we will use this for debugging.
import json


from system_sl.core.tasks import load_completed_tasks
from system_sl.utils import get_tasks_file_path

log = logging.getLogger(__name__)


class Setup:
    _nlp = None
    def __init__(self) -> None:
        pass  # For lazy model loading

    # def _get_config_dir(self) -> Path:
    #     """
    #     This is same technique as used in tasks.py.
    #     Return the persistent config directory for system-sl
    #     just we need to use
    #     _get_config_dir

    #     Windows - %APPDATA%/system-sl
    #     Linux - ~/.config/system-sl

    #     """
    #     prog = "system-sl"
    #     if os.name == "nt":
    #         base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    #     else:
    #         base = Path.home() / ".config"

    #     config_dir = base / prog
    #     config_dir.mkdir(
    #         parents=True, exist_ok=True
    #     )  # works as safety net if the parent floder won't exist it creates otherwise leaves.

    #     return config_dir

    def _get_file_path(self, filename: str) -> Path:
        """
        This is same technique as used in tasks.py.
        Return the persistent config directory for system-sl
        just we need to use
        _get_config_dir

        Windows - %APPDATA%/system-sl
        Linux - ~/.config/system-sl


        """
        return get_tasks_file_path(filename)

    def _load_spacy_model(self):
        """
        Load spacy medium model (en_core_web_md) i will not explain why i choose this.

        """
        try:
            import sys
            import spacy

            if getattr(sys, "frozen", False):
                bundle_dir = sys._MEIPASS
                model_pkg = os.path.join(bundle_dir, "en_core_web_md")
                if os.path.isdir(model_pkg):
                    for entry in os.listdir(model_pkg):
                        candidate = os.path.join(model_pkg, entry)
                        if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "config.cfg")):
                            model = spacy.load(candidate)
                            break
                    else:
                        model = spacy.load("en_core_web_md")
                else:
                    model = spacy.load("en_core_web_md")
            else:
                model = spacy.load("en_core_web_md")
            log.info("spacy en_core_web_md is loaded. ")
            return model
        except OSError:
            raise RuntimeError("[System Error] model is not installed — run: python -m spacy download en_core_web_md")

    # _nlp = None for lazy loading

    def _get_model(self):
        """Return the global spacy model, loading it on first call."""
        if Setup._nlp is None:
            Setup._nlp = self._load_spacy_model()
        return Setup._nlp


class Real_worker:
    def __init__(self) -> None:
        self.my_setup = Setup()  # created to use the function from setup class

    def _build_persona_blob(self, persona: dict) -> str:
        """
        Convert persona.json responses into a single impact-weighted text blob.

        Parameters
        ----------
        persona : dict
        Loaded persona.json — must contain a 'responses' list where each
        element has 'answer' (str) and 'impact' (int 1|2|3).

        Returns
        -------
        str
        A space-separated string where each answer is repeated `impact` times.
        Example output for impact=2:
           "python docker python docker algorithms algorithms "
        """

        responses = persona.get("responses", [])
        if not responses:
            raise ValueError(
                "persona.json has no responsesRun the onboarding questionarrie first"
            )
        blob_parts = []
        log.info("Building persona blob from %d responses: ", len(responses))

        for entry in responses:
            answer = str(entry.get("answer", "")).strip().lower()
            impact = int(entry.get("impact", 1))  # added 1 if impact is empty

            if not answer:
                log.warning("Skipping answer of some questions")
                continue

            impact = max(1, min(3, impact))

            weighted_chunk = (answer + " ") * impact
            blob_parts.append(weighted_chunk)
            # Visual log showing what's happening
            log.info(" impact=%d | %r (×%d)", impact, answer, impact)

        return " ".join(blob_parts)

    def _vectorize_blob(self, blob: str) -> np.ndarray:
        """
        Convert a text blob into a 300-dim spaCy vector.

        spaCy's Doc.vector is the mean of all token vectors in the document.
        Tokens with no vector entry in the model are silently skipped.

        Parameters
        ----------
        blob : str
        Any text string. Longer = more tokens = more robust average.

        Returns
        -------
        np.ndarray  shape (300,)
        """

        nlp = self.my_setup._get_model()
        doc = nlp(blob)  # NOTE: doc is name of vector

        if not doc.has_vector:
            log.warning(
                "Document has no vector - all tokens may be OOV. Returning zeros"
            )
            return np.zeros(300, dtype=np.float32)
        return doc.vector.copy()  # .copy() prevents mutation of spaCy internal state


class Decay_stradegy:
    def __init__(self) -> None:
        self.my_setup = Setup()

        self.DECAY_HALF_LIFE_DAYS = 3  # TODO: Try to make a logic so dynamically adjust this for now i setted it for 3.
        self._LAMBDA = (
            math.log(2) / self.DECAY_HALF_LIFE_DAYS
        )  # Remembering old days physical chemistry
        # With a 3-day half-life, weight 0.05 is reached at about 13 days.
        self._MIN_DECAY_WEIGHT = 0.05

    # def _read_completion_entry(self, entry: dict) -> tuple[str, datetime] | None:
    #     """
    #     Read a single completed_tasks.json entry into (title, completion_date).
    #
    #     completed_tasks.json entries are structured dicts written by
    #     mark_task_completed() in tasks.py:
    #
    #        {
    #         "title":        "Implement login endpoint",
    #         "completed_at": "2025-05-01"
    #        }
    #
    #     Parameters
    #     ----------
    #     entry : dict
    #        A single entry from the completed_tasks.json list.
    #        Must have "title" (str) and "completed_at" (str "YYYY-MM-DD").
    #
    #     Returns
    #     -------
    #     tuple (title: str, date: datetime) or None if entry is malformed.
    #     """
    #     # FIXME: Change tasks.py mark_task_completed function to correctly write completed.json
    #     # NOTE: Also make changes in priortization_engine
    #
    #     if not isinstance(entry, dict):
    #         log.debug("Skipping non-dict entry in completed_tasks.json: %r", entry)
    #         return None
    #
    #     title = entry.get("title", "").strip()
    #     date_str = entry.get("completed_at", "").strip()
    #
    #     if not title:
    #         log.debug("Skipping entry with the empty title: %r", entry)
    #         return None
    #
    #     if not date_str:
    #         log.debug("Skipping entry with no completed_at date: %r", entry)
    #         return None
    #
    #     try:
    #         date = datetime.strptime(date_str, "%Y-%m-%d")
    #         return title, date
    #
    #     except ValueError:
    #         log.debug("Invalid completed_at date %r in entry %r", date_str, entry)
    #         return None
    def _read_completion_entry(self, entry: dict) -> tuple[str, datetime] | None:
        if not isinstance(entry, dict):
            return None

        title = entry.get("title", "").strip()
        date_str = entry.get("completed_at", "").strip()

        if not title or not date_str:
            return None

        try:
            date = datetime.strptime(date_str, "%Y-%m-%d")
            return title, date
        except ValueError:
            return None

    def _compute_decay_weight(self, completion_date: datetime) -> float:
        """
        Compute the temporal decay weight for a completed task.

        Formula: weight = e^(−λ × days_since_completion)

        The older the completion, the less it should influence the user vector.

        Parameters
        ----------
        completion_date : datetime

        Returns
        -------
        float  in range (0.0, 1.0]
        1.0 = completed today
        0.5 = completed one half-life (3 days) ago
        0.0 → (asymptote, never exactly zero)
        """
        days_since = max(0, (datetime.now() - completion_date).days)
        return math.exp(-self._LAMBDA * days_since)

    def _build_decay_vector(self, completed_tasks: list) -> tuple[np.ndarray, int]:
        """
        Build a purely time-weighted vector from a flat list of completed tasks.


        Returns: (decay_vector, task_count, raw_coherence_norm)
        """
        nlp = self.my_setup._get_model()

        accumulated_vector = np.zeros(300, dtype=np.float64)
        total_weight = 0.0
        count = 0

        if not isinstance(completed_tasks, list):
            log.warning(
                "completed_tasks data is not a flat list. Skipping behavioral history."
            )
            return np.zeros(300, dtype=np.float32), 0, 0.0

        for entry in completed_tasks:
            parsed = self._read_completion_entry(entry)
            if not parsed:
                continue

            title, completion_date = parsed
            w = self._compute_decay_weight(completion_date)

            if w < self._MIN_DECAY_WEIGHT:
                log.debug("skipping old tasks %r", title)
                continue

            doc = nlp(title.lower())
            if not doc.has_vector:
                log.debug("  No vector for completed task %r — skipping", title)
                continue

            accumulated_vector += w * doc.vector.astype(np.float64)
            total_weight += w
            count += 1

            log.info(
                "[completed] %r | days_old = %d | w = %.4f",
                title,
                (datetime.now() - completion_date).days,
                w,
            )

        if total_weight == 0:
            log.info("No valid completed tasks found — decay vector is zero")
            return np.zeros(300, dtype=np.float32), 0

        # --- THE NEW COHERENCE MATH ---
        # 1. Get the raw weighted mean
        raw_mean_vector = accumulated_vector / total_weight

        decay_vector = raw_mean_vector.astype(np.float32)

        log.info("Decay vector built from %d tasks | weight=%.2f", count, total_weight)
        # 3. Return the raw_norm so the fusion engine can use it
        return decay_vector, count

    def _build_fusion_weights(self, decay_task_count: int, coherence: float):
        if decay_task_count == 0:
            return 1.0, 0.0

        MAX_DECAY = 0.80
        STEEEPNESS = 0.15

        dynamic_midpoint = 30 - (
            20 * coherence
        )  # ALigned tasks = 10 , non-aligned tasks = 30
        x = STEEEPNESS * (decay_task_count - dynamic_midpoint)
        decay_w = MAX_DECAY / (
            1 + math.exp(-x)
        )  # symoid type function for reaching the threashold

        return round(1.0 - decay_w, 4), round(
            decay_w, 4
        )  # 1st is weight for persona and second is weight for the decay_vec


class Caching:
    def __init__(self) -> None:
        # Initialize dependencies ONCE here
        self.setup = Setup()

        # Define all file paths ONCE here
        self.persona_path = Path(self.setup._get_file_path("persona.json"))
        self.completed_path = Path(self.setup._get_file_path("completed_tasks.json"))
        self.cache_path = Path(self.setup._get_file_path("vector_cache.json"))

    def _get_file_mtime(self, path: Path) -> float:
        """Return file mtime as float, or 0.0 if file doesn't exist."""
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    def _load_cache(self) -> np.ndarray | None:
        """Load the cached vector if source files haven't changed."""
        if not self.cache_path.exists():
            return None

        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)

            if "vector" not in cache or "persona_mtime" not in cache:
                return None

            persona_mtime = self._get_file_mtime(self.persona_path)
            completed_mtime = self._get_file_mtime(self.completed_path)

            if (
                persona_mtime > cache["persona_mtime"]
                or completed_mtime > cache["completed_mtime"]
            ):
                log.info("Source files changed — cache invalidated, rebuilding vector")
                return None

            vector = np.array(cache["vector"], dtype=np.float32)
            log.info(
                "Loaded user vector from cache (built %s)",
                cache.get("built_at", "unknown"),
            )
            return vector

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            log.warning("Cache read failed (%s) — rebuilding", e)
            return None

    def _save_cache(self, vector: np.ndarray) -> None:
        """Persist the computed vector to disk for fast re-use."""
        try:
            cache = {
                "built_at": datetime.now().isoformat(),
                "persona_mtime": self._get_file_mtime(self.persona_path),
                "completed_mtime": self._get_file_mtime(self.completed_path),
                "vector": vector.tolist(),
            }
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            log.info("Vector cached to %s", self.cache_path)
        except Exception as e:
            log.warning("Could not save vector cache: %s", e)


class Helper:
    def __init__(self):
        # Initialize dependencies ONCE here
        self.setup = Setup()

        # Define all file paths ONCE here
        self.persona_path = Path(self.setup._get_file_path("persona.json"))
        self.completed_path = Path(self.setup._get_file_path("completed_tasks.json"))
        self.cache_path = Path(self.setup._get_file_path("vector_cache.json"))

    def get_vector_info(self) -> dict:
        """Helper to return a human-readable summary of the current vector."""
        info = {
            "persona_exists": self.persona_path.exists(),
            "completed_tasks_exists": self.completed_path.exists(),
            "cache_exists": self.cache_path.exists(),
            "built_at": None,
            "persona_answers": [],
            "completed_task_count": 0,
            "fusion_weights": {"persona": 1.0, "decay": 0.0},
        }

        if self.cache_path.exists():
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                info["built_at"] = cache.get("built_at")
            except Exception:
                log.debug("could not read the vector cache", exc_info=True)

        if self.persona_path.exists():
            try:
                with open(self.persona_path, "r", encoding="utf-8") as f:
                    persona = json.load(f)
                info["persona_answers"] = [
                    {
                        "category": r["category"],
                        "answer": r["answer"],
                        "impact": r["impact"],
                    }
                    for r in persona.get("responses", [])
                ]
            except Exception:
                log.debug("could not read persona.json", exc_info=True)

        if self.completed_path.exists():
            try:
                with open(self.completed_path, "r", encoding="utf-8") as f:
                    completed = json.load(f)
                count = len(completed) if isinstance(completed, list) else 0
                info["completed_task_count"] = count

                decay_strategy = Decay_stradegy()
                pw, dw = decay_strategy._build_fusion_weights(count, 1.0)
                info["fusion_weights"] = {"persona": pw, "decay": dw}
            except Exception:
                log.debug("could not read completed_tasks.json", exc_info=True)

        return info


class Fusion:
    def __init__(self) -> None:
        # Initialize dependencies ONCE here
        self.setup = Setup()

        # Define all file paths ONCE here
        self.persona_path = Path(self.setup._get_file_path("persona.json"))
        self.completed_path = Path(self.setup._get_file_path("completed_tasks.json"))
        self.cache_path = Path(self.setup._get_file_path("vector_cache.json"))
        self.cache = Caching()

    def build_user_vector(self, use_cache: bool = True) -> np.ndarray:
        """
        The main orchestrator: Builds or loads the 300-dim user vector.
        Brings together Real_worker and Decay_stradegy.
        """
        if not self.persona_path.exists():
            raise FileNotFoundError(
                f"persona.json not found at {self.persona_path}. "
                "Complete onboarding first."
            )

        if use_cache:
            cached = self.cache._load_cache()
            if cached is not None:
                return cached

        log.info("Building user vector from scratch...")

        # Load JSONs
        with open(self.persona_path, "r", encoding="utf-8") as f:
            persona = json.load(f)

        completed_tasks = []  # FIXME: Check we have to tuple or list
        if self.completed_path.exists():
            try:
                # completed_tasks=json.load(f)
                completed_tasks = load_completed_tasks()

            except Exception as e:
                log.error("Error %s", e)
        else:
            log.info("No completed_tasks.json found — using persona only")

        # Phase 1: Persona Vector
        log.info("─── Phase 1: Persona Vector ─────────────────────")
        worker = Real_worker()
        blob = worker._build_persona_blob(persona)
        persona_vec = worker._vectorize_blob(blob)
        log.info("Persona vector built | norm=%.4f", float(np.linalg.norm(persona_vec)))

        # Phase 2: Temporal Decay Vector
        log.info("─── Phase 2: Temporal Decay Vector ──────────────")
        decay_strategy = Decay_stradegy()

        decay_vec, task_count = decay_strategy._build_decay_vector(completed_tasks)

        # Phase 3: Fusion and Coherence
        log.info("─── Phase 3: Fusion ─────────────────────────────")

        # Coherence calculation
        if task_count > 0 and np.linalg.norm(decay_vec) > 0:
            sim = float(
                np.dot(persona_vec, decay_vec)
                / (np.linalg.norm(persona_vec) * np.linalg.norm(decay_vec))
            )
            coherence = max(0.0, sim)
        else:
            coherence = 0.0

        persona_w_adj, decay_w_adj = decay_strategy._build_fusion_weights(
            task_count, coherence
        )

        log.info(
            "Fusion — coherence=%.3f → final(persona=%.2f decay=%.2f) tasks=%d",
            coherence,
            persona_w_adj,
            decay_w_adj,
            task_count,
        )

        fused = (persona_w_adj * persona_vec) + (decay_w_adj * decay_vec)

        # L2 Normalise
        norm = np.linalg.norm(fused)
        if norm > 0:
            fused = (fused / norm).astype(np.float32)
        else:
            log.error("Fused vector has zero norm — returning zeros.")
            fused = np.zeros(300, dtype=np.float32)

        log.info(
            "Final vector | norm=%.4f (should be ~1.0)", float(np.linalg.norm(fused))
        )

        self.cache._save_cache(fused)
        return fused


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — CLI STANDALONE MODE
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("\n" + "═" * 60)
    log.info("  USER VECTOR BUILDER — Standalone Debug Mode")
    log.info("═" * 60)

    try:
        helper = Helper()
        fusion_engine = Fusion()
        info = helper.get_vector_info()

        persona = "✓ found" if info["persona_exists"] else "✗ MISSING"
        completed = "✓ found" if info["completed_tasks_exists"] else "✗ not found"
        cache = "✓ exists" if info["cache_exists"] else "○ none"
        weights = info["fusion_weights"]
        log.info(f"\n  Persona file   : {persona}")
        log.info(f"  Completed tasks: {completed}")
        log.info(f"  Cache          : {cache}")
        log.info(f"  Completed count: {info['completed_task_count']}")
        log.info(
            f"  Fusion weights : persona={weights['persona']:.2f}"
            f"  decay={weights['decay']:.2f}"
        )

        if not info["persona_exists"]:
            log.info("\n  ✗ Cannot build vector — run onboarding first.")
        else:
            log.info("\n  Building vector (may take a few seconds on first run)...")
            vec = fusion_engine.build_user_vector(use_cache=False)
            log.info(f"\n  Vector shape : {vec.shape}")
            log.info(f"  Vector norm  : {np.linalg.norm(vec):.6f}  (should be ~1.0)")
            log.info(f"  Non-zero dims: {np.count_nonzero(vec)}")
            log.info(f"  Top 5 values : {sorted(vec, reverse=True)[:5]}")
            log.info("\n  ✓ Vector built successfully.")

    except FileNotFoundError as e:
        log.info(f"\n  ✗ {e}")
    except RuntimeError as e:
        log.error("%s", e)
    except Exception as e:
        log.info(f"\n  ✗ Unexpected error: {e}")
        raise

# TODO: IMplement temporal decay maths here and also doing caching for optimization
