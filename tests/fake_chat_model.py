"""A chat model that answers from a script, for tests with no network.

Kept out of ``test_*.py`` so pytest does not try to collect it. It supports
tool binding (``create_agent`` binds tools on every call) and streaming, and
it records the messages it was given so a test can assert what the model
actually saw.
"""

import json
from typing import Any, Iterator, List, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


# A test whose script keeps asking for tools would loop until LangGraph's
# recursion limit (or forever, if a subclass resets it) and grow the
# checkpoint database without bound. Fail fast instead.
MAX_CALLS = 50


class FakeChatModel(BaseChatModel):
    """Return ``replies`` in order (the last one repeats)."""

    replies: List[AIMessage] = []
    seen: List[List[BaseMessage]] = []
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeChatModel":
        # The agent binds tools on every model call; the fake ignores them.
        return self

    def _next_reply(self, messages: List[BaseMessage]) -> AIMessage:
        self.seen.append(list(messages))
        index = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        return self.replies[index]

    def _guard(self) -> None:
        if len(self.seen) > MAX_CALLS:
            raise RuntimeError(f"FakeChatModel called more than {MAX_CALLS} times; the test is looping")

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        reply = self._next_reply(messages)
        self._guard()
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        reply = self._next_reply(messages)
        self._guard()
        chunks = [
            ChatGenerationChunk(message=AIMessageChunk(content=token + " "))
            for token in str(reply.content).split(" ")
        ]
        # Real providers attach usage to the final chunk (or to the message
        # when there is no text); mirror that so callers can be tested.
        if chunks and reply.usage_metadata:
            chunks[-1] = ChatGenerationChunk(
                message=AIMessageChunk(
                    content=chunks[-1].message.content,
                    usage_metadata=reply.usage_metadata,
                )
            )
        for chunk in chunks:
            if run_manager:
                run_manager.on_llm_new_token(str(chunk.message.content))
            yield chunk
        # Tool calls stream as chunks too, like a real provider's.
        if reply.tool_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": call["name"],
                            "args": json.dumps(call.get("args") or {}),
                            "id": call.get("id"),
                            "index": index,
                        }
                        for index, call in enumerate(reply.tool_calls)
                    ],
                )
            )
