from datetime import UTC, datetime
import json

import pytest

from github_daily_reporter.editorial import validate_reports
from github_daily_reporter.models import CohortScoreBreakdown, RepositoryCandidate, RankedCandidate


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
