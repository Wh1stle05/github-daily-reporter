import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from github_daily_reporter.models import (
    QualityReview,
    RankedCandidate,
    RepositoryCandidate,
    SourceHealth,
    SourceObservation,
)


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
"""


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
            connection.execute(
                "INSERT INTO repositories (canonical_name, candidate_json, last_seen_at) "
                "VALUES (?, ?, ?) ON CONFLICT(canonical_name) DO UPDATE SET "
                "candidate_json = excluded.candidate_json, last_seen_at = excluded.last_seen_at",
                (candidate.canonical_name, candidate.model_dump_json(), timestamp),
            )
            connection.execute(
                "INSERT INTO repo_snapshots "
                "(canonical_name, observed_at, stars_total, forks_total, open_issues_count) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(canonical_name, observed_at) DO UPDATE SET "
                "stars_total = excluded.stars_total, forks_total = excluded.forks_total, "
                "open_issues_count = excluded.open_issues_count",
                (
                    candidate.canonical_name,
                    timestamp,
                    candidate.stars_total,
                    candidate.forks_total,
                    candidate.open_issues_count,
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
        earliest = max(cutoff, now - timedelta(hours=28))
        latest = now - timedelta(hours=20)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT stars_total, observed_at FROM repo_snapshots "
                "WHERE canonical_name = ? AND observed_at >= ? AND observed_at <= ?",
                (canonical_name, _timestamp(earliest), _timestamp(latest)),
            ).fetchall()
        if not rows:
            return None
        stars_total, observed_at = min(
            rows,
            key=lambda row: abs((_parse_timestamp(row[1]) - (now - timedelta(hours=24))).total_seconds()),
        )
        return max(current_stars - stars_total, 0), _parse_timestamp(observed_at)

    def save_ranking(
        self,
        run_id: str,
        ranked: list[RankedCandidate],
        reviews: list[QualityReview],
    ) -> None:
        reviews_by_name = {review.canonical_name: review for review in reviews}
        with self._connection() as connection:
            for item in ranked:
                review = reviews_by_name[item.candidate.canonical_name]
                connection.execute(
                    "INSERT INTO ranking_decisions "
                    "(run_id, canonical_name, review_json, score_json, excluded) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(run_id, canonical_name) DO UPDATE SET "
                    "review_json = excluded.review_json, score_json = excluded.score_json, "
                    "excluded = excluded.excluded",
                    (
                        run_id,
                        item.candidate.canonical_name,
                        review.model_dump_json(),
                        item.score.model_dump_json(),
                        int(review.exclude),
                    ),
                )


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)
