from datetime import UTC, datetime

import pytest

from github_daily_reporter.hybrid import HybridRunError, run_hybrid


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

    with pytest.raises(HybridRunError, match="collection"):
        await run_hybrid(tmp_path / "config.yaml", now=datetime(2026, 7, 29, tzinfo=UTC))
    assert invoked is False


@pytest.mark.asyncio
async def test_agent_timeout_does_not_deliver(monkeypatch, tmp_path):
    delivered = False

    async def agent(*args, **kwargs):
        raise TimeoutError("hermes editorial timed out")

    async def send(*args, **kwargs):
        nonlocal delivered
        delivered = True

    monkeypatch.setattr("github_daily_reporter.hybrid.run_hermes_editorial", agent)
    monkeypatch.setattr("github_daily_reporter.hybrid.deliver_reports", send)

    with pytest.raises(HybridRunError, match="timeout"):
        await run_hybrid(tmp_path / "config.yaml", now=datetime(2026, 7, 29, tzinfo=UTC))
    assert delivered is False
