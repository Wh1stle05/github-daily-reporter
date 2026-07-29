from pathlib import Path

import pytest

from github_daily_reporter.config import ReporterConfig, load_config


DIRECT_RUNTIME_SECRETS = {
    "llm_api_key": "llm-secret",
    "telegram_bot_token": "bot-secret",
    "telegram_chat_id": "123456",
}


def reporter_config(**overrides: object) -> ReporterConfig:
    values: dict[str, object] = {
        "timezone": "UTC",
        "github_token": "github-secret",
        **DIRECT_RUNTIME_SECRETS,
    }
    values.update(overrides)
    return ReporterConfig.model_validate(values)


def test_load_config_resolves_state_db_and_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "reporter.yaml"
    config_path.write_text(
        "timezone: Asia/Shanghai\nstate_db: data/reporter.sqlite3\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")

    config = load_config(config_path)

    assert config.timezone == "Asia/Shanghai"
    assert config.state_db == tmp_path / "data/reporter.sqlite3"
    assert config.github_token.get_secret_value() == "secret-token"
    assert config.llm_api_key.get_secret_value() == "llm-secret"
    assert config.telegram_bot_token.get_secret_value() == "bot-secret"
    assert config.telegram_chat_id == "123456"
    assert config.llm_model == "gpt-4.1-mini"
    assert config.growth_report_items == 10
    assert config.mature_report_items == 10


def test_load_config_ignores_runtime_secrets_from_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "reporter.yaml"
    config_path.write_text(
        "\n".join(
            (
                "timezone: UTC",
                "llm_api_key: yaml-llm-secret",
                "telegram_bot_token: yaml-bot-secret",
                "telegram_chat_id: yaml-chat-id",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("LLM_API_KEY", "env-llm-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-bot-secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "env-chat-id")

    config = load_config(config_path)

    assert config.llm_api_key.get_secret_value() == "env-llm-secret"
    assert config.telegram_bot_token.get_secret_value() == "env-bot-secret"
    assert config.telegram_chat_id == "env-chat-id"
    serialized = config.model_dump()
    assert "llm_api_key" not in serialized
    assert "telegram_bot_token" not in serialized
    assert "telegram_chat_id" not in serialized


def test_config_rejects_invalid_timezone() -> None:
    with pytest.raises(ValueError, match="IANA timezone"):
        reporter_config(timezone="Mars/Olympus")


def test_config_rejects_report_limit_above_llm_limit() -> None:
    with pytest.raises(ValueError, match="max_report_items"):
        reporter_config(
            max_report_items=11,
            max_llm_candidates=10,
        )


def test_config_allows_ten_items_per_cohort() -> None:
    ReporterConfig(
        timezone="UTC",
        github_token="token",
        growth_report_items=10,
        mature_report_items=10,
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        (
            {"growth_review_candidates": 4, "growth_report_items": 5},
            "growth_review_candidates",
        ),
        (
            {"mature_review_candidates": 2, "mature_report_items": 3},
            "mature_review_candidates",
        ),
    ),
)
def test_config_rejects_review_pool_smaller_than_report(
    overrides: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        reporter_config(**overrides)
