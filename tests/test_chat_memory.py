"""Tests for persistent chat memory (checkpointer + conversation index).

The chat model is a stub (``fake_chat_model.FakeChatModel``), so nothing here
touches the network or a real provider.
"""

import sys
from pathlib import Path

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

sys.path.insert(0, str(Path(__file__).parent))

from fake_chat_model import FakeChatModel  # noqa: E402

from system_sl.chatbot import memory as memory_mod  # noqa: E402
from system_sl.chatbot.client import ChatSession  # noqa: E402
from system_sl.chatbot.system import SYSTEM_TOOLS, reset_agent_cache  # noqa: E402


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Point chat memory at a throwaway config directory."""
    directory = tmp_path / "system-sl"
    directory.mkdir()
    monkeypatch.setattr(
        memory_mod, "get_tasks_file_path", lambda name: str(directory / name)
    )
    memory_mod.close_checkpointer()
    yield directory
    memory_mod.close_checkpointer()


def _agent(*replies: AIMessage, tools=None):
    """Build an agent on the shared checkpointer and return ``(agent, model)``."""
    model = FakeChatModel(replies=list(replies))
    agent = create_agent(
        model=model,
        tools=tools if tools is not None else SYSTEM_TOOLS,
        system_prompt="test prompt",
        checkpointer=memory_mod.get_checkpointer(),
    )
    return agent, model


def _texts(messages) -> list[str]:
    return [str(getattr(message, "content", "")) for message in messages]


def _humans(messages) -> list[str]:
    return [str(m.content) for m in messages if getattr(m, "type", "") == "human"]


# =============================================================================
# 1. PERSISTENT MEMORY
# =============================================================================

def test_second_turn_sees_the_first(config_dir):
    """(a) Turn 2's state contains turn 1, both on disk and in the prompt."""
    agent, model = _agent(AIMessage(content="first answer"), AIMessage(content="second answer"))
    session = ChatSession(thread_id="conv-two-turns", agent=agent, model_name="fake")

    session.run_turn("my code word is aurora")
    session.run_turn("what is my code word?")

    stored = _texts(session.messages())
    assert any("aurora" in text for text in stored)
    assert "first answer" in stored
    assert len(_humans(session.messages())) == 2

    # The model's second prompt carried the first exchange, not just the new
    # message — this is what used to be missing.
    second_prompt = _texts(model.seen[-1])
    assert any("aurora" in text for text in second_prompt)
    assert any("first answer" in text for text in second_prompt)


def test_memory_survives_model_switch_and_agent_reset(config_dir):
    """(b) A new model on the same thread continues the conversation."""
    agent_a, model_a = _agent(AIMessage(content="noted, aurora it is"))
    session = ChatSession(thread_id="conv-switch", agent=agent_a, model_name="model-a")
    session.run_turn("remember: aurora")
    assert "aurora" in str(model_a.seen[-1])

    checkpointer_before = memory_mod.get_checkpointer()
    reset_agent_cache()
    # The memory is not an agent-cache entry: resetting the agent must not
    # drop it.
    assert memory_mod.get_checkpointer() is checkpointer_before

    agent_b, model_b = _agent(AIMessage(content="you said aurora"))
    session.switch_model("model-b", agent=agent_b)
    session.run_turn("what did I just say?")

    assert session.model_name == "model-b"
    assert "aurora" in str(model_b.seen[-1]), "the new model did not get the old history"
    assert len(_humans(session.messages())) == 2


def test_memory_survives_restart(config_dir):
    """(c) A new checkpointer instance on the same file still has the history."""
    agent, _model = _agent(AIMessage(content="before the restart"))
    session = ChatSession(thread_id="conv-restart", agent=agent)
    session.run_turn("I like green tea")
    assert any("green tea" in text for text in _texts(session.messages()))

    # Simulate quitting the app: drop the connection, then reopen the file.
    memory_mod.close_checkpointer()
    agent_after, model_after = _agent(AIMessage(content="yes, green tea"))
    reopened = ChatSession(thread_id="conv-restart", agent=agent_after)
    reopened.run_turn("what do I like?")

    assert "green tea" in str(model_after.seen[-1])
    assert len(_humans(reopened.messages())) == 2


def test_stop_leaves_a_repairable_checkpoint(config_dir):
    """A cancelled tool call is closed before the next turn, not re-run."""
    calls: list[str] = []

    @tool
    def failing_tool(category: str, title: str) -> str:
        """Pretend to add a task, failing the first time like a cancelled turn."""
        calls.append(title)
        raise RuntimeError("stream cancelled")

    agent, _model = _agent(
        AIMessage(
            content="Adding that now.",
            tool_calls=[
                {
                    "name": "failing_tool",
                    "args": {"category": "study", "title": "Wave equation"},
                    "id": "call_1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="continuing"),
        tools=[failing_tool],
    )
    session = ChatSession(thread_id="conv-stop", agent=agent)

    # The turn dies mid-tool, leaving LangGraph with a pending tool task.
    with pytest.raises(RuntimeError):
        session.run_turn("add wave equation")
    assert agent.get_state(session.config).next == ("tools",)

    session.run_turn("never mind, what next?")
    messages = session.messages()

    assert calls == ["Wave equation"], "the cancelled tool must not run twice"
    assert agent.get_state(session.config).next == ()
    assert not any(getattr(m, "tool_calls", None) for m in messages)
    assert memory_mod.STOP_NOTE in _texts(messages)
    # What the model had already written is kept.
    assert "Adding that now." in _texts(messages)
    assert "continuing" in _texts(messages)


# =============================================================================
# 2. CONVERSATION INDEX
# =============================================================================

def test_conversation_index_tracks_title_model_and_active(config_dir):
    first = memory_mod.create_conversation(model_name="model-a")
    second = memory_mod.create_conversation(model_name="model-b")

    assert memory_mod.active_conversation_id() == second["id"]
    assert [c["id"] for c in memory_mod.list_conversations()][0] == second["id"]

    memory_mod.update_conversation(
        first["id"], title_if_empty=memory_mod.title_from_message("Plan my week please")
    )
    memory_mod.select_conversation(first["id"])
    stored = memory_mod.get_conversation(first["id"])

    assert stored["title"] == "Plan my week please"
    assert stored["last_model"] == "model-a"
    assert memory_mod.active_conversation_id() == first["id"]

    # A later title must not overwrite the first one.
    memory_mod.update_conversation(
        first["id"], title_if_empty=memory_mod.title_from_message("something else")
    )
    assert memory_mod.get_conversation(first["id"])["title"] == "Plan my week please"


def test_titles_are_short_and_ignore_the_date_prefix(config_dir):
    title = memory_mod.title_from_message("[Today's date: 2026-11-12]\n   Rank my tasks  ")
    assert title == "Rank my tasks"

    long_title = memory_mod.title_from_message("word " * 40)
    assert len(long_title) <= 61
    assert long_title.endswith("…")


def test_deleting_a_conversation_forgets_its_messages(config_dir):
    entry = memory_mod.create_conversation(model_name="fake")
    agent, _model = _agent(AIMessage(content="hello"))
    session = ChatSession(thread_id=entry["id"], agent=agent)
    session.run_turn("hi there")
    assert session.messages()

    memory_mod.delete_conversation(entry["id"])

    assert memory_mod.get_conversation(entry["id"]) is None
    assert memory_mod.load_thread_messages(entry["id"]) == []


# =============================================================================
# 3. TASK CONFIRMATION FEEDBACK
# =============================================================================

def test_confirmation_note_is_in_state_before_the_next_turn(config_dir):
    """(d) The confirmation result is part of the history the model reads."""
    agent, model = _agent(AIMessage(content="shall I add those?"), AIMessage(content="yes"))
    session = ChatSession(thread_id="conv-confirm", agent=agent)
    session.run_turn("suggest tasks for today")

    stored = session.record_task_confirmation(
        added=[("Read chapter 3", "study")],
        declined=[("Old idea", "work")],
        failed=[("Read chapter 3", "study")],
    )

    assert stored is True
    notes = [
        text
        for text in _texts(session.messages())
        if memory_mod.APP_NOTE_PREFIX in text
    ]
    assert len(notes) == 1, "one summarizing note, not one message per task"
    assert "Read chapter 3" in notes[0]
    assert "Old idea" in notes[0]
    assert "not added" in notes[0]

    session.run_turn("did you add it?")
    last_prompt = _texts(model.seen[-1])
    assert any("Read chapter 3" in text for text in last_prompt)
    # The note is not duplicated by the next turn.
    assert sum("Old idea" in text for text in _texts(session.messages())) == 1


def test_single_confirmation_wrapper_writes_one_note(config_dir):
    agent, _model = _agent(AIMessage(content="proposed"))
    session = ChatSession(thread_id="conv-single", agent=agent)
    session.run_turn("propose something")

    assert session.inject_confirmation_result("Read chapter 3", "study", True) is True
    assert session.inject_confirmation_result("Old idea", "work", False) is True

    notes = [
        text
        for text in _texts(session.messages())
        if memory_mod.APP_NOTE_PREFIX in text
    ]
    assert len(notes) == 2
    assert "confirmed" in notes[0]
    assert "declined" in notes[1]


def test_confirmation_without_a_conversation_is_harmless(config_dir):
    """Recording a result before any turn is written, not raised.

    ``update_state`` on a thread with no checkpoint may or may not create one;
    either way the caller gets a boolean instead of an exception.
    """
    agent, _model = _agent(AIMessage(content="unused"))
    session = ChatSession(thread_id="conv-empty", agent=agent)
    assert isinstance(session.record_task_confirmation(added=[("x", "study")]), bool)


# =============================================================================
# HELPERS
# =============================================================================

def test_thread_messages_are_plain_langchain_messages(config_dir):
    agent, _model = _agent(AIMessage(content="answer"))
    session = ChatSession(thread_id="conv-types", agent=agent)
    session.run_turn("question")

    for message in session.messages():
        assert isinstance(message, (HumanMessage, AIMessage, ToolMessage))


# =============================================================================
# 4. INTERRUPTED TURNS (Stop at every point of the graph)
# =============================================================================


class _ScriptedModel(FakeChatModel):
    """Raise on the listed call numbers (1-based), otherwise answer from replies."""

    fail_on: list = []

    def _next_reply(self, messages):
        self.seen.append(list(messages))
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError("stream cancelled")
        index = min(self.calls - 1 - len([n for n in self.fail_on if n < self.calls]), len(self.replies) - 1)
        return self.replies[max(index, 0)]


def _call(name: str, call_id: str, **args) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _scripted_agent(replies, fail_on, tools):
    model = _ScriptedModel(replies=list(replies), fail_on=list(fail_on))
    agent = create_agent(
        model=model,
        tools=tools,
        system_prompt="test prompt",
        checkpointer=memory_mod.get_checkpointer(),
    )
    return agent, model


def _assert_clean_history(messages):
    """No dangling tool call and no orphan tool result."""
    asked = {c["id"] for m in messages for c in (getattr(m, "tool_calls", None) or [])}
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    assert asked == answered, f"unpaired tool traffic: asked={asked} answered={answered}"


def _record_tool(calls):
    @tool
    def record(title: str) -> str:
        """Record a title."""
        calls.append(title)
        return f"recorded {title}"

    return record


def test_stop_before_any_model_output_is_repaired(config_dir):
    """(a) The model step dies before writing anything."""
    agent, model = _scripted_agent([AIMessage(content="hello back")], fail_on=[1], tools=[])
    session = ChatSession(thread_id="conv-stop-a", agent=agent)

    with pytest.raises(RuntimeError):
        session.run_turn("hello")
    assert agent.get_state(session.config).next == ("model",)

    assert session.run_turn("hello again") == "hello back"
    assert agent.get_state(session.config).next == ()
    assert len(_humans(session.messages())) == 2
    _assert_clean_history(session.messages())


def test_stop_after_tools_ran_keeps_their_results(config_dir):
    """(c) Stop while the final answer streams, after a tool already ran.

    This used to raise ``KeyError: 'model'`` in the repair and leave the
    conversation unusable for good.
    """
    calls: list[str] = []
    agent, model = _scripted_agent(
        [
            AIMessage(content="Recording.", tool_calls=[_call("record", "c1", title="Relativity")]),
            AIMessage(content="all good"),
        ],
        fail_on=[2],
        tools=[_record_tool(calls)],
    )
    session = ChatSession(thread_id="conv-stop-c", agent=agent)

    with pytest.raises(RuntimeError):
        session.run_turn("record relativity")
    assert agent.get_state(session.config).next == ("model",)

    assert session.run_turn("what happened?") == "all good"
    messages = session.messages()

    assert calls == ["Relativity"], "the tool must not run again"
    assert agent.get_state(session.config).next == ()
    _assert_clean_history(messages)
    # The tool really ran: its call and result stay, and the model is told so.
    assert any(isinstance(m, ToolMessage) and "Relativity" in str(m.content) for m in messages)
    stop_notes = [str(m.content) for m in messages if memory_mod.is_app_note(str(m.content))]
    assert len(stop_notes) == 1
    assert "record" in stop_notes[0]
    assert "nothing was changed" not in stop_notes[0]
    # And the model saw it on the next turn.
    assert any("record" in str(getattr(m, "content", "")) and memory_mod.STOP_NOTE_PREFIX in str(getattr(m, "content", "")) for m in model.seen[-1])


def test_stop_after_parallel_tools_ran(config_dir):
    """(c) with two tool calls that both finished."""
    calls: list[str] = []
    agent, _model = _scripted_agent(
        [
            AIMessage(
                content="",
                tool_calls=[_call("record", "c1", title="A"), _call("record", "c2", title="B")],
            ),
            AIMessage(content="ok"),
        ],
        fail_on=[2],
        tools=[_record_tool(calls)],
    )
    session = ChatSession(thread_id="conv-stop-parallel", agent=agent)

    with pytest.raises(RuntimeError):
        session.run_turn("record both")
    session.run_turn("continue")

    assert sorted(calls) == ["A", "B"]
    assert agent.get_state(session.config).next == ()
    _assert_clean_history(session.messages())


def test_repair_is_idempotent_and_reports_nothing_to_do(config_dir):
    agent, _model = _scripted_agent([AIMessage(content="hi")], fail_on=[1], tools=[])
    session = ChatSession(thread_id="conv-stop-idem", agent=agent)
    with pytest.raises(RuntimeError):
        session.run_turn("hello")

    assert memory_mod.repair_interrupted_turn(agent, session.config) is True
    assert memory_mod.repair_interrupted_turn(agent, session.config) is False
    assert agent.get_state(session.config).next == ()


def test_stopped_turn_renders_as_a_note(config_dir):
    """The closing message is shown dimmed, not as something the System said."""
    from system_sl.chatbot.transcript import ASSISTANT, NOTE, Transcript

    calls: list[str] = []
    agent, _model = _scripted_agent(
        [AIMessage(content="", tool_calls=[_call("record", "c1", title="X")]), AIMessage(content="done")],
        fail_on=[2],
        tools=[_record_tool(calls)],
    )
    session = ChatSession(thread_id="conv-stop-render", agent=agent)
    with pytest.raises(RuntimeError):
        session.run_turn("record x")
    memory_mod.repair_interrupted_turn(agent, session.config)

    transcript = Transcript.from_history(session.messages())
    roles = [m.role for m in transcript]
    assert roles[-1] == NOTE
    assert ASSISTANT not in roles
    assert not transcript.messages[-1].text.startswith(memory_mod.STOP_NOTE_PREFIX)


def test_rollback_keeps_answered_calls_and_drops_the_rest():
    """A pending tools step with one call answered and one not."""
    from langchain_core.messages import HumanMessage

    messages = [
        HumanMessage(content="add two", id="h1"),
        AIMessage(
            content="Adding.",
            tool_calls=[_call("add_task", "c1", title="Real"), _call("add_task", "c2", title="Pending")],
            id="a1",
        ),
        ToolMessage(content="added Real", tool_call_id="c1", name="add_task", id="t1"),
    ]
    patched, note = memory_mod._rollback_messages(messages)

    assert patched.id == "a1"
    assert [c["id"] for c in patched.tool_calls] == ["c1"]
    assert patched.content == "Adding."
    assert "nothing was changed" not in note.content
    assert memory_mod.is_app_note(note.content)


# =============================================================================
# 5. REWIND (regenerate / retry / edit-and-resend)
# =============================================================================


def test_regenerate_rewind_leaves_one_copy_of_the_question(config_dir):
    agent, model = _agent(
        AIMessage(content="first try"),
        AIMessage(content="second try"),
    )
    session = ChatSession(thread_id="conv-regen", agent=agent)
    session.run_turn("what is 2+2?", message_id="q1")

    removed = session.rewind_before("q1")
    assert removed == 2
    assert session.messages() == []

    session.run_turn("what is 2+2?", message_id="q1-again")
    stored = session.messages()
    assert _humans(stored) == ["what is 2+2?"]
    assert "first try" not in _texts(stored)
    assert "second try" in _texts(stored)
    # The model never saw the discarded answer.
    assert not any("first try" in text for text in _texts(model.seen[-1]))


def test_edit_rewind_drops_the_message_and_everything_after(config_dir):
    calls: list[str] = []
    agent, model = _agent(
        AIMessage(content="ok one"),
        AIMessage(content="", tool_calls=[_call("record", "c1", title="T")]),
        AIMessage(content="ok two"),
        AIMessage(content="edited answer"),
        tools=[_record_tool(calls)],
    )
    session = ChatSession(thread_id="conv-edit", agent=agent)
    session.run_turn("one", message_id="u1")
    session.run_turn("two", message_id="u2")
    assert len(session.messages()) == 6  # h, ai, h, ai(call), tool, ai

    session.rewind_before("u2")
    assert _humans(session.messages()) == ["one"]
    _assert_clean_history(session.messages())

    session.run_turn("two, edited", message_id="u2b")
    assert _humans(session.messages()) == ["one", "two, edited"]
    assert agent.get_state(session.config).next == ()
    assert not any("two" == text for text in _texts(model.seen[-1]))


def test_rewind_after_an_interrupted_turn(config_dir):
    """Retry after a failed turn: repair, then rewind past the failed question."""
    agent, model = _scripted_agent([AIMessage(content="answer")], fail_on=[1], tools=[])
    session = ChatSession(thread_id="conv-retry", agent=agent)
    with pytest.raises(RuntimeError):
        session.run_turn("hello", message_id="h1")

    session.rewind_before("h1")
    assert session.messages() == []
    assert session.run_turn("hello", message_id="h2") == "answer"
    assert _humans(session.messages()) == ["hello"]


def test_rewind_unknown_message_raises_lookup_error(config_dir):
    agent, _model = _agent(AIMessage(content="hi"))
    session = ChatSession(thread_id="conv-rewind-missing", agent=agent)
    session.run_turn("hello", message_id="known")
    with pytest.raises(LookupError):
        session.rewind_before("never-stored")
    assert _humans(session.messages()) == ["hello"]


def test_transcript_keeps_the_stored_message_ids(config_dir):
    from system_sl.chatbot.transcript import ASSISTANT, USER, Transcript

    agent, _model = _agent(AIMessage(content="hi"))
    session = ChatSession(thread_id="conv-ids", agent=agent)
    session.run_turn("hello", message_id="stored-1")

    transcript = Transcript.from_history(session.messages())
    user = next(m for m in transcript if m.role == USER)
    reply = next(m for m in transcript if m.role == ASSISTANT)
    assert user.source_id == "stored-1"
    assert reply.source_id


def test_confirmation_after_a_stopped_turn_leaves_the_thread_idle(config_dir):
    agent, _model = _scripted_agent([AIMessage(content="ok")], fail_on=[1], tools=[])
    session = ChatSession(thread_id="conv-note-after-stop", agent=agent)
    with pytest.raises(RuntimeError):
        session.run_turn("hello")
    memory_mod.repair_interrupted_turn(agent, session.config)

    assert session.record_task_confirmation(declined=[("Gym", "health")])
    assert agent.get_state(session.config).next == ()
    # No second stop note appears on the next turn.
    session.run_turn("next")
    notes = [t for t in _texts(session.messages()) if t.startswith(memory_mod.STOP_NOTE_PREFIX)]
    assert len(notes) == 1
