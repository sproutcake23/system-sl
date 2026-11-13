"""Middleware that keeps the model's prompt small and current.

Two jobs, both done on the way to the model and never written to the
checkpoint:

* the static system prompt gets the CONTEXT block appended, so the model
  knows the date, the Player and their tasks without a tool call;
* the history is trimmed to what the current model's context window can
  hold, keeping the newest turns and never splitting a tool call from its
  result.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import wrap_model_call
from langchain_core.messages import SystemMessage

logger = logging.getLogger(__name__)


def make_context_middleware():
    """Return the middleware that injects the context block and trims history."""

    @wrap_model_call
    def context_middleware(request, handler):
        from system_sl.chatbot.context_window import (
            MIN_BUDGET_TOKENS,
            context_window_for_llm,
            count_text_tokens,
            count_tool_schema_tokens,
            history_budget_tokens,
        )
        from system_sl.chatbot.dynamic_context import build_context_block
        from system_sl.chatbot.trimming import trim_history

        static_prompt = request.system_prompt or ""
        context_block = build_context_block()
        system_text = f"{static_prompt}\n\n{context_block}" if static_prompt else context_block

        window = context_window_for_llm(getattr(request, "model", None))
        # The bound tool schemas are sent with every request; leaving them out
        # of the budget lets the history push them past the window.
        schema_tokens = count_tool_schema_tokens(getattr(request, "tools", None) or [])
        budget = (
            history_budget_tokens(window)
            - count_text_tokens(system_text)
            - schema_tokens
        )
        # A tiny window can leave nothing for the history once the system
        # prompt is counted. Keep a floor so the model still sees the turn
        # the Player just sent.
        budget = max(budget, MIN_BUDGET_TOKENS)
        kept, summary = trim_history(request.messages, budget)

        if summary:
            system_text = f"{system_text}\n\n{summary}"

        return handler(
            request.override(
                system_message=SystemMessage(content=system_text),
                messages=kept,
            )
        )

    return context_middleware
