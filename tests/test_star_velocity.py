from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from github_daily_reporter.models import RepositoryCandidate


NOW = datetime(2026, 7, 23, 9, tzinfo=UTC)


def _edge(starred_at: datetime) -> dict[str, str]:
    return {"starredAt": starred_at.isoformat()}


def _page(
    edges: list[dict[str, str]],
    *,
    stargazer_count: int = 100,
    has_next_page: bool = False,
    end_cursor: str | None = None,
) -> dict[str, Any]:
    return {
        "repository": {
            "stargazerCount": stargazer_count,
            "stargazers": {
                "edges": edges,
                "pageInfo": {
                    "hasNextPage": has_next_page,
                    "endCursor": end_cursor,
                },
            },
        },
        "rateLimit": {"cost": 1, "remaining": 4999, "resetAt": NOW.isoformat()},
    }


class FakeGraphQL:
    def __init__(self, *pages: dict[str, Any]) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((query, variables))
        return self.pages.pop(0)


class FailingGraphQL:
    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("token=secret-value")


@pytest.fixture
def fake_graphql() -> FakeGraphQL:
    return FakeGraphQL(_page([_edge(NOW - timedelta(hours=1))]))


@pytest.fixture
def failing_graphql() -> FailingGraphQL:
    return FailingGraphQL()


@pytest.fixture
def snapshot_estimator(monkeypatch: pytest.MonkeyPatch):
    class Estimator:
        return_value: tuple[int, datetime] | None = None
        calls: list[tuple[str, int, datetime, datetime]] = []

        def __call__(self, *args: object) -> tuple[int, datetime] | None:
            self.calls.append(args)  # type: ignore[arg-type]
            return self.return_value

    return Estimator()


def test_count_recent_stars_stops_at_first_old_edge():
    from github_daily_reporter.collectors.star_velocity import count_recent_stars

    cutoff = NOW - timedelta(hours=24)
    edges = [
        _edge(NOW - timedelta(hours=1)),
        _edge(NOW - timedelta(hours=23)),
        _edge(NOW - timedelta(hours=25)),
    ]

    assert count_recent_stars(edges, cutoff) == (2, True)


def test_count_recent_stars_includes_the_cutoff_and_normalizes_offsets():
    from github_daily_reporter.collectors.star_velocity import count_recent_stars

    cutoff = NOW - timedelta(hours=24)
    offset_timestamp = cutoff.astimezone(timezone(timedelta(hours=-4)))

    assert count_recent_stars([_edge(offset_timestamp)], cutoff) == (1, False)


def test_count_recent_stars_rejects_malformed_timestamp():
    from github_daily_reporter.collectors.star_velocity import (
        StarVelocityResponseError,
        count_recent_stars,
    )

    with pytest.raises(StarVelocityResponseError, match="incomplete"):
        count_recent_stars([{"starredAt": "not-a-timestamp"}], NOW - timedelta(hours=24))


def test_count_recent_stars_rejects_edges_out_of_descending_order():
    from github_daily_reporter.collectors.star_velocity import (
        StarVelocityResponseError,
        count_recent_stars,
    )

    with pytest.raises(StarVelocityResponseError, match="incomplete"):
        count_recent_stars(
            [_edge(NOW - timedelta(hours=25)), _edge(NOW - timedelta(hours=1))],
            NOW - timedelta(hours=24),
        )


@pytest.mark.asyncio
async def test_velocity_hit_is_strictly_greater_than_threshold(
    candidate: RepositoryCandidate, fake_graphql: FakeGraphQL
):
    from github_daily_reporter.collectors.star_velocity import enrich_velocity

    fake_graphql.pages[0]["repository"]["stargazerCount"] = 51
    fake_graphql.pages[0]["repository"]["stargazers"]["edges"] = [
        _edge(NOW - timedelta(hours=1)) for _ in range(51)
    ]

    enriched = await enrich_velocity(candidate, fake_graphql, NOW, 24, 50, None)

    assert enriched.stars_24h == 51
    assert enriched.velocity_hit is True


@pytest.mark.asyncio
async def test_enrich_velocity_continues_pages_until_an_old_edge(
    candidate: RepositoryCandidate,
):
    from github_daily_reporter.collectors.star_velocity import enrich_velocity

    graphql = FakeGraphQL(
        _page(
            [_edge(NOW - timedelta(minutes=offset)) for offset in range(100)],
            stargazer_count=200,
            has_next_page=True,
            end_cursor="page-1",
        ),
        _page(
            [_edge(NOW - timedelta(hours=2)), _edge(NOW - timedelta(hours=25))],
            stargazer_count=200,
        ),
    )

    enriched = await enrich_velocity(candidate, graphql, NOW, 24, 50, None)

    assert enriched is candidate
    assert enriched.stars_24h == 101
    assert enriched.stars_24h_estimated is False
    assert [call[1]["cursor"] for call in graphql.calls] == [None, "page-1"]
    assert "stargazers(first: 100" in graphql.calls[0][0]
    assert "field: STARRED_AT" in graphql.calls[0][0]


@pytest.mark.asyncio
async def test_enrich_velocity_stops_without_fetching_after_old_edge(
    candidate: RepositoryCandidate,
):
    from github_daily_reporter.collectors.star_velocity import enrich_velocity

    graphql = FakeGraphQL(
        _page(
            [_edge(NOW - timedelta(hours=1)), _edge(NOW - timedelta(hours=25))],
            has_next_page=True,
            end_cursor="ignored",
        )
    )

    enriched = await enrich_velocity(candidate, graphql, NOW, 24, 50, None)

    assert enriched.stars_24h == 1
    assert len(graphql.calls) == 1


@pytest.mark.asyncio
async def test_enrich_velocity_falls_back_when_final_recent_edges_do_not_match_total(
    candidate: RepositoryCandidate, snapshot_estimator: Any
):
    from github_daily_reporter.collectors.star_velocity import enrich_velocity

    graphql = FakeGraphQL(
        _page([_edge(NOW - timedelta(hours=1))], stargazer_count=100)
    )
    snapshot_estimator.return_value = (8, NOW - timedelta(hours=24))

    enriched = await enrich_velocity(candidate, graphql, NOW, 24, 50, snapshot_estimator)

    assert enriched.stars_24h == 8
    assert enriched.stars_24h_estimated is True


@pytest.mark.asyncio
async def test_enrich_velocity_counts_an_empty_repository_exactly(
    candidate: RepositoryCandidate,
):
    from github_daily_reporter.collectors.star_velocity import enrich_velocity

    enriched = await enrich_velocity(
        candidate,
        FakeGraphQL(_page([], stargazer_count=0)),
        NOW,
        24,
        50,
        None,
    )

    assert enriched.stars_24h == 0
    assert enriched.stars_24h_estimated is False


@pytest.mark.asyncio
async def test_enrich_velocity_rejects_a_short_nonfinal_page(
    candidate: RepositoryCandidate, snapshot_estimator: Any
):
    from github_daily_reporter.collectors.star_velocity import enrich_velocity

    graphql = FakeGraphQL(
        _page(
            [_edge(NOW - timedelta(hours=1))],
            stargazer_count=200,
            has_next_page=True,
            end_cursor="page-1",
        ),
        _page([_edge(NOW - timedelta(hours=2))], stargazer_count=200),
    )
    snapshot_estimator.return_value = (9, NOW - timedelta(hours=24))

    enriched = await enrich_velocity(candidate, graphql, NOW, 24, 50, snapshot_estimator)

    assert enriched.stars_24h == 9
    assert enriched.stars_24h_estimated is True
    assert len(graphql.calls) == 1


@pytest.mark.asyncio
async def test_enrich_velocity_success_clears_only_its_previous_error(
    candidate: RepositoryCandidate, failing_graphql: FailingGraphQL
):
    from github_daily_reporter.collectors.star_velocity import (
        VELOCITY_ERROR,
        enrich_velocity,
    )

    candidate.source_errors = ["Other source failed"]
    await enrich_velocity(candidate, failing_graphql, NOW, 24, 50, None)

    enriched = await enrich_velocity(
        candidate,
        FakeGraphQL(_page([_edge(NOW - timedelta(hours=1))], stargazer_count=1)),
        NOW,
        24,
        50,
        None,
    )

    assert VELOCITY_ERROR not in enriched.source_errors
    assert enriched.source_errors == ["Other source failed"]


@pytest.mark.asyncio
async def test_enrich_velocity_estimate_success_clears_its_previous_error(
    candidate: RepositoryCandidate,
    failing_graphql: FailingGraphQL,
    snapshot_estimator: Any,
):
    from github_daily_reporter.collectors.star_velocity import (
        VELOCITY_ERROR,
        enrich_velocity,
    )

    candidate.source_errors = ["Other source failed"]
    await enrich_velocity(candidate, failing_graphql, NOW, 24, 50, None)
    snapshot_estimator.return_value = (10, NOW - timedelta(hours=24))

    enriched = await enrich_velocity(
        candidate, failing_graphql, NOW, 24, 50, snapshot_estimator
    )

    assert enriched.stars_24h_estimated is True
    assert VELOCITY_ERROR not in enriched.source_errors
    assert enriched.source_errors == ["Other source failed"]


@pytest.mark.asyncio
async def test_enrich_velocity_does_not_duplicate_its_error_on_repeated_failures(
    candidate: RepositoryCandidate, failing_graphql: FailingGraphQL
):
    from github_daily_reporter.collectors.star_velocity import (
        VELOCITY_ERROR,
        enrich_velocity,
    )

    await enrich_velocity(candidate, failing_graphql, NOW, 24, 50, None)
    enriched = await enrich_velocity(candidate, failing_graphql, NOW, 24, 50, None)

    assert enriched.source_errors == [VELOCITY_ERROR]


@pytest.mark.asyncio
async def test_enrich_velocity_falls_back_when_star_order_decreases_across_pages(
    candidate: RepositoryCandidate, snapshot_estimator: Any
):
    from github_daily_reporter.collectors.star_velocity import enrich_velocity

    graphql = FakeGraphQL(
        _page(
            [_edge(NOW - timedelta(minutes=offset)) for offset in range(100)],
            stargazer_count=200,
            has_next_page=True,
            end_cursor="page-1",
        ),
        _page([_edge(NOW - timedelta(minutes=1))], stargazer_count=200),
    )
    snapshot_estimator.return_value = (4, NOW - timedelta(hours=24))

    enriched = await enrich_velocity(candidate, graphql, NOW, 24, 50, snapshot_estimator)

    assert enriched.stars_24h == 4
    assert enriched.stars_24h_estimated is True


@pytest.mark.asyncio
async def test_enrich_velocity_rejects_an_empty_page_with_a_next_cursor(
    candidate: RepositoryCandidate, snapshot_estimator: Any
):
    from github_daily_reporter.collectors.star_velocity import enrich_velocity

    graphql = FakeGraphQL(
        _page([], has_next_page=True, end_cursor="page-1"),
        _page([_edge(NOW - timedelta(hours=1))]),
    )
    snapshot_estimator.return_value = (5, NOW - timedelta(hours=24))

    enriched = await enrich_velocity(candidate, graphql, NOW, 24, 50, snapshot_estimator)

    assert enriched.stars_24h == 5
    assert enriched.stars_24h_estimated is True
    assert len(graphql.calls) == 1


@pytest.mark.asyncio
async def test_enrich_velocity_rejects_more_pages_than_stargazer_count_allows(
    candidate: RepositoryCandidate, snapshot_estimator: Any
):
    from github_daily_reporter.collectors.star_velocity import enrich_velocity

    graphql = FakeGraphQL(
        _page(
            [_edge(NOW - timedelta(minutes=offset)) for offset in range(100)],
            has_next_page=True,
            end_cursor="page-1",
        ),
        _page([_edge(NOW - timedelta(hours=2))]),
    )
    snapshot_estimator.return_value = (6, NOW - timedelta(hours=24))

    enriched = await enrich_velocity(candidate, graphql, NOW, 24, 50, snapshot_estimator)

    assert enriched.stars_24h == 6
    assert enriched.stars_24h_estimated is True
    assert len(graphql.calls) == 1


@pytest.mark.asyncio
async def test_enrich_velocity_uses_snapshot_gain_when_graphql_fails(
    candidate: RepositoryCandidate,
    failing_graphql: FailingGraphQL,
    snapshot_estimator: Any,
):
    from github_daily_reporter.collectors.star_velocity import enrich_velocity

    snapshot_estimator.return_value = (60, NOW - timedelta(hours=25))

    enriched = await enrich_velocity(candidate, failing_graphql, NOW, 24, 50, snapshot_estimator)

    assert enriched.stars_24h == 60
    assert enriched.stars_24h_estimated is True
    assert enriched.growth_rate_24h == pytest.approx(60 / 40)
    assert enriched.velocity_hit is True
    assert snapshot_estimator.calls == [
        (candidate.canonical_name, candidate.stars_total, NOW - timedelta(hours=24), NOW)
    ]


@pytest.mark.asyncio
async def test_enrich_velocity_uses_a_minimum_growth_denominator(
    candidate: RepositoryCandidate, failing_graphql: FailingGraphQL, snapshot_estimator: Any
):
    from github_daily_reporter.collectors.star_velocity import enrich_velocity

    snapshot_estimator.return_value = (95, NOW - timedelta(hours=24))

    enriched = await enrich_velocity(candidate, failing_graphql, NOW, 24, 95, snapshot_estimator)

    assert enriched.growth_rate_24h == pytest.approx(95 / 30)
    assert enriched.velocity_hit is False


@pytest.mark.asyncio
async def test_enrich_velocity_falls_back_on_malformed_graphql_and_sanitizes_error(
    candidate: RepositoryCandidate, snapshot_estimator: Any
):
    from github_daily_reporter.collectors.star_velocity import enrich_velocity

    graphql = FakeGraphQL({"repository": {"stargazers": {"edges": []}}})
    snapshot_estimator.return_value = (3, NOW - timedelta(hours=24))

    enriched = await enrich_velocity(candidate, graphql, NOW, 24, 50, snapshot_estimator)

    assert enriched.stars_24h == 3
    assert enriched.stars_24h_estimated is True
    assert all("secret" not in error.lower() for error in enriched.source_errors)


@pytest.mark.asyncio
async def test_enrich_velocity_keeps_fields_unknown_without_an_estimate(
    candidate: RepositoryCandidate, failing_graphql: FailingGraphQL
):
    from github_daily_reporter.collectors.star_velocity import enrich_velocity

    candidate.stars_24h = 1
    candidate.stars_24h_estimated = True
    candidate.growth_rate_24h = 0.1
    candidate.velocity_hit = True

    enriched = await enrich_velocity(candidate, failing_graphql, NOW, 24, 50, None)

    assert enriched.stars_24h is None
    assert enriched.stars_24h_estimated is False
    assert enriched.growth_rate_24h is None
    assert enriched.velocity_hit is False
    assert enriched.source_errors == ["GitHub star velocity unavailable"]


@pytest.mark.asyncio
async def test_enrich_velocity_falls_back_when_pagination_cursor_repeats(
    candidate: RepositoryCandidate, snapshot_estimator: Any
):
    from github_daily_reporter.collectors.star_velocity import enrich_velocity

    graphql = FakeGraphQL(
        _page(
            [_edge(NOW - timedelta(minutes=offset)) for offset in range(100)],
            stargazer_count=300,
            has_next_page=True,
            end_cursor="repeat",
        ),
        _page(
            [_edge(NOW - timedelta(minutes=offset)) for offset in range(100, 200)],
            stargazer_count=300,
            has_next_page=True,
            end_cursor="repeat",
        ),
    )
    snapshot_estimator.return_value = (7, NOW - timedelta(hours=24))

    enriched = await enrich_velocity(candidate, graphql, NOW, 24, 50, snapshot_estimator)

    assert enriched.stars_24h == 7
    assert enriched.stars_24h_estimated is True
    assert len(graphql.calls) == 2
