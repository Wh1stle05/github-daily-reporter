from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from github_daily_reporter.config import load_config
from github_daily_reporter.github_client import GitHubClient
from github_daily_reporter.models import RepositoryCandidate, CollectionEnvelope
from github_daily_reporter.pipeline import CollectionPipeline
from github_daily_reporter.state import StateStore


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
    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    return path


class EndToEndHarness:
    """Runs real reporter collection components against recorded HTTP responses."""

    def __init__(self, root: Path, respx_mock: Any) -> None:
        self.root = root
        self.respx_mock = respx_mock
        self.fixture_root = Path(__file__).parent / "fixtures"
        self.config_path = root / "config" / "reporter.yaml"
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(
            "\n".join(
                (
                    "timezone: UTC",
                    "state_db: data/reporter.sqlite3",
                    "max_candidates_per_source: 10",
                    "max_llm_candidates: 10",
                    "max_report_items: 10",
                    "velocity_threshold: 0",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")

    async def collect(
        self,
        *,
        failing_sources: set[str] | None = None,
        graphql_failure: bool = False,
    ) -> CollectionEnvelope:
        self._install_responses(failing_sources or set(), graphql_failure)
        config = load_config(self.config_path)
        store = StateStore(config.state_db)
        async with GitHubClient("test-token", max_attempts=1) as github, httpx.AsyncClient() as web:
            return await CollectionPipeline(config, store, github, web).collect(
                datetime(2026, 7, 24, 12, tzinfo=UTC)
            )

    def _install_responses(self, failing_sources: set[str], graphql_failure: bool) -> None:
        self.respx_mock.clear()
        payload = json.loads((self.fixture_root / "collection_success.json").read_text(encoding="utf-8"))
        status = 404
        self.respx_mock.get("https://github.com/trending").respond(
            status if "trending" in failing_sources else 200,
            text="" if "trending" in failing_sources else payload["trending_html"],
        )
        self.respx_mock.get("https://api.github.com/search/repositories").respond(
            status if "github_search" in failing_sources else 200,
            json={} if "github_search" in failing_sources else payload["search"],
        )
        self.respx_mock.get("https://hn.algolia.com/api/v1/search_by_date").respond(
            status if "hacker_news" in failing_sources else 200,
            json={} if "hacker_news" in failing_sources else payload["algolia"],
        )
        self.respx_mock.get("https://hacker-news.firebaseio.com/v0/showstories.json").respond(404)
        self.respx_mock.get("https://hacker-news.firebaseio.com/v0/item/123.json").respond(
            200, json=payload["hn_item"]
        )
        self.respx_mock.get("https://api.github.com/repos/acme/tool").respond(
            200, json=payload["repository"]
        )
        self.respx_mock.get("https://api.github.com/repos/acme/tool/readme").respond(
            200, json={"content": payload["readme"]}
        )
        self.respx_mock.post("https://api.github.com/graphql").respond(
            400 if graphql_failure else 200,
            json={"errors": [{"message": "unavailable"}]} if graphql_failure else payload["graphql"],
        )


@pytest.fixture
def e2e_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, respx_mock: Any) -> EndToEndHarness:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    return EndToEndHarness(tmp_path / "e2e", respx_mock)
