from pathlib import Path

import pytest

from github_daily_reporter.config import ReporterConfig, load_config


def test_load_config_resolves_state_db_and_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "reporter.yaml"
    config_path.write_text(
        "timezone: Asia/Shanghai\nstate_db: data/reporter.sqlite3\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    config = load_config(config_path)

    assert config.timezone == "Asia/Shanghai"
    assert config.state_db == tmp_path / "data/reporter.sqlite3"
    assert config.github_token.get_secret_value() == "secret-token"


def test_config_rejects_invalid_timezone() -> None:
    with pytest.raises(ValueError, match="IANA timezone"):
        ReporterConfig(timezone="Mars/Olympus", github_token="token")


def test_config_rejects_report_limit_above_llm_limit() -> None:
    with pytest.raises(ValueError, match="max_report_items"):
        ReporterConfig(
            timezone="UTC",
            github_token="token",
            max_report_items=11,
            max_llm_candidates=10,
        )
