from pathlib import Path
import stat


ROOT = Path(__file__).parents[1]


def test_no_agent_wrapper_runs_daily_only():
    wrapper = ROOT / "deploy/hermes/github-daily-run.sh"
    text = wrapper.read_text(encoding="utf-8")
    assert text == """#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="${GITHUB_DAILY_REPORTER_HOME:-$HOME/workspace/github-daily-reporter}"
cd "$PROJECT_ROOT"
exec .venv/bin/python -m github_daily_reporter.cli daily --config config/reporter.yaml
"""
    assert wrapper.stat().st_mode & stat.S_IXUSR
    assert "github-daily-editor" not in text
    assert " cli collect" not in text


def test_legacy_agent_assets_are_removed():
    assert not (ROOT / "deploy/hermes/github-daily-collect.sh").exists()
    assert not (ROOT / "deploy/hermes/skills/github-daily-editor/SKILL.md").exists()
