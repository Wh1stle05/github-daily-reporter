"""Deterministic scoring for legacy and agent-hybrid daily reports."""

from collections.abc import Iterable
from datetime import UTC, datetime
from math import log
from numbers import Real

from github_daily_reporter.models import (
    CohortScoreBreakdown,
    MomentumSource,
    RankedCandidate,
    RepositoryCandidate,
    ScoreBreakdown,
)


SCORING_VERSION = "agent-hybrid-v1"
MAX_SNAPSHOT_AGE_HOURS = 48.0


def score_candidate(
    candidate: RepositoryCandidate, now: datetime, quality_score: float
) -> ScoreBreakdown:
    """Calculate the pre-hybrid legacy score.

    This adapter remains for old callers while the active path uses the cohort
    functions below. It intentionally preserves the former quality-weighted
    formula until the compatibility command is retired.
    """
    now_utc = _as_utc(now)
    created_at = _activity_timestamp(candidate)
    momentum = _momentum(candidate)
    evidence = _evidence(candidate)
    freshness = _freshness(now_utc, created_at)
    hacker_news = _hacker_news(candidate)
    quality = _clamp(quality_score)
    popularity = _popularity(candidate)
    final = (
        0.40 * momentum
        + 0.15 * evidence
        + 0.10 * freshness
        + 0.10 * hacker_news
        + 0.20 * quality
        + 0.05 * popularity
    )
    return ScoreBreakdown(
        momentum=_round_component(momentum),
        evidence=_round_component(evidence),
        freshness=_round_component(freshness),
        hacker_news=_round_component(hacker_news),
        quality=_round_component(quality),
        popularity=_round_component(popularity),
        final=_round_component(final),
    )


def rank_candidates(
    items: Iterable[tuple[RepositoryCandidate, float, bool]], now: datetime
) -> list[RankedCandidate]:
    """Score legacy items and return their deterministic order."""
    ranked = [
        RankedCandidate(
            candidate=candidate,
            score=score_candidate(candidate, now, quality_score),
            quality_degraded=quality_degraded,
        )
        for candidate, quality_score, quality_degraded in items
    ]
    return sorted(ranked, key=ranking_key)


def ranking_key(item: RankedCandidate) -> tuple[float | bool | int | str, ...]:
    candidate = item.candidate
    return (
        -item.score.final,
        candidate.stars_24h is None,
        -(candidate.stars_24h or 0),
        -(candidate.growth_rate_24h or 0),
        -candidate.discovery_source_count,
        -candidate.hn_points,
        -_activity_timestamp(candidate).timestamp(),
        # A real review wins a tie over the old default quality=50 result.
        item.quality_degraded,
        candidate.canonical_name,
    )





def score_growth_candidate(
    candidate: RepositoryCandidate,
    now: datetime,
    quality_score: float | None = None,
) -> CohortScoreBreakdown:
    """Score a 1--9,999 Star candidate without an LLM quality dimension."""
    del quality_score  # accepted only for source compatibility; never affects score
    now_utc = _as_utc(now)
    absolute, source = _absolute_momentum_signal(candidate)
    relative = _relative_growth(candidate, "growth")
    evidence = _evidence_v1(candidate)
    activity = _freshness(now_utc, _activity_timestamp(candidate))
    hacker_news = _hacker_news(candidate)
    popularity = _popularity(candidate)
    final = (
        0.35 * absolute
        + 0.20 * relative
        + 0.20 * evidence
        + 0.15 * activity
        + 0.05 * hacker_news
        + 0.05 * popularity
    )
    return CohortScoreBreakdown(
        scoring_version=SCORING_VERSION,
        cohort="growth",
        momentum_source=source,
        momentum=_round_component(absolute),
        relative_growth=_round_component(relative),
        evidence=_round_component(evidence),
        quality=0,
        activity=_round_component(activity),
        hacker_news=_round_component(hacker_news),
        popularity=_round_component(popularity),
        final=_round_final(final),
    )


def score_mature_candidate(
    candidate: RepositoryCandidate,
    now: datetime,
    quality_score: float | None = None,
) -> CohortScoreBreakdown:
    """Score a repository with at least 10,000 Stars."""
    del quality_score
    now_utc = _as_utc(now)
    absolute, source = _absolute_momentum_signal(candidate)
    relative = _relative_growth(candidate, "mature")
    evidence = _evidence_v1(candidate)
    activity = _freshness(now_utc, _activity_timestamp(candidate))
    hacker_news = _hacker_news(candidate)
    popularity = _popularity(candidate)
    final = (
        0.50 * absolute
        + 0.20 * relative
        + 0.10 * evidence
        + 0.10 * activity
        + 0.05 * hacker_news
        + 0.05 * popularity
    )
    return CohortScoreBreakdown(
        scoring_version=SCORING_VERSION,
        cohort="mature",
        momentum_source=source,
        momentum=_round_component(absolute),
        relative_growth=_round_component(relative),
        evidence=_round_component(evidence),
        quality=0,
        activity=_round_component(activity),
        hacker_news=_round_component(hacker_news),
        popularity=_round_component(popularity),
        final=_round_final(final),
    )


def cohort_ranking_key(item: RankedCandidate) -> tuple[float | bool | int | str, ...]:
    """Stable ordering for candidates already assigned to one cohort."""
    score = item.score
    candidate = item.candidate
    known = getattr(score, "momentum_source", "unknown") != "unknown"
    return (
        -score.final,
        not known,
        -score.momentum,
        -candidate.discovery_source_count,
        -candidate.hn_points,
        -_activity_timestamp(candidate).timestamp(),
        item.quality_degraded,
        candidate.canonical_name,
    )


def normalize_elapsed_velocity(delta: int | float, elapsed_hours: int | float) -> float:
    """Convert a non-negative observed Star delta to a 24-hour rate."""
    if isinstance(delta, bool) or not isinstance(delta, Real) or delta < 0:
        raise ValueError("delta must be non-negative")
    if isinstance(elapsed_hours, bool) or not isinstance(elapsed_hours, Real):
        raise ValueError("elapsed_hours must be positive")
    elapsed = float(elapsed_hours)
    if elapsed <= 0 or elapsed > MAX_SNAPSHOT_AGE_HOURS:
        raise ValueError("elapsed_hours must be between 0 and 48")
    return float(delta) * 24.0 / elapsed


def _momentum(candidate: RepositoryCandidate) -> float:
    """Former blended momentum used only by the compatibility rank command."""
    if candidate.stars_24h is None:
        return 0.0
    absolute_velocity = _unit_interval(log(1 + candidate.stars_24h) / log(1001))
    relative_velocity = _unit_interval(candidate.growth_rate_24h or 0.0)
    return _clamp(100 * (0.70 * absolute_velocity + 0.30 * relative_velocity))


def _absolute_momentum_signal(
    candidate: RepositoryCandidate,
) -> tuple[float, MomentumSource]:
    if candidate.stars_24h is not None:
        velocity = (
            candidate.velocity_rate_24h
            if candidate.velocity_rate_24h is not None
            else candidate.stars_24h
        )
        absolute = _clamp(100 * _unit_interval(log(1 + velocity) / log(1001)))
        estimated = candidate.stars_24h_estimated or candidate.velocity_source == "snapshot_estimate"
        multiplier = 0.90 if estimated else 1.00
        source: MomentumSource = "snapshot_estimate" if estimated else "exact"
        return _round_component(absolute * multiplier), source
    if candidate.trending_stars_today is not None and "trending" in candidate.discovery_sources:
        absolute = _unit_interval(log(1 + candidate.trending_stars_today) / log(1001))
        return _round_component(100 * absolute * 0.80), "trending_proxy"
    return 0.0, "unknown"


def _relative_growth(candidate: RepositoryCandidate, cohort: str = "growth") -> float:
    ratio = max(float(candidate.growth_rate_24h or 0.0), 0.0)
    cap = 0.10 if cohort == "growth" else 0.01
    return _clamp(100 * ratio / cap)


def _evidence_v1(candidate: RepositoryCandidate) -> float:
    """Source confirmation and ranks; HN engagement is scored separately."""
    score = min(20 * max(candidate.discovery_source_count - 1, 0), 40)
    if candidate.trending_rank is not None:
        if candidate.trending_rank <= 5:
            score += 25
        elif candidate.trending_rank <= 15:
            score += 15
        else:
            score += 8
    if candidate.search_rank is not None and candidate.search_rank <= 20:
        score += 10
    return _clamp(score)


def _evidence(candidate: RepositoryCandidate) -> float:
    """Former evidence formula retained for old score_candidate tests."""
    score = _evidence_v1(candidate)
    if "hacker_news" in candidate.discovery_sources and (
        candidate.hn_points > 0 or candidate.hn_comments > 0
    ):
        score += 15
    return _clamp(score)


def _freshness(now: datetime, created_at: datetime) -> float:
    age_seconds = max((now - created_at).total_seconds(), 0.0)
    if age_seconds <= 7 * 24 * 60 * 60:
        return 100.0
    if age_seconds <= 30 * 24 * 60 * 60:
        return 75.0
    if age_seconds <= 90 * 24 * 60 * 60:
        return 50.0
    if age_seconds <= 180 * 24 * 60 * 60:
        return 25.0
    return 0.0


def _hacker_news(candidate: RepositoryCandidate) -> float:
    if "hacker_news" not in candidate.discovery_sources:
        return 0.0
    points_score = _unit_interval(log(1 + candidate.hn_points) / log(201))
    comments_score = _unit_interval(log(1 + candidate.hn_comments) / log(101))
    return _clamp(100 * (0.60 * points_score + 0.40 * comments_score))


def _popularity(candidate: RepositoryCandidate) -> float:
    return _clamp(100 * _unit_interval(log(1 + candidate.stars_total) / log(50001)))


def _unit_interval(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _clamp(value: float) -> float:
    return min(max(float(value), 0.0), 100.0)


def _round_component(value: float) -> float:
    return round(_clamp(value), 6)


def _round_final(value: float) -> float:
    return round(_clamp(value), 1)





def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _activity_timestamp(candidate: RepositoryCandidate) -> datetime:
    created = _as_utc(candidate.created_at)
    if candidate.pushed_at is None:
        return created
    return max(created, _as_utc(candidate.pushed_at))
