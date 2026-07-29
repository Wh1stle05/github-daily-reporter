from datetime import UTC, datetime, timedelta
import hashlib
import sqlite3

import pytest

from github_daily_reporter.models import (
    DeliveryPart,
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
    ) == (40, now - timedelta(hours=29))


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


def test_delivery_part_round_trip(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    digest = hashlib.sha256(b"message").hexdigest()
    store.enqueue_delivery("run-1", 0, "message", digest)

    pending = store.pending_deliveries()
    assert isinstance(pending[0], DeliveryPart)
    assert pending[0].body == "message"
    assert pending[0].digest == digest
    store.mark_delivery_delivered("run-1", 0, "42")

    assert store.pending_deliveries() == []


def test_enqueue_delivery_is_idempotent_for_same_digest_and_rejects_changed_digest(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    digest = hashlib.sha256(b"message").hexdigest()
    store.enqueue_delivery("run-1", 0, "message", digest)
    store.enqueue_delivery("run-1", 0, "message", digest)

    with pytest.raises(ValueError, match="digest"):
        store.enqueue_delivery("run-1", 0, "changed", "other-digest")


def test_enqueue_delivery_rejects_digest_not_derived_from_body(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")

    with pytest.raises(ValueError, match="SHA-256"):
        store.enqueue_delivery("run-1", 0, "message", "digest")


def test_delivery_attempt_and_pending_status_are_recorded_without_raw_error(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.enqueue_delivery("run-1", 0, "message", hashlib.sha256(b"message").hexdigest())

    store.record_delivery_attempt("run-1", 0)
    store.mark_delivery_pending("run-1", 0, "timeout: secret token")

    pending = store.pending_deliveries()[0]
    assert pending.attempts == 1
    assert pending.state == "pending"
    assert pending.error_category == "timeout"


def test_delivery_claim_allows_only_one_overlapping_reporter(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.enqueue_delivery("run-1", 0, "message")

    first_claim = store.claim_delivery("run-1", 0)
    second_claim = store.claim_delivery("run-1", 0)

    assert first_claim is not None
    assert first_claim.state == "in_flight"
    assert first_claim.claim_token is not None
    assert first_claim.attempts == 1
    assert second_claim is None
    assert store.pending_deliveries() == []


def test_delivery_claim_blocks_later_part_until_all_predecessors_delivered(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.enqueue_delivery_batch("run-1", [(0, "first"), (1, "second")])

    assert store.claim_delivery("run-1", 1) is None
    first_claim = store.claim_delivery("run-1", 0)
    assert first_claim is not None
    assert store.claim_delivery("run-1", 1) is None

    assert store.mark_delivery_delivered("run-1", 0, "42", first_claim.claim_token)
    second_claim = store.claim_delivery("run-1", 1)
    assert second_claim is not None


def test_enqueue_delivery_batch_rolls_back_all_parts_on_insert_failure(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_second_delivery_part "
            "BEFORE INSERT ON delivery_parts WHEN NEW.part_index = 1 "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        store.enqueue_delivery_batch("run-1", [(0, "first"), (1, "second")])

    assert store.pending_deliveries("run-1") == []


@pytest.mark.parametrize("category", ["http_429", "http_5xx", "message_entry_too_large"])
def test_delivery_failure_categories_are_persisted_stably(tmp_path, category):
    store = StateStore(tmp_path / "state.sqlite3")
    store.enqueue_delivery("run-1", 0, "message")

    store.mark_delivery_pending("run-1", 0, category)

    assert store.pending_deliveries("run-1")[0].error_category == category


def test_stale_claim_failure_cannot_requeue_a_later_delivery(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.enqueue_delivery("run-1", 0, "message")
    first_claim = store.claim_delivery("run-1", 0)
    assert first_claim is not None

    assert store.mark_delivery_pending(
        "run-1", 0, "timeout", first_claim.claim_token
    )
    second_claim = store.claim_delivery("run-1", 0)
    assert second_claim is not None
    assert store.mark_delivery_delivered(
        "run-1", 0, "42", second_claim.claim_token
    )

    assert not store.mark_delivery_pending(
        "run-1", 0, "timeout", first_claim.claim_token
    )
    assert store.pending_deliveries() == []


def test_expired_delivery_claim_is_reclaimed_while_active_claim_stays_hidden(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.enqueue_delivery("run-1", 0, "message")
    claimed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    claim = store.claim_delivery("run-1", 0, now=claimed_at, lease_seconds=60)
    assert claim is not None

    assert store.pending_deliveries(now=claimed_at + timedelta(seconds=59)) == []
    reclaimed = store.pending_deliveries(now=claimed_at + timedelta(seconds=61))

    assert len(reclaimed) == 1
    assert reclaimed[0].state == "pending"
    assert reclaimed[0].claim_token is None
    assert store.claim_delivery("run-1", 0, now=claimed_at + timedelta(seconds=61)) is not None


def test_error_notifications_have_independent_pending_state(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.enqueue_error_notification("run-1", "collection failed")
    pending = store.pending_error_notifications()
    assert [item["run_id"] for item in pending] == ["run-1"]
    store.mark_error_notification_delivered("run-1", "99")
    assert store.pending_error_notifications() == []
