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


def test_probe_assets_detects_a_restored_legacy_wrapper(monkeypatch, tmp_path):
    source = tmp_path / "src" / "github_daily_reporter" / "cli.py"
    source.parent.mkdir(parents=True)
    source.write_text("", encoding="utf-8")
    monkeypatch.setattr(cli, "__file__", str(source))

    assert cli._probe_assets()["legacy_wrapper_absent"] is True

    legacy = tmp_path / "deploy" / "hermes" / "github-daily-run.sh"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    assert cli._probe_assets()["legacy_wrapper_absent"] is False


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
    assert result["checks"]["delivery_database_access"] is True
    assert result["checks"]["telegram_bot_token_present"] is True
    assert result["checks"]["telegram_chat_id_present"] is True
    assert result["checks"]["telegram_thread_id_valid"] is True


def test_doctor_does_not_construct_direct_delivery_clients(
    monkeypatch, capsys, config_path
):
    def unexpected_client(_config):
        raise AssertionError("doctor must not contact direct delivery services")

    monkeypatch.setattr("github_daily_reporter.cli.probe_github", lambda config: {"ok": True})
    monkeypatch.setattr(
        "github_daily_reporter.cli.probe_hermes",
        lambda: {"ok": True, "timezone": "Asia/Shanghai"},
    )
    monkeypatch.setattr(cli, "TelegramClient", unexpected_client, raising=False)

    assert main(["doctor", "--config", str(config_path)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


@pytest.mark.parametrize(
    ("variable", "check"),
    (
        ("TELEGRAM_BOT_TOKEN", "telegram_bot_token_present"),
        ("TELEGRAM_CHAT_ID", "telegram_chat_id_present"),
    ),
)
def test_doctor_rejects_empty_direct_delivery_secret(
    monkeypatch, capsys, config_path, variable, check
):
    monkeypatch.setenv(variable, "   ")
    monkeypatch.setattr("github_daily_reporter.cli.probe_github", lambda config: {"ok": True})
    monkeypatch.setattr(
        "github_daily_reporter.cli.probe_hermes",
        lambda: {"ok": True, "timezone": "Asia/Shanghai"},
    )

    assert main(["doctor", "--config", str(config_path)]) == 2
    assert json.loads(capsys.readouterr().out)["checks"][check] is False


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
    assert result["checks"]["delivery_database_access"] is False
    assert captured.err == ""
