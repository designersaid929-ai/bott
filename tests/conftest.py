"""Offline test harness: a recording Telegram session, a fake generator and a fake clock."""
from __future__ import annotations

import asyncio
import itertools
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetBusinessConnection, SendMessage, TelegramMethod
from aiogram.types import BusinessConnection, Chat, Message, Update

from assistant.config import load_settings
from assistant.handlers import build_router
from assistant.service import Assistant

OWNER_ID = 111
FRIEND_ID = 222
STRANGER_ID = 333
BOT_TOKEN = "123456:TEST-token_value"
BOT_USERNAME = "test_assistant_bot"
CONNECTION_ID = "conn-1"
START_TS = 1_800_000_000.0


class RecordingSession(BaseSession):
    """Records every Bot API call; never touches the network."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: List[TelegramMethod] = []
        self.connections: Dict[str, BusinessConnection] = {}
        self._message_ids = itertools.count(10_000)

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: Optional[int] = None) -> Any:
        self.requests.append(method)
        if isinstance(method, GetBusinessConnection):
            connection = self.connections.get(method.business_connection_id)
            if connection is None:
                raise TelegramBadRequest(method=method, message="Bad Request: business connection not found")
            return connection
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(self._message_ids),
                date=datetime.now(),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
                business_connection_id=method.business_connection_id,
            )
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("tests must not download anything")

    async def close(self) -> None:
        pass

    def of_type(self, kind: type) -> List[Any]:
        return [r for r in self.requests if isinstance(r, kind)]


class FakeGenerator:
    """Returns queued replies (or raises queued exceptions) and records calls."""

    def __init__(self, results: Optional[List[Any]] = None, default: str = "Salom, keyinroq yozaman") -> None:
        self.results = list(results or [])
        self.default = default
        self.calls: List[Dict[str, str]] = []
        self.hook: Optional[Callable[[], Awaitable[None]]] = None

    def push(self, *results: Any) -> None:
        self.results.extend(results)

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append({"system": system_prompt, "user": user_prompt})
        await asyncio.sleep(0)
        if self.hook is not None:
            await self.hook()
        result = self.results.pop(0) if self.results else self.default
        if isinstance(result, BaseException):
            raise result
        return result


class Clock:
    def __init__(self, now: float = START_TS) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def connection_payload(
    connection_id: str = CONNECTION_ID,
    user_id: int = OWNER_ID,
    enabled: bool = True,
    can_reply: bool = True,
) -> Dict[str, Any]:
    # Both the new `rights.can_reply` and the old top-level `can_reply` (older aiogram).
    return {
        "id": connection_id,
        "user": {"id": user_id, "is_bot": False, "first_name": "Owner"},
        "user_chat_id": user_id,
        "date": int(START_TS),
        "can_reply": can_reply,
        "rights": {"can_reply": True} if can_reply else {},
        "is_enabled": enabled,
    }


class Harness:
    def __init__(self, tmp_path: Path, *, auto_send: bool = False, **overrides: str) -> None:
        self.tmp_path = tmp_path
        self.data_dir = tmp_path / "data"
        self.profile_path = tmp_path / "profile.md"
        self.profile_path.write_text("# Men haqimda\n- Ism: Sardor\n- Shahar: Toshkent\n", encoding="utf-8")
        env_file = tmp_path / ".env"
        env_file.write_text("", encoding="utf-8")
        values = {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "OWNER_ID": str(OWNER_ID),
            "AUTO_SEND": "true" if auto_send else "false",
            "DATA_DIR": str(self.data_dir),
            "PROFILE_PATH": str(self.profile_path),
            "CLAUDE_BIN": "claude-is-not-called-in-these-tests",
        }
        values.update(overrides)
        self.settings = load_settings(env_file, overrides=values)
        self.clock = Clock()
        self.generator = FakeGenerator()
        self.previews: List[Any] = []
        self.session = RecordingSession()
        self.bot = Bot(BOT_TOKEN, session=self.session)
        self._ids = itertools.count(1)
        self._build()

    def _build(self) -> None:
        self.assistant = Assistant(
            self.settings, self.generator, clock=self.clock, preview_sink=self.previews.append
        )
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(build_router(self.assistant, self.settings.owner_id, BOT_USERNAME))

    def restart(self) -> None:
        """A new Assistant with the same data directory (as after a process restart)."""
        self._build()

    @property
    def requests(self) -> List[TelegramMethod]:
        return self.session.requests

    def set_connection(self, **kwargs: Any) -> BusinessConnection:
        """Change what Telegram answers for GetBusinessConnection, without an update."""
        connection = BusinessConnection.model_validate(connection_payload(**kwargs))
        self.session.connections[connection.id] = connection
        return connection

    async def feed(self, update: Dict[str, Any]) -> None:
        update.setdefault("update_id", next(self._ids))
        await self.dispatcher.feed_update(self.bot, Update.model_validate(update, context={"bot": self.bot}))

    async def connect(self, **kwargs: Any) -> BusinessConnection:
        connection = self.set_connection(**kwargs)
        await self.feed({"business_connection": connection_payload(**kwargs)})
        return connection

    def business_update(
        self,
        text: Optional[str] = "Salom, qalesan?",
        *,
        from_id: int = FRIEND_ID,
        chat_id: Optional[int] = None,
        message_id: Optional[int] = None,
        connection_id: Optional[str] = CONNECTION_ID,
        is_bot: bool = False,
        via_business_bot: bool = False,
        first_name: str = "Ali",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        chat = FRIEND_ID if chat_id is None else chat_id
        message: Dict[str, Any] = {
            "message_id": next(self._ids) if message_id is None else message_id,
            "date": int(self.clock()),
            "chat": {"id": chat, "type": "private", "first_name": first_name},
            "from": {"id": from_id, "is_bot": is_bot, "first_name": first_name},
        }
        if connection_id is not None:
            message["business_connection_id"] = connection_id
        if text is not None:
            message["text"] = text
        if via_business_bot:
            message["sender_business_bot"] = {
                "id": 999, "is_bot": True, "first_name": "Assistant", "username": BOT_USERNAME,
            }
        message.update(extra or {})
        return {"business_message": message}

    async def business_message(self, text: Optional[str] = "Salom, qalesan?", **kwargs: Any) -> Any:
        await self.feed(self.business_update(text, **kwargs))
        return self.assistant.outcomes[-1]

    async def direct_message(self, text: str, *, from_id: int = OWNER_ID, first_name: str = "Owner") -> None:
        message: Dict[str, Any] = {
            "message_id": next(self._ids),
            "date": int(self.clock()),
            "chat": {"id": from_id, "type": "private", "first_name": first_name},
            "from": {"id": from_id, "is_bot": False, "first_name": first_name},
            "text": text,
        }
        if text.startswith("/"):
            command = text.split()[0]
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        await self.feed({"message": message})

    def sent_texts(self, chat_id: Optional[int] = None) -> List[str]:
        return [
            m.text for m in self.session.of_type(SendMessage)
            if chat_id is None or int(m.chat_id) == chat_id
        ]


@pytest.fixture
def make_harness(tmp_path: Path) -> Callable[..., Harness]:
    def factory(**kwargs: Any) -> Harness:
        return Harness(tmp_path, **kwargs)
    return factory
