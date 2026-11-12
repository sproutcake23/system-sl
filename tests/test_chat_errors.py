"""The friendly text shown when a reply fails."""

import pytest

from system_sl.chatbot import errors
from system_sl.chatbot.errors import classify_error


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("AuthenticationError: Error code: 401 - {'error': 'bad key'}", errors.AUTH),
        ("RateLimitError: Error code: 429 - slow down", errors.RATE),
        ("APIConnectionError: Connection error.", errors.NETWORK),
        ("ConnectError: [Errno 111] Connection refused", errors.NETWORK),
        ("ReadTimeout: timed out", errors.TIMEOUT),
        ("NotFoundError: Error code: 404 - model 'x' not found", errors.NOT_FOUND),
        ("PermissionDeniedError: Error code: 403", errors.FORBIDDEN),
        ("InternalServerError: Error code: 500", errors.SERVER),
        ("APIStatusError: Error code: 503 - overloaded", errors.SERVER),
        (
            "BadRequestError: Error code: 400 - {'code': 'context_length_exceeded'}",
            errors.CONTEXT,
        ),
        ("BadRequestError: Error code: 400 - invalid tool schema", errors.BAD_REQUEST),
    ],
)
def test_known_errors(raw, expected):
    friendly, detail = classify_error(raw)
    assert friendly == expected
    assert detail == raw


@pytest.mark.parametrize(
    "raw",
    [
        # Used to be misfiled by loose keywords.
        "KeyError: 'context'",
        "ValueError: could not connect the dots in the outline",
        "TypeError: expected 500 items, got 3",
        "AttributeError: 'NoneType' object has no attribute 'not found'",
    ],
)
def test_unrelated_errors_are_not_misfiled_and_never_shown_raw(raw):
    friendly, detail = classify_error(raw)
    assert friendly == errors.UNKNOWN
    assert detail == raw


def test_rejected_temperature_points_to_the_setting():
    raw = (
        "BadRequestError: Error code: 400 - {'error': {'message': \"Unsupported value: "
        "'temperature' does not support 0.0 with this model. Only the default (1) value "
        "is supported.\"}}"
    )
    assert classify_error(raw)[0] == errors.TEMPERATURE


def test_model_default_temperature_is_left_out_of_requests(monkeypatch):
    from system_sl.chatbot import llm_factory

    seen = {}

    class _Spy:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(llm_factory, "ReasoningChatOpenAI", _Spy)
    llm_factory.create_llm("openai", "o3", base_url="http://x", temperature=None)
    assert "temperature" not in seen and "max_tokens" not in seen

    seen.clear()
    llm_factory.create_llm("openai", "m", base_url="http://x", temperature=0.3, max_tokens=99)
    assert seen["temperature"] == 0.3 and seen["max_tokens"] == 99


def test_saved_null_temperature_means_model_default(tmp_path, monkeypatch):
    import json

    from system_sl.chatbot import config as config_mod

    path = tmp_path / config_mod.CONFIG_FILENAME
    path.write_text(json.dumps({"provider": "openai", "base_url": "http://x", "temperature": None}))
    monkeypatch.setattr(config_mod, "get_tasks_file_path", lambda name: str(tmp_path / name))
    assert config_mod.load_config().temperature is None
