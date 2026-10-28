"""System-SL AI Agent (System Persona).

Provides a LangGraph agent with tools for task management,
using a configurable LLM backend (Google Gemini or OpenAI-compatible).
"""

from typing import Any, Optional
from functools import lru_cache

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

from system_sl.chatbot import SYSTEM_MENTOR_PROMPT
from system_sl.chatbot.config import ChatConfig, load_config
from system_sl.chatbot.llm_factory import create_llm_from_config, LLMFactoryError


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
    """Add a brand new task to a specific category."""
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
def read_persona() -> Any:
    """Read the user's personal archetype profile configuration."""
    return load_data(get_tasks_file_path("persona.json"))


@tool
def read_user_info() -> Any:
    """Read the legacy user goals and behavioral keywords profile file."""
    return load_data(get_tasks_file_path("user_info.json"))


# Tool list for the system agent
SYSTEM_TOOLS = [
    load_tasks,
    load_completed_tasks,
    add_task,
    remove_task,
    mark_task_completed,
    get_random_task,
    read_persona,
    read_user_info,
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


@lru_cache(maxsize=1)
def get_system_agent():
    """Build (once) and return the SYSTEM mentor agent.

    Uses the configured LLM provider and model.
    Rebuilds if config changes (cache keyed on config hash via lru_cache).
    """
    try:
        llm = _get_cached_llm()
    except LLMFactoryError as e:
        # Provide a helpful error message
        raise RuntimeError(f"Failed to initialize LLM: {e}") from e

    return create_agent(
        model=llm,
        tools=SYSTEM_TOOLS,
        system_prompt=SYSTEM_MENTOR_PROMPT,
    )


# Force rebuild of agent when config changes
def reset_agent_cache() -> None:
    """Clear the cached agent and LLM. Call after config changes."""
    _get_cached_llm.cache_clear()
    get_system_agent.cache_clear()