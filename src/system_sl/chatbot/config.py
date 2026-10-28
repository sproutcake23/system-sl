"""Chatbot configuration management.

Supports multiple LLM providers via a unified OpenAI-compatible interface
plus native Google Gemini support.
"""

import json
import os
import subprocess
import requests
from dataclasses import dataclass, asdict
from typing import Literal, Optional

from system_sl.utils import get_tasks_file_path, secure_storage
from system_sl.chatbot.llm_factory import Provider, ProviderPreset, PRESETS, get_preset, list_presets
from system_sl.chatbot.model_fetcher import (
    ModelInfo,
    fetch_models_for_provider,
    save_model_cache,
    load_model_cache,
    clear_model_cache,
    CACHE_TTL_SECONDS,
)


CONFIG_FILENAME = "llm_config.json"


@dataclass
class ChatConfig:
    """Runtime configuration for the chatbot LLM.

    Attributes:
        provider: "google" for Gemini, "openai" for OpenAI-compatible APIs
        base_url: Base URL for OpenAI-compatible APIs (required for "openai" provider)
        api_key: API key (required for "google", optional for local "openai" servers)
        system_model: Model name for the system persona
    """
    provider: Provider = "google"
    base_url: Optional[str] = None
    api_key: str = ""
    system_model: str = "gemini-1.5-flash"


class ChatConfigError(RuntimeError):
    pass


def load_config() -> ChatConfig:
    """Load configuration from file, environment, and defaults.

    Priority (highest first):
    1. ~/.config/system-sl/llm_config.json
    2. Environment variables (LLM_PROVIDER, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL)
    3. Hardcoded defaults (Google Gemini)

    Returns:
        ChatConfig with resolved values.
    """
    path = get_tasks_file_path(CONFIG_FILENAME)
    data: dict = {}

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read().strip()
            if text:
                data = json.loads(text)
        except OSError as e:
            raise ChatConfigError(f"Failed to read {path}: {e}") from e
        except json.JSONDecodeError as e:
            raise ChatConfigError(f"{path} is not valid JSON: {e}") from e

    # Provider
    provider = (
        data.get("provider")
        or os.environ.get("LLM_PROVIDER")
        or "google"
    )

    # Base URL (only used for openai provider)
    base_url = (
        data.get("base_url")
        or os.environ.get("LLM_BASE_URL")
    )

    # API Key
    # Stored securely in the system keyring (KDE Wallet / GNOME Keyring /
    # macOS Keychain / Windows Credential Manager) with an encrypted-file
    # fallback for headless environments. Environment variables still take
    # precedence for explicit overrides.
    api_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or secure_storage.get_secret(secure_storage.SECRET_LLM_API_KEY)
        or data.get("api_key", "")  # backward compat with existing installs
        or ""
    )

    # Model
    system_model = (
        data.get("system_model")
        or os.environ.get("LLM_MODEL")
        or _default_model_for_provider(provider)
    )

    return ChatConfig(
        provider=provider,  # type: ignore[arg-type]
        base_url=base_url,
        api_key=api_key,
        system_model=system_model,
    )


def _default_model_for_provider(provider: str) -> str:
    """Get default model for a provider."""
    if provider == "google":
        return "gemini-1.5-flash"
    # openai-compatible defaults
    return "llama3.1"


def save_config(config: ChatConfig) -> None:
    """Save configuration to ~/.config/system-sl/llm_config.json.

    Non-secret fields (provider, base_url, system_model) are written to the
    config file. The API key is stored separately in the system keyring so it
    is never written to disk in plaintext.
    """
    path = get_tasks_file_path(CONFIG_FILENAME)
    try:
        non_secret = {
            "provider": config.provider,
            "base_url": config.base_url,
            "system_model": config.system_model,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(non_secret, f, indent=2)
    except OSError as e:
        raise ChatConfigError(f"Failed to write {path}: {e}") from e

    # Persist the API key securely, never in plaintext on disk.
    if config.api_key:
        secure_storage.set_secret(secure_storage.SECRET_LLM_API_KEY, config.api_key)


def validate_config(config: ChatConfig) -> list[str]:
    """Validate configuration and return list of errors (empty if valid)."""
    errors = []

    if config.provider not in ("google", "openai"):
        errors.append(f"Invalid provider: {config.provider}. Must be 'google' or 'openai'")

    if config.provider == "google":
        if not config.api_key:
            errors.append("Google provider requires an API key (set LLM_API_KEY or GOOGLE_API_KEY)")
    elif config.provider == "openai":
        if not config.base_url:
            errors.append("OpenAI-compatible provider requires base_url")

    if not config.system_model:
        errors.append("system_model cannot be empty")

    return errors


def get_config_path() -> str:
    """Get the path to the LLM config file."""
    return get_tasks_file_path(CONFIG_FILENAME)


def get_model_cache(provider: str, max_age_seconds: int = CACHE_TTL_SECONDS) -> list[ModelInfo] | None:
    """Load cached models for a provider if not expired."""
    path = get_config_path()
    return load_model_cache(path, provider, max_age_seconds)


def set_model_cache(provider: str, models: list[ModelInfo]) -> None:
    """Save fetched models to cache."""
    path = get_config_path()
    save_model_cache(path, provider, models)


def invalidate_model_cache(provider: str | None = None) -> None:
    """Clear model cache for a provider or all providers."""
    path = get_config_path()
    clear_model_cache(path, provider)


# =============================================================================
# MODEL DISCOVERY
# =============================================================================

DiscoveredModel = tuple[str, str]  # (display_name, model_id)


def discover_ollama_models(base_url: str = "http://localhost:11434") -> list[DiscoveredModel]:
    """Discover models from a running Ollama instance.

    Args:
        base_url: Ollama API base URL (default: http://localhost:11434)

    Returns:
        List of (display_name, model_id) tuples.
    """
    models = []

    # Try ollama CLI first (more reliable)
    try:
        result = subprocess.run(
            ["ollama", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            import json as json_mod
            data = json_mod.loads(result.stdout)
            for item in data:
                name = item.get("name", "")
                if name:
                    models.append((f"🦙 {name}", name))
            return models
    except (subprocess.SubprocessError, FileNotFoundError, json.JSONDecodeError):
        pass

    # Fallback to HTTP API
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("models", []):
                name = item.get("name", "")
                if name:
                    models.append((f"🦙 {name}", name))
    except requests.RequestException:
        pass

    return models


def discover_lm_studio_models(base_url: str = "http://localhost:1234/v1") -> list[DiscoveredModel]:
    """Discover models from a running LM Studio instance.

    Args:
        base_url: LM Studio OpenAI-compatible API base URL

    Returns:
        List of (display_name, model_id) tuples.
    """
    try:
        resp = requests.get(f"{base_url}/models", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            models = []
            for item in data.get("data", []):
                model_id = item.get("id", "")
                if model_id:
                    models.append((f"🖥️ {model_id}", model_id))
            return models
    except requests.RequestException:
        pass
    return []


def discover_local_models() -> list[DiscoveredModel]:
    """Discover all locally available models from known servers.

    Returns:
        Combined list of discovered models with source indicators.
    """
    models = []
    models.extend(discover_ollama_models())
    models.extend(discover_lm_studio_models())
    return models


def get_preset_models() -> list[DiscoveredModel]:
    """Get well-known cloud models from presets (for dropdown suggestions)."""
    models = []
    for preset in list_presets():
        if preset.provider == "openai" and preset.default_model and preset.base_url:
            # Only add cloud presets (not local ones)
            if "localhost" not in preset.base_url and "127.0.0.1" not in preset.base_url:
                models.append((f"☁️ {preset.name}: {preset.default_model}", preset.default_model))
    return models