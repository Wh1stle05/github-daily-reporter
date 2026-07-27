from datetime import date
from types import SimpleNamespace

from github_daily_reporter.render import render_failure_alert, render_report


def _ranked(name: str, stars: int = 100, *, momentum_source: str = "exact"):
    candidate = SimpleNamespace(
        canonical_name=name,
        full_name=name,
        html_url=f"https://github.com/{name}",
        stars_total=stars,
        stars_24h=12,
        stars_24h_estimated=False,
        trending_stars_today=None,
        primary_language="Python",
        license_spdx="MIT",
        discovery_sources={"trending", "github_search"},
        source_errors=[],
    )
    score = SimpleNamespace(momentum_source=momentum_source)
    review = SimpleNamespace(summary_zh="简洁介绍", highlight_zh="值得关注")
    return SimpleNamespace(candidate=candidate, score=score, review=review)


def test_render_report_has_two_ordered_cohort_sections_and_limits_items():
    growth = [_ranked(f"growth/repo{i}") for i in range(8)]
    mature = [_ranked(f"mature/repo{i}", stars=10_000) for i in range(6)]

    text = render_report(date(2026, 7, 27), growth, mature, source_health=[])

    assert "# GitHub 每日趋势 · 2026-07-27" in text
    assert "## 成长项目榜" in text
    assert "## 万星增量榜" in text
    assert text.index("growth/repo0") < text.index("growth/repo1")
    assert "growth/repo6" not in text
    assert "mature/repo4" not in text


def test_render_report_hard_caps_cohorts_when_caller_requests_larger_limits():
    growth = [_ranked(f"growth/repo{i}") for i in range(8)]
    mature = [_ranked(f"mature/repo{i}", stars=10_000) for i in range(6)]

    text = render_report(
        date(2026, 7, 27),
        growth,
        mature,
        source_health=[],
        growth_limit=99,
        mature_limit=99,
    )

    assert "growth/repo5" in text
    assert "growth/repo6" not in text
    assert "mature/repo3" in text
    assert "mature/repo4" not in text


def test_render_report_data_notes_only_when_needed():
    healthy = [_ranked("owner/repo")]
    text = render_report(date(2026, 7, 27), healthy, [], source_health=[])
    assert "## 数据说明" not in text

    degraded = render_report(
        date(2026, 7, 27), healthy, [],
        source_health=[SimpleNamespace(source="trending", status="degraded", item_count=1)],
    )
    assert "## 数据说明" in degraded
    assert "trending" in degraded

    estimated = render_report(
        date(2026, 7, 27), [_ranked("owner/repo", momentum_source="snapshot_estimate")], [], source_health=[]
    )
    assert "## 数据说明" in estimated
    assert "估算" in estimated


def test_failure_alert_excludes_raw_error_and_secrets():
    alert = render_failure_alert(
        "run-123", "llm", "timeout",
        [SimpleNamespace(source="github_search", status="failed", item_count=0, error="Bearer super-secret")],
    )
    assert "run-123" in alert
    assert "llm" in alert and "timeout" in alert
    assert "github_search" in alert
    assert "super-secret" not in alert
    assert "Bearer" not in alert


def test_failure_alert_allowlists_caller_controlled_identifiers():
    hostile = "delivery\nBearer super-secret [x](https://attacker.invalid)"

    alert = render_failure_alert(
        hostile,
        hostile,
        hostile,
        [SimpleNamespace(source=hostile, status=hostile, item_count=hostile)],
    )

    assert hostile not in alert
    assert "super-secret" not in alert
    assert "attacker.invalid" not in alert
    assert "run_id：unknown" in alert
    assert "阶段：unknown" in alert
    assert "类别：unknown" in alert
    assert "unknown: unknown（0 条）" in alert


def test_render_escapes_markdown_from_llm_copy():
    ranked = _ranked("owner/repo")
    ranked.review = SimpleNamespace(
        summary_zh="[model link](https://attacker.invalid)",
        highlight_zh="# fake heading",
    )

    text = render_report(date(2026, 7, 27), [ranked], [], source_health=[])

    assert "[model link](https://attacker.invalid)" not in text
    assert "\\[model link\\]\\(https://attacker\\.invalid\\)" in text
    assert "\\# fake heading" in text
