import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from github_daily_reporter.collectors.hacker_news import (
    ALGOLIA_SEARCH_URL,
    FIREBASE_BASE_URL,
    HackerNewsFetchError,
    collect_hacker_news,
    collect_showstories_fallback,
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


def test_extract_github_urls_keeps_valid_anchor_targets_in_document_order():
    urls = extract_github_urls(
        '<p>https://github.com/Visible/Repo <a href="https://github.com/Linked/Project.git">'
        "project</a> <a href=\"https://github.com/Linked/Project/issues/1\">bad</a>"
        '<a href="javascript:alert(1)">also bad</a></p>'
    )

    assert urls == ["https://github.com/Visible/Repo", "https://github.com/Linked/Project"]


def test_extract_github_urls_ignores_urls_in_html_comments():
    assert extract_github_urls("<!-- https://github.com/Hidden/Project --><p>no link</p>") == []


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

    assert verified_observation(hit, item, OBSERVED_AT) == []


def test_verified_observation_uses_official_firebase_metrics_not_algolia_values():
    observations = verified_observation(
        fixture("hn_algolia.json")["hits"][0],
        fixture("hn_firebase_item.json"),
        OBSERVED_AT,
    )

    assert len(observations) == 1
    assert observations[0].repository_url == "https://github.com/Example-Org/First-Repo"
    assert observations[0].source_metadata == {"item_id": 101, "points": 42, "comments": 11}


def test_verified_observation_rejects_id_mismatch_and_unverified_repository_reference():
    hit = fixture("hn_algolia.json")["hits"][0]
    mismatched = fixture("hn_firebase_item.json") | {"id": 102}
    unrelated = fixture("hn_firebase_item.json") | {"url": "https://github.com/Other/Project"}

    assert verified_observation(hit, mismatched, OBSERVED_AT) == []
    assert verified_observation(hit, unrelated, OBSERVED_AT) == []


def test_verified_observation_returns_every_repository_in_deterministic_order():
    hit = {
        "objectID": "105",
        "url": "https://github.com/Acme/Tool",
        "story_text": "and https://github.com/Other/Project",
    }
    item = {
        "id": 105,
        "type": "story",
        "url": "https://github.com/Acme/Tool",
        "text": "and https://github.com/Other/Project",
        "score": 7,
        "descendants": 4,
    }

    observations = verified_observation(hit, item, OBSERVED_AT)

    assert [observation.repository_url for observation in observations] == [
        "https://github.com/Acme/Tool",
        "https://github.com/Other/Project",
    ]
    assert [observation.source_metadata for observation in observations] == [
        {"item_id": 105, "points": 7, "comments": 4},
        {"item_id": 105, "points": 7, "comments": 4},
    ]


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
async def test_collect_hacker_news_keeps_both_repositories_from_one_verified_story():
    payload = {
        "nbPages": 1,
        "hits": [
            {
                "objectID": "106",
                "story_text": "https://github.com/Acme/Tool and https://github.com/Other/Project",
            }
        ],
    }
    item = {
        "id": 106,
        "type": "story",
        "text": "https://github.com/Acme/Tool and https://github.com/Other/Project",
    }
    respx.get(ALGOLIA_SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))
    firebase = respx.get(f"{FIREBASE_BASE_URL}/item/106.json").mock(
        return_value=httpx.Response(200, json=item)
    )

    async with httpx.AsyncClient() as client:
        result = await collect_hacker_news(client, OBSERVED_AT, lookback_hours=24, limit=2)

    assert firebase.call_count == 1
    assert [observation.repository_url for observation in result.observations] == [
        "https://github.com/Acme/Tool",
        "https://github.com/Other/Project",
    ]


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
async def test_showstories_fallback_keeps_two_repositories_from_one_recent_story():
    observed_timestamp = int(OBSERVED_AT.timestamp())
    respx.get(f"{FIREBASE_BASE_URL}/showstories.json").mock(return_value=httpx.Response(200, json=[107]))
    respx.get(f"{FIREBASE_BASE_URL}/item/107.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 107,
                "type": "story",
                "time": observed_timestamp,
                "text": "https://github.com/Acme/Tool and https://github.com/Other/Project",
            },
        )
    )

    async with httpx.AsyncClient() as client:
        observations = await collect_showstories_fallback(
            client, OBSERVED_AT, limit=2, lookback_hours=24
        )

    assert [observation.repository_url for observation in observations] == [
        "https://github.com/Acme/Tool",
        "https://github.com/Other/Project",
    ]


@pytest.mark.asyncio
@respx.mock
async def test_showstories_fallback_skips_old_items_and_keeps_cutoff_boundary():
    cutoff = int((OBSERVED_AT - timedelta(hours=24)).timestamp())
    respx.get(f"{FIREBASE_BASE_URL}/showstories.json").mock(return_value=httpx.Response(200, json=[108, 109]))
    respx.get(f"{FIREBASE_BASE_URL}/item/108.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 108,
                "type": "story",
                "time": cutoff - 1,
                "url": "https://github.com/Old/Project",
            },
        )
    )
    respx.get(f"{FIREBASE_BASE_URL}/item/109.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 109,
                "type": "story",
                "time": cutoff,
                "url": "https://github.com/Current/Project",
            },
        )
    )

    async with httpx.AsyncClient() as client:
        observations = await collect_showstories_fallback(
            client, OBSERVED_AT, limit=2, lookback_hours=24
        )

    assert [observation.repository_url for observation in observations] == [
        "https://github.com/Current/Project"
    ]


@pytest.mark.asyncio
@respx.mock
async def test_collect_hacker_news_skips_oversized_algolia_ids_without_raw_value_errors():
    respx.get(ALGOLIA_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"nbPages": 1, "hits": [{"objectID": "9" * 10_000}]})
    )

    async with httpx.AsyncClient() as client:
        result = await collect_hacker_news(client, OBSERVED_AT, lookback_hours=24, limit=1)

    assert result.observations == []
    assert result.health.status == "success"


def test_verified_observation_rejects_oversized_numeric_firebase_ids():
    oversized_id = 10**30

    assert (
        verified_observation(
            {"objectID": oversized_id, "url": "https://github.com/Acme/Tool"},
            {"id": oversized_id, "type": "story", "url": "https://github.com/Acme/Tool"},
            OBSERVED_AT,
        )
        == []
    )


@pytest.mark.asyncio
@respx.mock
async def test_collect_hacker_news_verifies_later_hit_after_dead_same_repo_hit():
    payload = {
        "nbPages": 1,
        "hits": [
            {"objectID": "201", "url": "https://github.com/acme/tool"},
            {"objectID": "202", "url": "https://github.com/acme/tool"},
        ],
    }
    respx.get(ALGOLIA_SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))
    dead = respx.get(f"{FIREBASE_BASE_URL}/item/201.json").mock(
        return_value=httpx.Response(
            200,
            json={"id": 201, "type": "story", "dead": True, "url": "https://github.com/acme/tool"},
        )
    )
    valid = respx.get(f"{FIREBASE_BASE_URL}/item/202.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 202,
                "type": "story",
                "url": "https://github.com/acme/tool",
                "score": 9,
                "descendants": 3,
            },
        )
    )

    async with httpx.AsyncClient() as client:
        result = await collect_hacker_news(client, OBSERVED_AT, lookback_hours=24, limit=1)

    assert dead.called and valid.called
    assert [item.source_metadata["item_id"] for item in result.observations] == [202]


@pytest.mark.asyncio
@respx.mock
async def test_collect_hacker_news_keeps_distinct_submissions_without_crowding_out_repositories():
    payload = {
        "nbPages": 1,
        "hits": [
            {"objectID": "301", "url": "https://github.com/acme/tool"},
            {"objectID": "302", "url": "https://github.com/acme/tool"},
            {"objectID": "303", "url": "https://github.com/other/project"},
        ],
    }
    respx.get(ALGOLIA_SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))
    for item_id, url in [(301, "acme/tool"), (302, "acme/tool"), (303, "other/project")]:
        respx.get(f"{FIREBASE_BASE_URL}/item/{item_id}.json").mock(
            return_value=httpx.Response(
                200,
                json={"id": item_id, "type": "story", "url": f"https://github.com/{url}"},
            )
        )

    async with httpx.AsyncClient() as client:
        result = await collect_hacker_news(client, OBSERVED_AT, lookback_hours=24, limit=2)

    assert [item.source_metadata["item_id"] for item in result.observations] == [301, 302, 303]


@pytest.mark.asyncio
@respx.mock
async def test_collect_hacker_news_fetches_duplicate_object_id_and_repository_once():
    payload = {
        "nbPages": 1,
        "hits": [
            {"objectID": "401", "url": "https://github.com/acme/tool"},
            {"objectID": "401", "url": "https://github.com/acme/tool"},
        ],
    }
    respx.get(ALGOLIA_SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))
    item = respx.get(f"{FIREBASE_BASE_URL}/item/401.json").mock(
        return_value=httpx.Response(
            200,
            json={"id": 401, "type": "story", "url": "https://github.com/acme/tool"},
        )
    )

    async with httpx.AsyncClient() as client:
        result = await collect_hacker_news(client, OBSERVED_AT, lookback_hours=24, limit=1)

    assert item.call_count == 1
    assert [observation.source_metadata["item_id"] for observation in result.observations] == [401]


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
