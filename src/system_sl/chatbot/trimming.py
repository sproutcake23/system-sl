"""Trim the history that is sent to the model.

The conversation is stored in full — that is what the transcript renders and
what survives a restart. Only a slice of it fits in the model's context
window, so the slice is chosen here, on the way to the model, and never
written back to the checkpoint.

Two rules matter more than the budget:

* an AI message that calls a tool is never separated from the tool result
  that answers it — most providers reject such a history outright;
* the history never starts with a tool result, because there would be no
  call for it to belong to.

Both are guaranteed by trimming whole *blocks* instead of whole messages. A
block is one Player message together with everything the System did in
response to it (its answer, its tool calls, their results). A block is
therefore self-contained.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

logger = logging.getLogger(__name__)


# How much of a dropped turn survives in the summary.
SUMMARY_CHARS = 140

# The summary header, so the model knows what it is reading.
SUMMARY_HEADER = "[Earlier conversation — older turns were dropped to fit the context window]"


def _message_text(message: Any) -> str:
    """Return the plain text of a message, whatever shape its content has."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def group_into_blocks(messages: Sequence[BaseMessage]) -> list[list[BaseMessage]]:
    """Split the history into self-contained blocks.

    A block starts at a Player message and runs until the next one. Tool
    calls and their results always live inside the same block, so a block can
    be dropped or kept as a whole.
    """
    blocks: list[list[BaseMessage]] = []
    current: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, HumanMessage) and current:
            blocks.append(current)
            current = []
        current.append(message)
    if current:
        blocks.append(current)
    return blocks


def _block_text(block: Iterable[BaseMessage]) -> str:
    return "\n".join(_message_text(message) for message in block)


def _shorten(text: str, limit: int = SUMMARY_CHARS) -> str:
    """Collapse a piece of text into a single short line."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def summarize_blocks(blocks: Sequence[Sequence[BaseMessage]]) -> str:
    """Summarize the dropped turns into a few lines.

    The summary is rebuilt from the full history on every model call instead
    of being stored in the checkpoint. A stored summary would have to be
    rewritten (and its history rewritten again) every time a turn is
    dropped, and it would grow the checkpoint for no benefit: the trimmed
    history is recomputed from the same source every time anyway.
    """
    lines: list[str] = []
    for block in blocks:
        player = next((m for m in block if isinstance(m, HumanMessage)), None)
        system = next(
            (m for m in block if isinstance(m, AIMessage) and _message_text(m).strip()),
            None,
        )
        parts = []
        if player is not None:
            parts.append(f"Player: {_shorten(_message_text(player))}")
        if system is not None:
            parts.append(f"System: {_shorten(_message_text(system))}")
        if parts:
            lines.append("- " + " | ".join(parts))
    if not lines:
        return ""
    return SUMMARY_HEADER + "\n" + "\n".join(lines)


def trim_history(
    messages: Sequence[BaseMessage],
    budget_tokens: int,
) -> tuple[list[BaseMessage], str]:
    """Return the history to send to the model, plus a summary of the rest.

    The most recent blocks are kept until the budget is spent. The newest
    block is always kept, even when it alone busts the budget: dropping the
    turn the Player just sent would break the conversation, and a block is
    never cut in half because that would separate a tool call from its
    result.
    """
    messages = list(messages)
    if not messages:
        return [], ""

    from system_sl.chatbot.context_window import count_message_tokens

    blocks = group_into_blocks(messages)
    kept: list[list[BaseMessage]] = []
    used = 0

    for block in reversed(blocks):
        cost = count_message_tokens(block)
        if kept and used + cost > budget_tokens:
            break
        kept.insert(0, block)
        used += cost

    if not kept:  # pragma: no cover - the loop always keeps the newest block
        kept = [blocks[-1]]

    dropped_count = len(blocks) - len(kept)
    if dropped_count:
        logger.info(
            "trimmed %d of %d turns from the history sent to the model",
            dropped_count,
            len(blocks),
        )

    kept_messages = [message for block in kept for message in block]
    summary = summarize_blocks(blocks[:dropped_count]) if dropped_count else ""
    return kept_messages, summary
