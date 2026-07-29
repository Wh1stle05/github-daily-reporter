"""Production orchestration for deterministic collection plus one Hermes pass."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from uuid import uuid4

import httpx
from filelock import FileLock, Timeout as FileLockTimeout

from github_daily_reporter.agent_runner import run_hermes_editorial
from github_daily_reporter.config import ReporterConfig, load_config
from github_daily_reporter.editorial import (
    build_editorial_input,
    promote_reports,
    write_editorial_artifacts,
)
from github_daily_reporter.github_client import GitHubClient
from github_daily_reporter.models import CollectionEnvelope, EditorialInput
from github_daily_reporter.pipeline import CollectionPipeline
from github_daily_reporter.state import StateStore
from github_daily_reporter.telegram import DeliveryResult, TelegramClient, deliver_report_parts


class HybridRunError(RuntimeError):
    """A bounded stage failed and no Agent repair loop is attempted."""

    def __init__(self, stage: str, reason: str):
        super().__init__(f"{stage}: {reason}")
        self.stage = stage
        self.reason = reason


@dataclass(frozen=True)
class HybridOutcome:
    run_id: str
    status: str
    growth_count: int = 0
    mature_count: int = 0
    error_category: str | None = None


# Kept as a module-level seam for tests and for deployments that wrap delivery.
deliver_reports = deliver_report_parts


async def collect_for_hybrid(
    config_path: Path, now: datetime | None = None
) -> CollectionEnvelope:
    config = load_config(config_path)
    observed_at = _as_utc(now or datetime.now(UTC))
    timeout = httpx.Timeout(config.request_timeout_seconds)
    headers = {"User-Agent": "github-daily-reporter/0.1"}
    async with GitHubClient(
        config.github_token.get_secret_value(), timeout=config.request_timeout_seconds
    ) as github, httpx.AsyncClient(timeout=timeout, headers=headers) as web:
        pipeline = CollectionPipeline(config, StateStore(config.state_db), github, web)
        return await pipeline.collect(observed_at)


async def run_hybrid(
    config_path: Path,
    *,
    now: datetime | None = None,
) -> HybridOutcome:
    """Run collection, one bounded editorial attempt, validation, and delivery."""
    config = load_config(config_path)
    observed_at = _as_utc(now or datetime.now(UTC))
    run_dir = _run_dir(config, observed_at)
    status_path = run_dir / "run-status.json"
    previous = _read_json(status_path)
    if previous.get("status") == "delivered":
        return HybridOutcome(
            run_id=run_dir.name,
            status="delivered",
            growth_count=int(previous.get("growth_count", 10)),
            mature_count=int(previous.get("mature_count", 10)),
        )

    lock = FileLock(str(config.state_db) + ".hybrid.lock", timeout=0.1)
    try:
        await asyncio.to_thread(lock.acquire)
    except FileLockTimeout:
        return HybridOutcome(run_id=run_dir.name, status="skipped")

    try:
        try:
            envelope = await collect_for_hybrid(config_path, observed_at)
        except Exception as error:
            _record_failure(run_dir, run_dir.name, "collection", _safe_reason(error))
            raise HybridRunError("collection", _safe_reason(error)) from error
        if envelope.status == "failed":
            reason = envelope.fatal_error or "collection failed"
            _record_failure(run_dir, run_dir.name, "collection", reason)
            raise HybridRunError("collection", reason)

        handoff = _load_or_build_handoff(envelope, run_dir, observed_at)
        attempt_id = uuid4().hex
        try:
            await run_hermes_editorial(
                run_dir,
                attempt_id,
                timeout_seconds=config.hermes_timeout_seconds,
            )
        except TimeoutError as error:
            _record_failure(run_dir, handoff.run_id, "hermes", "timeout")
            raise HybridRunError("hermes", "timeout") from error
        except Exception as error:
            _record_failure(run_dir, handoff.run_id, "hermes", _safe_reason(error))
            raise HybridRunError("hermes", _safe_reason(error)) from error

        try:
            promote_reports(run_dir, handoff, attempt_id)
        except Exception as error:
            _record_failure(run_dir, handoff.run_id, "validation", _safe_reason(error))
            raise HybridRunError("validation", _safe_reason(error)) from error

        growth = (run_dir / "growth-report.md").read_text(encoding="utf-8")
        mature = (run_dir / "mature-report.md").read_text(encoding="utf-8")
        store = StateStore(config.state_db)
        telegram = TelegramClient(config)
        delivery: DeliveryResult = await deliver_reports(
            store,
            telegram,
            run_dir.name,
            [(0, growth), (1, mature)],
        )
        if delivery.status != "delivered":
            _record_failure(
                run_dir,
                handoff.run_id,
                "delivery",
                delivery.error_category or "delivery_pending",
            )
            return HybridOutcome(
                run_id=handoff.run_id,
                status="delivery_pending",
                growth_count=_report_count(growth),
                mature_count=_report_count(mature),
                error_category=delivery.error_category,
            )

        _write_status(
            run_dir,
            {
                "run_id": handoff.run_id,
                "status": "delivered",
                "growth_count": _report_count(growth),
                "mature_count": _report_count(mature),
                "delivery_order": ["growth", "mature"],
            },
        )
        return HybridOutcome(
            run_id=handoff.run_id,
            status="delivered",
            growth_count=_report_count(growth),
            mature_count=_report_count(mature),
        )
    finally:
        await asyncio.shield(asyncio.to_thread(lock.release))


def _load_or_build_handoff(
    envelope: CollectionEnvelope, run_dir: Path, now: datetime
) -> EditorialInput:
    path = run_dir / "editorial-input.json"
    if path.exists():
        return EditorialInput.model_validate_json(path.read_text(encoding="utf-8"))
    handoff = build_editorial_input(envelope.candidates, envelope.source_health, run_dir, now=now)
    write_editorial_artifacts(handoff, run_dir)
    return handoff


def _run_dir(config: ReporterConfig, now: datetime) -> Path:
    root = config.project_root or config.state_db.parent.parent
    return root / "data" / "runs" / f"github-daily-report-{now.date().isoformat()}"


def _record_failure(run_dir: Path, run_id: str, stage: str, reason: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_status(
        run_dir,
        {"run_id": run_id, "status": "failed", "failed_stage": stage, "reason": reason},
    )


def _write_status(run_dir: Path, payload: dict[str, object]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    temporary = run_dir / "run-status.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(run_dir / "run-status.json")


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _report_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.startswith("### "))


def _safe_reason(error: BaseException) -> str:
    return type(error).__name__.lower().replace("error", "") or "failed"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)
