from types import SimpleNamespace

import httpx
import pytest

from github_daily_reporter.telegram import TelegramClient, split_message


def _config(**overrides):
    values = dict(
        telegram_bot_token="bot",
        telegram_chat_id="123",
        telegram_message_thread_id=None,
        telegram_timeout_seconds=1,
        telegram_max_attempts=3,
        telegram_retry_base_seconds=0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_telegram_retries_503_then_succeeds(respx_mock):
    route = respx_mock.post("https://api.telegram.org/botbot/sendMessage")
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(200, json={"ok": True, "result": {"message_id": 42}}),
    ]

    assert await TelegramClient(_config()).send("hello") == "42"
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_telegram_sends_optional_thread_id(respx_mock):
    route = respx_mock.post("https://api.telegram.org/botbot/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 7}}
    )

    await TelegramClient(_config(telegram_message_thread_id=99)).send("hello")

    assert route.calls[0].request.content == b'{"chat_id":"123","text":"hello","message_thread_id":99}'


def test_split_message_breaks_only_between_entries():
    entries = ["## title", "### 1. one\n" + "x" * 30, "### 2. two\n" + "y" * 30]
    parts = split_message("\n\n".join(entries), limit=45)
    assert len(parts) == 3
    assert all(len(part) < 3800 for part in parts)
    assert all("### " not in part or part.count("### ") == 1 for part in parts)
