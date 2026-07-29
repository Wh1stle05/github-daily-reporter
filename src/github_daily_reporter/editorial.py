"""Bounded handoff artifacts and non-repairing Agent report validation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Any

from github_daily_reporter.models import (
    Cohort,
    EditorialCandidate,
    EditorialCohort,
    EditorialInput,
    RankedCandidate,
    RepositoryCandidate,
    SourceHealth,
)
from github_daily_reporter.scoring import cohort_ranking_key
from github_daily_reporter.selection import assign_cohort


MAX_README_EXCERPT_BYTES = 1_800
MAX_EVIDENCE_BYTES = 12_000
REPORT_SCORE_RE = re.compile(r"(?m)^\s*-\s*综合评分：([0-9]+(?:\.[0-9])?)/100\s*$")
REPO_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")
ENTRY_RE = re.compile(r"(?m)^###\s+(\d+)\.\s+([^\n]+)\s*$")


def build_editorial_input(
    candidates: Iterable[RepositoryCandidate],
    source_health: list[SourceHealth],
    run_dir: Path,
    *,
    now: datetime | None = None,
) -> EditorialInput:
    """Score, cohort, cap and serialize a compact Agent-facing candidate index."""
    from github_daily_reporter.scoring import score_growth_candidate, score_mature_candidate

    now_utc = _as_utc(now or datetime.now(UTC))
    unique: dict[str, RepositoryCandidate] = {}
    for candidate in candidates:
        unique.setdefault(candidate.canonical_name.lower(), candidate)

    grouped: dict[Cohort, list[RankedCandidate]] = {"growth": [], "mature": []}
    for candidate in unique.values():
        cohort = assign_cohort(candidate)
        if cohort is None:
            continue
        score = (
            score_growth_candidate(candidate, now_utc)
            if cohort == "growth"
            else score_mature_candidate(candidate, now_utc)
        )
        grouped[cohort].append(RankedCandidate(candidate=candidate, score=score))

    pools: dict[Cohort, EditorialCohort] = {}
    available_counts: dict[Cohort, int] = {}
    for cohort in ("growth", "mature"):
        ordered = sorted(grouped[cohort], key=cohort_ranking_key)
        available_counts[cohort] = len(ordered)
        pools[cohort] = EditorialCohort(
            primary=[_editorial_candidate(item, cohort, index + 1) for index, item in enumerate(ordered[:20])],
            reserve=[
                _editorial_candidate(item, cohort, index + 21)
                for index, item in enumerate(ordered[20:25])
            ],
        )

    status = "success" if all(count >= 10 for count in available_counts.values()) else "partial"
    envelope = EditorialInput(
        run_id=run_dir.name,
        generated_at=now_utc,
        status=status,
        source_health=source_health,
        available_counts=available_counts,
        cohorts=pools,
    )
    return envelope


def write_editorial_artifacts(
    envelope: EditorialInput,
    run_dir: Path,
    *,
    attempt_id: str | None = None,
) -> Path:
    """Write collection/index/evidence/status files with atomic JSON replacement."""
    run_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(run_dir / "editorial-input.json", envelope.model_dump(mode="json"))
    collection = {
        "schema_version": envelope.schema_version,
        "run_id": envelope.run_id,
        "generated_at": envelope.generated_at.isoformat(),
        "status": envelope.status,
        "source_health": [item.model_dump(mode="json") for item in envelope.source_health],
        "available_counts": envelope.available_counts,
    }
    _atomic_json(run_dir / "collection.json", collection)
    for cohort in ("growth", "mature"):
        pool = envelope.cohorts[cohort]
        for item in [*pool.primary, *pool.reserve]:
            filename = _safe_filename(item.canonical_name) + ".json"
            evidence = {
                "canonical_name": item.canonical_name,
                "url": item.html_url,
                "description": item.description,
                "readme_excerpt": item.readme_excerpt,
                "risk_markers": item.risk_markers,
                "source_errors": item.source_errors,
            }
            _atomic_json(evidence_dir / filename, evidence, max_bytes=MAX_EVIDENCE_BYTES)

    attempt_dir = None
    if attempt_id is not None:
        attempt_dir = run_dir / "attempts" / _safe_filename(attempt_id)
        attempt_dir.mkdir(parents=True, exist_ok=True)
    status = {
        "run_id": envelope.run_id,
        "status": "collected",
        "editorial_status": envelope.status,
        "available_counts": envelope.available_counts,
        "attempt_id": attempt_id,
        "reports": {"growth": None, "mature": None},
    }
    _atomic_json(run_dir / "run-status.json", status)
    return attempt_dir or run_dir


def validate_reports(
    run_dir: Path,
    handoff: EditorialInput | Mapping[str, Any],
    growth_report: str,
    mature_report: str,
) -> dict[str, list[str]]:
    """Validate report identity, cohort membership and immutable Python scores."""
    data = handoff.model_dump(mode="json") if isinstance(handoff, EditorialInput) else dict(handoff)
    run_id = str(data.get("run_id", ""))
    date_text = run_id.rsplit("-", 3)[-3:] if run_id.startswith("github-daily-report-") else None
    date_label = "-".join(date_text) if date_text else run_id
    pools = data.get("cohorts") or {}
    growth_pool = _pool_records(pools.get("growth", {}))
    mature_pool = _pool_records(pools.get("mature", {}))
    growth = _validate_one_report(
        growth_report,
        expected_title=f"# GitHub 成长项目榜 · {date_label}",
        expected_cohort="growth",
        pool=growth_pool,
        other_pool=mature_pool,
        complete_count=int((data.get("available_counts") or {}).get("growth", 10)),
        status=str(data.get("status", "success")),
    )
    mature = _validate_one_report(
        mature_report,
        expected_title=f"# GitHub 万星增量榜 · {date_label}",
        expected_cohort="mature",
        pool=mature_pool,
        other_pool=growth_pool,
        complete_count=int((data.get("available_counts") or {}).get("mature", 10)),
        status=str(data.get("status", "success")),
    )
    overlap = set(growth) & set(mature)
    if overlap:
        raise ValueError("duplicate repository across cohorts")
    return {"growth": growth, "mature": mature}


def _validate_one_report(
    text: str,
    *,
    expected_title: str,
    expected_cohort: Cohort,
    pool: dict[str, dict[str, Any]],
    other_pool: dict[str, dict[str, Any]],
    complete_count: int,
    status: str,
) -> list[str]:
    if not text.strip():
        raise ValueError(f"{expected_cohort} report is empty")
    first_line = text.lstrip().splitlines()[0].strip()
    if first_line != expected_title:
        raise ValueError(f"wrong {expected_cohort} title")
    entries = list(ENTRY_RE.finditer(text))
    quick_urls = [url.lower().rstrip("/") for url in REPO_URL_RE.findall(text)]
    if len(quick_urls) != len(set(quick_urls)):
        raise ValueError("duplicate repository URL")
    expected_count = (
        10
        if status != "partial" and complete_count >= 10 and len(pool) >= 10
        else min(10, complete_count, len(pool))
    )
    if len(entries) != expected_count:
        raise ValueError(f"{expected_cohort} report has wrong item count")
    selected: list[str] = []
    for index, entry in enumerate(entries, start=1):
        if int(entry.group(1)) != index:
            raise ValueError("missing numbering")
        start = entry.end()
        next_start = entries[index].start() if index < len(entries) else len(text)
        block = text[start:next_start]
        urls = REPO_URL_RE.findall(block)
        if len(urls) != 1:
            raise ValueError("repository URL is missing or duplicated")
        canonical = urls[0].lower().rstrip("/")
        if canonical in other_pool:
            raise ValueError("wrong cohort URL")
        if canonical not in pool:
            raise ValueError("repository URL is outside input pool")
        if canonical in selected:
            raise ValueError("duplicate repository URL")
        score_match = REPORT_SCORE_RE.search(block)
        if score_match is None:
            raise ValueError("score line is missing")
        expected = _record_score(pool[canonical])
        actual = float(score_match.group(1))
        if round(actual, 1) != round(expected, 1):
            raise ValueError("score does not match canonical Python score")
        selected.append(canonical)
    return selected


def _pool_records(pool: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for section in ("primary", "reserve"):
        rows = pool.get(section, []) if isinstance(pool, Mapping) else []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            name = str(row.get("canonical_name", "")).lower()
            if name:
                result[name] = dict(row)
    return result


def _record_score(record: Mapping[str, Any]) -> float:
    value = record.get("python_score", record.get("score"))
    if value is None:
        breakdown = record.get("score_breakdown") or {}
        value = breakdown.get("final")
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("candidate score is missing") from error


def _editorial_candidate(item: RankedCandidate, cohort: Cohort, rank: int) -> EditorialCandidate:
    candidate = item.candidate
    score = item.score
    readme_excerpt = ""
    try:
        payload = json.loads(candidate.quality_evidence or "{}")
        if isinstance(payload, dict):
            readme_excerpt = str(payload.get("readme_excerpt", ""))
    except (TypeError, ValueError):
        pass
    risk_markers = []
    if candidate.archived:
        risk_markers.append("archived")
    if candidate.is_fork:
        risk_markers.append("fork")
    if candidate.is_empty:
        risk_markers.append("empty")
    return EditorialCandidate(
        canonical_name=candidate.canonical_name,
        full_name=candidate.full_name,
        html_url=candidate.html_url,
        cohort=cohort,
        python_rank=rank,
        python_score=round(float(score.final), 1),
        score_breakdown=score,
        stars_total=candidate.stars_total,
        stars_24h=(candidate.velocity_rate_24h or candidate.stars_24h),
        growth_rate_24h=candidate.growth_rate_24h,
        velocity_source=(
            candidate.velocity_source
            if candidate.velocity_source != "unknown"
            else getattr(score, "momentum_source", "unknown")
        ),
        stars_24h_estimated=candidate.stars_24h_estimated,
        primary_language=candidate.primary_language,
        topics=candidate.topics[:20],
        description=_bounded_text(candidate.description or "", 512) or None,
        readme_excerpt=_bounded_text(readme_excerpt, MAX_README_EXCERPT_BYTES),
        created_at=candidate.created_at,
        pushed_at=candidate.pushed_at,
        discovery_sources=sorted(candidate.discovery_sources),
        source_errors=candidate.source_errors[:10],
        risk_markers=risk_markers,
    )


def _atomic_json(path: Path, value: Any, *, max_bytes: int | None = None) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if max_bytes is not None and len(payload.encode("utf-8")) > max_bytes:
        if isinstance(value, dict) and "readme_excerpt" in value:
            value = dict(value)
            value["readme_excerpt"] = _bounded_text(str(value["readme_excerpt"]), max_bytes // 3)
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        payload = payload.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(path)


def _bounded_text(value: str, max_bytes: int) -> str:
    safe = "".join(character if character.isprintable() else " " for character in value)
    return safe.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:160] or "item"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)
