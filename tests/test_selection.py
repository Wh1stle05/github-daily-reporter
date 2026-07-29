from datetime import UTC, datetime

from github_daily_reporter.scoring import score_growth_candidate, score_mature_candidate
from github_daily_reporter.selection import assign_cohort

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
