# Personal Telegram Business assistant

A bot that answers people who write to **your own Telegram account**, in your name and in your style.
It connects through Telegram Business (*Settings → Telegram Business → Chatbots*), learns about you from
`profile.md`, and generates every reply with the **Claude Code CLI** (`claude -p`) using your Claude
**subscription** (a `claude setup-token` token). An Anthropic API key is never used.

## What it does

- Someone writes to you → the bot drafts the reply you would write (same language, same script, same tone).
- **PREVIEW mode is the default** (`AUTO_SEND=false`): replies are only printed to the logs as
  `PREVIEW, not sent` blocks and appended to `data/previews.jsonl`. Nothing is sent until you set `AUTO_SEND=true`.
- Which chats the bot sees is decided **only by Telegram**: the chats you pick in
  *Settings → Telegram Business → Chatbots*. There is no allowlist in the code.
- It never agrees to money, debts, meetings or plans, never shares addresses, cards, passwords or codes,
  and admits it is an automatic assistant when someone seriously asks.

## Behaviour

| Situation | What happens |
|---|---|
| Business connection of another account | Logged as a warning, messages ignored (Claude is not called) |
| Which chats | Whatever you selected in Telegram's *Chatbots* settings; every such chat can get a reply |
| You write in a chat yourself | Saved to history, never answered; the bot stays quiet in that chat for `OWNER_TAKEOVER_MINUTES` |
| Messages from bots (or the bot's own replies) | Ignored |
| Duplicate updates | Processed once (also across restarts, `data/seen.json`) |
| Service messages (title changes etc.) | Ignored; stickers, voice, photos are described as `[sticker] 😀`, `[voice message]`, … |
| `/pause` | No replies at all until `/resume` (survives restarts) |
| Subscription usage limit | No Claude calls until the reset time (or `LIMIT_COOLDOWN_MINUTES`); survives restarts; owner is notified in AUTO-SEND mode; `/resume` clears it |
| Timeout / login error | Logged; nothing is sent to the chat; in AUTO-SEND mode the owner gets at most one notice per 10 minutes |
| Claude answers `[SKIP]` | Nothing is shown or sent |
| Before every auto-send | Pause / limit / takeover are checked again, and the business connection is re-fetched from Telegram (owner, enabled, `can_reply`) |

Owner commands (private chat with the bot, owner only): `/pause`, `/resume`, `/status`.
`/start` shows the owner an Uzbek guide (mode, state, how to connect, commands). Anyone else gets a short
Uzbek note that this is a private assistant; Claude is not called and nothing about the owner is revealed.
All other direct messages to the bot are silently ignored.

## How Claude is called

One `claude -p` process per reply, started with `asyncio.create_subprocess_exec` (never a shell):

```
claude -p --safe-mode --settings '{"disableAllHooks": true, "enabledPlugins": {...: false}}'
       --tools "" --strict-mcp-config --setting-sources "" --disable-slash-commands
       --no-session-persistence --system-prompt-file <temp file> --output-format json --effort low
```

- No tools, no MCP servers, no plugins (every builtin plugin is disabled explicitly), no hooks, no skills,
  no user/project settings, no session files.
- `--bare` is **not** used: it ignores OAuth login, so the subscription token would not work.
- The conversation goes only through **stdin**. The system prompt (with your profile) goes into a 0600 temp
  file inside `data/claude-run/` (0700) that is deleted afterwards, so it never shows up in `ps`.
- The child environment is cleaned: every `ANTHROPIC_*` variable, `TELEGRAM_BOT_TOKEN`, Bedrock/Vertex/Foundry
  switches, `CLAUDE_CODE_SIMPLE`, `CLAUDECODE` are removed; auto-memory and CLAUDE.md loading are disabled.
  `CLAUDE_CODE_OAUTH_TOKEN` is added only when it is configured.
- The token is replaced by `[REDACTED]` in all CLI output before it is parsed or logged. Settings never write
  to `os.environ`.
- Token rules: must start with `sk-ant-oat` and be on one line; `sk-ant-api...` keys are rejected.
- `python bot.py --check-claude` sends a tiny request and verifies the isolation (tools, MCP, plugins and
  skills empty, no API key source, first-party provider) and shows which auth was used.

## Deploy to Railway

1. On a computer with a browser run `claude setup-token`, approve with your subscription account and copy the
   `sk-ant-oat...` token (valid for 1 year).
2. In @BotFather: `/newbot`, then *Bot Settings → Business Mode* → turn on. Get your own numeric id from @userinfobot.
3. Create a **private** GitHub repository and push this code (`profile.md` is baked into the Docker image).
4. Railway → *New Project* → *GitHub Repository* → pick the repo. It builds with the `Dockerfile`.
5. Service → *Variables* → *Raw Editor*:
   ```
   TELEGRAM_BOT_TOKEN=...
   OWNER_ID=...
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...
   AUTO_SEND=false
   ```
   Optional: `CLAUDE_MODEL`, `CLAUDE_EFFORT`, `CLAUDE_TIMEOUT_SECONDS`, `LIMIT_COOLDOWN_MINUTES`,
   `OWNER_TAKEOVER_MINUTES`, `HISTORY_LIMIT`. Not needed: `CLAUDE_BIN`, `DATA_DIR`, `PROFILE_PATH`.
6. Volume: right-click the canvas (or Cmd/Ctrl+K) → *Volume* → choose the service → mount path exactly
   `/app/data` → *Deploy*. Without a volume, history, pause and limit state are lost on every deploy.
7. Logs should show `Bot @... is running`, `Mode: PREVIEW` and
   `Claude auth: subscription token from environment variables`. Red lines in Railway are just stderr, not errors.
8. Send `/start` to your bot, then in Telegram *Settings → Telegram Business → Chatbots* (needs Telegram Premium):
   enter the bot username, choose the chats, allow replying. The log shows
   `Business connection ...: user=<id> enabled=True`.
9. When someone writes to you, the log shows a `PREVIEW, not sent` block. When the replies look good,
   set `AUTO_SEND=true`.

Notes:
- Run the bot in **one place only**: two pollers with the same bot token interfere with each other
  (do not run `python bot.py` locally while Railway is running).
- Every push redeploys; changing `profile.md` also means commit + push.
- Token expired or leaked: run `claude setup-token` again and update the Railway variable.
  Bot token leaked: `/revoke` in @BotFather.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env && chmod 600 .env     # then put your values in with an editor
.venv/bin/python bot.py --check-claude
.venv/bin/python bot.py --try "Salom, bugun bo'shmisan?"
.venv/bin/python bot.py
```

(On Windows use `.venv\Scripts\python.exe`.) If `CLAUDE_CODE_OAUTH_TOKEN` is empty, this machine's own
`claude` login is used, which is fine for local testing only.

### Commands and exit codes

| Command | Purpose | Exit code |
|---|---|---|
| `python bot.py` | Run the bot with `.env` | 0 on stop, 2 on config error or rejected bot token |
| `python bot.py --settings-from-env` | Container mode: settings from environment variables, then removed from `os.environ` | same |
| `python bot.py --check-claude` | Check the Claude CLI setup (no Telegram) | 0 ok, 1 problem, 2 config error |
| `python bot.py --try "text"` | Show the reply to a made-up message (no Telegram) | 0 ok, 1 Claude error, 2 config error |

### Tests

```bash
.venv/bin/python -m pytest
```

All tests run offline: Telegram is replaced by a recording session and `claude` by a fake executable.

### Files

```
bot.py                     entry point
assistant/config.py        settings (.env or environment), validation
assistant/claude_cli.py    isolated `claude -p` runner, error classification
assistant/language.py      uz / ru / en hint
assistant/prompts.py       system and user prompts
assistant/store.py         history, seen messages, runtime state (JSON files in data/)
assistant/service.py       business-message logic, owner controls
assistant/handlers.py      aiogram router
profile.md                 everything the bot knows about you (fill it in)
Dockerfile, railway.json   Railway deployment
tests/                     offline tests
```
