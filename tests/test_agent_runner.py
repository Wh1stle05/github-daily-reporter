import asyncio
import signal

import pytest

from github_daily_reporter.agent_runner import run_hermes_editorial


class _Process:
    pid = 43210
    returncode = 0

    def __init__(self, *, hanging: bool = False):
        self.hanging = hanging
        self.terminated = []

    async def communicate(self):
        if self.hanging:
            await asyncio.Event().wait()
        return b"done", b""


@pytest.mark.asyncio
async def test_hermes_starts_a_new_session_and_returns_output(monkeypatch, tmp_path):
    process = _Process()
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        return process

    monkeypatch.setattr("github_daily_reporter.agent_runner.asyncio.create_subprocess_exec", fake_exec)
    result = await run_hermes_editorial(tmp_path, "attempt-1", timeout_seconds=1)
    assert captured["start_new_session"] is True
    assert result.returncode == 0
    assert result.stdout == "done"


@pytest.mark.asyncio
async def test_hermes_timeout_terminates_then_kills_process_group(monkeypatch, tmp_path):
    process = _Process(hanging=True)
    signals = []

    async def fake_exec(*args, **kwargs):
        return process

    monkeypatch.setattr("github_daily_reporter.agent_runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("github_daily_reporter.agent_runner.os.killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr("github_daily_reporter.agent_runner.TERMINATION_GRACE_SECONDS", 0)

    with pytest.raises(TimeoutError, match="timed out"):
        await run_hermes_editorial(tmp_path, "attempt-1", timeout_seconds=0.01)
    assert (process.pid, signal.SIGTERM) in signals
    assert (process.pid, signal.SIGKILL) in signals
