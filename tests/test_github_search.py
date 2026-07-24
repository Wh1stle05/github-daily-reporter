from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
import respx

from github_daily_reporter.github_client import GitHubClient


OBSERVED_AT = datetime(2026, 7, 24, 2, 30, tzinfo=UTC)
SEARCH_URL = "https://api.github.com/search/repositories"


def _repository(number: int) -> dict[str, str]:
    return {
        "html_url": f"https://github.com/Owner/repository-{number}",
        "full_name": f"Owner/repository-{number}",
    }


def test_build_search_query_uses_the_utc_cutoff_date():
    from github_daily_reporter.collectors.github_search import build_search_query

    now = datetime(2026, 7, 23, 23, 30, tzinfo=UTC)

    assert build_search_query(now, 7) == (
        "created:>=2026-07-16 stars:>30 fork:false archived:false"
    )


def test_build_search_query_converts_an_offset_aware_datetime_to_utc():
    from github_daily_reporter.collectors.github_search import build_search_query

    now = datetime(2026, 7, 23, 20, 30, tzinfo=timezone(timedelta(hours=-4)))

    assert build_search_query(now, 7) == (
        "created:>=2026-07-17 stars:>30 fork:false archived:false"
    )


def test_build_search_query_rejects_naive_datetimes():
    from github_daily_reporter.collectors.github_search import build_search_query

    with pytest.raises(ValueError, match="timezone-aware"):
        build_search_query(datetime(2026, 7, 23), 7)


@pytest.mark.asyncio
@respx.mock
async def test_collect_github_search_caps_a_single_response_at_limit():
    from github_daily_reporter.collectors.github_search import collect_github_search

    route = respx.get(
        SEARCH_URL,
        params={
            "q": "created:>=2026-07-17 stars:>30 fork:false archived:false",
            "sort": "stars",
            "order": "desc",
            "per_page": "40",
            "page": "1",
        },
    ).mock(return_value=httpx.Response(200, json={"total_count": 100, "incomplete_results": False, "items": [_repository(number) for number in range(100)]}))

    async with GitHubClient("token") as client:
        result = await collect_github_search(client, OBSERVED_AT, lookback_days=7, limit=40)

    assert route.called
    assert len(result.observations) == 40
    assert [observation.source_rank for observation in result.observations] == list(range(1, 41))
    assert result.observations[0].source == "github_search"
    assert result.observations[0].source_metadata == {
        "total_count": 100,
        "incomplete_results": False,
        "full_name": "Owner/repository-0",
    }
    assert result.health.status == "success"
    assert result.health.item_count == 40


@pytest.mark.asyncio
@respx.mock
async def test_collect_github_search_paginates_until_the_limit_with_a_fixed_page_size():
    from github_daily_reporter.collectors.github_search import collect_github_search

    route = respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(200, json={"total_count": 3, "incomplete_results": False, "items": [_repository(1), _repository(2)]}),
            httpx.Response(200, json={"total_count": 3, "incomplete_results": False, "items": [_repository(3)]}),
        ]
    )

    async with GitHubClient("token") as client:
        result = await collect_github_search(client, OBSERVED_AT, lookback_days=7, limit=3)

    assert route.call_count == 2
    assert [call.request.url.params["per_page"] for call in route.calls] == ["3", "3"]
    assert [call.request.url.params["page"] for call in route.calls] == ["1", "2"]
    assert [observation.name for observation in result.observations] == [
        "repository-1",
        "repository-2",
        "repository-3",
    ]


@pytest.mark.asyncio
@respx.mock
async def test_collect_github_search_keeps_real_api_offsets_for_limit_101():
    from github_daily_reporter.collectors.github_search import collect_github_search

    repositories = [_repository(number) for number in range(1, 202)]

    def paged_response(request: httpx.Request) -> httpx.Response:
        page_size = int(request.url.params["per_page"])
        page = int(request.url.params["page"])
        offset = (page - 1) * page_size
        return httpx.Response(
            200,
            json={
                "total_count": len(repositories),
                "incomplete_results": False,
                "items": repositories[offset : offset + page_size],
            },
        )

    route = respx.get(SEARCH_URL).mock(side_effect=paged_response)

    async with GitHubClient("token") as client:
        result = await collect_github_search(client, OBSERVED_AT, lookback_days=7, limit=101)

    assert [call.request.url.params["per_page"] for call in route.calls] == ["100", "100"]
    assert [call.request.url.params["page"] for call in route.calls] == ["1", "2"]
    assert len(result.observations) == 101
    assert [observation.owner + "/" + observation.name for observation in result.observations] == [
        f"Owner/repository-{number}" for number in range(1, 102)
    ]


@pytest.mark.asyncio
@respx.mock
async def test_collect_github_search_preserves_source_offsets_across_malformed_page_rows():
    from github_daily_reporter.collectors.github_search import collect_github_search

    repositories: list[dict[str, str]] = [_repository(number) for number in range(1, 201)]
    repositories[0] = {"html_url": "https://github.com/search"}

    def paged_response(request: httpx.Request) -> httpx.Response:
        page_size = int(request.url.params["per_page"])
        page = int(request.url.params["page"])
        offset = (page - 1) * page_size
        return httpx.Response(
            200,
            json={
                "total_count": len(repositories),
                "incomplete_results": False,
                "items": repositories[offset : offset + page_size],
            },
        )

    route = respx.get(SEARCH_URL).mock(side_effect=paged_response)

    async with GitHubClient("token") as client:
        result = await collect_github_search(client, OBSERVED_AT, lookback_days=7, limit=101)

    assert [call.request.url.params["per_page"] for call in route.calls] == ["100", "100"]
    assert [observation.name for observation in result.observations[-2:]] == [
        "repository-101",
        "repository-102",
    ]
    assert [observation.source_rank for observation in result.observations[-2:]] == [101, 102]


@pytest.mark.asyncio
@respx.mock
async def test_collect_github_search_stops_after_an_empty_page():
    from github_daily_reporter.collectors.github_search import collect_github_search

    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"total_count": 0, "incomplete_results": False, "items": []}
        )
    )

    async with GitHubClient("token") as client:
        result = await collect_github_search(client, OBSERVED_AT, lookback_days=7, limit=5)

    assert route.call_count == 1
    assert result.observations == []
    assert result.health.item_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_collect_github_search_rejects_naive_observed_at():
    from github_daily_reporter.collectors.github_search import collect_github_search

    async with GitHubClient("token") as client:
        with pytest.raises(ValueError, match="timezone-aware"):
            await collect_github_search(client, datetime(2026, 7, 24), lookback_days=7, limit=1)


@pytest.mark.asyncio
@respx.mock
async def test_collect_github_search_rejects_invalid_lookback_days():
    from github_daily_reporter.collectors.github_search import collect_github_search

    async with GitHubClient("token") as client:
        with pytest.raises(ValueError, match="non-negative integer"):
            await collect_github_search(client, OBSERVED_AT, lookback_days=-1, limit=1)


@pytest.mark.asyncio
@respx.mock
async def test_collect_github_search_records_observations_in_utc():
    from github_daily_reporter.collectors.github_search import collect_github_search

    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"total_count": 1, "incomplete_results": False, "items": [_repository(1)]},
        )
    )
    observed_at = datetime(2026, 7, 23, 20, 30, tzinfo=timezone(timedelta(hours=-4)))

    async with GitHubClient("token") as client:
        result = await collect_github_search(client, observed_at, lookback_days=7, limit=1)

    assert result.observations[0].observed_at == datetime(2026, 7, 24, 0, 30, tzinfo=UTC)


@pytest.mark.asyncio
@respx.mock
async def test_collect_github_search_skips_malformed_items_without_reordering_valid_ones():
    from github_daily_reporter.collectors.github_search import collect_github_search

    respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "total_count": 3,
                    "incomplete_results": False,
                    "items": [
                        _repository(1),
                        {"html_url": "https://github.com/search"},
                        _repository(3),
                    ],
                },
            ),
            httpx.Response(
                200, json={"total_count": 3, "incomplete_results": False, "items": []}
            ),
        ]
    )

    async with GitHubClient("token") as client:
        result = await collect_github_search(client, OBSERVED_AT, lookback_days=7, limit=3)

    assert [observation.name for observation in result.observations] == [
        "repository-1",
        "repository-3",
    ]
    assert [observation.source_rank for observation in result.observations] == [1, 3]


@pytest.mark.asyncio
@respx.mock
async def test_collect_github_search_does_not_request_past_githubs_1000_result_boundary():
    from github_daily_reporter.collectors.github_search import collect_github_search

    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1001,
                "incomplete_results": False,
                "items": [_repository(number) for number in range(100)],
            },
        )
    )

    async with GitHubClient("token") as client:
        result = await collect_github_search(client, OBSERVED_AT, lookback_days=7, limit=1001)

    assert route.call_count == 10
    assert [call.request.url.params["page"] for call in route.calls] == [str(page) for page in range(1, 11)]
    assert len(result.observations) == 1000
