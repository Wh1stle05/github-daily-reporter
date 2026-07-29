"""Command-line entry points for collection and deterministic ranking."""

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import traceback
from typing import Any

import httpx
import yaml
from pydantic import ValidationError

from github_daily_reporter.config import ReporterConfig, load_config
from github_daily_reporter.daily import DailyReporter
from github_daily_reporter.github_client import GitHubClient
from github_daily_reporter.hybrid import run_hybrid as run_hybrid_pipeline
from github_daily_reporter.llm import LlmReviewClient
from github_daily_reporter.models import (
    CollectionEnvelope,
    DailyRunResult,
    QualityEnvelope,
    QualityReview,
    RepositoryCandidate,
)
from github_daily_reporter.pipeline import CollectionPipeline, serialize_collection_envelope
from github_daily_reporter.quality import deterministic_exclusion
from github_daily_reporter.scoring import rank_candidates
from github_daily_reporter.state import StateStore
from github_daily_reporter.telegram import TelegramClient


class OperatorError(ValueError):
    """An input or operator error that should be reported without a traceback."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise OperatorError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="github-daily-reporter")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect")
    collect.add_argument("--config", type=Path, required=True)

    daily = subparsers.add_parser("daily")
    daily.add_argument("--config", type=Path, required=True)

    hybrid = subparsers.add_parser("hybrid")
    hybrid.add_argument("--config", type=Path, required=True)

    rank = subparsers.add_parser("rank")
    rank.add_argument("--config", type=Path, required=True)
    rank.add_argument("--run-id", required=True)
    rank.add_argument("--quality-file", type=Path, required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--config", type=Path, required=True)

    backfill = subparsers.add_parser("backfill-snapshots")
    backfill.add_argument("--config", type=Path, required=True)
    backfill.add_argument("--snapshot-file", type=Path, required=True)
    return parser


async def run_collection(config_path: Path) -> CollectionEnvelope:
    """Build the owned transports and run one collection pipeline."""
    config = load_config(config_path)
    timeout = httpx.Timeout(config.request_timeout_seconds)
    headers = {"User-Agent": "github-daily-reporter/0.1"}
    async with GitHubClient(
        config.github_token.get_secret_value(), timeout=config.request_timeout_seconds
    ) as github, httpx.AsyncClient(timeout=timeout, headers=headers) as web:
        pipeline = CollectionPipeline(config, StateStore(config.state_db), github, web)
        return await pipeline.collect()


async def run_daily(config_path: Path) -> DailyRunResult:
    """Build the owned transports and run one self-contained daily report."""
    config = load_config(config_path)
    store = StateStore(config.state_db)
    timeout = httpx.Timeout(config.request_timeout_seconds)
    headers = {"User-Agent": "github-daily-reporter/0.1"}
    async with GitHubClient(
        config.github_token.get_secret_value(), timeout=config.request_timeout_seconds
    ) as github, httpx.AsyncClient(timeout=timeout, headers=headers) as web:
        collector = CollectionPipeline(config, store, github, web)
        reporter = DailyReporter(
            config=config,
            collector=collector,
            llm=LlmReviewClient(config),
            telegram=TelegramClient(config),
            store=store,
        )
        return await reporter.run()


def main(argv: list[str] | None = None) -> int:
    """Run the requested command and return a shell-compatible status code."""
    try:
        args = _parser().parse_args(argv)
        if args.command == "collect":
            envelope = asyncio.run(run_collection(args.config))
            print(serialize_collection_envelope(envelope))
            return 0
        if args.command == "daily":
            raise OperatorError("legacy daily command retired; use hybrid")
        if args.command == "hybrid":
            result = asyncio.run(run_hybrid_pipeline(args.config))
            operational = {
                "run_id": result.run_id,
                "status": result.status,
                "growth_count": result.growth_count,
                "mature_count": result.mature_count,
            }
            if result.error_category is not None:
                operational["error_category"] = result.error_category
            print(json.dumps(operational, sort_keys=True))
            return 0 if result.status not in {"failed", "delivery_pending"} else 1
        if args.command == "rank":
            _rank(args.config, args.run_id, args.quality_file)
            return 0
        if args.command == "doctor":
            return _doctor(args.config)
        if args.command == "backfill-snapshots":
            _backfill_snapshots(args.config, args.snapshot_file)
            return 0
        raise OperatorError("unknown command")
    except (OperatorError, ValidationError, json.JSONDecodeError, OSError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except SystemExit as error:
        return int(error.code)
    except Exception:
        print(_sanitized_traceback(), file=sys.stderr, end="")
        return 1


def _doctor(config_path: Path) -> int:
    """Run non-destructive deployment checks and print one sanitized JSON result."""
    result: dict[str, Any] = {"checks": {}}
    checks: dict[str, bool] = result["checks"]
    try:
        config = load_config(config_path)
    except (OSError, ValidationError, ValueError, yaml.YAMLError) as error:
        checks["config_valid"] = False
        result["config_error"] = _safe_error(error)
        print(json.dumps(result, sort_keys=True))
        return 2

    checks["config_valid"] = True
    checks["github_token_present"] = bool(config.github_token.get_secret_value().strip())
    checks["llm_api_key_present"] = bool(config.llm_api_key.get_secret_value().strip())
    checks["telegram_bot_token_present"] = bool(
        config.telegram_bot_token.get_secret_value().strip()
    )
    checks["telegram_chat_id_present"] = bool(config.telegram_chat_id.strip())
    checks["telegram_thread_id_valid"] = (
        config.telegram_message_thread_id is None
        or config.telegram_message_thread_id > 0
    )
    database_access = _probe_database(config.state_db)
    checks["database_writable"] = database_access
    checks["delivery_database_access"] = database_access

    github = probe_github(config)
    result["github"] = github
    checks["github_authenticated"] = bool(github.get("ok"))

    hermes = probe_hermes()
    result["hermes"] = hermes
    checks["hermes_available"] = bool(hermes.get("executable", hermes.get("ok")))
    checks["hermes_cron_status"] = bool(hermes.get("cron_status", hermes.get("ok")))
    checks["hermes_timezone_configured"] = bool(hermes.get("timezone"))
    checks["timezone_match"] = hermes.get("timezone") == config.timezone

    assets = _probe_assets()
    result["assets"] = assets
    checks["wrapper_source"] = assets["wrapper_source"]
    checks["wrapper_executable"] = assets["wrapper_executable"]
    checks["legacy_wrapper_absent"] = assets["legacy_wrapper_absent"]
    checks["skill_source"] = assets["skill_source"]

    result["ok"] = all(checks.values())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 2


def _probe_database(path: Path) -> bool:
    """Prove the state database can be initialized and accept a rolled-back write."""
    try:
        StateStore(path)
        with sqlite3.connect(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("SELECT 1 FROM report_artifacts LIMIT 1")
            connection.execute("SELECT 1 FROM delivery_parts LIMIT 1")
            connection.execute("CREATE TABLE __doctor_write_probe (id INTEGER)")
            connection.rollback()
        return True
    except (OSError, sqlite3.Error):
        return False


def probe_github(config: ReporterConfig) -> dict[str, Any]:
    """Check token authentication without returning headers, tokens, or response bodies."""
    try:
        response = httpx.get(
            "https://api.github.com/rate_limit",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {config.github_token.get_secret_value()}",
            },
            timeout=config.request_timeout_seconds,
        )
        response.raise_for_status()
        remaining = response.json().get("resources", {}).get("core", {}).get("remaining")
        return {"ok": isinstance(remaining, int) and remaining > 0, "remaining": remaining}
    except (httpx.HTTPError, ValueError, TypeError):
        return {"ok": False, "error": "request failed"}


def probe_hermes() -> dict[str, Any]:
    """Check the active Hermes profile without exposing its configuration values."""
    executable = shutil.which("hermes")
    result: dict[str, Any] = {"ok": False, "executable": bool(executable), "cron_status": False}
    if not executable:
        return result
    try:
        status = subprocess.run(
            [executable, "cron", "status"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        result["cron_status"] = status.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pass

    config_path = Path(
        os.environ.get(
            "HERMES_CONFIG_PATH",
            Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "config.yaml",
        )
    ).expanduser()
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        timezone = payload.get("timezone") if isinstance(payload, dict) else None
        if isinstance(timezone, str) and timezone.strip():
            result["timezone"] = timezone
    except (OSError, yaml.YAMLError):
        pass
    result["ok"] = bool(result["executable"] and result["cron_status"] and result.get("timezone"))
    return result


def _probe_assets() -> dict[str, bool]:
    project_root = Path(__file__).resolve().parents[2]
    wrapper = project_root / "deploy" / "hermes" / "github-daily-run.sh"
    legacy_wrapper = project_root / "deploy" / "hermes" / "github-daily-collect.sh"
    skill = project_root / "skills" / "github-daily-reporter" / "SKILL.md"
    return {
        "wrapper_source": wrapper.is_file(),
        "wrapper_executable": wrapper.is_file() and os.access(wrapper, os.X_OK),
        "legacy_wrapper_absent": not legacy_wrapper.exists(),
        "skill_source": skill.is_file(),
        "skill_source_absent": not skill.exists(),
    }


def _safe_error(error: Exception) -> str:
    return re.sub(r"(?i)(token|authorization)(=|:)[^\s,]+", r"\1\2[REDACTED]", str(error))


def _rank(config_path: Path, run_id: str, supplied_quality_path: Path) -> None:
    config = load_config(config_path)
    store = StateStore(config.state_db)
    try:
        ranked_at = store.get_run_started_at(run_id)
    except KeyError as error:
        raise OperatorError(f"unknown run_id: {run_id}") from error
    run_dir = _run_directory(config, run_id)
    quality_path = supplied_quality_path.expanduser().resolve()
    if not quality_path.is_relative_to(run_dir):
        raise OperatorError(f"quality file must be inside {run_dir}")

    try:
        payload = json.loads(quality_path.read_text(encoding="utf-8"))
        envelope = QualityEnvelope.model_validate(payload)
    except FileNotFoundError as error:
        raise OperatorError(f"quality file does not exist: {quality_path}") from error
    except (json.JSONDecodeError, ValidationError) as error:
        raise OperatorError(f"invalid quality review: {error}") from error
    if envelope.run_id != run_id:
        raise OperatorError("quality review run_id does not match --run-id")

    candidates = store.get_run_candidates(run_id)
    candidates_by_name = {candidate.canonical_name: candidate for candidate in candidates}
    reviews_by_name = _validate_reviews(envelope.reviews, candidates_by_name)

    deterministic_exclusions = {
        name: reason
        for name, candidate in candidates_by_name.items()
        if (reason := deterministic_exclusion(candidate)) is not None
    }
    excluded_reasons = dict(deterministic_exclusions)
    excluded_reasons.update(
        {
            name: review.exclude_reason or "llm_exclusion"
            for name, review in reviews_by_name.items()
            if review.exclude
        }
    )
    duplicate_names = _duplicate_names(reviews_by_name)
    excluded_reasons.update(
        {
            name: f"duplicate_of:{reviews_by_name[name].duplicate_of}"
            for name in duplicate_names
        }
    )
    excluded = set(excluded_reasons)

    ranking_items = []
    for canonical_name, candidate in candidates_by_name.items():
        if canonical_name in excluded:
            continue
        review = reviews_by_name.get(canonical_name)
        if review is None:
            ranking_items.append((candidate, 50.0, True))
        else:
            ranking_items.append((candidate, _quality_score(review), False))

    ranked = rank_candidates(ranking_items, ranked_at)
    store.save_ranking(
        run_id,
        ranked,
        envelope.reviews,
        excluded_reasons,
        deterministic_exclusions,
    )
    print(json.dumps({"run_id": run_id, "ranked": [item.model_dump(mode="json") for item in ranked]}))


def _run_directory(config: ReporterConfig, run_id: str) -> Path:
    project_root = config.project_root
    if project_root is None:
        raise OperatorError("configured project root is unavailable")
    runs_root = (project_root / "data" / "runs").resolve()
    run_dir = (runs_root / run_id).resolve()
    if not run_dir.is_relative_to(runs_root):
        raise OperatorError("run_id must not escape data/runs")
    return run_dir


def _validate_reviews(
    reviews: list[QualityReview], candidates: dict[str, RepositoryCandidate]
) -> dict[str, QualityReview]:
    result: dict[str, QualityReview] = {}
    for review in reviews:
        if review.canonical_name not in candidates:
            raise OperatorError("review must reference a candidate in this run")
        if review.canonical_name in result:
            raise OperatorError("quality review contains duplicate canonical_name")
        if review.exclude and not (review.exclude_reason or "").strip():
            raise OperatorError("exclude_reason is required when exclude=True")
        if review.duplicate_of is not None:
            if review.duplicate_of == review.canonical_name:
                raise OperatorError("duplicate_of cannot reference itself")
            if review.duplicate_of not in candidates:
                raise OperatorError("duplicate_of must reference a candidate in this run")
        result[review.canonical_name] = review
    return result


def _duplicate_names(reviews_by_name: dict[str, QualityReview]) -> set[str]:
    """Return every duplicate and reject chains that never reach a target."""
    duplicates: set[str] = set()
    for name, review in reviews_by_name.items():
        if review.duplicate_of is None:
            continue
        duplicates.add(name)
        seen = {name}
        target = review.duplicate_of
        while target in reviews_by_name and reviews_by_name[target].duplicate_of is not None:
            if target in seen:
                raise OperatorError("duplicate_of references form a cycle")
            seen.add(target)
            duplicates.add(target)
            target = reviews_by_name[target].duplicate_of
    return duplicates


def _quality_score(review: QualityReview) -> float:
    return 100 * (
        review.usefulness + review.completeness + review.novelty + review.maintenance
    ) / 20


def _backfill_snapshots(config_path: Path, snapshot_path: Path) -> None:
    config = load_config(config_path)
    try:
        raw: Any = json.loads(snapshot_path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise OperatorError(f"snapshot file does not exist: {snapshot_path}") from error
    except json.JSONDecodeError as error:
        raise OperatorError(f"invalid snapshot file: {error}") from error
    records = raw.get("snapshots") if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        raise OperatorError("snapshot file must contain a list or a snapshots list")

    store = StateStore(config.state_db)
    for record in records:
        if not isinstance(record, dict):
            raise OperatorError("each snapshot record must be an object")
        try:
            candidate = RepositoryCandidate.model_validate(record.get("candidate", record))
            observed_at = datetime.fromisoformat(record["observed_at"])
            if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                raise ValueError("observed_at must include a timezone")
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise OperatorError(f"invalid snapshot record: {error}") from error
        store.record_snapshot(candidate, observed_at)
    print(json.dumps({"snapshots_imported": len(records)}))


def _sanitized_traceback() -> str:
    trace = traceback.format_exc()
    secrets = [value for value in (os.environ.get("GITHUB_TOKEN"),) if value]
    for secret in secrets:
        trace = trace.replace(secret, "[REDACTED]")
    return re.sub(r"(?i)(token|authorization)(=|:)[^\s,]+", r"\1\2[REDACTED]", trace)


if __name__ == "__main__":
    raise SystemExit(main())
