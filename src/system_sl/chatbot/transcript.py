"""The message model behind the chat transcript.

The chat panel used to treat its ``QTextBrowser`` as the transcript: whatever
had been inserted into the document *was* the conversation, as far as the UI
knew. That makes per-message actions (copy, quote, and later regenerate or
edit-and-resend) guesswork, because the document has no notion of a message.

This module holds the other half: a plain list of :class:`ChatMessage` objects
built from the checkpoint history when a conversation is opened and kept up to
date while a reply streams. The browser is a view of this list. Message ids are
stable for the lifetime of a transcript, and the view tags each message's
blocks with its id (see ``chat_panel._MessageBlockData``) so a click or a
selection maps back to the message it came from.

Nothing here imports Qt: the model is used and tested without a GUI.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Iterable, Optional

from system_sl.chatbot.memory import is_app_note, note_body
from system_sl.chatbot.quotes import Quote, parse_quotes

# Roles. "note" is the dim, centred line the app writes itself (a task
# confirmation, a model switch); it is never something a participant said.
USER = "user"
ASSISTANT = "assistant"
NOTE = "note"
ALERT = "alert"
CONFIRMATION = "confirmation"

# Older turns were stored with a "[Today's date: …]" prefix. The date now
# comes from the CONTEXT block, but old conversations still carry the prefix
# and the transcript should not show it.
_DATE_PREFIX_RE = re.compile(r"^\s*\[Today's date:[^\]]*\]\s*")


def strip_date_prefix(text: str) -> str:
    """Drop the "[Today's date: …]" line older turns were stored with."""
    if not text:
        return text
    return _DATE_PREFIX_RE.sub("", text, count=1)


# Attached documents as stored in a Player message: a <document name="…">
# block (full or compacted), or a "[uploaded document: …]" line when the file
# could not be read.
_DOCUMENT_BLOCK_RE = re.compile(
    r'<document\s+name="(?P<name>[^"]*)"[^>]*>.*?</document>\s*', re.DOTALL | re.IGNORECASE
)
_FAILED_DOCUMENT_RE = re.compile(
    r"^\[uploaded document: (?P<name>[^\]\n]*?)(?: — [^\]\n]*)?\]\s*$\n?", re.MULTILINE
)


def split_documents(text: str) -> tuple[list[str], str]:
    """Split a stored Player message into its documents' names and the rest.

    The documents are the model's copy of the attachments; on screen they are
    shown as attachment names, not as the raw (often compacted) text.
    """
    names: list[str] = []

    def _take(match: re.Match) -> str:
        names.append(html.unescape(match.group("name")).strip())
        return ""

    rest = _DOCUMENT_BLOCK_RE.sub(_take, text or "")
    rest = _FAILED_DOCUMENT_RE.sub(_take, rest)
    return [n for n in names if n], rest.strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ChatMessage:
    """One message as the transcript knows it."""

    id: str
    role: str
    text: str
    timestamp: str = ""
    # The Player's text without the quote blocks that precede it.
    body: str = ""
    quotes: list[Quote] = field(default_factory=list)
    # Paths of the files attached to a Player message, for the bubble preview.
    attachments: list[str] = field(default_factory=list)
    # Names of documents already embedded in ``text`` (a stored message
    # reopened from history), shown like attachments but not re-read.
    documents: list[str] = field(default_factory=list)
    # Flags. Display keeps timestamps out of scope for now, but the model
    # carries them so later work does not need another migration.
    stopped: bool = False
    error: str = ""
    streaming: bool = False
    # Id of the stored LangGraph message this one came from (or will be stored
    # as). Regenerate / retry / edit-and-resend rewind the conversation to
    # just before it, so the model's memory matches what is on screen.
    source_id: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = _now()
        if self.role == USER:
            # A stored Player message carries its quotes as delimited blocks;
            # an in-memory one is built the same way before it is shown, so
            # both paths agree on what the body is.
            documents, rest = split_documents(self.text)
            if documents:
                self.documents = documents
            quotes, body = parse_quotes(rest)
            if quotes:
                self.quotes = quotes
            self.body = body
        else:
            self.body = self.text

    def plain_text(self) -> str:
        """The text to put on the clipboard.

        Quotes are excluded on purpose: copying an answer should not paste the
        quoted block above it again.
        """
        return self.body or self.text

    def as_quote(self, text: str | None = None) -> Quote:
        """Quote this message, or a selection from it."""
        return Quote(
            speaker="assistant" if self.role == ASSISTANT else "player",
            text=(text if text is not None else self.plain_text()).strip(),
            message_id=self.id,
        )


class Transcript:
    """The ordered messages of one conversation, as shown on screen."""

    def __init__(self, messages: Optional[Iterable[ChatMessage]] = None) -> None:
        self.messages: list[ChatMessage] = list(messages or [])
        self._next_id = len(self.messages) + 1

    # ----- construction -----

    @classmethod
    def from_history(cls, history: Iterable) -> "Transcript":
        """Build the transcript from a conversation's stored messages.

        Only the Player's messages and the System's answers are messages; tool
        traffic is internal, and app-injected notes are dimmed so they are not
        mistaken for something the Player typed.
        """
        transcript = cls()
        for message in history or []:
            role = getattr(message, "type", "")
            content = getattr(message, "content", "")
            text = content if isinstance(content, str) else _flatten_content(content)
            if not text.strip():
                continue

            source_id = str(getattr(message, "id", "") or "")
            if role == "human":
                text = strip_date_prefix(text)
                if is_app_note(text):
                    transcript.append_note(note_body(text))
                else:
                    transcript.append(USER, text, source_id=source_id)
            elif role == "ai":
                # A turn closed after Stop is recorded as the model's output
                # (see memory.repair_interrupted_turn); show it as a note.
                if is_app_note(text):
                    transcript.append_note(note_body(text))
                else:
                    transcript.append(ASSISTANT, text, source_id=source_id)
        return transcript

    # ----- reading -----

    def __len__(self) -> int:
        return len(self.messages)

    def __iter__(self):
        return iter(self.messages)

    def get(self, message_id: str) -> Optional[ChatMessage]:
        for message in self.messages:
            if message.id == message_id:
                return message
        return None

    def index_of(self, message_id: str) -> int:
        for index, message in enumerate(self.messages):
            if message.id == message_id:
                return index
        return -1

    def after(self, message_id: str) -> list[ChatMessage]:
        """Messages appended after ``message_id`` (empty when it is unknown)."""
        index = self.index_of(message_id)
        if index < 0:
            return []
        return self.messages[index + 1 :]

    def last(self, role: str | None = None) -> Optional[ChatMessage]:
        for message in reversed(self.messages):
            if role is None or message.role == role:
                return message
        return None

    # ----- writing -----

    def append(self, role: str, text: str, **kwargs) -> ChatMessage:
        """Append a message and return it."""
        message = ChatMessage(
            id=f"m{self._next_id}", role=role, text=text, **kwargs
        )
        self._next_id += 1
        self.messages.append(message)
        return message

    def append_note(self, text: str) -> ChatMessage:
        return self.append(NOTE, text)

    def append_alert(self, text: str) -> ChatMessage:
        return self.append(ALERT, text)

    def set_text(self, message_id: str, text: str) -> Optional[ChatMessage]:
        """Replace a message's text in place (streaming answers, final replies)."""
        index = self.index_of(message_id)
        if index < 0:
            return None
        message = replace(self.messages[index], text=text)
        # ``replace`` re-runs __post_init__, which splits a Player message
        # into its documents, quotes and body.
        if message.role != USER:
            message.body = text
        self.messages[index] = message
        return message

    def set_flag(self, message_id: str, **flags) -> Optional[ChatMessage]:
        """Set boolean/str flags (``stopped``, ``error``, ``streaming``)."""
        index = self.index_of(message_id)
        if index < 0:
            return None
        self.messages[index] = replace(self.messages[index], **flags)
        return self.messages[index]


def _flatten_content(content) -> str:
    """Join a rich content payload into plain text."""
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content or "")
