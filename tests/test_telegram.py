import json
from types import SimpleNamespace

import httpx
import pytest

from github_daily_reporter.state import StateStore
from github_daily_reporter.telegram import (
    TelegramClient,
    deliver_report_parts,
    message_length_utf16,
    split_message,
)


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
        b'{"chat_id":"123","text":"hello","message_thread_id":99}'
    )


@pytest.mark.asyncio
async def test_telegram_sends_plain_text_without_parse_mode(respx_mock):
    route = respx_mock.post("https://api.telegram.org/botbot/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 7}}
    )

    await TelegramClient(_config()).send("hello")

    assert "parse_mode" not in json.loads(route.calls[0].request.content)


@pytest.mark.asyncio
async def test_telegram_rejects_utf16_message_above_limit(respx_mock):
    route = respx_mock.post("https://api.telegram.org/botbot/sendMessage")
    with pytest.raises(ValueError, match="4096"):
        await TelegramClient(_config()).send("😀" * 2049)
    assert route.call_count == 0


def test_message_length_counts_utf16_units():
    assert message_length_utf16("a" * 4096) == 4096
    assert message_length_utf16("😀" * 2048) == 4096


@pytest.mark.asyncio
async def test_report_parts_are_delivered_in_order_and_second_failure_stays_pending(
    respx_mock, tmp_path
):
    route = respx_mock.post("https://api.telegram.org/botbot/sendMessage")
    route.side_effect = [
        httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}),
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(503),
    ]
    store = StateStore(tmp_path / "state.sqlite3")
    result = await deliver_report_parts(
        store,
        TelegramClient(_config()),
        "run-1",
        [(0, "growth"), (1, "mature")],
    )
    assert result.status == "delivery_pending"
    assert result.delivered_parts == [0]
    assert [item.part_index for item in store.pending_deliveries("run-1")] == [1]
    assert [json.loads(call.request.content)["text"] for call in route.calls] == [
        "growth", "mature", "mature", "mature"
    ]


@pytest.mark.asyncio
async def test_same_digest_rerun_does_not_send_already_delivered_parts(respx_mock, tmp_path):
    route = respx_mock.post("https://api.telegram.org/botbot/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 1}}
    )
    store = StateStore(tmp_path / "state.sqlite3")
    client = TelegramClient(_config())
    first = await deliver_report_parts(store, client, "run-1", [(0, "growth"), (1, "mature")])
    second = await deliver_report_parts(store, client, "run-1", [(0, "growth"), (1, "mature")])
    assert first.status == second.status == "delivered"
    assert route.call_count == 2


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
