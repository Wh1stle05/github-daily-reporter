"""Concurrent collection orchestration for the daily repository reporter."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from filelock import FileLock, Timeout as FileLockTimeout

from github_daily_reporter.collectors.github_search import collect_github_search
from github_daily_reporter.collectors.hacker_news import collect_hacker_news
from github_daily_reporter.collectors.star_velocity import enrich_velocity
from github_daily_reporter.collectors.trending import collect_trending
from github_daily_reporter.config import ReporterConfig
from github_daily_reporter.editorial import build_editorial_input, write_editorial_artifacts
from github_daily_reporter.github_client import GitHubClient
from github_daily_reporter.models import (
    CollectionEnvelope,
    CollectorResult,
    RepositoryCandidate,
    SourceHealth,
    SourceObservation,
)
from github_daily_reporter.normalize import merge_observations
from github_daily_reporter.quality import deterministic_exclusion
from github_daily_reporter.state import StateStore


LOGGER = logging.getLogger(__name__)
README_ERROR = "GitHub README unavailable"
MAX_OUTPUT_CANDIDATE_BYTES = 160_000
MAX_EVIDENCE_BYTES = 3_500
COMPACT_JSON_KWARGS = {"ensure_ascii": False, "separators": (",", ":")}

TrendingCollector = Callable[[httpx.AsyncClient, datetime], Awaitable[CollectorResult]]
SearchCollector = Callable[[GitHubClient, datetime], Awaitable[CollectorResult]]
HackerNewsCollector = Callable[[httpx.AsyncClient, datetime], Awaitable[CollectorResult]]


class CollectionPipeline:
    """Collect discovery signals, enrich repositories, and persist a bounded handoff."""

    def __init__(
        self,
        config: ReporterConfig,
        store: StateStore,
        github: GitHubClient,
        web: httpx.AsyncClient,
        *,
        trending_collector: TrendingCollector | None = None,
        github_search_collector: SearchCollector | None = None,
        hacker_news_collector: HackerNewsCollector | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.github = github
        self.web = web
        self.trending_collector = trending_collector or self._collect_trending
        self.github_search_collector = github_search_collector or self._collect_github_search
        self.hacker_news_collector = hacker_news_collector or self._collect_hacker_news

    async def collect(self, now: datetime | None = None) -> CollectionEnvelope:
        """Run a bounded collection transaction and return its model-facing payload."""
        observed_at = _as_utc(now or datetime.now(UTC))
        lock = FileLock(f"{self.config.state_db}.lock", timeout=1)
        try:
            await asyncio.to_thread(lock.acquire)
        except FileLockTimeout:
            LOGGER.warning("collection lock is already held")
            run_id = str(uuid4())
            return CollectionEnvelope(
                run_id=run_id,
                status="failed",
                generated_at=observed_at,
                source_health=[],
                candidates=[],
                fatal_error="collection already in progress",
            )
        try:
            return await self._collect_locked(observed_at)
        finally:
            await asyncio.shield(asyncio.to_thread(lock.release))

    async def _collect_locked(self, now: datetime) -> CollectionEnvelope:
        run_id = self.store.start_run(now)
        source_health: list[SourceHealth] = []
        observations: list[SourceObservation] = []
        try:
            results = await self._discover(now)
            for source, result in zip(("trending", "github_search", "hacker_news"), results, strict=True):
                if isinstance(result, BaseException):
                    source_health.append(_failed_health(source, result))
                    continue
                source_health.append(result.health)
                observations.extend(result.observations)

            if all(health.status == "failed" for health in source_health):
                return self._failed_envelope(run_id, now, source_health, "all discovery sources failed")

            candidates = await self._discovery_candidates(observations)
            eligible = [candidate for candidate in candidates if deterministic_exclusion(candidate) is None]
            eligible = await self._add_velocity_candidates(eligible, now)

            self.store.save_collection_transaction(run_id, eligible, observations, now)

            bounded = self._bound_output_candidates(eligible[: self.config.max_llm_candidates])

            date_run_dir = self._date_run_dir(now)
            editorial = build_editorial_input(eligible, source_health, date_run_dir, now=now)
            write_editorial_artifacts(editorial, date_run_dir)

            status = "partial" if any(item.status != "success" for item in source_health) else "success"
            self.store.finish_run(run_id, status, source_health)
            return CollectionEnvelope(
                run_id=run_id,
                status=status,
                generated_at=now,
                source_health=source_health,
                candidates=bounded,
            )
        except Exception:
            LOGGER.exception("collection pipeline failed")
            self.store.finish_run(run_id, "failed", source_health, "collection pipeline failed")
            return CollectionEnvelope(
                run_id=run_id,
                status="failed",
                generated_at=now,
                source_health=source_health,
                candidates=[],
                fatal_error="collection pipeline failed",
            )

    async def _discover(self, now: datetime) -> list[CollectorResult | BaseException]:
        tasks = [
            asyncio.create_task(self.trending_collector(self.web, now)),
            asyncio.create_task(self.github_search_collector(self.github, now)),
            asyncio.create_task(self.hacker_news_collector(self.web, now)),
        ]
        try:
            async with asyncio.timeout(self.config.collection_timeout_seconds):
                done, pending = await asyncio.wait(tasks, return_when=asyncio.ALL_COMPLETED)
        except TimeoutError:
            done = {task for task in tasks if task.done()}
            pending = set(tasks) - done

        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        results: list[CollectorResult | BaseException] = []
        for task in tasks:
            if task in pending:
                results.append(TimeoutError())
                continue
            try:
                results.append(task.result())
            except BaseException as error:
                results.append(error)
        return results

    async def _discovery_candidates(
        self, observations: list[SourceObservation]
    ) -> list[RepositoryCandidate]:
        merged = merge_observations(observations)
        semaphore = asyncio.Semaphore(10)

        async def enrich(name: str, signals: dict[str, Any]) -> RepositoryCandidate | None:
            async with semaphore:
                try:
                    candidate = await self.github.get_repository(name, signals)
                except Exception:
                    LOGGER.warning("repository metadata unavailable for %s", name)
                    return None
                await self._add_readme_evidence(candidate)
                return candidate

        resolved = await asyncio.gather(
            *(enrich(name, signals) for name, signals in sorted(merged.items()))
        )
        return [candidate for candidate in resolved if candidate is not None]

    async def _add_velocity_candidates(
        self, today: list[RepositoryCandidate], now: datetime
    ) -> list[RepositoryCandidate]:
        today_by_name = {candidate.canonical_name: candidate for candidate in today}
        cutoff = now - timedelta(days=self.config.tracked_repo_days)
        names: list[str] = list(today_by_name)
        names.extend(
            name for name in self.store.recent_repository_names(cutoff) if name not in today_by_name
        )
        names = names[: self.config.max_velocity_candidates]

        semaphore = asyncio.Semaphore(5)

        async def velocity(name: str) -> tuple[str, RepositoryCandidate | None]:
            async with semaphore:
                candidate = today_by_name.get(name)
                if candidate is None:
                    try:
                        candidate = await self.github.get_repository(name, {})
                    except Exception:
                        LOGGER.warning("tracked repository metadata unavailable for %s", name)
                        return name, None
                    await self._add_readme_evidence(candidate)
                    if deterministic_exclusion(candidate) is not None:
                        return name, None
                await enrich_velocity(
                    candidate,
                    self.github,
                    now,
                    self.config.velocity_window_hours,
                    self.config.velocity_threshold,
                    self.store.estimate_stars_24h,
                )
                return name, candidate

        enriched = await asyncio.gather(*(velocity(name) for name in names))
        result = list(today)
        for name, candidate in enriched:
            if name not in today_by_name and candidate is not None and candidate.velocity_hit:
                result.append(candidate)
        return result

    async def _add_readme_evidence(self, candidate: RepositoryCandidate) -> None:
        try:
            readme = await self.github.get_readme_excerpt(candidate.canonical_name, max_chars=2000)
        except Exception:
            candidate.source_errors.append(README_ERROR)
            readme = ""
        candidate.quality_evidence = json.dumps(
            {
                "readme_excerpt": _bounded_text(readme, 1_800),
                "description": _bounded_text(candidate.description or "", 500),
                "language": _bounded_text(candidate.primary_language or "", 120),
                "license": _bounded_text(candidate.license_spdx or "", 120),
                "created_at": candidate.created_at.isoformat(),
                "pushed_at": candidate.pushed_at.isoformat() if candidate.pushed_at else None,
                "stars_total": candidate.stars_total,
                "forks_total": candidate.forks_total,
                "open_issues_count": candidate.open_issues_count,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _bound_output_candidates(
        candidates: list[RepositoryCandidate],
    ) -> list[RepositoryCandidate]:
        """Return a deterministic, transport-safe subset below the handoff budget."""
        bounded = [_bound_candidate(candidate) for candidate in candidates]
        while _candidate_payload_size(bounded) > MAX_OUTPUT_CANDIDATE_BYTES:
            evidence_candidate = next(
                (candidate for candidate in reversed(bounded) if candidate.quality_evidence != "{}"),
                None,
            )
            if evidence_candidate is not None:
                evidence_candidate.quality_evidence = "{}"
                continue
            description_candidate = next(
                (candidate for candidate in reversed(bounded) if candidate.description), None
            )
            if description_candidate is not None:
                description_candidate.description = None
                continue
            error_candidate = next(
                (candidate for candidate in reversed(bounded) if candidate.source_errors), None
            )
            if error_candidate is not None:
                error_candidate.source_errors = []
                continue
            bounded.pop()
        return bounded

    async def _collect_trending(self, web: httpx.AsyncClient, now: datetime) -> CollectorResult:
        return await collect_trending(web, now)

    async def _collect_github_search(self, github: GitHubClient, now: datetime) -> CollectorResult:
        return await collect_github_search(
            github, now, self.config.new_repo_lookback_days, self.config.max_candidates_per_source
        )

    async def _collect_hacker_news(self, web: httpx.AsyncClient, now: datetime) -> CollectorResult:
        return await collect_hacker_news(
            web, now, self.config.hn_lookback_hours, self.config.max_candidates_per_source
        )

    def _date_run_dir(self, now: datetime) -> Path:
        project_root = self.config.project_root or self.config.state_db.parent.parent
        return project_root / "data" / "runs" / f"github-daily-report-{now.date().isoformat()}"

    def _failed_envelope(
        self, run_id: str, now: datetime, source_health: list[SourceHealth], error: str
    ) -> CollectionEnvelope:
        self.store.finish_run(run_id, "failed", source_health, error)
        return CollectionEnvelope(
            run_id=run_id,
            status="failed",
            generated_at=now,
            source_health=source_health,
            candidates=[],
            fatal_error=error,
        )


def _failed_health(source: str, error: BaseException) -> SourceHealth:
    LOGGER.warning("discovery source %s failed: %s", source, type(error).__name__)
    return SourceHealth(
        source=source,
        status="failed",
        error=f"collection failed ({type(error).__name__})",
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


def _bound_candidate(candidate: RepositoryCandidate) -> RepositoryCandidate:
    """Copy and bound all untrusted text that can reach the public handoff."""
    result = candidate.model_copy(deep=True)
    result.canonical_name = _bounded_text(result.canonical_name, 256)
    result.full_name = _bounded_text(result.full_name, 256)
    result.html_url = _bounded_text(result.html_url, 1_024)
    result.description = (
        _bounded_text(result.description, 512) if result.description is not None else None
    )
    result.license_spdx = (
        _bounded_text(result.license_spdx, 128) if result.license_spdx is not None else None
    )
    result.primary_language = (
        _bounded_text(result.primary_language, 128)
        if result.primary_language is not None
        else None
    )
    result.quality_evidence = _bounded_text(result.quality_evidence, MAX_EVIDENCE_BYTES)
    result.source_errors = [_bounded_text(error, 160) for error in result.source_errors[:10]]
    result.hn_item_ids = result.hn_item_ids[:100]
    return result


def _bounded_text(value: str, max_bytes: int) -> str:
    """Remove JSON-expensive controls and truncate on UTF-8 byte boundaries."""
    safe = "".join(character if character.isprintable() else " " for character in value)
    encoded = safe.encode("utf-8")[:max_bytes]
    return encoded.decode("utf-8", errors="ignore")


def _candidate_payload_size(candidates: list[RepositoryCandidate]) -> int:
    return len(serialize_candidates(candidates).encode("utf-8"))


def serialize_candidates(candidates: list[RepositoryCandidate]) -> str:
    """Serialize the candidate handoff with the project's single JSON encoding."""
    return json.dumps(
        [candidate.model_dump(mode="json") for candidate in candidates], **COMPACT_JSON_KWARGS
    )


def serialize_collection_envelope(envelope: CollectionEnvelope) -> str:
    """Serialize the CLI envelope using the same UTF-8-safe compact encoding."""
    return json.dumps(envelope.model_dump(mode="json"), **COMPACT_JSON_KWARGS)
