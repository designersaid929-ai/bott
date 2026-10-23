"""Runs `claude -p` (Claude Code CLI) once per reply, isolated from the host setup.

The conversation goes to the CLI only through stdin, the system prompt only
through a private temporary file, and the subscription token only through the
child's environment. No shell is ever involved.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .config import AUTH_LOCAL_LOGIN

# Every builtin plugin reported by `claude -p --output-format stream-json --verbose`
# (init event, `plugins` field) in Claude Code 2.1.309.
BUILTIN_PLUGINS = (
    "cc-plugin-agents-md@builtin",
    "cc-plugin-telemetry@builtin",
    "cc-plugin-plugin-authoring@builtin",
)

ISOLATION_SETTINGS: Dict[str, Any] = {
    "disableAllHooks": True,
    "enabledPlugins": {name: False for name in BUILTIN_PLUGINS},
}

# Removed from the child environment (plus everything starting with ANTHROPIC_).
STRIPPED_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    # Set by --bare; it turns off OAuth login.
    "CLAUDE_CODE_SIMPLE",
)

ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
}

INIT_KEYS = ("tools", "mcp_servers", "plugins", "skills", "apiKeySource", "apiProvider", "model")

REDACTED = "[REDACTED]"

_LIMIT_RE = re.compile(
    r"usage limit|hit your (usage )?limit|limit reached|rate[ _-]?limit|out of (extra )?usage|quota",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"not logged in|/login|invalid api key|authentication|unauthori[sz]ed|oauth token",
    re.IGNORECASE,
)
_EPOCH_RE = re.compile(r"\|(\d{9,12})")
_RESET_TEXT_RE = re.compile(r"\bresets\s+([^|\n]+)", re.IGNORECASE)


class ClaudeError(Exception):
    """Claude CLI failure. The message is safe to show to the owner."""


class ClaudeTimeout(ClaudeError):
    pass


class ClaudeAuthError(ClaudeError):
    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class ClaudeLimitError(ClaudeError):
    def __init__(
        self,
        detail: str,
        resets_at: Optional[float] = None,
        reset_text: Optional[str] = None,
    ) -> None:
        if reset_text:
            when = reset_text
        elif resets_at:
            when = datetime.fromtimestamp(resets_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        else:
            when = "at an unknown time"
        super().__init__(f"Claude subscription usage limit reached (resets {when})")
        self.detail = detail
        self.resets_at = resets_at
        self.reset_text = reset_text


def build_env(oauth_token: Optional[str] = None) -> Dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("ANTHROPIC_") and key.upper() not in STRIPPED_ENV_KEYS
    }
    env.update(ISOLATION_ENV)
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


def classify_failure(message: str, status: Optional[int] = None) -> ClaudeError:
    """Turn CLI error text into the matching exception (returned, not raised)."""
    text = (message or "").strip()
    if status == 429 or _LIMIT_RE.search(text):
        epoch = _EPOCH_RE.search(text)
        resets_at = float(epoch.group(1)) if epoch else None
        detail = _EPOCH_RE.sub("", text).strip()
        reset = _RESET_TEXT_RE.search(detail)
        reset_text = reset.group(1).strip() if reset else None
        return ClaudeLimitError(detail, resets_at, reset_text)
    if status in (401, 403) or _AUTH_RE.search(text):
        return ClaudeAuthError(
            "Claude CLI is not logged in: run `claude` on this machine and use /login "
            "(or set CLAUDE_CODE_OAUTH_TOKEN)",
            detail=text[:300],
        )
    return ClaudeError(f"Claude CLI failed: {text[:500]}")


def parse_result(returncode: Optional[int], stdout: str, stderr: str) -> str:
    """Return the reply text from `--output-format json/stream-json` output or raise."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    data: Any = None
    if lines:
        try:
            data = json.loads(lines[-1])
        except ValueError:
            data = None
    if not isinstance(data, dict):
        raise classify_failure(stderr.strip() or stdout.strip() or f"exit code {returncode}")
    if data.get("is_error") or data.get("subtype") != "success":
        parts = [str(data.get("result") or data.get("subtype") or ""), stderr.strip()]
        raise classify_failure(" ".join(p for p in parts if p), data.get("api_error_status"))
    result = str(data.get("result") or "").strip()
    if not result:
        raise ClaudeError("Claude CLI returned an empty reply")
    return result


def _spawn_kwargs() -> Dict[str, Any]:
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


async def _kill_tree(proc: "asyncio.subprocess.Process") -> None:
    if proc.returncode is None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except (ProcessLookupError, PermissionError):
            pass
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


class ClaudeCLI:
    def __init__(
        self,
        claude_bin: str,
        *,
        workdir: Union[str, os.PathLike],
        model: Optional[str] = None,
        effort: str = "low",
        timeout: float = 120.0,
        oauth_token: Optional[str] = None,
        auth_label: Optional[str] = None,
    ) -> None:
        self.claude_bin = claude_bin
        self.workdir = Path(workdir)
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self._oauth_token = oauth_token
        self._auth_label = auth_label

    @property
    def auth_mode(self) -> str:
        if self._auth_label:
            return self._auth_label
        return "subscription token" if self._oauth_token else AUTH_LOCAL_LOGIN

    def __repr__(self) -> str:
        return (
            f"ClaudeCLI(claude_bin={self.claude_bin!r}, model={self.model!r}, "
            f"effort={self.effort!r}, timeout={self.timeout!r}, auth_mode={self.auth_mode!r})"
        )

    def build_command(self, system_prompt_file: str, output_format: str = "json") -> List[str]:
        cmd = [
            self.claude_bin, "-p",
            "--safe-mode",
            "--settings", json.dumps(ISOLATION_SETTINGS),
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt-file", system_prompt_file,
            "--output-format", output_format,
            "--effort", self.effort,
        ]
        if output_format == "stream-json":
            cmd.append("--verbose")
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        returncode, stdout, stderr = await self._run(system_prompt, user_prompt, "json")
        return self._finish(returncode, stdout, stderr)

    async def inspect(self) -> Dict[str, Any]:
        """Send a tiny request and report what the CLI loaded (for --check-claude)."""
        returncode, stdout, stderr = await self._run("Reply with exactly: OK", "ping", "stream-json")
        init: Optional[Dict[str, Any]] = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
                init = {key: event.get(key) for key in INIT_KEYS}
                break
        reply = self._finish(returncode, stdout, stderr)
        return {"auth": self.auth_mode, "init": init, "reply": reply}

    def _finish(self, returncode: Optional[int], stdout: str, stderr: str) -> str:
        try:
            return parse_result(returncode, stdout, stderr)
        except ClaudeAuthError as exc:
            if not self._oauth_token:
                raise
            raise ClaudeAuthError(
                "Claude rejected the subscription token or it has expired: run `claude setup-token` "
                "on a computer with a browser and update CLAUDE_CODE_OAUTH_TOKEN. "
                f"CLI said: {exc.detail}",
                exc.detail,
            ) from None

    def _redact(self, text: str) -> str:
        if self._oauth_token:
            text = text.replace(self._oauth_token, REDACTED)
        return text

    async def _run(
        self, system_prompt: str, user_prompt: str, output_format: str
    ) -> Tuple[Optional[int], str, str]:
        self.workdir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.workdir, 0o700)
        fd, prompt_path = tempfile.mkstemp(prefix="system-", suffix=".txt", dir=str(self.workdir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(system_prompt)
            os.chmod(prompt_path, 0o600)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.build_command(prompt_path, output_format),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.workdir),
                    env=build_env(self._oauth_token),
                    **_spawn_kwargs(),
                )
            except FileNotFoundError:
                raise ClaudeError(
                    f"claude binary not found at {self.claude_bin!r}: install Claude Code or set CLAUDE_BIN"
                ) from None
            except OSError as exc:
                raise ClaudeError(f"could not start claude ({self.claude_bin!r}): {exc}") from None
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(user_prompt.encode("utf-8")), self.timeout
                )
            except asyncio.TimeoutError:
                await _kill_tree(proc)
                raise ClaudeTimeout(f"Claude CLI did not answer within {self.timeout:g} seconds") from None
            except asyncio.CancelledError:
                await _kill_tree(proc)
                raise
        finally:
            try:
                os.unlink(prompt_path)
            except OSError:
                pass
        return (
            proc.returncode,
            self._redact(stdout.decode("utf-8", "replace")),
            self._redact(stderr.decode("utf-8", "replace")),
        )
