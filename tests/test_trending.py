from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from github_daily_reporter.collectors.trending import (
    TrendingParseError,
    collect_trending,
    parse_count,
    parse_trending,
)


FIXTURES = Path(__file__).parent / "fixtures"
OBSERVED_AT = datetime(2026, 7, 24, tzinfo=UTC)


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


def test_parse_trending_skips_invalid_rows_and_fails_visibly_without_repositories():
    html = "<article class='Box-row'><h2><a href='/trending'>no repo</a></h2></article>"

    with pytest.raises(TrendingParseError, match="no repository rows recognized"):
        parse_trending(html, OBSERVED_AT)

    with pytest.raises(TrendingParseError, match="no repository rows recognized"):
        parse_trending(
            (FIXTURES / "github_trending_empty.html").read_text(encoding="utf-8"),
            OBSERVED_AT,
        )


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
