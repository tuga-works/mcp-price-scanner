"""Logging to stdout (journald under systemd) with redaction.

Every record passes through :class:`RedactingFilter` before it is written, so
the bearer token, cookie and authorization headers, and the browser profile
path never reach the log even if some library tries to print them.
"""

from __future__ import annotations

import logging
import re
import sys

_HEADER_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[^\s,;'\"]+"),
    re.compile(r"(?i)((?:set-)?cookie\s*[:=]\s*)[^\n]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
]

REDACTED = "[REDACTED]"


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = [s for s in (secrets or []) if s and len(s) >= 4]

    def add_secret(self, value: str | None) -> None:
        if value and len(value) >= 4 and value not in self._secrets:
            self._secrets.append(value)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        for pattern in _HEADER_PATTERNS:
            text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record
            return True
        redacted = self.redact(message)
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        record.msg = redacted
        record.args = None
        return True


def setup_logging(level: str = "INFO", secrets: list[str] | None = None) -> RedactingFilter:
    """Configure the root logger once and return the redaction filter."""
    redactor = RedactingFilter(secrets)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(redactor)
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    # Libraries that may log request details stay at WARNING or above.
    for noisy in ("httpx", "httpcore", "curl_cffi", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return redactor
