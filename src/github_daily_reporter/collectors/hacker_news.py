"""Verified Show HN repository discovery."""

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
import json
import math
import random
import re
from typing import Any
from urllib.parse import urlparse
import warnings

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
from bs4.element import Comment, NavigableString, Tag
import httpx

from github_daily_reporter.models import CollectorResult, RepoRef, SourceHealth, SourceObservation
from github_daily_reporter.normalize import extract_repo_ref


ALGOLIA_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
FIREBASE_BASE_URL = "https://hacker-news.firebaseio.com/v0"
DEFAULT_MAX_RESPONSE_BYTES = 500_000
DEFAULT_MAX_ATTEMPTS = 3
MAX_ALGOLIA_PAGES = 10
MAX_ALGOLIA_CANDIDATES = 1_000
MAX_SHOWSTORIES_CANDIDATES = 500
MAX_ASCII_INTEGER_DIGITS = 20
MAX_ASCII_INTEGER_VALUE = 10**MAX_ASCII_INTEGER_DIGITS - 1
FIREBASE_FETCH_CONCURRENCY = 10
RETRYABLE_STATUS = {429, 502, 503, 504}
USER_AGENT = "github-daily-reporter/0.1"
_GITHUB_URL_PATTERN = re.compile(r"https://(?:www\.)?github\.com/[^\s<>\"']+", re.IGNORECASE)


class HackerNewsResponseError(ValueError):
    """A Hacker News API response did not have the expected shape."""


class HackerNewsFetchError(RuntimeError):
    """A Hacker News API response could not be fetched safely."""


def extract_github_urls(text: str) -> list[str]:
    """Extract first-seen canonical repository URLs from HTML or plain text."""
    if not isinstance(text, str):
        return []

    urls: list[str] = []
    seen: set[str] = set()

    def append(url: str) -> None:
        ref = _strict_repo_ref(url.rstrip(".,;:!?)]}"))
        if ref is None or ref.canonical_name in seen:
            return
        seen.add(ref.canonical_name)
        urls.append(_canonical_url(ref))

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
        soup = BeautifulSoup(text, "html.parser")
    for node in soup.descendants:
        if isinstance(node, Tag) and node.name == "a":
            href = node.get("href")
            if isinstance(href, str):
                append(href)
        elif (
            isinstance(node, NavigableString)
            and not isinstance(node, Comment)
            and node.parent
            and node.parent.name not in {
                "script",
                "style",
            }
        ):
            for match in _GITHUB_URL_PATTERN.finditer(str(node)):
                append(match.group())
    return urls


def verified_observation(
    hit: dict[str, Any], item: dict[str, Any], observed_at: datetime
) -> list[SourceObservation]:
    """Turn an Algolia hit into an observation only after Firebase verification."""
    if not isinstance(hit, dict) or not isinstance(item, dict):
        return []
    item_id = _positive_id(item.get("id"))
    if (
        item_id is None
        or _positive_id(hit.get("objectID")) != item_id
        or item.get("type") != "story"
        or bool(item.get("dead"))
        or bool(item.get("deleted"))
    ):
        return []

    discovered = _urls_from_hit(hit)
    official = _urls_from_item(item)
    official_refs = {
        ref.canonical_name
        for url in official
        if (ref := _strict_repo_ref(url)) is not None
    }
    observations: list[SourceObservation] = []
    for url in discovered:
        ref = _strict_repo_ref(url)
        if ref is None or ref.canonical_name not in official_refs:
            continue
        observations.append(
            SourceObservation(
                source="hacker_news",
                repository_url=_canonical_url(ref),
                owner=ref.owner,
                name=ref.name,
                observed_at=_normalize_utc(observed_at),
                source_metadata={
                    "item_id": item_id,
                    "points": _nonnegative_metric(item.get("score")),
                    "comments": _nonnegative_metric(item.get("descendants")),
                },
            )
        )
    return observations


async def collect_hacker_news(
    client: httpx.AsyncClient,
    observed_at: datetime,
    lookback_hours: int,
    limit: int,
) -> CollectorResult:
    """Collect recently posted Show HN repositories, verified from Firebase."""
    observed_at_utc = _normalize_utc(observed_at)
    _validate_positive_int(lookback_hours, "lookback_hours")
    _validate_positive_int(limit, "limit")

    try:
        candidates = await _algolia_candidates(client, observed_at_utc, lookback_hours)
        observations = await _fetch_verified_candidates(client, candidates, observed_at_utc, limit)
    except (httpx.HTTPError, HackerNewsFetchError, HackerNewsResponseError) as error:
        observations = await collect_showstories_fallback(
            client, observed_at_utc, limit, lookback_hours=lookback_hours
        )
        return CollectorResult(
            source="hacker_news",
            observations=observations,
            health=SourceHealth(
                source="hacker_news",
                status="degraded",
                item_count=len(observations),
                error=(
                    "Algolia Show HN search failed; Firebase showstories fallback has "
                    f"narrower coverage ({type(error).__name__})"
                ),
            ),
        )

    return CollectorResult(
        source="hacker_news",
        observations=observations,
        health=SourceHealth(source="hacker_news", status="success", item_count=len(observations)),
    )


async def collect_showstories_fallback(
    client: httpx.AsyncClient,
    observed_at: datetime,
    limit: int,
    *,
    lookback_hours: int = 24,
) -> list[SourceObservation]:
    """Collect the current Firebase Show HN listing when Algolia is unavailable."""
    observed_at_utc = _normalize_utc(observed_at)
    _validate_positive_int(limit, "limit")
    _validate_positive_int(lookback_hours, "lookback_hours")
    cutoff = int((observed_at_utc - timedelta(hours=lookback_hours)).timestamp())
    payload = await _fetch_json(client, f"{FIREBASE_BASE_URL}/showstories.json")
    if not isinstance(payload, list):
        raise HackerNewsResponseError("Firebase showstories response was not a list")

    candidates: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for value in payload:
        item_id = _positive_id(value)
        if item_id is None or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        candidates.append({"objectID": str(item_id)})
        if len(candidates) >= MAX_SHOWSTORIES_CANDIDATES:
            break

    observations: list[SourceObservation] = []
    for batch in _batches(candidates, FIREBASE_FETCH_CONCURRENCY):
        items = await asyncio.gather(
            *(_fetch_json(client, f"{FIREBASE_BASE_URL}/item/{candidate['objectID']}.json") for candidate in batch)
        )
        for candidate, item in zip(batch, items, strict=True):
            if not isinstance(item, dict) or not _is_recent_item(item, cutoff):
                continue
            synthetic_hit = candidate | {
                "url": item.get("url"),
                "story_text": item.get("text"),
            }
            observations.extend(verified_observation(synthetic_hit, item, observed_at_utc))
    return _limit_unique_repositories(observations, limit)


async def _algolia_candidates(
    client: httpx.AsyncClient,
    observed_at: datetime,
    lookback_hours: int,
) -> list[dict[str, Any]]:
    cutoff = int((observed_at - timedelta(hours=lookback_hours)).timestamp())
    candidates: list[dict[str, Any]] = []
    seen_pairs: set[tuple[int, str]] = set()

    for page in range(MAX_ALGOLIA_PAGES):
        params: dict[str, str | int] = {
            "tags": "show_hn",
            "numericFilters": f"created_at_i>{cutoff}",
            "hitsPerPage": 100,
        }
        if page:
            params["page"] = page
        payload = await _fetch_json(client, ALGOLIA_SEARCH_URL, params=params)
        hits, page_count = _algolia_page(payload)

        for hit in hits:
            if not isinstance(hit, dict):
                continue
            item_id = _positive_id(hit.get("objectID"))
            if item_id is None:
                continue
            for url in _urls_from_hit(hit):
                ref = _strict_repo_ref(url)
                if ref is None or (item_id, ref.canonical_name) in seen_pairs:
                    continue
                seen_pairs.add((item_id, ref.canonical_name))
                candidates.append(hit | {"url": _canonical_url(ref), "story_text": ""})
                if len(candidates) >= MAX_ALGOLIA_CANDIDATES:
                    return candidates

        if not hits or page + 1 >= page_count:
            break
    return candidates


async def _fetch_verified_candidates(
    client: httpx.AsyncClient,
    candidates: list[dict[str, Any]],
    observed_at: datetime,
    limit: int,
) -> list[SourceObservation]:
    observations: list[SourceObservation] = []
    groups = _candidate_item_groups(candidates)
    for batch in _batches(groups, FIREBASE_FETCH_CONCURRENCY):
        items = await asyncio.gather(
            *(_fetch_json(client, f"{FIREBASE_BASE_URL}/item/{group['objectID']}.json") for group in batch)
        )
        for group, item in zip(batch, items, strict=True):
            if not isinstance(item, dict):
                continue
            for candidate in group["candidates"]:
                observations.extend(verified_observation(candidate, item, observed_at))
    return _limit_unique_repositories(observations, limit)


async def _fetch_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str | int] | None = None,
) -> Any:
    for attempt in range(1, DEFAULT_MAX_ATTEMPTS + 1):
        try:
            async with client.stream(
                "GET", url, params=params, headers={"User-Agent": USER_AGENT}, timeout=20
            ) as response:
                if response.status_code not in RETRYABLE_STATUS:
                    response.raise_for_status()
                    return _decode_json(await _read_response_bytes(response))
                if attempt == DEFAULT_MAX_ATTEMPTS:
                    response.raise_for_status()
                delay = _retry_delay(response, attempt)
        except httpx.TransportError:
            if attempt == DEFAULT_MAX_ATTEMPTS:
                raise
            delay = min(2 ** (attempt - 1) + random.uniform(0, 0.25), 10.0)
        await asyncio.sleep(delay)
    raise RuntimeError("unreachable")


async def _read_response_bytes(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > DEFAULT_MAX_RESPONSE_BYTES:
            raise HackerNewsFetchError("Hacker News response exceeded maximum size")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_json(data: bytes) -> Any:
    try:
        return json.loads(data)
    except (UnicodeDecodeError, ValueError) as error:
        raise HackerNewsResponseError("Hacker News response was not valid JSON") from error


def _algolia_page(payload: Any) -> tuple[list[Any], int]:
    if not isinstance(payload, dict):
        raise HackerNewsResponseError("Algolia Show HN response was not an object")
    hits = payload.get("hits")
    pages = payload.get("nbPages")
    if (
        not isinstance(hits, list)
        or isinstance(pages, bool)
        or not isinstance(pages, int)
        or pages < 0
    ):
        raise HackerNewsResponseError("Algolia Show HN response had an invalid page shape")
    return hits, pages


def _urls_from_hit(hit: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    if isinstance(hit.get("url"), str):
        urls.extend(extract_github_urls(hit["url"]))
    if isinstance(hit.get("story_text"), str):
        urls.extend(extract_github_urls(hit["story_text"]))
    return _deduplicated_urls(urls)


def _urls_from_item(item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    if isinstance(item.get("url"), str):
        urls.extend(extract_github_urls(item["url"]))
    if isinstance(item.get("text"), str):
        urls.extend(extract_github_urls(item["text"]))
    return _deduplicated_urls(urls)


def _deduplicated_urls(urls: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for url in urls:
        ref = _strict_repo_ref(url)
        if ref is None or ref.canonical_name in seen:
            continue
        seen.add(ref.canonical_name)
        result.append(_canonical_url(ref))
    return result


def _strict_repo_ref(url: str) -> RepoRef | None:
    """Validate a URL as exactly an owner/repository path before normalizing it."""
    try:
        path = urlparse(url).path.strip("/")
    except ValueError:
        return None
    segments = path.split("/")
    if len(segments) != 2 or not all(segments):
        return None
    return extract_repo_ref(url)


def _canonical_url(ref: RepoRef) -> str:
    return f"https://github.com/{ref.owner}/{ref.name}"


def _positive_id(value: object) -> int | None:
    return _ascii_integer(value, minimum=1)


def _timestamp(value: object) -> int | None:
    return _ascii_integer(value, minimum=0)


def _ascii_integer(value: object, *, minimum: int) -> int | None:
    """Parse bounded ASCII decimal values without delegating unbounded input to ``int``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if minimum <= value <= MAX_ASCII_INTEGER_VALUE else None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_ASCII_INTEGER_DIGITS
        or not value.isascii()
        or not value.isdecimal()
    ):
        return None
    parsed = int(value)
    return parsed if parsed >= minimum else None


def _nonnegative_metric(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _normalize_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _validate_positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _batches(values: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _candidate_item_groups(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group already-deduplicated repository targets so each Firebase item is fetched once."""
    groups: dict[int, list[dict[str, Any]]] = {}
    for candidate in candidates:
        item_id = _positive_id(candidate.get("objectID"))
        if item_id is not None:
            groups.setdefault(item_id, []).append(candidate)
    return [
        {"objectID": str(item_id), "candidates": item_candidates}
        for item_id, item_candidates in groups.items()
    ]


def _is_recent_item(item: dict[str, Any], cutoff: int) -> bool:
    item_time = _timestamp(item.get("time"))
    return item_time is not None and item_time >= cutoff


def _limit_unique_repositories(
    observations: list[SourceObservation], limit: int
) -> list[SourceObservation]:
    """Keep all submissions for the first ``limit`` verified repository identities."""
    retained: list[SourceObservation] = []
    repositories: set[str] = set()
    for observation in observations:
        identity = f"{observation.owner}/{observation.name}".lower()
        if identity not in repositories:
            if len(repositories) >= limit:
                continue
            repositories.add(identity)
        retained.append(observation)
    return retained


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
