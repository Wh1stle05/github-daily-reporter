from collections.abc import Iterable
from datetime import UTC, datetime
from math import log

from github_daily_reporter.models import RankedCandidate, RepositoryCandidate, ScoreBreakdown


def score_candidate(
    candidate: RepositoryCandidate, now: datetime, quality_score: float
) -> ScoreBreakdown:
    """Calculate the reproducible score components for one repository candidate."""
    now_utc = _as_utc(now)
    created_at = _as_utc(candidate.created_at)

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
        momentum=_round_score(momentum),
        evidence=_round_score(evidence),
        freshness=_round_score(freshness),
        hacker_news=_round_score(hacker_news),
        quality=_round_score(quality),
        popularity=_round_score(popularity),
        final=_round_score(final),
    )


def rank_candidates(
    items: Iterable[tuple[RepositoryCandidate, float, bool]], now: datetime
) -> list[RankedCandidate]:
    """Score candidates and return the specified deterministic ranking order."""
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
        -_as_utc(candidate.created_at).timestamp(),
        candidate.canonical_name,
    )


def _momentum(candidate: RepositoryCandidate) -> float:
    if candidate.stars_24h is None:
        return 0.0

    absolute_velocity = _unit_interval(log(1 + candidate.stars_24h) / log(1001))
    relative_velocity = _unit_interval(candidate.growth_rate_24h or 0.0)
    return _clamp(100 * (0.70 * absolute_velocity + 0.30 * relative_velocity))


def _evidence(candidate: RepositoryCandidate) -> float:
    score = min(20 * max(candidate.discovery_source_count - 1, 0), 40)
    if candidate.trending_rank is not None:
        if candidate.trending_rank <= 5:
            score += 25
        elif candidate.trending_rank <= 15:
            score += 15
        else:
            score += 8
    if "hacker_news" in candidate.discovery_sources and (
        candidate.hn_points > 0 or candidate.hn_comments > 0
    ):
        score += 15
    if candidate.search_rank is not None and candidate.search_rank <= 20:
        score += 10
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


def _round_score(value: float) -> float:
    return round(_clamp(value), 6)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)
