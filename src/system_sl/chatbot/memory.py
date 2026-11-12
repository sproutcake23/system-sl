"""Persistent conversation memory for the System agent.

One SQLite-backed LangGraph checkpointer stores every message of every
conversation. It is created once per process so that rebuilding the agent
(model switch, Stop, settings change) never loses the history. Message history
is kept in LangGraph state (``messages``), not replayed by hand, so a trimming
middleware can later be added in front of it.

The database lives next to the other user data (see ``utils.paths``):
``~/.config/system-sl/chat_memory.sqlite`` on Linux,
``%APPDATA%\\system-sl\\chat_memory.sqlite`` on Windows.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage

from system_sl.utils.paths import get_tasks_file_path

logger = logging.getLogger(__name__)

CHECKPOINT_FILENAME = "chat_memory.sqlite"

# Prefix for messages the app itself injects into the history (stopped turns).
# The chat panel uses it to render them as notes instead of Player bubbles.
STOP_NOTE_PREFIX = "[turn stopped]"
NOTE_PREFIXES = (STOP_NOTE_PREFIX,)

# What an interrupted turn records instead of a tool result.
STOP_NOTE = (
    STOP_NOTE_PREFIX
    + " The Player stopped this reply before the tool ran, so nothing was changed."
)


def is_app_note(text: str) -> bool:
    """Return True when a stored message is a note the app wrote itself."""
    return (text or "").startswith(NOTE_PREFIXES)


def note_body(text: str) -> str:
    """Return a stored note's text without its prefix."""
    for prefix in NOTE_PREFIXES:
        if (text or "").startswith(prefix):
            return text[len(prefix) :].strip()
    return (text or "").strip()


# =============================================================================
# CHECKPOINTER
# =============================================================================

_checkpointer = None
_checkpointer_lock = threading.Lock()


def get_checkpointer():
    """Return the process-wide SQLite checkpointer, creating it on first use.

    Creation is deliberately *not* tied to the agent: ``reset_agent_cache()``
    rebuilds the agent but must keep the same saver, otherwise every Stop or
    model switch would orphan the conversation on disk. SqliteSaver takes a
    lock around its connection, so the worker thread and the GUI thread can
    share it (``check_same_thread=False``).
    """
    global _checkpointer
    with _checkpointer_lock:
        if _checkpointer is None:
            from langgraph.checkpoint.sqlite import SqliteSaver

            path = get_tasks_file_path(CHECKPOINT_FILENAME)
            conn = sqlite3.connect(path, check_same_thread=False)
            saver = SqliteSaver(conn)
            saver.setup()
            _checkpointer = saver
            logger.debug("conversation memory opened at %s", path)
        return _checkpointer


def close_checkpointer() -> None:
    """Close and forget the shared checkpointer.

    The next ``get_checkpointer()`` call reopens the same file, so this only
    drops the live connection (tests, and an explicit app shutdown).
    """
    global _checkpointer
    with _checkpointer_lock:
        saver = _checkpointer
        _checkpointer = None
        if saver is not None:
            try:
                saver.conn.close()
            except Exception:  # pragma: no cover - closing must never raise
                logger.debug("could not close the conversation memory", exc_info=True)


def load_thread_messages(thread_id: str) -> list[BaseMessage]:
    """Return the messages stored for ``thread_id`` (empty when unknown).

    Reads the checkpoint directly instead of going through an agent, so the
    GUI can rebuild a transcript even when no LLM is configured yet.
    """
    if not thread_id:
        return []
    config = {"configurable": {"thread_id": thread_id}}
    try:
        checkpoint = get_checkpointer().get_tuple(config)
    except Exception:
        logger.warning("could not read conversation %s", thread_id, exc_info=True)
        return []
    if checkpoint is None:
        return []
    values = checkpoint.checkpoint.get("channel_values") or {}
    return list(values.get("messages") or [])


# =============================================================================
# INTERRUPTED TURNS
# =============================================================================


def repair_interrupted_turn(agent, config: dict) -> bool:
    """Close a turn that was cut off mid-graph, leaving the checkpoint valid.

    LangGraph checkpoints each node's result. If a turn was abandoned after the
    model asked for a tool but before the tool node ran (Stop, crash, network
    error), the thread is left with pending tasks: the next invoke either runs
    the cancelled tool for real or fails with ``KeyError: 'model'``. Both are
    wrong, so the interrupted tool call is rolled back (the model's words stay,
    the call is dropped) and a note tells the model nothing was changed.

    Returns True when something was repaired. Raises only if the repair itself
    cannot be written; callers treat that as a failed turn.
    """
    state = agent.get_state(config)
    pending = tuple(state.next or ())
    if not pending:
        return False

    if "tools" in pending:
        messages = list(state.values.get("messages") or [])
        rollback = _rollback_messages(messages)
        if rollback is None:
            logger.warning(
                "conversation %s is paused before the tools node with no tool call",
                _thread_id(config),
            )
            return False
        agent.update_state(config, {"messages": rollback})
        logger.info(
            "rolled back an interrupted tool call in conversation %s",
            _thread_id(config),
        )
    else:
        # The model step died before writing anything. Mark it done with no
        # output so the thread can accept the next message.
        agent.update_state(config, {}, as_node="model")
        logger.info(
            "cleared an interrupted model step in conversation %s",
            _thread_id(config),
        )
    return True


def _rollback_messages(messages: Sequence[BaseMessage]) -> list[BaseMessage] | None:
    """Describe how to undo an interrupted tool call, or None if there is none.

    Nothing executed yet: the model's text is kept and only the dangling call is
    dropped (the reducer replaces a message with the same id). If some tool
    results were already written, the whole tail is removed instead, so no
    orphan tool message survives. Either way the last message becomes the
    stop note, which sends the graph to END.
    """
    last_ai = next(
        (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], AIMessage)),
        None,
    )
    if last_ai is None or not messages[last_ai].tool_calls:
        return None

    trailing = list(messages[last_ai + 1 :])
    if not trailing:
        patched = messages[last_ai].model_copy(
            update={"tool_calls": [], "invalid_tool_calls": []}
        )
        return [patched, HumanMessage(content=STOP_NOTE)]

    removals = [
        RemoveMessage(id=message.id)
        for message in messages[last_ai:]
        if getattr(message, "id", None)
    ]
    return [*removals, HumanMessage(content=STOP_NOTE)]


def _thread_id(config: dict) -> str:
    return str((config.get("configurable") or {}).get("thread_id") or "?")
