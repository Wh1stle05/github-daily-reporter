"""GitHub repository search discovery collector."""

from datetime import UTC, datetime, timedelta
from typing import Any

from github_daily_reporter.github_client import GitHubClient
from github_daily_reporter.models import CollectorResult, SourceHealth, SourceObservation
from github_daily_reporter.normalize import extract_repo_ref


SEARCH_PATH = "/search/repositories"
GITHUB_SEARCH_RESULT_BOUNDARY = 1_000


class GitHubSearchResponseError(ValueError):
    """GitHub search returned a response that could not be safely consumed."""


def build_search_query(now: datetime, lookback_days: int) -> str:
    """Build the recent, popular, active-repository search query."""
    if isinstance(lookback_days, bool) or not isinstance(lookback_days, int) or lookback_days < 0:
        raise ValueError("lookback_days must be a non-negative integer")
    cutoff = now.astimezone(UTC).date() - timedelta(days=lookback_days)
    return f"created:>={cutoff.isoformat()} stars:>30 fork:false archived:false"


async def collect_github_search(
    client: GitHubClient,
    observed_at: datetime,
    lookback_days: int,
    limit: int,
) -> CollectorResult:
    """Collect recently created, popular repositories from GitHub search."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer")

    query = build_search_query(observed_at, lookback_days)
    observations: list[SourceObservation] = []
    page = 1
    source_rows_seen = 0

    while len(observations) < limit and source_rows_seen < GITHUB_SEARCH_RESULT_BOUNDARY:
        remaining = limit - len(observations)
        payload = await client.rest_json(
            SEARCH_PATH,
            params={
                "q": query,
                "sort": "stars",
                "order": "desc",
                "per_page": min(100, remaining),
                "page": page,
            },
        )
        total_count, incomplete_results, items = _search_response_values(payload)
        if not items:
            break

        for item in items:
            if source_rows_seen >= GITHUB_SEARCH_RESULT_BOUNDARY or len(observations) >= limit:
                break
            source_rows_seen += 1
            observation = _observation_from_item(
                item,
                observed_at=observed_at,
                source_rank=source_rows_seen,
                total_count=total_count,
                incomplete_results=incomplete_results,
            )
            if observation is not None:
                observations.append(observation)

        page += 1

    return CollectorResult(
        source="github_search",
        observations=observations,
        health=SourceHealth(
            source="github_search", status="success", item_count=len(observations)
        ),
    )


def _search_response_values(payload: dict[str, Any]) -> tuple[int, bool, list[Any]]:
    total_count = payload.get("total_count")
    incomplete_results = payload.get("incomplete_results")
    items = payload.get("items")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 0
        or not isinstance(incomplete_results, bool)
        or not isinstance(items, list)
    ):
        raise GitHubSearchResponseError("GitHub repository search response was incomplete")
    return total_count, incomplete_results, items


def _observation_from_item(
    item: Any,
    *,
    observed_at: datetime,
    source_rank: int,
    total_count: int,
    incomplete_results: bool,
) -> SourceObservation | None:
    if not isinstance(item, dict):
        return None
    html_url = item.get("html_url")
    full_name = item.get("full_name")
    if not isinstance(html_url, str) or not isinstance(full_name, str):
        return None
    ref = extract_repo_ref(html_url)
    if ref is None:
        return None
    return SourceObservation(
        source="github_search",
        repository_url=f"https://github.com/{ref.owner}/{ref.name}",
        owner=ref.owner,
        name=ref.name,
        observed_at=observed_at,
        source_rank=source_rank,
        source_metadata={
            "total_count": total_count,
            "incomplete_results": incomplete_results,
            "full_name": full_name,
        },
    )
