import json
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

    assert route.calls[0].request.content == (
        b'{"chat_id":"123","text":"hello","parse_mode":"MarkdownV2","message_thread_id":99}'
    )


@pytest.mark.asyncio
async def test_telegram_sends_markdown_v2_parse_mode(respx_mock):
    route = respx_mock.post("https://api.telegram.org/botbot/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 7}}
    )

    await TelegramClient(_config()).send("hello")

    assert json.loads(route.calls[0].request.content)["parse_mode"] == "MarkdownV2"


@pytest.mark.asyncio
async def test_telegram_honors_429_retry_after_without_capping(monkeypatch, respx_mock):
    route = respx_mock.post("https://api.telegram.org/botbot/sendMessage")
    route.side_effect = [
        httpx.Response(429, json={"parameters": {"retry_after": 45}}),
        httpx.Response(200, json={"ok": True, "result": {"message_id": 42}}),
    ]
    delays = []

    async def record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("github_daily_reporter.telegram.asyncio.sleep", record_sleep)

    assert await TelegramClient(_config()).send("hello") == "42"
    assert delays == [45]


def test_split_message_breaks_only_between_entries():
    entries = ["## title", "### 1. one\n" + "x" * 30, "### 2. two\n" + "y" * 30]
    parts = split_message("\n\n".join(entries), limit=45)
    assert len(parts) == 3
    assert all(len(part) < 3800 for part in parts)
    assert all("### " not in part or part.count("### ") == 1 for part in parts)


def test_split_message_rejects_an_entry_that_exceeds_telegram_limit():
    entry = "### 1. oversized\n" + "x" * 3800

    with pytest.raises(ValueError, match="^message_entry_too_large$"):
        split_message("## title\n\n" + entry)
