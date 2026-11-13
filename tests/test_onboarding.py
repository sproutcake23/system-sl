"""Onboarding: Back works, answers are prefilled, the summary is editable."""

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from system_sl.core.onboarding import ONBOARDING_QUESTIONS  # noqa: E402


def test_back_prefills_and_summary_is_saved_from_the_table(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    QApplication.instance() or QApplication([])

    from system_sl.frontend.gui.popup_windows import OnboardingWindow

    window = OnboardingWindow()
    window._on_begin()

    # Answer the first question, move on, then come back.
    window.answer_input.setText("Engineer")
    window.impact_buttons[2].setChecked(True)
    window._on_next()
    assert window.current_index == 1
    window._on_back()
    assert window.current_index == 0
    assert window.answer_input.text() == "engineer"
    assert window.impact_buttons[2].isChecked()

    # Complete the rest, then edit the summary.
    for index in range(1, len(ONBOARDING_QUESTIONS)):
        window.current_index = index
        window._show_question()
        window.answer_input.setText(f"answer {index}")
        window.impact_buttons[1].setChecked(True)
        window._on_next()

    assert window.stack.currentIndex() == 2
    assert window.summary_table.rowCount() == len(ONBOARDING_QUESTIONS)
    window.summary_table.item(0, 1).setText("Senior Engineer")
    window.summary_table.item(0, 2).setText("3")
    window._on_finish()

    persona_path = tmp_path / ".config" / "system-sl" / "persona.json"
    assert persona_path.exists()
    data = json.loads(persona_path.read_text())
    assert data["responses"][0]["answer"] == "senior engineer"
    assert data["responses"][0]["impact"] == 3
    assert data["statistics"]["high_count"] >= 1


def test_rerunning_onboarding_starts_from_the_saved_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    QApplication.instance() or QApplication([])

    from system_sl.frontend.gui.popup_windows import OnboardingWindow

    first = OnboardingWindow()
    first._on_begin()
    for index in range(len(ONBOARDING_QUESTIONS)):
        first.answer_input.setText(f"answer {index}")
        first.impact_buttons[index % 3].setChecked(True)
        first._on_next()
    first._on_finish()

    again = OnboardingWindow()
    again._on_begin()
    assert again.answer_input.text() == "answer 0"
    assert again.impact_buttons[0].isChecked()
    assert len(again.responses) == len(ONBOARDING_QUESTIONS)

    # Changing one answer keeps the others.
    again.answer_input.setText("changed")
    again._on_next()
    again._show_completion()
    assert again.summary_table.item(0, 1).text() == "changed"
    assert again.summary_table.item(1, 1).text() == "answer 1"


def test_saved_answers_stop_at_the_first_gap(tmp_path):
    from system_sl.core.onboarding import PersonaStorageHandler

    handler = PersonaStorageHandler()
    handler.persona_file = tmp_path / "persona.json"
    ids = [q["id"] for q in ONBOARDING_QUESTIONS]
    handler.persona_file.write_text(json.dumps({"responses": [
        {"question_id": ids[0], "answer": "a", "impact": 1},
        {"question_id": ids[2], "answer": "c", "impact": 1},
    ]}))
    saved = handler.load_saved_responses(ONBOARDING_QUESTIONS)
    assert [r["answer"] for r in saved] == ["a"]

    handler.persona_file.write_text("{broken")
    assert handler.load_saved_responses(ONBOARDING_QUESTIONS) == []


def test_user_goals_no_longer_prompt():
    from system_sl.core import user_info

    assert user_info.user_goal_check("/nonexistent/path.json") == []