from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from github_daily_reporter.models import (
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
