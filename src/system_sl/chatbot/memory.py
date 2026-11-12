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
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langsmith import uuid7

from system_sl.utils.atomic_json import atomic_write_json, load_json_or_default
from system_sl.utils.file_lock import file_lock
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

# The index is read-modify-write from the GUI and the background process.
# Atomic writes prevent torn files; this lock prevents a lost update between
# the read and the write.
_INDEX_LOCK = threading.RLock()
_INDEX_LOCK_PATH = CONVERSATION_INDEX_FILENAME + ".lock"


@contextmanager
def _index_transaction():
    """Hold the conversation index across a read-modify-write."""
    with _INDEX_LOCK, file_lock(get_tasks_file_path(_INDEX_LOCK_PATH)):
        yield

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

    Two shapes can be left behind:

    * **Paused before the tools node** (``next == ('tools',)``): the model asked
      for a tool that never finished. Its call is rolled back (the model's
      words stay) and a note says so.
    * **Paused before the model node** (``next == ('model',)``): the model step
      died — before it wrote anything, or after tools had already run (Stop
      while the final answer streams). Tool results that are in the history
      really happened, so they are kept, and the turn is closed by writing a
      short stop note *as the model's output*. The model node's edge then sees
      an answer with no tool calls and routes to END. (Writing an empty update
      as the model instead re-routes to "model", which is not a destination of
      that edge, and raises ``KeyError: 'model'`` on every later turn.)

    Returns True when something was repaired. Raises only if the repair itself
    cannot be written, or the thread is still paused afterwards; callers treat
    that as a failed turn.
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
        else:
            agent.update_state(config, {"messages": rollback})
            logger.info(
                "rolled back an interrupted tool call in conversation %s",
                _thread_id(config),
            )
        state = agent.get_state(config)
        pending = tuple(state.next or ())

    if pending:
        messages = list(state.values.get("messages") or [])
        close = AIMessage(content=_model_stop_note(messages))
        agent.update_state(config, {"messages": [close]}, as_node="model")
        logger.info(
            "closed an interrupted model step in conversation %s",
            _thread_id(config),
        )

    remaining = tuple(agent.get_state(config).next or ())
    if remaining:
        raise RuntimeError(
            f"conversation is still paused at {remaining} after the repair"
        )
    return True


def rewind_before(agent, config: dict, message_id: str) -> int:
    """Remove the stored message ``message_id`` and everything after it.

    Used by regenerate, retry and edit-and-resend: the conversation goes back
    to how it was before that Player message, so resending it does not leave
    the old question, the discarded answer, or their tool traffic in the
    model's memory. Removal goes through the ``messages`` reducer
    (``RemoveMessage``), the supported way to delete from LangGraph state; the
    thread id, and so the conversation index entry, are unchanged.

    Returns how many messages were removed. Raises ``LookupError`` when the
    message is not in the conversation (it never reached the checkpoint).
    """
    state = agent.get_state(config)
    if state.next:
        raise RuntimeError("the conversation has an unfinished turn; repair it first")
    messages = list(state.values.get("messages") or [])
    index = next(
        (i for i, message in enumerate(messages) if getattr(message, "id", None) == message_id),
        None,
    )
    if index is None:
        raise LookupError(f"message {message_id} is not in conversation {_thread_id(config)}")

    removals = [
        RemoveMessage(id=message.id)
        for message in messages[index:]
        if getattr(message, "id", None)
    ]
    # Written as the model's step: its edge then sees no pending tool call and
    # ends the turn. Left to LangGraph to infer, the writer after a repaired
    # turn is the wrong node and the thread stays paused at "model".
    agent.update_state(config, {"messages": removals}, as_node="model")

    remaining = tuple(agent.get_state(config).next or ())
    if remaining:
        raise RuntimeError(
            f"conversation is paused at {remaining} after rewinding"
        )
    logger.info(
        "rewound conversation %s by %d message(s)", _thread_id(config), len(removals)
    )
    return len(removals)


def _completed_tools_in_turn(messages: Sequence[BaseMessage]) -> list[str]:
    """Names of the tools that returned a result since the Player's last message."""
    names: list[str] = []
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, ToolMessage):
            names.append(getattr(message, "name", None) or "a tool")
    names.reverse()
    return names


def _model_stop_note(messages: Sequence[BaseMessage]) -> str:
    """The text that closes a turn whose model step was interrupted."""
    completed = _completed_tools_in_turn(messages)
    if completed:
        unique = list(dict.fromkeys(completed))
        return (
            STOP_NOTE_PREFIX
            + f" The Player stopped this reply after {', '.join(unique)} had"
            " already run; those results stand, but the answer was not finished."
        )
    return STOP_NOTE_PREFIX + " The Player stopped this reply before it was finished."


def _rollback_messages(messages: Sequence[BaseMessage]) -> list[BaseMessage] | None:
    """Describe how to undo an interrupted tool call, or None if there is none.

    Calls that already have a result in the history ran for real: they and
    their results are kept. Only calls with no result are dropped from the
    model's message (the reducer replaces a message with the same id), so no
    call is left without an answer and no result is left without its call. The
    stop note says which tools were cancelled. Tool calls run in parallel can
    finish without their result being stored, so with more than one call the
    note does not promise that nothing changed.
    """
    last_ai = next(
        (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], AIMessage)),
        None,
    )
    if last_ai is None or not messages[last_ai].tool_calls:
        return None

    ai_message = messages[last_ai]
    answered = {
        getattr(message, "tool_call_id", None)
        for message in messages[last_ai + 1 :]
        if isinstance(message, ToolMessage)
    }
    kept = [call for call in ai_message.tool_calls if call.get("id") in answered]
    dropped = [call for call in ai_message.tool_calls if call.get("id") not in answered]
    if not dropped:
        return None

    patched = ai_message.model_copy(
        update={"tool_calls": kept, "invalid_tool_calls": []}
    )
    if len(ai_message.tool_calls) == 1:
        note = STOP_NOTE
    else:
        names = ", ".join(dict.fromkeys(call.get("name") or "a tool" for call in dropped))
        note = (
            STOP_NOTE_PREFIX
            + f" The Player stopped this reply while {names} was running; it may"
            " not have finished, so check before assuming it changed anything."
        )
    return [patched, HumanMessage(content=note)]


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


def create_conversation(
    model_name: str = "", title: str = "", thread_id: str | None = None
) -> dict:
    """Create a conversation, make it active, and return its entry."""
    with _index_transaction():
        index = load_conversation_index()
        entry = {
            "id": thread_id or new_thread_id(),
            "title": title or "",
            "created": _now(),
            "updated": _now(),
            "last_model": model_name or "",
        }
        index["conversations"].append(entry)
        index["active"] = entry["id"]
        save_conversation_index(index)
    return entry


def ensure_conversation(
    thread_id: str,
    *,
    model_name: str = "",
    title: str = "",
    touch: bool = True,
) -> dict | None:
    """Return a conversation entry, creating it when it is still only local.

    ``New chat`` hands out a thread id without writing an index entry, so an
    unused conversation never appears in history. The first sent message calls
    this to persist it.
    """
    with _index_transaction():
        index = load_conversation_index()
        entry = next(
            (e for e in index["conversations"] if e["id"] == thread_id), None
        )
        if entry is None:
            entry = create_conversation(
                model_name=model_name, title=title, thread_id=thread_id
            )
            return entry
        changed = False
        if model_name and entry.get("last_model") != model_name:
            entry["last_model"] = model_name
            changed = True
        if title and not entry.get("title"):
            entry["title"] = title
            changed = True
        if touch:
            entry["updated"] = _now()
            changed = True
        if changed:
            index["active"] = thread_id
            save_conversation_index(index)
    return entry


def select_conversation(thread_id: str) -> dict | None:
    """Mark ``thread_id`` as the active conversation."""
    with _index_transaction():
        index = load_conversation_index()
        entry = next(
            (e for e in index["conversations"] if e["id"] == thread_id), None
        )
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
    with _index_transaction():
        index = load_conversation_index()
        entry = next(
            (e for e in index["conversations"] if e["id"] == thread_id), None
        )
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
    with _index_transaction():
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
