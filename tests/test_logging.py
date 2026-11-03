import json
import logging
import sys

import pytest

from vara.logging import REDACTED, JsonFormatter, redact

# Fake values shaped like real credentials.
SECRETS = [
    "sk-ant-api03-" + "a" * 40,
    "sk-proj-" + "B" * 40,
    "gsk_" + "c" * 40,
    "xai-" + "d" * 40,
    "grok_" + "e" * 40,
]


@pytest.mark.parametrize("secret", SECRETS)
def test_provider_keys_are_redacted(secret: str) -> None:
    result = redact(f"calling provider with {secret} now")

    assert secret not in result
    assert result == f"calling provider with {REDACTED} now"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Bearer abc123def456", f"Authorization: Bearer {REDACTED}"),
        ("api_key=hunter2hunter2", f"api_key={REDACTED}"),
        ('{"password": "letmein"}', f'{{"password": "{REDACTED}"}}'),
    ],
)
def test_secret_fields_are_redacted(text: str, expected: str) -> None:
    assert redact(text) == expected


def test_ordinary_text_is_unchanged() -> None:
    assert redact("model claude answered in 1.2s") == "model claude answered in 1.2s"


def test_formatter_emits_json_without_secrets() -> None:
    secret = SECRETS[0]
    try:
        raise ValueError(f"bad key {secret}")
    except ValueError:
        record = logging.LogRecord(
            "vara", logging.ERROR, __file__, 1, "using %s", (secret,), sys.exc_info()
        )

    output = JsonFormatter().format(record)

    assert secret not in output
    entry = json.loads(output)
    assert entry["level"] == "ERROR"
    assert entry["message"] == f"using {REDACTED}"
    assert REDACTED in entry["exception"]
