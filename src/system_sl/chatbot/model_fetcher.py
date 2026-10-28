"""Model fetching from provider APIs (OpenRouter, etc.)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


@dataclass
class ModelInfo:
    """Information about a model from a provider."""
    id: str
    name: str
    context_length: int
    prompt_price: float  # per 1M tokens
    completion_price: float  # per 1M tokens
    is_free: bool
    provider: str = "openrouter"

    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.id})"

    @property
    def price_display(self) -> str:
        if self.is_free:
            return "✨ FREE"
        return f"${self.prompt_price:.2f}/${self.completion_price:.2f} per 1M"


class ModelFetcherError(Exception):
    pass


def fetch_openrouter_models(api_key: str, base_url: str = "https://openrouter.ai/api/v1") -> list[ModelInfo]:
    """Fetch available models from OpenRouter API.

    Args:
        api_key: OpenRouter API key
        base_url: OpenRouter base URL (default: https://openrouter.ai/api/v1)

    Returns:
        List of ModelInfo objects sorted by name.

    Raises:
        ModelFetcherError: If fetch fails.
    """
    url = f"{base_url.rstrip('/')}/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    req = Request(url, headers=headers)

    try:
        with urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                raise ModelFetcherError(f"HTTP {resp.status}: {resp.read().decode()}")
            data = json.loads(resp.read().decode())
    except HTTPError as e:
        raise ModelFetcherError(f"API error: {e.code} - {e.read().decode()}")
    except URLError as e:
        raise ModelFetcherError(f"Network error: {e.reason}")
    except json.JSONDecodeError as e:
        raise ModelFetcherError(f"Invalid JSON response: {e}")

    models = []
    for item in data.get("data", []):
        model_id = item.get("id", "")
        if not model_id:
            continue

        # Parse pricing
        pricing = item.get("pricing", {})
        prompt_price = float(pricing.get("prompt", "0") or "0")
        completion_price = float(pricing.get("completion", "0") or "0")

        # OpenRouter uses per-token pricing; convert to per-1M
        prompt_price_per_m = prompt_price * 1_000_000
        completion_price_per_m = completion_price * 1_000_000

        # Detect free models
        is_free = (prompt_price == 0 and completion_price == 0)

        models.append(ModelInfo(
            id=model_id,
            name=item.get("name", model_id),
            context_length=item.get("context_length", 0),
            prompt_price=prompt_price_per_m,
            completion_price=completion_price_per_m,
            is_free=is_free,
            provider="openrouter",
        ))

    # Sort: free first, then by name
    models.sort(key=lambda m: (not m.is_free, m.name.lower()))
    return models


def fetch_models_for_provider(provider: str, api_key: str, base_url: str) -> list[ModelInfo]:
    """Fetch models for a given provider.

    Currently only supports OpenRouter. Extend for others as needed.
    """
    if provider == "openrouter":
        return fetch_openrouter_models(api_key, base_url)
    elif provider in ("ollama", "lm_studio"):
        # Local servers - use existing discovery
        from system_sl.chatbot.config import discover_ollama_models, discover_lm_studio_models
        if provider == "ollama":
            return [ModelInfo(id=m[1], name=m[0].replace("🦙 ", ""), context_length=0,
                              prompt_price=0, completion_price=0, is_free=True, provider="ollama")
                    for m in discover_ollama_models(base_url.replace("/v1", ""))]
        else:
            return [ModelInfo(id=m[1], name=m[0].replace("🖥️ ", ""), context_length=0,
                              prompt_price=0, completion_price=0, is_free=True, provider="lm_studio")
                    for m in discover_lm_studio_models(base_url)]
    else:
        raise ModelFetcherError(f"Model fetching not supported for provider: {provider}")


# =============================================================================
# CACHE HELPERS
# =============================================================================

CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours


def _get_cache_key(provider: str) -> str:
    return f"{provider}_models_cache"


def save_model_cache(config_path: str, provider: str, models: list[ModelInfo]) -> None:
    """Save fetched models to config file cache."""
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config = {}

    cache = {
        "fetched_at": time.time(),
        "models": [asdict(m) for m in models],
    }
    config.setdefault("model_cache", {})[provider] = cache

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)


def load_model_cache(config_path: str, provider: str, max_age_seconds: int = CACHE_TTL_SECONDS) -> Optional[list[ModelInfo]]:
    """Load cached models if not expired."""
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    cache = config.get("model_cache", {}).get(provider)
    if not cache:
        return None

    fetched_at = cache.get("fetched_at", 0)
    if time.time() - fetched_at > max_age_seconds:
        return None  # Expired

    models_data = cache.get("models", [])
    return [ModelInfo(**m) for m in models_data]


def clear_model_cache(config_path: str, provider: Optional[str] = None) -> None:
    """Clear model cache for a provider or all providers."""
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return

    if provider:
        config.get("model_cache", {}).pop(provider, None)
    else:
        config["model_cache"] = {}

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)