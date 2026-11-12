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
from system_sl.chatbot.config import ChatConfig, load_config
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

# Attachment marker inserted by the chat panel when a document is uploaded.
_ATTACHMENT_MARKER = "[uploaded document:"


def _attached_doc_present(text: str) -> bool:
    """Return True when the user message contains an uploaded document marker."""
    return _ATTACHMENT_MARKER in (text or "")


def is_explicit_task_request(text: str) -> bool:
    """Return True when the Player directly asked for a task to be added.

    Deliberately strict: only clause-initial commands count ("add a task X",
    "please create task X", "remind me to X"). Suggestions, questions and
    hypotheticals fall through to the confirmation prompt.
    """
    return bool(text and _EXPLICIT_ADD.search(text))


@dataclass
class _TurnContext:
    """The user message (and proposal sink) for the turn currently running."""

    user_text: str
    on_proposal: Optional[Callable[[dict], None]] = None


_turn_context: Optional[_TurnContext] = None


def set_turn_context(
    user_text: str,
    on_proposal: Optional[Callable[[dict], None]] = None,
) -> None:
    """Scope a user message to the tools that run during this turn."""
    global _turn_context
    _turn_context = _TurnContext(user_text=user_text or "", on_proposal=on_proposal)


def clear_turn_context() -> None:
    """Drop the turn context once the turn has finished or been abandoned."""
    global _turn_context
    _turn_context = None


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
    context = _turn_context
    if context is None or not is_explicit_task_request(context.user_text) or _attached_doc_present(context.user_text):
        proposal = {"category": category, "title": title, "deadline": deadline}
        if context is not None and context.on_proposal is not None:
            try:
                context.on_proposal(proposal)
            except Exception:
                pass
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
    context = _turn_context
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
    if context is not None and context.on_proposal is not None:
        try:
            context.on_proposal(proposal)
        except Exception:
            pass

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


def validate_config(config: ChatConfig) -> list[str]:
    """Validate configuration and return list of errors."""
    errors = []
    if config.provider not in ("google", "openai"):
        errors.append(f"Invalid provider: {config.provider}. Must be 'google' or 'openai'")
    if config.provider == "google":
        if not config.api_key:
            errors.append("Google provider requires an API key")
    elif config.provider == "openai":
        if not config.base_url:
            errors.append("OpenAI-compatible provider requires base_url")
    if not config.system_model:
        errors.append("system_model cannot be empty")
    return errors


@lru_cache(maxsize=8)
def build_agent_for_model(model_name: str):
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

    agent = create_agent(
        model=llm,
        tools=SYSTEM_TOOLS,
        system_prompt=SYSTEM_MENTOR_PROMPT,
        middleware=[make_context_middleware()],
        checkpointer=get_checkpointer(),
    )
    return agent, llm


@lru_cache(maxsize=1)
def get_system_agent():
    """Build (once) and return the SYSTEM mentor agent for the saved default."""
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
    get_system_agent.cache_clear()
    build_agent_for_model.cache_clear()
    clear_cached_llm()
