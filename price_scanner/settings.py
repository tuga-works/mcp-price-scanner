"""Runtime settings, read from environment variables at startup.

Secrets (the bearer token, the optional browser profile path) only ever come
from the environment. A local ``.env`` file is read as a convenience for
manual runs; values already present in the environment win. Under systemd the
unit's ``EnvironmentFile=`` supplies the same variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PLACEHOLDER_TOKENS = {"", "change-me-at-install", "change-me", "changeme"}
MIN_TOKEN_LENGTH = 32


class SettingsError(RuntimeError):
    """Raised when the environment does not allow a safe start."""


def load_env_file(path: Path) -> None:
    """Load ``KEY=VALUE`` lines into ``os.environ`` without overriding.

    Minimal on purpose: comments, blank lines, optional ``export`` prefix and
    matching single or double quotes. Values are never logged.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8765
    config_path: Path = Path("config/sources.yaml")
    profile_dir: Path | None = field(default=None, repr=False)
    headless: bool = True
    log_level: str = "INFO"
    cache_ttl_seconds: int = 300
    allowed_hosts: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, *, require_token: bool = True) -> "Settings":
        env_file = Path(os.environ.get("PRICE_SCANNER_ENV_FILE", ".env"))
        load_env_file(env_file)

        token = os.environ.get("PRICE_SCANNER_TOKEN", "")
        if require_token:
            if token.strip() in PLACEHOLDER_TOKENS:
                raise SettingsError(
                    "PRICE_SCANNER_TOKEN is missing or still the placeholder; "
                    "generate one, e.g. `openssl rand -hex 32`."
                )
            if len(token) < MIN_TOKEN_LENGTH:
                raise SettingsError(
                    f"PRICE_SCANNER_TOKEN is shorter than {MIN_TOKEN_LENGTH} characters."
                )

        profile = os.environ.get("PRICE_SCANNER_PROFILE_DIR", "").strip()
        allowed = os.environ.get("PRICE_SCANNER_ALLOWED_HOSTS", "")
        return cls(
            token=token,
            host=os.environ.get("PRICE_SCANNER_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=int(os.environ.get("PRICE_SCANNER_PORT", "8765")),
            config_path=Path(os.environ.get("PRICE_SCANNER_CONFIG", "config/sources.yaml")),
            profile_dir=Path(profile).expanduser() if profile else None,
            headless=_bool(os.environ.get("PRICE_SCANNER_HEADLESS"), True),
            log_level=os.environ.get("PRICE_SCANNER_LOG_LEVEL", "INFO").upper(),
            cache_ttl_seconds=int(os.environ.get("PRICE_SCANNER_CACHE_TTL_SECONDS", "300")),
            allowed_hosts=tuple(h.strip() for h in allowed.split(",") if h.strip()),
        )
