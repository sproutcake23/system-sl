"""Quoted messages in a Player's turn.

A quote is a piece of an earlier message the Player wants the System to talk
about. It travels inside the Player's next message as a delimited block::

    <quote from="assistant">the part being quoted</quote>

    what the Player actually typed

The model therefore sees both what was quoted and who said it, while the
transcript can render the same text as a block-quote without keeping a second
copy of the conversation. Delimiters are deliberately plain text, like the
``<document>`` blocks attachments use: every provider understands them.

This module has no Qt dependency, so the formatting rules are testable without
a GUI.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, replace
from typing import Iterable

# A literal closing tag inside the quoted text would end the block early and
# spill the rest of the quote into the Player's own words. Inserting a
# zero-width space breaks the tag while staying invisible in the transcript.
_ZERO_WIDTH = "\u200b"

_QUOTE_TAG_RE = re.compile(
    r'<quote\s+from="(?P<speaker>[^"]*)"\s*>(?P<text>.*?)</quote>',
    re.DOTALL | re.IGNORECASE,
)

# Shown in place of the text a long quote lost. It is part of the quote block,
# so the model reads it too.
TRUNCATION_MARKER = "…[quote truncated: {cut:,} of {total:,} characters]"

# Share of the attachment budget a turn's quotes may use. Quotes travel in the
# same user message as the attachments, so they must not claim a full budget
# of their own.
QUOTE_BUDGET_SHARE = 0.5
MIN_QUOTE_BUDGET_CHARS = 1_000


@dataclass(frozen=True)
class Quote:
    """One quoted excerpt, plus who said it and where it came from."""

    speaker: str
    text: str
    message_id: str = ""

    @property
    def label(self) -> str:
        """Human-readable speaker name for the transcript and the chips."""
        return speaker_label(self.speaker)


def speaker_label(speaker: str) -> str:
    """Turn a stored role into the name shown next to a quote."""
    key = (speaker or "").strip().lower()
    if key in ("assistant", "ai", "system", "bot"):
        return "System"
    if key in ("player", "user", "human"):
        return "Player"
    return (speaker or "Unknown").replace("_", " ").strip().title()


def speaker_for_role(role: str) -> str:
    """Map a transcript role to the ``from`` attribute used in a quote."""
    return "assistant" if (role or "").lower() in ("assistant", "ai") else "player"


_CLOSING_QUOTE_RE = re.compile(r"<(?=\s*/\s*quote)", re.IGNORECASE)


def _neutralise_tags(text: str) -> str:
    """Stop quoted text from ending (or nesting) its own quote block."""
    # Case-insensitive, like the parser: "</QUOTE>" must not close the block.
    return _CLOSING_QUOTE_RE.sub(f"<{_ZERO_WIDTH}", text or "")


def format_quote(quote: Quote) -> str:
    """Render one quote as a delimited block."""
    speaker = html.escape((quote.speaker or "assistant").strip(), quote=True)
    return f'<quote from="{speaker}">{_neutralise_tags(quote.text)}</quote>'


def format_quotes(quotes: Iterable[Quote], body: str) -> str:
    """Put the quotes above the Player's own text, in the order they were added."""
    blocks = [format_quote(quote) for quote in quotes if (quote.text or "").strip()]
    body = (body or "").strip()
    if not blocks:
        return body
    return "\n".join(blocks) + "\n\n" + body


def parse_quotes(text: str) -> tuple[list[Quote], str]:
    """Split a stored message into its quotes and the Player's own text.

    Called when a conversation is reopened: the quotes are only stored as the
    delimited blocks the model was sent, so this is what turns them back into
    styled block-quotes in the transcript.
    """
    raw = text or ""
    if "<quote" not in raw.lower():
        return [], raw.strip()

    quotes: list[Quote] = []
    for match in _QUOTE_TAG_RE.finditer(raw):
        quoted = match.group("text").replace(_ZERO_WIDTH, "").strip()
        if not quoted:
            continue
        speaker = html.unescape(match.group("speaker")).strip() or "assistant"
        quotes.append(Quote(speaker=speaker, text=quoted))

    body = _QUOTE_TAG_RE.sub("", raw).strip()
    return quotes, body


def quote_budget_chars(context_tokens: int) -> int:
    """Return how many characters of quotes one message may carry."""
    from system_sl.chatbot.context_window import attachment_budget_chars

    budget = attachment_budget_chars(context_tokens)
    return max(MIN_QUOTE_BUDGET_CHARS, int(budget * QUOTE_BUDGET_SHARE))


def fit_quotes(
    quotes: Iterable[Quote],
    budget_chars: int,
) -> tuple[list[Quote], list[str]]:
    """Cut a turn's quotes down to a shared character budget.

    The budget is split between the quotes the same way the attachments share
    theirs, so quoting a second message does not silently double the size of
    the turn. Every quote that lost text says so, both to the Player (the
    returned notices) and to the model (the marker inside the block).

    Returns the fitted quotes and one notice per truncated quote.
    """
    from system_sl.chatbot.documents import allocate_budget

    items = [quote for quote in quotes if (quote.text or "").strip()]
    if not items:
        return [], []

    lengths = [len(quote.text) for quote in items]
    allocation = allocate_budget(lengths, max(0, int(budget_chars)))

    fitted: list[Quote] = []
    notices: list[str] = []
    for quote, allowed in zip(items, allocation):
        total = len(quote.text)
        if allowed >= total:
            fitted.append(quote)
            continue
        cut = total - max(0, allowed)
        marker = TRUNCATION_MARKER.format(cut=cut, total=total)
        kept = quote.text[: max(0, allowed)].rstrip()
        fitted.append(replace(quote, text=f"{kept}\n{marker}" if kept else marker))
        notices.append(
            f"{quote.label}: quote truncated, {cut:,} of {total:,} characters not sent"
        )
    return fitted, notices
