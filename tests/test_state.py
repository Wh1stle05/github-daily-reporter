from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from github_daily_reporter.models import (
    DeliveryPart,
    QualityReview,
    RankedCandidate,
    ScoreBreakdown,
    SourceObservation,
)
from github_daily_reporter.state import StateStore


def candidate(name: str = "owner/repo", stars_total: int = 100):
    from github_daily_reporter.models import RepositoryCandidate

    return RepositoryCandidate(
        canonical_name=name,
        full_name=name,
        html_url=f"https://github.com/{name}",
        created_at=datetime.now(UTC),
        stars_total=stars_total,
        discovery_sources={"trending"},
    )


def ranked_candidate(name: str = "owner/repo", score: float = 42.0):
    return RankedCandidate(
        candidate=candidate(name),
        score=ScoreBreakdown(
            momentum=1.0,
            evidence=2.0,
            freshness=3.0,
            hacker_news=4.0,
            quality=5.0,
            popularity=6.0,
            final=score,
        ),
    )


def test_run_transitions_and_candidates_round_trip(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    run_id = store.start_run(datetime.now(UTC))
    store.save_collection(run_id, [candidate()], [])
    store.finish_run(run_id, "success")

    assert store.get_run_status(run_id) == "success"
    assert store.get_run_candidates(run_id)[0].canonical_name == "owner/repo"


def test_save_collection_rolls_back_candidate_writes_on_invalid_observation(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.sqlite3")
    run_id = store.start_run(datetime.now(UTC))
    bad = SourceObservation.model_construct(source="unknown")
    statements = []
    original_connection = store._connection

    def traced_connection():
        connection = original_connection()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store, "_connection", traced_connection)

    with pytest.raises(Exception):
        store.save_collection(run_id, [candidate()], [bad])

    assert any("INSERT INTO repositories" in statement for statement in statements)
    assert any("INSERT INTO run_candidates" in statement for statement in statements)
    assert store.get_run_candidates(run_id) == []
    assert "owner/repo" not in store.recent_repository_names(datetime.now(UTC) - timedelta(days=1))


def test_save_collection_transaction_rolls_back_snapshots_and_collection_on_invalid_observation(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    run_id = store.start_run(datetime.now(UTC))
    bad = SourceObservation.model_construct(source="unknown")

    with pytest.raises(Exception):
        store.save_collection_transaction(run_id, [candidate()], [bad], datetime.now(UTC))

    assert store.get_run_candidates(run_id) == []
    assert "owner/repo" not in store.recent_repository_names(datetime.now(UTC) - timedelta(days=1))
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM repo_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM source_hits").fetchone()[0] == 0


def test_recent_repositories_respects_cutoff(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    now = datetime.now(UTC)
    store.record_snapshot(candidate("new/repo"), now)
    store.record_snapshot(candidate("old/repo"), now - timedelta(days=20))

    assert store.recent_repository_names(now - timedelta(days=14)) == ["new/repo"]


def test_estimate_stars_24h_uses_closest_snapshot_in_window(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    now = datetime(2026, 7, 23, 12, tzinfo=UTC)
    store.record_snapshot(candidate(stars_total=80), now - timedelta(hours=20))
    expected_at = now - timedelta(hours=24)
    store.record_snapshot(candidate(stars_total=70), expected_at)
    store.record_snapshot(candidate(stars_total=60), now - timedelta(hours=29))

    assert store.estimate_stars_24h(
        "owner/repo", 100, now - timedelta(days=2), now
    ) == (30, expected_at)


def test_estimate_stars_24h_keeps_24h_snapshot_when_cutoff_is_22h(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    now = datetime(2026, 7, 23, 12, tzinfo=UTC)
    expected_at = now - timedelta(hours=24)
    store.record_snapshot(candidate(stars_total=70), expected_at)

    assert store.estimate_stars_24h(
        "owner/repo", 100, now - timedelta(hours=22), now
    ) == (30, expected_at)


def test_estimate_stars_24h_uses_inclusive_window_boundaries_and_cutoff_target(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    now = datetime(2026, 7, 23, 12, tzinfo=UTC)
    earliest = now - timedelta(hours=28)
    latest = now - timedelta(hours=20)
    store.record_snapshot(candidate(stars_total=20), earliest)
    store.record_snapshot(candidate(stars_total=40), now - timedelta(hours=24))
    store.record_snapshot(candidate(stars_total=80), latest)

    assert store.estimate_stars_24h(
        "owner/repo", 100, now - timedelta(hours=21), now
    ) == (20, latest)
    assert store.estimate_stars_24h(
        "owner/repo", 100, now - timedelta(hours=27), now
    ) == (80, earliest)


def test_save_ranking_persists_excluded_review_without_ranked_candidate(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    run_id = store.start_run(datetime.now(UTC))
    review = QualityReview(
        canonical_name="review-only/repo",
        usefulness=1,
        completeness=2,
        novelty=3,
        maintenance=4,
        exclude=True,
        exclude_reason="duplicate",
    )

    store.save_ranking(run_id, [], [review])

    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT review_json, score_json, excluded FROM ranking_decisions "
            "WHERE run_id = ? AND canonical_name = ?",
            (run_id, review.canonical_name),
        ).fetchone()

    assert row == (review.model_dump_json(), "{}", 1)


def test_save_ranking_persists_ranked_candidate_without_review(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    run_id = store.start_run(datetime.now(UTC))
    ranked = ranked_candidate("ranked-only/repo")

    store.save_ranking(run_id, [ranked], [])

    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT review_json, score_json, excluded FROM ranking_decisions "
            "WHERE run_id = ? AND canonical_name = ?",
            (run_id, ranked.candidate.canonical_name),
        ).fetchone()

    assert row == ("{}", ranked.score.model_dump_json(), 0)


def test_save_ranking_persists_actual_review_and_score_for_ranked_review(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    run_id = store.start_run(datetime.now(UTC))
    ranked = ranked_candidate("ranked-and-reviewed/repo")
    review = QualityReview(
        canonical_name=ranked.candidate.canonical_name,
        usefulness=1,
        completeness=2,
        novelty=3,
        maintenance=4,
    )

    store.save_ranking(run_id, [ranked], [review])

    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT review_json, score_json, excluded FROM ranking_decisions "
            "WHERE run_id = ? AND canonical_name = ?",
            (run_id, ranked.candidate.canonical_name),
        ).fetchone()

    assert row == (review.model_dump_json(), ranked.score.model_dump_json(), 0)


def test_initialization_creates_required_tables(tmp_path):
    path = tmp_path / "state.sqlite3"
    StateStore(path)

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {
        "collection_runs",
        "repositories",
        "repo_snapshots",
        "source_hits",
        "run_candidates",
        "ranking_decisions",
        "reports",
        "report_artifacts",
        "delivery_parts",
    } <= tables


def test_save_report_artifacts_persists_all_payloads_and_timestamp(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")

    store.save_report_artifacts("run-1", "source", "review", "ranking", "# Report")

    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT source_json, review_json, ranking_json, markdown, created_at, updated_at "
            "FROM report_artifacts WHERE run_id = ?",
            ("run-1",),
        ).fetchone()
    assert row[:4] == ("source", "review", "ranking", "# Report")
    assert row[4] == row[5]


def test_delivery_part_round_trip(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.enqueue_delivery("run-1", 0, "message", "digest")

    pending = store.pending_deliveries()
    assert isinstance(pending[0], DeliveryPart)
    assert pending[0].body == "message"
    assert pending[0].digest == "digest"
    store.mark_delivery_delivered("run-1", 0, "42")

    assert store.pending_deliveries() == []


def test_enqueue_delivery_is_idempotent_for_same_digest_and_rejects_changed_digest(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.enqueue_delivery("run-1", 0, "message", "digest")
    store.enqueue_delivery("run-1", 0, "message", "digest")

    with pytest.raises(ValueError, match="digest"):
        store.enqueue_delivery("run-1", 0, "changed", "other-digest")


def test_delivery_attempt_and_pending_status_are_recorded_without_raw_error(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.enqueue_delivery("run-1", 0, "message", "digest")

    store.record_delivery_attempt("run-1", 0)
    store.mark_delivery_pending("run-1", 0, "timeout: secret token")

    pending = store.pending_deliveries()[0]
    assert pending.attempts == 1
    assert pending.state == "pending"
    assert pending.error_category == "timeout"
