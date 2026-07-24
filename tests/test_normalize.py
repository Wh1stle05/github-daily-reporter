from datetime import UTC, datetime
from itertools import permutations

import pytest

from github_daily_reporter.models import SourceObservation
from github_daily_reporter.normalize import extract_repo_ref, merge_observations


def observation(
    source: str,
    owner: str = "Owner",
    name: str = "Repo",
    source_rank: int | None = None,
    **source_metadata: object,
) -> SourceObservation:
    return SourceObservation(
        source=source,  # type: ignore[arg-type]
        repository_url=f"https://github.com/{owner}/{name}",
        owner=owner,
        name=name,
        observed_at=datetime(2026, 7, 24, tzinfo=UTC),
        source_rank=source_rank,
        source_metadata=source_metadata,
    )


def test_extract_repo_ref_normalizes_git_suffix_and_ignores_query_fragment() -> None:
    repo = extract_repo_ref("https://www.github.com/Owner/Repo.GiT?tab=readme#install")

    assert repo is not None
    assert repo.owner == "Owner"
    assert repo.name == "Repo"
    assert repo.canonical_name == "owner/repo"


def test_extract_repo_ref_accepts_repository_issue_subpath() -> None:
    repo = extract_repo_ref("https://github.com/Owner/Repo/issues/4")

    assert repo is not None
    assert repo.canonical_name == "owner/repo"


def test_extract_repo_ref_rejects_profile_and_organization_pages() -> None:
    assert extract_repo_ref("https://github.com/Owner") is None
    assert extract_repo_ref("https://github.com/orgs/Acme") is None


def test_extract_repo_ref_rejects_non_github_hosts() -> None:
    assert extract_repo_ref("https://example.com/Owner/Repo") is None


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/Owner/Repo",
        "//github.com/Owner/Repo",
        "https://user:secret@github.com/Owner/Repo",
        "https://github.com:444/Owner/Repo",
        "https://github.com:invalid/Owner/Repo",
        "https://github.com:99999/Owner/Repo",
        "https://[invalid/Owner/Repo",
    ],
)
def test_extract_repo_ref_rejects_unsafe_or_invalid_authorities(url: str) -> None:
    assert extract_repo_ref(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Owner%2FTeam/Repo",
        "https://github.com/Owner/Repo%5Cname",
        "https://github.com/Owner/%00Repo",
        "https://github.com/Owner/%252FRepo",
        "https://github.com/./Repo",
        "https://github.com/Owner/..",
        "https://github.com/Owner!/Repo",
    ],
)
def test_extract_repo_ref_rejects_encoded_or_invalid_identity_segments(url: str) -> None:
    assert extract_repo_ref(url) is None


def test_extract_repo_ref_decodes_once_and_normalizes_encoded_git_suffix() -> None:
    repo = extract_repo_ref("https://github.com/Owner/Repo%2EGiT")

    assert repo is not None
    assert repo.owner == "Owner"
    assert repo.name == "Repo"


@pytest.mark.parametrize(
    "route",
    [
        "about",
        "apps",
        "collections",
        "enterprise",
        "events",
        "features",
        "marketplace",
        "orgs",
        "settings",
        "sponsors",
        "topics",
        "login",
        "logout",
        "join",
        "new",
        "notifications",
        "dashboard",
        "explore",
        "search",
        "site",
        "organizations",
        "users",
    ],
)
def test_extract_repo_ref_rejects_reserved_github_routes(route: str) -> None:
    assert extract_repo_ref(f"https://github.com/{route}/project") is None


def test_merge_observations_combines_trending_and_hn_signals_by_identity() -> None:
    merged = merge_observations(
        [
            observation("trending", "Owner", "Repo", source_rank=1, stars_today=42),
            observation("hacker_news", "owner", "repo", points=12, comments=3, item_id=99),
        ]
    )

    result = merged["owner/repo"]
    assert result["discovery_sources"] == {"trending", "hacker_news"}
    assert result["trending_rank"] == 1
    assert result["trending_stars_today"] == 42
    assert result["hn_points"] == 12
    assert result["hn_comments"] == 3
    assert result["hn_item_ids"] == [99]


def test_merge_observations_deduplicates_hn_item_ids_and_keeps_peak_metrics() -> None:
    merged = merge_observations(
        [
            observation("hacker_news", points=4, comments=8, item_id=5),
            observation("hacker_news", points=9, comments=2, item_id=5),
        ]
    )

    result = merged["owner/repo"]
    assert result["hn_points"] == 9
    assert result["hn_comments"] == 8
    assert result["hn_item_ids"] == [5]


def test_merge_observations_records_github_search_rank() -> None:
    merged = merge_observations([observation("github_search", source_rank=3)])

    assert merged["owner/repo"]["search_rank"] == 3


def test_merge_observations_is_order_independent_for_duplicate_signals() -> None:
    observations = [
        observation("trending", source_rank=3, stars_today=50),
        observation("trending", source_rank=1, stars_today=10),
        observation("trending", source_rank=1, stars_today=20),
        observation("github_search", source_rank=4),
        observation("github_search", source_rank=2),
        observation("hacker_news", points="7", comments=None, item_id=9),
        observation("hacker_news", points=True, comments=-2, item_id=3),
        observation("hacker_news", points="invalid", comments="11", item_id=9),
    ]
    expected = {
        "discovery_sources": {"trending", "github_search", "hacker_news"},
        "hn_item_ids": [3, 9],
        "hn_points": 7,
        "hn_comments": 11,
        "trending_rank": 1,
        "trending_stars_today": 20,
        "search_rank": 2,
    }

    for ordered_observations in permutations(observations):
        assert merge_observations(ordered_observations)["owner/repo"] == expected
