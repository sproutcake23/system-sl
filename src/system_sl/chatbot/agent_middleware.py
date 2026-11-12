"""Middleware that keeps the model's prompt current.

The static system prompt gets the CONTEXT block appended, so the model knows
the date, the Player and their tasks without a tool call. The block is
rebuilt for every model call and never written to the checkpoint: anything
put into a user message stays in the conversation history forever, and this
block changes every turn.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import wrap_model_call
from langchain_core.messages import SystemMessage

logger = logging.getLogger(__name__)


def make_context_middleware():
    """Return the middleware that injects the context block."""

    @wrap_model_call
    def context_middleware(request, handler):
        from system_sl.chatbot.dynamic_context import build_context_block

        static_prompt = request.system_prompt or ""
        context_block = build_context_block()
        system_text = f"{static_prompt}\n\n{context_block}" if static_prompt else context_block

        return handler(
            request.override(system_message=SystemMessage(content=system_text))
        )

    return context_middleware
