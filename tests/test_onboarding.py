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


def test_user_goals_no_longer_prompt():
    from system_sl.core import user_info

    assert user_info.user_goal_check("/nonexistent/path.json") == []