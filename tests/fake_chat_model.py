"""A chat model that answers from a script, for tests with no network.

Kept out of ``test_*.py`` so pytest does not try to collect it. It supports
tool binding (``create_agent`` binds tools on every call) and streaming, and
it records the messages it was given so a test can assert what the model
actually saw.
"""

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
        for token in str(reply.content).split(" "):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=token + " "))
            if run_manager:
                run_manager.on_llm_new_token(token + " ")
            yield chunk
