import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from github_daily_reporter.models import (
    DeliveryPart,
    QualityReview,
    RankedCandidate,
    RepositoryCandidate,
    SourceHealth,
    SourceObservation,
)


DELIVERY_PARTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_parts (
  run_id TEXT NOT NULL,
  part_index INTEGER NOT NULL CHECK (part_index >= 0),
  body TEXT NOT NULL,
  digest TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','in_flight','delivered')),
  claim_token TEXT,
  claim_deadline TEXT,
  telegram_message_id TEXT,
  error_category TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, part_index)
);
"""


SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_runs (
  id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('running','success','partial','failed')),
  source_health_json TEXT NOT NULL DEFAULT '[]',
  fatal_error TEXT
);
CREATE TABLE IF NOT EXISTS repositories (
  canonical_name TEXT PRIMARY KEY,
  candidate_json TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repo_snapshots (
  canonical_name TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  stars_total INTEGER NOT NULL,
  forks_total INTEGER NOT NULL,
  open_issues_count INTEGER NOT NULL,
  PRIMARY KEY (canonical_name, observed_at)
);
CREATE TABLE IF NOT EXISTS source_hits (
  run_id TEXT NOT NULL REFERENCES collection_runs(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  observation_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_candidates (
  run_id TEXT NOT NULL REFERENCES collection_runs(id) ON DELETE CASCADE,
  canonical_name TEXT NOT NULL,
  candidate_json TEXT NOT NULL,
  PRIMARY KEY (run_id, canonical_name)
);
CREATE TABLE IF NOT EXISTS ranking_decisions (
  run_id TEXT NOT NULL REFERENCES collection_runs(id) ON DELETE CASCADE,
  canonical_name TEXT NOT NULL,
  review_json TEXT NOT NULL,
  score_json TEXT NOT NULL,
  excluded INTEGER NOT NULL,
  PRIMARY KEY (run_id, canonical_name)
);
CREATE TABLE IF NOT EXISTS reports (
  run_id TEXT PRIMARY KEY REFERENCES collection_runs(id) ON DELETE CASCADE,
  markdown TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivery_metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS report_artifacts (
  run_id TEXT PRIMARY KEY,
  source_json TEXT NOT NULL,
  review_json TEXT NOT NULL,
  ranking_json TEXT NOT NULL,
  markdown TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
""" + DELIVERY_PARTS_SCHEMA


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(SCHEMA)
            self._migrate_delivery_parts(connection)

    @staticmethod
    def _migrate_delivery_parts(connection: sqlite3.Connection) -> None:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(delivery_parts)")
        }
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'delivery_parts'"
        ).fetchone()[0]
        if {"claim_token", "claim_deadline"} <= columns and "in_flight" in table_sql:
            return

        connection.execute("ALTER TABLE delivery_parts RENAME TO delivery_parts_legacy")
        connection.executescript(DELIVERY_PARTS_SCHEMA)
        claim_token = "claim_token" if "claim_token" in columns else "NULL"
        claim_deadline = "claim_deadline" if "claim_deadline" in columns else "NULL"
        connection.execute(
            "INSERT INTO delivery_parts "
            "(run_id, part_index, body, digest, attempts, state, claim_token, claim_deadline, "
            "telegram_message_id, error_category, created_at, updated_at) "
            "SELECT run_id, part_index, body, digest, attempts, state, "
            f"{claim_token}, {claim_deadline}, telegram_message_id, error_category, created_at, updated_at "
            "FROM delivery_parts_legacy"
        )
        connection.execute(
            "UPDATE delivery_parts SET state = 'pending', claim_token = NULL "
            "WHERE state = 'in_flight' AND claim_deadline IS NULL"
        )
        connection.execute("DROP TABLE delivery_parts_legacy")

    def start_run(self, started_at: datetime) -> str:
        run_id = str(uuid4())
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO collection_runs (id, started_at, status) VALUES (?, ?, 'running')",
                (run_id, _timestamp(started_at)),
            )
        return run_id

    def save_collection(
        self,
        run_id: str,
        candidates: list[RepositoryCandidate],
        observations: list[SourceObservation],
    ) -> None:
        seen_at = _timestamp(datetime.now(UTC))
        with self._connection() as connection:
            self._save_collection(connection, run_id, candidates, observations, seen_at)

    def save_collection_transaction(
        self,
        run_id: str,
        candidates: list[RepositoryCandidate],
        observations: list[SourceObservation],
        observed_at: datetime,
    ) -> None:
        """Atomically record snapshots and all collection records for one run."""
        timestamp = _timestamp(observed_at)
        with self._connection() as connection:
            for candidate in candidates:
                self._record_snapshot(connection, candidate, timestamp)
            self._save_collection(connection, run_id, candidates, observations, timestamp)

    def finish_run(
        self,
        run_id: str,
        status: str,
        source_health: list[SourceHealth] | None = None,
        fatal_error: str | None = None,
    ) -> None:
        source_health_json = (
            "[" + ",".join(item.model_dump_json() for item in source_health) + "]"
            if source_health is not None
            else "[]"
        )
        with self._connection() as connection:
            connection.execute(
                "UPDATE collection_runs SET finished_at = ?, status = ?, source_health_json = ?, "
                "fatal_error = ? WHERE id = ?",
                (_timestamp(datetime.now(UTC)), status, source_health_json, fatal_error, run_id),
            )

    def get_run_status(self, run_id: str) -> str:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT status FROM collection_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return row[0]

    def get_run_started_at(self, run_id: str) -> datetime:
        """Return the immutable collection timestamp used for reproducible ranking."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT started_at FROM collection_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _parse_timestamp(row[0])

    def get_run_candidates(self, run_id: str) -> list[RepositoryCandidate]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT candidate_json FROM run_candidates WHERE run_id = ? "
                "ORDER BY canonical_name",
                (run_id,),
            ).fetchall()
        return [RepositoryCandidate.model_validate_json(row[0]) for row in rows]

    def record_snapshot(self, candidate: RepositoryCandidate, observed_at: datetime) -> None:
        timestamp = _timestamp(observed_at)
        with self._connection() as connection:
            self._record_snapshot(connection, candidate, timestamp)

    @staticmethod
    def _record_snapshot(
        connection: sqlite3.Connection, candidate: RepositoryCandidate, timestamp: str
    ) -> None:
        connection.execute(
            "INSERT INTO repositories (canonical_name, candidate_json, last_seen_at) "
            "VALUES (?, ?, ?) ON CONFLICT(canonical_name) DO UPDATE SET "
            "candidate_json = excluded.candidate_json, last_seen_at = excluded.last_seen_at",
            (candidate.canonical_name, candidate.model_dump_json(), timestamp),
        )
        existing = connection.execute(
            "SELECT 1 FROM repo_snapshots WHERE canonical_name = ? AND observed_at = ?",
            (candidate.canonical_name, timestamp),
        ).fetchone()
        if existing is not None:
            raise ValueError("duplicate snapshot timestamp")
        connection.execute(
            "INSERT INTO repo_snapshots "
            "(canonical_name, observed_at, stars_total, forks_total, open_issues_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                candidate.canonical_name,
                timestamp,
                candidate.stars_total,
                candidate.forks_total,
                candidate.open_issues_count,
            ),
        )

    @staticmethod
    def _save_collection(
        connection: sqlite3.Connection,
        run_id: str,
        candidates: list[RepositoryCandidate],
        observations: list[SourceObservation],
        seen_at: str,
    ) -> None:
        for candidate in candidates:
            connection.execute(
                "INSERT INTO repositories (canonical_name, candidate_json, last_seen_at) "
                "VALUES (?, ?, ?) ON CONFLICT(canonical_name) DO UPDATE SET "
                "candidate_json = excluded.candidate_json, last_seen_at = excluded.last_seen_at",
                (candidate.canonical_name, candidate.model_dump_json(), seen_at),
            )
            connection.execute(
                "INSERT INTO run_candidates (run_id, canonical_name, candidate_json) VALUES (?, ?, ?)",
                (run_id, candidate.canonical_name, candidate.model_dump_json()),
            )
        for observation in observations:
            validated = SourceObservation.model_validate(observation.model_dump())
            canonical_name = f"{validated.owner}/{validated.name}".lower()
            connection.execute(
                "INSERT INTO source_hits (run_id, source, canonical_name, observation_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    run_id,
                    validated.source,
                    canonical_name,
                    validated.model_dump_json(),
                ),
            )

    def recent_repository_names(self, cutoff: datetime) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT canonical_name FROM repositories WHERE last_seen_at >= ? "
                "ORDER BY canonical_name",
                (_timestamp(cutoff),),
            ).fetchall()
        return [row[0] for row in rows]

    def estimate_stars_24h(
        self,
        canonical_name: str,
        current_stars: int,
        cutoff: datetime,
        now: datetime,
    ) -> tuple[int, datetime] | None:
        now_utc = _require_aware(now)
        target = _require_aware(cutoff)
        earliest = now_utc - timedelta(hours=48)
        latest = now_utc
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT stars_total, observed_at FROM repo_snapshots "
                "WHERE canonical_name = ? AND observed_at >= ? AND observed_at <= ?",
                (canonical_name, _timestamp(earliest), _timestamp(latest)),
            ).fetchall()
        if not rows:
            return None
        stars_total, observed_at = min(
            rows, key=lambda row: abs((_parse_timestamp(row[1]) - target).total_seconds())
        )
        observed = _parse_timestamp(observed_at)
        if observed >= now_utc or current_stars < stars_total:
            return None
        return current_stars - stars_total, observed

    def save_ranking(
        self,
        run_id: str,
        ranked: list[RankedCandidate],
        reviews: list[QualityReview],
        excluded_reasons: dict[str, str] | None = None,
        deterministic_exclusions: dict[str, str] | None = None,
    ) -> None:
        """Persist every rank or exclusion decision for a collection run."""
        ranked_by_name = {item.candidate.canonical_name: item for item in ranked}
        reviews_by_name = {review.canonical_name: review for review in reviews}
        deterministic_exclusions = deterministic_exclusions or {}
        excluded_reasons = {
            review.canonical_name: review.exclude_reason or "llm_exclusion"
            for review in reviews
            if review.exclude
        } | (excluded_reasons or {})
        with self._connection() as connection:
            for canonical_name in sorted(
                ranked_by_name.keys() | reviews_by_name.keys() | excluded_reasons.keys()
            ):
                item = ranked_by_name.get(canonical_name)
                review = reviews_by_name.get(canonical_name)
                review_json = (
                    json.dumps(
                        {
                            "review": review.model_dump(mode="json"),
                            "deterministic_exclusion": deterministic_exclusions[canonical_name],
                        },
                        separators=(",", ":"),
                    )
                    if review is not None and canonical_name in deterministic_exclusions
                    else review.model_dump_json()
                    if review is not None
                    else json.dumps(
                        {"deterministic_exclusion": excluded_reasons[canonical_name]},
                        separators=(",", ":"),
                    )
                    if canonical_name in excluded_reasons
                    else "{}"
                )
                connection.execute(
                    "INSERT INTO ranking_decisions "
                    "(run_id, canonical_name, review_json, score_json, excluded) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(run_id, canonical_name) DO UPDATE SET "
                    "review_json = excluded.review_json, score_json = excluded.score_json, "
                    "excluded = excluded.excluded",
                    (
                        run_id,
                        canonical_name,
                        review_json,
                        item.score.model_dump_json() if item is not None else "{}",
                        int(canonical_name in excluded_reasons or canonical_name in deterministic_exclusions),
                    ),
                )

    def save_report_artifacts(
        self,
        run_id: str,
        source_json: str,
        review_json: str,
        ranking_json: str,
        markdown: str,
        created_at: datetime | None = None,
    ) -> None:
        """Atomically persist the rendered report and its source artifacts."""
        timestamp = _timestamp(created_at or datetime.now(UTC))
        with self._connection() as connection:
            self._save_report_artifacts(
                connection,
                run_id,
                source_json,
                review_json,
                ranking_json,
                markdown,
                timestamp,
            )

    def save_report_artifacts_and_enqueue_delivery(
        self,
        run_id: str,
        source_json: str,
        review_json: str,
        ranking_json: str,
        markdown: str,
        parts: Iterable[tuple[int, str]],
        created_at: datetime | None = None,
    ) -> None:
        """Persist a successful report and every delivery part in one transaction."""
        prepared = self._prepare_delivery_parts(parts)
        timestamp = _timestamp(created_at or datetime.now(UTC))
        with self._connection() as connection:
            self._save_report_artifacts(
                connection,
                run_id,
                source_json,
                review_json,
                ranking_json,
                markdown,
                timestamp,
            )
            self._enqueue_delivery_parts(connection, run_id, prepared, timestamp)

    @staticmethod
    def _save_report_artifacts(
        connection: sqlite3.Connection,
        run_id: str,
        source_json: str,
        review_json: str,
        ranking_json: str,
        markdown: str,
        timestamp: str,
    ) -> None:
        connection.execute(
            "INSERT INTO report_artifacts "
            "(run_id, source_json, review_json, ranking_json, markdown, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "source_json = excluded.source_json, review_json = excluded.review_json, "
            "ranking_json = excluded.ranking_json, markdown = excluded.markdown, "
            "updated_at = excluded.updated_at",
            (run_id, source_json, review_json, ranking_json, markdown, timestamp, timestamp),
        )

    def enqueue_delivery(
        self,
        run_id: str,
        part_index: int,
        body: str,
        digest: str | None = None,
    ) -> None:
        """Queue a Telegram part, rejecting content changes for an existing key."""
        if part_index < 0:
            raise ValueError("part_index must be non-negative")
        expected_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if digest is not None and digest != expected_digest:
            raise ValueError("digest must match SHA-256 of delivery body")
        self.enqueue_delivery_batch(run_id, [(part_index, body)])

    def enqueue_delivery_batch(
        self,
        run_id: str,
        parts: Iterable[tuple[int, str]],
    ) -> None:
        """Atomically queue all Telegram parts for a report.

        Existing same-key, same-content rows are idempotent. Any validation or
        database error rolls back the complete batch, leaving no partial queue.
        """
        prepared = self._prepare_delivery_parts(parts)
        if not prepared:
            return
        timestamp = _timestamp(datetime.now(UTC))
        with self._connection() as connection:
            self._enqueue_delivery_parts(connection, run_id, prepared, timestamp)

    @staticmethod
    def _prepare_delivery_parts(parts: Iterable[tuple[int, str]]) -> list[tuple[int, str, str]]:
        """Validate and hash delivery parts before beginning a write transaction."""
        prepared: list[tuple[int, str, str]] = []
        seen_indices: set[int] = set()
        for part_index, body in parts:
            if part_index < 0:
                raise ValueError("part_index must be non-negative")
            if part_index in seen_indices:
                raise ValueError("duplicate delivery part index")
            seen_indices.add(part_index)
            prepared.append(
                (part_index, body, hashlib.sha256(body.encode("utf-8")).hexdigest())
            )
        return prepared

    @staticmethod
    def _enqueue_delivery_parts(
        connection: sqlite3.Connection,
        run_id: str,
        prepared: Iterable[tuple[int, str, str]],
        timestamp: str,
    ) -> None:
        for part_index, body, digest in prepared:
            existing = connection.execute(
                "SELECT digest FROM delivery_parts WHERE run_id = ? AND part_index = ?",
                (run_id, part_index),
            ).fetchone()
            if existing is not None:
                if existing[0] != digest:
                    raise ValueError("delivery digest does not match existing part")
                continue
            connection.execute(
                "INSERT INTO delivery_parts "
                "(run_id, part_index, body, digest, attempts, state, claim_token, claim_deadline, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, 0, 'pending', NULL, NULL, ?, ?)",
                (run_id, part_index, body, digest, timestamp, timestamp),
            )

    def pending_deliveries(
        self, run_id: str | None = None, now: datetime | None = None
    ) -> list[DeliveryPart]:
        """Return queued parts in stable run and part order."""
        timestamp = _timestamp(now or datetime.now(UTC))
        query = (
            "SELECT run_id, part_index, body, digest, attempts, state, "
            "claim_token, claim_deadline, telegram_message_id, error_category, created_at, updated_at "
            "FROM delivery_parts WHERE state = 'pending'"
        )
        parameters: tuple[str, ...] = ()
        if run_id is not None:
            query += " AND run_id = ?"
            parameters = (run_id,)
        query += " ORDER BY run_id, part_index"
        with self._connection() as connection:
            self._reclaim_expired_deliveries(connection, timestamp)
            rows = connection.execute(query, parameters).fetchall()
        return [self._delivery_part(row) for row in rows]

    def claim_delivery(
        self,
        run_id: str,
        part_index: int,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> DeliveryPart | None:
        """Atomically claim one pending part so only its holder may transition it."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        claim_token = str(uuid4())
        claimed_at = now or datetime.now(UTC)
        timestamp = _timestamp(claimed_at)
        claim_deadline = _timestamp(claimed_at + timedelta(seconds=lease_seconds))
        with self._connection() as connection:
            self._reclaim_expired_deliveries(connection, timestamp)
            cursor = connection.execute(
                "UPDATE delivery_parts SET state = 'in_flight', claim_token = ?, claim_deadline = ?, "
                "attempts = attempts + 1, updated_at = ? "
                "WHERE run_id = ? AND part_index = ? AND state = 'pending' AND claim_token IS NULL "
                "AND NOT EXISTS ("
                "SELECT 1 FROM delivery_parts AS predecessor "
                "WHERE predecessor.run_id = delivery_parts.run_id "
                "AND predecessor.part_index < delivery_parts.part_index "
                "AND predecessor.state IN ('pending', 'in_flight')"
                ")",
                (claim_token, claim_deadline, timestamp, run_id, part_index),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT run_id, part_index, body, digest, attempts, state, "
                "claim_token, claim_deadline, telegram_message_id, error_category, created_at, updated_at "
                "FROM delivery_parts WHERE run_id = ? AND part_index = ?",
                (run_id, part_index),
            ).fetchone()
        return self._delivery_part(row)

    def record_delivery_attempt(
        self, run_id: str, part_index: int, error_category: str | None = None
    ) -> None:
        """Increment a part's attempt count and optionally store a safe category."""
        timestamp = _timestamp(datetime.now(UTC))
        sanitized = _sanitize_error_category(error_category) if error_category else None
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE delivery_parts SET attempts = attempts + 1, "
                "error_category = COALESCE(?, error_category), updated_at = ? "
                "WHERE run_id = ? AND part_index = ?",
                (sanitized, timestamp, run_id, part_index),
            )
            if cursor.rowcount == 0:
                raise KeyError((run_id, part_index))

    def mark_delivery_delivered(
        self,
        run_id: str,
        part_index: int,
        telegram_message_id: str,
        claim_token: str | None = None,
    ) -> bool:
        timestamp = _timestamp(datetime.now(UTC))
        with self._connection() as connection:
            where, parameters = _delivery_transition_condition(claim_token)
            cursor = connection.execute(
                "UPDATE delivery_parts SET state = 'delivered', telegram_message_id = ?, "
                "claim_token = NULL, claim_deadline = NULL, error_category = NULL, updated_at = ? "
                f"WHERE run_id = ? AND part_index = ? AND {where}",
                (telegram_message_id, timestamp, run_id, part_index, *parameters),
            )
            if cursor.rowcount == 0:
                if not self._delivery_exists(connection, run_id, part_index):
                    raise KeyError((run_id, part_index))
                return False
        return True

    def mark_delivery_pending(
        self,
        run_id: str,
        part_index: int,
        error_category: str | None = None,
        claim_token: str | None = None,
    ) -> bool:
        timestamp = _timestamp(datetime.now(UTC))
        sanitized = _sanitize_error_category(error_category) if error_category else None
        with self._connection() as connection:
            where, parameters = _delivery_transition_condition(claim_token)
            cursor = connection.execute(
                "UPDATE delivery_parts SET state = 'pending', telegram_message_id = NULL, "
                "claim_token = NULL, claim_deadline = NULL, error_category = ?, updated_at = ? "
                f"WHERE run_id = ? AND part_index = ? AND {where}",
                (sanitized, timestamp, run_id, part_index, *parameters),
            )
            if cursor.rowcount == 0:
                if not self._delivery_exists(connection, run_id, part_index):
                    raise KeyError((run_id, part_index))
                return False
        return True

    @staticmethod
    def _delivery_part(row: tuple[object, ...]) -> DeliveryPart:
        return DeliveryPart(
            run_id=row[0],
            part_index=row[1],
            body=row[2],
            digest=row[3],
            attempts=row[4],
            state=row[5],
            claim_token=row[6],
            claim_deadline=_parse_timestamp(row[7]) if row[7] is not None else None,
            telegram_message_id=row[8],
            error_category=row[9],
            created_at=_parse_timestamp(row[10]),
            updated_at=_parse_timestamp(row[11]),
        )

    @staticmethod
    def _delivery_exists(connection: sqlite3.Connection, run_id: str, part_index: int) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM delivery_parts WHERE run_id = ? AND part_index = ?",
                (run_id, part_index),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _reclaim_expired_deliveries(connection: sqlite3.Connection, timestamp: str) -> None:
        connection.execute(
            "UPDATE delivery_parts SET state = 'pending', claim_token = NULL, "
            "claim_deadline = NULL, updated_at = ? "
            "WHERE state = 'in_flight' AND claim_deadline IS NOT NULL AND claim_deadline <= ?",
            (timestamp, timestamp),
        )


def _timestamp(value: datetime) -> str:
    return _require_aware(value).astimezone(UTC).isoformat()


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


_DELIVERY_ERROR_CATEGORIES = {
    "timeout",
    "transport",
    "http_status",
    "rate_limited",
    "server_error",
    "http_429",
    "http_5xx",
    "message_entry_too_large",
    "invalid_response",
    "unknown",
}


def _sanitize_error_category(value: str | None) -> str:
    if not value:
        return "unknown"
    normalized = str(value).strip().lower()
    for category in _DELIVERY_ERROR_CATEGORIES:
        if normalized == category or normalized.startswith(category + ":"):
            return category
    return "unknown"


def _delivery_transition_condition(claim_token: str | None) -> tuple[str, tuple[str, ...]]:
    if claim_token is None:
        return "state = 'pending' AND claim_token IS NULL", ()
    return "state = 'in_flight' AND claim_token = ?", (claim_token,)
