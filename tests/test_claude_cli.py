"""ClaudeCLI against a fake `claude` executable that records how it was started."""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from assistant.claude_cli import (
    ISOLATION_SETTINGS,
    ClaudeAuthError,
    ClaudeCLI,
    ClaudeError,
    ClaudeLimitError,
    ClaudeTimeout,
    build_env,
    classify_failure,
    parse_result,
)

TOKEN = "sk-ant-oat01-" + "Q" * 60

FAKE_CLAUDE = r'''
import json, os, subprocess, sys, time

args = sys.argv[1:]
stdin = sys.stdin.buffer.read().decode("utf-8")
record = {
    "argv": args,
    "stdin": stdin,
    "env": dict(os.environ),
    "cwd": os.getcwd(),
    "listing": sorted(os.listdir(".")),
    "pid": os.getpid(),
}
if "--system-prompt-file" in args:
    path = args[args.index("--system-prompt-file") + 1]
    record["system_prompt_path"] = os.path.abspath(path)
    record["system_prompt_mode"] = os.stat(path).st_mode & 0o777
    with open(path, encoding="utf-8") as handle:
        record["system_prompt"] = handle.read()
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    record["grandchild_pid"] = child.pid
with open(os.environ["FAKE_RECORD"], "w", encoding="utf-8") as handle:
    json.dump(record, handle)

def result(text, is_error=False, status=None, subtype="success"):
    print(json.dumps({"type": "result", "subtype": subtype, "is_error": is_error,
                      "api_error_status": status, "result": text}))

token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
if mode == "ok":
    if "stream-json" in args:
        print(json.dumps({"type": "system", "subtype": "init", "tools": [], "mcp_servers": [],
                          "plugins": [], "skills": [], "apiKeySource": "none", "model": "claude-test"}))
    result("  Hello there  \n")
elif mode == "sleep":
    time.sleep(60)
elif mode == "limit_epoch":
    result("Claude AI usage limit reached|1900000000", is_error=True)
elif mode == "limit_text":
    result("You've hit your limit · resets 3pm (Asia/Tashkent)", is_error=True)
elif mode == "auth":
    result("Invalid API key · token " + token + " was rejected · Please run /login", is_error=True, status=401)
elif mode == "crash":
    sys.stderr.write("boom: something broke\n")
    sys.exit(3)
elif mode == "empty":
    result("   ")
elif mode == "echo_token":
    result("token=" + token)
'''


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    if os.name == "nt":
        binary = tmp_path / "claude.cmd"
        binary.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        binary = tmp_path / "claude"
        binary.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}", encoding="utf-8")
        binary.chmod(0o755)
    record_path = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_RECORD", str(record_path))
    monkeypatch.setenv("FAKE_MODE", "ok")

    class Fake:
        workdir = tmp_path / "data" / "claude-run"

        def cli(self, **kwargs: Any) -> ClaudeCLI:
            kwargs.setdefault("workdir", self.workdir)
            kwargs.setdefault("timeout", 30)
            return ClaudeCLI(str(binary), **kwargs)

        def mode(self, value: str) -> None:
            monkeypatch.setenv("FAKE_MODE", value)

        def record(self) -> Dict[str, Any]:
            return json.loads(record_path.read_text(encoding="utf-8"))

    return Fake()


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ----- running the CLI ---------------------------------------------------------------------

async def test_reply_is_stripped(fake_claude):
    assert await fake_claude.cli().generate("system", "hi") == "Hello there"


async def test_message_goes_only_through_stdin(fake_claude):
    message = "Salom! Bu maxfiy xabar 12345"
    profile = "<profile>PROFILE-SECRET-777</profile>"
    await fake_claude.cli().generate(profile, message)
    record = fake_claude.record()
    assert record["stdin"] == message
    argv = " ".join(record["argv"])
    assert "maxfiy" not in argv
    assert "PROFILE-SECRET-777" not in argv
    assert record["system_prompt"] == profile
    if os.name == "posix":
        assert record["system_prompt_mode"] == 0o600


async def test_system_prompt_file_is_removed(fake_claude):
    await fake_claude.cli().generate("system", "hi")
    assert not os.path.exists(fake_claude.record()["system_prompt_path"])
    fake_claude.mode("crash")
    with pytest.raises(ClaudeError):
        await fake_claude.cli().generate("system", "hi")
    assert not os.path.exists(fake_claude.record()["system_prompt_path"])
    assert os.listdir(fake_claude.workdir) == []


async def test_shell_syntax_is_never_executed(fake_claude, tmp_path):
    marker = tmp_path / "pwned"
    message = f'$(touch {marker}) `touch {marker}` "; touch {marker}; echo " & echo x > {marker}'
    await fake_claude.cli().generate("system", message)
    assert not marker.exists()
    assert fake_claude.record()["stdin"] == message


async def test_isolation_flags_in_order(fake_claude):
    await fake_claude.cli(effort="medium", model="sonnet").generate("system", "hi")
    argv = fake_claude.record()["argv"]
    expected_prefix = [
        "-p",
        "--safe-mode",
        "--settings", json.dumps(ISOLATION_SETTINGS),
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--system-prompt-file",
    ]
    assert argv[:len(expected_prefix)] == expected_prefix
    assert argv[len(expected_prefix) + 1:] == ["--output-format", "json", "--effort", "medium", "--model", "sonnet"]
    assert "--bare" not in argv
    assert "--mcp-config" not in argv
    settings = json.loads(argv[3])
    assert settings["disableAllHooks"] is True
    assert settings["enabledPlugins"] and not any(settings["enabledPlugins"].values())


def test_stream_json_adds_verbose():
    cmd = ClaudeCLI("claude", workdir=".").build_command("p.txt", "stream-json")
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd


async def test_child_environment_is_cleaned(fake_claude, monkeypatch):
    for key in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_SIMPLE",
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
    ):
        monkeypatch.setenv(key, "parent-value")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-from-parent")
    monkeypatch.setenv("HOME", "/home/someone")
    await fake_claude.cli().generate("system", "hi")
    env = {k.upper(): v for k, v in fake_claude.record()["env"].items()}
    assert not [k for k in env if k.startswith("ANTHROPIC_")]
    for key in (
        "TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
    ):
        assert key not in env
    assert env["HOME"] == "/home/someone"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"


async def test_configured_token_reaches_only_the_child_env(fake_claude, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    cli = fake_claude.cli(oauth_token=TOKEN)
    await cli.generate("system prompt", "hello")
    record = fake_claude.record()
    env = {k.upper(): v for k, v in record["env"].items()}
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert TOKEN not in " ".join(record["argv"])
    assert TOKEN not in record["stdin"]
    assert TOKEN not in record["system_prompt"]
    assert TOKEN not in repr(cli)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ


async def test_no_token_in_child_when_not_configured(fake_claude, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-parent")
    await fake_claude.cli().generate("system", "hi")
    env = {k.upper() for k in fake_claude.record()["env"]}
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


async def test_token_is_redacted_in_output(fake_claude):
    fake_claude.mode("echo_token")
    reply = await fake_claude.cli(oauth_token=TOKEN).generate("system", "hi")
    assert reply == "token=[REDACTED]"


async def test_rejected_token_message(fake_claude):
    fake_claude.mode("auth")
    with pytest.raises(ClaudeAuthError) as info:
        await fake_claude.cli(oauth_token=TOKEN).generate("system", "hi")
    message = str(info.value)
    assert "claude setup-token" in message
    assert "CLI said:" in message
    assert "[REDACTED]" in message
    assert TOKEN not in message
    assert TOKEN not in info.value.detail


async def test_auth_error_without_token_mentions_login(fake_claude):
    fake_claude.mode("auth")
    with pytest.raises(ClaudeAuthError) as info:
        await fake_claude.cli().generate("system", "hi")
    assert "/login" in str(info.value)


async def test_inspect_reports_auth_and_init(fake_claude):
    info = await fake_claude.cli(oauth_token=TOKEN, auth_label="subscription token from .env").inspect()
    assert info["auth"] == "subscription token from .env"
    assert info["init"]["tools"] == [] and info["init"]["plugins"] == []
    assert info["reply"] == "Hello there"
    argv = fake_claude.record()["argv"]
    assert "--verbose" in argv and "stream-json" in argv
    assert fake_claude.record()["stdin"] == "ping"

    info = await fake_claude.cli().inspect()
    assert info["auth"] == "this machine's claude login"


async def test_runs_in_its_own_private_empty_dir(fake_claude):
    await fake_claude.cli().generate("system", "hi")
    record = fake_claude.record()
    assert Path(record["cwd"]).resolve() == fake_claude.workdir.resolve()
    assert record["listing"] == [Path(record["system_prompt_path"]).name]
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(fake_claude.workdir).st_mode) == 0o700


async def test_timeout_kills_process_group(fake_claude):
    fake_claude.mode("sleep")
    started = time.monotonic()
    with pytest.raises(ClaudeTimeout):
        await fake_claude.cli(timeout=1.5).generate("system", "hi")
    assert time.monotonic() - started < 5
    record = fake_claude.record()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (pid_alive(record["pid"]) or pid_alive(record["grandchild_pid"])):
        time.sleep(0.1)
    assert not pid_alive(record["pid"])
    assert not pid_alive(record["grandchild_pid"])


async def test_limit_with_epoch(fake_claude):
    fake_claude.mode("limit_epoch")
    with pytest.raises(ClaudeLimitError) as info:
        await fake_claude.cli().generate("system", "hi")
    assert info.value.resets_at == 1900000000
    assert "|" not in info.value.detail
    assert str(info.value).startswith("Claude subscription usage limit reached (resets ")


async def test_limit_with_text(fake_claude):
    fake_claude.mode("limit_text")
    with pytest.raises(ClaudeLimitError) as info:
        await fake_claude.cli().generate("system", "hi")
    assert info.value.resets_at is None
    assert info.value.reset_text == "3pm (Asia/Tashkent)"


async def test_crash_and_empty(fake_claude):
    fake_claude.mode("crash")
    with pytest.raises(ClaudeError) as info:
        await fake_claude.cli().generate("system", "hi")
    assert "boom" in str(info.value)
    fake_claude.mode("empty")
    with pytest.raises(ClaudeError, match="empty"):
        await fake_claude.cli().generate("system", "hi")


async def test_missing_binary(tmp_path):
    cli = ClaudeCLI(str(tmp_path / "no-such-claude"), workdir=tmp_path / "run")
    with pytest.raises(ClaudeError, match="not found"):
        await cli.generate("system", "hi")


# ----- pure helpers -----------------------------------------------------------------------

def test_classify_failure():
    limit = classify_failure("Claude AI usage limit reached|1760000000")
    assert isinstance(limit, ClaudeLimitError)
    assert limit.resets_at == 1760000000 and limit.detail == "Claude AI usage limit reached"
    assert isinstance(classify_failure("anything", 429), ClaudeLimitError)
    assert isinstance(classify_failure("Rate-limit exceeded"), ClaudeLimitError)
    assert isinstance(classify_failure("You are out of extra usage"), ClaudeLimitError)
    assert classify_failure("hit your limit · resets 5am").reset_text == "5am"

    auth = classify_failure("Not logged in · Please run /login")
    assert isinstance(auth, ClaudeAuthError) and "/login" in str(auth)
    assert isinstance(classify_failure("nope", 401), ClaudeAuthError)
    assert isinstance(classify_failure("OAuth token has expired"), ClaudeAuthError)
    assert len(classify_failure("x" * 1000, 401).detail) == 300

    other = classify_failure("y" * 2000)
    assert type(other) is ClaudeError and len(str(other)) < 600


def test_parse_result():
    ok = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": " hi \n"})
    assert parse_result(0, "noise\n" + ok + "\n\n", "") == "hi"
    error = json.dumps({"subtype": "success", "is_error": True, "result": "Not logged in", "api_error_status": None})
    with pytest.raises(ClaudeAuthError):
        parse_result(1, error, "")
    with pytest.raises(ClaudeError):
        parse_result(1, json.dumps({"subtype": "error_max_turns", "is_error": False, "result": "x"}), "")
    with pytest.raises(ClaudeError, match="empty"):
        parse_result(0, json.dumps({"subtype": "success", "is_error": False, "result": ""}), "")
    with pytest.raises(ClaudeError, match="exit code 9"):
        parse_result(9, "", "")
    with pytest.raises(ClaudeLimitError):
        parse_result(1, "not json", "usage limit reached")


def test_build_env_adds_token_only_when_given(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "parent")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in build_env()
    assert build_env(TOKEN)["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
