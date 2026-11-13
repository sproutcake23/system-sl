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


def test_quitting_mid_reply_records_the_task_outcome(panel):
    """A Yes/No answered while the reply streams is saved when the app quits."""
    widget, app = panel

    class _Slow(_Model):
        def _next_reply(self, messages):
            time.sleep(0.3)
            self.seen.append(list(messages))
            self.calls += 1
            return AIMessage(content="word " * 200)

    widget._model.__class__ = _Slow
    widget.input_box.setPlainText("hello")
    widget._on_send()
    deadline = time.monotonic() + 5
    while not widget._streamed_any and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert widget.is_busy()

    # The Player answers a proposal while the reply is still streaming.
    widget._record_task_confirmation(declined=[("Gym", "health")])
    assert widget._pending_confirmations, "the write must wait for the turn"

    widget.shutdown()

    assert widget._thread is None
    texts = [t for _k, t in _stored(widget)]
    assert any("Gym" in t for t in texts), "the outcome was lost on quit"


def test_attachment_text_is_compacted_even_when_the_reply_fails(panel, tmp_path):
    widget, app = panel
    widget._model.fail_on = [1]
    doc = tmp_path / "notes.txt"
    doc.write_text("lorem ipsum dolor sit amet " * 400)

    widget._add_attachments([doc])
    _send(widget, app, "summarise this")

    humans = [t for k, t in _stored(widget) if k == "human"]
    assert len(humans) == 1
    assert "notes.txt" in humans[0]
    assert len(humans[0]) < 3_000, "the full document stayed in the history"


def test_streaming_writes_completed_blocks_once_and_keeps_selection(panel):
    """A growing answer is appended to, and a selection inside it survives."""
    widget, app = panel
    widget._current_model = "fake-model"
    widget._reset_stream_state()
    widget._start_answer_line()

    assert widget._stable_answer_boundary("alpha\n\nbeta") == 7
    assert widget._stable_answer_boundary("```\na\n\nb\n```") == 0

    for chunk in ["alpha\n\n", "beta", " gamma\n\n", "tail"]:
        widget._on_chunk(chunk)
        widget._render_answer()

    text = widget.transcript.toPlainText()
    assert "alpha" in text and "gamma" in text and "tail" in text
    # The completed part stays in place; only the tail is rewritten.
    assert widget._answer_committed_chars > 0

    # Put a selection inside the answer and grow the tail: it must survive.
    doc = widget.transcript.document()
    from PySide6.QtGui import QTextCursor

    cursor = QTextCursor(doc)
    cursor.setPosition(widget._answer_start + 1)
    cursor.setPosition(widget._answer_start + 6, QTextCursor.KeepAnchor)
    widget.transcript.setTextCursor(cursor)
    selected = widget.transcript.textCursor().selectedText()

    widget._on_chunk(" more")
    widget._render_answer()

    after = widget.transcript.textCursor()
    assert after.hasSelection()
    assert after.selectedText() == selected


def test_edit_bar_warns_when_documents_are_only_names(panel):
    widget, _app = panel
    from system_sl.chatbot.transcript import USER, ChatMessage

    message = ChatMessage(
        id="m-doc",
        role=USER,
        text="summarise this",
        documents=["report.pdf"],
    )
    widget._messages.messages.append(message)
    widget._edit_and_resend("m-doc")
    assert "only names" in widget.edit_label.text()
    assert "not resent" in widget.edit_label.text()
    widget._cancel_edit()


def _blocks(widget):
    doc = widget.transcript.document()
    block, out = doc.firstBlock(), []
    while block.isValid():
        data = block.userData()
        if block.text():
            out.append((block.text(), getattr(data, "message_id", None)))
        block = block.next()
    return out


def _stream(widget, deltas):
    for delta in deltas:
        widget._on_chunk(delta)
        widget._render_answer()


def test_streamed_paragraphs_stay_separate(panel):
    widget, _app = panel
    widget.transcript.clear()
    widget._reset_stream_state()
    widget._start_answer_line()
    _stream(widget, ["First para.", "\n\n", "Second para", " grows."])
    texts = [t for t, _ in _blocks(widget)]
    assert "First para." in texts and "Second para grows." in texts


def test_note_appended_mid_stream_is_not_swallowed(panel):
    widget, _app = panel
    widget.transcript.clear()
    widget._reset_stream_state()
    widget._start_answer_line()
    _stream(widget, ["Hello there", " friend."])
    answer_id = widget._stream_message_id
    widget._append_note("A note")
    _stream(widget, [" More", " words."])
    blocks = _blocks(widget)
    assert blocks[-1][0] == "A note" and blocks[-1][1] != answer_id
    assert ("Hello there friend. More words.", answer_id) in blocks


def test_final_reply_rerenders_whole_answer(panel):
    widget, _app = panel
    widget.transcript.clear()
    widget._reset_stream_state()
    widget._start_answer_line()
    _stream(widget, ["Para one.", "\n\n", "Para two"])
    widget._on_reply("Completely different reply.")
    texts = [t for t, _ in _blocks(widget)]
    assert texts[-1] == "Completely different reply."
    assert "Para one." not in texts


def test_alert_bubble_renders(panel):
    widget, _app = panel
    widget.transcript.clear()
    widget._append_alert("Something failed")
    assert any("Something failed" in t for t, _ in _blocks(widget))
