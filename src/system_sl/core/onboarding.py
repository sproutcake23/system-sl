"""Onboarding questions and the player-profile store.

The questionnaire itself is the GUI's OnboardingWindow; this module holds the
questions and reads and writes ``persona.json``.
"""

import json
import os
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List


# =====================================================================
# 0. SHARED CONFIGURATION (single source of truth for both frontends)
# =====================================================================

# The onboarding questionnaire shown by OnboardingWindow.
ONBOARDING_QUESTIONS: List[Dict[str, Any]] = [
    {
        "id": 1,
        "category": "IDENTITY",
        "question": "What is your ultimate career or life ambition?",
        "hint": "e.g., Senior AI Engineer, Product Manager, Startup Founder",
    },
    {
        "id": 2,
        "category": "ACADEMIC",
        "question": "What subjects or fields are you studying/interested in?",
        "hint": "e.g., Computer Science, Mathematics, Data Science",
    },
    {
        "id": 3,
        "category": "SKILLS",
        "question": "What technical skills, tools, or languages are you focusing on?",
        "hint": "e.g., Python, Docker, React, AWS, TensorFlow",
    },
    {
        "id": 4,
        "category": "ACTIONS",
        "question": "What actions do you want to spend most of your time doing?",
        "hint": "e.g., Coding, designing, writing, researching",
    },
    {
        "id": 5,
        "category": "PROJECTS",
        "question": "What kind of projects do you love building?",
        "hint": "e.g., Web apps, ML models, APIs, mobile apps",
    },
    {
        "id": 6,
        "category": "MILESTONE",
        "question": "What is your main objective for the next 3-6 months?",
        "hint": "e.g., Land internship, build portfolio, launch product",
    },
    {
        "id": 7,
        "category": "COGNITION",
        "question": "What types of problems do you enjoy solving most?",
        "hint": "e.g., Algorithms, system design, optimization",
    },
    {
        "id": 8,
        "category": "HABITS",
        "question": "What daily habits are essential to your growth?",
        "hint": "e.g., Reading, gym, meditation, learning",
    },
    {
        "id": 9,
        "category": "INDUSTRY",
        "question": "Which industries do you want your work to impact?",
        "hint": "e.g., AI, FinTech, Healthcare, Climate Tech",
    },
    {
        "id": 10,
        "category": "VALUES",
        "question": "What keywords describe your core professional values?",
        "hint": "e.g., Efficient, innovative, scalable, quality",
    },
]


# =====================================================================
# 1. STORAGE LAYER (Single Responsibility: File System I/O)
# =====================================================================


class PersonaStorageHandler:
    """Manages the storage lifecycle, tracking flags, and serialization of user persona data."""

    def __init__(self, application_name: str = "system-sl") -> None:
        """Initializes storage directories based on the host Operating System.

        Args:
            application_name (str): Directory name where configurations are cached. Defaults to "system-sl".
        """
        if os.name == "nt":
            base_dir: Path = Path(
                os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")
            )
            self.data_dir: Path = base_dir / application_name
        else:
            self.data_dir: Path = Path.home() / ".config" / application_name

        self.persona_file: Path = self.data_dir / "persona.json"
        self.setup_flag: Path = self.data_dir / ".setup_complete"
        self.declined_flag: Path = self.data_dir / ".onboarding_declined"

    def has_been_asked(self) -> bool:
        """Whether the player has ever been shown onboarding, answered or not.

        ``.onboarding_declined`` records an explicit Skip. ``.setup_complete``
        is checked as well because it predates that marker: a config dir
        holding it without a persona can only have come from a skip, since
        ``write_persona_profile`` always writes both.
        """
        return self.declined_flag.exists() or self.setup_flag.exists()

    def _profile_state(self) -> str:
        """Classify the profile on disk as ``valid``, ``corrupt`` or ``missing``."""
        if not self.persona_file.is_file():
            return "missing"
        try:
            with self.persona_file.open("r", encoding="utf-8") as file_pointer:
                persona_data = json.load(file_pointer)
        except (OSError, json.JSONDecodeError):
            return "corrupt"
        responses = persona_data.get("responses") if isinstance(persona_data, dict) else None
        if not isinstance(responses, list) or len(responses) != len(ONBOARDING_QUESTIONS):
            return "corrupt"
        return "valid"

    def has_profile(self) -> bool:
        """Whether a usable player profile exists.

        This is deliberately separate from :meth:`is_first_time`: a player who
        skipped has no profile but should not be onboarded again, and the main
        window shows its banner based on the profile being missing rather than
        on onboarding being pending.
        """
        return self._profile_state() == "valid"

    def is_first_time(self) -> bool:
        """Determine whether onboarding should be shown at startup.

        Returns:
            bool: True when the player has a profile to build, or has never
                been asked.

        A profile is an enhancement, not a prerequisite: ``run_prioritization``
        ranks against a neutral vector when ``persona.json`` is absent, so a
        player who declined must not be re-prompted on every launch.
        """
        state = self._profile_state()
        if state == "valid":
            return False
        if state == "corrupt":
            # A damaged profile is worth asking about again.
            return True
        return not self.has_been_asked()

    def load_saved_responses(self, questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return the saved answers in question order, for prefilling a re-run.

        Answers are matched by question id. The list stops at the first
        question with no saved answer, because the wizard treats it as the
        answers given so far. A profile that is incomplete (for example after
        the questions changed) still prefills what it has; one that is
        missing or unreadable gives ``[]``.
        """
        try:
            with open(self.persona_file, "r", encoding="utf-8") as file_pointer:
                saved = json.load(file_pointer).get("responses") or []
        except (OSError, ValueError, AttributeError):
            return []
        by_id = {
            r.get("question_id"): r
            for r in saved
            if isinstance(r, dict) and isinstance(r.get("answer"), str)
        }
        ordered = []
        for question in questions:
            response = by_id.get(question["id"])
            if response is None:
                break
            ordered.append(dict(response))
        return ordered

    def ensure_data_directory(self) -> None:
        """Ensures the application data directories are physically allocated on disk."""
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def write_persona_profile(
        self, responses: List[Dict[str, Any]], statistics: Dict[str, int]
    ) -> None:
        """Serializes player profiles and saves them as a structured JSON file.

        Args:
            responses (List[Dict[str, Any]]): Collected list of question dictionaries with user answers.
            statistics (Dict[str, int]): Priority tier counts calculated from the survey metrics.
        """
        self.ensure_data_directory()

        persona_data: Dict[str, Any] = {
            "version": "1.0",
            "created_at": datetime.now().isoformat(),
            "player_name": os.getenv("USER", "Player"),
            "responses": responses,
            "statistics": statistics,
        }

        with open(self.persona_file, "w") as file_pointer:
            json.dump(persona_data, file_pointer, indent=2)

        # A profile rewrite must take effect immediately rather than leaving a
        # semantic vector calculated from the previous answers in place.
        (self.data_dir / "vector_cache.json").unlink(missing_ok=True)
        self.setup_flag.touch()
        # The player answered after all, so a past decline no longer applies.
        self.declined_flag.unlink(missing_ok=True)

    def record_onboarding_declined(self) -> None:
        """Record that the player chose to skip profile creation.

        Kept distinct from :meth:`write_persona_profile` so "declined" and
        "finished" are not conflated under one legacy marker.
        """
        self.ensure_data_directory()
        self.declined_flag.touch()
