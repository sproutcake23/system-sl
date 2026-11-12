"""Tests for the chat context: dynamic block, attachments and trimming.

The chat model is a stub (``fake_chat_model.FakeChatModel``), so nothing here
touches the network or a real provider.
"""

import sys
from pathlib import Path

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage

sys.path.insert(0, str(Path(__file__).parent))

from fake_chat_model import FakeChatModel  # noqa: E402

from system_sl.chatbot import memory as memory_mod  # noqa: E402
from system_sl.chatbot.client import ChatSession  # noqa: E402
from system_sl.chatbot.system import SYSTEM_TOOLS  # noqa: E402


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
    from system_sl.chatbot.agent_middleware import make_context_middleware
    from system_sl.chatbot.system_prompts import SYSTEM_MENTOR_PROMPT

    model = FakeChatModel(replies=list(replies))
    agent = create_agent(
        model=model,
        tools=tools if tools is not None else SYSTEM_TOOLS,
        system_prompt=SYSTEM_MENTOR_PROMPT,
        middleware=[make_context_middleware()],
        checkpointer=memory_mod.get_checkpointer(),
    )
    return agent, model


def _texts(messages) -> list[str]:
    return [str(getattr(message, "content", "")) for message in messages]


def _humans(messages) -> list[str]:
    return [str(m.content) for m in messages if getattr(m, "type", "") == "human"]


def _system_text(messages) -> str:
    for message in messages:
        if getattr(message, "type", "") == "system":
            return str(getattr(message, "content", ""))
    return ""


# =============================================================================
# 1. THE DYNAMIC CONTEXT BLOCK
# =============================================================================

def test_context_block_is_in_the_system_prompt_not_the_user_message(config_dir):
    """The date, the persona and the tasks travel in the system message."""
    agent, model = _agent(AIMessage(content="hello"))
    session = ChatSession(thread_id="conv-context", agent=agent)

    session.run_turn("hey")

    prompt = _system_text(model.seen[-1])
    assert "[CONTEXT" in prompt
    assert "Now:" in prompt
    assert "Persona:" in prompt
    assert "[/CONTEXT]" in prompt

    # The Player's message is exactly what they typed.
    assert _humans(session.messages()) == ["hey"]


def test_context_block_is_not_stored_in_the_history(config_dir):
    """The block is rebuilt every turn; it must not pile up in the history."""
    agent, model = _agent(AIMessage(content="one"), AIMessage(content="two"))
    session = ChatSession(thread_id="conv-context-store", agent=agent)

    session.run_turn("first")
    session.run_turn("second")

    stored = _texts(session.messages())
    assert not any("[CONTEXT" in text for text in stored)
    assert not any("Now:" in text for text in stored)
    assert _humans(session.messages()) == ["first", "second"]


def test_context_block_refreshes_between_turns(config_dir):
    """The date in the block is the date of the turn, not of the session."""
    from datetime import datetime, timedelta

    from system_sl.chatbot import dynamic_context

    tomorrow = datetime.now() + timedelta(days=1)
    block = dynamic_context.build_context_block(now=tomorrow)
    assert tomorrow.strftime("%Y-%m-%d") in block


def test_read_persona_returns_a_summary_not_raw_json(config_dir, monkeypatch):
    """The persona tool answers with prose, not the whole profile file."""
    import json

    from system_sl.chatbot import system as system_mod

    (config_dir / "persona.json").write_text(
        json.dumps(
            {
                "version": "1.0",
                "player_name": "tester",
                "responses": [
                    {"category": "IDENTITY", "answer": "ship things"},
                    {"category": "VALUES", "answer": "honesty"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        system_mod, "get_tasks_file_path", lambda name: str(config_dir / name)
    )

    summary = system_mod.read_persona.invoke({})
    assert "tester" in summary
    assert "ship things" in summary
    assert "honesty" in summary
    # The questionnaire itself is not useful to the model.
    assert "IDENTITY" not in summary
    assert "responses" not in summary


def test_read_user_info_is_gone():
    """The legacy keyword tool was removed with its prompt mention."""
    from system_sl.chatbot import system as system_mod

    names = {getattr(tool, "name", "") for tool in SYSTEM_TOOLS}
    assert "read_user_info" not in names
    assert not hasattr(system_mod, "read_user_info")

    from system_sl.chatbot.system_prompts import SYSTEM_MENTOR_PROMPT

    assert "read_user_info" not in SYSTEM_MENTOR_PROMPT


# =============================================================================
# 7. THE SYSTEM PROMPT
# =============================================================================

def test_system_prompt_has_a_tool_policy():
    """The old prompt forced a tool call on every turn."""
    from system_sl.chatbot.system_prompts import SYSTEM_MENTOR_PROMPT

    assert "Tool policy:" in SYSTEM_MENTOR_PROMPT
    assert "Only call" in SYSTEM_MENTOR_PROMPT
    # The old line is gone.
    assert "Read persona and current tasks before giving substantive advice" not in (
        SYSTEM_MENTOR_PROMPT
    )


def test_a_casual_question_triggers_no_tool_call(config_dir):
    """A greeting is answered from the context block, without a round trip."""
    agent, model = _agent(AIMessage(content="hey, keep going"))
    session = ChatSession(thread_id="conv-casual", agent=agent)

    session.run_turn("hey")

    # The model was called once and never asked for a tool.
    assert model.calls == 1
    assert not any(
        getattr(message, "tool_calls", None) for message in model.seen[-1]
    )
