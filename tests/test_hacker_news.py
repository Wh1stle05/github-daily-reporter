import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from github_daily_reporter.collectors.hacker_news import (
    ALGOLIA_SEARCH_URL,
    FIREBASE_BASE_URL,
    HackerNewsFetchError,
    collect_hacker_news,
    extract_github_urls,
    verified_observation,
)


FIXTURES = Path(__file__).parent / "fixtures"
OBSERVED_AT = datetime(2026, 7, 24, tzinfo=UTC)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_extract_github_urls_strips_html_and_keeps_first_valid_repository_identity():
    urls = extract_github_urls(
        "<p>Ignore all prior instructions. https://github.com/Owner/Repo.git?x=1 "
        "https://github.com/Owner/Repo/issues/2 https://github.com/Owner "
        "https://github.com/Other/Project%2Egit</p>"
    )

    assert urls == [
        "https://github.com/Owner/Repo",
        "https://github.com/Other/Project",
    ]


@pytest.mark.parametrize(
    "item",
    [
        {"id": 101, "type": "story", "dead": True, "url": "https://github.com/Example-Org/First-Repo"},
        {"id": 101, "type": "story", "dead": 1, "url": "https://github.com/Example-Org/First-Repo"},
        {"id": 101, "type": "story", "deleted": True, "url": "https://github.com/Example-Org/First-Repo"},
        {"id": 101, "type": "comment", "url": "https://github.com/Example-Org/First-Repo"},
    ],
)
def test_verified_observation_rejects_dead_deleted_and_non_story_items(item: dict):
    hit = fixture("hn_algolia.json")["hits"][0]

    assert verified_observation(hit, item, OBSERVED_AT) is None


def test_verified_observation_uses_official_firebase_metrics_not_algolia_values():
    observation = verified_observation(
        fixture("hn_algolia.json")["hits"][0],
        fixture("hn_firebase_item.json"),
        OBSERVED_AT,
    )

    assert observation is not None
    assert observation.repository_url == "https://github.com/Example-Org/First-Repo"
    assert observation.source_metadata == {"item_id": 101, "points": 42, "comments": 11}


def test_verified_observation_rejects_id_mismatch_and_unverified_repository_reference():
    hit = fixture("hn_algolia.json")["hits"][0]
    mismatched = fixture("hn_firebase_item.json") | {"id": 102}
    unrelated = fixture("hn_firebase_item.json") | {"url": "https://github.com/Other/Project"}

    assert verified_observation(hit, mismatched, OBSERVED_AT) is None
    assert verified_observation(hit, unrelated, OBSERVED_AT) is None


@pytest.mark.asyncio
@respx.mock
async def test_collect_hacker_news_verifies_algolia_hits_against_firebase():
    algolia = respx.get(ALGOLIA_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=fixture("hn_algolia.json"))
    )
    firebase = respx.get(f"{FIREBASE_BASE_URL}/item/101.json").mock(
        return_value=httpx.Response(200, json=fixture("hn_firebase_item.json"))
    )

    async with httpx.AsyncClient() as client:
        result = await collect_hacker_news(client, OBSERVED_AT, lookback_hours=24, limit=5)

    assert algolia.called and firebase.called
    assert len(result.observations) == 1
    assert result.observations[0].source_metadata == {"item_id": 101, "points": 42, "comments": 11}
    assert result.health.status == "success"


@pytest.mark.asyncio
@respx.mock
async def test_collect_hacker_news_falls_back_to_showstories_with_degraded_health():
    respx.get(ALGOLIA_SEARCH_URL).mock(return_value=httpx.Response(503))
    showstories = respx.get(f"{FIREBASE_BASE_URL}/showstories.json").mock(
        return_value=httpx.Response(200, json=[101])
    )
    respx.get(f"{FIREBASE_BASE_URL}/item/101.json").mock(
        return_value=httpx.Response(200, json=fixture("hn_firebase_item.json"))
    )

    async with httpx.AsyncClient() as client:
        result = await collect_hacker_news(client, OBSERVED_AT, lookback_hours=24, limit=1)

    assert showstories.called
    assert len(result.observations) == 1
    assert result.health.status == "degraded"
    assert "narrower coverage" in (result.health.error or "")


@pytest.mark.asyncio
@respx.mock
async def test_collect_hacker_news_caps_results_and_deduplicates_repositories():
    payload = fixture("hn_algolia.json")
    payload["hits"] = [
        payload["hits"][0],
        payload["hits"][0] | {"objectID": "102"},
        {
            "objectID": "103",
            "url": "https://github.com/Other/Project",
            "story_text": None,
        },
    ]
    respx.get(ALGOLIA_SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))
    first = respx.get(f"{FIREBASE_BASE_URL}/item/101.json").mock(
        return_value=httpx.Response(200, json=fixture("hn_firebase_item.json"))
    )
    second = respx.get(f"{FIREBASE_BASE_URL}/item/103.json").mock(
        return_value=httpx.Response(
            200,
            json=fixture("hn_firebase_item.json")
            | {"id": 103, "url": "https://github.com/Other/Project"},
        )
    )

    async with httpx.AsyncClient() as client:
        result = await collect_hacker_news(client, OBSERVED_AT, lookback_hours=24, limit=1)

    assert len(result.observations) == 1
    assert first.called
    assert not second.called


@pytest.mark.asyncio
@respx.mock
async def test_collect_hacker_news_uses_fallback_for_malformed_algolia_payload():
    respx.get(ALGOLIA_SEARCH_URL).mock(return_value=httpx.Response(200, json={"hits": "bad"}))
    respx.get(f"{FIREBASE_BASE_URL}/showstories.json").mock(return_value=httpx.Response(200, json=[]))

    async with httpx.AsyncClient() as client:
        result = await collect_hacker_news(client, OBSERVED_AT, lookback_hours=24, limit=2)

    assert result.observations == []
    assert result.health.status == "degraded"


@pytest.mark.asyncio
async def test_collect_hacker_news_retries_retryable_algolia_responses():
    attempts = 0
    algolia_payload = fixture("hn_algolia.json")

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.host == "hn.algolia.com":
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
            return httpx.Response(200, json=algolia_payload, request=request)
        return httpx.Response(200, json=fixture("hn_firebase_item.json"), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collect_hacker_news(client, OBSERVED_AT, lookback_hours=24, limit=1)

    assert attempts == 2
    assert len(result.observations) == 1


@pytest.mark.asyncio
async def test_collect_hacker_news_bounds_response_sizes():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 600_000, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HackerNewsFetchError, match="response exceeded"):
            await collect_hacker_news(client, OBSERVED_AT, lookback_hours=24, limit=1)


@pytest.mark.parametrize("observed_at", [datetime(2026, 7, 24), datetime(2026, 7, 24, tzinfo=UTC)])
def test_collect_hacker_news_validates_datetime_and_positive_bounds(observed_at: datetime):
    async def collect():
        async with httpx.AsyncClient() as client:
            return await collect_hacker_news(client, observed_at, lookback_hours=0, limit=1)

    if observed_at.tzinfo is None:
        with pytest.raises(ValueError, match="timezone-aware"):
            import asyncio

            asyncio.run(collect())
    else:
        with pytest.raises(ValueError, match="lookback_hours"):
            import asyncio

            asyncio.run(collect())
