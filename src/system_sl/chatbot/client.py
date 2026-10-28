"""Chat session management for System-SL."""

import json
from typing import Callable, Iterator, List, Optional, Union

from langchain_core.messages import AIMessageChunk

from system_sl.chatbot.system import (
    get_system_agent,
    reset_agent_cache,
    set_turn_context,
    clear_turn_context,
)
from system_sl.chatbot.config import ChatConfig, load_config, save_config

from langsmith import uuid7


def _content_text(content) -> str:
    """Extract plain text from a message chunk's ``content`` payload."""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                # Anthropic-style thinking blocks are not answer text
                if block.get("type") in ("thinking", "reasoning"):
                    continue
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    return ""


def _reasoning_text(chunk: AIMessageChunk) -> str:
    """Extract reasoning/thinking text from a message chunk, if any.

    Providers disagree on where this lives: OpenAI-compatible servers
    (LM Studio, vLLM, DeepSeek) use ``additional_kwargs["reasoning_content"]``,
    some use ``reasoning``, and others send a ``thinking`` content block.
    """
    additional = getattr(chunk, "additional_kwargs", None) or {}
    for key in ("reasoning_content", "reasoning"):
        value = additional.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list):
            joined = "".join(
                part.get("text", "") for part in value if isinstance(part, dict)
            )
            if joined:
                return joined

    content = getattr(chunk, "content", None)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("thinking", "reasoning"):
                parts.append(block.get("text") or block.get("thinking") or "")
        return "".join(parts)

    return ""


def _user_text(user_msg: Union[str, List[str]]) -> str:
    """Flatten a turn's input into the text the task gate inspects."""
    if isinstance(user_msg, str):
        return user_msg
    return "\n".join(str(part) for part in user_msg)


class ChatSession:
    """Manages an active multi-turn conversation session with the System agent.

    Attributes:
        config (dict): The runtime configuration dictionary containing the `thread_id`
            for state tracking.
        agent (Runnable): The compiled LangGraph agent graph.
        model_name (str): The model name being used (for display purposes).
    """

    def __init__(
        self,
        thread_id: str | None = None,
        config: dict | None = None,
        model_name: str | None = None,
    ):
        """Initializes a new or existing ChatSession.

        Args:
            thread_id (str | None, optional): An existing unique identifier to resume a past conversation.
                If both thread_id and config are None, a new UUIDv7 is generated. Defaults to None.
            config (dict | None, optional): A complete pre-built LangChain/LangGraph
                configuration dictionary. Overrides thread_id if provided. Defaults to None.
            model_name (str | None, optional): Display name of the model being used.
        """
        if config:
            self.config = config
        else:
            chosen_id = thread_id or str(uuid7())
            self.config = {"configurable": {"thread_id": chosen_id}}

        # Select the pre-compiled agent (built on first use via lazy init)
        self.agent = get_system_agent()
        self.model_name = model_name or "system"

    def wrap_message(self, user_msg: Union[str, List[str]]) -> List[dict]:
        """Formats a raw string or list of strings into the standard message format.

        Args:
            user_msg (Union[str, List[str]]): A single text message string or a list
                of text message strings sent by the user.

        Returns:
            List[dict]: A list of formatted message dictionaries containing the 'role'
                and 'content' keys ready for LangGraph input.
        """
        if isinstance(user_msg, str):
            return [{"role": "user", "content": user_msg}]
        else:
            return [{"role": "user", "content": msg} for msg in user_msg]

    def run_turn(
        self,
        user_msg: Union[str, List[str]],
        on_proposal: Optional[Callable[[dict], None]] = None,
    ) -> str:
        """Executes a single conversational turn with the agent.

        Args:
            user_msg (Union[str, List[str]]): The input text message(s) from the user.
            on_proposal (Callable[[dict], None], optional): Receives tasks the
                agent tried to add without an explicit request.

        Returns:
            str: The extracted plain text content of the agent's reply.
        """
        formatted_messages = self.wrap_message(user_msg)

        set_turn_context(_user_text(user_msg), on_proposal)
        try:
            response = self.agent.invoke(
                {"messages": formatted_messages},
                config=self.config
            )
        finally:
            clear_turn_context()

        if response is not None and "messages" in response:
            last_message = response["messages"][-1]

            # Extract content property from the base message class instance
            if hasattr(last_message, 'content'):
                content = last_message.content

                # CASE 1: Content is a rich list: [{'type': 'text', 'text': '...'}]
                if isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    return "".join(text_parts)  # Merges all text blocks into one clean string

                # CASE 2: Content is already a standard direct string
                if isinstance(content, str):
                    return content

            # CASE 3: Fallback check if the message structure parsed as a raw dictionary primitive
            elif isinstance(last_message, dict) and 'content' in last_message:
                content = last_message['content']
                if isinstance(content, list) and len(content) > 0:
                    return str(content[0].get('text', ''))
                return str(content)

        return "[The System failed to reply]"

    def stream_turn(
        self,
        user_msg: Union[str, List[str]],
        on_proposal: Optional[Callable[[dict], None]] = None,
    ) -> Iterator[tuple[str, str]]:
        """Executes a single turn, yielding text as it is produced.

        Yields:
            ``("reasoning", delta)`` for reasoning/thinking text and
            ``("content", delta)`` for answer text. Models that do not expose
            reasoning simply never yield the former.

        ``on_proposal`` receives any task the agent tried to add without the
        Player explicitly asking for it, so the UI can confirm it.
        """
        formatted_messages = self.wrap_message(user_msg)

        set_turn_context(_user_text(user_msg), on_proposal)
        try:
            for chunk, metadata in self.agent.stream(
                {"messages": formatted_messages},
                config=self.config,
                stream_mode="messages",
            ):
                # Tool results and other graph chatter are not model output.
                if not metadata or metadata.get("langgraph_node") != "model":
                    continue
                if not isinstance(chunk, AIMessageChunk):
                    continue

                reasoning = _reasoning_text(chunk)
                if reasoning:
                    yield "reasoning", reasoning
                    # A chunk can carry reasoning and answer text at once.
                    # Fall through only when there is content as well.

                text = _content_text(chunk.content)
                if text:
                    yield "content", text
        finally:
            clear_turn_context()

    @classmethod
    def create_with_model(cls, model_name: str, thread_id: str | None = None) -> "ChatSession":
        """Create a session with a specific model by temporarily overriding config.

        Note: This creates a new agent with the specified model. The model change
        persists for the session lifetime.
        """
        # We'll handle model switching at the ChatPanel level by recreating sessions
        return cls(thread_id=thread_id, model_name=model_name)


def reload_agent() -> None:
    """Reload the agent with current config. Call after config changes."""
    reset_agent_cache()


if __name__ == "__main__":
    session = ChatSession()
    print(session.run_turn("Hello"))