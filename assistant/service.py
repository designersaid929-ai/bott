"""Business-message handling: decide whether to reply, generate, preview or send."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BusinessConnection, Message

from .claude_cli import ClaudeCLI, ClaudeError, ClaudeLimitError
from .config import Settings
from .language import detect_language
from .prompts import SKIP_TOKEN, build_system_prompt, build_user_prompt
from .store import HistoryStore, RuntimeState, SeenMessages, append_private_line, ensure_private_dir

log = logging.getLogger("assistant")

TELEGRAM_CHUNK = 4000
ERROR_NOTICE_INTERVAL = 600.0

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


class Outcome(str, Enum):
    PREVIEWED = "previewed"
    SENT = "sent"
    DUPLICATE = "duplicate"
    IGNORED_BOT = "ignored_bot"
    NO_CONNECTION = "no_connection"
    FOREIGN_CONNECTION = "foreign_connection"
    CONNECTION_DISABLED = "connection_disabled"
    UNSUPPORTED = "unsupported"
    OWNER_MESSAGE = "owner_message"
    OWNER_ACTIVE = "owner_active"
    PAUSED = "paused"
    LIMITED = "limited"
    SKIPPED = "skipped"
    PERMISSION_DENIED = "permission_denied"
    ERROR = "error"


@dataclass
class Preview:
    connection_id: str
    chat_id: int
    chat_name: str
    language: Optional[str]
    incoming: str
    reply: str
    note: str = ""


def print_preview(preview: Preview) -> None:
    lines = [
        "",
        "========== PREVIEW, not sent ==========",
        f"Chat:     {preview.chat_name} ({preview.chat_id})",
        f"Language: {preview.language or 'unknown'}",
        f"Incoming: {preview.incoming}",
        f"Reply:    {preview.reply}",
    ]
    if preview.note:
        lines.append(f"Note:     {preview.note}")
    lines.append("=======================================")
    log.info("\n".join(lines))


_MEDIA_LABELS = (
    ("voice", "voice message"),
    ("video_note", "video message"),
    ("audio", "audio"),
    ("photo", "photo"),
    ("video", "video"),
    ("animation", "GIF"),
    ("document", "file"),
    ("location", "location"),
    ("venue", "venue"),
    ("contact", "contact"),
    ("poll", "poll"),
    ("dice", "dice"),
)


def describe(message: Message) -> Optional[str]:
    """Text for the conversation log, or None for service messages."""
    if message.text:
        return message.text
    if message.sticker:
        return f"[sticker] {message.sticker.emoji or ''}".strip()
    caption = message.caption or ""
    for attr, label in _MEDIA_LABELS:
        if getattr(message, attr, None):
            return f"[{label}] {caption}".strip()
    return None


def can_reply(connection: BusinessConnection) -> bool:
    rights = getattr(connection, "rights", None)
    if rights is not None:
        return bool(getattr(rights, "can_reply", False))
    return bool(getattr(connection, "can_reply", False))


def read_profile(path: Path) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return _COMMENT_RE.sub("", text).strip()


def profile_is_empty(profile: str) -> bool:
    """True when the profile only has headings and empty "- Field:" lines."""
    for line in profile.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.fullmatch(r"-\s*[^:]+:\s*", stripped):
            continue
        return False
    return True


def split_text(text: str, size: int = TELEGRAM_CHUNK) -> List[str]:
    chunks = []
    rest = text
    while len(rest) > size:
        cut = rest.rfind("\n", 0, size)
        if cut < size // 2:
            cut = rest.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


def _name_of(message: Message) -> str:
    if message.from_user:
        return message.from_user.full_name
    return message.chat.full_name or str(message.chat.id)


class Assistant:
    def __init__(
        self,
        settings: Settings,
        generator: Any,
        *,
        clock: Callable[[], float] = time.time,
        preview_sink: Callable[[Preview], None] = print_preview,
    ) -> None:
        self.settings = settings
        self.generator = generator
        self.clock = clock
        self.preview_sink = preview_sink
        data_dir = ensure_private_dir(settings.data_dir)
        self.history = HistoryStore(data_dir / "history.json", settings.history_limit)
        self.seen = SeenMessages(data_dir / "seen.json")
        self.state = RuntimeState(data_dir / "state.json")
        self.previews_path = data_dir / "previews.jsonl"
        self.outcomes: Deque[Outcome] = deque(maxlen=100)
        self._connections: Dict[str, BusinessConnection] = {}
        self._takeover_until: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._last_error_notice: Optional[float] = None

    # ----- Telegram updates -------------------------------------------------

    def on_business_connection(self, connection: BusinessConnection) -> None:
        self._connections[connection.id] = connection
        if connection.user.id != self.settings.owner_id:
            log.warning(
                "Business connection %s belongs to user %s, not OWNER_ID; it will be ignored",
                connection.id, connection.user.id,
            )
            return
        log.info(
            "Business connection %s: user=%s enabled=%s can_reply=%s",
            connection.id, connection.user.id, connection.is_enabled, can_reply(connection),
        )

    async def on_business_message(self, bot: Bot, message: Message) -> Outcome:
        outcome = await self._handle(bot, message)
        self.outcomes.append(outcome)
        log.debug("Business message %s in chat %s: %s", message.message_id, message.chat.id, outcome.value)
        return outcome

    async def _handle(self, bot: Bot, message: Message) -> Outcome:
        conn_id = message.business_connection_id
        if not conn_id:
            return Outcome.NO_CONNECTION
        chat_id = message.chat.id
        chat_key = HistoryStore.key(conn_id, chat_id)

        # Marked before any await, so concurrent copies of one update are dropped too.
        if not self.seen.first_time(f"{chat_key}:{message.message_id}"):
            return Outcome.DUPLICATE

        if message.sender_business_bot or (message.from_user and message.from_user.is_bot):
            return Outcome.IGNORED_BOT

        connection = self._connections.get(conn_id)
        if connection is None:
            try:
                connection = await bot.get_business_connection(business_connection_id=conn_id)
            except TelegramAPIError as exc:
                log.warning("Could not look up business connection %s: %s", conn_id, exc)
                return Outcome.NO_CONNECTION
            self._connections[conn_id] = connection
        if connection.user.id != self.settings.owner_id:
            return Outcome.FOREIGN_CONNECTION
        if not connection.is_enabled:
            return Outcome.CONNECTION_DISABLED

        text = describe(message)
        if text is None:
            return Outcome.UNSUPPORTED

        if message.from_user and message.from_user.id == self.settings.owner_id:
            self.history.add(conn_id, chat_id, "owner", "You", text, detect_language(text), self.clock())
            if self.settings.owner_takeover_minutes > 0:
                self._takeover_until[chat_key] = self.clock() + self.settings.owner_takeover_minutes * 60
            return Outcome.OWNER_MESSAGE

        language = detect_language(text) or self.history.last_language(conn_id, chat_id)
        chat_name = _name_of(message)
        self.history.add(conn_id, chat_id, "other", chat_name, text, language, self.clock())

        lock = self._locks.setdefault(chat_key, asyncio.Lock())
        async with lock:
            blocked = self._blocked(chat_key)
            if blocked is not None:
                return blocked

            system_prompt = build_system_prompt(
                read_profile(self.settings.profile_path), datetime.fromtimestamp(self.clock())
            )
            user_prompt = build_user_prompt(self.history.get(conn_id, chat_id), language)
            try:
                reply = await self.generator.generate(system_prompt, user_prompt)
            except ClaudeLimitError as exc:
                return await self._on_limit(bot, exc)
            except ClaudeError as exc:
                log.error("Claude failed for chat %s: %s", chat_id, exc)
                await self._notify_error(bot, exc)
                return Outcome.ERROR

            reply = (reply or "").strip()
            if not reply or reply == SKIP_TOKEN:
                log.info("No reply needed in chat %s (%s)", chat_id, SKIP_TOKEN)
                return Outcome.SKIPPED

            preview = Preview(conn_id, chat_id, chat_name, language, text, reply)
            if not self.settings.auto_send:
                self._show(preview)
                return Outcome.PREVIEWED
            return await self._send(bot, preview, chat_key)

    async def _send(self, bot: Bot, preview: Preview, chat_key: str) -> Outcome:
        # The owner may have written (or paused the bot) while Claude was thinking.
        blocked = self._blocked(chat_key)
        if blocked is not None:
            preview.note = f"not sent: {blocked.value}"
            self._show(preview)
            return blocked

        # Always ask Telegram again right before sending; never trust the cache here.
        reason = None
        try:
            connection = await bot.get_business_connection(business_connection_id=preview.connection_id)
        except TelegramAPIError as exc:
            reason = f"could not verify the business connection: {exc}"
        else:
            self._connections[preview.connection_id] = connection
            if connection.user.id != self.settings.owner_id:
                reason = "the connection belongs to another account"
            elif not connection.is_enabled:
                reason = "the connection is disabled"
            elif not can_reply(connection):
                reason = "the bot has no permission to reply (Chatbots settings)"
        if reason:
            log.warning("Not sending reply to chat %s: %s", preview.chat_id, reason)
            preview.note = f"not sent: {reason}"
            self._show(preview)
            return Outcome.PERMISSION_DENIED

        try:
            for chunk in split_text(preview.reply):
                await bot.send_message(
                    chat_id=preview.chat_id, text=chunk, business_connection_id=preview.connection_id
                )
        except TelegramAPIError as exc:
            log.error("Sending reply to chat %s failed: %s", preview.chat_id, exc)
            return Outcome.ERROR
        self.history.add(
            preview.connection_id, preview.chat_id, "owner", "You", preview.reply, preview.language, self.clock()
        )
        log.info("Sent reply to %s (%s)", preview.chat_name, preview.chat_id)
        return Outcome.SENT

    # ----- helpers ----------------------------------------------------------

    def _blocked(self, chat_key: str) -> Optional[Outcome]:
        now = self.clock()
        if self.state.paused:
            return Outcome.PAUSED
        limit_until = self.state.limit_until
        if limit_until is not None:
            if limit_until > now:
                return Outcome.LIMITED
            log.info("Claude usage limit period is over; replying again")
            self.state.limit_until = None
        if self._takeover_until.get(chat_key, 0) > now:
            return Outcome.OWNER_ACTIVE
        return None

    def _show(self, preview: Preview) -> None:
        record = asdict(preview)
        record["ts"] = self.clock()
        append_private_line(self.previews_path, json.dumps(record, ensure_ascii=False))
        self.preview_sink(preview)

    async def _on_limit(self, bot: Bot, exc: ClaudeLimitError) -> Outcome:
        now = self.clock()
        if exc.resets_at and exc.resets_at > now:
            until = exc.resets_at
        else:
            until = now + self.settings.limit_cooldown_minutes * 60
        self.state.limit_until = until
        until_text = datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M")
        log.warning("%s. No replies until %s. CLI said: %s", exc, until_text, exc.detail)
        if self.settings.auto_send:
            await self._notify_owner(
                bot,
                f"Claude obuna limiti tugadi. {until_text} gacha javob bermayman. "
                "/resume bilan oldinroq qayta urinib ko'rish mumkin.",
            )
        return Outcome.LIMITED

    async def _notify_error(self, bot: Bot, exc: ClaudeError) -> None:
        if not self.settings.auto_send:
            return
        now = self.clock()
        if self._last_error_notice is not None and now - self._last_error_notice < ERROR_NOTICE_INTERVAL:
            return
        self._last_error_notice = now
        await self._notify_owner(bot, f"Javob tayyorlab bo'lmadi: {exc}")

    async def _notify_owner(self, bot: Bot, text: str) -> None:
        try:
            await bot.send_message(chat_id=self.settings.owner_id, text=text)
        except TelegramAPIError as exc:
            log.warning("Could not notify the owner: %s", exc)

    # ----- owner controls ---------------------------------------------------

    def pause(self) -> None:
        self.state.paused = True
        log.info("Paused by the owner")

    def resume(self) -> None:
        self.state.paused = False
        self.state.limit_until = None
        self._takeover_until.clear()
        log.info("Resumed by the owner")

    def _limit_text(self) -> Optional[str]:
        until = self.state.limit_until
        if until is not None and until > self.clock():
            return datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M")
        return None

    def status_text(self) -> str:
        if self.settings.auto_send:
            mode = "AUTO-SEND: replies are sent from your account"
        else:
            mode = "PREVIEW: replies are only shown in the logs"
        limit = self._limit_text()
        lines = [
            f"Mode: {mode}",
            f"State: {'paused' if self.state.paused else 'running'}",
            f"Claude usage limit: {'until ' + limit if limit else 'none'}",
            f"Chats in history: {self.history.chat_count()}",
        ]
        return "\n".join(lines)

    def start_text(self, bot_username: Optional[str] = None) -> str:
        username = f"@{bot_username}" if bot_username else "botning username'i"
        if self.settings.auto_send:
            mode = "AUTO-SEND — javoblar sizning nomingizdan avtomatik yuboriladi"
        else:
            mode = "PREVIEW — javoblar faqat logda ko'rinadi, hech kimga yuborilmaydi"
        state = "pauzada (/resume bilan yoqing)" if self.state.paused else "ishlayapti"
        limit = self._limit_text()
        minutes = self.settings.owner_takeover_minutes
        if minutes > 0:
            takeover = f"• Chatda o'zingiz yozsangiz, bot o'sha chatda {minutes} daqiqa jim turadi."
        else:
            takeover = "• O'zingiz yozganingizda jim turish o'chirilgan (OWNER_TAKEOVER_MINUTES=0)."
        lines = [
            "Assalomu alaykum! Men sizning shaxsiy yordamchi botingizman.",
            "Sizga kimdir yozsa, profile.md asosida sizning uslubingizda, sizning nomingizdan javob tayyorlayman.",
            "",
            "Hozirgi holat:",
            f"• Rejim: {mode}",
            f"• Holat: {state}",
        ]
        if limit:
            lines.append(f"• Claude limiti: {limit} gacha javob berilmaydi")
        lines += [
            "",
            "Qanday ulanadi:",
            "1. Telegram → Settings → Telegram Business → Chatbots (Telegram Premium kerak).",
            f"2. {username} ni kiriting.",
            "3. Bot qaysi chatlarni ko'rishini tanlang.",
            "4. Javob berish (reply) ruxsatini yoqing.",
            "",
            "Buyruqlar:",
            "/pause — javob berishni to'xtatish",
            "/resume — qayta yoqish",
            "/status — holatni ko'rish",
            "",
            "Bilib qo'ying:",
            takeover,
            "• Pul, qarz, uchrashuv va rejalar bo'yicha hech narsaga rozilik bermaydi.",
            "• profile.md ni to'ldiring — bot sizni shu fayldan taniydi.",
        ]
        if not self.settings.auto_send:
            lines.append("• Preview'lar yaxshi bo'lsa, AUTO_SEND=true qiling.")
        return "\n".join(lines)


def make_claude_cli(settings: Settings) -> ClaudeCLI:
    return ClaudeCLI(
        settings.claude_bin,
        workdir=settings.data_dir / "claude-run",
        model=settings.claude_model,
        effort=settings.claude_effort,
        timeout=settings.claude_timeout,
        oauth_token=settings.claude_oauth_token,
        auth_label=settings.claude_auth_mode,
    )


def build_assistant(settings: Settings, **kwargs: Any) -> Assistant:
    generator = kwargs.pop("generator", None) or make_claude_cli(settings)
    return Assistant(settings, generator, **kwargs)
