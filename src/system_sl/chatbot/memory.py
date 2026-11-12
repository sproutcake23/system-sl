"""Persistent conversation memory for the System agent.

Two things live here:

* **The checkpointer** — one SQLite-backed LangGraph checkpointer that stores
  every message of every conversation. It is created once per process so that
  rebuilding the agent (model switch, Stop, settings change) never loses the
  history. Message history is kept in LangGraph state (``messages``), not
  replayed by hand, so a trimming middleware can later be added in front of it.
* **The conversation index** — a small JSON file listing conversations
  (id, title, timestamps, last model) plus which one was last open, so the
  chat panel can offer history and restore it at start-up.

The database lives next to the other user data (see ``utils.paths``):
``~/.config/system-sl/chat_memory.sqlite`` on Linux,
``%APPDATA%\\system-sl\\chat_memory.sqlite`` on Windows.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage
from langsmith import uuid7

from system_sl.utils.atomic_json import atomic_write_json, load_json_or_default
from system_sl.utils.paths import get_tasks_file_path

logger = logging.getLogger(__name__)

CHECKPOINT_FILENAME = "chat_memory.sqlite"
CONVERSATION_INDEX_FILENAME = "chat_conversations.json"

# Prefix for messages the app itself injects into the history (task
# confirmations, stopped turns). The chat panel uses it to render them as
# notes instead of Player bubbles when rebuilding a transcript.
APP_NOTE_PREFIX = "[task confirmation]"
STOP_NOTE_PREFIX = "[turn stopped]"
NOTE_PREFIXES = (APP_NOTE_PREFIX, STOP_NOTE_PREFIX)

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


_INDEX_VERSION = 1
_TITLE_LIMIT = 60

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


# =============================================================================
# CONVERSATION INDEX
# =============================================================================


def _index_path() -> str:
    return get_tasks_file_path(CONVERSATION_INDEX_FILENAME)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def load_conversation_index() -> dict:
    """Return the conversation index, tolerating a missing/corrupt file."""
    data = load_json_or_default(_index_path(), {})
    if not isinstance(data, dict):
        return {"version": _INDEX_VERSION, "active": None, "conversations": []}

    conversations = [
        entry
        for entry in data.get("conversations") or []
        if isinstance(entry, dict) and entry.get("id")
    ]
    active = data.get("active")
    if active and not any(entry["id"] == active for entry in conversations):
        active = None
    return {
        "version": _INDEX_VERSION,
        "active": active,
        "conversations": conversations,
    }


def save_conversation_index(index: dict) -> None:
    """Persist the conversation index atomically."""
    try:
        atomic_write_json(_index_path(), index)
    except OSError:
        logger.warning("could not save the conversation index", exc_info=True)


def new_thread_id() -> str:
    """Return a fresh, time-ordered conversation id."""
    return str(uuid7())


def title_from_message(text: str, limit: int = _TITLE_LIMIT) -> str:
    """Make a conversation title from the first Player message.

    Deterministic on purpose: the title must appear immediately and must never
    wait on (or fail because of) a network call.
    """
    flat = " ".join((text or "").split())
    # Old turns were stored with a date prefix; it is not part of the title.
    if flat.startswith("[Today's date:"):
        closing = flat.find("]")
        if closing != -1:
            flat = flat[closing + 1 :].strip()
    if not flat:
        return ""
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0] or flat[:limit]
    return cut.rstrip(" ,.;:-") + "…"


def list_conversations() -> list[dict]:
    """Return every conversation, most recently updated first.

    Timestamps only have microsecond resolution, so two conversations created
    in the same tick would otherwise sort oldest-first (stable sort on insertion
    order). Reversing first makes "newest wins" true even for a tie.
    """
    conversations = load_conversation_index()["conversations"]
    return sorted(
        reversed(conversations),
        key=lambda entry: entry.get("updated") or entry.get("created") or "",
        reverse=True,
    )


def get_conversation(thread_id: str) -> dict | None:
    """Return one conversation's index entry, or None."""
    for entry in load_conversation_index()["conversations"]:
        if entry["id"] == thread_id:
            return entry
    return None


def active_conversation_id() -> str | None:
    """Return the conversation that was open when the app last closed."""
    return load_conversation_index()["active"]


def create_conversation(model_name: str = "", title: str = "") -> dict:
    """Create a conversation, make it active, and return its entry."""
    index = load_conversation_index()
    entry = {
        "id": new_thread_id(),
        "title": title or "",
        "created": _now(),
        "updated": _now(),
        "last_model": model_name or "",
    }
    index["conversations"].append(entry)
    index["active"] = entry["id"]
    save_conversation_index(index)
    return entry


def select_conversation(thread_id: str) -> dict | None:
    """Mark ``thread_id`` as the active conversation."""
    index = load_conversation_index()
    entry = next((e for e in index["conversations"] if e["id"] == thread_id), None)
    if entry is None:
        return None
    if index["active"] != thread_id:
        index["active"] = thread_id
        save_conversation_index(index)
    return entry


def update_conversation(
    thread_id: str,
    *,
    title: str | None = None,
    title_if_empty: str | None = None,
    last_model: str | None = None,
    touch: bool = True,
) -> dict | None:
    """Update a conversation's title/model and bump its ``updated`` stamp."""
    index = load_conversation_index()
    entry = next((e for e in index["conversations"] if e["id"] == thread_id), None)
    if entry is None:
        return None

    changed = False
    if title is not None and entry.get("title") != title:
        entry["title"] = title
        changed = True
    if title_if_empty and not entry.get("title"):
        entry["title"] = title_if_empty
        changed = True
    if last_model is not None and entry.get("last_model") != last_model:
        entry["last_model"] = last_model
        changed = True
    if touch:
        entry["updated"] = _now()
        changed = True

    if changed:
        save_conversation_index(index)
    return entry


def ensure_active_conversation(model_name: str = "") -> dict:
    """Return the conversation to open at start-up, creating one if needed."""
    active = active_conversation_id()
    if active:
        entry = get_conversation(active)
        if entry is not None:
            return entry
    return create_conversation(model_name=model_name)


def delete_conversation(thread_id: str) -> None:
    """Drop a conversation from the index and delete its checkpoints."""
    index = load_conversation_index()
    remaining = [e for e in index["conversations"] if e["id"] != thread_id]
    if len(remaining) == len(index["conversations"]):
        return
    index["conversations"] = remaining
    if index["active"] == thread_id:
        index["active"] = remaining[0]["id"] if remaining else None
    save_conversation_index(index)
    try:
        get_checkpointer().delete_thread(thread_id)
    except Exception:
        logger.warning("could not delete checkpoints for %s", thread_id, exc_info=True)
