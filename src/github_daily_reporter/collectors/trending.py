"""GitHub Trending discovery collector."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
import math
import random
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import httpx

from github_daily_reporter.models import CollectorResult, SourceHealth, SourceObservation
from github_daily_reporter.normalize import extract_repo_ref


TRENDING_URL = "https://github.com/trending"
DEFAULT_MAX_RESPONSE_BYTES = 2_000_000
DEFAULT_MAX_ATTEMPTS = 3
RETRYABLE_STATUS = {429, 502, 503, 504}
USER_AGENT = "github-daily-reporter/0.1"
_COUNT_PATTERN = re.compile(r"[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?[kKmM]?")
_STARS_TODAY_PATTERN = re.compile(r"stars\s+today", re.IGNORECASE)


class TrendingParseError(ValueError):
    """GitHub Trending HTML did not contain recognizable repository rows."""


class TrendingFetchError(RuntimeError):
    """GitHub Trending could not be fetched safely."""


def parse_count(text: str) -> int:
    """Extract one well-formed ASCII GitHub counter from surrounding label text."""
    match = _COUNT_PATTERN.search(text)
    if match is None or any(character.isnumeric() for character in text[: match.start()] + text[match.end() :]):
        raise ValueError("count text did not contain an integer")
    count = match.group()
    suffix = count[-1].lower() if count[-1].lower() in {"k", "m"} else ""
    number = count[:-1] if suffix else count
    if "." in number and not suffix:
        raise ValueError("decimal count must use a suffix")
    value = float(number.replace(",", ""))
    if not math.isfinite(value):
        raise ValueError("count was not finite")
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[suffix]
    return int(value * multiplier)


def _optional_count(node_text: str | None) -> int | None:
    if node_text is None:
        return None
    try:
        return parse_count(node_text)
    except ValueError:
        return None


def parse_trending(
    html: str, observed_at: datetime, *, limit: int = 100
) -> list[SourceObservation]:
    """Parse repository observations from a GitHub Trending document."""
    soup = BeautifulSoup(html, "html.parser")
    observations: list[SourceObservation] = []

    for rank, row in enumerate(soup.select("article.Box-row"), start=1):
        if len(observations) >= limit:
            break
        link = row.select_one("h2 a")
        if link is None:
            continue
        href = link.get("href")
        if not isinstance(href, str):
            continue
        ref = extract_repo_ref(urljoin(TRENDING_URL, href))
        if ref is None:
            continue

        description = row.select_one("p")
        language = row.find("span", itemprop="programmingLanguage")
        counters = row.select("a.Link--muted")
        daily = row.find(string=_STARS_TODAY_PATTERN)
        observations.append(
            SourceObservation(
                source="trending",
                repository_url=f"https://github.com/{ref.owner}/{ref.name}",
                owner=ref.owner,
                name=ref.name,
                observed_at=observed_at,
                source_rank=rank,
                source_metadata={
                    "description": description.get_text(" ", strip=True) if description else None,
                    "language": language.get_text(" ", strip=True) if language else None,
                    "stars_total": _optional_count(
                        counters[0].get_text(" ", strip=True) if len(counters) > 0 else None
                    ),
                    "forks_total": _optional_count(
                        counters[1].get_text(" ", strip=True) if len(counters) > 1 else None
                    ),
                    "stars_today": _optional_count(daily),
                },
            )
        )

    if not observations:
        raise TrendingParseError("no repository rows recognized in GitHub Trending HTML")
    return observations


async def collect_trending(
    client: httpx.AsyncClient,
    observed_at: datetime,
    *,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> CollectorResult:
    """Fetch GitHub's daily Trending page and return its repository observations."""
    if max_response_bytes < 0:
        raise ValueError("max_response_bytes must not be negative")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least one")

    html = await _fetch_trending_html(client, max_response_bytes, max_attempts, sleep)
    observations = parse_trending(html, observed_at)
    return CollectorResult(
        source="trending",
        observations=observations,
        health=SourceHealth(source="trending", status="success", item_count=len(observations)),
    )


async def _fetch_trending_html(
    client: httpx.AsyncClient,
    max_response_bytes: int,
    max_attempts: int,
    sleep: Callable[[float], Awaitable[object]],
) -> str:
    for attempt in range(1, max_attempts + 1):
        async with client.stream(
            "GET",
            TRENDING_URL,
            params={"since": "daily"},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        ) as response:
            if response.status_code not in RETRYABLE_STATUS:
                response.raise_for_status()
                return await _read_response_text(response, max_response_bytes)
            if attempt == max_attempts:
                response.raise_for_status()
            delay = _retry_delay(response, attempt)
        await sleep(delay)
    raise RuntimeError("unreachable")


async def _read_response_text(response: httpx.Response, max_response_bytes: int) -> str:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_response_bytes:
            raise TrendingFetchError("GitHub Trending response exceeded maximum size")
        chunks.append(chunk)
    return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            delay = float(retry_after)
        except ValueError:
            delay = -1
        if math.isfinite(delay) and delay >= 0:
            return min(delay, 60.0)
    return min(2 ** (attempt - 1) + random.uniform(0, 0.25), 10.0)
