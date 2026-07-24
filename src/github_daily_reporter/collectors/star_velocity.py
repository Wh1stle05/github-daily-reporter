"""Exact and snapshot-estimated GitHub star velocity."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from github_daily_reporter.github_client import GitHubClient
from github_daily_reporter.models import RepositoryCandidate


REPO_STARS_QUERY = """query RepoStars($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    stargazerCount
    stargazers(first: 100, after: $cursor,
      orderBy: {field: STARRED_AT, direction: DESC}) {
      edges { starredAt }
      pageInfo { hasNextPage endCursor }
    }
  }
  rateLimit { cost remaining resetAt }
}"""

VELOCITY_ERROR = "GitHub star velocity unavailable"
STARGAZERS_PAGE_SIZE = 100
SnapshotEstimator = Callable[[str, int, datetime, datetime], tuple[int, datetime] | None]


class StarVelocityResponseError(ValueError):
    """GitHub star data cannot be safely used for an exact count."""


def count_recent_stars(edges: list[Any], cutoff: datetime) -> tuple[int, bool]:
    """Count descending stargazer edges through the first edge before ``cutoff``."""
    count, reached_old_edge, _ = _count_page_stars(edges, cutoff, None)
    return count, reached_old_edge


def _count_page_stars(
    edges: list[Any], cutoff: datetime, previous_starred_at: datetime | None
) -> tuple[int, bool, datetime | None]:
    """Count a page while validating its order against prior pages."""
    cutoff_utc = _normalize_utc(cutoff)
    if not isinstance(edges, list):
        raise StarVelocityResponseError("GitHub star response was incomplete")

    count = 0
    reached_old_edge = False
    for edge in edges:
        starred_at = _starred_at(edge)
        if previous_starred_at is not None and starred_at > previous_starred_at:
            raise StarVelocityResponseError("GitHub star response was incomplete")
        previous_starred_at = starred_at
        if starred_at < cutoff_utc:
            reached_old_edge = True
        elif not reached_old_edge:
            count += 1
    return count, reached_old_edge, previous_starred_at


async def enrich_velocity(
    candidate: RepositoryCandidate,
    client: GitHubClient,
    now: datetime,
    window_hours: int,
    threshold: int,
    snapshot_estimator: SnapshotEstimator | None,
) -> RepositoryCandidate:
    """Populate a candidate's star velocity from GraphQL, falling back to snapshots."""
    now_utc = _normalize_utc(now)
    _validate_window_hours(window_hours)
    cutoff = now_utc - timedelta(hours=window_hours)

    try:
        count = await _count_exact_stars(candidate, client, cutoff)
    except Exception:
        estimate = _snapshot_estimate(candidate, cutoff, now_utc, snapshot_estimator)
        if estimate is None:
            _clear_velocity(candidate)
            candidate.source_errors.append(VELOCITY_ERROR)
            return candidate
        _set_velocity(candidate, estimate, threshold, estimated=True)
        return candidate

    _set_velocity(candidate, count, threshold, estimated=False)
    return candidate


async def _count_exact_stars(
    candidate: RepositoryCandidate, client: GitHubClient, cutoff: datetime
) -> int:
    owner, separator, name = candidate.canonical_name.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise StarVelocityResponseError("GitHub star response was incomplete")

    cursor: str | None = None
    seen_cursors: set[str] = set()
    count = 0
    expected_stargazer_count: int | None = None
    previous_starred_at: datetime | None = None
    page_count = 0
    while True:
        payload = await client.graphql(
            REPO_STARS_QUERY,
            {"owner": owner, "name": name, "cursor": cursor},
        )
        stargazer_count, edges, has_next_page, end_cursor = _page_values(payload)
        if expected_stargazer_count is None:
            expected_stargazer_count = stargazer_count
        elif stargazer_count != expected_stargazer_count:
            raise StarVelocityResponseError("GitHub star response was incomplete")

        page_count += 1
        recent_count, reached_old_edge, previous_starred_at = _count_page_stars(
            edges, cutoff, previous_starred_at
        )
        count += recent_count
        if count > expected_stargazer_count:
            raise StarVelocityResponseError("GitHub star response was incomplete")

        if reached_old_edge or not has_next_page:
            return count
        max_pages = max(1, (expected_stargazer_count + STARGAZERS_PAGE_SIZE - 1) // STARGAZERS_PAGE_SIZE)
        if (
            not edges
            or page_count >= max_pages
            or not isinstance(end_cursor, str)
            or not end_cursor
            or end_cursor in seen_cursors
        ):
            raise StarVelocityResponseError("GitHub star response was incomplete")
        seen_cursors.add(end_cursor)
        cursor = end_cursor


def _page_values(payload: object) -> tuple[int, list[Any], bool, str | None]:
    if not isinstance(payload, Mapping):
        raise StarVelocityResponseError("GitHub star response was incomplete")
    repository = payload.get("repository")
    if not isinstance(repository, Mapping):
        raise StarVelocityResponseError("GitHub star response was incomplete")
    stargazer_count = repository.get("stargazerCount")
    stargazers = repository.get("stargazers")
    if (
        isinstance(stargazer_count, bool)
        or not isinstance(stargazer_count, int)
        or stargazer_count < 0
        or not isinstance(stargazers, Mapping)
    ):
        raise StarVelocityResponseError("GitHub star response was incomplete")
    edges = stargazers.get("edges")
    page_info = stargazers.get("pageInfo")
    if not isinstance(edges, list) or not isinstance(page_info, Mapping):
        raise StarVelocityResponseError("GitHub star response was incomplete")
    has_next_page = page_info.get("hasNextPage")
    end_cursor = page_info.get("endCursor")
    if not isinstance(has_next_page, bool) or (
        end_cursor is not None and not isinstance(end_cursor, str)
    ):
        raise StarVelocityResponseError("GitHub star response was incomplete")
    return stargazer_count, edges, has_next_page, end_cursor


def _starred_at(edge: object) -> datetime:
    if not isinstance(edge, Mapping):
        raise StarVelocityResponseError("GitHub star response was incomplete")
    raw_timestamp = edge.get("starredAt")
    if not isinstance(raw_timestamp, str):
        raise StarVelocityResponseError("GitHub star response was incomplete")
    try:
        return _normalize_utc(datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00")))
    except ValueError as exc:
        raise StarVelocityResponseError("GitHub star response was incomplete") from exc


def _snapshot_estimate(
    candidate: RepositoryCandidate,
    cutoff: datetime,
    now: datetime,
    estimator: SnapshotEstimator | None,
) -> int | None:
    if estimator is None:
        return None
    try:
        estimate = estimator(candidate.canonical_name, candidate.stars_total, cutoff, now)
    except Exception:
        return None
    if not isinstance(estimate, tuple) or len(estimate) != 2:
        return None
    gain, observed_at = estimate
    if isinstance(gain, bool) or not isinstance(gain, int) or gain < 0:
        return None
    try:
        _normalize_utc(observed_at)
    except (TypeError, ValueError):
        return None
    return gain


def _set_velocity(
    candidate: RepositoryCandidate, count: int, threshold: int, *, estimated: bool
) -> None:
    candidate.stars_24h = count
    candidate.stars_24h_estimated = estimated
    candidate.growth_rate_24h = count / max(candidate.stars_total - count, 30)
    candidate.velocity_hit = count > threshold


def _clear_velocity(candidate: RepositoryCandidate) -> None:
    candidate.stars_24h = None
    candidate.stars_24h_estimated = False
    candidate.growth_rate_24h = None
    candidate.velocity_hit = False


def _normalize_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _validate_window_hours(window_hours: int) -> None:
    if isinstance(window_hours, bool) or not isinstance(window_hours, int) or window_hours <= 0:
        raise ValueError("window_hours must be a positive integer")
