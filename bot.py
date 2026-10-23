"""Entry point.

  python bot.py                      run the bot (settings from .env)
  python bot.py --settings-from-env  run in a container (settings from environment variables)
  python bot.py --check-claude       check the Claude CLI setup, no Telegram
  python bot.py --try "text"         show the reply to a made-up message, no Telegram
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError

from assistant.claude_cli import ClaudeError
from assistant.config import DEFAULT_ENV_FILE, SETTING_KEYS, ConfigError, Settings, env_file_warning, load_settings
from assistant.handlers import ALLOWED_UPDATES, build_router
from assistant.language import detect_language
from assistant.prompts import build_system_prompt, build_user_prompt
from assistant.service import build_assistant, make_claude_cli, profile_is_empty, read_profile
from assistant.store import ensure_private_dir

log = logging.getLogger("bot")

FORBIDDEN_KEY_SOURCES = ("ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key")


def settings_from_process_env(*, require_telegram: bool = True) -> Settings:
    """Read SETTING_KEYS from os.environ, then remove them so child processes never inherit them."""
    try:
        source = {key: os.environ[key] for key in SETTING_KEYS if key in os.environ}
        return load_settings(DEFAULT_ENV_FILE, require_telegram=require_telegram, source=source)
    finally:
        for key in SETTING_KEYS:
            os.environ.pop(key, None)


def check_inspection(info: Dict[str, Any]) -> List[str]:
    """Problems that make the Claude setup unsafe or unusable for the bot."""
    problems = []
    init = info.get("init")
    if not init:
        return ["the CLI did not report an init event"]
    for key in ("tools", "mcp_servers", "plugins", "skills"):
        if init.get(key):
            problems.append(f"{key} must be empty, got: {init.get(key)}")
    if init.get("apiKeySource") in FORBIDDEN_KEY_SOURCES:
        problems.append(f"apiKeySource is {init.get('apiKeySource')!r}: an API key is used, not the subscription")
    if init.get("apiProvider") not in ("firstParty", None):
        problems.append(f"apiProvider is {init.get('apiProvider')!r}, expected firstParty")
    return problems


async def check_claude(settings: Settings) -> int:
    cli = make_claude_cli(settings)
    try:
        info = await cli.inspect()
    except ClaudeError as exc:
        print(json.dumps({"auth": cli.auth_mode, "error": str(exc)}, indent=2, ensure_ascii=False))
        return 1
    print(json.dumps(info, indent=2, ensure_ascii=False))
    problems = check_inspection(info)
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1
    print(f"OK: Claude CLI answered using {info['auth']} ({settings.claude_bin})")
    return 0


async def try_reply(settings: Settings, text: str) -> int:
    cli = make_claude_cli(settings)
    language = detect_language(text)
    system_prompt = build_system_prompt(read_profile(settings.profile_path), datetime.now())
    user_prompt = build_user_prompt(
        [{"role": "other", "name": "Friend", "text": text, "lang": language, "ts": time.time()}], language
    )
    try:
        reply = await cli.generate(system_prompt, user_prompt)
    except ClaudeError as exc:
        print(f"Claude error: {exc}", file=sys.stderr)
        return 1
    print(f"Language: {language or 'unknown'}")
    print(f"Message:  {text}")
    print(f"Reply:    {reply}")
    return 0


async def run_bot(settings: Settings) -> int:
    bot = Bot(settings.bot_token)
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            log.error("Telegram rejected TELEGRAM_BOT_TOKEN. Check it with @BotFather (/mybots → API Token).")
            return 2
        assistant = build_assistant(settings)
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(assistant, settings.owner_id, me.username))

        log.info("Bot @%s is running", me.username)
        if settings.auto_send:
            log.warning("Mode: AUTO-SEND (replies are sent from your account without review)")
        else:
            log.info("Mode: PREVIEW (replies are only shown in the logs, nothing is sent)")
        log.info("Claude auth: %s", settings.claude_auth_mode)
        log.info("Claude binary: %s", settings.claude_bin)
        if assistant.state.paused:
            log.warning("The bot starts PAUSED: send /resume to the bot to turn it on")
        if profile_is_empty(read_profile(settings.profile_path)):
            log.warning("%s is not filled in: replies will be generic", settings.profile_path)

        await dispatcher.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
        return 0
    finally:
        await bot.session.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Personal Telegram Business assistant")
    parser.add_argument("--settings-from-env", action="store_true",
                        help="read settings from environment variables (container mode), not .env")
    parser.add_argument("--check-claude", action="store_true", help="check the Claude CLI setup and exit")
    parser.add_argument("--try", dest="try_text", metavar="TEXT", help="show the reply to a made-up message")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    require_telegram = not (args.check_claude or args.try_text is not None)
    try:
        if args.settings_from_env:
            settings = settings_from_process_env(require_telegram=require_telegram)
        else:
            settings = load_settings(DEFAULT_ENV_FILE, require_telegram=require_telegram)
            warning = env_file_warning(DEFAULT_ENV_FILE)
            if warning:
                log.warning(warning)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    ensure_private_dir(settings.data_dir)
    ensure_private_dir(settings.data_dir / "claude-run")

    if args.check_claude:
        return asyncio.run(check_claude(settings))
    if args.try_text is not None:
        return asyncio.run(try_reply(settings, args.try_text))
    try:
        return asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
