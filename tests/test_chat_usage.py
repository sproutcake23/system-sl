"""Tests for per-reply token usage and cost estimates."""

import sys
from pathlib import Path

from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).parent))

from fake_chat_model import FakeChatModel  # noqa: E402

from system_sl.chatbot import memory as memory_mod  # noqa: E402
from system_sl.chatbot.client import ChatSession  # noqa: E402
from system_sl.chatbot.usage import (  # noqa: E402
    TokenUsage,
    estimate_cost,
    format_usage,
    merge_usage,
    usage_from_message,
    usage_from_metadata,
)


def test_usage_from_langchain_metadata():
    usage = usage_from_metadata(
        {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
    )
    assert usage == TokenUsage(120, 30, 150)


def test_usage_from_openai_response_metadata():
    message = AIMessage(
        content="hi",
        response_metadata={"token_usage": {"prompt_tokens": 10, "completion_tokens": 4}},
    )
    usage = usage_from_message(message)
    # The fallback shape is not the normalised one; it is only recognised when
    # it uses LangChain's field names.
    assert usage is None

    message = AIMessage(
        content="hi",
        response_metadata={"token_usage": {"input_tokens": 10, "output_tokens": 4}},
    )
    assert usage_from_message(message) == TokenUsage(10, 4, 14)


def test_merge_adds_chunk_and_call_usage():
    # LangChain chunk usage is additive (Gemini deltas, Anthropic's split
    # input/output chunks, one usage chunk per call on OpenAI-compatible APIs).
    first = TokenUsage(10, 0, 10)
    second = TokenUsage(0, 5, 5)
    assert merge_usage(first, second) == TokenUsage(10, 5, 15)
    # A second model call in the same turn (after a tool call) adds up too.
    assert merge_usage(TokenUsage(10, 5, 15), TokenUsage(20, 3, 23)) == TokenUsage(30, 8, 38)
    assert merge_usage(None, first) == first
    assert merge_usage(first, None) == first


def test_cost_uses_cached_prices(monkeypatch):
    import system_sl.chatbot.usage as usage_mod

    monkeypatch.setattr(usage_mod, "model_prices", lambda name: (3.0, 15.0))
    usage = TokenUsage(1_000_000, 1_000_000, 2_000_000)
    assert estimate_cost(usage, "model") == 18.0
    assert estimate_cost(None, "model") is None


def test_format_distinguishes_free_from_unknown(monkeypatch):
    import system_sl.chatbot.usage as usage_mod

    usage = TokenUsage(1_000, 500, 1_500)
    monkeypatch.setattr(usage_mod, "model_prices", lambda name: (0.0, 0.0))
    assert "free" in format_usage(usage, "free-model")
    monkeypatch.setattr(usage_mod, "model_prices", lambda name: None)
    assert "free" not in format_usage(usage, "unknown-model")


def test_run_turn_records_usage(tmp_path, monkeypatch):
    directory = tmp_path / "system-sl"
    directory.mkdir()
    monkeypatch.setattr(
        memory_mod, "get_tasks_file_path", lambda name: str(directory / name)
    )
    memory_mod.close_checkpointer()

    from langchain.agents import create_agent

    from system_sl.chatbot.agent_middleware import make_context_middleware
    from system_sl.chatbot.system_prompts import SYSTEM_MENTOR_PROMPT

    model = FakeChatModel(
        replies=[
            AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 11, "output_tokens": 3, "total_tokens": 14},
            )
        ]
    )
    agent = create_agent(
        model=model,
        tools=[],
        system_prompt=SYSTEM_MENTOR_PROMPT,
        middleware=[make_context_middleware()],
        checkpointer=memory_mod.get_checkpointer(),
    )
    session = ChatSession(thread_id="conv-usage", agent=agent)
    session.run_turn("hello")

    assert session.last_usage == TokenUsage(11, 3, 14)
    memory_mod.close_checkpointer()


def test_stream_turn_reports_usage(tmp_path, monkeypatch):
    directory = tmp_path / "system-sl"
    directory.mkdir()
    monkeypatch.setattr(
        memory_mod, "get_tasks_file_path", lambda name: str(directory / name)
    )
    memory_mod.close_checkpointer()

    from langchain.agents import create_agent

    from system_sl.chatbot.agent_middleware import make_context_middleware
    from system_sl.chatbot.system_prompts import SYSTEM_MENTOR_PROMPT

    model = FakeChatModel(
        replies=[
            AIMessage(
                content="streamed answer",
                usage_metadata={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
            )
        ]
    )
    agent = create_agent(
        model=model,
        tools=[],
        system_prompt=SYSTEM_MENTOR_PROMPT,
        middleware=[make_context_middleware()],
        checkpointer=memory_mod.get_checkpointer(),
    )
    session = ChatSession(thread_id="conv-usage-stream", agent=agent)

    kinds = []
    for kind, value in session.stream_turn("hello"):
        kinds.append((kind, value))

    usage = [value for kind, value in kinds if kind == "usage"]
    assert usage and usage[-1]["input_tokens"] == 7 and usage[-1]["output_tokens"] == 2
    # The context fill is the latest call's prompt plus what it wrote.
    assert usage[-1]["context_tokens"] == 9
    assert session.last_usage == TokenUsage(7, 2, 9)
    assert session.last_context_tokens == 9
    memory_mod.close_checkpointer()

def test_format_context_shows_fill_of_the_window():
    from system_sl.chatbot.usage import format_context

    assert format_context(12_400, 128_000) == "Context 12.4k / 128k (10%)"
    assert format_context(900, 32_000, estimated=True) == "Context ~900 / 32k (3%)"
    assert format_context(0, 128_000) == ""
