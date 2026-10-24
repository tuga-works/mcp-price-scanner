import logging

import pytest

from price_scanner.auth import BearerAuthMiddleware
from price_scanner.logging_setup import REDACTED, RedactingFilter
from price_scanner.settings import Settings, SettingsError

TOKEN = "t" * 40  # test-only dummy value


async def call(headers):
    sent = []
    reached = []

    async def app(scope, receive, send):
        reached.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    mw = BearerAuthMiddleware(app, token=TOKEN)
    await mw({"type": "http", "headers": headers, "path": "/mcp"}, receive, send)
    return sent, reached


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"authorization", b"Bearer wrong")],
        [(b"authorization", b"Basic " + TOKEN.encode())],
        [(b"authorization", b"Bearer ")],
        [(b"authorization", b"Bearer " + TOKEN.encode() + b"x")],
    ],
)
async def test_rejects_with_bare_401(headers):
    sent, reached = await call(headers)
    assert not reached
    assert sent[0]["status"] == 401
    assert sent[1]["body"] == b""


async def test_accepts_correct_token():
    sent, reached = await call([(b"authorization", b"Bearer " + TOKEN.encode())])
    assert reached and sent[0]["status"] == 200


def test_redaction():
    f = RedactingFilter([TOKEN])
    assert TOKEN not in f.redact(f"token is {TOKEN}")
    assert "abc123secret" not in f.redact("Authorization: Bearer abc123secret")
    assert "sessionid=1" not in f.redact("Set-Cookie: sessionid=1; Path=/")
    assert "c=2" not in f.redact("cookie: c=2")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "value %s", (TOKEN,), None)
    f.filter(record)
    assert record.getMessage() == f"value {REDACTED}"


def test_settings_refuse_placeholder_and_short_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("PRICE_SCANNER_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("PRICE_SCANNER_TOKEN", "change-me-at-install")
    with pytest.raises(SettingsError):
        Settings.from_env()
    monkeypatch.setenv("PRICE_SCANNER_TOKEN", "short")
    with pytest.raises(SettingsError):
        Settings.from_env()
    monkeypatch.setenv("PRICE_SCANNER_TOKEN", TOKEN)
    settings = Settings.from_env()
    assert settings.host == "127.0.0.1"
    assert TOKEN not in repr(settings)
