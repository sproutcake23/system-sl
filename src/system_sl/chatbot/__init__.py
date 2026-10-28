"""System-SL Chatbot Package.

Exports the system agent, prompts, and session management.
"""

from system_sl.chatbot.system_prompts import SYSTEM_MENTOR_PROMPT
from system_sl.chatbot.system_adi import get_system_agent, reset_agent_cache
from system_sl.chatbot.client import ChatSession, reload_agent
from system_sl.chatbot.config import (
    ChatConfig,
    ChatConfigError,
    load_config,
    save_config,
    validate_config,
    discover_local_models,
    get_preset_models,
    ProviderPreset,
    get_config_path,
    get_model_cache,
    set_model_cache,
    invalidate_model_cache,
)
from system_sl.chatbot.llm_factory import list_presets, get_preset
from system_sl.chatbot.model_fetcher import (
    ModelInfo,
    ModelFetcherError,
    fetch_models_for_provider,
)

__all__ = [
    "SYSTEM_MENTOR_PROMPT",
    "get_system_agent",
    "reset_agent_cache",
    "ChatSession",
    "reload_agent",
    "ChatConfig",
    "ChatConfigError",
    "load_config",
    "save_config",
    "validate_config",
    "discover_local_models",
    "get_preset_models",
    "ProviderPreset",
    "list_presets",
    "get_preset",
    "get_config_path",
    "get_model_cache",
    "set_model_cache",
    "invalidate_model_cache",
    "ModelInfo",
    "ModelFetcherError",
    "fetch_models_for_provider",
]