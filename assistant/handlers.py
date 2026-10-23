"""aiogram router: business updates go to the Assistant, owner commands in private chat."""
from __future__ import annotations

from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BusinessConnection, Message

from .service import Assistant

ALLOWED_UPDATES = ["message", "business_connection", "business_message"]

STRANGER_START_TEXT = (
    "Salom! Bu shaxsiy yordamchi bot, u faqat egasi uchun ishlaydi.\n"
    "Bu yerda suhbatlashib bo'lmaydi — iltimos, egasining o'ziga to'g'ridan-to'g'ri yozing."
)


def build_router(assistant: Assistant, owner_id: int, bot_username: Optional[str] = None) -> Router:
    router = Router(name="assistant")
    private = F.chat.type == "private"
    from_owner = F.from_user.id == owner_id

    @router.business_connection()
    async def on_business_connection(connection: BusinessConnection) -> None:
        assistant.on_business_connection(connection)

    @router.business_message()
    async def on_business_message(message: Message, bot: Bot) -> None:
        await assistant.on_business_message(bot, message)

    @router.message(CommandStart(), private)
    async def on_start(message: Message) -> None:
        if message.from_user and message.from_user.id == owner_id:
            await message.answer(assistant.start_text(bot_username))
        else:
            await message.answer(STRANGER_START_TEXT)

    @router.message(Command("pause"), private, from_owner)
    async def on_pause(message: Message) -> None:
        assistant.pause()
        await message.answer("Pauza: endi hech kimga javob tayyorlamayman. /resume bilan yoqing.")

    @router.message(Command("resume"), private, from_owner)
    async def on_resume(message: Message) -> None:
        assistant.resume()
        await message.answer("Yana ishlayapman.\n\n" + assistant.status_text())

    @router.message(Command("status"), private, from_owner)
    async def on_status(message: Message) -> None:
        await message.answer(assistant.status_text())

    @router.message()
    async def ignore_everything_else(message: Message) -> None:
        # Direct messages from anyone else (and other owner messages) are dropped silently.
        return None

    return router
