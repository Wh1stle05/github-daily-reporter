import pytest
from datetime import UTC, datetime

from pydantic import ValidationError

from github_daily_reporter.models import (
    CohortScoreBreakdown,
    DeliveryPart,
    RankedCandidate,
    RepositoryCandidate,
    ScoreBreakdown,
    SourceObservation,
)


def test_candidate_derives_discovery_source_count() -> None:
    candidate = RepositoryCandidate(
        canonical_name="owner/repo",
        full_name="Owner/Repo",
        html_url="https://github.com/Owner/Repo",
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
        stars_total=100,
        discovery_sources={"trending", "github_search"},
    )

    assert candidate.discovery_source_count == 2


def test_velocity_is_not_a_discovery_source() -> None:
    with pytest.raises(ValidationError):
        RepositoryCandidate(
            canonical_name="owner/repo",
            full_name="Owner/Repo",
            html_url="https://github.com/Owner/Repo",
            created_at=datetime.now(UTC),
            discovery_sources={"star_velocity"},
        )


def test_delivery_part_contract() -> None:
    delivery = DeliveryPart(
        run_id="run-1",
        part_index=0,
        body="message",
        digest="abc123",
    )

    assert delivery.state == "pending"
    assert delivery.attempts == 0


def test_ranked_candidate_accepts_cohort_score_breakdown() -> None:
    candidate = RepositoryCandidate(
        canonical_name="owner/repo",
        full_name="Owner/Repo",
        html_url="https://github.com/Owner/Repo",
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    score = CohortScoreBreakdown(
        cohort="growth",
        momentum_source="exact",
        momentum=80,
        evidence=50,
        quality=75,
        activity=100,
        hacker_news=20,
        popularity=30,
        final=70,
    )

    ranked = RankedCandidate(candidate=candidate, score=score)

    assert ranked.score == score


def test_ranked_candidate_keeps_accepting_legacy_score_breakdown() -> None:
    candidate = RepositoryCandidate(
        canonical_name="owner/repo",
        full_name="Owner/Repo",
        html_url="https://github.com/Owner/Repo",
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    score = ScoreBreakdown(
        momentum=80,
        evidence=50,
        freshness=100,
        hacker_news=20,
        quality=75,
        popularity=30,
        final=70,
    )

    ranked = RankedCandidate(candidate=candidate, score=score)

    assert ranked.score == score
