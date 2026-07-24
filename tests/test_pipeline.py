import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from github_daily_reporter.config import ReporterConfig
from github_daily_reporter.models import CollectorResult, SourceHealth, SourceObservation
from github_daily_reporter.pipeline import CollectionPipeline, serialize_collection_envelope
from github_daily_reporter.state import StateStore


class Probes:
    def __init__(self) -> None:
        self.current = 0
        self.max_simultaneous = 0
        self.started = asyncio.Event()

    async def collect(self, source: str, now: datetime, limit: int) -> CollectorResult:
        self.current += 1
        self.started.set()
        self.max_simultaneous = max(self.max_simultaneous, self.current)
        try:
            await asyncio.sleep(0)
            return CollectorResult(
                source=source,  # type: ignore[arg-type]
                observations=[
                    SourceObservation(
                        source=source,  # type: ignore[arg-type]
                        repository_url=f"https://github.com/owner/repo{index}",
                        owner="owner",
                        name=f"repo{index}",
                        observed_at=now,
                        source_rank=1,
                    )
                    for index in range(limit)
                ],
                health=SourceHealth(source=source, status="success", item_count=limit),
            )
        finally:
            self.current -= 1


class FakeGitHub:
    def __init__(
        self,
        *,
        archived_names: set[str] | None = None,
        description: str | None = None,
        readme: str = "# README",
    ) -> None:
        self.archived_names = archived_names or set()
        self.description = description
        self.readme = readme
        self.readme_requests: list[str] = []

    async def get_repository(self, canonical_name: str, signals: dict[str, Any]):
        from github_daily_reporter.models import RepositoryCandidate

        return RepositoryCandidate(
            canonical_name=canonical_name,
            full_name=canonical_name,
            html_url=f"https://github.com/{canonical_name}",
            created_at=datetime(2026, 7, 20, tzinfo=UTC),
            stars_total=100,
            archived=canonical_name in self.archived_names,
            description=self.description,
            **signals,
        )

    async def get_readme_excerpt(self, canonical_name: str, max_chars: int = 2000) -> str:
        self.readme_requests.append(canonical_name)
        return self.readme

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {
            "repository": {
                "stargazerCount": 1,
                "stargazers": {
                    "edges": [{"starredAt": "2026-07-24T00:00:00Z"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        }


@pytest.fixture
def pipeline_factory(tmp_path: Path):
    def make(
        *,
        failing_sources: set[str] | None = None,
        candidate_count: int = 1,
        max_llm_candidates: int = 40,
        tracked_name: str | None = None,
        archived_names: set[str] | None = None,
        state_db: Path | None = None,
        project_root: Path | None = None,
        description: str | None = None,
        readme: str = "# README",
        collection_timeout_seconds: float = 180,
        delayed_sources: set[str] | None = None,
        discovery_gate: asyncio.Event | None = None,
    ):
        failing_sources = failing_sources or set()
        config = ReporterConfig(
            timezone="UTC",
            github_token="test-token",
            max_candidates_per_source=candidate_count,
            max_llm_candidates=max_llm_candidates,
            velocity_threshold=0,
            collection_timeout_seconds=collection_timeout_seconds,
            state_db=state_db or tmp_path / "data" / "reporter.sqlite3",
            project_root=project_root,
        )
        probes = Probes()
        store = StateStore(config.state_db)
        if tracked_name:
            from github_daily_reporter.models import RepositoryCandidate

            store.record_snapshot(
                RepositoryCandidate(
                    canonical_name=tracked_name,
                    full_name=tracked_name,
                    html_url=f"https://github.com/{tracked_name}",
                    created_at=datetime(2026, 7, 20, tzinfo=UTC),
                    stars_total=100,
                ),
                datetime(2026, 7, 23, tzinfo=UTC),
            )
        github = FakeGitHub(
            archived_names=archived_names,
            description=description,
            readme=readme,
        )

        async def collector(source: str, now: datetime, limit: int):
            if source in failing_sources:
                raise RuntimeError(f"{source} unavailable")
            if source in (delayed_sources or set()):
                if discovery_gate is None:
                    await asyncio.Event().wait()
                else:
                    await discovery_gate.wait()
            return await probes.collect(source, now, limit)

        pipeline = CollectionPipeline(
            config,
            store,
            github,  # type: ignore[arg-type]
            httpx.AsyncClient(),
            trending_collector=lambda web, now: collector("trending", now, candidate_count),
            github_search_collector=lambda github, now: collector("github_search", now, candidate_count),
            hacker_news_collector=lambda web, now: collector("hacker_news", now, candidate_count),
        )
        return pipeline, probes, github

    return make


@pytest.mark.asyncio
async def test_pipeline_runs_discovery_collectors_concurrently(pipeline_factory):
    pipeline, probes, _ = pipeline_factory()
    envelope = await pipeline.collect()
    assert envelope.status == "success"
    assert probes.max_simultaneous == 3


@pytest.mark.asyncio
async def test_one_failed_source_produces_partial_envelope(pipeline_factory):
    pipeline, _, _ = pipeline_factory(failing_sources={"trending"})
    envelope = await pipeline.collect()
    assert envelope.status == "partial"
    assert next(h for h in envelope.source_health if h.source == "trending").status == "failed"
    assert envelope.candidates


@pytest.mark.asyncio
async def test_all_discovery_sources_failed_produces_fatal_payload(pipeline_factory):
    pipeline, _, _ = pipeline_factory(failing_sources={"trending", "github_search", "hacker_news"})
    envelope = await pipeline.collect()
    assert envelope.status == "failed"
    assert envelope.fatal_error == "all discovery sources failed"


@pytest.mark.asyncio
async def test_lock_contention_does_not_block_holder_and_returns_sanitized_failure(pipeline_factory):
    gate = asyncio.Event()
    holder, holder_probes, _ = pipeline_factory(discovery_gate=gate, delayed_sources={"trending"})
    contender, _, _ = pipeline_factory()
    holding = asyncio.create_task(holder.collect())
    await holder_probes.started.wait()

    outcome = await asyncio.wait_for(contender.collect(), timeout=2)
    assert outcome.status == "failed"
    assert outcome.fatal_error == "collection already in progress"
    assert not holding.done()

    gate.set()
    assert (await holding).status == "success"


@pytest.mark.asyncio
async def test_timeout_keeps_completed_discovery_results_and_marks_only_pending_source_failed(pipeline_factory):
    pipeline, _, _ = pipeline_factory(
        delayed_sources={"trending"},
        collection_timeout_seconds=0.01,
    )
    envelope = await pipeline.collect()
    assert envelope.status == "partial"
    assert next(health for health in envelope.source_health if health.source == "trending").status == "failed"
    assert all(
        health.status == "success"
        for health in envelope.source_health
        if health.source != "trending"
    )
    assert envelope.candidates


@pytest.mark.asyncio
async def test_output_is_capped_to_llm_candidate_limit(pipeline_factory):
    pipeline, _, _ = pipeline_factory(candidate_count=80, max_llm_candidates=40)
    envelope = await pipeline.collect()
    assert len(envelope.candidates) == 40
    assert len(json.dumps(envelope.model_dump(mode="json"))) < 200_000


@pytest.mark.asyncio
async def test_worst_case_candidate_text_keeps_returned_and_persisted_payloads_below_budget(pipeline_factory):
    pipeline, _, _ = pipeline_factory(
        candidate_count=80,
        max_llm_candidates=40,
        description="\x00" * 250_000,
        readme="\x00" * 2_000,
    )
    envelope = await pipeline.collect()
    returned = json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")
    persisted = (pipeline._run_dir(envelope.run_id) / "candidates.json").read_bytes()
    assert len(returned) < 200_000
    assert len(persisted) < 200_000


@pytest.mark.asyncio
async def test_non_ascii_payload_uses_the_same_compact_utf8_encoding_within_budget(pipeline_factory):
    pipeline, _, _ = pipeline_factory(
        candidate_count=80,
        max_llm_candidates=40,
        description="汉" * 250_000,
        readme="汉" * 2_000,
    )
    envelope = await pipeline.collect()
    returned = serialize_collection_envelope(envelope).encode("utf-8")
    persisted = (pipeline._run_dir(envelope.run_id) / "candidates.json").read_bytes()
    assert len(returned) < 200_000
    assert len(persisted) < 200_000


@pytest.mark.asyncio
async def test_quality_review_path_is_relative_while_candidates_use_configured_data_dir(pipeline_factory):
    pipeline, _, _ = pipeline_factory()
    envelope = await pipeline.collect()
    assert envelope.quality_review_path == f"data/runs/{envelope.run_id}/quality-review.json"
    assert (pipeline.config.state_db.parent / "runs" / envelope.run_id / "candidates.json").is_file()


@pytest.mark.asyncio
async def test_candidates_use_project_data_directory_when_state_db_is_elsewhere(pipeline_factory, tmp_path):
    pipeline, _, _ = pipeline_factory(
        state_db=tmp_path / "external-state" / "reporter.sqlite3",
        project_root=tmp_path,
    )
    envelope = await pipeline.collect()
    assert (tmp_path / "data" / "runs" / envelope.run_id / "candidates.json").is_file()
    assert not (tmp_path / "external-state" / "runs" / envelope.run_id / "candidates.json").exists()


@pytest.mark.asyncio
async def test_tracked_velocity_hit_has_bounded_readme_evidence(pipeline_factory):
    pipeline, _, github = pipeline_factory(tracked_name="owner/tracked")
    envelope = await pipeline.collect(now=datetime(2026, 7, 24, tzinfo=UTC))
    tracked = next(candidate for candidate in envelope.candidates if candidate.canonical_name == "owner/tracked")
    assert tracked.quality_evidence
    assert "owner/tracked" in github.readme_requests


@pytest.mark.asyncio
async def test_archived_tracked_velocity_hit_is_not_admitted(pipeline_factory):
    pipeline, _, _ = pipeline_factory(
        tracked_name="owner/archived",
        archived_names={"owner/archived"},
    )
    envelope = await pipeline.collect(now=datetime(2026, 7, 24, tzinfo=UTC))
    assert "owner/archived" not in {candidate.canonical_name for candidate in envelope.candidates}
