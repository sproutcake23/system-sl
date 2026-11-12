"""Turn a failed reply's raw error into something the Player can act on.

The chat worker reports failures as ``"ExceptionType: message"``. Matching on
loose keywords ("connect", "context", "500") misfiled unrelated errors and let
others (``TypeError: ...``) through raw, so errors are recognised by the
exception's type first, then by an HTTP status code stated as one, and only
then by a few specific phrases.
"""

from __future__ import annotations

import re

AUTH = "Authentication failed. Check the API key in Settings."
RATE = "The provider is rate-limiting requests. Wait a moment and try again."
NETWORK = (
    "Could not reach the model provider. Check your connection and the Base URL "
    "in Settings."
)
TIMEOUT = "The model provider took too long to answer. Try again."
CONTEXT = (
    "This conversation is too long for the model. Start a new chat, or switch to "
    "a model with a larger context window."
)
NOT_FOUND = (
    "The selected model is not available. Pick another one or refresh the model list."
)
FORBIDDEN = "Access denied: this API key may not be allowed to use this model."
SERVER = "The model provider is having problems. Try again later."
BAD_REQUEST = "The model provider rejected the request."
TEMPERATURE = (
    "This model does not accept a temperature setting. Set Temperature to "
    "\"Model default\" in Settings → LLM."
)
UNKNOWN = "Something went wrong while getting the reply."

# Exception class names used by the OpenAI/Anthropic/Google SDKs and httpx.
_BY_TYPE = {
    "AuthenticationError": AUTH,
    "RateLimitError": RATE,
    "APIConnectionError": NETWORK,
    "ConnectError": NETWORK,
    "ConnectionError": NETWORK,
    "ConnectionRefusedError": NETWORK,
    "RemoteProtocolError": NETWORK,
    "APITimeoutError": TIMEOUT,
    "TimeoutException": TIMEOUT,
    "ReadTimeout": TIMEOUT,
    "ConnectTimeout": TIMEOUT,
    "TimeoutError": TIMEOUT,
    "NotFoundError": NOT_FOUND,
    "PermissionDeniedError": FORBIDDEN,
    "InternalServerError": SERVER,
    "ServiceUnavailableError": SERVER,
}

_BY_STATUS = {
    401: AUTH,
    403: FORBIDDEN,
    404: NOT_FOUND,
    408: TIMEOUT,
    413: CONTEXT,
    429: RATE,
}

# "Error code: 401", "status code 429", "HTTP 503", "status: 500"
_STATUS_RE = re.compile(
    r"\b(?:error\s+code|status(?:\s+code)?|http)\s*[:=]?\s*(\d{3})\b", re.IGNORECASE
)
_TEMPERATURE_RE = re.compile(
    r"\btemperature\b.{0,80}\b(?:unsupported|not supported|does not support|invalid|only the default)"
    r"|(?:unsupported|invalid)[^.\n]{0,40}\btemperature\b",
    re.IGNORECASE,
)
_CONTEXT_RE = re.compile(
    r"context[_ ]length[_ ]exceeded|maximum context length|context window"
    r"|too many tokens|prompt is too long|input is too long",
    re.IGNORECASE,
)


def classify_error(raw: str) -> tuple[str, str | None]:
    """Return ``(friendly, detail)`` for a worker error string.

    ``detail`` is the raw text for the "Show details" link; it is None only
    when there is nothing to add (an empty error).
    """
    raw = (raw or "").strip()
    if not raw:
        return UNKNOWN, None

    type_name, _, text = raw.partition(": ")
    type_name = type_name.rsplit(".", 1)[-1] if " " not in type_name else ""

    # Context overflow is reported as a 400 by most providers: check it first.
    if _CONTEXT_RE.search(raw):
        return CONTEXT, raw
    if _TEMPERATURE_RE.search(raw):
        return TEMPERATURE, raw
    if type_name in _BY_TYPE:
        return _BY_TYPE[type_name], raw

    match = _STATUS_RE.search(raw)
    if match:
        status = int(match.group(1))
        if status in _BY_STATUS:
            return _BY_STATUS[status], raw
        if 500 <= status < 600:
            return SERVER, raw
        if status == 400:
            return BAD_REQUEST, raw

    return UNKNOWN, raw
