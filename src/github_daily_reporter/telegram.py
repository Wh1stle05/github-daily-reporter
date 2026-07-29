"""Direct Telegram Bot API delivery with bounded, classified retries."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx


TELEGRAM_MAX_UTF16_UNITS = 4096
DELIVERY_FAILURE_CATEGORIES = {
    "timeout",
    "transport",
    "http_status",
    "http_429",
    "http_5xx",
    "invalid_response",
}


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    delivered_parts: list[int]
    error_category: str | None = None


class TelegramClient:
    def __init__(self, config: Any, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client

    async def send(self, text: str) -> str:
        if message_length_utf16(text) > TELEGRAM_MAX_UTF16_UNITS:
            raise ValueError("message exceeds Telegram 4096 UTF-16 unit limit")
        token = _secret(self.config.telegram_bot_token)
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": str(self.config.telegram_chat_id),
            "text": text,
        }
        thread_id = getattr(self.config, "telegram_message_thread_id", None)
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        timeout = float(getattr(self.config, "telegram_timeout_seconds", 15))
        attempts = int(getattr(self.config, "telegram_max_attempts", 3))
        base_delay = float(getattr(self.config, "telegram_retry_base_seconds", 2))

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=timeout)
        try:
            for attempt in range(attempts):
                try:
                    response = await client.post(url, json=payload)
                except httpx.TimeoutException:
                    category = "timeout"
                    retry = True
                    retry_after = None
                except httpx.RequestError:
                    category = "transport"
                    retry = True
                    retry_after = None
                else:
                    if response.status_code == 429:
                        category, retry = "http_429", True
                        retry_after = _retry_after(response)
                    elif 500 <= response.status_code <= 599:
                        category, retry = "http_5xx", True
                        retry_after = _retry_after(response)
                    elif response.status_code >= 400:
                        return "http_status"
                    else:
                        try:
                            body = response.json()
                            message_id = body["result"]["message_id"]
                            return str(message_id)
                        except (ValueError, KeyError, TypeError):
                            return "invalid_response"
                if not retry or attempt >= attempts - 1:
                    return category
                delay = retry_after if retry_after is not None else base_delay * (2**attempt)
                await asyncio.sleep(max(0.0, delay))
            return "transport"
        finally:
            if owns_client:
                await client.aclose()


def split_message(text: str, *, limit: int = 3800) -> list[str]:
    """Split plain text between entries, respecting UTF-16 Telegram units."""
    limit = min(int(limit), TELEGRAM_MAX_UTF16_UNITS)
    if message_length_utf16(text) <= limit:
        return [text]
    blocks = text.split("\n\n")
    parts: list[str] = []
    current = ""
    for block in blocks:
        candidate = block if not current else f"{current}\n\n{block}"
        if message_length_utf16(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        if message_length_utf16(block) <= limit:
            current = block
        else:
            raise ValueError("message_entry_too_large")
    if current:
        parts.append(current)
    return parts or [""]


def message_length_utf16(text: str) -> int:
    """Return Telegram's UTF-16 code-unit length for a Python string."""
    return len(text.encode("utf-16-le")) // 2


async def deliver_report_parts(
    store: Any,
    client: TelegramClient,
    run_id: str,
    parts: Iterable[tuple[int, str]],
) -> DeliveryResult:
    """Persist and deliver report parts strictly in index order."""
    prepared = sorted((int(index), body) for index, body in parts)
    store.enqueue_delivery_batch(run_id, prepared)
    delivered: list[int] = []
    failure: str | None = None
    for part_index, _ in prepared:
        part = store.get_delivery_part(run_id, part_index)
        if part.state == "delivered":
            delivered.append(part_index)
            continue
        claim = store.claim_delivery(run_id, part_index)
        if claim is None:
            failure = "delivery_claim_unavailable"
            break
        try:
            message_id = await client.send(claim.body)
        except ValueError:
            message_id = "message_entry_too_large"
        if message_id in DELIVERY_FAILURE_CATEGORIES:
            store.mark_delivery_pending(run_id, part_index, message_id, claim.claim_token)
            failure = message_id
            break
        if not store.mark_delivery_delivered(
            run_id, part_index, message_id, claim.claim_token
        ):
            failure = "delivery_claim_lost"
            break
        delivered.append(part_index)
    status = "delivered" if len(delivered) == len(prepared) else "delivery_pending"
    return DeliveryResult(status, delivered, failure)


def _secret(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if getter else value)


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if header is not None:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
    try:
        value = response.json().get("parameters", {}).get("retry_after")
        return max(0.0, float(value)) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None
