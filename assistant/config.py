"""Settings: read from the local .env file or, in a container, from a given mapping.

The two sources are never mixed, and nothing is ever written to os.environ, so
secrets do not leak into child processes by accident.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional, Union

from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_DIR / ".env"

SETTING_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "OWNER_ID",
    "AUTO_SEND",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_BIN",
    "CLAUDE_MODEL",
    "CLAUDE_EFFORT",
    "CLAUDE_TIMEOUT_SECONDS",
    "LIMIT_COOLDOWN_MINUTES",
    "OWNER_TAKEOVER_MINUTES",
    "HISTORY_LIMIT",
    "DATA_DIR",
    "PROFILE_PATH",
)

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

AUTH_LOCAL_LOGIN = "this machine's claude login"
AUTH_ENV_FILE = "subscription token from .env"
AUTH_ENVIRONMENT = "subscription token from environment variables"

_BOOL_VALUES = {
    "true": True, "1": True, "yes": True, "on": True,
    "false": False, "0": False, "no": False, "off": False,
}
_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")


class ConfigError(Exception):
    """Invalid or missing configuration. Messages never contain secret values."""


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    owner_id: int
    auto_send: bool
    claude_bin: str
    claude_model: Optional[str]
    claude_effort: str
    claude_timeout: float
    limit_cooldown_minutes: int
    owner_takeover_minutes: int
    history_limit: int
    data_dir: Path
    profile_path: Path
    claude_oauth_token: Optional[str] = field(default=None, repr=False)
    from_environment: bool = False

    @property
    def claude_auth_mode(self) -> str:
        if not self.claude_oauth_token:
            return AUTH_LOCAL_LOGIN
        return AUTH_ENVIRONMENT if self.from_environment else AUTH_ENV_FILE


def default_claude_bin() -> str:
    found = shutil.which("claude")
    if found:
        return found
    name = "claude.exe" if os.name == "nt" else "claude"
    return str(Path.home() / ".local" / "bin" / name)


def load_settings(
    env_file: Union[str, os.PathLike] = DEFAULT_ENV_FILE,
    *,
    require_telegram: bool = True,
    overrides: Optional[Mapping[str, str]] = None,
    source: Optional[Mapping[str, str]] = None,
) -> Settings:
    env_path = Path(env_file)
    base_dir = env_path.resolve().parent

    values: Dict[str, str] = {}
    if source is not None:
        where = "the environment variables"
        values.update({k: v for k, v in source.items() if v is not None})
    else:
        where = str(env_path)
        if not env_path.is_file():
            raise ConfigError(
                f"{env_path} not found. Create it with: cp .env.example .env && chmod 600 .env"
            )
        values.update({k: v for k, v in dotenv_values(env_path).items() if v is not None})
    if overrides:
        values.update(overrides)

    def get(key: str) -> str:
        return (values.get(key) or "").strip()

    def fail(message: str) -> ConfigError:
        return ConfigError(f"{message} (check {where})")

    bot_token = get("TELEGRAM_BOT_TOKEN")
    owner_raw = get("OWNER_ID")
    owner_id = 0
    if require_telegram:
        if not bot_token:
            raise fail("TELEGRAM_BOT_TOKEN is not set")
        if not _BOT_TOKEN_RE.match(bot_token):
            raise fail("TELEGRAM_BOT_TOKEN does not look like a bot token (<digits>:<text>)")
        if not owner_raw:
            raise fail("OWNER_ID is not set")
        if not owner_raw.isdigit():
            raise fail("OWNER_ID must be your numeric Telegram user id")
        owner_id = int(owner_raw)
    elif owner_raw.isdigit():
        owner_id = int(owner_raw)

    auto_raw = get("AUTO_SEND").lower() or "false"
    if auto_raw not in _BOOL_VALUES:
        raise fail("AUTO_SEND must be true or false")
    auto_send = _BOOL_VALUES[auto_raw]

    oauth_token = get("CLAUDE_CODE_OAUTH_TOKEN") or None
    if oauth_token:
        if oauth_token.startswith("sk-ant-api"):
            raise fail(
                "CLAUDE_CODE_OAUTH_TOKEN is an Anthropic API key, not a subscription token. "
                "This bot never uses API keys: run `claude setup-token` and use its sk-ant-oat... token"
            )
        problems = []
        if not oauth_token.startswith("sk-ant-oat"):
            problems.append("it does not start with sk-ant-oat")
        spaces = [i for i, ch in enumerate(oauth_token) if ch.isspace() or not ch.isprintable()]
        if spaces:
            positions = ", ".join(str(i) for i in spaces[:5])
            problems.append(
                f"it contains {len(spaces)} space/invisible character(s) at position(s) {positions} "
                f"of {len(oauth_token)}"
            )
        if problems:
            raise fail(
                "CLAUDE_CODE_OAUTH_TOKEN is not a valid `claude setup-token` token: "
                + "; ".join(problems)
                + " (it must be one line with no spaces)"
            )

    effort = get("CLAUDE_EFFORT").lower() or "low"
    if effort not in EFFORT_LEVELS:
        raise fail(f"CLAUDE_EFFORT must be one of: {', '.join(EFFORT_LEVELS)}")

    def number(key: str, default: int, minimum: int) -> int:
        raw = get(key)
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise fail(f"{key} must be a whole number") from None
        if value < minimum:
            raise fail(f"{key} must be at least {minimum}")
        return value

    def path(key: str, default: str) -> Path:
        raw = get(key)
        result = Path(raw).expanduser() if raw else Path(default)
        return result if result.is_absolute() else base_dir / result

    return Settings(
        bot_token=bot_token,
        owner_id=owner_id,
        auto_send=auto_send,
        claude_bin=get("CLAUDE_BIN") or default_claude_bin(),
        claude_model=get("CLAUDE_MODEL") or None,
        claude_effort=effort,
        claude_timeout=float(number("CLAUDE_TIMEOUT_SECONDS", 120, 1)),
        limit_cooldown_minutes=number("LIMIT_COOLDOWN_MINUTES", 30, 1),
        owner_takeover_minutes=number("OWNER_TAKEOVER_MINUTES", 15, 0),
        history_limit=number("HISTORY_LIMIT", 30, 1),
        data_dir=path("DATA_DIR", "data"),
        profile_path=path("PROFILE_PATH", "profile.md"),
        claude_oauth_token=oauth_token,
        from_environment=source is not None,
    )


def env_file_warning(path: Union[str, os.PathLike]) -> Optional[str]:
    """Return a warning if the .env file is readable by group or others (POSIX only)."""
    if os.name != "posix":
        return None
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None
    if mode & 0o077:
        return f"{path} is accessible by other users (mode {mode:o}). Fix it with: chmod 600 {path}"
    return None
