from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from github_daily_reporter.hybrid import HybridRunError, run_hybrid
from github_daily_reporter.models import CollectionEnvelope
from github_daily_reporter.telegram import DeliveryResult


def _config(tmp_path):
    return SimpleNamespace(
        project_root=tmp_path,
        state_db=tmp_path / "data" / "reporter.sqlite3",
        github_token=SimpleNamespace(get_secret_value=lambda: "token"),
        request_timeout_seconds=1,
        hermes_timeout_seconds=1,
        telegram_bot_token="",
        telegram_chat_id="",
        telegram_message_thread_id=None,
        telegram_timeout_seconds=1,
        telegram_max_attempts=1,
        telegram_retry_base_seconds=0,
    )


@pytest.mark.asyncio
async def test_collection_failure_does_not_invoke_agent(monkeypatch, tmp_path):
    invoked = False

    async def collect(*args, **kwargs):
        raise RuntimeError("collection failed")

    async def agent(*args, **kwargs):
        nonlocal invoked
        invoked = True

    monkeypatch.setattr("github_daily_reporter.hybrid.collect_for_hybrid", collect)
    monkeypatch.setattr("github_daily_reporter.hybrid.run_hermes_editorial", agent)
    monkeypatch.setattr("github_daily_reporter.hybrid.load_config", lambda _: _config(tmp_path))

    with pytest.raises(HybridRunError, match="collection"):
        await run_hybrid(tmp_path / "config.yaml", now=datetime(2026, 7, 29, tzinfo=UTC))
    assert invoked is False


@pytest.mark.asyncio
async def test_agent_timeout_does_not_deliver(monkeypatch, tmp_path):
    delivered = False

    async def agent(*args, **kwargs):
        raise TimeoutError("hermes editorial timed out")

    async def collect(*args, **kwargs):
        return CollectionEnvelope(
            run_id="run-1",
            status="success",
            generated_at=datetime(2026, 7, 29, tzinfo=UTC),
            source_health=[],
            candidates=[],
        )

    async def send(*args, **kwargs):
        nonlocal delivered
        delivered = True

    monkeypatch.setattr("github_daily_reporter.hybrid.run_hermes_editorial", agent)
    monkeypatch.setattr("github_daily_reporter.hybrid.deliver_reports", send)
    monkeypatch.setattr("github_daily_reporter.hybrid.collect_for_hybrid", collect)
    monkeypatch.setattr("github_daily_reporter.hybrid.load_config", lambda _: _config(tmp_path))

    with pytest.raises(HybridRunError, match="timeout"):
        await run_hybrid(tmp_path / "config.yaml", now=datetime(2026, 7, 29, tzinfo=UTC))
    assert delivered is False


@pytest.mark.asyncio
async def test_successful_hybrid_delivers_growth_before_mature(monkeypatch, tmp_path):
    config = _config(tmp_path)
    run_dir = tmp_path / "data" / "runs" / "github-daily-report-2026-07-29"
    envelope = CollectionEnvelope(
        run_id="collection-run",
        status="success",
        generated_at=datetime(2026, 7, 29, tzinfo=UTC),
        source_health=[],
        candidates=[],
    )
    handoff = SimpleNamespace(run_id=run_dir.name)
    order = []

    async def collect(*args, **kwargs):
        return envelope

    async def agent(run_path, attempt_id, **kwargs):
        attempt = run_path / "attempts" / attempt_id
        attempt.mkdir(parents=True, exist_ok=True)
        (attempt / "growth-report.md").write_text("growth", encoding="utf-8")
        (attempt / "mature-report.md").write_text("mature", encoding="utf-8")

    def promote(run_path, supplied_handoff, attempt_id):
        attempt = run_path / "attempts" / attempt_id
        (run_path / "growth-report.md").write_text("growth", encoding="utf-8")
        (run_path / "mature-report.md").write_text("mature", encoding="utf-8")

    async def deliver(store, telegram, run_id, parts):
        order.extend(body for _, body in parts)
        return DeliveryResult("delivered", [0, 1])

    monkeypatch.setattr("github_daily_reporter.hybrid.load_config", lambda _: config)
    monkeypatch.setattr("github_daily_reporter.hybrid.collect_for_hybrid", collect)
    monkeypatch.setattr("github_daily_reporter.hybrid._load_or_build_handoff", lambda *args: handoff)
    monkeypatch.setattr("github_daily_reporter.hybrid.run_hermes_editorial", agent)
    monkeypatch.setattr("github_daily_reporter.hybrid.promote_reports", promote)
    monkeypatch.setattr("github_daily_reporter.hybrid.deliver_reports", deliver)

    outcome = await run_hybrid(tmp_path / "config.yaml", now=datetime(2026, 7, 29, tzinfo=UTC))
    assert outcome.status == "delivered"
    assert order == ["growth", "mature"]


@pytest.mark.asyncio
async def test_same_day_delivered_run_is_not_sent_again(monkeypatch, tmp_path):
    config = _config(tmp_path)
    run_dir = tmp_path / "data" / "runs" / "github-daily-report-2026-07-29"
    run_dir.mkdir(parents=True)
    (run_dir / "run-status.json").write_text(
        '{"status":"delivered","growth_count":10,"mature_count":10}', encoding="utf-8"
    )
    invoked = False

    async def agent(*args, **kwargs):
        nonlocal invoked
        invoked = True

    monkeypatch.setattr("github_daily_reporter.hybrid.load_config", lambda _: config)
    monkeypatch.setattr("github_daily_reporter.hybrid.run_hermes_editorial", agent)
    outcome = await run_hybrid(tmp_path / "config.yaml", now=datetime(2026, 7, 29, tzinfo=UTC))
    assert outcome.status == "delivered"
    assert invoked is False
