from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from github_daily_reporter.models import (
    CohortScoreBreakdown,
    DailyRunResult,
    DeliveryPart,
    LlmReview,
    LlmReviewEnvelope,
    QualityEnvelope,
    RepositoryCandidate,
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


def test_quality_envelope_rejects_scores_outside_zero_to_five() -> None:
    with pytest.raises(ValidationError):
        QualityEnvelope.model_validate(
            {
                "run_id": "run-1",
                "reviews": [
                    {
                        "canonical_name": "owner/repo",
                        "usefulness": 6,
                        "completeness": 3,
                        "novelty": 3,
                        "maintenance": 3,
                    }
                ],
            }
        )


def test_llm_review_accepts_bounded_structured_copy() -> None:
    review = LlmReview.model_validate(
        {
            "canonical_name": "owner/repo",
            "quality_score": 100,
            "exclude": False,
            "summary_zh": "简短介绍",
            "highlight_zh": "值得关注",
        }
    )

    envelope = LlmReviewEnvelope(reviews=[review])

    assert envelope.reviews == [review]


def test_llm_review_rejects_score_above_one_hundred() -> None:
    with pytest.raises(ValidationError):
        LlmReview.model_validate(
            {
                "canonical_name": "a/repo",
                "quality_score": 101,
                "exclude": False,
                "summary_zh": "x",
                "highlight_zh": "y",
            }
        )


def test_llm_review_rejects_non_integer_score() -> None:
    with pytest.raises(ValidationError):
        LlmReview.model_validate(
            {
                "canonical_name": "a/repo",
                "quality_score": "50",
                "exclude": False,
                "summary_zh": "x",
                "highlight_zh": "y",
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (("summary_zh", "x" * 161), ("highlight_zh", "y" * 241)),
)
def test_llm_review_rejects_oversized_copy(field: str, value: str) -> None:
    payload = {
        "canonical_name": "a/repo",
        "quality_score": 50,
        "exclude": False,
        "summary_zh": "x",
        "highlight_zh": "y",
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        LlmReview.model_validate(payload)


def test_llm_review_requires_reason_when_excluded() -> None:
    with pytest.raises(ValidationError, match="exclude_reason"):
        LlmReview.model_validate(
            {
                "canonical_name": "a/repo",
                "quality_score": 50,
                "exclude": True,
                "exclude_reason": "  ",
                "summary_zh": "x",
                "highlight_zh": "y",
            }
        )


def test_direct_runtime_result_and_delivery_contracts() -> None:
    score = CohortScoreBreakdown(
        cohort="growth",
        momentum_source="trending_proxy",
        momentum=80,
        relative_growth=0,
        evidence=50,
        quality=75,
        activity=100,
        hacker_news=20,
        popularity=30,
        final=70,
    )
    result = DailyRunResult(run_id="run-1", status="delivered")
    delivery = DeliveryPart(
        run_id="run-1",
        part_index=0,
        body="message",
        digest="abc123",
    )

    assert score.momentum_source == "trending_proxy"
    assert result.growth == []
    assert result.mature == []
    assert delivery.state == "pending"
    assert delivery.attempts == 0
