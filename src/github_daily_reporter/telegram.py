"""Direct Telegram Bot API delivery with bounded, classified retries."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

import httpx


class TelegramClient:
    def __init__(self, config: Any, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client

    async def send(self, text: str) -> str:
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
                await asyncio.sleep(max(0.0, min(delay, 30.0)))
            return "transport"
        finally:
            if owns_client:
                await client.aclose()


def split_message(text: str, *, limit: int = 3800) -> list[str]:
    """Split Markdown between complete ``###`` entries, respecting ``limit``."""
    limit = min(int(limit), 3799)
    if len(text) <= limit:
        return [text]
    blocks = text.split("\n\n")
    parts: list[str] = []
    current = ""
    for block in blocks:
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        if len(block) <= limit:
            current = block
        else:
            for offset in range(0, len(block), limit):
                parts.append(block[offset : offset + limit])
    if current:
        parts.append(current)
    return parts or [""]


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
