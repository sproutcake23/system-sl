"""Regenerate / retry / edit-and-resend in the real chat panel (offscreen Qt).

HOME points at a temporary directory, so no real config, conversation or
task file is touched, and the agent is a scripted fake (no network).
"""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from langchain.agents import create_agent  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

from fake_chat_model import FakeChatModel  # noqa: E402


class _Model(FakeChatModel):
    """Answers "reply N"; fails on the call numbers in ``fail_on``."""

    fail_on: list = []

    def _next_reply(self, messages):
        self.seen.append(list(messages))
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError("provider exploded")
        return AIMessage(content=f"reply {self.calls}")


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    from system_sl.chatbot import memory as memory_mod
    from system_sl.chatbot import system as system_mod
    from system_sl.frontend.gui import chat_panel as chat_panel_mod

    memory_mod.close_checkpointer()
    model = _Model(replies=[])

    def build_agent_for_model(_name):
        agent = create_agent(
            model=model,
            tools=[],
            system_prompt="test",
            checkpointer=memory_mod.get_checkpointer(),
        )
        return agent, None

    monkeypatch.setattr(system_mod, "build_agent_for_model", build_agent_for_model)
    monkeypatch.setattr(chat_panel_mod.ChatPanel, "_start_discovery", lambda self: None)
    monkeypatch.setattr(
        chat_panel_mod.QMessageBox,
        "question",
        staticmethod(lambda *a, **k: chat_panel_mod.QMessageBox.StandardButton.Yes),
    )

    widget = chat_panel_mod.ChatPanel()
    widget._current_model = "fake-model"
    widget._model = model
    yield widget, app
    widget.deleteLater()
    app.processEvents()
    memory_mod.close_checkpointer()


def _send(widget, app, text):
    widget.input_box.setPlainText(text)
    widget._on_send()
    _wait(widget, app)


def _wait(widget, app, timeout=10.0):
    deadline = time.monotonic() + timeout
    app.processEvents()
    while (widget.is_busy() or widget._thread is not None) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    for _ in range(5):
        app.processEvents()
    assert not widget.is_busy(), "the reply did not finish"


def _stored(widget):
    from system_sl.chatbot.memory import load_thread_messages

    return [(m.type, str(m.content).strip()) for m in load_thread_messages(widget._thread_id)]


def _shown(widget):
    from system_sl.chatbot.transcript import ALERT, ASSISTANT, USER

    return [
        (m.role, (m.text or "").strip())
        for m in widget._messages
        if m.role in (USER, ASSISTANT, ALERT)
    ]


def test_regenerate_replaces_the_reply_everywhere(panel):
    widget, app = panel
    _send(widget, app, "hello")
    assert _stored(widget) == [("human", "hello"), ("ai", "reply 1")]

    reply = widget._messages.last("assistant")
    widget._regenerate_reply(reply.id)
    _wait(widget, app)

    assert _stored(widget) == [("human", "hello"), ("ai", "reply 2")]
    assert _shown(widget) == [("user", "hello"), ("assistant", "reply 2")]
    # The model never saw the discarded reply.
    assert not any("reply 1" in str(m.content) for m in widget._model.seen[-1])


def test_regenerate_keeps_the_quotes(panel):
    from system_sl.chatbot.quotes import Quote

    widget, app = panel
    widget._quotes = [Quote(speaker="assistant", text="earlier words")]
    _send(widget, app, "explain this")
    widget._regenerate_reply(widget._messages.last("assistant").id)
    _wait(widget, app)

    humans = [text for kind, text in _stored(widget) if kind == "human"]
    assert len(humans) == 1
    assert "earlier words" in humans[0] and "explain this" in humans[0]


def test_edit_and_resend_rewinds_on_send(panel):
    widget, app = panel
    _send(widget, app, "first")
    _send(widget, app, "second")

    first = next(m for m in widget._messages if m.role == "user" and m.body == "first")
    widget._edit_and_resend(first.id)
    # Nothing changes until the edit is sent.
    assert len(_stored(widget)) == 4
    assert widget.input_box.toPlainText() == "first"

    widget.input_box.setPlainText("first, edited")
    widget._on_send()
    _wait(widget, app)

    assert [t for k, t in _stored(widget) if k == "human"] == ["first, edited"]
    assert [r for r, _ in _shown(widget)] == ["user", "assistant"]
    assert widget._editing_message is None


def test_cancelling_an_edit_changes_nothing(panel):
    widget, app = panel
    _send(widget, app, "keep me")
    widget._edit_and_resend(widget._messages.last("user").id)
    widget._cancel_edit()
    assert widget.input_box.toPlainText() == ""
    assert widget._editing_message is None
    assert _stored(widget) == [("human", "keep me"), ("ai", "reply 1")]


def test_retry_after_a_failed_reply(panel):
    widget, app = panel
    widget._model.fail_on = [1]
    _send(widget, app, "hello")
    alert = widget._messages.last("alert")
    assert alert is not None

    menu, entries = widget._build_transcript_menu(alert.id, "")
    labels = [action.text() for action, _ in entries]
    assert "Retry" in labels

    widget._retry_message(alert.id)
    _wait(widget, app)
    assert [t for k, t in _stored(widget) if k == "human"] == ["hello"]
    assert ("ai", "reply 2") in _stored(widget)
    assert ("assistant", "reply 2") in _shown(widget)
