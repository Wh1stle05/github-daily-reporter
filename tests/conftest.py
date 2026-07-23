from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from github_daily_reporter.models import RepositoryCandidate


def make_candidate(**overrides: Any) -> RepositoryCandidate:
    values = {
        "canonical_name": "owner/repo",
        "full_name": "Owner/Repo",
        "html_url": "https://github.com/Owner/Repo",
        "created_at": datetime(2026, 7, 20, tzinfo=UTC),
        "stars_total": 100,
        "discovery_sources": {"trending"},
    }
    values.update(overrides)
    return RepositoryCandidate(**values)


@pytest.fixture
def candidate() -> RepositoryCandidate:
    return make_candidate()


@pytest.fixture
def candidate_factory():
    return make_candidate


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config" / "reporter.yaml"
    path.parent.mkdir()
    path.write_text(
        "timezone: Asia/Shanghai\nstate_db: data/reporter.sqlite3\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    return path
