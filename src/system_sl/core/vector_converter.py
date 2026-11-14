"""
INPUT :
It takes persona.json and completed_tasks.json

Implements -
SentenceTransformer (all-MiniLM-L6-v2) based semantic vectors.
spaCy is no longer used or supported.

"""

from __future__ import annotations

import os
import sys
import math
import numpy as np
from datetime import datetime
from pathlib import Path
import logging  # important we will use this for debugging.
import json


from system_sl.core.tasks import load_completed_tasks
from system_sl.utils import get_tasks_file_path

log = logging.getLogger(__name__)

# Model configuration
SENTENCE_TRANSFORMER_MODEL = "all-MiniLM-L6-v2"
VECTOR_DIM = 384  # all-MiniLM-L6-v2 dimension

# Directory (inside the PyInstaller bundle) where the model files are shipped.
BUNDLED_MODEL_DIR = Path("models") / SENTENCE_TRANSFORMER_MODEL


def _model_dim(model) -> int:
    """Return a SentenceTransformer model's embedding dimension.

    ``get_embedding_dimension`` is the current name; older sentence-transformers
    releases only expose ``get_sentence_embedding_dimension``.
    """
    for attr in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
        getter = getattr(model, attr, None)
        if getter is not None:
            return int(getter())
    return VECTOR_DIM


def model_source() -> str:
    """Return the model path/name SentenceTransformer should load.

    Precedence:
    1. ``SYSTEM_SL_MINILM_MODEL`` — an explicit local model directory (used by
       the build and by tests).
    2. The model shipped inside a frozen build (``sys._MEIPASS/models/...``),
       so releases work offline.
    3. The HuggingFace model id, which downloads once and is cached.

    A frozen build that is missing the bundled model falls through to (3) and
    logs a warning rather than crashing: ranking still works, it just needs
    network access the first time.
    """
    override = os.environ.get("SYSTEM_SL_MINILM_MODEL")
    if override and Path(override).is_dir():
        return override

    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        bundled = Path(bundle_root) / BUNDLED_MODEL_DIR
        if bundled.is_dir():
            return str(bundled)
        log.warning(
            "Bundled embedding model missing at %s — falling back to the "
            "HuggingFace id (needs network on first run).",
            bundled,
        )

    return SENTENCE_TRANSFORMER_MODEL


class Setup:
    _st_model = None  # SentenceTransformer instance

    def __init__(self) -> None:
        pass  # For lazy model loading

    def _get_file_path(self, filename: str) -> Path:
        """
        Return the persistent config directory for system-sl.
        Linux - ~/.config/system-sl
        """
        return get_tasks_file_path(filename)

    def get_model(self):
        """Return the shared SentenceTransformer, loading it on first call.

        all-MiniLM-L6-v2 is the only embedding model the app supports; there is
        no fallback backend. A missing package is a hard error, because silently
        ranking without a semantic model would look like the priority engine
        merely being bad at its job.
        """
        if self._st_model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "sentence-transformers is required for persona vectors. "
                    f"Install it with: pip install sentence-transformers ({exc})"
                ) from exc

            source = model_source()
            log.info(
                "Loading SentenceTransformer from %s (%s)",
                source,
                "bundled" if Path(source).is_dir() else "HuggingFace id",
            )
            self._st_model = SentenceTransformer(source)
            log.info(
                "SentenceTransformer loaded (dim: %d)", _model_dim(self._st_model)
            )
        return self._st_model


# Shared, process-wide setup instance.
_setup_instance = Setup()


def get_shared_model():
    """Return the process-wide shared SentenceTransformer, loading on first use."""
    return _setup_instance.get_model()


def encode(text: str) -> np.ndarray:
    """Encode ``text`` into a normalized all-MiniLM-L6-v2 vector."""
    model = _setup_instance.get_model()
    embedding = model.encode(text, normalize_embeddings=True)
    return np.asarray(embedding, dtype=np.float32)


def get_vector_dim() -> int:
    """Return the active model's embedding dimension (384 for MiniLM)."""
    try:
        return _model_dim(_setup_instance.get_model())
    except Exception:
        return VECTOR_DIM


class Real_worker:
    def __init__(self) -> None:
        self.my_setup = _setup_instance  # use the module-level setup

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
                "persona.json has no responses - run the onboarding questionnaire first"
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
        Convert a text blob into a normalized all-MiniLM-L6-v2 vector.

        Parameters
        ----------
        blob : str
        Any text string. Longer = more tokens = more robust average.

        Returns
        -------
        np.ndarray  shape (384,)
        """
        vec = encode(blob)
        log.info("Vectorized blob | dim=%d", len(vec))
        return vec


class Decay_stradegy:
    def __init__(self) -> None:
        self.my_setup = _setup_instance

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
    # FIXME: Change tasks.py mark_task_completed function to correctly write completed.json
    # NOTE: Also make changes in priortization_engine

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


        Returns: (decay_vector, task_count)
        """
        accumulated_vector = np.zeros(get_vector_dim(), dtype=np.float64)
        total_weight = 0.0
        count = 0

        if not isinstance(completed_tasks, list):
            log.warning(
                "completed_tasks data is not a flat list. Skipping behavioral history."
            )
            return np.zeros(get_vector_dim(), dtype=np.float32), 0

        for entry in completed_tasks:
            parsed = self._read_completion_entry(entry)
            if not parsed:
                continue

            title, completion_date = parsed
            w = self._compute_decay_weight(completion_date)

            if w < self._MIN_DECAY_WEIGHT:
                log.debug("skipping old tasks %r", title)
                continue

            vec = encode(title.lower())
            accumulated_vector += w * vec.astype(np.float64)
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
            return np.zeros(get_vector_dim(), dtype=np.float32), 0

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
        )  # Aligned tasks = 10 , non-aligned tasks = 30
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
        self.setup = _setup_instance

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
            if len(vector) != get_vector_dim() or cache.get("model") != SENTENCE_TRANSFORMER_MODEL:
                log.info(
                    "Cache was built with a different model/dimension — rebuilding"
                )
                return None
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
                "model": SENTENCE_TRANSFORMER_MODEL,
                "dim": len(vector),
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
        self.setup = _setup_instance

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
            "vector_dim": get_vector_dim(),
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
        self.setup = _setup_instance

        # Define all file paths ONCE here
        self.persona_path = Path(self.setup._get_file_path("persona.json"))
        self.completed_path = Path(self.setup._get_file_path("completed_tasks.json"))
        self.cache_path = Path(self.setup._get_file_path("vector_cache.json"))
        self.cache = Caching()

    def build_user_vector(self, use_cache: bool = True) -> np.ndarray:
        """
        The main orchestrator: Builds or loads the 384-dim user vector.
        Brings together Real_worker and Decay_stradegy.
        Uses SentenceTransformer all-MiniLM-L6-v2.
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

        log.info("Building user vector from scratch (dim=%d)...", get_vector_dim())

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
        log.info(
            "Persona vector built | norm=%.4f | dim=%s",
            float(np.linalg.norm(persona_vec)),
            len(persona_vec),
        )

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
            fused = np.zeros(get_vector_dim(), dtype=np.float32)

        log.info(
            "Final vector | norm=%.4f (should be ~1.0) | dim=%s",
            float(np.linalg.norm(fused)),
            len(fused),
        )

        self.cache._save_cache(fused)
        return fused


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — CLI STANDALONE MODE
# ══════════════════════════════════════════════════════════════════════════════
