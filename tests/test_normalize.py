from datetime import UTC, datetime

from github_daily_reporter.models import SourceObservation
from github_daily_reporter.normalize import extract_repo_ref, merge_observations


def observation(
    source: str,
    owner: str = "Owner",
    name: str = "Repo",
    source_rank: int | None = None,
    **source_metadata: int,
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
