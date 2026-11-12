"""System-SL AI Agent (System Persona).

Provides a LangGraph agent with tools for task management,
using a configurable LLM backend (Google Gemini or OpenAI-compatible).
"""

from dataclasses import dataclass, replace
from typing import Any, Callable, Optional
from functools import lru_cache
import re

# LangChain imports
from langchain.agents import create_agent
from langchain_core.tools import tool

# Core task management functions
from system_sl.core import (
    add_tasks,
    remove_tasks,
    mark_task_completed as core_mark_task_completed,
    load_tasks as core_load_tasks,
    load_completed_tasks as core_load_completed_tasks,
    get_random_task as core_get_random_task,
)

from system_sl.utils import (
    load_data,
    get_tasks_file_path,
)

from system_sl.chatbot.system_prompts import SYSTEM_MENTOR_PROMPT
from system_sl.chatbot.agent_middleware import make_context_middleware
from system_sl.chatbot.config import ChatConfig, load_config, validate_config
from system_sl.chatbot.llm_factory import create_llm_from_config, LLMFactoryError
from system_sl.chatbot.memory import get_checkpointer


# =============================================================================
# TURN CONTEXT — who is asking, and where task proposals go
# =============================================================================

# A task may only be created without asking when the Player's own message
# contains a direct instruction. The match must start a clause, so advice and
# questions ("you could add a task", "should I add a task?") do not count.
_CLAUSE_START = r"(?:^|[.!?;\n]|,\s*|\b(?:and|then|also)\s+)\s*"
_EXPLICIT_ADD = re.compile(
    _CLAUSE_START
    + r"(?:please\s+)?(?:"
    r"(?:add|create)\b[^.?!\n]{0,60}\b(?:tasks?|list)\b"
    r"|remind\s+me\s+to\b"
    r")",
    re.IGNORECASE,
)

# Content the chat panel puts around the Player's own words: attached
# documents (<document …>…</document>, or a "[uploaded document: …]" line when
# extraction failed) and quoted messages (<quote …>…</quote>).
_BLOCK_RE = re.compile(
    r"<\s*(document|quote)\b[^>]*>.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL
)
_FAILED_ATTACHMENT_RE = re.compile(r"^\[uploaded document:[^\n]*\]\s*$", re.MULTILINE)
_LEFTOVER_TAG_RE = re.compile(r"<\s*/?\s*(?:document|quote)\b", re.IGNORECASE)
_DOCUMENT_RE = re.compile(
    r"<\s*document\b|^\[uploaded document:", re.IGNORECASE | re.MULTILINE
)


def message_has_documents(message: str) -> bool:
    """True when a composed chat message carries an attached document.

    A document can hide instructions ("create a task to ..."). Even when the
    Player typed an explicit request alongside it, the model could add other
    tasks the document asked for, so with a document attached every task
    still needs the Player's Yes.
    """
    return bool(_DOCUMENT_RE.search(message or ""))


def player_typed_text(message: str) -> str:
    """Recover the Player's own words from a composed chat message.

    Fallback for callers that did not pass the typed text separately. Fails
    closed: if a document or quote block cannot be cleanly removed (a tag left
    over), nothing is treated as typed, so the task gate asks for confirmation.
    """
    text = _BLOCK_RE.sub("", message or "")
    text = _FAILED_ATTACHMENT_RE.sub("", text)
    if _LEFTOVER_TAG_RE.search(text):
        return ""
    return text.strip()


def is_explicit_task_request(text: str) -> bool:
    """Return True when the Player directly asked for a task to be added.

    Deliberately strict: only clause-initial commands count ("add a task X",
    "please create task X", "remind me to X"). Suggestions, questions and
    hypotheticals fall through to the confirmation prompt.

    ``text`` must be only what the Player typed: quoted messages and attached
    documents are not the Player's instruction (see ``player_typed_text``).
    """
    return bool(text and _EXPLICIT_ADD.search(text))


# Context variable for the current turn's user text and proposal callback.
# Using contextvars ensures isolation between concurrent turns (e.g., when
# multiple workers run) and avoids the module-global race.
import contextvars

_turn_context: contextvars.ContextVar[tuple[str, Callable[[dict], None]] | None] = contextvars.ContextVar(
    "_turn_context", default=None
)


def set_turn_context(
    user_text: str,
    on_proposal: Optional[Callable[[dict], None]] = None,
) -> None:
    """Scope a user message to the tools that run during this turn."""
    _turn_context.set((user_text or "", on_proposal))


def clear_turn_context() -> None:
    """Drop the turn context once the turn has finished or been abandoned."""
    _turn_context.set(None)


def _get_turn_context() -> tuple[str, Callable[[dict], None] | None]:
    """Get the current turn's context, returning empty defaults if unset."""
    return _turn_context.get() or ("", None)


# =============================================================================
# TOOLS (same as before)
# =============================================================================

@tool
def load_tasks() -> Any:
    """Return the user's current open tasks, grouped by category."""
    return core_load_tasks()


@tool
def load_completed_tasks() -> Any:
    """Return the user's historical completed tasks, grouped by category."""
    return core_load_completed_tasks()


@tool
def add_task(category: str, title: str, deadline: Optional[str] = None) -> Any:
    """Add a brand new task to a specific category.

    The task is only created when the Player explicitly asked for it in their
    latest message. Otherwise it is queued for a Yes/No confirmation and the
    result is ``pending_confirmation`` — do not claim it was added.
    """
    user_text, on_proposal = _get_turn_context()
    if not is_explicit_task_request(user_text):
        proposal = {"category": category, "title": title, "deadline": deadline}
        if on_proposal is not None:
            try:
                on_proposal(proposal)
            except Exception as e:
                logger.warning("on_proposal callback failed: %s", e)
        return {
            "status": "pending_confirmation",
            "message": (
                "The task was NOT added. The Player has been shown a Yes/No "
                "prompt and must confirm it. Do not tell them it was added."
            ),
            **proposal,
        }

    try:
        return add_tasks(category, title, deadline)
    except ValueError as e:
        msg = str(e)
        kind = "duplicate" if "already exists" in msg else "invalid"
        return {"error": kind, "message": msg}


@tool
def remove_task(title: str, category: str) -> Any:
    """Remove/delete a specific task from a targeted category completely."""
    try:
        return remove_tasks(title, category)
    except ValueError as e:
        return {"error": "not_found", "message": str(e)}


@tool
def mark_task_completed(title: str, category: str) -> Any:
    """Move a task from the open status to the completed status pool."""
    try:
        return core_mark_task_completed(title, category)
    except ValueError as e:
        return {"error": "not_found", "message": str(e)}


@tool
def get_random_task() -> Any:
    """Pick and select one random open task from the user's active categories."""
    return core_get_random_task()


@tool
def propose_tasks_batch(tasks: list[dict]) -> Any:
    """Propose multiple tasks at once from an uploaded document.

    This tool ALWAYS returns pending_confirmation and shows a preview dialog.
    It never auto-adds tasks, even when the request looks explicit.
    Use this when the Player uploads a document (timetable, syllabus, etc.)
    and asks for multiple tasks to be created from it.

    Args:
        tasks: List of task dicts, each with keys:
            - title (str, required): Task title. Should embed subject+date to avoid
              global dedupe, e.g. "Physics — final exam prep (2026-11-17)".
            - category (str, optional): Category name (default "study").
            - deadline (str, optional): YYYY-MM-DD format.

    Returns:
        dict with status "pending_confirmation" and the full task list.
    """
    _, on_proposal = _get_turn_context()
    validated: list[dict] = []
    errors: list[str] = []

    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"Item {i}: not a dict")
            continue
        title = task.get("title")
        if not title or not isinstance(title, str) or not title.strip():
            errors.append(f"Item {i}: missing or empty title")
            continue
        category = task.get("category", "study")
        if not isinstance(category, str):
            category = "study"
        deadline = task.get("deadline")
        if deadline is not None and not isinstance(deadline, str):
            deadline = None
        validated.append({"title": title.strip(), "category": category.strip().lower(), "deadline": deadline})

    if not validated:
        return {"status": "error", "message": "No valid tasks in batch: " + "; ".join(errors)}

    proposal = {"type": "batch", "tasks": validated}
    if on_proposal is not None:
        try:
            on_proposal(proposal)
        except Exception as e:
            logger.warning("on_proposal callback failed: %s", e)

    return {
        "status": "pending_confirmation",
        "message": (
            f"Proposed {len(validated)} task(s) from document. "
            "The Player must confirm via the preview dialog."
        ),
        **proposal,
    }


@tool
def read_persona() -> Any:
    """Read the Player's persona: background, values and goals.

    Returns a short summary, not the raw profile file.
    """
    from system_sl.chatbot.dynamic_context import summarize_persona

    return summarize_persona(load_data(get_tasks_file_path("persona.json")))


# Tool list for the system agent
SYSTEM_TOOLS = [
    load_tasks,
    load_completed_tasks,
    add_task,
    remove_task,
    mark_task_completed,
    get_random_task,
    propose_tasks_batch,
    read_persona,
]


# =============================================================================
# LAZY INITIALIZATION
# =============================================================================

def _maybe_reset_on_config_change() -> None:
    """Check if config file changed externally and invalidate caches if so."""
    from system_sl.chatbot.config import _check_config_mtime
    if _check_config_mtime():
        reset_agent_cache()


@lru_cache(maxsize=1)
def _get_cached_llm():
    """Build (once) and return the shared LLM based on current config.

    Construction is deferred until first use so that config is loaded
    and any required API keys are available.
    """
    config = load_config()
    errors = validate_config(config)
    if errors:
        raise LLMFactoryError("; ".join(errors))
    return create_llm_from_config(config)


def build_agent_for_model(model_name: str):
    """Return the agent for ``model_name`` and its LLM, as ``(agent, llm)``.

    Built once per model and cached. The config file is checked first on
    every call: if it was changed outside the app (another window, an
    editor), the cache is dropped so the new settings take effect. The check
    has to sit in front of the cache — inside a cached builder it would only
    ever run on the first build.
    """
    _maybe_reset_on_config_change()
    return _build_agent_for_model(model_name)


@lru_cache(maxsize=8)
def _build_agent_for_model(model_name: str):
    """Build (once per model) an agent and return ``(agent, llm)``.

    Every agent shares the one process-wide checkpointer, so switching models
    continues the same conversation instead of starting from empty state.
    The LLM is returned alongside because the chat panel has to know which
    HTTP clients belong to the model that is about to answer, to cancel a
    stream on Stop.
    """
    config = load_config()

    # LLMFactoryError is raised unchanged so callers can tell "no API key
    # configured" apart from a genuine failure. The chat panel uses that to
    # offer the settings dialog instead of showing a raw error.
    if model_name == config.system_model:
        llm = _get_cached_llm()
    else:
        # A model picked in the dropdown is a per-conversation override; it
        # must never be written into the saved default.
        override = replace(config, system_model=model_name)
        errors = validate_config(override)
        if errors:
            raise LLMFactoryError("; ".join(errors))
        llm = create_llm_from_config(override)

    # Build system prompt with optional addendum
    system_prompt = SYSTEM_MENTOR_PROMPT
    addendum = config.system_prompt_addendum.strip()
    if addendum:
        system_prompt += "\n\n--- Additional Instructions ---\n" + addendum

    agent = create_agent(
        model=llm,
        tools=SYSTEM_TOOLS,
        system_prompt=system_prompt,
        middleware=[make_context_middleware()],
        checkpointer=get_checkpointer(),
    )
    return agent, llm


def get_system_agent():
    """Return the SYSTEM mentor agent for the saved default model.

    Cached per model by ``build_agent_for_model``, which also notices config
    changes made outside the app; ``reset_agent_cache()`` drops it explicitly.
    """
    return build_agent_for_model(load_config().system_model)[0]


def get_system_agent_for_model(model_name: str):
    """Build an agent for ``model_name`` without changing the saved default.

    The chat panel lets the Player try another model for the current
    conversation. Writing that choice into the global config used to make a
    casual switch the new default, so the override stays ephemeral here.
    """
    return build_agent_for_model(model_name)[0]


# Force rebuild of agent when config changes
def reset_agent_cache() -> None:
    """Clear the cached agents and LLMs. Call after config changes.

    The checkpointer is intentionally left alone: the conversation memory is
    not an agent cache entry, and dropping it would erase every conversation.
    """
    from system_sl.chatbot.llm_factory import clear_cached_llm
    _get_cached_llm.cache_clear()
    _build_agent_for_model.cache_clear()
    clear_cached_llm()
