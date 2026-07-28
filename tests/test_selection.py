from datetime import UTC, datetime, timedelta

from github_daily_reporter.scoring import score_growth_candidate, score_mature_candidate
from github_daily_reporter.selection import assign_cohort, select_review_candidates

NOW = datetime(2026, 7, 23, tzinfo=UTC)


def test_assign_cohort_splits_at_ten_thousand(candidate_factory):
    assert assign_cohort(candidate_factory(stars_total=9_999, stars_24h=1)) == "growth"
    assert assign_cohort(candidate_factory(stars_total=10_000)) == "mature"


def test_growth_rejects_one_star_search_only_candidate(candidate_factory):
    candidate = candidate_factory(
        stars_total=1, stars_24h=None, discovery_sources={"github_search"}
    )
    assert assign_cohort(candidate) is None


def test_trending_velocity_is_discounted_proxy(candidate_factory):
    candidate = candidate_factory(
        stars_24h=None, trending_stars_today=500, discovery_sources={"trending"}
    )
    score = score_growth_candidate(candidate, NOW, quality_score=50)
    assert score.momentum_source == "trending_proxy"
    assert score.momentum > 0


def test_selection_applies_cohort_caps_and_growth_evidence_reserve(candidate_factory):
    growth = [
        candidate_factory(
            canonical_name=f"owner/g{i}",
            stars_total=i + 1,
            stars_24h=100 if i < 2 else None,
            trending_rank=1 if i >= 2 else None,
            discovery_sources={"trending"} if i < 2 else {"github_search", "hacker_news"},
            hn_points=100 if i >= 2 else 0,
        )
        for i in range(6)
    ]
    mature = [
        candidate_factory(canonical_name=f"owner/m{i}", stars_total=10_000 + i)
        for i in range(3)
    ]
    selected = select_review_candidates(
        growth + mature, NOW, growth_cap=3, mature_cap=2, growth_reserve=1
    )
    assert len(selected["growth"]) == 3
    assert len(selected["mature"]) == 2
    assert "owner/g2" in {item.candidate.canonical_name for item in selected["growth"]}
    assert not (
        {item.candidate.canonical_name for item in selected["growth"]}
        & {item.candidate.canonical_name for item in selected["mature"]}
    )


def test_selection_is_deterministic_for_ties(candidate_factory):
    candidates = [
        candidate_factory(
            canonical_name=name,
            stars_total=10_000,
            stars_24h=2,
            created_at=NOW - timedelta(days=2),
            pushed_at=NOW - timedelta(days=1),
        )
        for name in ("z/repo", "a/repo", "m/repo")
    ]
    selected = select_review_candidates(reversed(candidates), NOW, mature_cap=3)
    assert [x.candidate.canonical_name for x in selected["mature"]] == [
        "a/repo",
        "m/repo",
        "z/repo",
    ]


def test_selection_deduplicates_repeated_candidate_identity(candidate_factory):
    candidate = candidate_factory(canonical_name="owner/repo", stars_total=10_000)
    selected = select_review_candidates([candidate, candidate], NOW)
    assert [item.candidate.canonical_name for item in selected["mature"]] == [
        "owner/repo"
    ]
