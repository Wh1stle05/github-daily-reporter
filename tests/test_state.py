from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from github_daily_reporter.models import SourceObservation
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


def test_save_collection_rolls_back_on_invalid_observation(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    run_id = store.start_run(datetime.now(UTC))
    bad = SourceObservation.model_construct(source="unknown")

    with pytest.raises(Exception):
        store.save_collection(run_id, [candidate()], [bad])

    assert store.get_run_candidates(run_id) == []


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
    } <= tables
