"""Provider-agnostic LLM factory for System-SL chatbot.

Supports:
- Google Gemini (ChatGoogleGenerativeAI)
- OpenAI-compatible APIs (ChatOpenAI): Ollama, LM Studio, vLLM, OpenRouter, Groq, Together, custom endpoints
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Any

import httpx
from langchain_core.language_models.chat_models import BaseChatModel

# Optional imports - only loaded when needed
try:
    from langchain_google_genai import ChatGoogleGenerativeAI
except ImportError:
    ChatGoogleGenerativeAI = None  # type: ignore

try:
    from langchain_openai import ChatOpenAI
except ImportError:
    ChatOpenAI = None  # type: ignore

# Track the HTTP client(s) of the currently active LLM for cancellation support.
# Only one chat request runs at a time (enforced by ChatPanel), so a single
# reference is sufficient. Set to None when no request is active.
_current_llm_client: Any = None
_current_llm_async_client: Any = None

# Reference to the cached LLM instance, so we can access its HTTP clients.
_cached_llm_instance: BaseChatModel | None = None


def _set_current_llm_clients(sync_client: Any = None, async_client: Any = None) -> None:
    """Register the HTTP client(s) of the LLM currently handling a request."""
    global _current_llm_client, _current_llm_async_client
    _current_llm_client = sync_client
    _current_llm_async_client = async_client


def _clear_current_llm_clients() -> None:
    """Clear the tracked HTTP clients (called when request completes or is cancelled)."""
    global _current_llm_client, _current_llm_async_client
    _current_llm_client = None
    _current_llm_async_client = None


def _register_cached_llm(llm: BaseChatModel) -> None:
    """Store a reference to the cached LLM for client access."""
    global _cached_llm_instance
    _cached_llm_instance = llm


def register_active_llm(llm: BaseChatModel | None) -> None:
    """Point the client registry at the LLM that is about to answer.

    Each model in the dropdown has its own LLM instance. Registering it before
    a turn starts makes ``get_cached_llm_clients()`` return the right HTTP
    clients, so Stop cancels the stream that is actually running.
    """
    if llm is not None:
        _register_cached_llm(llm)


def get_cached_llm_clients() -> tuple[Any, Any]:
    """Get the HTTP client(s) from the cached LLM instance.

    Returns (sync_client, async_client) or (None, None) if not available.
    For OpenAI-compatible LLMs, returns the httpx.Client / httpx.AsyncClient.
    For Google GenAI, returns the google.genai.Client / its aio attribute.
    """
    global _cached_llm_instance
    if _cached_llm_instance is None:
        return None, None

    llm = _cached_llm_instance
    # OpenAI-compatible (ChatOpenAI / ReasoningChatOpenAI) - uses http_client/http_async_client
    if hasattr(llm, "http_client") and hasattr(llm, "http_async_client"):
        return getattr(llm, "http_client", None), getattr(llm, "http_async_client", None)
    # Google GenAI (ChatGoogleGenerativeAI)
    if hasattr(llm, "client"):
        client = getattr(llm, "client", None)
        if client is not None:
            return client, getattr(client, "aio", None)
    return None, None


def cancel_current_stream() -> bool:
    """Cancel the in-flight LLM streaming request by closing its HTTP client(s).

    Returns True if a client was closed, False if no active stream was tracked.
    Callers should follow up with reset_agent_cache() to ensure a fresh LLM
    with new HTTP clients for the next request.
    """
    global _current_llm_client, _current_llm_async_client
    closed = False
    if _current_llm_client is not None:
        try:
            # OpenAI-compatible: httpx.Client has close()
            # Google GenAI: google.genai.Client has close()
            _current_llm_client.close()
            closed = True
        except Exception:
            pass
    if _current_llm_async_client is not None:
        try:
            # OpenAI-compatible: httpx.AsyncClient has aclose()
            # Google GenAI: client.aio has aclose()
            import asyncio

            asyncio.run(_current_llm_async_client.aclose())
            closed = True
        except Exception:
            pass
    _clear_current_llm_clients()
    return closed


def clear_cached_llm() -> None:
    """Clear the cached LLM instance reference.

    Called by reset_agent_cache() to ensure the next request creates a fresh LLM.
    """
    global _cached_llm_instance
    _cached_llm_instance = None


if ChatOpenAI is not None:

    class ReasoningChatOpenAI(ChatOpenAI):  # type: ignore[misc,valid-type]
        """ChatOpenAI that keeps provider-specific reasoning text.

        langchain-openai targets the official OpenAI schema and deliberately
        drops non-standard fields such as ``reasoning_content``. Servers like
        LM Studio, vLLM, DeepSeek and Ollama send their thinking there, so we
        lift it into ``additional_kwargs`` where the chat panel can render it.
        """

        def _convert_chunk_to_generation_chunk(
            self, chunk, default_chunk_class, base_generation_info
        ):
            generation_chunk = super()._convert_chunk_to_generation_chunk(
                chunk, default_chunk_class, base_generation_info
            )
            if generation_chunk is None:
                return None

            choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices") or []
            if not choices:
                return generation_chunk

            delta = choices[0].get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                generation_chunk.message.additional_kwargs["reasoning_content"] = reasoning
            return generation_chunk

else:  # pragma: no cover - optional dependency
    ReasoningChatOpenAI = None  # type: ignore[assignment]


Provider = Literal["google", "openai"]


@dataclass(frozen=True)
class ProviderPreset:
    """Pre-filled configuration for a known provider."""
    name: str
    provider: Provider
    base_url: str | None
    api_key: str
    default_model: str
    description: str


# =============================================================================
# PRESET CONFIGURATIONS
# =============================================================================

PRESETS: dict[str, ProviderPreset] = {
    "google": ProviderPreset(
        name="Google Gemini",
        provider="google",
        base_url=None,
        api_key="",  # User must provide
        default_model="gemini-3.5-flash-lite",
        description="Google's Gemini models (requires API key)",
    ),
    "ollama": ProviderPreset(
        name="Ollama (Local)",
        provider="openai",
        base_url="http://localhost:11434/v1",
        api_key="ollama",  # Dummy key, Ollama ignores it
        default_model="llama3.1",
        description="Local models via Ollama server",
    ),
    "lm_studio": ProviderPreset(
        name="LM Studio (Local)",
        provider="openai",
        base_url="http://localhost:1234/v1",
        api_key="lm-studio",  # Dummy key
        default_model="llama3.2-1b-instruct",
        description="Local models via LM Studio server",
    ),
    "openrouter": ProviderPreset(
        name="OpenRouter",
        provider="openai",
        base_url="https://openrouter.ai/api/v1",
        api_key="",  # User must provide
        default_model="anthropic/claude-3.5-sonnet",
        description="100+ models via OpenRouter (requires API key)",
    ),
    "groq": ProviderPreset(
        name="Groq",
        provider="openai",
        base_url="https://api.groq.com/openai/v1",
        api_key="",  # User must provide
        default_model="llama-3.1-70b-versatile",
        description="Fast inference for open models (requires API key)",
    ),
    "together": ProviderPreset(
        name="Together AI",
        provider="openai",
        base_url="https://api.together.xyz/v1",
        api_key="",  # User must provide
        default_model="meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        description="Open-source model hosting (requires API key)",
    ),
    "custom": ProviderPreset(
        name="Custom OpenAI-Compatible",
        provider="openai",
        base_url="",  # User must provide
        api_key="",   # User must provide
        default_model="",
        description="Any OpenAI-compatible endpoint",
    ),
}


def get_preset(key: str) -> ProviderPreset | None:
    """Get a preset by key (case-insensitive)."""
    return PRESETS.get(key.lower())


def list_preset_items() -> list[tuple[str, ProviderPreset]]:
    """Return ``(key, preset)`` pairs in a consistent order.

    The UI needs the key, not just the display name: ``get_preset`` looks
    presets up by key, so storing ``preset.name.lower()`` in a combo box made
    lookups fail for every preset whose name differs from its key.
    """
    order = ["google", "ollama", "lm_studio", "openrouter", "groq", "together", "custom"]
    return [(key, PRESETS[key]) for key in order if key in PRESETS]


def list_presets() -> list[ProviderPreset]:
    """Return all presets in a consistent order."""
    return [preset for _, preset in list_preset_items()]


# =============================================================================
# LLM FACTORY
# =============================================================================

class LLMFactoryError(RuntimeError):
    """Raised when LLM creation fails."""


def create_llm(
    provider: Provider,
    model: str,
    base_url: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.0,
    **kwargs,
) -> BaseChatModel:
    """Create a LangChain chat model for the given provider.

    Args:
        provider: "google" or "openai"
        model: Model name (e.g., "gemini-3.5-flash-lite", "llama3.1")
        base_url: Base URL for OpenAI-compatible APIs (required for "openai" provider)
        api_key: API key (required for "google", optional for "openai" if server doesn't need it)
        temperature: Sampling temperature (default 0.0 for deterministic output)
        **kwargs: Additional arguments passed to the model constructor

    Returns:
        A BaseChatModel instance ready for use with LangChain/LangGraph.

    Raises:
        LLMFactoryError: If provider is unknown, required dependencies missing,
            or required parameters are not provided.
    """
    if provider == "google":
        if ChatGoogleGenerativeAI is None:
            raise LLMFactoryError(
                "langchain-google-genai not installed. Run: pip install langchain-google-genai"
            )
        if not api_key:
            raise LLMFactoryError("Google provider requires an API key (GOOGLE_API_KEY)")
        return ChatGoogleGenerativeAI(
            model=model,
            temperature=temperature,
            google_api_key=api_key,
            **kwargs,
        )

    elif provider == "openai":
        if ChatOpenAI is None:
            raise LLMFactoryError(
                "langchain-openai not installed. Run: pip install langchain-openai"
            )
        if not base_url:
            raise LLMFactoryError("OpenAI-compatible provider requires base_url")
        # Create dedicated HTTP clients for this LLM instance so we can cancel
        # the in-flight request by closing them.
        http_client = httpx.Client()
        http_async_client = httpx.AsyncClient()
        _set_current_llm_clients(sync_client=http_client, async_client=http_async_client)

        return ReasoningChatOpenAI(
            model=model,
            temperature=temperature,
            base_url=base_url,
            api_key=api_key or "dummy",
            http_client=http_client,
            http_async_client=http_async_client,
            **kwargs,
        )

    else:
        raise LLMFactoryError(f"Unknown provider: {provider}")


def create_llm_from_config(config: "ChatConfig") -> BaseChatModel:
    """Convenience: create LLM from a ChatConfig object."""
    llm = create_llm(
        provider=config.provider,
        model=config.system_model,
        base_url=config.base_url,
        api_key=config.api_key,
    )
    _register_cached_llm(llm)
    return llm