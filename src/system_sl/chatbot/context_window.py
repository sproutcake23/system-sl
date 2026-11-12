"""Context-window budgets for the chat agent.

The agent needs to know how much room the current model gives it: attachments
have to fit inside it and the history has to be trimmed to it. Providers
report their window very differently (some report nothing at all), so the
lookup falls through several sources before settling on a constant.

Nothing here talks to the network. The only live source is the LangChain
model profile, which the chat model resolves locally when it is constructed.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

# Used when nothing better is known about the model. Deliberately modest:
# a local model with 8k tokens must still work, and the budgets below scale
# down with it.
DEFAULT_CONTEXT_WINDOW = 32_000

# Rough characters-per-token ratio for English prose. Good enough to turn a
# token budget into a character budget and back.
CHARS_PER_TOKEN = 4.0

# Share of the window that one message's attachments may use.
ATTACHMENT_BUDGET_RATIO = 0.15

# Share of the window the conversation history may use. The rest is left for
# the system prompt, the dynamic context block, the tool schemas and the
# model's own answer.
HISTORY_BUDGET_RATIO = 0.70

# Room kept aside for the answer the model is about to write.
RESERVED_OUTPUT_TOKENS = 2_048

# Never let a budget collapse to nothing: a window that small cannot hold a
# conversation anyway, but the agent must still send *something*.
MIN_BUDGET_TOKENS = 512


# Context windows for models we recognise by name. Only used when the model
# itself does not report one (Google's chat model reports an empty profile,
# and OpenAI-compatible servers usually report none either).
_KNOWN_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("gpt-5", 400_000),
    ("gpt-4.1", 1_000_000),
    ("gpt-4o", 128_000),
    ("gpt-4", 128_000),
    ("o4-mini", 200_000),
    ("o3", 200_000),
    ("o1", 200_000),
    ("claude", 200_000),
    ("gemini-3", 1_000_000),
    ("gemini-2.5", 1_000_000),
    ("gemini-2.0", 1_000_000),
    ("gemini-1.5", 2_000_000),
    ("llama-3", 128_000),
    ("llama3", 128_000),
    ("deepseek", 64_000),
    ("mistral", 32_000),
    ("mixtral", 32_000),
    ("qwen", 32_000),
    ("gemma", 8_192),
    ("phi-4", 16_384),
    ("command-r", 128_000),
)


# =============================================================================
# TOKEN COUNTING
# =============================================================================

def count_text_tokens(text: str) -> int:
    """Approximate the number of tokens in a piece of text."""
    if not text:
        return 0
    return int(len(text) / CHARS_PER_TOKEN) + 1


def count_message_tokens(messages: Iterable[Any]) -> int:
    """Approximate the number of tokens a list of messages costs.

    Uses LangChain's own approximation so tool calls and tool results are
    counted the way the provider would count them.
    """
    items = list(messages)
    if not items:
        return 0
    try:
        from langchain_core.messages.utils import count_tokens_approximately

        return int(count_tokens_approximately(items))
    except Exception:  # pragma: no cover - the fallback is enough
        return sum(count_text_tokens(_message_text(m)) for m in items)


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
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


# =============================================================================
# CONTEXT WINDOW LOOKUP
# =============================================================================

def _profile_context_window(llm: Any) -> Optional[int]:
    """Return the window the model itself reports, if it reports one."""
    profile = getattr(llm, "profile", None)
    if not isinstance(profile, dict):
        return None
    value = profile.get("max_input_tokens")
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# Parsed llm_config.json, keyed by path and file mtime. The model list is
# read for every model call when a provider reports no profile; re-reading and
# re-parsing the whole file each time is pure overhead.
_config_cache: dict[str, tuple[float, Any]] = {}


def _load_config_data() -> Any:
    """Read llm_config.json, reusing the parse while the file is unchanged."""
    from system_sl.chatbot.config import get_config_path

    path = get_config_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    cached = _config_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = None
    _config_cache[path] = (mtime, data)
    return data


def reset_config_cache() -> None:
    """Forget the parsed config (used by tests and after an external edit)."""
    _config_cache.clear()


def _cached_context_length(model_name: str) -> Optional[int]:
    """Return the window the settings dialog cached for this model.

    The model list is fetched from OpenRouter when the Player picks a cloud
    model, and it carries the real context length. It is only a cache, so a
    missing or unreadable file is not an error.
    """
    if not model_name:
        return None
    data = _load_config_data()
    if not isinstance(data, dict):
        return None
    cache = data.get("model_cache") or {}
    if not isinstance(cache, dict):
        return None
    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        for model in entry.get("models") or []:
            if not isinstance(model, dict):
                continue
            if model.get("id") != model_name:
                continue
            try:
                length = int(model.get("context_length") or 0)
            except (TypeError, ValueError):
                continue
            if length > 0:
                return length
    return None


def _known_context_window(model_name: str) -> Optional[int]:
    """Return the window for a model we know by name."""
    if not model_name:
        return None
    lowered = model_name.lower()
    for marker, window in _KNOWN_CONTEXT_WINDOWS:
        if marker in lowered:
            return window
    return None


def context_window_for_model(model_name: str) -> int:
    """Return the context window of a model, by name."""
    return (
        _cached_context_length(model_name)
        or _known_context_window(model_name)
        or DEFAULT_CONTEXT_WINDOW
    )


def context_window_for_llm(llm: Any) -> int:
    """Return the context window of a chat model instance.

    The model itself is the best source: LangChain resolves a model profile
    (with ``max_input_tokens``) when the chat model is built. The name is
    only consulted when the model stays silent.
    """
    if llm is not None:
        window = _profile_context_window(llm)
        if window:
            return window
        name = getattr(llm, "model", None) or getattr(llm, "model_name", None)
        if isinstance(name, str) and name:
            return context_window_for_model(name)
    return DEFAULT_CONTEXT_WINDOW


# =============================================================================
# TOOL SCHEMAS
# =============================================================================

def count_tool_schema_tokens(tools: Iterable[Any]) -> int:
    """Approximate the tokens the bound tool schemas cost.

    Providers send the whole schema with every request, so it competes with the
    history for the same window. The schema is JSON; counting its text is a
    close enough estimate.
    """
    parts: list[str] = []
    for tool in tools or []:
        if isinstance(tool, dict):
            parts.append(json.dumps(tool, default=str, sort_keys=True))
            continue
        name = getattr(tool, "name", "") or ""
        description = getattr(tool, "description", "") or ""
        schema = getattr(tool, "args_schema", None)
        if schema is not None and hasattr(schema, "model_json_schema"):
            try:
                schema_text = json.dumps(schema.model_json_schema(), sort_keys=True)
            except Exception:
                schema_text = str(schema)
        elif isinstance(schema, dict):
            schema_text = json.dumps(schema, sort_keys=True)
        else:
            schema_text = str(schema or "")
        parts.append(name + description + schema_text)
    if not parts:
        return 0
    return count_text_tokens("\n".join(parts))


# =============================================================================
# BUDGETS
# =============================================================================

def attachment_budget_chars(context_tokens: int) -> int:
    """Return how many characters of attachments one message may carry."""
    tokens = max(MIN_BUDGET_TOKENS, int(context_tokens or 0))
    return max(2_000, int(tokens * ATTACHMENT_BUDGET_RATIO * CHARS_PER_TOKEN))


def history_budget_tokens(context_tokens: int) -> int:
    """Return how many tokens the conversation history may use."""
    tokens = max(MIN_BUDGET_TOKENS, int(context_tokens or 0))
    budget = int(tokens * HISTORY_BUDGET_RATIO)
    return max(MIN_BUDGET_TOKENS, budget - RESERVED_OUTPUT_TOKENS)
