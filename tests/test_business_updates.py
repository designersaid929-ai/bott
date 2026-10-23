from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest
from aiogram.methods import GetBusinessConnection, SendMessage

from assistant.claude_cli import ClaudeAuthError, ClaudeLimitError, ClaudeTimeout
from assistant.service import Outcome, describe

from .conftest import CONNECTION_ID, FRIEND_ID, OWNER_ID, STRANGER_ID

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


# ----- preview mode -----------------------------------------------------------

async def test_preview_shows_reply_and_sends_nothing(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.push("Zo'r, o'zing-chi?")
    outcome = await h.business_message("Salom, qalesan?")

    assert outcome == Outcome.PREVIEWED
    assert len(h.previews) == 1
    assert h.previews[0].reply == "Zo'r, o'zing-chi?"
    assert h.previews[0].incoming == "Salom, qalesan?"
    assert h.requests == []

    lines = (h.data_dir / "previews.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["reply"] == "Zo'r, o'zing-chi?"
    history = h.assistant.history.get(CONNECTION_ID, FRIEND_ID)
    assert [item["role"] for item in history] == ["other"]


@posix_only
async def test_previews_file_is_private(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Salom")
    mode = stat.S_IMODE(os.stat(h.data_dir / "previews.jsonl").st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(os.stat(h.data_dir).st_mode) == 0o700


async def test_unknown_connection_is_looked_up_once_then_cached(make_harness):
    h = make_harness()
    h.set_connection()
    assert await h.business_message("Salom") == Outcome.PREVIEWED
    assert await h.business_message("Qalesan?") == Outcome.PREVIEWED
    assert len(h.requests) == 1
    assert isinstance(h.requests[0], GetBusinessConnection)


async def test_failed_lookup_is_no_connection(make_harness):
    h = make_harness()
    assert await h.business_message("Salom") == Outcome.NO_CONNECTION
    assert h.generator.calls == []


async def test_message_without_connection_id(make_harness):
    h = make_harness()
    assert await h.business_message("Salom", connection_id=None) == Outcome.NO_CONNECTION


async def test_foreign_and_disabled_connections_never_call_claude(make_harness):
    h = make_harness()
    await h.connect(connection_id="foreign", user_id=STRANGER_ID)
    await h.connect(connection_id="off", enabled=False)
    assert await h.business_message("Salom", connection_id="foreign") == Outcome.FOREIGN_CONNECTION
    assert await h.business_message("Salom", connection_id="off") == Outcome.CONNECTION_DISABLED
    assert h.generator.calls == []
    assert h.session.of_type(SendMessage) == []


async def test_any_chat_gets_a_reply_without_allowlist(make_harness):
    h = make_harness()
    await h.connect()
    for chat_id in (FRIEND_ID, 5555, 987654321):
        assert await h.business_message("Salom", from_id=chat_id, chat_id=chat_id) == Outcome.PREVIEWED


# ----- owner and bots -------------------------------------------------------------

async def test_owner_message_is_recorded_and_never_answered(make_harness):
    h = make_harness()
    await h.connect()
    outcome = await h.business_message("Hozir yozaman", from_id=OWNER_ID, chat_id=FRIEND_ID)
    assert outcome == Outcome.OWNER_MESSAGE
    assert h.generator.calls == []
    history = h.assistant.history.get(CONNECTION_ID, FRIEND_ID)
    assert history[-1]["role"] == "owner"
    assert history[-1]["text"] == "Hozir yozaman"


async def test_takeover_keeps_bot_quiet_then_expires(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Men o'zim yozaman", from_id=OWNER_ID)
    assert await h.business_message("Ok, kutaman") == Outcome.OWNER_ACTIVE
    assert h.generator.calls == []

    # Another chat is not affected.
    assert await h.business_message("Salom", from_id=4444, chat_id=4444) == Outcome.PREVIEWED

    h.clock.advance(15 * 60 + 1)
    assert await h.business_message("Hali ham bormisan?") == Outcome.PREVIEWED
    prompt = h.generator.calls[-1]["user"]
    assert "[You]: Men o'zim yozaman" in prompt
    assert "[Ali]: Ok, kutaman" in prompt


async def test_takeover_can_be_disabled(make_harness):
    h = make_harness(OWNER_TAKEOVER_MINUTES="0")
    await h.connect()
    await h.business_message("Salom", from_id=OWNER_ID)
    assert await h.business_message("Salom") == Outcome.PREVIEWED


async def test_bot_messages_are_ignored(make_harness):
    h = make_harness()
    await h.connect()
    assert await h.business_message("Javob", from_id=OWNER_ID, via_business_bot=True) == Outcome.IGNORED_BOT
    assert await h.business_message("I am a bot", from_id=777, is_bot=True) == Outcome.IGNORED_BOT
    assert h.generator.calls == []


# ----- duplicates -------------------------------------------------------------------

async def test_duplicate_updates_are_processed_once(make_harness):
    h = make_harness()
    await h.connect()
    update = h.business_update("Salom", message_id=42)
    await asyncio.gather(h.feed(dict(update)), h.feed(dict(update)))
    await h.feed(dict(update))
    assert len(h.generator.calls) == 1
    assert list(h.assistant.outcomes).count(Outcome.DUPLICATE) == 2

    h.restart()
    await h.feed(dict(update))
    assert h.assistant.outcomes[-1] == Outcome.DUPLICATE
    assert len(h.generator.calls) == 1


async def test_same_message_id_in_other_chat_is_not_duplicate(make_harness):
    h = make_harness()
    await h.connect()
    assert await h.business_message("Salom", message_id=7) == Outcome.PREVIEWED
    assert await h.business_message("Salom", message_id=7, from_id=4444, chat_id=4444) == Outcome.PREVIEWED


# ----- message kinds ------------------------------------------------------------------

async def test_service_message_is_unsupported(make_harness):
    h = make_harness()
    await h.connect()
    outcome = await h.business_message(None, extra={"new_chat_title": "x"})
    assert outcome == Outcome.UNSUPPORTED
    assert h.generator.calls == []


async def test_sticker_and_voice_are_described(make_harness):
    h = make_harness()
    await h.connect()
    sticker = {
        "file_id": "s1", "file_unique_id": "u1", "type": "regular", "width": 512, "height": 512,
        "is_animated": False, "is_video": False, "emoji": "😀",
    }
    voice = {"file_id": "v1", "file_unique_id": "u2", "duration": 3}
    await h.business_message(None, extra={"sticker": sticker})
    await h.business_message(None, extra={"voice": voice})
    texts = [item["text"] for item in h.assistant.history.get(CONNECTION_ID, FRIEND_ID)]
    assert texts == ["[sticker] 😀", "[voice message]"]


def test_describe_photo_with_caption(make_harness):
    from aiogram.types import Message

    message = Message.model_validate({
        "message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"},
        "photo": [{"file_id": "p", "file_unique_id": "p", "width": 1, "height": 1}],
        "caption": "qara",
    })
    assert describe(message) == "[photo] qara"


# ----- history and language -------------------------------------------------------------

async def test_history_is_separate_per_connection_and_chat(make_harness):
    h = make_harness()
    await h.connect()
    await h.connect(connection_id="conn-2")
    await h.business_message("Birinchi chat siri", chat_id=FRIEND_ID)
    await h.business_message("Ikkinchi chat", from_id=4444, chat_id=4444)
    await h.business_message("Boshqa connection", connection_id="conn-2")

    assert "Birinchi chat siri" in h.generator.calls[0]["user"]
    assert "Birinchi chat siri" not in h.generator.calls[1]["user"]
    assert "Birinchi chat siri" not in h.generator.calls[2]["user"]
    assert "Boshqa connection" in h.generator.calls[2]["user"]
    assert h.assistant.history.chat_count() == 3


async def test_language_hint(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Привет, как дела?", from_id=1, chat_id=1)
    await h.business_message("Hi, are you free tonight?", from_id=2, chat_id=2)
    await h.business_message("Bugun bo'shmisan?", from_id=3, chat_id=3)
    await h.business_message("👍", from_id=1, chat_id=1)
    hints = [call["user"].splitlines()[-1] for call in h.generator.calls]
    assert hints == [
        "Their latest message is in Russian.",
        "Their latest message is in English.",
        "Their latest message is in Uzbek.",
        "Their latest message is in Russian.",
    ]
    assert [p.language for p in h.previews] == ["ru", "en", "uz", "ru"]


async def test_skip_is_not_shown(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.push("[SKIP]", "   ")
    assert await h.business_message("Ok") == Outcome.SKIPPED
    assert await h.business_message("👍") == Outcome.SKIPPED
    assert h.previews == []


async def test_profile_is_in_system_prompt(make_harness):
    h = make_harness()
    await h.connect()
    h.profile_path.write_text("<!-- hidden note -->\n# Men haqimda\n- Ism: Jasur\n", encoding="utf-8")
    await h.business_message("Salom")
    system = h.generator.calls[0]["system"]
    assert "<profile>" in system and "Jasur" in system
    assert "hidden note" not in system


# ----- auto-send ----------------------------------------------------------------------------

async def test_auto_send_rechecks_connection_then_sends(make_harness):
    h = make_harness(auto_send=True)
    await h.connect()
    h.generator.push("Yaxshi, o'zing-chi?")
    assert await h.business_message("Salom, qalesan?") == Outcome.SENT

    assert [type(r) for r in h.requests] == [GetBusinessConnection, SendMessage]
    sent = h.requests[1]
    assert sent.business_connection_id == CONNECTION_ID
    assert int(sent.chat_id) == FRIEND_ID
    assert sent.text == "Yaxshi, o'zing-chi?"
    history = h.assistant.history.get(CONNECTION_ID, FRIEND_ID)
    assert history[-1] == {**history[-1], "role": "owner", "text": "Yaxshi, o'zing-chi?"}


async def test_auto_send_splits_long_replies(make_harness):
    h = make_harness(auto_send=True)
    await h.connect()
    h.generator.push("a" * 9000)
    assert await h.business_message("Salom") == Outcome.SENT
    chunks = h.sent_texts(FRIEND_ID)
    assert [len(c) for c in chunks] == [4000, 4000, 1000]


async def test_auto_send_without_reply_right_does_not_send(make_harness):
    h = make_harness(auto_send=True)
    await h.connect()
    h.set_connection(can_reply=False)
    assert await h.business_message("Salom") == Outcome.PERMISSION_DENIED
    assert h.session.of_type(SendMessage) == []
    assert "permission" in h.previews[-1].note


async def test_auto_send_with_disabled_connection_does_not_send(make_harness):
    h = make_harness(auto_send=True)
    await h.connect()
    h.set_connection(enabled=False)
    assert await h.business_message("Salom") == Outcome.PERMISSION_DENIED
    assert h.session.of_type(SendMessage) == []


async def test_auto_send_stops_if_owner_writes_during_generation(make_harness):
    h = make_harness(auto_send=True)
    await h.connect()

    async def owner_writes() -> None:
        h.generator.hook = None
        await h.business_message("Men o'zim javob beraman", from_id=OWNER_ID)

    h.generator.hook = owner_writes
    assert await h.business_message("Salom") == Outcome.OWNER_ACTIVE
    assert h.session.of_type(SendMessage) == []
    assert "owner_active" in h.previews[-1].note


# ----- owner commands ------------------------------------------------------------------------

async def test_pause_and_resume_only_for_owner(make_harness):
    h = make_harness()
    await h.connect()

    await h.direct_message("/pause", from_id=STRANGER_ID)
    assert h.assistant.state.paused is False
    assert h.session.of_type(SendMessage) == []

    await h.direct_message("/pause")
    assert h.assistant.state.paused is True
    assert await h.business_message("Salom") == Outcome.PAUSED

    h.restart()
    assert h.assistant.state.paused is True
    assert await h.business_message("Salom yana") == Outcome.PAUSED

    await h.direct_message("/resume", from_id=STRANGER_ID)
    assert h.assistant.state.paused is True
    await h.direct_message("/resume")
    assert h.assistant.state.paused is False
    assert await h.business_message("Endi-chi?") == Outcome.PREVIEWED
    assert all(int(m.chat_id) == OWNER_ID for m in h.session.of_type(SendMessage))


async def test_strangers_direct_messages_are_ignored(make_harness):
    h = make_harness()
    await h.direct_message("Salom bot", from_id=STRANGER_ID)
    await h.direct_message("/status", from_id=STRANGER_ID)
    await h.direct_message("/resume", from_id=STRANGER_ID)
    assert h.session.of_type(SendMessage) == []
    assert h.generator.calls == []


async def test_status_for_owner(make_harness):
    h = make_harness()
    await h.direct_message("/status")
    text = h.sent_texts(OWNER_ID)[-1]
    assert "PREVIEW" in text and "running" in text


async def test_start_for_owner_is_uzbek_guide(make_harness):
    h = make_harness()
    await h.direct_message("/start")
    text = h.sent_texts(OWNER_ID)[-1]
    assert "Rejim: PREVIEW" in text
    assert "ishlayapti" in text
    assert "Chatbots" in text
    assert "/pause" in text and "/resume" in text and "/status" in text
    assert "AUTO_SEND=true" in text
    assert "@test_assistant_bot" in text


async def test_start_shows_auto_send_and_paused(make_harness):
    h = make_harness(auto_send=True)
    await h.direct_message("/pause")
    await h.direct_message("/start")
    text = h.sent_texts(OWNER_ID)[-1]
    assert "AUTO-SEND" in text
    assert "pauzada" in text
    assert "AUTO_SEND=true" not in text


async def test_start_for_stranger_is_short_and_private(make_harness):
    h = make_harness()
    await h.direct_message("/start", from_id=STRANGER_ID, first_name="Begona")
    texts = h.sent_texts(STRANGER_ID)
    assert len(texts) == 1
    assert "faqat egasi uchun" in texts[0]
    for secret in ("Sardor", "Toshkent", "profile", "Rejim", "/pause"):
        assert secret not in texts[0]
    assert h.generator.calls == []


# ----- limits and errors ----------------------------------------------------------------------

async def test_limit_waits_until_reset_time(make_harness):
    h = make_harness()
    await h.connect()
    resets_at = h.clock() + 3600
    h.generator.push(ClaudeLimitError("limit", resets_at=resets_at))
    assert await h.business_message("Salom") == Outcome.LIMITED
    assert h.assistant.state.limit_until == resets_at

    assert await h.business_message("Javob ber") == Outcome.LIMITED
    assert len(h.generator.calls) == 1

    h.restart()
    assert await h.business_message("Hali ham?") == Outcome.LIMITED
    assert len(h.generator.calls) == 1

    h.clock.advance(3601)
    assert await h.business_message("Endi?") == Outcome.PREVIEWED
    assert h.assistant.state.limit_until is None
    assert h.session.of_type(SendMessage) == []


async def test_limit_without_reset_time_uses_cooldown(make_harness):
    h = make_harness(LIMIT_COOLDOWN_MINUTES="10")
    await h.connect()
    h.generator.push(ClaudeLimitError("limit"))
    assert await h.business_message("Salom") == Outcome.LIMITED
    assert h.assistant.state.limit_until == h.clock() + 600
    h.clock.advance(599)
    assert await h.business_message("Salom?") == Outcome.LIMITED
    h.clock.advance(2)
    assert await h.business_message("Salom!") == Outcome.PREVIEWED


async def test_limit_notifies_owner_in_auto_send_and_resume_clears_it(make_harness):
    h = make_harness(auto_send=True)
    await h.connect()
    h.generator.push(ClaudeLimitError("limit", resets_at=h.clock() + 3600))
    assert await h.business_message("Salom") == Outcome.LIMITED
    owner_notes = h.sent_texts(OWNER_ID)
    assert len(owner_notes) == 1 and "limit" in owner_notes[0]
    assert h.sent_texts(FRIEND_ID) == []

    await h.direct_message("/resume")
    assert h.assistant.state.limit_until is None
    assert await h.business_message("Endi?") == Outcome.SENT


@pytest.mark.parametrize("error", [ClaudeTimeout("too slow"), ClaudeAuthError("not logged in", "detail")])
async def test_errors_send_nothing_to_chat_and_notify_owner_once(make_harness, error):
    h = make_harness(auto_send=True)
    await h.connect()
    h.generator.push(error, error)
    assert await h.business_message("Salom") == Outcome.ERROR
    assert await h.business_message("Salom?") == Outcome.ERROR
    assert h.sent_texts(FRIEND_ID) == []
    assert len(h.sent_texts(OWNER_ID)) == 1


async def test_errors_in_preview_mode_send_nothing(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.push(ClaudeTimeout("too slow"))
    assert await h.business_message("Salom") == Outcome.ERROR
    assert h.requests == []
