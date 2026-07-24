import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import github_daily_reporter.cli as cli
from github_daily_reporter.cli import main
from github_daily_reporter.config import load_config
from github_daily_reporter.models import CollectionEnvelope
from github_daily_reporter.state import StateStore


ENVELOPE = CollectionEnvelope(
    run_id="run-1",
    status="success",
    generated_at=datetime(2026, 7, 23, tzinfo=UTC),
    source_health=[],
    candidates=[],
    quality_review_path="data/runs/run-1/quality-review.json",
)


@pytest.fixture
def store_with_run(config_path, candidate_factory):
    config = load_config(config_path)
    store = StateStore(config.state_db)
    run_id = store.start_run(datetime(2026, 7, 23, tzinfo=UTC))
    candidates = [
        candidate_factory(
            canonical_name="a/repo", full_name="a/repo", html_url="https://github.com/a/repo"
        ),
        candidate_factory(
            canonical_name="b/repo", full_name="b/repo", html_url="https://github.com/b/repo"
        ),
        candidate_factory(
            canonical_name="spam/repo", full_name="spam/repo", html_url="https://github.com/spam/repo"
        ),
        candidate_factory(
            canonical_name="archived/repo",
            full_name="archived/repo",
            html_url="https://github.com/archived/repo",
            archived=True,
        ),
    ]
    store.save_collection(run_id, candidates, [])
    store.finish_run(run_id, "success")
    run_dir = config.project_root / "data" / "runs" / run_id
    run_dir.mkdir(parents=True)
    return SimpleNamespace(config_path=config_path, run_id=run_id, run_dir=run_dir, store=store)


def test_collect_prints_exactly_one_json_document(monkeypatch, capsys, config_path):
    async def fake_collection(_config_path):
        return ENVELOPE

    monkeypatch.setattr("github_daily_reporter.cli.run_collection", fake_collection)

    assert main(["collect", "--config", str(config_path)]) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == "1"


def test_rank_rejects_quality_file_outside_run_directory(tmp_path, capsys, store_with_run):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"run_id": store_with_run.run_id, "reviews": []}), encoding="utf-8")

    code = main([
        "rank", "--config", str(store_with_run.config_path), "--run-id", store_with_run.run_id,
        "--quality-file", str(outside),
    ])

    assert code == 2
    assert "quality file must be inside" in capsys.readouterr().err


def test_rank_rejects_an_unknown_run_id(config_path, capsys):
    quality_file = config_path.parent / "data" / "runs" / "missing-run" / "quality-review.json"
    quality_file.parent.mkdir(parents=True)
    quality_file.write_text('{"run_id":"missing-run","reviews":[]}', encoding="utf-8")
    assert main([
        "rank", "--config", str(config_path), "--run-id", "missing-run",
        "--quality-file", str(quality_file),
    ]) == 2
    assert "unknown run_id: missing-run" in capsys.readouterr().err


def test_rank_applies_duplicate_and_exclusion_reviews(store_with_run, capsys):
    review = store_with_run.run_dir / "quality-review.json"
    review.write_text(json.dumps({"run_id": store_with_run.run_id, "reviews": [
        {"canonical_name": "a/repo", "usefulness": 4, "completeness": 4, "novelty": 4, "maintenance": 4},
        {"canonical_name": "b/repo", "usefulness": 3, "completeness": 3, "novelty": 3, "maintenance": 3,
         "duplicate_of": "a/repo"},
        {"canonical_name": "spam/repo", "usefulness": 0, "completeness": 0, "novelty": 0, "maintenance": 0,
         "exclude": True, "exclude_reason": "repository evidence shows no code"},
    ]}), encoding="utf-8")

    assert main([
        "rank", "--config", str(store_with_run.config_path), "--run-id", store_with_run.run_id,
        "--quality-file", str(review),
    ]) == 0
    names = [item["candidate"]["canonical_name"] for item in json.loads(capsys.readouterr().out)["ranked"]]
    assert names == ["a/repo"]


def test_rank_uses_neutral_quality_for_omitted_review(store_with_run, capsys):
    review = store_with_run.run_dir / "quality-review.json"
    review.write_text(json.dumps({"run_id": store_with_run.run_id, "reviews": []}), encoding="utf-8")

    assert main([
        "rank", "--config", str(store_with_run.config_path), "--run-id", store_with_run.run_id,
        "--quality-file", str(review),
    ]) == 0

    ranked = json.loads(capsys.readouterr().out)["ranked"]
    assert all(item["score"]["quality"] == 50 for item in ranked)
    assert all(item["quality_degraded"] for item in ranked)


def test_rank_uses_immutable_run_start_time_not_wall_clock(store_with_run, capsys, monkeypatch):
    review = store_with_run.run_dir / "quality-review.json"
    review.write_text(json.dumps({"run_id": store_with_run.run_id, "reviews": []}), encoding="utf-8")

    class FirstClock:
        @staticmethod
        def now():
            return datetime(2026, 8, 1, tzinfo=UTC)

    class SecondClock:
        @staticmethod
        def now():
            return datetime(2027, 8, 1, tzinfo=UTC)

    arguments = [
        "rank", "--config", str(store_with_run.config_path), "--run-id", store_with_run.run_id,
        "--quality-file", str(review),
    ]
    monkeypatch.setattr(cli, "datetime", FirstClock)
    assert main(arguments) == 0
    first = json.loads(capsys.readouterr().out)["ranked"]

    monkeypatch.setattr(cli, "datetime", SecondClock)
    assert main(arguments) == 0
    second = json.loads(capsys.readouterr().out)["ranked"]

    assert first == second


@pytest.mark.parametrize(
    ("review_data", "message"),
    [
        ({"duplicate_of": "missing/repo"}, "duplicate_of must reference a candidate in this run"),
        ({"duplicate_of": "a/repo"}, "cannot reference itself"),
        ({"exclude": True, "exclude_reason": " "}, "exclude_reason is required"),
    ],
)
def test_rank_rejects_invalid_review_references(store_with_run, capsys, review_data, message):
    review = store_with_run.run_dir / "quality-review.json"
    payload = {"canonical_name": "a/repo", "usefulness": 1, "completeness": 1, "novelty": 1, "maintenance": 1}
    payload.update(review_data)
    review.write_text(json.dumps({"run_id": store_with_run.run_id, "reviews": [payload]}), encoding="utf-8")

    assert main([
        "rank", "--config", str(store_with_run.config_path), "--run-id", store_with_run.run_id,
        "--quality-file", str(review),
    ]) == 2
    assert message in capsys.readouterr().err


def test_rank_persists_excluded_decision(store_with_run, capsys):
    review = store_with_run.run_dir / "quality-review.json"
    review.write_text(json.dumps({"run_id": store_with_run.run_id, "reviews": [
        {"canonical_name": "a/repo", "usefulness": 0, "completeness": 0, "novelty": 0, "maintenance": 0,
         "exclude": True, "exclude_reason": "evidence"},
    ]}), encoding="utf-8")

    assert main([
        "rank", "--config", str(store_with_run.config_path), "--run-id", store_with_run.run_id,
        "--quality-file", str(review),
    ]) == 0
    capsys.readouterr()
    with sqlite3.connect(store_with_run.store.path) as connection:
        assert connection.execute(
            "SELECT excluded FROM ranking_decisions WHERE run_id = ? AND canonical_name = ?",
            (store_with_run.run_id, "a/repo"),
        ).fetchone() == (1,)


def test_rank_persists_duplicate_decision_as_excluded(store_with_run, capsys):
    review = store_with_run.run_dir / "quality-review.json"
    review.write_text(json.dumps({"run_id": store_with_run.run_id, "reviews": [
        {"canonical_name": "b/repo", "usefulness": 1, "completeness": 1, "novelty": 1, "maintenance": 1,
         "duplicate_of": "a/repo"},
    ]}), encoding="utf-8")

    assert main([
        "rank", "--config", str(store_with_run.config_path), "--run-id", store_with_run.run_id,
        "--quality-file", str(review),
    ]) == 0
    capsys.readouterr()
    with sqlite3.connect(store_with_run.store.path) as connection:
        assert connection.execute(
            "SELECT excluded FROM ranking_decisions WHERE run_id = ? AND canonical_name = ?",
            (store_with_run.run_id, "b/repo"),
        ).fetchone() == (1,)


def test_rank_persists_deterministic_exclusion_decision(store_with_run, capsys):
    review = store_with_run.run_dir / "quality-review.json"
    review.write_text(json.dumps({"run_id": store_with_run.run_id, "reviews": []}), encoding="utf-8")

    assert main([
        "rank", "--config", str(store_with_run.config_path), "--run-id", store_with_run.run_id,
        "--quality-file", str(review),
    ]) == 0
    capsys.readouterr()
    with sqlite3.connect(store_with_run.store.path) as connection:
        assert connection.execute(
            "SELECT review_json, score_json, excluded FROM ranking_decisions "
            "WHERE run_id = ? AND canonical_name = ?",
            (store_with_run.run_id, "archived/repo"),
        ).fetchone() == ('{"deterministic_exclusion":"archived"}', "{}", 1)


def test_rank_persists_review_and_deterministic_exclusion_reason(store_with_run, capsys):
    review = store_with_run.run_dir / "quality-review.json"
    review.write_text(json.dumps({"run_id": store_with_run.run_id, "reviews": [
        {"canonical_name": "archived/repo", "usefulness": 4, "completeness": 4, "novelty": 4, "maintenance": 4},
    ]}), encoding="utf-8")

    assert main([
        "rank", "--config", str(store_with_run.config_path), "--run-id", store_with_run.run_id,
        "--quality-file", str(review),
    ]) == 0
    capsys.readouterr()
    with sqlite3.connect(store_with_run.store.path) as connection:
        review_json, excluded = connection.execute(
            "SELECT review_json, excluded FROM ranking_decisions WHERE run_id = ? AND canonical_name = ?",
            (store_with_run.run_id, "archived/repo"),
        ).fetchone()
    persisted = json.loads(review_json)
    assert persisted["review"]["canonical_name"] == "archived/repo"
    assert persisted["deterministic_exclusion"] == "archived"
    assert excluded == 1


def test_doctor_reports_config_database_and_github(monkeypatch, capsys, config_path):
    monkeypatch.setattr(
        "github_daily_reporter.cli.probe_github", lambda config: {"ok": True, "remaining": 4999}
    )
    monkeypatch.setattr(
        "github_daily_reporter.cli.probe_hermes",
        lambda: {"ok": True, "timezone": "Asia/Shanghai"},
    )

    assert main(["doctor", "--config", str(config_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["checks"]["timezone_match"] is True
    assert result["checks"]["database_writable"] is True


def test_doctor_fails_on_timezone_mismatch(monkeypatch, capsys, config_path):
    monkeypatch.setattr("github_daily_reporter.cli.probe_github", lambda config: {"ok": True})
    monkeypatch.setattr("github_daily_reporter.cli.probe_hermes", lambda: {"ok": True, "timezone": "UTC"})

    assert main(["doctor", "--config", str(config_path)]) == 2
    assert json.loads(capsys.readouterr().out)["checks"]["timezone_match"] is False


def test_doctor_prints_json_for_malformed_yaml(capsys, config_path):
    config_path.write_text("timezone: [not valid yaml", encoding="utf-8")

    assert main(["doctor", "--config", str(config_path)]) == 2
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["checks"]["config_valid"] is False
    assert captured.err == ""


def test_doctor_prints_json_when_database_initialization_fails(monkeypatch, capsys, config_path):
    def fail_database(_path):
        raise PermissionError("database unavailable")

    monkeypatch.setattr("github_daily_reporter.cli.StateStore", fail_database)
    monkeypatch.setattr("github_daily_reporter.cli.probe_github", lambda config: {"ok": True})
    monkeypatch.setattr(
        "github_daily_reporter.cli.probe_hermes",
        lambda: {"ok": True, "timezone": "Asia/Shanghai"},
    )

    assert main(["doctor", "--config", str(config_path)]) == 2
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["checks"]["database_writable"] is False
    assert captured.err == ""
