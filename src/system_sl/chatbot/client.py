"""Chat session management for System-SL."""

import logging
from typing import Callable, Iterable, Iterator, List, Optional, Union

from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

from system_sl.chatbot.system import (
    get_system_agent,
    get_system_agent_for_model,
    message_has_documents,
    player_typed_text,
    reset_agent_cache,
    set_turn_context,
    clear_turn_context,
)
from system_sl.chatbot.memory import (
    APP_NOTE_PREFIX,
    load_thread_messages,
    repair_interrupted_turn,
    rewind_before,
)
from system_sl.chatbot.usage import TokenUsage, usage_from_message, merge_usage

from langsmith import uuid7

logger = logging.getLogger(__name__)


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


def _gate_text(message: str, typed_text: Optional[str]) -> str:
    """The text the add_task gate may treat as the Player's own request.

    Only what the Player typed counts. Quoted messages and attached documents
    are someone else's words: a document saying "create a task to ..." must
    not add a task without asking. With a document attached nothing counts as
    an explicit request (see ``message_has_documents``). Callers that know the
    typed text pass it; otherwise it is recovered from the composed message,
    failing closed.
    """
    if message_has_documents(message):
        return ""
    if typed_text is not None:
        return typed_text
    return player_typed_text(message)


def _user_text(user_msg: Union[str, List[str]]) -> str:
    """Flatten a turn's input into the text the task gate inspects."""
    if isinstance(user_msg, str):
        return user_msg
    return "\n".join(str(part) for part in user_msg)


class ChatSession:
    """Manages an active multi-turn conversation session with the System agent.

    The conversation is identified by ``thread_id`` and stored by the shared
    LangGraph checkpointer, so it survives model switches, Stop presses and
    application restarts. Several ``ChatSession`` objects can point at the same
    thread (one per model) without losing the history.

    Attributes:
        config (dict): The runtime configuration dictionary containing the `thread_id`
            for state tracking.
        thread_id (str): The conversation id shared with the checkpoint store.
        agent (Runnable): The compiled LangGraph agent graph.
        llm: The chat model behind ``agent``, used to cancel a running stream.
        model_name (str): The model name being used (for display purposes).
    """

    def __init__(
        self,
        thread_id: str | None = None,
        config: dict | None = None,
        model_name: str | None = None,
        agent=None,
        llm=None,
    ):
        """Initializes a new or existing ChatSession.

        Args:
            thread_id (str | None, optional): An existing unique identifier to resume a past conversation.
                If both thread_id and config are None, a new UUIDv7 is generated. Defaults to None.
            config (dict | None, optional): A complete pre-built LangChain/LangGraph
                configuration dictionary. Overrides thread_id if provided. Defaults to None.
            model_name (str | None, optional): Display name of the model being used.
            agent (optional): A pre-built agent for a model chosen for this
                session only. Defaults to the shared, config-backed agent.
            llm (optional): The chat model behind ``agent``, if the caller built it.
        """
        if config:
            self.config = config
        else:
            chosen_id = thread_id or str(uuid7())
            self.config = {"configurable": {"thread_id": chosen_id}}

        self.thread_id = str(
            (self.config.get("configurable") or {}).get("thread_id") or ""
        )

        # Select the pre-compiled agent (built on first use via lazy init)
        self.agent = agent or get_system_agent()
        self.llm = llm
        self.model_name = model_name or "system"
        # Token usage of the most recent turn. Providers report it on the
        # final AI message (or the last streamed chunk), not on the request.
        self.last_usage = TokenUsage()

    def switch_model(self, model_name: str, agent=None, llm=None) -> None:
        """Continue this conversation with a different model.

        The thread id (and therefore the checkpoint) is untouched: the new
        model answers from the same message history the old one built.
        """
        if model_name == self.model_name and agent is None:
            return
        self.agent = agent or get_system_agent_for_model(model_name)
        self.llm = llm
        self.model_name = model_name

    def messages(self) -> list:
        """Return this conversation's stored messages (oldest first)."""
        return load_thread_messages(self.thread_id)

    def compact_attachment_references(self, keep_chars: int = 400) -> int:
        """Replace the full text of attachments in the stored history.

        An attachment's text is only useful for the turn that used it. Left
        in the history, it is paid for again in every later turn (and it
        grows the checkpoint on disk), so the body is replaced by a short
        reference: the file name, the first lines and how much was there.

        The message is replaced in place — same id, same position — so the
        conversation keeps its order.

        Returns the number of messages that were compacted.
        """
        from system_sl.chatbot.documents import compact_document_blocks

        updates = []
        for message in self.messages():
            if getattr(message, "type", "") != "human":
                continue
            content = getattr(message, "content", None)
            if not isinstance(content, str) or "<document" not in content:
                continue
            compacted = compact_document_blocks(content, keep_chars=keep_chars)
            if compacted != content:
                updates.append(message.model_copy(update={"content": compacted}))

        if not updates:
            return 0

        try:
            self.agent.update_state(self.config, {"messages": updates})
        except Exception:
            logger.warning(
                "could not compact the attachments of conversation %s",
                self.thread_id,
                exc_info=True,
            )
            return 0
        return len(updates)

    def _prepare_turn(self) -> None:
        """Make the checkpoint safe to continue from before a new turn.

        A turn abandoned mid-graph (Stop, crash, provider error) leaves
        LangGraph pending tasks behind. Repairing them here, on the thread that
        owns the graph, avoids running a cancelled tool call or failing the
        next turn outright. If the repair cannot be written the turn is
        abandoned: continuing would resume the very tool the Player cancelled.
        """
        try:
            repaired = repair_interrupted_turn(self.agent, self.config)
        except Exception as exc:
            logger.error(
                "could not repair the interrupted turn in conversation %s",
                self.thread_id,
                exc_info=True,
            )
            raise RuntimeError(
                "the previous reply did not finish cleanly and could not be closed"
            ) from exc
        if repaired:
            logger.info("conversation %s repaired before the next turn", self.thread_id)

    def wrap_message(
        self, user_msg: Union[str, List[str]], message_id: Optional[str] = None
    ) -> List[HumanMessage]:
        """Turn the Player's text into the messages LangGraph receives.

        ``message_id`` becomes the stored message's id, so the chat panel can
        later rewind the conversation to just before it (see
        ``rewind_before``). Without one, LangGraph assigns an id itself.
        """
        if isinstance(user_msg, str):
            return [HumanMessage(content=user_msg, id=message_id)]
        return [HumanMessage(content=msg) for msg in user_msg]

    def rewind_before(self, message_id: str) -> int:
        """Remove a stored Player message and everything after it.

        Finishes an interrupted turn first, so the rewind starts from a valid
        checkpoint. Raises ``LookupError`` when the message was never stored.
        """
        self._prepare_turn()
        return rewind_before(self.agent, self.config, message_id)

    def run_turn(
        self,
        user_msg: Union[str, List[str]],
        on_proposal: Optional[Callable[[dict], None]] = None,
        typed_text: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> str:
        """Executes a single conversational turn with the agent.

        Args:
            user_msg (Union[str, List[str]]): The input text message(s) from the user.
            on_proposal (Callable[[dict], None], optional): Receives tasks the
                agent tried to add without an explicit request.

        Returns:
            str: The extracted plain text content of the agent's reply.
        """
        augmented = _user_text(user_msg)
        formatted_messages = self.wrap_message(augmented, message_id)
        self.last_usage = TokenUsage()
        self.last_context_tokens = 0

        self._prepare_turn()
        set_turn_context(_gate_text(augmented, typed_text), on_proposal)
        try:
            response = self.agent.invoke(
                {"messages": formatted_messages},
                config=self.config
            )
        finally:
            clear_turn_context()

        if response is not None and "messages" in response:
            last_message = response["messages"][-1]
            reported = usage_from_message(last_message)
            if reported is not None:
                self.last_usage = reported

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
        on_tool: Optional[Callable[[str, str, bool], None]] = None,
        typed_text: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> Iterator[tuple[str, object]]:
        """Executes a single turn, yielding text as it is produced.

        Yields:
            ``("reasoning", delta)`` for reasoning/thinking text,
            ``("content", delta)`` for answer text, and ``("usage", dict)``
            once at the end when the provider reported token counts. Models
            that do not expose reasoning simply never yield the former.

        ``on_proposal`` receives any task the agent tried to add without the
        Player explicitly asking for it, so the UI can confirm it.

        ``on_tool`` receives (tool_name, status, detail) where status is
        "start" or "end", for tool activity display.
        """
        augmented = _user_text(user_msg)
        formatted_messages = self.wrap_message(augmented, message_id)
        self.last_usage = TokenUsage()
        self.last_context_tokens = 0

        self._prepare_turn()
        set_turn_context(_gate_text(augmented, typed_text), on_proposal)
        reported: Optional[TokenUsage] = None
        # Size of the latest model call (its prompt plus what it wrote): how
        # full the context window is after this turn. A chunk that reports
        # input tokens starts a call (Anthropic's first chunk, Gemini's first
        # delta, OpenAI's single usage chunk); output after it adds up.
        last_prompt = 0
        last_output = 0
        try:
            for chunk, metadata in self.agent.stream(
                {"messages": formatted_messages},
                config=self.config,
                stream_mode="messages",
            ):
                if not metadata:
                    continue
                node = metadata.get("langgraph_node", "")
                
                # Tool activity. In "messages" mode the tools node only
                # streams its results (ToolMessage), i.e. after the tool has
                # run, so "start" comes from the model's tool-call chunks and
                # "end" from the result.
                if node == "tools":
                    if on_tool and isinstance(chunk, ToolMessage):
                        on_tool(chunk.name or "tool", "end", "")
                    continue
                
                # Model output
                if node != "model":
                    continue
                if not isinstance(chunk, AIMessageChunk):
                    continue

                chunk_usage = usage_from_message(chunk)
                reported = merge_usage(reported, chunk_usage)
                if chunk_usage is not None:
                    if chunk_usage.input_tokens:
                        last_prompt = chunk_usage.input_tokens
                        last_output = chunk_usage.output_tokens
                    else:
                        last_output += chunk_usage.output_tokens

                if on_tool:
                    for call in chunk.tool_call_chunks or []:
                        # The name arrives once, on the call's first chunk.
                        if call.get("name"):
                            on_tool(call["name"], "start", "")

                reasoning = _reasoning_text(chunk)
                if reasoning:
                    yield "reasoning", reasoning
                    # A chunk can carry reasoning and answer text at once.
                    # Fall through only when there is content as well.

                text = _content_text(chunk.content)
                if text:
                    yield "content", text

            if reported is not None:
                self.last_usage = reported
                usage = reported.as_dict()
                if last_prompt:
                    self.last_context_tokens = last_prompt + last_output
                    usage["context_tokens"] = self.last_context_tokens
                yield "usage", usage
        finally:
            clear_turn_context()

    def inject_confirmation_result(
        self,
        task_title: str,
        category: str,
        confirmed: bool,
    ) -> bool:
        """Record a single task confirmation/decline in the conversation.

        Thin wrapper around :meth:`record_task_confirmation` for the one-task
        cases; batch dialogs pass every task at once so the model reads one
        summarizing message instead of a burst of them.
        """
        if confirmed:
            return self.record_task_confirmation(added=[(task_title, category)])
        return self.record_task_confirmation(declined=[(task_title, category)])

    def record_task_confirmation(
        self,
        added: Iterable[tuple[str, str]] = (),
        declined: Iterable[tuple[str, str]] = (),
        failed: Iterable[tuple[str, str]] = (),
    ) -> bool:
        """Append one note telling the model what happened to proposed tasks.

        This feeds the confirmation result back so the model knows whether a
        task it proposed was actually created, and does not claim otherwise
        or propose it again. The note is written with ``update_state`` — the
        supported LangGraph API — which applies the ``messages`` reducer the
        same way a turn does, leaving the checkpoint consistent.

        Returns True when the note was stored.
        """
        added = [(t, c) for t, c in added if t]
        declined = [(t, c) for t, c in declined if t]
        failed = [(t, c) for t, c in failed if t]
        if not (added or declined or failed):
            return False

        parts: list[str] = []
        if added:
            parts.append(
                "Player confirmed; added: "
                + ", ".join(f'"{t}" ({c})' for t, c in added)
            )
        if declined:
            parts.append(
                "Player declined; not added: "
                + ", ".join(f'"{t}" ({c})' for t, c in declined)
            )
        if failed:
            parts.append(
                "Could not be added: " + ", ".join(f'"{t}" ({c})' for t, c in failed)
            )
        text = f"{APP_NOTE_PREFIX} {'; '.join(parts)}."

        note = HumanMessage(content=text)
        try:
            # Written as the model's step so the thread is not left paused:
            # with the writer inferred, a note recorded after a repaired
            # (stopped) turn leaves the graph waiting at "model".
            self.agent.update_state(self.config, {"messages": [note]}, as_node="model")
        except Exception:
            logger.warning(
                "could not record a task confirmation in conversation %s",
                self.thread_id,
                exc_info=True,
            )
            return False
        return True


def reload_agent() -> None:
    """Reload the agent with current config. Call after config changes."""
    reset_agent_cache()
