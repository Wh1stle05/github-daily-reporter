import json
from datetime import UTC, datetime

import httpx
import pytest

from github_daily_reporter.config import ReporterConfig
from github_daily_reporter.llm import LlmReviewClient, LlmReviewError
from github_daily_reporter.models import LlmReviewEnvelope
from github_daily_reporter.selection import assign_cohort


def make_config() -> ReporterConfig:
    return ReporterConfig(
        timezone="UTC",
        github_token="github-secret",
        llm_base_url="https://llm.example/v1/",
        llm_model="test-model",
        llm_api_key="llm-secret",
        llm_timeout_seconds=7,
    )


def make_candidate(candidate_factory, **overrides):
    return candidate_factory(
        canonical_name="owner/repo",
        full_name="Owner/Repo",
        description="A useful project",
        quality_evidence=json.dumps({"readme_excerpt": "README"}),
        stars_total=120,
        forks_total=8,
        open_issues_count=2,
        stars_24h=10,
        growth_rate_24h=0.1,
        discovery_sources={"trending", "github_search"},
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
        pushed_at=datetime(2026, 7, 26, tzinfo=UTC),
        **overrides,
    )


def response_for(*names: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "reviews": [
                                {
                                    "canonical_name": name,
                                    "quality_score": 80,
                                    "exclude": False,
                                    "exclude_reason": None,
                                    "summary_zh": "简短介绍",
                                    "highlight_zh": "值得关注",
                                }
                                for name in names
                            ]
                        }
                    )
                }
            }
        ]
    }


@pytest.mark.asyncio
async def test_review_posts_one_bounded_request(respx_mock, candidate_factory):
    route = respx_mock.post("https://llm.example/v1/chat/completions").respond(
        200, json=response_for()
    )
    candidate = make_candidate(
        candidate_factory,
        source_errors=["do not send this"],
        is_fork=True,
        archived=True,
    )

    with pytest.raises(LlmReviewError, match="identity_mismatch"):
        await LlmReviewClient(make_config()).review([candidate])

    assert route.call_count == 1
    request = route.calls[0].request
    payload = json.loads(request.content)
    assert request.headers["authorization"] == "Bearer llm-secret"
    assert payload["temperature"] == 0
    assert payload["response_format"] == {"type": "json_object"}
    projection = json.loads(payload["messages"][1]["content"])
    assert projection["candidates"][0]["identity"] == "owner/repo"
    assert projection["candidates"][0]["cohort"] == assign_cohort(candidate)
    assert projection["candidates"][0]["quality_evidence"]
    assert projection["candidates"][0]["metrics"]["stars_total"] == 120
    assert "source_errors" not in projection["candidates"][0]
    assert "full_name" not in projection["candidates"][0]
    assert "html_url" not in projection["candidates"][0]
    assert "do not send this" not in payload["messages"][0]["content"]


@pytest.mark.asyncio
async def test_review_returns_envelope_for_exact_identities(respx_mock, candidate_factory):
    respx_mock.post("https://llm.example/v1/chat/completions").respond(
        200, json=response_for("owner/repo")
    )

    result = await LlmReviewClient(make_config()).review(
        [make_candidate(candidate_factory)]
    )

    assert isinstance(result, LlmReviewEnvelope)
    assert [review.canonical_name for review in result.reviews] == ["owner/repo"]


@pytest.mark.asyncio
async def test_review_rejects_duplicate_or_extra_identity(respx_mock, candidate_factory):
    respx_mock.post("https://llm.example/v1/chat/completions").respond(
        200, json=response_for("owner/repo", "owner/repo", "other/repo")
    )

    with pytest.raises(LlmReviewError, match="identity_mismatch"):
        await LlmReviewClient(make_config()).review(
            [make_candidate(candidate_factory)]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "category"),
    [
        (503, {"error": "upstream"}, "http_status"),
        (200, {"choices": [{"message": {"content": "not-json"}}]}, "invalid_json"),
        (200, {"choices": []}, "invalid_schema"),
    ],
)
async def test_review_sanitizes_response_failures(
    respx_mock, status, body, category, candidate_factory
):
    respx_mock.post("https://llm.example/v1/chat/completions").respond(
        status, json=body
    )

    with pytest.raises(LlmReviewError) as exc_info:
        await LlmReviewClient(make_config()).review(
            [make_candidate(candidate_factory)]
        )

    assert exc_info.value.category == category
    assert str(exc_info.value) == f"llm review failed: {category}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "category"),
    [
        (httpx.ReadTimeout("upstream timeout"), "timeout"),
        (httpx.ConnectError("connection refused"), "transport"),
    ],
)
async def test_review_sanitizes_transport_failures(
    respx_mock, error, category, candidate_factory
):
    respx_mock.post("https://llm.example/v1/chat/completions").mock(
        side_effect=error
    )

    with pytest.raises(LlmReviewError) as exc_info:
        await LlmReviewClient(make_config()).review(
            [make_candidate(candidate_factory)]
        )

    assert exc_info.value.category == category
    assert str(exc_info.value) == f"llm review failed: {category}"
