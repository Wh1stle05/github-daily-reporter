from datetime import UTC, datetime
import json

import pytest

from github_daily_reporter.editorial import (
    build_editorial_input,
    validate_reports,
    write_editorial_artifacts,
)
from github_daily_reporter.models import RepositoryCandidate, SourceHealth


def _candidate(name: str, stars: int, cohort: str) -> dict:
    return {
        "canonical_name": name,
        "full_name": name,
        "html_url": f"https://github.com/{name}",
        "created_at": datetime(2026, 7, 1, tzinfo=UTC).isoformat(),
        "pushed_at": datetime(2026, 7, 28, tzinfo=UTC).isoformat(),
        "stars_total": stars,
        "cohort": cohort,
        "rank": 1,
        "score": 82.4,
        "score_breakdown": {
            "cohort": cohort,
            "momentum_source": "exact",
            "momentum": 80,
            "relative_growth": 70,
            "evidence": 60,
            "quality": 0,
            "activity": 90,
            "hacker_news": 0,
            "popularity": 20,
            "final": 82.4,
        },
    }


def _input() -> dict:
    return {
        "schema_version": "agent-hybrid-v1",
        "run_id": "2026-07-29",
        "generated_at": "2026-07-29T00:00:00Z",
        "status": "success",
        "cohorts": {
            "growth": {"primary": [_candidate("a/growth", 100, "growth")], "reserve": []},
            "mature": {"primary": [_candidate("a/mature", 10000, "mature")], "reserve": []},
        },
    }


def test_validate_reports_requires_canonical_score_lines(tmp_path):
    handoff = _input()
    with pytest.raises(ValueError, match="score"):
        validate_reports(
            tmp_path,
            handoff,
            "# GitHub 成长项目榜 · 2026-07-29\n\n### 1. a/growth\nhttps://github.com/a/growth\n",
            "# GitHub 万星增量榜 · 2026-07-29\n\n### 1. a/mature\nhttps://github.com/a/mature\n- 综合评分：82.4/100\n",
        )


def test_validate_reports_rejects_score_tampering(tmp_path):
    handoff = _input()
    with pytest.raises(ValueError, match="score"):
        validate_reports(
            tmp_path,
            handoff,
            "# GitHub 成长项目榜 · 2026-07-29\n\n### 1. a/growth\nhttps://github.com/a/growth\n- 综合评分：12.4/100\n",
            "# GitHub 万星增量榜 · 2026-07-29\n\n### 1. a/mature\nhttps://github.com/a/mature\n- 综合评分：82.4/100\n",
        )


def test_validate_reports_rejects_duplicate_and_wrong_cohort_urls(tmp_path):
    handoff = _input()
    growth = (
        "# GitHub 成长项目榜 · 2026-07-29\n\n"
        "### 1. a/growth\nhttps://github.com/a/growth\n- 综合评分：82.4/100\n\n"
        "### 2. a/growth\nhttps://github.com/a/growth\n- 综合评分：82.4/100\n"
    )
    mature = "# GitHub 万星增量榜 · 2026-07-29\n\n### 1. a/mature\nhttps://github.com/a/mature\n- 综合评分：82.4/100\n"
    with pytest.raises(ValueError, match="duplicate"):
        validate_reports(tmp_path, handoff, growth, mature)

    wrong = "# GitHub 成长项目榜 · 2026-07-29\n\n### 1. a/mature\nhttps://github.com/a/mature\n- 综合评分：82.4/100\n"
    with pytest.raises(ValueError, match="cohort"):
        validate_reports(tmp_path, handoff, wrong, mature)


def test_validate_reports_rejects_out_of_pool_url(tmp_path):
    handoff = _input()
    growth = "# GitHub 成长项目榜 · 2026-07-29\n\n### 1. other/repo\nhttps://github.com/other/repo\n- 综合评分：82.4/100\n"
    mature = "# GitHub 万星增量榜 · 2026-07-29\n\n### 1. a/mature\nhttps://github.com/a/mature\n- 综合评分：82.4/100\n"
    with pytest.raises(ValueError, match="pool"):
        validate_reports(tmp_path, handoff, growth, mature)


def test_build_editorial_input_writes_bounded_two_cohort_pools(tmp_path, candidate_factory):
    now = datetime(2026, 7, 29, tzinfo=UTC)
    candidates = []
    for index in range(30):
        candidates.append(
            candidate_factory(
                canonical_name=f"growth/repo-{index}",
                full_name=f"growth/repo-{index}",
                html_url=f"https://github.com/growth/repo-{index}",
                stars_total=100 + index,
                stars_24h=100 - index,
                growth_rate_24h=0.05,
                quality_evidence=json.dumps({"readme_excerpt": "R" * 5_000}),
                discovery_sources={"trending", "github_search"},
            )
        )
        candidates.append(
            candidate_factory(
                canonical_name=f"mature/repo-{index}",
                full_name=f"mature/repo-{index}",
                html_url=f"https://github.com/mature/repo-{index}",
                stars_total=10_000 + index,
                stars_24h=100 - index,
                growth_rate_24h=0.005,
                quality_evidence=json.dumps({"readme_excerpt": "M" * 5_000}),
                discovery_sources={"trending", "github_search"},
            )
        )
    run_dir = tmp_path / "data" / "runs" / "github-daily-report-2026-07-29"
    envelope = build_editorial_input(
        candidates,
        [SourceHealth(source="trending", status="success", item_count=30)],
        run_dir,
        now=now,
    )
    assert len(envelope.cohorts["growth"].primary) == 20
    assert len(envelope.cohorts["growth"].reserve) == 5
    assert len(envelope.cohorts["mature"].primary) == 20
    assert len(envelope.cohorts["mature"].reserve) == 5
    assert max(len(item.readme_excerpt.encode("utf-8")) for item in envelope.cohorts["growth"].primary) <= 1_800

    write_editorial_artifacts(envelope, run_dir, attempt_id="attempt-1")
    assert (run_dir / "collection.json").is_file()
    assert (run_dir / "editorial-input.json").is_file()
    assert (run_dir / "run-status.json").is_file()
    assert list((run_dir / "evidence").glob("*.json"))
    assert (run_dir / "attempts" / "attempt-1").is_dir()


def test_build_editorial_input_marks_partial_when_a_cohort_has_fewer_than_ten(
    tmp_path, candidate_factory
):
    now = datetime(2026, 7, 29, tzinfo=UTC)
    candidates = [
        candidate_factory(
            canonical_name=f"mature/repo-{index}",
            full_name=f"mature/repo-{index}",
            html_url=f"https://github.com/mature/repo-{index}",
            stars_total=10_000 + index,
            stars_24h=10,
            discovery_sources={"trending"},
        )
        for index in range(3)
    ]
    envelope = build_editorial_input(candidates, [], tmp_path / "run", now=now)
    assert envelope.status == "partial"
    assert envelope.available_counts["mature"] == 3
