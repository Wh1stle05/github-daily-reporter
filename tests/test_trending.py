from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from github_daily_reporter.collectors.trending import (
    TrendingFetchError,
    TrendingParseError,
    collect_trending,
    parse_count,
    parse_trending,
)


FIXTURES = Path(__file__).parent / "fixtures"
OBSERVED_AT = datetime(2026, 7, 24, tzinfo=UTC)


class ByteChunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def test_parse_trending_extracts_rank_and_daily_stars():
    result = parse_trending(
        (FIXTURES / "github_trending.html").read_text(encoding="utf-8"), OBSERVED_AT
    )

    assert len(result) == 2
    assert result[0].source == "trending"
    assert result[0].repository_url == "https://github.com/Example-Org/First-Repo"
    assert result[0].owner == "Example-Org"
    assert result[0].name == "First-Repo"
    assert result[0].source_rank == 1
    assert result[0].source_metadata == {
        "description": "A useful first repository.",
        "language": "Python",
        "stars_total": 52100,
        "forks_total": 3200,
        "stars_today": 4139,
    }


def test_parse_trending_omits_missing_optional_values_and_parses_count_text():
    html = """
    <article class="Box-row">
      <h2><a href="/owner/repo">owner/repo</a></h2>
      <a class="Link--muted"> 1,024 stars </a>
      <span>  99 stars today </span>
    </article>
    """

    result = parse_trending(html, OBSERVED_AT)

    assert result[0].source_metadata == {
        "description": None,
        "language": None,
        "stars_total": 1024,
        "forks_total": None,
        "stars_today": 99,
    }
    assert parse_count("  12,345 contributors ") == 12345


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1.2k stars", 1200), (" 2M forks ", 2_000_000), ("0 stars today", 0)],
)
def test_parse_count_accepts_well_formed_ascii_abbreviations(text, expected):
    assert parse_count(text) == expected


@pytest.mark.parametrize(
    "text", ["1 234 stars", "1,23 stars", "1.2.3k stars", "12 stars and 3 forks", "١٢ stars"]
)
def test_parse_count_rejects_malformed_or_ambiguous_numbers(text):
    with pytest.raises(ValueError):
        parse_count(text)


def test_parse_trending_skips_invalid_rows_and_fails_visibly_without_repositories():
    html = "<article class='Box-row'><h2><a href='/trending'>no repo</a></h2></article>"

    with pytest.raises(TrendingParseError, match="no repository rows recognized"):
        parse_trending(html, OBSERVED_AT)

    with pytest.raises(TrendingParseError, match="no repository rows recognized"):
        parse_trending(
            (FIXTURES / "github_trending_empty.html").read_text(encoding="utf-8"),
            OBSERVED_AT,
        )


def test_parse_trending_caps_observations_without_renumbering_rows():
    html = """
    <article class="Box-row"><h2><a href="/trending">invalid</a></h2></article>
    <article class="Box-row"><h2><a href="/owner/first">first</a></h2></article>
    <article class="Box-row"><h2><a href="/owner/second">second</a></h2></article>
    """

    result = parse_trending(html, OBSERVED_AT, limit=1)

    assert [observation.source_rank for observation in result] == [2]

    with pytest.raises(TrendingParseError, match="no repository rows recognized"):
        parse_trending(html, OBSERVED_AT, limit=0)


@pytest.mark.asyncio
@respx.mock
async def test_collect_trending_requests_daily_results_and_returns_success_health():
    route = respx.get("https://github.com/trending", params={"since": "daily"}).mock(
        return_value=httpx.Response(
            200,
            text=(FIXTURES / "github_trending.html").read_text(encoding="utf-8"),
        )
    )

    async with httpx.AsyncClient() as client:
        result = await collect_trending(client, OBSERVED_AT)

    assert route.called
    assert result.source == "trending"
    assert len(result.observations) == 2
    assert result.health.source == "trending"
    assert result.health.status == "success"
    assert result.health.item_count == 2


@pytest.mark.asyncio
async def test_collect_trending_bounds_stream_and_closes_it():
    stream = ByteChunks(b"12345", b"6")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TrendingFetchError, match="response exceeded"):
            await collect_trending(client, OBSERVED_AT, max_response_bytes=5)

    assert stream.closed


@pytest.mark.asyncio
@respx.mock
async def test_collect_trending_retries_then_succeeds_with_retry_after():
    sleeps: list[float] = []

    async def no_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    route = respx.get("https://github.com/trending", params={"since": "daily"}).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(
                200,
                text=(FIXTURES / "github_trending.html").read_text(encoding="utf-8"),
            ),
        ]
    )

    async with httpx.AsyncClient() as client:
        result = await collect_trending(client, OBSERVED_AT, sleep=no_sleep)

    assert len(result.observations) == 2
    assert route.call_count == 2
    assert sleeps == [0]


@pytest.mark.asyncio
@respx.mock
async def test_collect_trending_stops_after_retry_exhaustion():
    sleeps: list[float] = []

    async def no_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    route = respx.get("https://github.com/trending", params={"since": "daily"}).mock(
        return_value=httpx.Response(503)
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await collect_trending(client, OBSERVED_AT, sleep=no_sleep)

    assert route.call_count == 3
    assert len(sleeps) == 2


@pytest.mark.asyncio
async def test_collect_trending_sets_descriptive_user_agent_and_timeout():
    seen: dict[str, object] = {}
    payload = (FIXTURES / "github_trending.html").read_bytes()

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["user_agent"] = request.headers["User-Agent"]
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, content=payload, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await collect_trending(client, OBSERVED_AT)

    assert seen["user_agent"] == "github-daily-reporter/0.1"
    assert seen["timeout"] == {"connect": 20, "read": 20, "write": 20, "pool": 20}
