from datetime import UTC, datetime, timedelta, timezone
from math import log

import pytest

from github_daily_reporter.scoring import (
    momentum_signal,
    normalize_elapsed_velocity,
    rank_candidates,
    score_candidate,
    score_growth_candidate,
    score_mature_candidate,
)

NOW = datetime(2026, 7, 23, tzinfo=UTC)


def test_popularity_contributes_at_most_five_points(candidate_factory):
    score = score_candidate(candidate_factory(stars_total=10_000_000), NOW, quality_score=0)

    assert score.popularity == 100
    assert 0.05 * score.popularity == 5


def test_unknown_velocity_scores_zero_momentum(candidate_factory):
    score = score_candidate(candidate_factory(stars_24h=None), NOW, quality_score=50)

    assert score.momentum == 0


def test_known_velocity_with_unknown_growth_uses_zero_relative_velocity(candidate_factory):
    score = score_candidate(candidate_factory(stars_24h=100, growth_rate_24h=None), NOW, 0)

    assert score.momentum == round(100 * 0.70 * log(101) / log(1001), 6)


def test_momentum_caps_each_velocity_factor(candidate_factory):
    score = score_candidate(candidate_factory(stars_24h=1000, growth_rate_24h=4), NOW, 0)

    assert score.momentum == 100


def test_evidence_counts_sources_ranks_and_verified_hacker_news(candidate_factory):
    score = score_candidate(
        candidate_factory(
            discovery_sources={"trending", "github_search", "hacker_news"},
            trending_rank=5,
            search_rank=20,
            hn_points=1,
        ),
        NOW,
        0,
    )

    assert score.evidence == 90


def test_hacker_news_counters_require_a_hacker_news_discovery_source(candidate_factory):
    score = score_candidate(
        candidate_factory(hn_points=200, hn_comments=100, discovery_sources={"trending"}),
        NOW,
        0,
    )

    assert score.hacker_news == 0
    assert score.evidence == 0


def test_hacker_news_formula_is_applied_for_verified_discovery(candidate_factory):
    score = score_candidate(
        candidate_factory(
            hn_points=200, hn_comments=100, discovery_sources={"hacker_news"}
        ),
        NOW,
        0,
    )

    assert score.hacker_news == 100
    assert score.evidence == 15


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(days=7), 100),
        (timedelta(days=7, seconds=1), 75),
        (timedelta(days=30), 75),
        (timedelta(days=30, seconds=1), 50),
        (timedelta(days=90), 50),
        (timedelta(days=90, seconds=1), 25),
        (timedelta(days=180), 25),
        (timedelta(days=180, seconds=1), 0),
        (-timedelta(seconds=1), 100),
    ],
)
def test_freshness_uses_elapsed_seconds_and_clamps_future_dates(
    candidate_factory, age, expected
):
    score = score_candidate(candidate_factory(created_at=NOW - age), NOW, 0)

    assert score.freshness == expected


def test_score_normalizes_aware_timestamps_to_utc(candidate_factory):
    created_at = datetime(2026, 7, 16, 2, tzinfo=timezone(timedelta(hours=2)))

    score = score_candidate(candidate_factory(created_at=created_at), NOW, 0)

    assert score.freshness == 100


def test_activity_uses_reactivation_push_timestamp(candidate_factory):
    score = score_candidate(
        candidate_factory(
            created_at=NOW - timedelta(days=365),
            pushed_at=NOW - timedelta(days=1),
        ),
        NOW,
        0,
    )
    assert score.freshness == 100


def test_momentum_signal_tracks_provenance_and_discounts_estimates(candidate_factory):
    exact, exact_source = momentum_signal(candidate_factory(stars_24h=100))
    estimate, estimate_source = momentum_signal(
        candidate_factory(stars_24h=100, stars_24h_estimated=True)
    )
    assert exact_source == "exact"
    assert estimate_source == "snapshot_estimate"
    assert exact > estimate > 0


def test_mature_quality_does_not_change_numeric_score(candidate_factory):
    candidate = candidate_factory(stars_total=10_000, stars_24h=100)
    low = score_mature_candidate(candidate, NOW, quality_score=0)
    high = score_mature_candidate(candidate, NOW, quality_score=100)
    assert low.final == high.final


def test_mature_score_contains_relative_growth_component(candidate_factory):
    score = score_mature_candidate(
        candidate_factory(stars_total=10_000, stars_24h=100, growth_rate_24h=0.25),
        NOW,
        quality_score=100,
    )
    assert score.relative_growth == 100


def test_mature_momentum_component_is_absolute_only(candidate_factory):
    score = score_mature_candidate(
        candidate_factory(stars_total=10_000, stars_24h=100, growth_rate_24h=1),
        NOW,
        quality_score=0,
    )
    assert score.momentum == round(100 * log(101) / log(1001), 6)


@pytest.mark.parametrize("field", ["created_at", "now"])
def test_score_rejects_naive_datetimes(candidate_factory, field):
    candidate = candidate_factory()
    naive = datetime(2026, 7, 23)
    if field == "created_at":
        candidate = candidate.model_copy(update={"created_at": naive})

    with pytest.raises(ValueError, match="timezone-aware"):
        score_candidate(candidate, naive if field == "now" else NOW, 0)


def test_quality_input_is_clamped_and_final_uses_unrounded_components(candidate_factory):
    candidate = candidate_factory(stars_24h=1, growth_rate_24h=0.1, stars_total=1)

    score = score_candidate(candidate, NOW, quality_score=200)
    momentum = 100 * (0.70 * log(2) / log(1001) + 0.30 * 0.1)
    popularity = 100 * log(2) / log(50001)
    expected_final = round(0.40 * momentum + 0.20 * 100 + 0.05 * popularity + 10, 6)

    assert score.quality == 100
    assert score.final == expected_final


def test_ties_use_known_velocity_then_canonical_name(candidate_factory):
    a = candidate_factory(canonical_name="b/repo", stars_24h=None)
    b = candidate_factory(canonical_name="a/repo", stars_24h=0, growth_rate_24h=0)

    ranked = rank_candidates([(a, 50, False), (b, 50, False)], NOW)

    assert [item.candidate.canonical_name for item in ranked] == ["a/repo", "b/repo"]


def test_equal_final_scores_use_all_stable_tie_breakers(candidate_factory):
    later = candidate_factory(
        canonical_name="z/repo",
        stars_24h=1,
        growth_rate_24h=0,
        created_at=NOW - timedelta(days=1),
    )
    earlier = candidate_factory(
        canonical_name="a/repo",
        stars_24h=1,
        growth_rate_24h=0,
        created_at=NOW - timedelta(days=3),
    )

    ranked = rank_candidates([(earlier, 50, False), (later, 50, True)], NOW)

    assert [item.candidate.canonical_name for item in ranked] == ["z/repo", "a/repo"]
    assert ranked[0].quality_degraded is True


def test_growth_uses_versioned_six_component_weights(monkeypatch, candidate_factory):
    import github_daily_reporter.scoring as scoring

    monkeypatch.setattr(scoring, "_absolute_momentum_signal", lambda candidate: (80.0, "exact"))
    monkeypatch.setattr(scoring, "_relative_growth", lambda candidate, cohort="growth": 70.0)
    monkeypatch.setattr(scoring, "_evidence_v1", lambda candidate: 60.0)
    monkeypatch.setattr(scoring, "_freshness", lambda now, created_at: 90.0)
    monkeypatch.setattr(scoring, "_hacker_news", lambda candidate: 40.0)
    monkeypatch.setattr(scoring, "_popularity", lambda candidate: 20.0)

    score = score_growth_candidate(candidate_factory(), NOW)

    assert score.scoring_version == "agent-hybrid-v1"
    assert score.final == 70.5


def test_mature_relative_ratio_point_one_is_full_scale(candidate_factory):
    score = score_mature_candidate(candidate_factory(growth_rate_24h=0.01), NOW)
    assert score.relative_growth == 100.0


def test_new_scores_round_only_final_to_one_decimal(candidate_factory):
    score = score_growth_candidate(candidate_factory(stars_24h=17, growth_rate_24h=0.013), NOW)
    assert score.final == round(score.final, 1)


@pytest.mark.parametrize(
    ("delta", "elapsed_hours", "expected"),
    [(12, 12, 24.0), (36, 36, 24.0), (12, 36, 8.0)],
)
def test_normalize_elapsed_velocity_uses_observed_window(delta, elapsed_hours, expected):
    assert normalize_elapsed_velocity(delta, elapsed_hours) == expected


@pytest.mark.parametrize("elapsed_hours", [0, -1, 49, 100])
def test_normalize_elapsed_velocity_rejects_invalid_windows(elapsed_hours):
    with pytest.raises(ValueError):
        normalize_elapsed_velocity(1, elapsed_hours)
