"""Token accounting and cost estimates for one model reply.

Providers report usage in different places: LangChain exposes a normalised
``usage_metadata`` mapping on the final AI message (and, for some providers, on
the last streamed chunk), while OpenAI-compatible servers often only fill
``response_metadata["token_usage"]``. This module turns either shape into one
:class:`TokenUsage`, and prices it with the per-model rates the settings dialog
already caches from the provider.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Provider prices are per one million tokens (see model_fetcher.ModelInfo).
_TOKENS_PER_PRICE_UNIT = 1_000_000


@dataclass(frozen=True)
class TokenUsage:
    """Token counts for a single reply."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    @property
    def is_empty(self) -> bool:
        return not (self.input_tokens or self.output_tokens or self.total_tokens)

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def usage_from_metadata(metadata: Any) -> Optional[TokenUsage]:
    """Read a LangChain ``usage_metadata`` mapping."""
    if not isinstance(metadata, dict):
        return None
    input_tokens = _as_int(metadata.get("input_tokens"))
    output_tokens = _as_int(metadata.get("output_tokens"))
    total_tokens = _as_int(metadata.get("total_tokens")) or (
        input_tokens + output_tokens
    )
    if not (input_tokens or output_tokens or total_tokens):
        return None
    return TokenUsage(input_tokens, output_tokens, total_tokens)


def usage_from_message(message: Any) -> Optional[TokenUsage]:
    """Return the usage a message carries, in either supported shape."""
    usage = usage_from_metadata(getattr(message, "usage_metadata", None))
    if usage is not None:
        return usage

    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        usage = usage_from_metadata(response_metadata.get("token_usage"))
        if usage is not None:
            return usage
        # Some providers nest it under ``usage`` or ``tokenUsage``.
        for key in ("usage", "tokenUsage"):
            usage = usage_from_metadata(response_metadata.get(key))
            if usage is not None:
                return usage
    return None


def merge_usage(current: Optional[TokenUsage], incoming: Optional[TokenUsage]) -> Optional[TokenUsage]:
    """Add usage reported by another chunk or model call in the same turn.

    LangChain's streamed usage is additive: ``AIMessageChunk.__add__`` sums
    ``usage_metadata``. Gemini reports a delta on every chunk, Anthropic
    splits input (first chunk) from output (last chunk), OpenAI-compatible
    APIs send one usage chunk at the end, and a turn with tool calls makes
    several model calls. Summing is right for all of them; keeping the
    largest report under-counted every one except the last shape.
    """
    if incoming is None:
        return current
    if current is None:
        return incoming
    return TokenUsage(
        current.input_tokens + incoming.input_tokens,
        current.output_tokens + incoming.output_tokens,
        current.total_tokens + incoming.total_tokens,
    )


def usage_from_chunks(chunks: Any) -> Optional[TokenUsage]:
    """Return the usage carried by an iterable of streamed chunks."""
    usage: Optional[TokenUsage] = None
    for chunk in chunks:
        usage = merge_usage(usage, usage_from_message(chunk))
    return usage


def model_prices(model_name: str) -> Optional[tuple[float, float]]:
    """Return ``(prompt_price, completion_price)`` per 1M tokens, if cached.

    The settings dialog caches the provider's model list (with prices) in
    ``llm_config.json``. A missing cache is not an error: local models and
    models that were never fetched simply have no price.
    """
    if not model_name:
        return None
    try:
        from system_sl.chatbot.config import get_config_path

        with open(get_config_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    cache = data.get("model_cache")
    if not isinstance(cache, dict):
        return None
    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        for model in entry.get("models") or []:
            if not isinstance(model, dict) or model.get("id") != model_name:
                continue
            try:
                prompt = float(model.get("prompt_price") or 0.0)
                completion = float(model.get("completion_price") or 0.0)
            except (TypeError, ValueError):
                continue
            return prompt, completion
    return None


def estimate_cost(usage: Optional[TokenUsage], model_name: str) -> Optional[float]:
    """Estimate the cost of a reply in the configured currency.

    Returns ``None`` when the model has no cached price, so callers can tell
    "free" (0.0) from "unknown".
    """
    if usage is None or usage.is_empty:
        return None
    prices = model_prices(model_name)
    if prices is None:
        return None
    prompt_price, completion_price = prices
    return (
        usage.input_tokens * prompt_price
        + usage.output_tokens * completion_price
    ) / _TOKENS_PER_PRICE_UNIT


def _format_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}k".replace(".0k", "k")
    return f"{value / 1_000_000:.1f}M".replace(".0M", "M")


def format_context(used: int, window: int, estimated: bool = False) -> str:
    """How full the conversation's context window is, e.g. "Context 12k / 128k (9%)".

    ``used`` is what the model last had to read (the latest prompt plus the
    reply it wrote), so it grows over the conversation, unlike a single
    reply's input/output counts. ``estimated`` marks a local approximation
    (an opened conversation before any reply reported real numbers).
    """
    if used <= 0 or window <= 0:
        return ""
    percent = min(999, round(used * 100 / window))
    approx = "~" if estimated else ""
    return f"Context {approx}{_format_count(used)} / {_format_count(window)} ({percent}%)"


def format_cost(cost: float) -> str:
    if cost == 0.0:
        return "free"
    if cost < 0.01:
        return f"~${cost:.4f}"
    return f"~${cost:.2f}"


def format_conversation_total(totals: Optional[dict]) -> str:
    """Tokens and estimated cost spent over a whole conversation.

    ``totals`` is the ``usage`` entry kept in the conversation index. Replies
    from a model with no known price are counted but cannot be priced; the
    cost is then marked as a lower bound ("≥").
    """
    if not isinstance(totals, dict):
        return ""
    tokens = int(totals.get("input_tokens") or 0) + int(totals.get("output_tokens") or 0)
    if tokens <= 0:
        return ""
    text = f"conversation {_format_count(tokens)} tokens"
    replies = int(totals.get("replies") or 0)
    unpriced = int(totals.get("unpriced_replies") or 0)
    if replies and unpriced >= replies:
        return text  # nothing could be priced; don't show $0
    cost = float(totals.get("cost") or 0.0)
    if unpriced and cost == 0.0:
        return text  # free + unknown: the total isn't known
    cost_text = format_cost(cost)
    if unpriced:
        cost_text = "≥" + cost_text.lstrip("~")
    return f"{text} {cost_text}"


def format_usage(usage: Optional[TokenUsage], model_name: str = "") -> str:
    """A short, human-readable line for the transcript."""
    if usage is None or usage.is_empty:
        return ""

    parts = [f"{usage.total_tokens or (usage.input_tokens + usage.output_tokens):,} tokens"]
    if usage.input_tokens or usage.output_tokens:
        parts[0] += (
            f" ({_format_count(usage.input_tokens)} in /"
            f" {_format_count(usage.output_tokens)} out)"
        )

    cost = estimate_cost(usage, model_name)
    if cost is not None:
        # An unknown price (None) is not the same as a free model.
        parts.append(format_cost(cost))
    return " · ".join(parts)